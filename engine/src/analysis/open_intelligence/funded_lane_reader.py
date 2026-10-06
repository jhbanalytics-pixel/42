"""Bounded monthly ledger reader for the funded SocialCrawl staging lane."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from google.cloud import bigquery

from src.analysis.open_intelligence.funded_lane import FUNDED_CREDENTIAL_LANE

TARGET_PROJECT = "ogilvy-trends-v2"
TARGET_DATASET = "trends_v2_staging_funded"
TARGET_LOCATION = "US"
UNRECONCILED_CEILING = 100
_EXECUTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
_RESULT_FIELDS = frozenset(
    {
        "monthly_ledger_debit",
        "monthly_vendor_reported",
        "month_opening_balance",
        "unreconciled_execution_ids",
        "consecutive_complete_runs",
        "runs_today",
    }
)


class FundedLaneLedgerReadInvalid(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FundedLaneLedgerSnapshot:
    month_start: date
    monthly_ledger_debit: Decimal
    monthly_vendor_reported: Decimal
    month_opening_balance: Decimal | None
    unreconciled_execution_ids: tuple[str, ...]
    consecutive_complete_runs: int
    runs_today: int


def _sql(dataset: str) -> str:
    table = f"`{TARGET_PROJECT}.{dataset}.socialcrawl_credit_ledger_v1`"
    return f"""WITH lane_rows AS (
  SELECT *
  FROM {table}
  WHERE credential_lane = @credential_lane
),
month_rows AS (
  SELECT * FROM lane_rows
  WHERE trend_date >= @month_start
    AND trend_date <= @as_of_date
),
phase_spend AS (
  SELECT
    COALESCE(SUM(IF(event_type IN ('phase_close', 'attribution_gap'), budget_debit_credits, 0)), 0) AS monthly_ledger_debit,
    COALESCE(SUM(IF(event_type IN ('phase_close', 'attribution_gap'), vendor_reported_credits, 0)), 0) AS monthly_vendor_reported
  FROM month_rows
),
execution_states AS (
  SELECT
    execution_id,
    COUNTIF(event_type = 'phase_close') AS phase_close_count,
    COUNTIF(event_type = 'phase_close' AND STARTS_WITH(phase, 'wave1_')) AS wave1_phase_close_count,
    COUNTIF(event_type = 'run_close') AS run_close_count,
    COUNTIF(event_type = 'attribution_gap') AS gap_count
  FROM lane_rows
  GROUP BY execution_id
),
recent_close_states AS (
  SELECT ARRAY_AGG(attribution_state ORDER BY recorded_at DESC, execution_id DESC LIMIT 3) AS states
  FROM lane_rows
  WHERE event_type = 'run_close'
),
today_runs AS (
  SELECT COUNT(DISTINCT execution_id) AS runs_today
  FROM month_rows
  WHERE event_type = 'run_close'
    AND trend_date = @as_of_date
)
SELECT
  phase_spend.monthly_ledger_debit,
  phase_spend.monthly_vendor_reported,
  (SELECT ARRAY_AGG(month_opening_balance ORDER BY recorded_at ASC LIMIT 1)[SAFE_OFFSET(0)]
   FROM month_rows WHERE event_type = 'preflight') AS month_opening_balance,
  ARRAY(
    SELECT execution_id
    FROM execution_states
    WHERE gap_count > 0
      OR (
        -- An execution that recorded its preflight and then stopped before any
        -- phase spent nothing on this ledger; a vendor-side debit it caused
        -- shows as a balance delta gap at the next preflight. Executions that
        -- opened a phase must close every phase and the run exactly once
        -- (attempt 12 on staging, 4 Sep 2026, blocked every later run).
        phase_close_count > 0
        AND (
          run_close_count != 1
          OR CASE WHEN wave1_phase_close_count > 0
            THEN phase_close_count != 4 OR wave1_phase_close_count != 4
            ELSE phase_close_count != 21
          END
        )
      )
    ORDER BY execution_id
    LIMIT {UNRECONCILED_CEILING + 1}
  ) AS unreconciled_execution_ids,
  COALESCE((
    SELECT MIN(position)
    FROM UNNEST(recent_close_states.states) AS state WITH OFFSET AS position
    WHERE state != 'complete'
  ), COALESCE(ARRAY_LENGTH(recent_close_states.states), 0)) AS consecutive_complete_runs,
  today_runs.runs_today
