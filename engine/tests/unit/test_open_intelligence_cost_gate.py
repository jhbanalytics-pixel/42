from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import numpy as np
import pytest
import scripts.vertex_cost_watchdog as w
from src.utils.gemini_usage import _NEW_CONSUMER_STAGES, USAGE_CONSUMERS


class Frame:
    def __init__(self, records):
        self.records = records
        self.empty = not records

    def to_dict(self, orient):
        assert orient == "records"
        return self.records


def _install_report_boundary(
    monkeypatch,
    *,
    ledger_rows=None,
    base_rows=None,
    staging_rows=None,
    base_error=None,
    staging_error=None,
    staging_columns=True,
    persistence_rows=None,
    persistence_error=None,
    billing_cost=0.0,
):
    if ledger_rows is not None:
        base_rows = [row for row in ledger_rows if row.get("consumer") not in w.NEW_CONSUMER_STAGES]
        staging_rows = [row for row in ledger_rows if row.get("consumer") in w.NEW_CONSUMER_STAGES]
    base_rows = [] if base_rows is None else base_rows
    staging_rows = [] if staging_rows is None else staging_rows
    sqls = []

    def capture(sql):
        sqls.append(sql)
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            assert f"`{w.STAGING_USAGE_DATASET}.INFORMATION_SCHEMA.COLUMNS`" in sql
            if not staging_columns:
                return Frame([])
            return Frame(
                [
                    {"column_name": "run_id"},
                    {"column_name": "stage"},
                    {"column_name": "call_index"},
                ]
            )
        if ".gemini_usage`" in sql:
            if f"`{w.STAGING_USAGE_DATASET}.gemini_usage`" in sql:
                if staging_error is not None:
                    raise staging_error
                return Frame(staging_rows)
            if base_error is not None:
                raise base_error
            return Frame(base_rows)
        if ".system_events`" in sql:
            assert f"`{w.STAGING_USAGE_DATASET}.system_events`" in sql
            if persistence_error is not None:
                raise persistence_error
            return Frame([{"n": 0}] if persistence_rows is None else persistence_rows)
        if "billing_export" in sql:
            return Frame([{"day": "2026-08-20", "cost": billing_cost}])
        raise AssertionError(f"Unexpected SQL: {sql}")

    monkeypatch.setattr(w, "_run_query", capture)
    from src.utils import bigquery

    monkeypatch.setattr(bigquery, "get_client", lambda: SimpleNamespace(project="p"))
    monkeypatch.setattr(bigquery, "get_dataset", lambda: "d")
    return sqls


def _new_row(consumer, *, calls=1, prompt_in=0, prompt_out=0):
    return {
        "consumer": consumer,
        "model": "gemini-3.5-flash",
        "day": "2026-08-20",
        "calls": calls,
        "prompt_in": prompt_in,
        "prompt_out": prompt_out,
        "invalid_events": 0,
        "duplicate_events": 0,
    }


def _good_discrepancy():
    return w.reconcile_against_billing({"2026-08-22": 1.0}, {"2026-08-22": 1.05})


def _ready_kwargs():
    return {
        "declared_consumers": USAGE_CONSUMERS,
        "per_call_columns": True,
        "ledger_notes": (),
        "billing_notes": (),
        "invalid_events": 0,
        "duplicate_events": 0,
        "discrepancy": _good_discrepancy(),
        "projected_monthly": 29.99,
        "valid_new_consumers": frozenset(),
        "observed_call_consumers": None,
        "persistence_fault": False,
    }


def test_capability_read_uses_exact_dataset_and_falls_back_to_legacy_sql(monkeypatch):
    sqls = []

    def capture(sql):
        sqls.append(sql)
        return Frame([])

    monkeypatch.setattr(w, "_run_query", capture)
    read = w.fetch_usage_ledger(7, "exact-project.exact_dataset")

    assert read.per_call_columns is False
    assert len(sqls) == 2
    assert "`exact-project.exact_dataset.INFORMATION_SCHEMA.COLUMNS`" in sqls[0]
    assert "column_name IN ('run_id', 'stage', 'call_index')" in sqls[0]
    assert "LIMIT" not in sqls[0].upper()
    assert "run_id" not in sqls[1]
    assert "call_index" not in sqls[1]
    assert "SUM(calls) AS calls" in sqls[1]
    assert "trend_date > DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)" in sqls[1]
    assert "trend_date <= CURRENT_DATE()" in sqls[1]
    assert "LIMIT" not in sqls[1].upper()
    assert any("per-call Open Intelligence rows cannot be validated" in n for n in read.notes)


