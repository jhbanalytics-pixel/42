"""Collection-only mode of the producer.

Drives ``_run_impl(stop_after_ingestion=True, receipt_ledger=...)`` with
connector fixtures, an in-memory receipt ledger and spies on the real scoring
and send entry points, and reads the terminal receipt back at the
post-collection boundary. Nothing here opens a network connection or
constructs a cloud client.
"""

import importlib
import inspect
import socket
import sys
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

STAGING_DATASET = "intelligence_42_sources_staging"
POLICY = "c" * 64
AUTHORITY = {
    "CLOUD_RUN_EXECUTION": "exec-0001",
    "COLLECTION_SOURCE_SHA": "a" * 40,
    "COLLECTION_IMAGE_URI": "registry.example/engine@sha256:" + "b" * 64,
    "COLLECTION_POLICY_SHA256": POLICY,
    "COLLECTION_PROFILE_SHA256": "d" * 64,
}
IDENTITY = {
    "execution_id": AUTHORITY["CLOUD_RUN_EXECUTION"],
    "source_sha": AUTHORITY["COLLECTION_SOURCE_SHA"],
    "image_uri": AUTHORITY["COLLECTION_IMAGE_URI"],
    "policy_sha256": AUTHORITY["COLLECTION_POLICY_SHA256"],
    "profile_sha256": AUTHORITY["COLLECTION_PROFILE_SHA256"],
}
DIGEST_VARIABLES = {
    "COLLECTION_SOURCE_SHA": "source_sha",
    "COLLECTION_IMAGE_URI": "image_uri",
    "COLLECTION_POLICY_SHA256": "policy_sha256",
    "COLLECTION_PROFILE_SHA256": "profile_sha256",
}
ALL_COLLECTED = {"za": "collected", "ng": "collected", "ke": "collected"}
ALL_SKIPPED = {"za": "skipped", "ng": "skipped", "ke": "skipped"}


def _today():
    return datetime.now(UTC).date().isoformat()


class Ledger:
    """In-memory stand-in for the durable receipt store keyed by operation."""

    def __init__(self):
        self.rows = []

    def operation(self, operation_id):
        for _day, receipt in self.rows:
            if receipt["execution_id"] == operation_id:
                return dict(receipt)
        return None

    def market_day(self, trend_date):
        return [dict(receipt) for day, receipt in self.rows if day == trend_date]

    def record(self, receipt, *, trend_date):
        self.rows.append((trend_date, dict(receipt)))


class Vendor:
    """Builds connector classes whose every fetch is counted as a vendor call."""

    def __init__(self):
        self.calls = []

    def connector(self, source_key, *, rows=1, failing=frozenset()):
        vendor = self

        class Connector:
            def __init__(self, *, market):
                self.market = market

            def safe_fetch(self):
                vendor.calls.append((source_key, self.market))
                if self.market in failing:
                    raise PermissionError(f"{source_key} denied for {self.market}")
                return pd.DataFrame(
                    [{"id": f"{source_key}-{self.market}-{index}"} for index in range(rows)]
                )

            @staticmethod
            def empty_dataframe():
                return pd.DataFrame()

        return (source_key, Connector)


