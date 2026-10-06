"""Immutable terminal events for funded SocialCrawl executions."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.analysis.open_intelligence.funded_control import FUNDED_ACCOUNT
from src.analysis.open_intelligence.funded_lane import FUNDED_CREDENTIAL_LANE
from src.contracts.open_intelligence import encode_identifier_part

FUNDED_TERMINAL_CONTRACT_VERSION = "socialcrawl_funded_terminal_event_v1"
FUNDED_TERMINAL_TABLE = "socialcrawl_funded_terminal_events_v1"
FUNDED_BUDGET_V2_VIEW = "v_socialcrawl_funded_budget_v2"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
_BALANCE_STATUSES = frozenset({"measured", "unavailable"})
_ATTRIBUTION_STATES = frozenset({"complete", "conservative", "gap_detected"})
_REASON_ORDER = (
    "post_balance_unavailable",
    "vendor_overage",
    "attribution_gap",
    "route_cap_breach",
    "phase_cap_breach",
    "quoted_run_cap_breach",
    "monthly_cap_breach",
    "reserve_floor_breach",
    "immutable_conflict",
    "authority_mismatch",
    "source_value_failed",
    "gdelt_failed",
)


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be a UTC timestamp")
    return value.astimezone(UTC)


def _credit(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{field} must be a nonnegative Decimal")
    return value


def _canonical(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, str):
        return value
    if isinstance(value, tuple):
        return ",".join(value)
    raise ValueError("terminal event contains an unsupported canonical value")


def _terminal_id(values: dict[str, object]) -> str:
    canonical = "".join(
        encode_identifier_part(_canonical(value))
        for field, value in values.items()
        if field not in {"terminal_id", "created_at"}
    )
    return "scte_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FundedTerminalEvent:
    terminal_contract_version: str
    terminal_id: str
    execution_id: str
    run_id: str
    credential_lane: str
    funding_account: str
    recorded_at: datetime
    balance_read_status: str
    last_measured_balance: Decimal
    last_balance_observed_at: datetime
    authorized_quoted_debit: Decimal
    vendor_reported_debit: Decimal
    overage_debit: Decimal
    ledger_debit: Decimal
    attribution_state: str
    kill_state: str
    reason_codes: tuple[str, ...]
    source_sha: str
    manifest_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.terminal_contract_version != FUNDED_TERMINAL_CONTRACT_VERSION:
            raise ValueError("terminal contract version is unsupported")
        if not isinstance(self.execution_id, str) or not self.execution_id.strip():
            raise ValueError("execution ID is invalid")
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run ID is invalid")
        if self.credential_lane != FUNDED_CREDENTIAL_LANE or self.funding_account != FUNDED_ACCOUNT:
            raise ValueError("terminal funding identity is not approved")
        recorded_at = _utc(self.recorded_at, "recorded at")
        balance_at = _utc(self.last_balance_observed_at, "last balance observed at")
        created_at = _utc(self.created_at, "created at")
        if created_at != recorded_at or balance_at > recorded_at:
            raise ValueError("terminal timestamps are inconsistent")
        if self.balance_read_status not in _BALANCE_STATUSES:
            raise ValueError("balance read status is unsupported")
        last_balance = _credit(self.last_measured_balance, "last measured balance")
        quoted = _credit(self.authorized_quoted_debit, "authorized quoted debit")
        vendor = _credit(self.vendor_reported_debit, "vendor reported debit")
        overage = _credit(self.overage_debit, "overage debit")
        ledger = _credit(self.ledger_debit, "ledger debit")
        if overage != max(Decimal("0"), vendor - quoted):
            raise ValueError("terminal overage formula disagrees")
        if ledger != max(quoted, vendor):
            raise ValueError("terminal ledger debit does not preserve known spend")
        if self.attribution_state not in _ATTRIBUTION_STATES:
            raise ValueError("terminal attribution state is unsupported")
        if self.kill_state != "killed":
            raise ValueError("terminal event must be killed")
        if not isinstance(self.reason_codes, tuple) or not self.reason_codes:
            raise ValueError("terminal reason codes are required")
        expected_reasons = tuple(reason for reason in _REASON_ORDER if reason in self.reason_codes)
        if self.reason_codes != expected_reasons or len(set(self.reason_codes)) != len(
            self.reason_codes
        ):
            raise ValueError("terminal reason codes are invalid or out of order")
        if (
            self.balance_read_status == "unavailable"
            and "post_balance_unavailable" not in self.reason_codes
        ):
            raise ValueError("unavailable balance requires its terminal reason")
        if overage > 0 and "vendor_overage" not in self.reason_codes:
            raise ValueError("vendor overage requires its terminal reason")
        if not isinstance(last_balance, Decimal):
            raise ValueError("last measured balance is invalid")
        if not isinstance(self.source_sha, str) or _SOURCE_SHA.fullmatch(self.source_sha) is None:
            raise ValueError("source SHA must be a lower case full commit SHA")
        if (
            not isinstance(self.manifest_sha256, str)
            or _DIGEST.fullmatch(self.manifest_sha256) is None
        ):
            raise ValueError("manifest SHA256 is invalid")
        values = {field.name: getattr(self, field.name) for field in fields(self)}
        if self.terminal_id != _terminal_id(values):
            raise ValueError("terminal ID does not match canonical event content")


def build_funded_terminal_event(
    *,
    execution_id: str,
    run_id: str,
    recorded_at: datetime,
    balance_read_status: str,
    last_measured_balance: Decimal,
    last_balance_observed_at: datetime,
    authorized_quoted_debit: Decimal,
    vendor_reported_debit: Decimal,
    attribution_state: str,
    reason_codes: tuple[str, ...],
    source_sha: str,
    manifest_sha256: str,
) -> FundedTerminalEvent:
    recorded_at = _utc(recorded_at, "recorded at")
    values: dict[str, object] = {
        "terminal_contract_version": FUNDED_TERMINAL_CONTRACT_VERSION,
        "terminal_id": "",
        "execution_id": execution_id,
        "run_id": run_id,
        "credential_lane": FUNDED_CREDENTIAL_LANE,
        "funding_account": FUNDED_ACCOUNT,
        "recorded_at": recorded_at,
        "balance_read_status": balance_read_status,
        "last_measured_balance": last_measured_balance,
        "last_balance_observed_at": last_balance_observed_at,
        "authorized_quoted_debit": authorized_quoted_debit,
        "vendor_reported_debit": vendor_reported_debit,
        "overage_debit": max(Decimal("0"), vendor_reported_debit - authorized_quoted_debit),
        "ledger_debit": max(authorized_quoted_debit, vendor_reported_debit),
        "attribution_state": attribution_state,
        "kill_state": "killed",
        "reason_codes": reason_codes,
        "source_sha": source_sha,
        "manifest_sha256": manifest_sha256,
        "created_at": recorded_at,
    }
    values["terminal_id"] = _terminal_id(values)
    return FundedTerminalEvent(**values)
