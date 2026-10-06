"""Fixed read projection for the funded SocialCrawl control view."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from decimal import Decimal

from google.cloud import bigquery

TARGET_PROJECT = "ogilvy-trends-v2"
TARGET_DATASET = "trends_v2_staging"
TARGET_LOCATION = "US"
TARGET_READER = "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
_LANE = "ogilvy_funded"
_ACCOUNT = "ogilvy_albert"


class FundedBudgetReadInvalid(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FundedBudgetProjection:
    state: str
    month_start: date | None
    credential_lane: str | None
    funding_account: str | None
    activation_stage: int | None
    opening_balance: Decimal | None
    current_balance: Decimal | None
    month_opening_balance: Decimal | None
    monthly_cap: Decimal | None
    monthly_ledger_debit: Decimal | None
    monthly_balance_delta: Decimal | None
    monthly_effective_spend: Decimal | None
    monthly_remaining: Decimal | None
    reserve_floor: Decimal | None
    reserve_remaining: Decimal | None
    stage_cap: Decimal | None
    run_allowance: Decimal | None
    attribution_state: str
    kill_state: str
    ledger_through: datetime | None


_MEASURED_FIELDS = tuple(
    field.name for field in fields(FundedBudgetProjection) if field.name != "state"
)


def _unknown() -> FundedBudgetProjection:
    return FundedBudgetProjection(
        state="unknown",
        month_start=None,
        credential_lane=None,
        funding_account=None,
        activation_stage=None,
        opening_balance=None,
        current_balance=None,
        month_opening_balance=None,
        monthly_cap=None,
        monthly_ledger_debit=None,
        monthly_balance_delta=None,
        monthly_effective_spend=None,
        monthly_remaining=None,
        reserve_floor=None,
        reserve_remaining=None,
        stage_cap=None,
        run_allowance=None,
        attribution_state="unavailable",
        kill_state="not_applicable",
        ledger_through=None,
    )


def _query() -> str:
    fields_sql = ",\n  ".join(f"budget.`{field}`" for field in _MEASURED_FIELDS)
    return (
        "SELECT\n  " + fields_sql + "\n"
        f"FROM `{TARGET_PROJECT}.{TARGET_DATASET}.v_socialcrawl_funded_budget_v1` AS budget\n"
        "LIMIT 2"
    )


def _mapping(row: object) -> dict[str, object]:
    if isinstance(row, dict):
        return row
    items = getattr(row, "items", None)
    if callable(items):
        return dict(items())
    raise FundedBudgetReadInvalid("funded budget row is invalid")


def _decimal(row: dict[str, object], field: str) -> Decimal:
    value = row.get(field)
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise FundedBudgetReadInvalid(f"funded budget {field} is invalid")
    return value


def _validated(row: object) -> FundedBudgetProjection:
    value = _mapping(row)
    if value.get("credential_lane") != _LANE or value.get("funding_account") != _ACCOUNT:
        raise FundedBudgetReadInvalid("funded budget identity is invalid")
    stage = value.get("activation_stage")
    if isinstance(stage, bool) or not isinstance(stage, int) or stage not in {0, 1, 2, 3}:
        raise FundedBudgetReadInvalid("funded budget activation stage is invalid")
    month_start = value.get("month_start")
    if isinstance(month_start, datetime) or not isinstance(month_start, date):
        raise FundedBudgetReadInvalid("funded budget month is invalid")
    ledger_through = value.get("ledger_through")
    if (
        not isinstance(ledger_through, datetime)
        or ledger_through.tzinfo is None
        or ledger_through.utcoffset() != timedelta(0)
    ):
        raise FundedBudgetReadInvalid("funded budget ledger time is invalid")
    credits = {
        field: _decimal(value, field)
        for field in (
            "opening_balance",
            "current_balance",
            "month_opening_balance",
            "monthly_cap",
            "monthly_ledger_debit",
            "monthly_balance_delta",
            "monthly_effective_spend",
            "monthly_remaining",
            "reserve_floor",
            "reserve_remaining",
            "stage_cap",
            "run_allowance",
        )
    }
    if credits["current_balance"] > credits["month_opening_balance"]:
        raise FundedBudgetReadInvalid("funded budget balance increased")
    expected_delta = credits["month_opening_balance"] - credits["current_balance"]
    expected_effective = max(credits["monthly_ledger_debit"], expected_delta)
    expected_remaining = max(Decimal("0"), credits["monthly_cap"] - expected_effective)
    expected_reserve = max(Decimal("0"), credits["current_balance"] - credits["reserve_floor"])
    expected_allowance = min(credits["stage_cap"], expected_remaining, expected_reserve)
    if (
        credits["monthly_balance_delta"] != expected_delta
        or credits["monthly_effective_spend"] != expected_effective
        or credits["monthly_remaining"] != expected_remaining
        or credits["reserve_remaining"] != expected_reserve
        or credits["run_allowance"] != expected_allowance
    ):
        raise FundedBudgetReadInvalid("funded budget formulas disagree")
    attribution = value.get("attribution_state")
    kill = value.get("kill_state")
    if attribution not in {"complete", "conservative", "gap_detected"}:
        raise FundedBudgetReadInvalid("funded budget attribution state is invalid")
    if kill not in {"not_tested", "passed", "failed", "killed"}:
        raise FundedBudgetReadInvalid("funded budget kill state is invalid")
    if expected_remaining == 0 or expected_reserve == 0:
        state = "exhausted"
    elif (
        attribution == "complete"
        and kill == "passed"
        and credits["stage_cap"] > 0
        and expected_allowance == credits["stage_cap"]
    ):
        state = "ready"
    else:
        state = "blocked"
    return FundedBudgetProjection(
        state=state,
        month_start=month_start,
        credential_lane=_LANE,
        funding_account=_ACCOUNT,
        activation_stage=stage,
        attribution_state=attribution,
        kill_state=kill,
        ledger_through=ledger_through,
        **credits,
    )


def read_funded_budget(*, client: object) -> FundedBudgetProjection:
    if (
        getattr(client, "project", None) != TARGET_PROJECT
        or getattr(client, "location", None) != TARGET_LOCATION
    ):
        raise FundedBudgetReadInvalid("funded budget reader requires the exact staging target")
    credentials = getattr(client, "_credentials", None)
    identity = getattr(credentials, "service_account_email", None)
    if identity != TARGET_READER:
        raise FundedBudgetReadInvalid("funded budget reader identity is not approved")
    try:
        job = client.query(
            _query(),
            location=TARGET_LOCATION,
            job_config=bigquery.QueryJobConfig(use_legacy_sql=False, query_parameters=[]),
        )
        if getattr(job, "errors", None):
            return _unknown()
        rows = tuple(job.result(max_results=2))
    except Exception:
        return _unknown()
    if not rows:
        return _unknown()
    if len(rows) != 1:
        raise FundedBudgetReadInvalid("funded budget read returned invalid cardinality")
    try:
        return _validated(rows[0])
    except FundedBudgetReadInvalid:
        return _unknown()
