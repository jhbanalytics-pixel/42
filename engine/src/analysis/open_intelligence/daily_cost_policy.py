"""The daily workload cost policy the derive routine charges each derivation against.

``sp_derive_open_intelligence_daily_execution_v1`` reads a ``cost_policy`` input artifact
of contract ``daily_workload_cost_policy_v1`` and reserves
``operation_reservations_micro_usd[operation]`` against the recurring grant's run and
monthly allowances. It accepts the policy only while ``reviewed_at <= now < expires_at``
and the two are at most 24 hours apart.

Nothing in the policy is supplied as a number to trust. Each reservation is computed:

* the caps are the approved registry contract's limits for the operation the daily
  operation maps to (``daily_operation_map``); a contract range whose maximum is the
  unbounded sentinel refuses instead of being replaced by a guess;
* the rates are one observed record (``daily_workload_rates_v1``) with https pricing
  sources and no zero rate, observed no more than 24 hours ago and not in the future,
  following the capture price observation rule in ``staging_source_profile``. The
  record's ``observed_at`` is declared by whoever wrote it, so the provider's storage time
  of the record bounds it: an observation after the record was stored, or a record stored
  after now, refuses;
* the administrative reservation is the query cost of the billed byte bound the
  caller's protected routine budget enforces.

reservation = ceil(max_bytes_billed * query_micro_usd_per_tib / 2**40)
            + max_credits * vendor_credit_micro_usd
            + max_model_calls * model_call_micro_usd
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from .daily_operation_map import ChildBinding, DailyOperationRefusal
from .staging_source_profile import PRICE_OBSERVATION_VALIDITY

RATES_CONTRACT = "daily_workload_rates_v1"
RATES_OBJECT = "42/daily/workload-rates/current.json"
MAX_RATES_BYTES = 65536
POLICY_CONTRACT = "daily_workload_cost_policy_v1"
VALIDITY = PRICE_OBSERVATION_VALIDITY
RATE_FIELDS = frozenset(
    {
        "contract_version",
        "observed_at",
        "pricing_sources",
        "query_micro_usd_per_tib",
        "vendor_credit_micro_usd",
        "model_call_micro_usd",
    }
)
_PRICE_FIELDS = ("query_micro_usd_per_tib", "vendor_credit_micro_usd", "model_call_micro_usd")
POLICY_FIELDS = frozenset(
    {
        "administrative_reservation_micro_usd",
        "assumptions",
        "contract_version",
        "expires_at",
        "job_policy_digest",
        "operation_reservations_micro_usd",
        "pricing_sources",
        "reviewed_at",
    }
)
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_TIB = 2**40


class DailyCostPolicyRefusal(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _refuse(code: str) -> None:
    raise DailyCostPolicyRefusal(code)


def _instant(value: object, code: str) -> datetime:
    if not isinstance(value, str):
        _refuse(code)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise DailyCostPolicyRefusal(code) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _refuse(code)
    return parsed


def _now(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _refuse("daily_cost_policy_invalid")
    return value


def _sources(value: object, code: str) -> list[str]:
    if type(value) is not list or not value:
        _refuse(code)
    for url in value:
        parts = urlsplit(url) if isinstance(url, str) else None
        if (
            parts is None
            or parts.scheme != "https"
            or not parts.netloc
            or parts.username is not None
            or parts.password is not None
        ):
            _refuse(code)
    return sorted(set(value))


def _count(value: object, code: str, floor: int = 0) -> int:
    if type(value) is not int or value < floor:
        _refuse(code)
    return value


def validate_workload_rates(record: object, *, now: datetime, stored_at: datetime) -> dict:
    """The observed rate record, or a refusal; stale, future and unwitnessed ones refuse.

    ``stored_at`` is the provider's storage time of the record, read with it.
    """
    code = "daily_cost_rates_invalid"
    current = _now(now)
    if (
        not isinstance(stored_at, datetime)
        or stored_at.tzinfo is None
        or stored_at.utcoffset() is None
    ):
        _refuse(code)
    if (
        not isinstance(record, Mapping)
        or set(record) != RATE_FIELDS
        or record["contract_version"] != RATES_CONTRACT
    ):
        _refuse(code)
    observed = _instant(record["observed_at"], code)
    checked = {
        "observed_at": observed,
        "pricing_sources": _sources(record["pricing_sources"], code),
    }
    for field in _PRICE_FIELDS:
        # A zero rate would price the operation at nothing; it is not an observation.
        checked[field] = _count(record[field], code, floor=1)
    if observed > current:
        _refuse("daily_cost_rates_future")
    if current - observed >= VALIDITY:
        _refuse("daily_cost_rates_stale")
    if observed > stored_at or stored_at > current:
        _refuse("daily_cost_rates_unwitnessed")
    return checked


def _reservation(limits: Mapping[str, int], rates: Mapping[str, object]) -> int:
    query = -(-limits["max_bytes_billed"] * rates["query_micro_usd_per_tib"] // _TIB)
    return (
        query
        + limits["max_credits"] * rates["vendor_credit_micro_usd"]
        + limits["max_model_calls"] * rates["model_call_micro_usd"]
    )


def build_daily_cost_policy(
    *,
    rates: object,
    bindings: Mapping[str, ChildBinding],
    administrative_bytes_billed: int,
    job_policy_digest: str,
    now: datetime,
    stored_at: datetime,
) -> dict:
    """Compute the cost policy for the daily operations in ``bindings``."""
    invalid = "daily_cost_policy_invalid"
    checked = validate_workload_rates(rates, now=now, stored_at=stored_at)
    if (
        not isinstance(bindings, Mapping)
        or not bindings
        or type(administrative_bytes_billed) is not int
        or administrative_bytes_billed <= 0
        or not isinstance(job_policy_digest, str)
        or _HEX_64.fullmatch(job_policy_digest) is None
    ):
        _refuse(invalid)
    reservations: dict[str, int] = {}
    assumptions = {f"{field}={checked[field]} observed rate" for field in _PRICE_FIELDS} | {
        f"administrative_bytes_billed={administrative_bytes_billed} billed byte bound of the"
        " protected routine budget, charged at the observed query rate",
        f"observed_at={checked['observed_at'].isoformat()} rate observation instant",
    }
    for operation, binding in sorted(bindings.items()):
        if type(binding) is not ChildBinding or binding.operation != operation:
            _refuse(invalid)
        try:
            limits = binding.bounded_limits()
        except DailyOperationRefusal as error:
            raise DailyCostPolicyRefusal(error.code) from error
        reservations[operation] = _reservation(limits, checked)
        assumptions |= {
            f"{operation} {name}={value} contract cap of {binding.registry_operation}"
            f" under contract_sha256={binding.contract_sha256}"
            for name, value in limits.items()
        }
    observed = checked["observed_at"]
    policy = {
        "administrative_reservation_micro_usd": -(
            -administrative_bytes_billed * checked["query_micro_usd_per_tib"] // _TIB
        ),
        "assumptions": sorted(assumptions),
        "contract_version": POLICY_CONTRACT,
        "expires_at": (observed + VALIDITY).isoformat(),
        "job_policy_digest": job_policy_digest,
        "operation_reservations_micro_usd": reservations,
        "pricing_sources": checked["pricing_sources"],
        "reviewed_at": observed.isoformat(),
    }
    return validate_daily_cost_policy(policy, now=now)


def validate_daily_cost_policy(policy: object, *, now: datetime) -> dict:
    """The derive routine's acceptance of a cost policy, applied before any derive call."""
    invalid = "daily_cost_policy_invalid"
    current = _now(now)
    if (
        not isinstance(policy, Mapping)
        or set(policy) != POLICY_FIELDS
        or policy["contract_version"] != POLICY_CONTRACT
        or not isinstance(policy["job_policy_digest"], str)
        or _HEX_64.fullmatch(policy["job_policy_digest"]) is None
        or type(policy["assumptions"]) is not list
        or not all(isinstance(item, str) and item for item in policy["assumptions"])
    ):
        _refuse(invalid)
    _count(policy["administrative_reservation_micro_usd"], invalid)
    reservations = policy["operation_reservations_micro_usd"]
    if not isinstance(reservations, Mapping) or not reservations:
        _refuse(invalid)
    for name, value in reservations.items():
        if not isinstance(name, str) or not name:
            _refuse(invalid)
        _count(value, invalid)
    _sources(policy["pricing_sources"], invalid)
    reviewed = _instant(policy["reviewed_at"], invalid)
    expires = _instant(policy["expires_at"], invalid)
    if expires <= reviewed or expires - reviewed > timedelta(hours=24):
        _refuse(invalid)
    if not reviewed <= current < expires:
        _refuse("daily_cost_policy_expired")
    return dict(policy)


__all__ = [
    "MAX_RATES_BYTES",
    "POLICY_CONTRACT",
    "RATES_CONTRACT",
    "RATES_OBJECT",
    "DailyCostPolicyRefusal",
    "build_daily_cost_policy",
    "validate_daily_cost_policy",
    "validate_workload_rates",
]