def test_mixed_query_validates_exact_events_deduplicates_and_keeps_legacy(monkeypatch):
    sqls = []
    responses = iter(
        [
            Frame(
                [
                    {"column_name": "run_id"},
                    {"column_name": "stage"},
                    {"column_name": "call_index"},
                ]
            ),
            Frame([]),
        ]
    )

    def capture(sql):
        sqls.append(sql)
        return next(responses)

    monkeypatch.setattr(w, "_run_query", capture)
    read = w.fetch_usage_ledger(7, "p.d")
    sql = sqls[1]

    assert read.per_call_columns is True
    assert read.notes == ()
    assert "consumer NOT IN ('dynamic_signal_summary', 'open_question_answer')" in sql
    assert "usage_id IS NOT NULL" in sql
    assert "run_id IS NOT NULL" in sql
    assert "stage IS NOT NULL" in sql
    assert "call_index IS NOT NULL" in sql
    assert "calls = 1" in sql
    assert "prompt_tokens >= 0" in sql
    assert "completion_tokens >= 0" in sql
    assert "consumer = 'dynamic_signal_summary' AND stage = 'summary'" in sql
    assert "consumer = 'open_question_answer' AND stage IN ('answering', 'planning')" in sql
    assert "ROW_NUMBER() OVER" in sql
    assert "PARTITION BY usage_id" in sql
    assert "new_rows_with_physical_counts AS" in sql
    assert "COUNT(*) OVER (PARTITION BY usage_id) AS event_physical_rows" in sql
    assert sql.index("new_rows_with_physical_counts AS") < sql.index("valid_new AS")
    assert "FROM valid_new\n      WHERE valid_event_rank = 1" in sql
    assert "FROM new_rows_with_physical_counts" in sql
    assert "WHERE physical_event_rank = 1 AND event_physical_rows > 1" in sql
    assert "SUM(invalid_events) AS invalid_events" in sql
    assert "SUM(duplicate_events) AS duplicate_events" in sql
    assert "trend_date > DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)" in sql
    assert "trend_date <= CURRENT_DATE()" in sql
    assert "LIMIT" not in sql.upper()


def test_combined_reader_uses_deployed_base_and_exact_staging_scopes(monkeypatch):
    sqls = []
    responses = iter(
        [
            Frame(
                [
                    {
                        "consumer": "trend_analysis",
                        "model": "gemini-3.5-flash",
                        "day": "2026-08-20",
                        "calls": 2,
                        "prompt_in": 100,
                        "prompt_out": 10,
                    }
                ]
            ),
            Frame(
                [
                    {"column_name": "run_id"},
                    {"column_name": "stage"},
                    {"column_name": "call_index"},
                ]
            ),
            Frame([_new_row("dynamic_signal_summary", prompt_in=8_000, prompt_out=800)]),
        ]
    )

    def capture(sql):
        sqls.append(sql)
        return next(responses)

    monkeypatch.setattr(w, "_run_query", capture)
    read = w.fetch_combined_usage_ledger(7, "ogilvy-trends-v2.trends_v2_dev")

    assert w.STAGING_USAGE_DATASET == "ogilvy-trends-v2.trends_v2_staging"
    assert [row["consumer"] for row in read.rows] == [
        "trend_analysis",
        "dynamic_signal_summary",
    ]
    assert read.base.notes == ()
    assert read.staging.notes == ()
    assert "`ogilvy-trends-v2.trends_v2_dev.gemini_usage`" in sqls[0]
    assert "consumer NOT IN ('dynamic_signal_summary', 'open_question_answer')" in sqls[0]
    assert "INFORMATION_SCHEMA" not in sqls[0]
    assert f"`{w.STAGING_USAGE_DATASET}.INFORMATION_SCHEMA.COLUMNS`" in sqls[1]
    assert f"`{w.STAGING_USAGE_DATASET}.gemini_usage`" in sqls[2]
    assert "consumer IN ('dynamic_signal_summary', 'open_question_answer')" in sqls[2]
    assert "THEN 'legacy'" not in sqls[2]
    assert all(
        "trend_date > DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)" in sql for sql in (sqls[0], sqls[2])
    )
    assert all("trend_date >= DATE_SUB" not in sql for sql in (sqls[0], sqls[2]))
    assert "LIMIT" not in "\n".join(sqls).upper()


