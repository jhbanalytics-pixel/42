from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation


CONTRACT_VERSION = "credit_budget_v2"
MONTHLY_CAP = Decimal("25000")
OPENING_BALANCE = Decimal("250100")
RESERVE_FLOOR = Decimal("225000")
STAGE_CAPS = {
    0: Decimal("0"),
    1: Decimal("100"),
    2: Decimal("250"),
    3: Decimal("750"),
}
AUTHORITY_FIELDS = {
    "credential_lane",
    "funding_account",
    "activation_stage",
    "opening_balance",
    "current_balance",
    "month_opening_balance",
    "balance_observed_at",
    "kill_state",
    "ledger_rows",
}
LEDGER_FIELDS = {
    "credential_lane",
    "phase",
    "event_type",
    "recorded_at",
    "budget_debit_credits",
    "attribution_state",
}
PHASES = {
    "preflight",
    "discover",
    "creators",
    "search",
    "reddit",
    "accounts",
    "news",
    "facebook",
    "run_close",
    "unattributed",
}
EVENT_TYPES = {"preflight", "phase_close", "run_close", "attribution_gap"}
ATTRIBUTION_STATES = {"complete", "conservative", "gap_detected"}
KILL_STATES = {"not_tested", "passed", "failed", "killed"}


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("credit_budget_authority_invalid")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("credit_budget_authority_invalid") from error
    if not result.is_finite() or result < 0:
        raise ValueError("credit_budget_authority_invalid")
    return result


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("credit_budget_authority_invalid")
    return value.astimezone(UTC)


def _iso_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _jhb_lane() -> dict[str, object]:
    return {
        "credential_lane": "jhb_core",
        "funding_account": "jhb_analytics",
        "activation_stage": None,
        "opening_balance": None,
        "current_balance": None,
        "month_opening_balance": None,
        "monthly_cap": None,
        "monthly_ledger_debit": None,
        "monthly_balance_delta": None,
        "monthly_effective_spend": None,
        "monthly_remaining": None,
        "reserve_floor": None,
        "reserve_remaining": None,
        "stage_cap": None,
        "run_allowance": None,
        "attribution_state": "unavailable",
        "kill_state": "not_applicable",
        "ledger_through": None,
        "limitation": "This credential remains operationally separate and has no funded-lane budget authority.",
    }


def _unknown_funded_lane() -> dict[str, object]:
    return {
        "credential_lane": "ogilvy_funded",
        "funding_account": "ogilvy_albert",
        "activation_stage": None,
        "opening_balance": None,
        "current_balance": None,
        "month_opening_balance": None,
        "monthly_cap": MONTHLY_CAP,
        "monthly_ledger_debit": None,
        "monthly_balance_delta": None,
        "monthly_effective_spend": None,
        "monthly_remaining": None,
        "reserve_floor": RESERVE_FLOOR,
        "reserve_remaining": None,
        "stage_cap": None,
        "run_allowance": None,
        "attribution_state": "unavailable",
        "kill_state": "not_tested",
        "ledger_through": None,
        "limitation": "Paid calls remain disabled until the approved activation gate passes.",
    }


def _reading(state: str, month_start: str, funded: dict[str, object]) -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "state": state,
        "month_start": month_start,
        "unit": "vendor_credits",
        "lanes": [funded, _jhb_lane()],
        "limitation": "Enumeration is not enablement. A positive allowance does not authorize a source or a run.",
    }