FROM phase_spend
CROSS JOIN recent_close_states
CROSS JOIN today_runs"""


def _config(month_start: date, as_of_date: date) -> bigquery.QueryJobConfig:
    return bigquery.QueryJobConfig(
        use_legacy_sql=False,
        query_parameters=[
            bigquery.ScalarQueryParameter("credential_lane", "STRING", FUNDED_CREDENTIAL_LANE),
            bigquery.ScalarQueryParameter("month_start", "DATE", month_start),
            bigquery.ScalarQueryParameter("as_of_date", "DATE", as_of_date),
        ],
    )


def _mapping(row: object) -> dict[str, object]:
    if isinstance(row, Mapping):
        return dict(row)
    items = getattr(row, "items", None)
    if not callable(items):
        raise FundedLaneLedgerReadInvalid("monthly ledger aggregate is not a mapping")
    return dict(items())


def _decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise FundedLaneLedgerReadInvalid(f"{field} is invalid")
    return value


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FundedLaneLedgerReadInvalid(f"{field} is invalid")
    return value


def read_monthly_funded_lane(
    *,
    client: object,
    dataset: str,
    as_of: datetime,
    credential_lane: str,
) -> FundedLaneLedgerSnapshot:
    if (
        getattr(client, "project", None) != TARGET_PROJECT
        or getattr(client, "location", None) != TARGET_LOCATION
        or dataset != TARGET_DATASET
    ):
        raise FundedLaneLedgerReadInvalid("exact staging target is required")
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() != timedelta(0):
        raise FundedLaneLedgerReadInvalid("as of must be a UTC timestamp")
    if credential_lane != FUNDED_CREDENTIAL_LANE:
        raise FundedLaneLedgerReadInvalid("credential lane is unsupported")
    month_start = date(as_of.year, as_of.month, 1)
    job = client.query(
        _sql(dataset),
        job_config=_config(month_start, as_of.date()),
        location=TARGET_LOCATION,
    )
    rows = tuple(job.result(max_results=2))
    if len(rows) != 1:
        raise FundedLaneLedgerReadInvalid("monthly ledger query must return exactly one row")
    row = _mapping(rows[0])
    if set(row) != _RESULT_FIELDS:
        raise FundedLaneLedgerReadInvalid("monthly ledger aggregate fields are invalid")
    ledger = _decimal(row["monthly_ledger_debit"], "monthly ledger debit")
    vendor = _decimal(row["monthly_vendor_reported"], "monthly vendor reported")
    if ledger < vendor:
        raise FundedLaneLedgerReadInvalid("monthly ledger debit is below vendor spend")
    raw_ids = row["unreconciled_execution_ids"]
    if not isinstance(raw_ids, (tuple, list)):
        raise FundedLaneLedgerReadInvalid("unreconciled execution IDs are invalid")
    execution_ids = tuple(raw_ids)
    if (
        len(execution_ids) > UNRECONCILED_CEILING
        or len(execution_ids) != len(set(execution_ids))
        or any(
            not isinstance(item, str) or _EXECUTION_ID.fullmatch(item) is None
            for item in execution_ids
        )
    ):
        raise FundedLaneLedgerReadInvalid("unreconciled execution IDs are invalid")
    return FundedLaneLedgerSnapshot(
        month_start=month_start,
        monthly_ledger_debit=ledger,
        monthly_vendor_reported=vendor,
        month_opening_balance=(
            _decimal(row["month_opening_balance"], "month opening balance")
            if row["month_opening_balance"] is not None
            else None
        ),
        unreconciled_execution_ids=execution_ids,
        consecutive_complete_runs=_integer(
            row["consecutive_complete_runs"], "consecutive complete runs"
        ),
        runs_today=_integer(row["runs_today"], "runs today"),
    )