def test_report_combines_base_and_staging_before_project_billing(monkeypatch):
    base = {
        "consumer": "trend_analysis",
        "model": "gemini-3.5-flash",
        "day": "2026-08-20",
        "calls": 1,
        "prompt_in": 1_000_000,
        "prompt_out": 0,
    }
    staging = _new_row("dynamic_signal_summary", calls=1, prompt_in=8_000, prompt_out=800)
    sqls = _install_report_boundary(
        monkeypatch,
        base_rows=[base],
        staging_rows=[staging],
        billing_cost=1.5192,
    )

    report = w.build_report(7)

    assert [consumer.name for consumer in report.consumers] == [
        "trend_analysis",
        "dynamic_signal_summary",
    ]
    assert report.window_cost == pytest.approx(1.5192)
    assert report.discrepancy is not None
    assert report.discrepancy.ledger_cost == pytest.approx(1.5192)
    assert report.discrepancy.billing_cost == pytest.approx(1.5192)
    assert report.discrepancy.within_tolerance is True
    assert any("`p.d.gemini_usage`" in sql for sql in sqls)
    assert any(f"`{w.STAGING_USAGE_DATASET}.gemini_usage`" in sql for sql in sqls)


def test_missing_staging_blocks_readiness_without_erasing_base_cost(monkeypatch):
    base = {
        "consumer": "trend_analysis",
        "model": "gemini-3.5-flash",
        "day": "2026-08-20",
        "calls": 1,
        "prompt_in": 1_000_000,
        "prompt_out": 0,
    }
    _install_report_boundary(
        monkeypatch,
        base_rows=[base],
        staging_error=RuntimeError("staging ledger missing"),
        persistence_error=RuntimeError("staging events missing"),
        billing_cost=1.5,
    )

    report = w.build_report(7)

    assert report.window_cost == pytest.approx(1.5)
    assert [consumer.name for consumer in report.consumers] == ["trend_analysis"]
    assert report.discrepancy is None
    assert report.enablement is not None
    assert report.enablement.ready is False
    assert any("staging ledger missing" in reason for reason in report.enablement.reasons)
    assert any(
        "staging enablement remains blocked" in reason for reason in report.enablement.reasons
    )


def test_missing_staging_columns_block_readiness_and_keep_base_cost(monkeypatch):
    base = {
        "consumer": "trend_analysis",
        "model": "gemini-3.5-flash",
        "day": "2026-08-20",
        "calls": 1,
        "prompt_in": 1_000_000,
        "prompt_out": 0,
    }
    _install_report_boundary(
        monkeypatch,
        base_rows=[base],
        staging_columns=False,
        billing_cost=1.5,
    )

    report = w.build_report(7)

    assert report.window_cost == pytest.approx(1.5)
    assert report.enablement is not None
    assert report.enablement.ready is False
    assert any("per-call columns" in reason for reason in report.enablement.reasons)


def test_base_ledger_failure_blocks_project_cost_truth(monkeypatch):
    staging = _new_row("dynamic_signal_summary", calls=1, prompt_in=8_000, prompt_out=800)
    _install_report_boundary(
        monkeypatch,
        staging_rows=[staging],
        base_error=RuntimeError("base ledger unavailable"),
        billing_cost=0.0192,
    )

    report = w.build_report(7)

    assert report.window_cost == pytest.approx(0.0192)
    assert report.discrepancy is None
    assert report.enablement is not None
    assert report.enablement.ready is False
    assert any("base ledger unavailable" in reason for reason in report.enablement.reasons)
    assert any("base ledger unavailable" in note for note in report.notes)


def test_staging_integrity_alone_drives_integrity_readiness(monkeypatch):
    base = {
        "consumer": "trend_analysis",
        "model": "gemini-3.5-flash",
        "day": "2026-08-20",
        "calls": 1,
        "prompt_in": 100,
        "prompt_out": 0,
        "invalid_events": 99,
        "duplicate_events": 99,
    }
    staging = [
        _new_row("dynamic_signal_summary", calls=1, prompt_in=8_000, prompt_out=800),
        _new_row("open_question_answer", calls=1, prompt_in=32_000, prompt_out=4_000),
    ]
    _install_report_boundary(
        monkeypatch,
        base_rows=[base],
        staging_rows=staging,
        billing_cost=0.10335,
    )

    report = w.build_report(7)

    assert report.enablement == w.EnablementResult(True, ())
    assert not any("99 invalid" in note for note in report.notes)
    assert not any("99 duplicate" in note for note in report.notes)