@pytest.fixture
def producer(monkeypatch):
    sys.path.insert(0, "scripts")
    import run_rss_now  # type: ignore

    run = importlib.reload(run_rss_now)
    from src.observability import events

    event_rows = []
    monkeypatch.setattr(socket.socket, "connect", lambda *_args: pytest.fail("network attempted"))
    monkeypatch.setattr(
        events,
        "insert_dataframe",
        lambda frame, _table: event_rows.extend(frame.to_dict(orient="records")),
    )
    monkeypatch.setattr(run, "get_dataset", lambda: STAGING_DATASET)
    monkeypatch.setattr("src.utils.config_loader.load_sources", lambda: {})
    monkeypatch.setattr(
        "src.ingestion.connectors.ensemble._ensemble_enabled", lambda _config: False
    )
    guard = MagicMock(name="_markets_already_ingested_today", return_value=set())
    monkeypatch.setattr(run, "_markets_already_ingested_today", guard)
    cleanup = MagicMock(name="_cleanup_market_day_rows")
    monkeypatch.setattr(run, "_cleanup_market_day_rows", cleanup)
    monkeypatch.setattr(run, "_begin_wave1_durable_execution", lambda *_args: None)
    monkeypatch.setattr(run, "_prepare_funded_socialcrawl", lambda *_args: None)
    monkeypatch.setattr(run, "_CONNECTOR_PARALLEL_OK", frozenset())
    dropped = set()

    def ingest(market, frames, _run_id, _started_at, _telemetry=None):
        rows = 0 if market in dropped else sum(len(frame) for frame in frames)
        return rows, rows, {}, 0, {}, {}

    monkeypatch.setattr(run, "_ingest_market_frames", ingest)
    pipeline_rows = []
    pipeline_writer = MagicMock(
        name="log_pipeline_run",
        side_effect=lambda *args, **kwargs: pipeline_rows.append((args, kwargs)),
    )
    monkeypatch.setattr(run, "log_pipeline_run", pipeline_writer)
    for variable, value in AUTHORITY.items():
        monkeypatch.setenv(variable, value)
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", "jhb_core")
    monkeypatch.delenv("FORCE_REINGEST_MARKETS", raising=False)
    spies = {
        "scoring": MagicMock(
            name="compute_trend_scores", side_effect=AssertionError("scoring entry point called")
        ),
        "send": MagicMock(
            name="send_daily_digest", side_effect=AssertionError("send entry point called")
        ),
    }
    monkeypatch.setattr(run, "compute_trend_scores", spies["scoring"])
    monkeypatch.setattr("src.alerts.detector.send_daily_digest", spies["send"])
    return SimpleNamespace(
        run=run,
        ledger=Ledger(),
        vendor=Vendor(),
        spies=spies,
        guard=guard,
        cleanup=cleanup,
        dropped=dropped,
        pipeline_writer=pipeline_writer,
        pipeline_rows=pipeline_rows,
        event_rows=event_rows,
    )


def _plan(producer, plan, monkeypatch):
    monkeypatch.setattr(producer.run, "_connector_plan_for_run", lambda *_args: plan)


def _collect(producer, ledger=None):
    return producer.run._run_impl(
        stop_after_ingestion=True, receipt_ledger=producer.ledger if ledger is None else ledger
    )


def _assert_nothing_downstream(producer):
    assert producer.spies["scoring"].call_count == 0
    assert producer.spies["send"].call_count == 0


def _record(producer, market_states, **identity):
    from scripts.staging.collect_42_sources import collection_receipt

    recorded = collection_receipt(
        run_id="prior-run",
        cutoff="2026-09-13T00:30:00+00:00",
        market_states=market_states,
        raw_count=9,
        enriched_count=9,
        **{**IDENTITY, "execution_id": "exec-0000", **identity},
    )
    producer.ledger.record(recorded, trend_date=_today())
    return recorded


