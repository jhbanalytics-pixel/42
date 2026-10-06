"""Immutable funded-lane control receipts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from src.analysis.open_intelligence.funded_lane import (
    FUNDED_CREDENTIAL_LANE,
    MONTHLY_CAP,
    OPENING_BALANCE,
    RESERVE_FLOOR,
    STAGE_CAPS,
    FundedLaneGateResult,
    approved_wave1_catalog_pair,
)
from src.contracts.open_intelligence import encode_identifier_part

CONTROL_CONTRACT_VERSION = "socialcrawl_funded_control_v1"
FUNDED_ACCOUNT = "ogilvy_albert"
FUNDED_CONTROL_TABLE = "socialcrawl_funded_control_receipts_v1"
FUNDED_CONTROL_VIEW = "v_socialcrawl_funded_budget_v1"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
_KILL_STATES = frozenset({"not_tested", "passed", "failed", "killed"})
_ATTRIBUTION_STATES = frozenset({"complete", "conservative", "gap_detected"})


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be a UTC timestamp")
    return value.astimezone(UTC)


def _credit(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{field} must be a nonnegative Decimal")
    return value


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _canonical(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return value
    raise ValueError("control receipt contains an unsupported canonical value")


def _control_id(values: dict[str, object]) -> str:
    canonical = "".join(
        encode_identifier_part(_canonical(value))
        for field, value in values.items()
        if field not in {"control_id", "created_at"}
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FundedControlReceipt:
    control_contract_version: str
    control_id: str
    recorded_at: datetime
    month_start: date
    credential_lane: str
    funding_account: str
    activation_stage: int
    kill_state: str
    current_balance: Decimal
    balance_observed_at: datetime
    opening_balance: Decimal
    month_opening_balance: Decimal
    monthly_cap: Decimal
    reserve_floor: Decimal
    stage_cap: Decimal
    monthly_ledger_debit: Decimal
    monthly_balance_delta: Decimal
    monthly_effective_spend: Decimal
    monthly_remaining: Decimal
    reserve_remaining: Decimal
    run_allowance: Decimal
    attribution_state: str
    consecutive_complete_runs: int
    runs_today: int
    catalog_digest: str
    metadata_digest: str
    source_sha: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.control_contract_version != CONTROL_CONTRACT_VERSION:
            raise ValueError("control contract version is unsupported")
        recorded_at = _utc(self.recorded_at, "recorded at")
        balance_at = _utc(self.balance_observed_at, "balance observed at")
        created_at = _utc(self.created_at, "created at")
        if created_at != recorded_at or balance_at > recorded_at:
            raise ValueError("control receipt timestamps are inconsistent")
        if isinstance(self.month_start, datetime) or not isinstance(self.month_start, date):
            raise ValueError("month start must be a date")
        if self.month_start != date(recorded_at.year, recorded_at.month, 1):
            raise ValueError("month start does not match the UTC calendar month")
        if self.credential_lane != FUNDED_CREDENTIAL_LANE:
            raise ValueError("credential lane is not approved")
        if self.funding_account != FUNDED_ACCOUNT:
            raise ValueError("funding account is not approved")
        stage = _integer(self.activation_stage, "activation stage")
        if stage not in STAGE_CAPS:
            raise ValueError("activation stage is unsupported")
        if self.kill_state not in _KILL_STATES:
            raise ValueError("kill state is unsupported")
        if self.attribution_state not in _ATTRIBUTION_STATES:
            raise ValueError("attribution state is unsupported")
        current = _credit(self.current_balance, "current balance")
        opening = _credit(self.opening_balance, "opening balance")
        month_opening = _credit(self.month_opening_balance, "month opening balance")
        cap = _credit(self.monthly_cap, "monthly cap")
        reserve_floor = _credit(self.reserve_floor, "reserve floor")
        stage_cap = _credit(self.stage_cap, "stage cap")
        ledger = _credit(self.monthly_ledger_debit, "monthly ledger debit")
        balance_delta = _credit(self.monthly_balance_delta, "monthly balance delta")
        effective = _credit(self.monthly_effective_spend, "monthly effective spend")
        remaining = _credit(self.monthly_remaining, "monthly remaining")
        reserve = _credit(self.reserve_remaining, "reserve remaining")
        allowance = _credit(self.run_allowance, "run allowance")
        if opening != OPENING_BALANCE or cap != MONTHLY_CAP or reserve_floor != RESERVE_FLOOR:
            raise ValueError("control receipt financial constants are not approved")
        if stage_cap != STAGE_CAPS[stage]:
            raise ValueError("stage cap does not match activation stage")
        expected_delta = month_opening - current if current <= month_opening else None
        if expected_delta is None or balance_delta != expected_delta:
            raise ValueError("monthly balance delta is inconsistent")
        expected_effective = max(ledger, balance_delta)
        expected_remaining = max(Decimal("0"), cap - expected_effective)
        expected_reserve = max(Decimal("0"), current - reserve_floor)
        expected_allowance = min(stage_cap, expected_remaining, expected_reserve)
        if (
            effective != expected_effective
            or remaining != expected_remaining
            or reserve != expected_reserve
            or allowance != expected_allowance
        ):
            raise ValueError("control receipt funded-lane formulas disagree")
        _integer(self.consecutive_complete_runs, "consecutive complete runs")
        _integer(self.runs_today, "runs today")
        if (
            not _DIGEST.fullmatch(self.catalog_digest)
            or not _DIGEST.fullmatch(self.metadata_digest)
            or not approved_wave1_catalog_pair(self.catalog_digest, self.metadata_digest)
        ):
            raise ValueError("catalog identity is not approved")
        if not isinstance(self.source_sha, str) or not _SOURCE_SHA.fullmatch(self.source_sha):
            raise ValueError("source SHA must be a lower case full commit SHA")
        values = {field.name: getattr(self, field.name) for field in fields(self)}
        if not _DIGEST.fullmatch(self.control_id) or self.control_id != _control_id(values):
            raise ValueError("control ID does not match canonical receipt content")


def build_funded_control_receipt(
    *,
    gate: FundedLaneGateResult,
    recorded_at: datetime,
    balance_observed_at: datetime,
    source_sha: str,
    kill_state: str,
) -> FundedControlReceipt:
    if not isinstance(gate, FundedLaneGateResult):
        raise ValueError("funded lane gate is invalid")
    if gate.current_balance is None or gate.monthly_balance_delta is None:
        raise ValueError("funded control receipt requires a current balance")
    recorded_at = _utc(recorded_at, "recorded at")
    balance_observed_at = _utc(balance_observed_at, "balance observed at")
    values: dict[str, object] = {
        "control_contract_version": CONTROL_CONTRACT_VERSION,
        "control_id": "",
        "recorded_at": recorded_at,
        "month_start": date(recorded_at.year, recorded_at.month, 1),
        "credential_lane": FUNDED_CREDENTIAL_LANE,
        "funding_account": FUNDED_ACCOUNT,
        "activation_stage": gate.activation_stage,
        "kill_state": kill_state,
        "current_balance": gate.current_balance,
        "balance_observed_at": balance_observed_at,
        "opening_balance": OPENING_BALANCE,
        "month_opening_balance": gate.month_opening_balance,
        "monthly_cap": MONTHLY_CAP,
        "reserve_floor": RESERVE_FLOOR,
        "stage_cap": gate.stage_cap,
        "monthly_ledger_debit": gate.monthly_ledger_debit,
        "monthly_balance_delta": gate.monthly_balance_delta,
        "monthly_effective_spend": gate.monthly_effective_spend,
        "monthly_remaining": gate.monthly_remaining,
        "reserve_remaining": gate.reserve_remaining,
        "run_allowance": gate.run_allowance,
        "attribution_state": gate.attribution_state,
        "consecutive_complete_runs": gate.consecutive_complete_runs,
        "runs_today": gate.runs_today,
        "catalog_digest": gate.catalog_digest,
        "metadata_digest": gate.metadata_digest,
        "source_sha": source_sha,
        "created_at": recorded_at,
    }
    values["control_id"] = _control_id(values)
    return FundedControlReceipt(**values)