def test_mixed_aggregate_costs_one_unique_event_and_keeps_fault_counts_visible():
    rows = [
        {
            "consumer": "dynamic_signal_summary",
            "model": "gemini-3.5-flash",
            "day": "2026-08-25",
            "calls": 1,
            "prompt_in": 8_000,
            "prompt_out": 800,
            "invalid_events": 1,
            "duplicate_events": 1,
        }
    ]

    (spend,) = w.usage_ledger_spends(rows)
    notes = w.ledger_integrity_notes(rows)

    assert spend.calls == 1
    assert spend.prompt_tokens == 8_000
    assert spend.completion_tokens == 800
    assert spend.window_cost == pytest.approx(0.0192)
    assert len(notes) == 2
    assert "1 invalid" in notes[0]
    assert "metering fault" in notes[0]
    assert "1 duplicate" in notes[1]
    assert "ledger integrity fault" in notes[1]


def test_ledger_cost_by_day_uses_deduplicated_sql_sums_without_hiding_faults():
    rows = [
        {
            "consumer": "open_question_answer",
            "model": "gemini-3.5-flash",
            "day": "2026-08-25",
            "calls": 1,
            "prompt_in": 32_000,
            "prompt_out": 4_000,
            "invalid_events": 3,
            "duplicate_events": 2,
        }
    ]

    assert w.ledger_cost_by_day(rows) == {"2026-08-25": pytest.approx(0.084)}
    assert w.ledger_fault_counts(rows) == (3, 2)


@pytest.mark.parametrize(
    ("rows", "expected_calls", "expected_cost", "expected_faults"),
    [
        (
            [
                {
                    "consumer": "dynamic_signal_summary",
                    "model": "gemini-3.5-flash",
                    "day": "2026-08-25",
                    "calls": 0,
                    "prompt_in": 0,
                    "prompt_out": 0,
                    "invalid_events": 2,
                    "duplicate_events": 1,
                }
            ],
            0,
            0.0,
            (2, 1),
        ),
        (
            [
                {
                    "consumer": "open_question_answer",
                    "model": "gemini-3.5-flash",
                    "day": "2026-08-25",
                    "calls": 1,
                    "prompt_in": 2_000,
                    "prompt_out": 200,
                    "invalid_events": 1,
                    "duplicate_events": 1,
                }
            ],
            1,
            0.0048,
            (1, 1),
        ),
    ],
)
def test_malformed_duplicate_aggregates_keep_faults_and_exclude_invalid_cost(
    rows, expected_calls, expected_cost, expected_faults
):
    (spend,) = w.usage_ledger_spends(rows)

    assert spend.calls == expected_calls
    assert spend.window_cost == pytest.approx(expected_cost)
    assert w.ledger_fault_counts(rows) == expected_faults


def test_new_consumer_registry_and_stage_pairs_have_one_source():
    assert w.EXPECTED_CONSUMERS is USAGE_CONSUMERS
    assert w.NEW_CONSUMER_STAGES is _NEW_CONSUMER_STAGES
    assert {
        "dynamic_signal_summary": frozenset({"summary"}),
        "open_question_answer": frozenset({"planning", "answering"}),
    } == w.NEW_CONSUMER_STAGES


def test_per_call_ceilings_use_canonical_model_price_and_are_immutable():
    summary, question = w.per_call_ceiling_rows()

    assert (
        summary.consumer,
        summary.model,
        summary.input_tokens,
        summary.output_tokens,
        summary.maximum_usd_per_call,
    ) == ("dynamic_signal_summary", "gemini-3.5-flash", 8_000, 800, pytest.approx(0.0192))
    assert (
        question.consumer,
        question.model,
        question.input_tokens,
        question.output_tokens,
        question.maximum_usd_per_call,
    ) == ("open_question_answer", "gemini-3.5-flash", 32_000, 4_000, pytest.approx(0.084))
    with pytest.raises(FrozenInstanceError):
        summary.input_tokens = 1


