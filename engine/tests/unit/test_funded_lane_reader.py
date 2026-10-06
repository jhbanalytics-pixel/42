"""Bounded monthly ledger reader for the funded SocialCrawl lane."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from src.analysis.open_intelligence.funded_lane_reader import (
    FundedLaneLedgerReadInvalid,
    read_monthly_funded_lane,
)

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging_funded"
AS_OF = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


class Job:
    def __init__(self, rows):
        self.rows = rows
        self.max_results = None

    def result(self, *, max_results):
        self.max_results = max_results
        return self.rows


class Client:
    project = PROJECT
    location = "US"

    def __init__(self, rows):
        self.job = Job(rows)
        self.calls = []

    def query(self, sql, *, job_config, location):
        self.calls.append((sql, job_config, location))
        return self.job


def row(**overrides):
    values = {
        "monthly_ledger_debit": Decimal("150"),
        "monthly_vendor_reported": Decimal("100"),
        "month_opening_balance": Decimal("250100"),
        "unreconciled_execution_ids": [],
        "consecutive_complete_runs": 3,
        "runs_today": 0,
    }
    values.update(overrides)
    return values


def parameters(job_config):
    return {parameter.name: parameter.value for parameter in job_config.query_parameters}


def test_reader_uses_one_exact_parameterized_bounded_staging_query() -> None:
    client = Client([row()])

    result = read_monthly_funded_lane(
        client=client,
        dataset=DATASET,
        as_of=AS_OF,
        credential_lane="ogilvy_funded",
    )

    assert result.month_start == date(2026, 8, 1)
    assert result.monthly_ledger_debit == Decimal("150")
    assert result.monthly_vendor_reported == Decimal("100")
    assert result.month_opening_balance == Decimal("250100")
    assert result.unreconciled_execution_ids == ()
    assert result.consecutive_complete_runs == 3
    assert result.runs_today == 0
    assert len(client.calls) == 1
    sql, config, location = client.calls[0]
    assert location == "US"
    assert f"`{PROJECT}.{DATASET}.socialcrawl_credit_ledger_v1`" in sql
    assert "event_type IN ('phase_close', 'attribution_gap')" in sql
    assert "event_type = 'run_close'" in sql
    assert "event_type = 'attribution_gap'" in sql
    assert "wave1_phase_close_count > 0" in sql
    # A preflight-only execution is not unreconciled (4 Sep 2026): the rule
    # demands phase rows before it demands a close.
    assert "phase_close_count > 0" in sql
    assert sql.index("phase_close_count > 0") < sql.index("run_close_count != 1")
    assert "THEN phase_close_count != 4 OR wave1_phase_close_count != 4" in sql
    assert "ELSE phase_close_count != 21" in sql
    assert "run_close_count != 1" in sql
    assert "gap_count > 0" in sql
    assert "WHERE gap_count = 0" not in sql
    assert "FROM lane_rows\n  GROUP BY execution_id" in sql
    phase_spend_sql = sql.split("execution_states AS", 1)[0]
    assert "FROM month_rows" in phase_spend_sql
    assert "FROM lane_rows" not in phase_spend_sql.split("phase_spend AS", 1)[1]
    assert "COALESCE(ARRAY_LENGTH(recent_close_states.states), 0)" in sql
    assert "credential_lane = @credential_lane" in sql
    assert "trend_date >= @month_start" in sql
    assert "trend_date <= @as_of_date" in sql
    assert "LIMIT 101" in sql
    assert parameters(config) == {
        "credential_lane": "ogilvy_funded",
        "month_start": date(2026, 8, 1),
        "as_of_date": date(2026, 8, 27),
    }
    assert client.job.max_results == 2


@pytest.mark.parametrize(
    "client",
    [
        Client([row()]),
        type("WrongProject", (Client,), {"project": "other"})([row()]),
        type("WrongLocation", (Client,), {"location": "EU"})([row()]),
    ],
)
def test_reader_refuses_wrong_target_or_dataset_before_query(client) -> None:
    dataset = "trends_v2" if type(client) is Client else DATASET

    with pytest.raises(FundedLaneLedgerReadInvalid, match="staging target"):
        read_monthly_funded_lane(
            client=client,
            dataset=dataset,
            as_of=AS_OF,
            credential_lane="ogilvy_funded",
        )

    assert client.calls == []


@pytest.mark.parametrize(
    ("as_of", "lane", "message"),
    [
        (datetime(2026, 8, 27, 10, 0), "ogilvy_funded", "UTC"),
        (AS_OF, "jhb_core", "credential lane"),
        (AS_OF, "", "credential lane"),
    ],
)
def test_reader_refuses_invalid_time_or_lane_before_query(as_of, lane, message) -> None:
    client = Client([row()])

    with pytest.raises(FundedLaneLedgerReadInvalid, match=message):
        read_monthly_funded_lane(
            client=client,
            dataset=DATASET,
            as_of=as_of,
            credential_lane=lane,
        )

    assert client.calls == []


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [row(), row()],
        [row(monthly_ledger_debit=Decimal("99"), monthly_vendor_reported=Decimal("100"))],
        [row(monthly_ledger_debit=-1)],
        [row(monthly_vendor_reported=1.0)],
        [row(month_opening_balance=1.0)],
        [row(unreconciled_execution_ids=["bad id"])],
        [row(unreconciled_execution_ids=["execution_1", "execution_1"])],
        [row(consecutive_complete_runs=True)],
        [row(runs_today=-1)],
    ],
)
def test_reader_rejects_missing_duplicate_or_malformed_aggregate(rows) -> None:
    with pytest.raises(FundedLaneLedgerReadInvalid):
        read_monthly_funded_lane(
            client=Client(rows),
            dataset=DATASET,
            as_of=AS_OF,
            credential_lane="ogilvy_funded",
        )