def test_run_impl_keeps_its_default_mode_signature(producer):
    parameters = inspect.signature(producer.run._run_impl).parameters
    for name in ("stop_after_ingestion", "receipt_ledger"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["stop_after_ingestion"].default is False
    assert parameters["receipt_ledger"].default is None


@pytest.mark.parametrize(
    ("plan_rows", "raw_rows"),
    [
        ({"rss": 2}, 6),
        ({"rss": 3, "apple_music": 1}, 12),
        ({"rss": 0, "apple_music": 4}, 12),
    ],
)
def test_collection_only_returns_the_receipt_before_scoring_and_send(
    producer, monkeypatch, plan_rows, raw_rows
):
    _plan(
        producer, [producer.vendor.connector(k, rows=n) for k, n in plan_rows.items()], monkeypatch
    )
    receipt = _collect(producer)
    _assert_nothing_downstream(producer)
    assert receipt["contract_version"] == "collection_receipt_v1"
    assert {field: receipt[field] for field in IDENTITY} == IDENTITY
    assert receipt["market_states"] == ALL_COLLECTED
    assert receipt["complete"] is True
    assert receipt["raw_rows_persisted"] == raw_rows
    assert receipt["enriched_rows_persisted"] == raw_rows
    assert receipt["funded_close"] is None
    assert len(producer.vendor.calls) == len(plan_rows) * len(producer.run.MARKETS)
    assert producer.ledger.rows == [(_today(), receipt)]
    ((args, kwargs),) = producer.pipeline_rows
    assert args[0] == receipt["run_id"]
    # The receipt names the closed day the run collected for, the day before the
    # run's own start day. The instants the run started and finished are not the
    # cutoff; they travel separately, to pipeline_runs.
    run_started_at, collection_completed_at = args[1], args[2]
    assert receipt["cutoff"] == (run_started_at.date() - timedelta(days=1)).isoformat()
    assert run_started_at.tzinfo is not None
    assert collection_completed_at.tzinfo is not None
    assert run_started_at <= collection_completed_at
    assert args[4] == []
    assert kwargs["skip_markets"] == set()


def test_terminal_receipt_is_admitted_by_the_completed_source_run_validator(producer, monkeypatch):
    """The D03 reader admits the receipt the terminal writes, with the run's own instants."""
    from src.analysis.open_intelligence.staging_source_profile import (
        validate_completed_source_run,
    )

    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    receipt = _collect(producer)
    ((args, _kwargs),) = producer.pipeline_rows
    run_started_at, collection_completed_at = args[1], args[2]
    instants = {
        "collection_started_at": run_started_at,
        "collection_completed_at": collection_completed_at,
    }
    validated = validate_completed_source_run({"receipt": receipt, **instants})
    assert validated["receipt"] == receipt
    assert validated["cutoff"] == run_started_at.date() - timedelta(days=1)
    assert validated["observation_window_end"] == datetime.combine(
        run_started_at.date(), time.min, UTC
    )
    assert validated["collection_started_at"] == run_started_at
    assert validated["collection_completed_at"] == collection_completed_at
    # The instant the run finished is not a cutoff: the timestamp shape still refuses.
    stamped = {**receipt, "cutoff": collection_completed_at.isoformat()}
    with pytest.raises(ValueError, match="source_run_invalid"):
        validate_completed_source_run({"receipt": stamped, **instants})


@pytest.mark.parametrize(
    ("failing", "state", "raw_rows"),
    [
        ({"rss"}, "partial", 5),
        ({"rss", "apple_music"}, "failed", 4),
    ],
)
def test_partial_market_failure_names_the_market_and_is_not_complete(
    producer, monkeypatch, failing, state, raw_rows
):
    plan = [
        producer.vendor.connector(key, failing=frozenset({"ng"}) if key in failing else frozenset())
        for key in ("rss", "apple_music")
    ]
    _plan(producer, plan, monkeypatch)
    receipt = _collect(producer)
    _assert_nothing_downstream(producer)
    assert receipt["market_states"] == {"za": "collected", "ng": state, "ke": "collected"}
    assert receipt["complete"] is False
    assert receipt["raw_rows_persisted"] == raw_rows
    assert producer.ledger.rows[0][1] == receipt


def test_market_with_nothing_to_persist_is_empty_not_complete(producer, monkeypatch):
    _plan(producer, [producer.vendor.connector("rss", rows=0)], monkeypatch)
    receipt = _collect(producer)
    _assert_nothing_downstream(producer)
    assert receipt["market_states"] == {"za": "empty", "ng": "empty", "ke": "empty"}
    assert receipt["complete"] is False
    assert receipt["raw_rows_persisted"] == 0


def test_market_whose_rows_were_all_dropped_at_ingest_is_empty(producer, monkeypatch):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    producer.dropped.add("ng")
    receipt = _collect(producer)
    assert receipt["market_states"] == {"za": "collected", "ng": "empty", "ke": "collected"}
    assert receipt["complete"] is False
    assert receipt["raw_rows_persisted"] == 4


def test_duplicate_operation_returns_the_same_receipt_without_a_second_vendor_call(
    producer, monkeypatch
):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    first = _collect(producer)
    calls = list(producer.vendor.calls)
    assert len(calls) == 3
    second = _collect(producer)
    _assert_nothing_downstream(producer)
    assert second == first
    assert producer.vendor.calls == calls
    assert len(producer.ledger.rows) == 1
    assert len(producer.pipeline_rows) == 1


@pytest.mark.parametrize("variable", sorted(DIGEST_VARIABLES))
def test_conflicting_duplicate_operation_is_refused(producer, monkeypatch, variable):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    first = _collect(producer)
    calls = list(producer.vendor.calls)
    monkeypatch.setenv(variable, "f" * 64)
    with pytest.raises(RuntimeError, match="exec-0001 conflicts"):
        _collect(producer)
    _assert_nothing_downstream(producer)
    assert producer.vendor.calls == calls
    assert producer.ledger.rows == [(_today(), first)]


@pytest.mark.parametrize("variable", sorted(AUTHORITY))
def test_missing_authority_is_refused_before_any_connector_call(producer, monkeypatch, variable):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    monkeypatch.delenv(variable)
    with pytest.raises(RuntimeError, match=f"collection authority is missing: {variable}"):
        _collect(producer)
    _assert_nothing_downstream(producer)
    assert producer.vendor.calls == []
    assert producer.guard.call_count == 0
    assert producer.ledger.rows == []
    assert producer.pipeline_rows == []


def test_unbound_ledger_is_refused_before_any_connector_call(producer, monkeypatch):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    with pytest.raises(RuntimeError, match="collection receipt ledger is not bound"):
        producer.run._run_impl(stop_after_ingestion=True)
    _assert_nothing_downstream(producer)
    assert producer.vendor.calls == []
    assert producer.guard.call_count == 0


def test_force_reingest_is_refused_in_collection_only_mode(producer, monkeypatch):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    monkeypatch.setenv("FORCE_REINGEST_MARKETS", "za")
    with pytest.raises(RuntimeError, match="never force deletes"):
        _collect(producer)
    assert producer.vendor.calls == []
    assert producer.cleanup.call_count == 0


# The ledger record is the single commit point.
def test_crash_between_the_ledger_record_and_the_pipeline_log_keeps_the_receipt(
    producer, monkeypatch
):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    producer.pipeline_writer.side_effect = ConnectionError("pipeline_runs insert failed")
    with pytest.raises(ConnectionError):
        _collect(producer)
    ((_day, recorded),) = producer.ledger.rows
    assert recorded["market_states"] == ALL_COLLECTED
    calls = list(producer.vendor.calls)
    retried = _collect(producer)
    _assert_nothing_downstream(producer)
    assert retried == recorded
    assert retried["market_states"] != ALL_SKIPPED
    assert producer.vendor.calls == calls
    assert producer.ledger.rows == [(_today(), recorded)]


def test_partial_first_attempt_without_a_receipt_is_recollected_never_skipped(
    producer, monkeypatch
):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    monkeypatch.setattr(
        producer.run,
        "_close_funded_socialcrawl",
        MagicMock(side_effect=[RuntimeError("crash before the record"), None]),
    )
    with pytest.raises(RuntimeError, match="crash before the record"):
        _collect(producer)
    assert producer.ledger.rows == []
    assert producer.pipeline_rows == []
    assert len(producer.vendor.calls) == 3
    receipt = _collect(producer)
    _assert_nothing_downstream(producer)
    assert receipt["market_states"] == ALL_COLLECTED
    assert receipt["complete"] is True
    assert len(producer.vendor.calls) == 6
    assert producer.ledger.rows == [(_today(), receipt)]


# The market-day decision is the ledger's, not the fail-open guard's.
def test_same_day_retrigger_under_the_same_authority_is_skipped_from_the_ledger(
    producer, monkeypatch
):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    _record(producer, ALL_COLLECTED)
    producer.guard.return_value = set()
    receipt = _collect(producer)
    _assert_nothing_downstream(producer)
    assert producer.vendor.calls == []
    assert receipt["market_states"] == ALL_SKIPPED
    assert receipt["complete"] is False
    assert receipt["raw_rows_persisted"] == 0
    assert producer.cleanup.call_count == 0
    assert producer.pipeline_rows[0][1]["skip_markets"] == set(producer.run.MARKETS)


def test_same_day_retrigger_recollects_only_the_markets_the_ledger_did_not_persist(
    producer, monkeypatch
):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    _record(producer, {"za": "collected", "ng": "failed", "ke": "partial"})
    receipt = _collect(producer)
    assert producer.vendor.calls == [("rss", "ng")]
    assert receipt["market_states"] == {"za": "skipped", "ng": "collected", "ke": "skipped"}
    assert receipt["complete"] is False
    assert receipt["raw_rows_persisted"] == 2


@pytest.mark.parametrize("guard_markets", [{"za"}, {"za", "ng", "ke"}])
def test_persisted_rows_without_a_receipt_are_held_for_recovery(
    producer, monkeypatch, guard_markets
):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    producer.guard.return_value = set(guard_markets)
    with pytest.raises(RuntimeError, match="carry no collection receipt; held for recovery"):
        _collect(producer)
    _assert_nothing_downstream(producer)
    assert producer.vendor.calls == []
    assert producer.cleanup.call_count == 0
    assert producer.ledger.rows == []
    assert producer.pipeline_rows == []


@pytest.mark.parametrize(("variable", "field"), sorted(DIGEST_VARIABLES.items()))
def test_existing_market_day_success_does_not_silently_skip_a_new_digest(
    producer, monkeypatch, variable, field
):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    recorded = _record(producer, ALL_COLLECTED, **{field: "e" * 64})
    producer.guard.return_value = set(producer.run.MARKETS)
    with pytest.raises(RuntimeError, match=f"another {field}; held for recovery"):
        _collect(producer)
    _assert_nothing_downstream(producer)
    assert producer.vendor.calls == []
    assert producer.cleanup.call_count == 0
    assert producer.pipeline_rows == []
    assert [receipt for _day, receipt in producer.ledger.rows] == [recorded]


def test_a_day_that_persisted_nothing_under_another_digest_is_collected(producer, monkeypatch):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    _record(producer, {"za": "failed", "ng": "empty", "ke": "skipped"}, policy_sha256="e" * 64)
    receipt = _collect(producer)
    assert receipt["market_states"] == ALL_COLLECTED
    assert len(producer.vendor.calls) == 3


def test_funded_close_with_unresolved_balance_is_retained_as_unknown(producer, monkeypatch):
    from src.analysis.open_intelligence import funded_lane_runtime

    unavailable = funded_lane_runtime.FundedRunCloseReceipt(
        attribution_state="gap_detected",
        calls=4,
        budget_debit_credits=Decimal("12"),
        vendor_reported_credits=Decimal("12"),
        balance_delta=None,
        attribution_gap_credits=None,
        balance_read_status="unavailable",
        terminal_id="terminal-0001",
    )
    runtime = SimpleNamespace(
        stage_name="stage_1_pilot",
        execution_capability=None,
        close=MagicMock(name="close", side_effect=AssertionError("measured close called")),
        close_unavailable=MagicMock(name="close_unavailable", return_value=unavailable),
    )
    monkeypatch.setattr(producer.run, "_prepare_funded_socialcrawl", lambda *_args: runtime)
    monkeypatch.setattr(
        funded_lane_runtime,
        "read_funded_balance",
        MagicMock(side_effect=ConnectionError("balance endpoint unreachable")),
    )
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    receipt = _collect(producer)
    _assert_nothing_downstream(producer)
    assert runtime.close.call_count == 0
    assert runtime.close_unavailable.call_count == 1
    assert receipt["funded_close"] == {
        "attribution_state": "gap_detected",
        "balance_read_status": "unavailable",
        "calls": 4,
        "balance_delta": None,
        "run_balance_delta": None,
        "terminal_id": "terminal-0001",
    }
    assert receipt["complete"] is True
    assert producer.ledger.rows[0][1]["funded_close"]["balance_delta"] is None


def test_collect_staging_returns_the_producer_receipt_through_the_bound_collector(monkeypatch):
    import scripts.run_rss_now as producer_module
    from scripts.staging.collect_42_sources import (
        collect_staging,
        producer_collector,
        unverified_authority,
    )

    calls = []
    receipt = {"contract_version": "collection_receipt_v1", "complete": False}
    monkeypatch.setattr(
        producer_module, "_run_impl", lambda **kwargs: (calls.append(kwargs), receipt)[1]
    )
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("BIGQUERY_DATASET", STAGING_DATASET)
    ledger = Ledger()
    collector = producer_collector(receipt_ledger=ledger)
    assert collector.__wrapped__ is producer_module._run_impl
    with pytest.raises(TypeError):
        collector(stop_after_ingestion=True, diagnostics=None)
    result = collect_staging(
        {"environment": "staging", "source_dataset": STAGING_DATASET},
        authority=unverified_authority("run_rss_now_collection_only"),
        collector=collector,
    )
    assert result is receipt
    assert calls == [{"stop_after_ingestion": True, "receipt_ledger": ledger}]


INJECTED = {**IDENTITY, "execution_id": "attempt:collect:7"}


def _collect_with(producer, authority, ledger=None):
    return producer.run._run_impl(
        stop_after_ingestion=True,
        receipt_ledger=producer.ledger if ledger is None else ledger,
        collection_authority=authority,
    )


def test_injected_authority_replaces_the_environment_read(producer, monkeypatch):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    for variable in AUTHORITY:
        monkeypatch.delenv(variable)
    receipt = _collect_with(producer, INJECTED)
    _assert_nothing_downstream(producer)
    assert {field: receipt[field] for field in INJECTED} == INJECTED
    assert receipt["market_states"] == ALL_COLLECTED
    assert receipt["complete"] is True
    assert producer.ledger.rows == [(_today(), receipt)]
    assert producer.ledger.operation("attempt:collect:7") == receipt


def test_injected_authority_wins_over_a_conflicting_environment(producer, monkeypatch):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    receipt = _collect_with(producer, INJECTED)
    assert receipt["execution_id"] == "attempt:collect:7"
    assert producer.ledger.operation(AUTHORITY["CLOUD_RUN_EXECUTION"]) is None
    assert producer.ledger.operation("attempt:collect:7") == receipt


def test_injected_authority_reconciles_the_same_attempt_without_a_second_vendor_call(
    producer, monkeypatch
):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    for variable in AUTHORITY:
        monkeypatch.delenv(variable)
    first = _collect_with(producer, INJECTED)
    calls = list(producer.vendor.calls)
    assert len(calls) == 3
    second = _collect_with(producer, dict(INJECTED))
    _assert_nothing_downstream(producer)
    assert second == first
    assert producer.vendor.calls == calls
    assert len(producer.ledger.rows) == 1
    assert len(producer.pipeline_rows) == 1


@pytest.mark.parametrize("field", sorted(IDENTITY))
@pytest.mark.parametrize("value", ["", "   ", None, 7], ids=["empty", "blank", "none", "int"])
def test_injected_authority_with_a_missing_or_empty_field_is_refused(
    producer, monkeypatch, field, value
):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    for variable in AUTHORITY:
        monkeypatch.delenv(variable)
    with pytest.raises(RuntimeError, match=f"collection authority is missing: {field}"):
        _collect_with(producer, {**INJECTED, field: value})
    absent = {name: value for name, value in INJECTED.items() if name != field}
    with pytest.raises(RuntimeError, match=f"collection authority is missing: {field}"):
        _collect_with(producer, absent)
    _assert_nothing_downstream(producer)
    assert producer.vendor.calls == []
    assert producer.guard.call_count == 0
    assert producer.ledger.rows == []
    assert producer.pipeline_rows == []


def test_injected_authority_with_an_unexpected_field_is_refused(producer, monkeypatch):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    with pytest.raises(
        RuntimeError, match="collection authority carries unexpected fields: run_id"
    ):
        _collect_with(producer, {**INJECTED, "run_id": "r-1"})
    assert producer.vendor.calls == []
    assert producer.ledger.rows == []


def test_producer_collector_forwards_the_injected_authority(monkeypatch):
    import scripts.run_rss_now as producer_module
    from scripts.staging.collect_42_sources import (
        collect_staging,
        producer_collector,
        unverified_authority,
    )

    calls = []
    receipt = {"contract_version": "collection_receipt_v1", "complete": False}
    monkeypatch.setattr(
        producer_module, "_run_impl", lambda **kwargs: (calls.append(kwargs), receipt)[1]
    )
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("BIGQUERY_DATASET", STAGING_DATASET)
    ledger = Ledger()
    collector = producer_collector(receipt_ledger=ledger, collection_authority=INJECTED)
    result = collect_staging(
        {"environment": "staging", "source_dataset": STAGING_DATASET},
        authority=unverified_authority("run_rss_now_collection_only"),
        collector=collector,
    )
    assert result is receipt
    assert calls == [
        {"stop_after_ingestion": True, "receipt_ledger": ledger, "collection_authority": INJECTED}
    ]


# The profile digest cross check, on the path that builds a real receipt. The
# entry point hands the producer the profile the run is placed under, so the
# digest the job definition stamps is checked against the profile that was used
# rather than recorded on the job definition's own word.
def _profile():
    from scripts.staging.collect_42_sources import entry_point_profile

    return entry_point_profile()


def test_collection_only_checks_the_stamped_profile_digest_against_the_profile(
    producer, monkeypatch
):
    profile = _profile()
    monkeypatch.setenv("COLLECTION_PROFILE_SHA256", profile["profile_sha256"])
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    receipt = producer.run._run_impl(
        stop_after_ingestion=True,
        receipt_ledger=producer.ledger,
        collection_profile=profile,
    )
    _assert_nothing_downstream(producer)
    assert receipt["profile_sha256"] == profile["profile_sha256"]
    assert receipt["market_states"] == ALL_COLLECTED
    assert producer.ledger.rows == [(_today(), receipt)]


def test_collection_only_refuses_a_stamped_profile_digest_naming_another_profile(
    producer, monkeypatch
):
    """A well shaped digest of something this run never used is not this run's profile.

    ``COLLECTION_PROFILE_SHA256`` stays at its fixture value here, which is a
    valid sha256 and is not the digest of the profile handed in. Without the
    profile reaching the receipt call the value is taken on the environment's
    word and the run is recorded as clean.
    """
    profile = _profile()
    assert AUTHORITY["COLLECTION_PROFILE_SHA256"] != profile["profile_sha256"]
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    with pytest.raises(ValueError, match="profile_sha256 must be the digest of the profile"):
        producer.run._run_impl(
            stop_after_ingestion=True,
            receipt_ledger=producer.ledger,
            collection_profile=profile,
        )
    _assert_nothing_downstream(producer)
    assert producer.ledger.rows == []


def test_collection_only_checks_the_profile_of_the_authority_it_was_handed(producer, monkeypatch):
    """The in-process authority is checked against the profile too, not only the environment."""
    profile = _profile()
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    authority = {**IDENTITY, "profile_sha256": profile["profile_sha256"]}
    receipt = producer.run._run_impl(
        stop_after_ingestion=True,
        receipt_ledger=producer.ledger,
        collection_authority=authority,
        collection_profile=profile,
    )
    assert receipt["profile_sha256"] == profile["profile_sha256"]
    with pytest.raises(ValueError, match="profile_sha256 must be the digest of the profile"):
        producer.run._run_impl(
            stop_after_ingestion=True,
            receipt_ledger=Ledger(),
            collection_authority={**IDENTITY, "execution_id": "exec-0002"},
            collection_profile=profile,
        )


@pytest.mark.parametrize("kind", ["verified_manifest", "unverified"])
def test_collection_only_records_the_authority_kind_it_was_placed_under(
    producer, monkeypatch, kind
):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    receipt = producer.run._run_impl(
        stop_after_ingestion=True, receipt_ledger=producer.ledger, authority_kind=kind
    )
    assert receipt["authority_kind"] == kind
    assert producer.ledger.operation(receipt["execution_id"])["authority_kind"] == kind


def test_collection_only_names_no_authority_kind_when_none_was_placed(producer, monkeypatch):
    _plan(producer, [producer.vendor.connector("rss", rows=2)], monkeypatch)
    assert "authority_kind" not in _collect(producer)


def test_the_bound_collector_forwards_the_placement_to_the_producer(producer, monkeypatch):
    """``producer_collector`` is the only binding site, so the placement travels through it."""
    import scripts.run_rss_now as bound_module
    from scripts.staging.collect_42_sources import producer_collector

    seen = {}
    monkeypatch.setattr(bound_module, "_run_impl", lambda **kwargs: seen.update(kwargs) or {})
    profile = _profile()
    collector = producer_collector(
        receipt_ledger=producer.ledger,
        collection_authority=IDENTITY,
        collection_profile=profile,
        authority_kind="verified_manifest",
    )
    assert collector(stop_after_ingestion=True) == {}
    assert seen == {
        "stop_after_ingestion": True,
        "receipt_ledger": producer.ledger,
        "collection_authority": dict(IDENTITY),
        "collection_profile": profile,
        "authority_kind": "verified_manifest",
    }