def test_enablement_is_immutable_and_first_call_needs_no_observed_rows():
    result = w.staging_enablement_result(**_ready_kwargs())

    assert result.ready is True
    assert result.reasons == ()
    with pytest.raises(FrozenInstanceError):
        result.ready = False


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"declared_consumers": USAGE_CONSUMERS[:-2]}, "not declared"),
        ({"per_call_columns": False}, "per-call columns"),
        ({"ledger_notes": ("ledger unreadable",)}, "ledger unreadable"),
        ({"billing_notes": ("billing unreadable",)}, "billing unreadable"),
        ({"invalid_events": 1}, "invalid per-call"),
        ({"duplicate_events": 1}, "duplicate per-call"),
        ({"discrepancy": None}, "billing discrepancy unavailable"),
        (
            {"discrepancy": w.Discrepancy(0.0, 0.0, 0.0, 0.0, 0, None)},
            "no settled overlapping day",
        ),
        (
            {"discrepancy": w.Discrepancy(1.0, 2.0, 1.0, 0.5, 1, None)},
            "billing verdict unknown",
        ),
        (
            {"discrepancy": w.Discrepancy(1.0, 2.0, 1.0, 0.5, 1, False)},
            "outside tolerance",
        ),
        ({"projected_monthly": 30.0}, "at or above $30.00"),
        ({"projected_monthly": True}, "finite nonnegative number"),
        ({"projected_monthly": "1.0"}, "finite nonnegative number"),
        ({"projected_monthly": None}, "finite nonnegative number"),
        ({"projected_monthly": float("nan")}, "finite nonnegative number"),
        ({"projected_monthly": float("inf")}, "finite nonnegative number"),
        ({"projected_monthly": float("-inf")}, "finite nonnegative number"),
        ({"projected_monthly": -0.01}, "finite nonnegative number"),
        ({"persistence_fault": True}, "persistence fault"),
    ],
)
def test_enablement_fails_closed_for_each_blocker(overrides, reason):
    kwargs = _ready_kwargs() | overrides
    result = w.staging_enablement_result(**kwargs)

    assert result.ready is False
    assert any(reason in item for item in result.reasons)


def test_observed_call_requires_a_valid_row_for_each_expected_new_consumer():
    kwargs = _ready_kwargs() | {
        "observed_call_consumers": frozenset(w.NEW_CONSUMER_STAGES),
        "valid_new_consumers": frozenset({"dynamic_signal_summary"}),
    }
    result = w.staging_enablement_result(**kwargs)

    assert result.ready is False
    assert result.reasons == (
        "open_question_answer has no valid per-call ledger event after calls were observed",
    )


def test_one_observed_new_consumer_ends_the_first_call_exception_for_the_pair():
    kwargs = _ready_kwargs() | {
        "observed_call_consumers": frozenset({"dynamic_signal_summary"}),
        "valid_new_consumers": frozenset({"dynamic_signal_summary"}),
    }
    result = w.staging_enablement_result(**kwargs)

    assert result.ready is False
    assert result.reasons == (
        "open_question_answer has no valid per-call ledger event after calls were observed",
    )


def test_persistence_fault_query_is_bounded_and_fails_closed(monkeypatch):
    sqls = []

    def capture(sql):
        sqls.append(sql)
        return Frame([{"n": 1}])

    monkeypatch.setattr(w, "_run_query", capture)
    read = w.fetch_metering_persistence_fault(7, "exact-project.exact_dataset")

    assert read.fault is True
    assert read.notes == ()
    assert "`exact-project.exact_dataset.system_events`" in sqls[0]
    assert "event_type = 'metering_persistence_failed'" in sqls[0]
    assert "status = 'failed'" in sqls[0]
    assert "DATE(event_time) > DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)" in sqls[0]
    assert "DATE(event_time) <= CURRENT_DATE()" in sqls[0]
    assert "LIMIT" not in sqls[0].upper()