def _project(authority: dict[str, object], now: datetime) -> dict:
    if set(authority) != AUTHORITY_FIELDS:
        raise ValueError("credit_budget_authority_invalid")
    if (
        authority["credential_lane"] != "ogilvy_funded"
        or authority["funding_account"] != "ogilvy_albert"
        or authority["activation_stage"] not in STAGE_CAPS
        or authority["kill_state"] not in KILL_STATES
        or not isinstance(authority["ledger_rows"], list)
    ):
        raise ValueError("credit_budget_authority_invalid")

    opening_balance = _decimal(authority["opening_balance"])
    current_balance = _decimal(authority["current_balance"])
    month_opening_balance = _decimal(authority["month_opening_balance"])
    observed_at = _utc(authority["balance_observed_at"])
    if opening_balance != OPENING_BALANCE:
        raise ValueError("credit_budget_authority_invalid")

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (
        month_start.replace(year=month_start.year + 1, month=1)
        if month_start.month == 12
        else month_start.replace(month=month_start.month + 1)
    )
    monthly_rows = []
    for row in authority["ledger_rows"]:
        if (
            not isinstance(row, dict)
            or set(row) != LEDGER_FIELDS
            or row["credential_lane"] not in {"ogilvy_funded", "jhb_core"}
            or row["phase"] not in PHASES
            or row["event_type"] not in EVENT_TYPES
            or row["attribution_state"] not in ATTRIBUTION_STATES
        ):
            raise ValueError("credit_budget_authority_invalid")
        recorded_at = _utc(row["recorded_at"])
        debit = _decimal(row["budget_debit_credits"])
        if (
            row["credential_lane"] == "ogilvy_funded"
            and month_start <= recorded_at < next_month
        ):
            monthly_rows.append((row, recorded_at, debit))

    if current_balance > month_opening_balance or month_opening_balance > opening_balance:
        raise ValueError("credit_budget_authority_invalid")

    ledger_debit = sum(
        (
            debit
            for row, _recorded_at, debit in monthly_rows
            if row["event_type"] in {"phase_close", "attribution_gap"}
        ),
        Decimal("0"),
    )
    balance_delta = month_opening_balance - current_balance
    effective_spend = max(ledger_debit, balance_delta)
    monthly_remaining = max(Decimal("0"), MONTHLY_CAP - effective_spend)
    reserve_remaining = max(Decimal("0"), current_balance - RESERVE_FLOOR)
    stage_cap = STAGE_CAPS[authority["activation_stage"]]
    run_allowance = min(stage_cap, monthly_remaining, reserve_remaining)

    attribution_state = "complete"
    if any(
        row["event_type"] == "attribution_gap"
        or row["attribution_state"] == "gap_detected"
        for row, _recorded_at, _debit in monthly_rows
    ):
        attribution_state = "gap_detected"
    elif any(
        row["attribution_state"] == "conservative"
        for row, _recorded_at, _debit in monthly_rows
    ):
        attribution_state = "conservative"

    if monthly_remaining == 0 or reserve_remaining == 0:
        state = "exhausted"
    elif (
        attribution_state == "complete"
        and authority["kill_state"] == "passed"
        and stage_cap > 0
        and run_allowance == stage_cap
    ):
        state = "ready"
    else:
        state = "blocked"

    through = max([observed_at, *(recorded for _row, recorded, _debit in monthly_rows)])
    funded = {
        "credential_lane": "ogilvy_funded",
        "funding_account": "ogilvy_albert",
        "activation_stage": authority["activation_stage"],
        "opening_balance": opening_balance,
        "current_balance": current_balance,
        "month_opening_balance": month_opening_balance,
        "monthly_cap": MONTHLY_CAP,
        "monthly_ledger_debit": ledger_debit,
        "monthly_balance_delta": balance_delta,
        "monthly_effective_spend": effective_spend,
        "monthly_remaining": monthly_remaining,
        "reserve_floor": RESERVE_FLOOR,
        "reserve_remaining": reserve_remaining,
        "stage_cap": stage_cap,
        "run_allowance": run_allowance,
        "attribution_state": attribution_state,
        "kill_state": authority["kill_state"],
        "ledger_through": _iso_z(through),
        "limitation": "Paid calls remain disabled until the approved activation gate passes.",
    }
    return _reading(state, month_start.date().isoformat(), funded)


def project_credit_budget(
    authority: dict[str, object] | None,
    *,
    now: datetime | None = None,
) -> dict:
    current = _utc(now or datetime.now(UTC))
    month_start = current.replace(day=1).date().isoformat()
    if authority is None:
        return _reading("unknown", month_start, _unknown_funded_lane())
    try:
        return _project(authority, current)
    except ValueError:
        return _reading("unknown", month_start, _unknown_funded_lane())


def _read_funded_authority() -> dict[str, object] | None:
    return None


def read_credit_budget() -> dict:
    return project_credit_budget(_read_funded_authority())