def test_persistence_zero_count_is_a_clean_verdict(monkeypatch):
    monkeypatch.setattr(w, "_run_query", lambda _sql: Frame([{"n": 0}]))

    read = w.fetch_metering_persistence_fault(7, "p.d")

    assert read.fault is False
    assert read.notes == ()


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (np.int64(0), False),
        (np.uint64(0), False),
        (np.int64(1), True),
        (np.uint64(1), True),
    ],
)
def test_persistence_numpy_integral_count_returns_exact_bool(monkeypatch, count, expected):
    monkeypatch.setattr(w, "_run_query", lambda _sql: Frame([{"n": count}]))

    read = w.fetch_metering_persistence_fault(7, "p.d")

    assert type(read.fault) is bool
    assert read.fault is expected
    assert read.notes == ()


@pytest.mark.parametrize(
    "records",
    [
        [],
        [{"n": 0}, {"n": 0}],
        [{}],
        [{"n": None}],
        [{"n": float("nan")}],
        [{"n": float("inf")}],
        [{"n": float("-inf")}],
        [{"n": True}],
        [{"n": "0"}],
        [{"n": 0.0}],
        [{"n": 1.0}],
        [{"n": -1}],
    ],
)
def test_persistence_count_requires_one_nonnegative_integer(monkeypatch, records):
    monkeypatch.setattr(w, "_run_query", lambda _sql: Frame(records))

    read = w.fetch_metering_persistence_fault(7, "p.d")

    assert read.fault is None
    assert len(read.notes) == 1
    assert "exactly one nonnegative integer count" in read.notes[0]


def test_persistence_fault_read_error_is_a_blocking_unknown(monkeypatch):
    def fail(_sql):
        raise RuntimeError("missing table")

    monkeypatch.setattr(w, "_run_query", fail)
    read = w.fetch_metering_persistence_fault(7, "p.d")

    assert read.fault is None
    assert len(read.notes) == 1
    assert "could not be checked" in read.notes[0]


@pytest.mark.parametrize("persistence_rows", [[{"n": None}], [{}]])
@pytest.mark.parametrize("json_mode", [False, True])
def test_main_is_not_ready_for_malformed_persistence_count(
    monkeypatch, capsys, persistence_rows, json_mode
):
    _install_report_boundary(
        monkeypatch,
        ledger_rows=[
            _new_row("dynamic_signal_summary", calls=1, prompt_in=8_000, prompt_out=800),
            _new_row("open_question_answer", calls=1, prompt_in=32_000, prompt_out=4_000),
        ],
        persistence_rows=persistence_rows,
        billing_cost=0.1032,
    )
    argv = ["vertex_cost_watchdog.py"] + (["--json"] if json_mode else [])
    monkeypatch.setattr(sys, "argv", argv)

    assert w.main() == 0
    rendered = capsys.readouterr().out
    if json_mode:
        decision = json.loads(rendered)["staging_enablement"]
        assert decision["ready"] is False
        assert any("nonnegative integer count" in reason for reason in decision["reasons"])
    else:
        assert "STAGING ENABLEMENT: NOT READY" in rendered
        assert "nonnegative integer count" in rendered


@pytest.mark.parametrize("count", [np.int64(1), np.uint64(1)])
@pytest.mark.parametrize("json_mode", [False, True])
def test_main_is_not_ready_for_positive_numpy_persistence_count(
    monkeypatch, capsys, count, json_mode
):
    _install_report_boundary(
        monkeypatch,
        ledger_rows=[
            _new_row("dynamic_signal_summary", calls=1, prompt_in=8_000, prompt_out=800),
            _new_row("open_question_answer", calls=1, prompt_in=32_000, prompt_out=4_000),
        ],
        persistence_rows=[{"n": count}],
        billing_cost=0.1032,
    )
    argv = ["vertex_cost_watchdog.py"] + (["--json"] if json_mode else [])
    monkeypatch.setattr(sys, "argv", argv)

    assert w.main() == 0
    rendered = capsys.readouterr().out
    if json_mode:
        decision = json.loads(rendered)["staging_enablement"]
        assert decision["ready"] is False
        assert any("persistence fault" in reason for reason in decision["reasons"])
    else:
        assert "STAGING ENABLEMENT: NOT READY" in rendered
        assert "persistence fault" in rendered


@pytest.mark.parametrize("json_mode", [False, True])
def test_main_is_not_ready_when_only_one_new_consumer_is_observed(monkeypatch, capsys, json_mode):
    _install_report_boundary(
        monkeypatch,
        ledger_rows=[_new_row("dynamic_signal_summary", calls=1, prompt_in=8_000, prompt_out=800)],
        billing_cost=0.0192,
    )
    argv = ["vertex_cost_watchdog.py"] + (["--json"] if json_mode else [])
    monkeypatch.setattr(sys, "argv", argv)

    assert w.main() == 0
    rendered = capsys.readouterr().out
    if json_mode:
        decision = json.loads(rendered)["staging_enablement"]
        assert decision["ready"] is False
        assert any("open_question_answer" in reason for reason in decision["reasons"])
    else:
        assert "STAGING ENABLEMENT: NOT READY" in rendered
        assert "open_question_answer" in rendered


@pytest.mark.parametrize("json_mode", [False, True])
def test_main_is_not_ready_when_a_persistence_fault_exists(monkeypatch, capsys, json_mode):
    _install_report_boundary(
        monkeypatch,
        ledger_rows=[
            _new_row("dynamic_signal_summary", calls=1, prompt_in=8_000, prompt_out=800),
            _new_row("open_question_answer", calls=1, prompt_in=32_000, prompt_out=4_000),
        ],
        persistence_rows=[{"n": 1}],
        billing_cost=0.1032,
    )
    argv = ["vertex_cost_watchdog.py"] + (["--json"] if json_mode else [])
    monkeypatch.setattr(sys, "argv", argv)

    assert w.main() == 0
    rendered = capsys.readouterr().out
    if json_mode:
        decision = json.loads(rendered)["staging_enablement"]
        assert decision["ready"] is False
        assert any("persistence fault" in reason for reason in decision["reasons"])
    else:
        assert "STAGING ENABLEMENT: NOT READY" in rendered
        assert "persistence fault" in rendered


def test_scope_aware_missing_notes_do_not_call_prelaunch_consumers_broken(monkeypatch):
    _install_report_boundary(
        monkeypatch,
        ledger_rows=[
            {
                "consumer": "trend_analysis",
                "model": "gemini-3.5-flash",
                "day": "2026-08-20",
                "calls": 1,
                "prompt_in": 100,
                "prompt_out": 10,
                "invalid_events": 0,
                "duplicate_events": 0,
            }
        ],
        billing_cost=1.0,
    )

    report = w.build_report(7)
    notes = " ".join(report.notes)

    assert "dynamic_signal_summary" not in notes
    assert "open_question_answer" not in notes


def test_scope_aware_missing_notes_name_only_expected_missing_new_consumer(monkeypatch):
    _install_report_boundary(
        monkeypatch,
        ledger_rows=[_new_row("dynamic_signal_summary", calls=1, prompt_in=8_000, prompt_out=800)],
        billing_cost=1.0,
    )

    report = w.build_report(7)
    notes = " ".join(report.notes)

    assert "open_question_answer" in notes
    assert "spend is unknown, not zero" in notes
    assert "open_question_answer wrote no" not in notes
    assert "open_question_answer" not in " ".join(
        note for note in report.notes if "broken writer" in note
    )


def test_report_json_and_text_render_ceilings_and_enablement_without_monthly_volume():
    report = w.CostReport(
        7,
        "2026-08-18",
        "2026-08-25",
        enablement=w.staging_enablement_result(**_ready_kwargs()),
    )

    payload = report.to_dict()
    rendered = w.render_text(report)

    assert payload["per_call_ceilings"] == [
        {
            "consumer": "dynamic_signal_summary",
            "model": "gemini-3.5-flash",
            "input_tokens": 8_000,
            "output_tokens": 800,
            "maximum_usd_per_call": pytest.approx(0.0192),
        },
        {
            "consumer": "open_question_answer",
            "model": "gemini-3.5-flash",
            "input_tokens": 32_000,
            "output_tokens": 4_000,
            "maximum_usd_per_call": pytest.approx(0.084),
        },
    ]
    assert payload["staging_enablement"] == {"ready": True, "reasons": []}
    assert "PER-CALL CEILINGS" in rendered
    assert "dynamic_signal_summary" in rendered
    assert "8000 input tokens" in rendered
    assert "$0.019200 maximum per call" in rendered
    assert "open_question_answer" in rendered
    assert "32000 input tokens" in rendered
    assert "$0.084000 maximum per call" in rendered
    assert "STAGING ENABLEMENT: READY" in rendered
    assert "monthly calls" not in rendered.lower()
    assert all("month" not in key for row in payload["per_call_ceilings"] for key in row)
