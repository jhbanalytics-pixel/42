"""Unit tests for the interim multi-connector run script.

Tests pure functions (build_raw_row, compute_trend_scores aggregation,
log_pipeline_run status logic, per-market scoring) that do not require
BigQuery or network access.
"""

import inspect
import json
import socket
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def load_run_module():
    """Import run_rss_now fresh so monkey-patches do not leak."""
    import importlib
    import sys

    sys.path.insert(0, "scripts")
    import run_rss_now  # type: ignore

    importlib.reload(run_rss_now)
    return run_rss_now


@pytest.fixture
def diagnostic_pipeline(load_run_module, monkeypatch):
    from src.observability import events

    run = load_run_module
    rows = []
    monkeypatch.setattr(socket.socket, "connect", lambda *_args: pytest.fail("network attempted"))
    monkeypatch.setattr(
        events,
        "insert_dataframe",
        lambda frame, _table: rows.extend(frame.to_dict(orient="records")),
    )
    monkeypatch.setattr(run, "MARKETS", ["za"])
    monkeypatch.setattr(run, "get_dataset", lambda: "synthetic")
    monkeypatch.setattr("src.utils.config_loader.load_sources", lambda: {})
    monkeypatch.setattr(
        "src.ingestion.connectors.ensemble._ensemble_enabled", lambda _config: False
    )
    monkeypatch.setattr(run, "_markets_already_ingested_today", lambda: set())
    monkeypatch.setattr(run, "_parse_force_reingest_markets", lambda: set())
    monkeypatch.setattr(run, "_begin_wave1_durable_execution", lambda *_args: None)
    run._real_connector_plan_for_test = run._connector_plan_for_run
    monkeypatch.setattr(run, "_connector_plan_for_run", lambda *_args: [])
    monkeypatch.setattr(run, "_prepare_funded_socialcrawl", lambda *_args: None)
    monkeypatch.setattr(run, "_close_funded_socialcrawl", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run, "load_scoring", lambda: {})
    monkeypatch.setattr(run, "compute_velocity_scores_for_today", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(run, "compute_velocity_windows_for_today", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(run, "compute_trend_scores", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(run, "log_pipeline_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run, "_log_email_status", lambda *_args: None)
    monkeypatch.setattr(run, "_email_already_sent_today", lambda: False)
    for helper in (
        "_micro_briefs_enabled",
        "_continuity_badges_enabled",
        "_lifecycle_enabled",
        "_seed_intelligence_enabled",
        "_tone_split_enabled",
        "_seed_score_enabled",
    ):
        monkeypatch.setattr(run, helper, lambda: False)
    for flag in (
        "SEED_GRAPH_ENABLED",
        "SEED_CANDIDATES_ENABLED",
        "PHASE_2_ENABLED",
        "FORECAST_ENABLED",
        "PAN_AFRICAN_ENABLED",
        "COMMENT_SENTIMENT_ENABLED",
        "DRIVING_HASHTAGS_ENABLED",
        "RECONCILE_ENABLED",
    ):
        monkeypatch.setenv(flag, "false")
    monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", "jhb_core")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "synthetic_execution")
    monkeypatch.setattr("src.alerts.self_heal.heal_errors", lambda errors, _path: ([], errors))
    monkeypatch.setattr(
        "src.alerts.detector.send_daily_digest",
        lambda *_args, **_kwargs: (0, 0, 0, "skipped_nothing_notable"),
    )
    return run, rows


def _diagnostic_terminals(rows):
    return [row for row in rows if row["event_type"] == "cron_run"]


def test_pipeline_batches_event_loads_without_losing_timeline(
    diagnostic_pipeline, monkeypatch, capsys
):
    from src.observability import events

    run, rows = diagnostic_pipeline
    loads = []

    def persist(frame, _table):
        loads.append(len(frame))
        rows.extend(frame.to_dict(orient="records"))
        return len(frame)

    monkeypatch.setattr(events, "insert_dataframe", persist)
    run.run()
    assert len(loads) == 1
    assert loads[0] >= 5
    assert _diagnostic_terminals(rows)[0]["status"] == "ok"
    streamed = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")
    ]
    assert [row["event_id"] for row in streamed] == [row["event_id"] for row in rows]


def test_pipeline_diagnostics_keep_empty_results_and_disabled_stages_normal(diagnostic_pipeline):
    run, rows = diagnostic_pipeline
    assert run.run() is None
    assert sum(row["event_type"] == "cron_run_started" for row in rows) == 1
    terminal = _diagnostic_terminals(rows)
    assert len(terminal) == 1
    assert terminal[0]["status"] == "ok"
    contexts = [json.loads(row["meta"]) for row in rows]
    assert len({context["run_id"] for context in contexts}) == 1
    assert all(context["cloud_execution"] == "synthetic_execution" for context in contexts)
    assert {
        context["stage"]
        for row, context in zip(rows, contexts, strict=True)
        if row["status"] == "skipped"
    } >= {"seed_graph", "seed_candidates", "digest"}
    assert not any(row["severity"] == "ERROR" for row in rows)


@pytest.mark.parametrize("stage", ["seed_graph", "seed_candidates"])
def test_pipeline_seed_failure_reaches_nonfatal_caller_and_degrades_terminal(
    diagnostic_pipeline, monkeypatch, capsys, stage
):
    run, rows = diagnostic_pipeline
    monkeypatch.setenv(f"{stage.upper()}_ENABLED", "true")
    if stage == "seed_graph":
        import pandas as pd
        from src.analysis import seed_graph

        client = MagicMock(project="synthetic")
        client.query.return_value.to_dataframe.return_value = pd.DataFrame()
        client.load_table_from_file.return_value.result.side_effect = RuntimeError(
            "synthetic partition write failed"
        )
        monkeypatch.setattr(seed_graph, "get_client", lambda: client)
        monkeypatch.setattr(seed_graph, "get_dataset", lambda: "synthetic")
    else:
        monkeypatch.setattr(
            "src.analysis.seed_candidates.get_client",
            MagicMock(side_effect=PermissionError("synthetic candidate query denied")),
        )
    assert run.run() is None
    output = capsys.readouterr().out
    assert f"{stage} error (non-fatal)" in output
    assert "seed_graph rows: za=0" not in output
    failed = [
        row
        for row in rows
        if row["status"] == "failed" and json.loads(row["meta"])["stage"] == stage
    ]
    assert len(failed) == 1
    assert json.loads(failed[0]["meta"])["reason_code"] == "stage_failed"
    assert _diagnostic_terminals(rows)[0]["status"] == "degraded"


@pytest.mark.parametrize("failure", ["send_failed", "raise"])
def test_pipeline_digest_failure_cannot_emit_clean_terminal(
    diagnostic_pipeline, monkeypatch, failure
):
    run, rows = diagnostic_pipeline
    sender = MagicMock(return_value=(0, 0, 0, "send_failed"))
    if failure == "raise":
        sender.side_effect = RuntimeError("synthetic render failed")
    monkeypatch.setattr("src.alerts.detector.send_daily_digest", sender)
    assert run.run() is None
    assert _diagnostic_terminals(rows)[0]["status"] == "degraded"
    failed = [row for row in rows if row["event_type"] == "digest_failed"]
    assert len(failed) == 1
    assert json.loads(failed[0]["meta"])["stage"] == "digest"
    assert (
        json.loads(failed[0]["meta"])["run_id"]
        == json.loads(_diagnostic_terminals(rows)[0]["meta"])["run_id"]
    )


def test_pipeline_uncaught_failure_has_one_terminal_and_keeps_exception(
    diagnostic_pipeline, monkeypatch
):
    run, rows = diagnostic_pipeline
    error = RuntimeError("synthetic setup failed")
    monkeypatch.setattr(run, "_begin_wave1_durable_execution", MagicMock(side_effect=error))
    with pytest.raises(RuntimeError) as raised:
        run.run()
    assert raised.value is error
    assert len(_diagnostic_terminals(rows)) == 1
    assert _diagnostic_terminals(rows)[0]["status"] == "failed"


def test_pipeline_telemetry_write_failure_does_not_break_optional_outcome(
    diagnostic_pipeline, monkeypatch
):
    run, rows = diagnostic_pipeline
    from src.observability import events

    def fail_sink(frame, _table):
        rows.extend(frame.to_dict(orient="records"))
        raise RuntimeError("synthetic telemetry unavailable")

    monkeypatch.setattr(events, "insert_dataframe", fail_sink)
    monkeypatch.setattr(
        "src.alerts.detector.send_daily_digest", lambda *_args, **_kwargs: (0, 0, 0, "send_failed")
    )
    assert run.run() is None
    assert _diagnostic_terminals(rows)[0]["status"] == "degraded"


def test_pipeline_connector_failure_shares_run_context(diagnostic_pipeline, monkeypatch):
    import pandas as pd

    run, rows = diagnostic_pipeline

    class Connector:
        def __init__(self, *, market):
            self.market = market

        def safe_fetch(self):
            return pd.DataFrame([{"id": "synthetic"}])

        @staticmethod
        def empty_dataframe():
            return pd.DataFrame()

    class FailedConnector(Connector):
        def safe_fetch(self):
            raise PermissionError("synthetic connector denied")

    monkeypatch.setattr(
        run,
        "_connector_plan_for_run",
        lambda *_args: [("rss", FailedConnector), ("apple_music", Connector)],
    )
    monkeypatch.setattr(run, "_CONNECTOR_PARALLEL_OK", frozenset())
    monkeypatch.setattr(run, "_ingest_market_frames", lambda *_args: (1, 1, {}, 0, {}, {}))
    assert run.run() is None
    failed = [row for row in rows if row["event_type"] == "connector_fail"]
    assert len(failed) == 1
    context = json.loads(failed[0]["meta"])
    terminal_context = json.loads(_diagnostic_terminals(rows)[0]["meta"])
    assert context["run_id"] == terminal_context["run_id"]
    assert context["stage"] == "connector:rss"
    assert _diagnostic_terminals(rows)[0]["status"] == "degraded"


@pytest.mark.parametrize(
    "helper,stage",
    [
        ("compute_velocity_scores_for_today", "velocity_scores"),
        ("compute_velocity_windows_for_today", "velocity_windows"),
    ],
)
def test_pipeline_caught_scoring_failure_cannot_emit_clean_terminal(
    diagnostic_pipeline, monkeypatch, helper, stage
):
    run, rows = diagnostic_pipeline
    monkeypatch.setattr(run, helper, MagicMock(side_effect=RuntimeError("synthetic lookup failed")))
    assert run.run() is None
    terminal = _diagnostic_terminals(rows)[0]
    assert terminal["status"] == "degraded"
    assert stage in json.loads(terminal["meta"])["failed_stages"]


def test_pipeline_reported_brief_failure_cannot_emit_clean_terminal(
    diagnostic_pipeline, monkeypatch
):
    run, rows = diagnostic_pipeline
    monkeypatch.setenv("PHASE_2_ENABLED", "true")
    report = SimpleNamespace(
        briefs={},
        failures={"synthetic": "failure"},
        skipped_empty=[],
        skipped_existing=[],
        estimated_cost_usd=0.0,
        persist_attempted=True,
        persist_succeeded=False,
        persist_row_count=1,
        persist_error="synthetic write failed",
    )
    monkeypatch.setattr("src.analysis.generate_briefs.generate_briefs", lambda **_kwargs: report)
    assert run.run() is None
    terminal = _diagnostic_terminals(rows)[0]
    assert terminal["status"] == "degraded"
    assert {"brief_generation", "brief_persistence"} <= set(
        json.loads(terminal["meta"])["failed_stages"]
    )


@pytest.mark.parametrize("stage", ["seed_graph", "seed_candidates"])
def test_pipeline_real_empty_seed_result_remains_successful(
    diagnostic_pipeline, monkeypatch, stage
):
    import pandas as pd
    from src.analysis import seed_candidates, seed_graph

    run, rows = diagnostic_pipeline
    client = MagicMock(project="synthetic")
    client.query.return_value.result.return_value = []
    client.query.return_value.to_dataframe.return_value = pd.DataFrame()
    client.load_table_from_file.return_value.result.return_value = None
    module = seed_graph if stage == "seed_graph" else seed_candidates
    monkeypatch.setenv(f"{stage.upper()}_ENABLED", "true")
    monkeypatch.setattr(module, "get_client", lambda: client)
    monkeypatch.setattr(module, "get_dataset", lambda: "synthetic")
    assert run.run() is None
    assert _diagnostic_terminals(rows)[0]["status"] == "ok"
    completed = [
        row for row in rows if row["status"] == "ok" and json.loads(row["meta"])["stage"] == stage
    ]
    assert len(completed) == 1
    if stage == "seed_graph":
        client.load_table_from_file.assert_called_once()


def test_pipeline_run_identity_matches_existing_pipeline_and_email_audits(
    diagnostic_pipeline, monkeypatch
):
    run, rows = diagnostic_pipeline
    audits = []
    monkeypatch.setattr(
        run, "log_pipeline_run", lambda run_id, *_args, **_kwargs: audits.append(run_id)
    )
    monkeypatch.setattr(run, "_log_email_status", lambda run_id, *_args: audits.append(run_id))
    run.run()
    identity = json.loads(_diagnostic_terminals(rows)[0]["meta"])["run_id"]
    assert audits == [identity, identity]


def test_pipeline_unresolved_receipt_remains_unavailable_without_fallback(
    diagnostic_pipeline, monkeypatch
):
    run, rows = diagnostic_pipeline
    error = run.execution_approval.ApprovalRefusal("execution_result_unresolved")
    monkeypatch.setattr(run, "_begin_wave1_durable_execution", MagicMock(side_effect=error))
    fallback = MagicMock()
    monkeypatch.setattr(run.execution_approval, "_record_execution_result", fallback)
    with pytest.raises(run.execution_approval.ApprovalRefusal) as raised:
        run.run()
    assert raised.value is error
    terminal = _diagnostic_terminals(rows)
    assert len(terminal) == 1
    assert terminal[0]["status"] == "unavailable"
    assert terminal[0]["severity"] == "WARN"
    fallback.assert_not_called()


@pytest.mark.parametrize("stage", ["connector", "seed_graph", "velocity", "digest"])
def test_pipeline_failure_sentinel_is_absent_from_stdout_and_event_rows(
    diagnostic_pipeline, monkeypatch, capsys, caplog, stage
):
    import pandas as pd

    run, rows = diagnostic_pipeline
    sentinel = "diagnostic-private-token-73628"
    failure = f"api_key={sentinel}"
    error = RuntimeError(failure)
    if stage == "connector":

        class Connector:
            def __init__(self, *, market):
                self._fetch_failures = [failure]

            def safe_fetch(self):
                return pd.DataFrame([{"id": "synthetic"}])

        monkeypatch.setattr(run, "_connector_plan_for_run", lambda *_args: [("rss", Connector)])
        monkeypatch.setattr(run, "_CONNECTOR_PARALLEL_OK", frozenset())
        monkeypatch.setattr(run, "_ingest_market_frames", lambda *_args: (1, 1, {}, 0, {}, {}))
    elif stage == "seed_graph":
        monkeypatch.setenv("SEED_GRAPH_ENABLED", "true")
        monkeypatch.setattr("src.analysis.seed_graph.get_client", MagicMock(side_effect=error))
    elif stage == "velocity":
        monkeypatch.setattr(run, "compute_velocity_scores_for_today", MagicMock(side_effect=error))
    else:
        monkeypatch.setattr("src.alerts.detector.send_daily_digest", MagicMock(side_effect=error))
    assert run.run() is None
    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in caplog.text
    assert sentinel not in json.dumps(rows, default=str)
    assert _diagnostic_terminals(rows)[0]["status"] == "degraded"


# --- build_raw_row ---------------------------------------------------------


def test_build_raw_row_computes_engagement_total(load_run_module):
    row = {
        "source": "YouTube",
        "views": 100,
        "likes": 50,
        "comments": 10,
        "shares": 5,
    }
    out = load_run_module.build_raw_row(row, "rid", datetime.now(UTC))
    assert out["engagement_total"] == 165.0


# --- _email_already_sent_today (duplicate-digest guard) --------------------


def _fake_bq_client_count(n):
    job = MagicMock()
    job.result.return_value = [MagicMock(n=n)]
    client = MagicMock()
    client.project = "p"
    client.query.return_value = job
    return client


def test_email_already_sent_true_when_sent_marker_exists(load_run_module):
    with (
        patch("src.utils.bigquery.get_client", return_value=_fake_bq_client_count(1)),
        patch.object(load_run_module, "get_dataset", return_value="ds"),
    ):
        assert load_run_module._email_already_sent_today() is True


def test_email_already_sent_false_when_no_marker(load_run_module):
    with (
        patch("src.utils.bigquery.get_client", return_value=_fake_bq_client_count(0)),
        patch.object(load_run_module, "get_dataset", return_value="ds"),
    ):
        assert load_run_module._email_already_sent_today() is False


def test_email_already_sent_failsafe_sends_on_error(load_run_module):
    # Any BigQuery error must return False so the digest still goes out
    # (a duplicate is recoverable; a missed digest is worse).
    with patch("src.utils.bigquery.get_client", side_effect=RuntimeError("bq down")):
        assert load_run_module._email_already_sent_today() is False


def test_funded_socialcrawl_helper_leaves_jhb_path_unconfigured(load_run_module, monkeypatch):
    reset = MagicMock()
    monkeypatch.setattr(load_run_module.SocialCrawlConnector, "reset_credits", reset)
    monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", "jhb_core")

    assert (
        load_run_module._prepare_funded_socialcrawl(
            "run_001", datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
        )
        is None
    )
    reset.assert_called_once_with()


def test_funded_socialcrawl_helper_configures_exact_stage_one_context(load_run_module, monkeypatch):
    from src.analysis.open_intelligence import funded_lane_runtime
    from src.ingestion.connectors import socialcrawl

    runtime = SimpleNamespace(
        run_allowance=100,
        phase_close=MagicMock(),
        assert_call_authority=MagicMock(),
    )
    client = MagicMock()
    writer = MagicMock()
    terminal_writer = MagicMock()
    prepare = MagicMock(return_value=runtime)
    configure = MagicMock()
    monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", "ogilvy_funded")
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "execution_001")
    monkeypatch.setenv("SOCIALCRAWL_FUNDED_STAGE", "1")
    monkeypatch.setattr("google.cloud.bigquery.Client", lambda **kwargs: client)
    monkeypatch.setattr(funded_lane_runtime, "prepare_funded_lane", prepare)
    monkeypatch.setattr(funded_lane_runtime, "make_funded_ledger_writer", lambda **kwargs: writer)
    monkeypatch.setattr(
        funded_lane_runtime,
        "make_funded_terminal_writer",
        lambda **kwargs: terminal_writer,
        raising=False,
    )
    monkeypatch.setattr(socialcrawl.SocialCrawlConnector, "configure_funded_run", configure)

    from tests.unit.test_source_wave1_contract import _wave1_capability

    capability = _wave1_capability()
    result = load_run_module._prepare_funded_socialcrawl(
        "run_001", datetime(2026, 8, 27, 10, 0, tzinfo=UTC), capability
    )

    assert result is runtime
    assert prepare.call_args.kwargs["dataset"] == "trends_v2_staging_funded"
    assert prepare.call_args.kwargs["activation_stage"] == 1
    assert prepare.call_args.kwargs["terminal_writer"] is terminal_writer
    context = configure.call_args.args[0]
    assert context.credential_lane == "ogilvy_funded"
    assert context.secret_id == "SOCIALCRAWL_OGILVY_API_KEY"
    assert context.run_allowance == 100
    assert context.phase_close_hook is runtime.phase_close
    assert context.pre_call_authority_hook is runtime.assert_call_authority
    assert context.execution_capability is capability


@pytest.mark.parametrize(
    ("active", "message"),
    [
        (None, "not active"),
        ("mismatch", "does not match the consumed manifest"),
    ],
)
def test_wave1_context_requires_capability_from_the_consumed_manifest(
    load_run_module, monkeypatch, active, message
):
    from src.analysis.open_intelligence import funded_lane_runtime
    from src.ingestion.connectors import socialcrawl

    runtime = SimpleNamespace(
        run_allowance=100,
        phase_close=MagicMock(),
        assert_call_authority=MagicMock(),
    )
    configure = MagicMock()
    monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", "ogilvy_funded")
    monkeypatch.setenv("SOCIALCRAWL_FUNDED_STAGE_NAME", "stage_1_wave_1")
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "execution_001")
    monkeypatch.setenv("SOCIALCRAWL_FUNDED_STAGE", "1")
    monkeypatch.setattr("google.cloud.bigquery.Client", lambda **kwargs: MagicMock())
    monkeypatch.setattr(funded_lane_runtime, "prepare_funded_lane", MagicMock(return_value=runtime))
    monkeypatch.setattr(
        funded_lane_runtime, "make_funded_ledger_writer", lambda **kwargs: MagicMock()
    )
    monkeypatch.setattr(
        funded_lane_runtime,
        "make_funded_terminal_writer",
        lambda **kwargs: MagicMock(),
        raising=False,
    )
    monkeypatch.setattr(
        funded_lane_runtime,
        "make_funded_control_writer",
        lambda **kwargs: MagicMock(),
        raising=False,
    )
    monkeypatch.setattr(socialcrawl.SocialCrawlConnector, "configure_funded_run", configure)

    from tests.unit.test_source_wave1_contract import _wave1_capability

    capability = _wave1_capability()
    if active == "mismatch":
        active = (object(), SimpleNamespace(manifest_sha256="0" * 64), "run_001")
    monkeypatch.setattr(load_run_module, "_ACTIVE_WAVE1_EXECUTION", active, raising=False)
    with pytest.raises(RuntimeError, match=message):
        load_run_module._prepare_funded_socialcrawl(
            "run_001", datetime(2026, 8, 27, 10, 0, tzinfo=UTC), capability
        )
    configure.assert_not_called()

    matching = (object(), SimpleNamespace(manifest_sha256=capability.manifest_sha256), "run_001")
    monkeypatch.setattr(load_run_module, "_ACTIVE_WAVE1_EXECUTION", matching, raising=False)
    load_run_module._prepare_funded_socialcrawl(
        "run_001", datetime(2026, 8, 27, 10, 0, tzinfo=UTC), capability
    )
    assert configure.call_args.args[0].execution_capability is capability


def test_wave1_connector_plan_only_uses_reviewed_socialcrawl_path(load_run_module):
    run = load_run_module
    ordinary = tuple(key for key, _connector in run._connector_plan_for_run(None))
    wave1 = tuple(
        key for key, _connector in run._connector_plan_for_run(SimpleNamespace(capability=object()))
    )

    assert "gdelt" in ordinary
    assert wave1 == ("socialcrawl",)
    assert set(ordinary) - set(wave1)


def test_wave1_gdelt_executes_exact_two_jobs_then_persists_bounded_rows(
    load_run_module, monkeypatch
):
    from src.analysis.open_intelligence import gdelt_wave1_persistence as persistence
    from src.ingestion.connectors import gdelt

    receipts = (
        SimpleNamespace(
            name="events",
            dry_run_receipt_id="gdry_" + "1" * 64,
            total_bytes_processed=100,
        ),
        SimpleNamespace(
            name="gcam",
            dry_run_receipt_id="gdry_" + "2" * 64,
            total_bytes_processed=200,
        ),
    )
    entries = (
        SimpleNamespace(name="events", maximum_bytes_billed=50_000_000_000),
        SimpleNamespace(name="gcam", maximum_bytes_billed=50_000_000_000),
    )
    durable = SimpleNamespace(
        capability=object(),
        gdelt_receipts=receipts,
        gdelt_entries=entries,
        authority=SimpleNamespace(approval=SimpleNamespace(manifest_sha256="a" * 64)),
    )
    events = [{"GLOBALEVENTID": 1}]
    gcam_rows = [{"document_url": "https://example.org"}]
    calls = []

    class Job:
        state = "DONE"

        def __init__(self, name, rows, processed):
            self.job_id = f"job-{name}"
            self.rows = rows
            self.total_bytes_processed = processed

        def result(self, **kwargs):
            assert kwargs == {"max_results": 10001, "retry": None, "job_retry": None}
            return self.rows

    jobs = {
        "events": Job("events", events, 110),
        "gcam": Job("gcam", gcam_rows, 220),
    }
    monkeypatch.setattr(
        gdelt,
        "execute_wave1_gdelt_query",
        lambda _client, entry, receipt, *, execution_capability: (
            calls.append((entry.name, receipt.name, execution_capability)) or jobs[entry.name]
        ),
    )
    batch = object()
    proof = SimpleNamespace(complete=True)
    monkeypatch.setattr(
        persistence,
        "build_wave1_gdelt_batch",
        lambda **kwargs: calls.append(("build", kwargs)) or batch,
    )
    monkeypatch.setattr(
        persistence,
        "persist_wave1_gdelt_batch",
        lambda **kwargs: calls.append(("persist", kwargs)) or proof,
    )
    monkeypatch.setattr(
        load_run_module.bigquery,
        "Client",
        lambda **kwargs: SimpleNamespace(client_kwargs=kwargs),
    )

    result = load_run_module._execute_wave1_gdelt(durable, "run_wave1")

    assert result.persistence is proof
    assert result.query_job_ids == (("events", "job-events"), ("gcam", "job-gcam"))
    assert result.query_bytes_processed == (("events", 110), ("gcam", 220))
    assert [call[0] for call in calls[:2]] == ["events", "gcam"]
    assert calls[2][0] == "build"
    assert calls[2][1]["events_rows"] == tuple(events)
    assert calls[2][1]["gcam_rows"] == tuple(gcam_rows)
    assert calls[3][0] == "persist"


def test_wave1_gdelt_resolves_compute_identity_before_the_result_query(
    load_run_module, monkeypatch
):
    # Refused the first funded Wave 1 pilot on staging, 4 Sep 2026, before any
    # SocialCrawl call: on Cloud Run the compute credential reports its service
    # account as the literal "default" until it is refreshed, and the GDELT
    # result query checks identity on a fresh client before any request. The
    # client is refreshed the way persistence.validate_real_client does it.
    from google.auth.compute_engine.credentials import Credentials as ComputeCredentials
    from src.analysis.open_intelligence.funded_lane import wave1_pilot_service_identity
    from src.ingestion.connectors import gdelt

    class Resolving(ComputeCredentials):
        refreshed = 0

        def refresh(self, request):
            type(self).refreshed += 1
            self._service_account_email = wave1_pilot_service_identity()

    seen = []

    def query(_client, entry, receipt, *, execution_capability):
        seen.append(_client._credentials.service_account_email)
        raise RuntimeError("stop after identity")

    monkeypatch.setattr(gdelt, "execute_wave1_gdelt_query", query)
    monkeypatch.setattr(
        load_run_module.bigquery,
        "Client",
        lambda **kwargs: SimpleNamespace(_credentials=Resolving(), **kwargs),
    )
    durable = SimpleNamespace(
        capability=object(),
        gdelt_receipts=(SimpleNamespace(name="events"), SimpleNamespace(name="gcam")),
        gdelt_entries=(SimpleNamespace(name="events"), SimpleNamespace(name="gcam")),
    )
    with pytest.raises(RuntimeError, match="stop after identity"):
        load_run_module._execute_wave1_gdelt(durable, "run_wave1")
    assert Resolving.refreshed == 1
    assert seen == [wave1_pilot_service_identity()]


def test_wave1_gdelt_refuses_result_job_over_approved_derived_ceiling(load_run_module, monkeypatch):
    from src.ingestion.connectors import gdelt

    receipt = SimpleNamespace(
        name="events",
        dry_run_receipt_id="gdry_" + "1" * 64,
        total_bytes_processed=100,
    )
    durable = SimpleNamespace(
        capability=object(),
        gdelt_receipts=(
            receipt,
            SimpleNamespace(
                name="gcam",
                dry_run_receipt_id="gdry_" + "2" * 64,
                total_bytes_processed=200,
            ),
        ),
        gdelt_entries=(
            SimpleNamespace(name="events", maximum_bytes_billed=50_000_000_000),
            SimpleNamespace(name="gcam", maximum_bytes_billed=50_000_000_000),
        ),
        authority=SimpleNamespace(approval=SimpleNamespace(manifest_sha256="a" * 64)),
    )
    job = SimpleNamespace(
        job_id="job-events",
        total_bytes_processed=121,
        state="DONE",
        result=lambda **_kwargs: (),
    )
    monkeypatch.setattr(
        gdelt,
        "execute_wave1_gdelt_query",
        lambda *_args, **_kwargs: job,
    )
    monkeypatch.setattr(load_run_module.bigquery, "Client", lambda **_kwargs: object())
    with pytest.raises(RuntimeError, match="byte ceiling"):
        load_run_module._execute_wave1_gdelt(durable, "run_wave1")


@pytest.mark.parametrize("stage", ["", "0", "01", "4", "bad"])
def test_funded_socialcrawl_helper_refuses_invalid_stage_before_client(
    load_run_module, monkeypatch, stage
):
    client = MagicMock()
    monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", "ogilvy_funded")
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "execution_001")
    monkeypatch.setenv("SOCIALCRAWL_FUNDED_STAGE", stage)
    monkeypatch.setattr("google.cloud.bigquery.Client", client)

    with pytest.raises(RuntimeError, match="stage"):
        load_run_module._prepare_funded_socialcrawl(
            "run_001", datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
        )
    client.assert_not_called()


def test_funded_socialcrawl_stage_comes_from_the_approved_stage_name(load_run_module, monkeypatch):
    # The sixth Wave 1 pilot on staging, 4 Sep 2026, refused "funded SocialCrawl
    # stage is invalid": the approved wave1_pilot manifest environment carries
    # SOCIALCRAWL_FUNDED_STAGE_NAME=stage_1_wave_1 and no numeric stage, and
    # that environment is pinned by the deployed approval routine. The stage
    # name carries the activation stage, so the runner reads it from there when
    # the numeric variable is absent, and refuses when the two disagree.
    from src.analysis.open_intelligence import funded_lane_runtime

    seen = {}

    def prepare(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop after stage")

    monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", "ogilvy_funded")
    monkeypatch.setenv("SOCIALCRAWL_FUNDED_STAGE_NAME", "stage_1_wave_1")
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "execution_001")
    monkeypatch.delenv("SOCIALCRAWL_FUNDED_STAGE", raising=False)
    monkeypatch.setattr("google.cloud.bigquery.Client", lambda **kwargs: MagicMock())
    monkeypatch.setattr(funded_lane_runtime, "prepare_funded_lane", prepare)
    monkeypatch.setattr(funded_lane_runtime, "make_funded_ledger_writer", lambda **kwargs: None)
    monkeypatch.setattr(funded_lane_runtime, "make_funded_control_writer", lambda **kwargs: None)
    monkeypatch.setattr(funded_lane_runtime, "make_funded_terminal_writer", lambda **kwargs: None)
    monkeypatch.setattr(
        "src.analysis.open_intelligence.funded_lane._require_wave1_execution_capability",
        lambda value, *, action: value,
    )
    with pytest.raises(RuntimeError, match="stop after stage"):
        load_run_module._prepare_funded_socialcrawl(
            "run_001", datetime(2026, 9, 4, 14, 0, tzinfo=UTC), object(), ()
        )
    assert seen["activation_stage"] == 1

    monkeypatch.setenv("SOCIALCRAWL_FUNDED_STAGE", "2")
    with pytest.raises(RuntimeError, match="stage"):
        load_run_module._prepare_funded_socialcrawl(
            "run_001", datetime(2026, 9, 4, 14, 0, tzinfo=UTC), object(), ()
        )


def test_close_funded_socialcrawl_reconciles_fresh_balance(load_run_module, monkeypatch):
    from src.analysis.open_intelligence import funded_lane_runtime

    runtime = MagicMock()
    now = datetime(2026, 8, 27, 11, 0, tzinfo=UTC)
    monkeypatch.setattr(
        funded_lane_runtime, "read_funded_balance", lambda **kwargs: Decimal("250000")
    )

    result = load_run_module._close_funded_socialcrawl(runtime, now)

    assert result is runtime.close.return_value
    runtime.close.assert_called_once_with(post_balance=Decimal("250000"), recorded_at=now)


def _issued_wave1_authority():
    from tests.unit.test_execution_runtime_v2 import fixture, load

    return load(fixture("wave1_pilot"))


def _funded_result_client(run, monkeypatch):
    """The execution credential the durable entry checks before it consumes."""
    from src.analysis.open_intelligence.funded_lane import wave1_pilot_service_identity

    client = SimpleNamespace(
        _credentials=SimpleNamespace(service_account_email=wave1_pilot_service_identity())
    )
    monkeypatch.setattr(run, "_wave1_result_client", lambda: client)


def _wave1_contract_artifact(route_set_sha256=None):
    import json

    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SET_SHA256

    payload = {"contract_version": "wave1-pilot-durable-v1", "max_credits": 63}
    payload["route_set_sha256"] = (
        WAVE1_ROUTE_SET_SHA256 if route_set_sha256 is None else route_set_sha256
    )
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_wave1_durable_entry_refuses_enabled_embedding_before_consumption(
    load_run_module, monkeypatch
):
    run = load_run_module
    monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", "ogilvy_funded")
    monkeypatch.setenv("SOCIALCRAWL_FUNDED_STAGE_NAME", "stage_1_wave_1")
    monkeypatch.setenv("EMBEDDING_CLASSIFIER_ENABLED", "true")
    monkeypatch.setattr(
        run.execution_approval,
        "_load_execution_authority",
        lambda *_args, **_kwargs: pytest.fail("approval loader reached with model flag"),
    )

    with pytest.raises(RuntimeError, match="zero-model authority"):
        run._begin_wave1_durable_execution("run_wave1", datetime(2026, 9, 27, 12, tzinfo=UTC))


def test_wave1_durable_entry_refuses_a_contract_artifact_without_route_digest(
    load_run_module, monkeypatch
):
    run = load_run_module
    artifacts = {
        "wave1_contract": b'{"contract_version":"wave1-pilot-durable-v1","max_credits":63}',
        "r3_seed_manifest": b'{"seed":1}',
        "gdelt_dry_run_set": b'{"receipts":[]}',
        "source_lab_snapshot": b"source-lab",
        "funded_preflight": b"preflight",
    }
    authority = _issued_wave1_authority()
    consumption = SimpleNamespace(consumption_id="exc_" + "c" * 64)
    monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", "ogilvy_funded")
    monkeypatch.setenv("SOCIALCRAWL_FUNDED_STAGE_NAME", "stage_1_wave_1")
    monkeypatch.setattr("google.cloud.bigquery.Client", lambda **_kwargs: object())
    monkeypatch.setattr(
        run, "_build_wave1_execution_artifacts", lambda **_kwargs: artifacts, raising=False
    )
    _funded_result_client(run, monkeypatch)

    def load(operation, *, mode, artifact_reader):
        assert operation == "wave1_pilot"
        assert mode == "new_consume"
        for name in artifacts:
            artifact_reader(name)
        return authority

    monkeypatch.setattr(run.execution_approval, "_load_execution_authority", load, raising=False)
    monkeypatch.setattr(
        run.execution_approval,
        "_consume_execution_authority",
        lambda _authority: consumption,
        raising=False,
    )
    monkeypatch.setattr(
        run,
        "_issue_wave1_execution_capability",
        lambda *_args, **_kwargs: pytest.fail("capability issued without a route digest"),
        raising=False,
    )
    monkeypatch.setattr(run, "_ACTIVE_WAVE1_EXECUTION", None, raising=False)
    with pytest.raises(RuntimeError, match="no route set digest"):
        run._begin_wave1_durable_execution("run_wave1", datetime(2026, 8, 31, 12, tzinfo=UTC))
    run._ACTIVE_WAVE1_EXECUTION = None


def test_wave1_durable_entry_consumes_once_before_capability_issue(load_run_module, monkeypatch):
    run = load_run_module
    events = []
    artifacts = {
        "wave1_contract": _wave1_contract_artifact(),
        "r3_seed_manifest": b'{"seed":1}',
        "gdelt_dry_run_set": b'{"receipts":[{"name":"events"},{"name":"gcam"}]}',
        "source_lab_snapshot": b"source-lab",
        "funded_preflight": b"preflight",
    }
    authority = _issued_wave1_authority()
    consumption = SimpleNamespace(consumption_id="exc_" + "c" * 64)
    capability = object()
    _funded_result_client(run, monkeypatch)
    monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", "ogilvy_funded")
    monkeypatch.setenv("SOCIALCRAWL_FUNDED_STAGE_NAME", "stage_1_wave_1")
    monkeypatch.setattr(
        run,
        "bigquery",
        SimpleNamespace(Client=lambda **_kwargs: object()),
        raising=False,
    )
    monkeypatch.setattr(
        run,
        "execution_approval",
        SimpleNamespace(_is_unresolved_result=lambda _error: False),
        raising=False,
    )
    monkeypatch.setattr(
        run,
        "_build_wave1_execution_artifacts",
        lambda **_kwargs: events.append("artifacts") or artifacts,
        raising=False,
    )

    def load(operation, *, mode, artifact_reader):
        events.append("load")
        assert operation == "wave1_pilot"
        assert mode == "new_consume"
        for name, content in artifacts.items():
            assert artifact_reader(name) == content
        return authority

    monkeypatch.setattr(run.execution_approval, "_load_execution_authority", load, raising=False)
    monkeypatch.setattr(
        run.execution_approval,
        "_consume_execution_authority",
        lambda _authority: events.append("consume") or consumption,
        raising=False,
    )

    def issue(bound_authority, bound_consumption, *, route_set_sha256):
        events.append("issue")
        from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SET_SHA256

        assert route_set_sha256 == WAVE1_ROUTE_SET_SHA256
        assert bound_authority is authority
        assert bound_consumption is consumption
        return capability

    monkeypatch.setattr(run, "_issue_wave1_execution_capability", issue, raising=False)
    monkeypatch.setattr(
        run,
        "_wave1_requests_from_seed_manifest",
        lambda _payload: events.append("requests") or ("request",),
    )
    from src.ingestion.connectors import gdelt

    monkeypatch.setattr(
        gdelt,
        "_dry_run_receipt_from_mapping",
        lambda item: SimpleNamespace(name=item["name"]),
    )
    monkeypatch.setattr(
        gdelt,
        "bind_wave1_gdelt_manifest_entry",
        lambda _capability, _artifact, name: events.append(f"gdelt:{name}") or f"entry:{name}",
    )
    execution = run._begin_wave1_durable_execution(
        "run_wave1", datetime(2026, 8, 31, 12, tzinfo=UTC)
    )
    assert events == [
        "load",
        "artifacts",
        "consume",
        "issue",
        "requests",
        "gdelt:events",
        "gdelt:gcam",
    ]
    assert execution.consumption is consumption
    assert execution.capability is capability
    assert execution.wave1_requests == ("request",)
    assert tuple(receipt.name for receipt in execution.gdelt_receipts) == (
        "events",
        "gcam",
    )
    assert execution.gdelt_entries == ("entry:events", "entry:gcam")
    assert run._DURABLE_WAVE1_ARTIFACT_CONTEXT is None


def test_wave1_durable_entry_tracks_consumption_before_setup_can_fail(load_run_module, monkeypatch):
    run = load_run_module
    _funded_result_client(run, monkeypatch)
    authority = _issued_wave1_authority()
    consumption = SimpleNamespace(consumption_id="exc_" + "c" * 64)
    monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", "ogilvy_funded")
    monkeypatch.setenv("SOCIALCRAWL_FUNDED_STAGE_NAME", "stage_1_wave_1")
    monkeypatch.setattr(
        run,
        "bigquery",
        SimpleNamespace(Client=lambda **_kwargs: object()),
        raising=False,
    )
    artifacts = {
        "wave1_contract": _wave1_contract_artifact(),
        "r3_seed_manifest": b'{"seed":1}',
        "gdelt_dry_run_set": b'{"receipts":[]}',
        "source_lab_snapshot": b"source-lab",
        "funded_preflight": b"preflight",
    }
    monkeypatch.setattr(
        run,
        "_build_wave1_execution_artifacts",
        lambda **_kwargs: artifacts,
    )

    def load(_operation, *, mode, artifact_reader):
        assert mode == "new_consume"
        for name, content in artifacts.items():
            assert artifact_reader(name) == content
        return authority

    monkeypatch.setattr(
        run.execution_approval,
        "_load_execution_authority",
        load,
    )
    monkeypatch.setattr(
        run.execution_approval,
        "_consume_execution_authority",
        lambda _authority: consumption,
    )
    monkeypatch.setattr(
        run,
        "_issue_wave1_execution_capability",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("post-consumption setup failed")
        ),
    )

    with pytest.raises(RuntimeError, match="post-consumption setup failed"):
        run._begin_wave1_durable_execution("run_wave1", datetime(2026, 8, 31, 12, tzinfo=UTC))

    assert (authority, consumption, "run_wave1") == run._ACTIVE_WAVE1_EXECUTION


def test_all_market_failure_precedes_wave1_terminal_success_recording(load_run_module):
    source = inspect.getsource(load_run_module._run_impl)
    assert source.index("if _all_markets_failed") < source.index("_record_wave1_execution_result")


def test_wave1_runner_closes_and_records_without_other_connectors_or_downstream_work(
    diagnostic_pipeline, monkeypatch
):
    import pandas as pd

    run, _rows = diagnostic_pipeline
    events = []
    execution = SimpleNamespace(capability=object(), wave1_requests=("request",))
    close_receipt = object()
    recorded = {"terminal": "funded-close"}

    class Connector:
        def __init__(self, *, market):
            self.market = market

        def safe_fetch(self):
            events.append("socialcrawl")
            return pd.DataFrame([{"market": self.market, "id": "funded-row"}])

    class UnapprovedConnector(Connector):
        def safe_fetch(self):
            events.append("unapproved-connector")
            return pd.DataFrame([{"market": self.market, "id": "unapproved-row"}])

    monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", "ogilvy_funded")
    monkeypatch.setenv("SOCIALCRAWL_FUNDED_STAGE_NAME", "stage_1_wave_1")
    monkeypatch.setattr("src.ingestion.connectors.ensemble._ensemble_enabled", lambda _config: True)
    monkeypatch.setattr("src.utils.secrets.get_secret", lambda _name: "synthetic-token")
    monkeypatch.setattr(
        "src.ingestion.connectors.customer_units.fetch_units_history",
        lambda *_args: events.append("ensemble-probe") or [],
    )
    monkeypatch.setattr(run, "CONNECTORS", [
        ("rss", UnapprovedConnector),
        ("socialcrawl", Connector),
        ("gdelt", UnapprovedConnector),
    ])
    monkeypatch.setattr(run, "_CONNECTOR_PARALLEL_OK", frozenset())
    monkeypatch.setattr(run, "_begin_wave1_durable_execution", lambda *_args: execution)
    monkeypatch.setattr(run, "_connector_plan_for_run", run._real_connector_plan_for_test)
    gdelt_proof = object()
    monkeypatch.setattr(run, "_execute_wave1_gdelt", lambda *_args: events.append("gdelt") or gdelt_proof)
    monkeypatch.setattr(
        run, "_prepare_funded_socialcrawl", lambda *_args: events.append("prepare") or object()
    )
    monkeypatch.setattr(
        run,
        "_ingest_market_frames",
        lambda _market, frames, *_args: (
            events.append("raw-and-enriched") or sum(len(frame) for frame in frames),
            sum(len(frame) for frame in frames),
            {},
            0,
            {},
            {},
        ),
    )
    monkeypatch.setattr(
        run,
        "_close_funded_socialcrawl",
        lambda *_args, **_kwargs: events.append("funded-close") or close_receipt,
    )

    def record(actual_execution, actual_close, *, gdelt_runtime_proof):
        assert actual_execution is execution
        assert actual_close is close_receipt
        assert gdelt_runtime_proof is not None
        events.append("durable-result")
        return recorded

    monkeypatch.setattr(run, "_record_wave1_execution_result", record)
    monkeypatch.setattr(
        run,
        "compute_velocity_scores_for_today",
        lambda *_args, **_kwargs: events.append("scoring") or {},
    )
    monkeypatch.setattr(
        run,
        "compute_trend_scores",
        lambda *_args, **_kwargs: events.append("trend-scores") or [],
    )
    monkeypatch.setattr(
        "src.alerts.detector.send_daily_digest",
        lambda *_args, **_kwargs: events.append("digest") or (0, 0, 0, "sent"),
    )
    monkeypatch.setattr(run, "log_pipeline_run", lambda *_args, **_kwargs: events.append("pipeline-summary"))

    assert run.run() == recorded
    assert events == [
        "gdelt", "prepare", "socialcrawl", "raw-and-enriched", "funded-close", "durable-result"
    ]
    assert run._ACTIVE_WAVE1_EXECUTION is None


def test_wave1_durable_result_records_the_real_close_receipt(load_run_module, monkeypatch):
    from src.analysis.open_intelligence.funded_lane_runtime import FundedRunCloseReceipt

    run = load_run_module
    authority = SimpleNamespace(
        approval=SimpleNamespace(manifest_sha256="a" * 64, approval_id="exa_" + "b" * 64),
        execution_name=(
            "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
            "trends-engine-open-intelligence-staging/executions/exe-1"
        ),
    )
    consumption = SimpleNamespace(consumption_id="exc_" + "c" * 64)
    execution = SimpleNamespace(authority=authority, consumption=consumption)
    result = SimpleNamespace(result_id="exr_" + "d" * 64)
    calls = []
    monkeypatch.setattr(run, "execution_approval", SimpleNamespace(), raising=False)
    monkeypatch.setattr(
        run.execution_approval,
        "_record_execution_result",
        lambda *args: calls.append(args) or result,
        raising=False,
    )
    payload = run._record_wave1_execution_result(
        execution,
        FundedRunCloseReceipt(
            attribution_state="complete",
            calls=8,
            budget_debit_credits=Decimal("53.000"),
            vendor_reported_credits=Decimal("53.0"),
            balance_delta=Decimal("53"),
            attribution_gap_credits=Decimal("0.000"),
        ),
        gdelt_runtime_proof=run.Wave1GDELTRuntimeProof(
            query_job_ids=(("events", "job-events"), ("gcam", "job-gcam")),
            query_bytes_processed=(("events", 100), ("gcam", 200)),
            persistence={"complete": True},
        ),
    )
    assert payload["execution_approval"]["result_id"] == result.result_id
    assert calls[0][2] == authority.execution_name + "#funded-run-close"
    assert json.loads(calls[0][3]) == {
        "attribution_gap_credits": "0",
        "attribution_state": "complete",
        "balance_delta": "53",
        "balance_read_status": "measured",
        "budget_debit_credits": "53",
        "calls": 8,
        "gdelt": {
            "persistence": {"complete": True},
            "query_bytes_processed": [["events", 100], ["gcam", 200]],
            "query_job_ids": [["events", "job-events"], ["gcam", "job-gcam"]],
        },
        "run_balance_delta": None,
        "source_values": {},
        "source_values_state": "persisted",
        "terminal_id": None,
        "vendor_reported_credits": "53",
    }
    assert "run_id" not in calls[0][3]
    assert calls[0][-1] == "succeeded"

    run._record_wave1_execution_result(
        execution,
        FundedRunCloseReceipt(
            attribution_state="complete",
            calls=1,
            budget_debit_credits=Decimal("3"),
            vendor_reported_credits=Decimal("3"),
            balance_delta=Decimal("3"),
            attribution_gap_credits=Decimal("0"),
            terminal_id="scte_" + "e" * 64,
        ),
        gdelt_runtime_proof=run.Wave1GDELTRuntimeProof(
            query_job_ids=(("events", "job-events"), ("gcam", "job-gcam")),
            query_bytes_processed=(("events", 100), ("gcam", 200)),
            persistence={"complete": True},
        ),
    )
    assert calls[-1][-1] == "failed"


def test_wave1_durable_result_records_unavailable_balance_terminal_exactly(
    load_run_module, monkeypatch
):
    from src.analysis.open_intelligence.funded_lane_runtime import FundedRunCloseReceipt

    run = load_run_module
    authority = SimpleNamespace(
        approval=SimpleNamespace(manifest_sha256="a" * 64, approval_id="exa_" + "b" * 64),
        execution_name="projects/p/locations/r/jobs/j/executions/e",
    )
    execution = SimpleNamespace(
        authority=authority,
        consumption=SimpleNamespace(consumption_id="exc_" + "c" * 64),
    )
    calls = []
    monkeypatch.setattr(
        run.execution_approval,
        "_record_execution_result",
        lambda *args: calls.append(args) or SimpleNamespace(result_id="exr_" + "d" * 64),
    )
    result = run._record_wave1_execution_result(
        execution,
        FundedRunCloseReceipt(
            attribution_state="gap_detected",
            calls=1,
            budget_debit_credits=Decimal("1"),
            vendor_reported_credits=Decimal("0"),
            balance_delta=None,
            attribution_gap_credits=None,
            balance_read_status="unavailable",
            terminal_id="scte_" + "e" * 64,
        ),
        gdelt_runtime_proof=run.Wave1GDELTRuntimeProof(
            query_job_ids=(("events", "job-events"), ("gcam", "job-gcam")),
            query_bytes_processed=(("events", 100), ("gcam", 200)),
            persistence={"complete": True},
        ),
    )

    assert result["balance_delta"] is None
    assert result["attribution_gap_credits"] is None
    assert result["balance_read_status"] == "unavailable"
    assert result["terminal_id"] == "scte_" + "e" * 64
    assert calls[0][-1] == "failed"


@pytest.mark.parametrize(
    ("attribution_state", "vendor_reported", "gap"),
    [
        ("gap_detected", Decimal("0"), Decimal("1")),
        ("conservative", Decimal("0"), Decimal("0")),
    ],
)
def test_wave1_noncomplete_close_records_failed_result(
    load_run_module, monkeypatch, attribution_state, vendor_reported, gap
):
    from src.analysis.open_intelligence.funded_lane_runtime import FundedRunCloseReceipt

    run = load_run_module
    execution = SimpleNamespace(
        authority=SimpleNamespace(
            approval=SimpleNamespace(
                manifest_sha256="a" * 64,
                approval_id="exa_" + "b" * 64,
            ),
            execution_name=(
                "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
                "trends-engine-open-intelligence-staging/executions/exe-gap"
            ),
        ),
        consumption=SimpleNamespace(consumption_id="exc_" + "c" * 64),
    )
    calls = []
    monkeypatch.setattr(run, "execution_approval", SimpleNamespace(), raising=False)
    monkeypatch.setattr(
        run.execution_approval,
        "_record_execution_result",
        lambda *args: calls.append(args) or SimpleNamespace(result_id="exr_" + "d" * 64),
        raising=False,
    )

    run._record_wave1_execution_result(
        execution,
        FundedRunCloseReceipt(
            attribution_state=attribution_state,
            calls=1,
            budget_debit_credits=Decimal("1"),
            vendor_reported_credits=vendor_reported,
            balance_delta=Decimal("2"),
            attribution_gap_credits=gap,
        ),
        gdelt_runtime_proof=run.Wave1GDELTRuntimeProof(
            query_job_ids=(("events", "job-events"), ("gcam", "job-gcam")),
            query_bytes_processed=(("events", 100), ("gcam", 200)),
            persistence={"complete": True},
        ),
    )

    assert calls[0][-1] == "failed"


def test_wave1_conservative_close_with_agreeing_vendor_and_balance_succeeds(
    load_run_module, monkeypatch
):
    # Attempt 19 on staging, 4 Sep 2026: eight calls, 8 quoted, 7 reported, the
    # balance moved 7 this run and 24 this month. The bound held; the month total
    # is not this run's charge.
    from types import MappingProxyType

    from src.analysis.open_intelligence.funded_lane_runtime import FundedRunCloseReceipt

    run = load_run_module
    execution = SimpleNamespace(
        authority=SimpleNamespace(
            approval=SimpleNamespace(manifest_sha256="a" * 64, approval_id="exa_" + "b" * 64),
            execution_name=(
                "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
                "trends-engine-open-intelligence-staging/executions/exe-cached"
            ),
        ),
        consumption=SimpleNamespace(consumption_id="exc_" + "c" * 64),
    )
    calls = []
    monkeypatch.setattr(run, "execution_approval", SimpleNamespace(), raising=False)
    monkeypatch.setattr(
        run.execution_approval,
        "_record_execution_result",
        lambda *args: calls.append(args) or SimpleNamespace(result_id="exr_" + "d" * 64),
        raising=False,
    )
    values = MappingProxyType(
        {
            "/v1/instagram/search/reels": MappingProxyType(
                {
                    "state": "active",
                    "reason": None,
                    "unique_observations": 3,
                    "marginal_candidates": 1,
                    "marginal_evidence": 2,
                }
            )
        }
    )

    run._record_wave1_execution_result(
        execution,
        FundedRunCloseReceipt(
            attribution_state="conservative",
            calls=8,
            budget_debit_credits=Decimal("8"),
            vendor_reported_credits=Decimal("7"),
            balance_delta=Decimal("24"),
            attribution_gap_credits=Decimal("0"),
            run_balance_delta=Decimal("7"),
            source_values=values,
        ),
        gdelt_runtime_proof=run.Wave1GDELTRuntimeProof(
            query_job_ids=(("events", "job-events"), ("gcam", "job-gcam")),
            query_bytes_processed=(("events", 100), ("gcam", 200)),
            persistence={"complete": True},
        ),
    )

    recorded = json.loads(calls[0][3])
    assert calls[0][-1] == "succeeded"
    assert recorded["source_values_state"] == "persisted"
    assert recorded["source_values"] == {
        "/v1/instagram/search/reels": {
            "marginal_candidates": 1,
            "marginal_evidence": 2,
            "reason": None,
            "state": "active",
            "unique_observations": 3,
        }
    }


def test_run_wrapper_records_sanitized_wave1_failure_after_consumption(
    load_run_module, monkeypatch
):
    run = load_run_module
    execution = SimpleNamespace(
        authority=SimpleNamespace(
            approval=SimpleNamespace(
                manifest_sha256="a" * 64,
                approval_id="exa_" + "b" * 64,
            ),
            execution_name=(
                "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
                "trends-engine-open-intelligence-staging/executions/exe-failed"
            ),
        ),
        consumption=SimpleNamespace(consumption_id="exc_" + "c" * 64),
    )
    recorded = []

    def fail_run(**_kwargs):
        run._ACTIVE_WAVE1_EXECUTION = (execution.authority, execution.consumption, "run_wave1")
        raise RuntimeError("private pipeline detail")

    monkeypatch.setattr(run, "_run_impl", fail_run)
    monkeypatch.setattr("src.observability.events.insert_dataframe", lambda *_args: None)
    monkeypatch.setattr(
        run,
        "execution_approval",
        SimpleNamespace(_is_unresolved_result=lambda _error: False),
        raising=False,
    )
    monkeypatch.setattr(
        run.execution_approval,
        "_record_execution_result",
        lambda *args: recorded.append(args) or SimpleNamespace(result_id="exr_" + "d" * 64),
        raising=False,
    )
    with pytest.raises(RuntimeError, match="private pipeline detail"):
        run.run()

    assert len(recorded) == 1
    payload = json.loads(recorded[0][3])
    assert recorded[0][5] == "failed"
    assert payload == {
        "error_code": "wave1_pilot_failed",
        "run_id": "run_wave1",
        "status": "failed",
    }
    assert "private pipeline detail" not in recorded[0][3]
    assert run._ACTIVE_WAVE1_EXECUTION is None


def test_run_wrapper_never_substitutes_failed_payload_after_unresolved_result(
    load_run_module, monkeypatch
):
    run = load_run_module
    authority = SimpleNamespace()
    consumption = SimpleNamespace()

    def unresolved(**_kwargs):
        run._ACTIVE_WAVE1_EXECUTION = (authority, consumption, "run_wave1")
        raise run.execution_approval.ApprovalRefusal("execution_result_unresolved")

    monkeypatch.setattr(run, "_run_impl", unresolved)
    monkeypatch.setattr("src.observability.events.insert_dataframe", lambda *_args: None)
    monkeypatch.setattr(
        run.execution_approval,
        "_record_execution_result",
        lambda *_args, **_kwargs: pytest.fail("fallback result reached"),
    )
    with pytest.raises(run.execution_approval.ApprovalRefusal, match="unresolved"):
        run.run()


def test_wave1_artifact_builder_is_complete_and_never_reads_a_secret(load_run_module, monkeypatch):
    run = load_run_module
    observed_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    values = {
        "wave1_contract": {"contract": "wave1"},
        "r3_seed_manifest": {"seeds": ["one"]},
        "gdelt_dry_run_set": {"receipts": ["events", "gcam"]},
        "source_lab_snapshot": {"routes": 8},
        "funded_preflight": {"monthly_spend": "0"},
    }
    for name, payload in values.items():
        monkeypatch.setattr(
            run,
            f"_{name}",
            lambda *_args, value=payload, **_kwargs: value,
            raising=False,
        )
    artifacts = run._build_wave1_execution_artifacts(client=object(), observed_at=observed_at)
    assert set(artifacts) == set(values)
    assert all(json.loads(artifacts[name]) == payload for name, payload in values.items())
    source = __import__("inspect").getsource(run._build_wave1_execution_artifacts)
    assert "get_secret" not in source
    assert "requests" not in source


def test_gdelt_dry_run_artifact_loads_only_the_fixed_canonical_receipt_file(
    load_run_module, monkeypatch, tmp_path
):
    from tests.unit.test_source_wave1_gdelt import (
        _GCAM_SQL_DIGEST,
        _receipt,
        _receipt_mapping,
    )

    events = _receipt()
    gcam = _receipt("gcam", _GCAM_SQL_DIGEST, 37_284_235_082)
    receipt_file = tmp_path / "gdelt-wave1-receipts.json"
    receipt_file.write_text(
        json.dumps(
            {
                "contract_version": "wave1-gdelt-dry-run-set-v1",
                "receipts": [_receipt_mapping(events), _receipt_mapping(gcam)],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(load_run_module, "GDELT_DRY_RUN_RECEIPT_SET_PATH", receipt_file)
    client = SimpleNamespace(query=lambda *_args, **_kwargs: pytest.fail("cloud query reached"))

    payload = load_run_module._gdelt_dry_run_set(client, datetime(2026, 9, 1, tzinfo=UTC))

    assert [item["dry_run_receipt_id"] for item in payload["receipts"]] == [
        events.dry_run_receipt_id,
        gcam.dry_run_receipt_id,
    ]


def test_gdelt_dry_run_artifact_missing_file_refuses_before_cloud(
    load_run_module, monkeypatch, tmp_path
):
    missing = tmp_path / "missing-gdelt-receipts.json"
    monkeypatch.setattr(load_run_module, "GDELT_DRY_RUN_RECEIPT_SET_PATH", missing)
    client = SimpleNamespace(query=lambda *_args, **_kwargs: pytest.fail("cloud query reached"))
    with pytest.raises(RuntimeError, match="receipt artifact"):
        load_run_module._gdelt_dry_run_set(client, datetime(2026, 9, 1, tzinfo=UTC))


def test_real_funded_preflight_decimals_have_stable_canonical_artifact_bytes(
    load_run_module, monkeypatch
):
    from src.analysis.open_intelligence import funded_lane_reader

    run = load_run_module
    observed_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    snapshot = SimpleNamespace(
        month_start=datetime(2026, 8, 1, tzinfo=UTC).date(),
        month_opening_balance=Decimal("250100.000000000"),
        monthly_ledger_debit=Decimal("10.500000000"),
        monthly_vendor_reported=Decimal("10.500000000"),
        unreconciled_execution_ids=(),
        consecutive_complete_runs=2,
        runs_today=0,
    )
    monkeypatch.setattr(funded_lane_reader, "read_monthly_funded_lane", lambda **_: snapshot)
    for name in ("wave1_contract", "r3_seed_manifest", "gdelt_dry_run_set", "source_lab_snapshot"):
        monkeypatch.setattr(run, f"_{name}", lambda *_args, **_kwargs: {"ok": True})

    artifact = json.loads(
        run._build_wave1_execution_artifacts(client=object(), observed_at=observed_at)[
            "funded_preflight"
        ]
    )

    assert artifact["month_opening_balance"] == "250100"
    assert artifact["monthly_ledger_debit"] == "10.5"
    assert artifact["monthly_vendor_reported"] == "10.5"


def test_wave1_contract_artifact_freezes_routes_phases_and_63_credit_ceiling(
    load_run_module,
):
    payload = load_run_module._wave1_contract(object(), datetime(2026, 8, 31, tzinfo=UTC))
    assert payload["contract_version"] == "wave1-pilot-durable-v1"
    assert payload["max_credits"] == 63
    assert set(payload["routes"]) == {
        "tiktok/song",
        "tiktok/song/videos",
        "instagram/music/trending",
        "instagram/audio/reels",
        "instagram/search/reels",
        "youtube/shorts/trending",
        "youtube/video/comments",
        "reddit/post/comments",
    }
    assert all(
        route["maximum_debit"] == route["credit_cost"] * route["maximum_calls"]
        for route in payload["routes"].values()
    )
    assert payload["routes"]["instagram/search/reels"]["credit_cost"] == 9
    assert payload["routes"]["youtube/shorts/trending"]["credit_cost"] == 15
    assert payload["routes"]["youtube/video/comments"]["credit_cost"] == 5
    assert payload["routes"]["reddit/post/comments"]["credit_cost"] == 9


def test_r3_seed_manifest_requires_every_seed_lane_and_exact_receipt(load_run_module):
    from google.cloud.bigquery.table import Row

    row = {
        "replacement_run_id": "run_20260903_dynamic_apply_v2_r16",
        "row_set_digest": "a" * 64,
        "source_sha": "b" * 40,
        "tiktok_music_identifiers": [
            Row(
                ("music_id", "song-1", "za"),
                {"identifier_type": 0, "value": 1, "market": 2},
            )
        ],
        "reddit_urls": [Row(("https://reddit.invalid/post", "za"), {"url": 0, "market": 1})],
        "instagram_search_terms": [Row(("culture", "ke"), {"term": 0, "market": 1})],
        "youtube_short_urls": [{"url": "https://youtube.invalid/short", "market": "ng"}],
    }

    class Job:
        def result(self, **_kwargs):
            return (row,)

    class Client:
        def query(self, *_args, **_kwargs):
            return Job()

    # Every seed carries the market it came from (v2, 4 Sep 2026): that market is
    # the retained geography of the rows the seeded call returns.
    assert load_run_module._r3_seed_manifest(Client(), datetime(2026, 8, 31, tzinfo=UTC)) == {
        "contract_version": "wave1-r3-seed-manifest-v2",
        "replacement_run_id": row["replacement_run_id"],
        "row_set_digest": row["row_set_digest"],
        "source_sha": row["source_sha"],
        "tiktok_music_ids": [{"music_id": "song-1", "market": "za"}],
        "reddit_urls": [{"url": "https://reddit.invalid/post", "market": "za"}],
        "instagram_search_terms": [{"term": "culture", "market": "ke"}],
        "youtube_short_urls": [{"url": "https://youtube.invalid/short", "market": "ng"}],
    }
    validated = {
        "replacement_run_id": row["replacement_run_id"],
        "row_set_digest": row["row_set_digest"],
        "source_sha": row["source_sha"],
        "tiktok_music_ids": [{"music_id": "song-1", "market": "za"}],
        "reddit_urls": [{"url": "https://reddit.invalid/post", "market": "za"}],
        "instagram_search_terms": [{"term": "culture", "market": "ke"}],
        "youtube_short_urls": [{"url": "https://youtube.invalid/short", "market": "ng"}],
    }
    for broken in (
        {"term": "culture"},
        {"term": "culture", "market": "us"},
        {"term": "", "market": "za"},
        "culture",
    ):
        with pytest.raises(RuntimeError, match="seed manifest"):
            load_run_module._validate_wave1_seed_manifest(
                {**validated, "instagram_search_terms": [broken]}
            )
    # A seed list the window cannot fill is empty, not invalid (4 Sep 2026: the
    # released run carried no TikTok music seed and no reddit evidence); only a
    # manifest with nothing to seed at all refuses.
    fields = (
        "tiktok_music_ids",
        "reddit_urls",
        "instagram_search_terms",
        "youtube_short_urls",
    )
    for field in fields:
        one_empty = load_run_module._validate_wave1_seed_manifest({**validated, field: []})
        assert one_empty[field] == []
    with pytest.raises(RuntimeError, match="seed manifest"):
        load_run_module._validate_wave1_seed_manifest(
            {**validated, **{field: [] for field in fields}}
        )


def test_seed_manifest_maximum_plan_reserves_61_and_rejects_an_extra_seed(load_run_module):
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

    seed = {
        "replacement_run_id": "run_20260903_dynamic_apply_v2_r16",
        "row_set_digest": "a" * 64,
        "source_sha": "b" * 40,
        "tiktok_music_ids": [
            {"music_id": f"song-{index}", "market": ("za", "ng", "ke")[index % 3]}
            for index in range(5)
        ],
        "reddit_urls": [
            {"url": f"https://reddit.invalid/post-{index}", "market": ("za", "ng")[index]}
            for index in range(2)
        ],
        "instagram_search_terms": [
            {"term": f"culture-{index}", "market": ("za", "ke")[index]}
            for index in range(2)
        ],
        "youtube_short_urls": [
            {"url": f"https://youtube.invalid/short-{index}", "market": ("za", "ng", "ke")[index]}
            for index in range(3)
        ],
    }
    validated = load_run_module._validate_wave1_seed_manifest(seed)
    requests = load_run_module._wave1_requests_from_seed_manifest(
        {"contract_version": load_run_module.WAVE1_SEED_MANIFEST_CONTRACT, **validated}
    )
    assert len(requests) == 17
    assert {request.market for request in requests} == {"za", "ng", "ke"}
    assert {request.call_role for request in requests} == {"qualification"}
    assert sum(WAVE1_ROUTE_SPECS[request.route].credit_cost for request in requests) == 61
    for field, key, value in (
        ("reddit_urls", "url", "https://reddit.invalid/extra"),
        ("instagram_search_terms", "term", "culture-extra"),
        ("youtube_short_urls", "url", "https://youtube.invalid/extra"),
    ):
        with pytest.raises(RuntimeError, match="seed manifest"):
            load_run_module._validate_wave1_seed_manifest(
                {**seed, field: [*seed[field], {key: value, "market": "za"}]}
            )


def test_seed_query_limits_match_the_maximum_61_credit_plan(load_run_module):
    row = {
        "replacement_run_id": "run_20260903_dynamic_apply_v2_r16",
        "row_set_digest": "a" * 64,
        "source_sha": "b" * 40,
        "tiktok_music_identifiers": [],
        "reddit_urls": [{"url": "https://reddit.invalid/post", "market": "za"}],
        "instagram_search_terms": [{"term": "culture", "market": "ng"}],
        "youtube_short_urls": [{"url": "https://youtube.invalid/short", "market": "ke"}],
    }

    class Job:
        def result(self, **_kwargs):
            return (row,)

    class Client:
        query_text = None

        def query(self, sql, **_kwargs):
            self.query_text = sql
            return Job()

    client = Client()
    load_run_module._r3_seed_manifest(client, datetime(2026, 9, 27, tzinfo=UTC))
    assert "FROM tiktok_music ORDER BY first_seen, value LIMIT 5" in client.query_text
    assert "ORDER BY first_id, url LIMIT 2) AS reddit_urls" in client.query_text
    assert "ORDER BY first_id, label LIMIT 2) AS instagram_search_terms" in client.query_text
    assert "ORDER BY first_id, url LIMIT 3) AS youtube_short_urls" in client.query_text


@pytest.mark.parametrize(
    "identifiers",
    [
        ["song-1"],
        [{"identifier_type": "post_id", "value": "song-1", "market": "za"}],
        [{"identifier_type": "music_id", "value": "", "market": "za"}],
        [{"identifier_type": "music_id", "value": "song-1"}],
    ],
)
def test_r3_seed_manifest_refuses_untyped_tiktok_music_identifiers(load_run_module, identifiers):
    row = {
        "replacement_run_id": "run_20260903_dynamic_apply_v2_r16",
        "row_set_digest": "a" * 64,
        "source_sha": "b" * 40,
        "tiktok_music_identifiers": identifiers,
        "reddit_urls": [{"url": "https://reddit.invalid/post", "market": "za"}],
        "instagram_search_terms": [{"term": "culture", "market": "za"}],
        "youtube_short_urls": [{"url": "https://youtube.invalid/short", "market": "za"}],
    }

    class Job:
        def result(self, **_kwargs):
            return (row,)

    class Client:
        def query(self, *_args, **_kwargs):
            return Job()

    with pytest.raises(RuntimeError, match="seed manifest"):
        load_run_module._r3_seed_manifest(Client(), datetime(2026, 8, 31, tzinfo=UTC))


def test_wave1_request_builder_uses_only_the_frozen_seed_manifest(load_run_module):
    seed = {
        "contract_version": "wave1-r3-seed-manifest-v2",
        "replacement_run_id": "run_20260903_dynamic_apply_v2_r16",
        "row_set_digest": "a" * 64,
        "source_sha": "b" * 40,
        "tiktok_music_ids": [
            {"music_id": "song-1", "market": "za"},
            {"music_id": "song-2", "market": "ng"},
        ],
        "reddit_urls": [{"url": "https://reddit.invalid/post", "market": "ke"}],
        "instagram_search_terms": [
            {"term": "culture", "market": "za"},
            {"term": "style", "market": "ke"},
        ],
        "youtube_short_urls": [{"url": "https://youtube.invalid/short", "market": "ng"}],
    }
    requests = load_run_module._wave1_requests_from_seed_manifest(seed)
    # Seeded calls only, each with its market; the global trending surfaces
    # return rows no market can claim and are not planned (4 Sep 2026).
    assert [(request.route, request.market) for request in requests] == [
        ("tiktok/song", "za"),
        ("tiktok/song/videos", "za"),
        ("tiktok/song", "ng"),
        ("tiktok/song/videos", "ng"),
        ("instagram/search/reels", "za"),
        ("instagram/search/reels", "ke"),
        ("youtube/video/comments", "ng"),
        ("reddit/post/comments", "ke"),
    ]
    youtube_comments = next(
        request for request in requests if request.route == "youtube/video/comments"
    )
    assert dict(youtube_comments.params) == {"url": "https://youtube.invalid/short"}
    assert {request.call_role for request in requests} == {"qualification"}
    with pytest.raises(RuntimeError, match="seed manifest"):
        load_run_module._wave1_requests_from_seed_manifest(
            {**seed, "contract_version": "wave1-r3-seed-manifest-v1"}
        )


def test_gdelt_artifact_never_regenerates_a_dry_run_inside_the_pilot(load_run_module):
    source = __import__("inspect").getsource(load_run_module._gdelt_dry_run_set)
    assert "execute_wave1_gdelt_dry_run" not in source
    assert "GDELT_DRY_RUN_RECEIPT_SET_PATH" in source


def test_source_lab_artifact_requires_all_eight_wave1_routes(load_run_module):
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

    rows = tuple(
        {"row_json": json.dumps({"route_path": f"/v1/{route}", "status": "inventory_only"})}
        for route in WAVE1_ROUTE_SPECS
    )

    class Job:
        def result(self, **_kwargs):
            return rows

    class Client:
        def query(self, *_args, **_kwargs):
            return Job()

    payload = load_run_module._source_lab_snapshot(Client(), datetime(2026, 8, 31, tzinfo=UTC))
    assert payload["contract_version"] == "wave1-source-lab-snapshot-v1"
    assert len(payload["routes"]) == 8
    with pytest.raises(RuntimeError, match="Source Lab"):
        load_run_module._validate_wave1_source_lab_rows(payload["routes"][:-1])


def test_funded_preflight_artifact_uses_monthly_ledger_without_secret_read(
    load_run_module, monkeypatch
):
    from src.analysis.open_intelligence import funded_lane_reader

    snapshot = SimpleNamespace(
        month_start=datetime(2026, 8, 1, tzinfo=UTC).date(),
        month_opening_balance=Decimal("250100"),
        monthly_ledger_debit=Decimal("10"),
        monthly_vendor_reported=Decimal("10"),
        unreconciled_execution_ids=(),
        consecutive_complete_runs=2,
        runs_today=0,
    )
    monkeypatch.setattr(
        funded_lane_reader,
        "read_monthly_funded_lane",
        lambda **_kwargs: snapshot,
    )
    payload = load_run_module._funded_preflight(object(), datetime(2026, 8, 31, 12, tzinfo=UTC))
    assert payload == {
        "contract_version": "wave1-funded-preflight-v1",
        "month_start": snapshot.month_start,
        "month_opening_balance": "250100",
        "monthly_ledger_debit": "10",
        "monthly_vendor_reported": "10",
        "unreconciled_execution_ids": (),
        "consecutive_complete_runs": 2,
        "runs_today": 0,
    }


def test_funded_preflight_artifact_admits_a_month_with_no_funded_run_yet(
    load_run_module, monkeypatch
):
    # Refused the first Wave 1 pilot on staging, 4 Sep 2026: the ledger carries
    # no preflight row until a funded run writes one, so the month opening
    # balance is None and the artifact gate blocked every first run of a month.
    # The runtime reads the live vendor balance at execution and refuses on
    # balance_unavailable and reserve_floor_reached itself; the artifact attests
    # the ledger it can read. An untouched month is admitted only when nothing
    # was debited, nothing was vendor reported and nothing is unreconciled.
    from src.analysis.open_intelligence import funded_lane_reader

    untouched = SimpleNamespace(
        month_start=datetime(2026, 9, 1, tzinfo=UTC).date(),
        month_opening_balance=None,
        monthly_ledger_debit=Decimal("0"),
        monthly_vendor_reported=Decimal("0"),
        unreconciled_execution_ids=(),
        consecutive_complete_runs=0,
        runs_today=0,
    )
    monkeypatch.setattr(funded_lane_reader, "read_monthly_funded_lane", lambda **_: untouched)
    payload = load_run_module._funded_preflight(object(), datetime(2026, 9, 4, 14, tzinfo=UTC))
    assert payload["month_opening_balance"] is None
    assert payload["monthly_ledger_debit"] == "0"
    assert payload["monthly_vendor_reported"] == "0"
    spent = SimpleNamespace(**{**vars(untouched), "monthly_ledger_debit": Decimal("5")})
    monkeypatch.setattr(funded_lane_reader, "read_monthly_funded_lane", lambda **_: spent)
    with pytest.raises(RuntimeError, match="preflight is blocked"):
        load_run_module._funded_preflight(object(), datetime(2026, 9, 4, 14, tzinfo=UTC))
    reported = SimpleNamespace(**{**vars(untouched), "monthly_vendor_reported": Decimal("5")})
    monkeypatch.setattr(funded_lane_reader, "read_monthly_funded_lane", lambda **_: reported)
    with pytest.raises(RuntimeError, match="preflight is blocked"):
        load_run_module._funded_preflight(object(), datetime(2026, 9, 4, 14, tzinfo=UTC))


def test_build_raw_row_engagement_total_with_missing_fields(load_run_module):
    row = {"source": "rss"}  # no engagement fields
    out = load_run_module.build_raw_row(row, "rid", datetime.now(UTC))
    assert out["engagement_total"] == 0.0


def test_build_raw_row_engagement_total_with_none_fields(load_run_module):
    row = {"views": None, "likes": None, "comments": None, "shares": None}
    out = load_run_module.build_raw_row(row, "rid", datetime.now(UTC))
    assert out["engagement_total"] == 0.0


def test_build_raw_row_preserves_market(load_run_module):
    row = {"market": "ng"}
    out = load_run_module.build_raw_row(row, "rid", datetime.now(UTC))
    assert out["market"] == "ng"


def test_build_raw_row_coerces_string_engagement(load_run_module):
    row = {"views": "42", "likes": "7", "comments": 0, "shares": 0}
    out = load_run_module.build_raw_row(row, "rid", datetime.now(UTC))
    assert out["engagement_total"] == 49.0


# --- build_raw_row SEARCHVEL bridge (SEARCH_VELOCITY_ENABLED) ---------------


def test_build_raw_row_search_velocity_flag_off_omits_key(load_run_module, monkeypatch):
    """Flag OFF (default): even a row carrying search_velocity_score does NOT
    propagate it into the raw row, keeping the frame byte-identical to today."""
    monkeypatch.delenv("SEARCH_VELOCITY_ENABLED", raising=False)
    row = {"source": "Google Trends", "search_velocity_score": 0.45}
    out = load_run_module.build_raw_row(row, "rid", datetime.now(UTC))
    assert "search_velocity_score" not in out


def test_build_raw_row_search_velocity_flag_on_carries_value(load_run_module, monkeypatch):
    """Flag ON: a bigquery_trends row carries its search_velocity_score through
    so enrich_dataframe can preserve it; a row without the key omits it."""
    monkeypatch.setenv("SEARCH_VELOCITY_ENABLED", "true")
    trends_row = {"source": "Google Trends", "search_velocity_score": 0.45}
    rss_row = {"source": "rss"}
    out_trends = load_run_module.build_raw_row(trends_row, "rid", datetime.now(UTC))
    out_rss = load_run_module.build_raw_row(rss_row, "rid", datetime.now(UTC))
    assert out_trends["search_velocity_score"] == 0.45
    assert "search_velocity_score" not in out_rss


# --- build_raw_row GCAM bridge (gdelt.gcam_enabled, Wave 0.2) ---------------


def test_build_raw_row_v2gcam_empty_omits_key(load_run_module):
    """Dark default: GDELT _normalise_row yields an empty v2gcam, so the bridge
    omits the key and df_raw carries no GCAM column, keeping the merged branch
    load-safe with no migration."""
    row = {"source": "gdelt", "v2gcam": ""}
    out = load_run_module.build_raw_row(row, "rid", datetime.now(UTC))
    assert "v2gcam" not in out


def test_build_raw_row_v2gcam_value_carries_through(load_run_module):
    """Flag on: a GKG row carrying the parsed code:value string bridges it into
    the raw row; a row without the key omits it."""
    gkg_row = {"source": "gdelt", "v2gcam": "v10.1:0.252,v19.9:5.102"}
    rss_row = {"source": "rss"}
    out_gkg = load_run_module.build_raw_row(gkg_row, "rid", datetime.now(UTC))
    out_rss = load_run_module.build_raw_row(rss_row, "rid", datetime.now(UTC))
    assert out_gkg["v2gcam"] == "v10.1:0.252,v19.9:5.102"
    assert "v2gcam" not in out_rss


# --- scoring weights invariant --------------------------------------------


def test_scoring_weights_sum_to_one():
    """The scoring weights must sum to 1.00. Momentum, when present, is carved
    FROM the velocity weight in code (not added on top), so it is excluded from
    the bundle sum and must not exceed the velocity weight it carves from. Guards
    against a weight drift that would let the composite exceed its 1.0 ceiling."""
    from src.utils.config_loader import load_scoring

    weights = load_scoring().get("weights", {})
    assert "search_velocity_score" in weights
    assert weights == {
        "velocity": 0.20,
        "momentum": 0.07,
        "genz_score": 0.00,
        "watchlist_score": 0.00,
        "engagement": 0.10,
        "slang_score": 0.10,
        "diversity": 0.16,
        "regional_score": 0.12,
        "creator_spread": 0.12,
        "search_velocity_score": 0.15,
        "tone_score": 0.05,
        "gcam_score": 0.00,
    }
    momentum = float(weights.get("momentum", 0.0))
    bundle = sum(float(v) for k, v in weights.items() if k != "momentum")
    assert round(bundle, 6) == 1.0
    assert momentum <= float(weights.get("velocity", 0.0))


# --- per-market ingest guard (_ingest_market_frames / _all_markets_failed) ---


def test_ingest_market_frames_happy_path(load_run_module):
    """The extracted per-market body returns the counts plus the sub-source
    counts and writes raw + enriched, with insert_dataframe / enrich_dataframe /
    _aggregate_by_topic stubbed so no BigQuery or Vertex call runs."""
    m = load_run_module
    frames = [m.pd.DataFrame([{"title": "t", "text": "x", "published_at": None, "market": "za"}])]
    enriched = m.pd.DataFrame(
        [{"topic_groups": ["music_amapiano"], "query_group": "music_amapiano"}]
    )
    with (
        patch.object(m, "insert_dataframe", side_effect=[5, 5]) as ins,
        patch.object(m, "enrich_dataframe", return_value=enriched),
        patch.object(
            m,
            "_aggregate_by_topic",
            return_value=({("za", "music_amapiano"): {"item_count": 1}}, 0),
        ),
    ):
        rows_raw, rows_enriched, topic_counts, unclassified, sub_counts, layer_counts = (
            m._ingest_market_frames("za", frames, "rid", datetime(2026, 6, 2, tzinfo=UTC))
        )
    assert rows_raw == 5
    assert rows_enriched == 5
    assert topic_counts == {("za", "music_amapiano"): {"item_count": 1}}
    assert unclassified == 0
    assert isinstance(sub_counts, dict)  # sub-source counts, empty for this frame
    assert isinstance(layer_counts, dict)  # Wave 1 per-layer tally, empty when dark
    assert ins.call_count == 2  # raw_content + enriched_content


def test_ingest_market_frames_drops_search_velocity_from_raw_content(load_run_module):
    """With SEARCH_VELOCITY_ENABLED on, a bigquery_trends row carries a
    search_velocity_score that build_raw_row bridges into the raw rows.
    raw_content has no such column, so it MUST be dropped from df_raw before
    the WRITE_APPEND load (the score survives only on the enriched frame, where
    enriched_content.search_velocity_score exists). Without this the ZA/NG
    raw_content load fails the day the flag flips."""
    m = load_run_module
    frames = [
        m.pd.DataFrame(
            [
                {
                    "source": "Google Trends",
                    "title": "t",
                    "text": "x",
                    "published_at": None,
                    "market": "za",
                    "search_velocity_score": 0.45,
                }
            ]
        )
    ]
    enriched = m.pd.DataFrame([{"topic_groups": ["other"], "query_group": "other"}])
    captured = {}

    def _capture(df, table, *a, **k):
        captured[table] = df
        return len(df)

    with (
        patch.object(m, "_search_velocity_on", return_value=True),
        patch.object(m, "insert_dataframe", side_effect=_capture),
        patch.object(m, "enrich_dataframe", return_value=enriched) as enr,
        patch.object(m, "_aggregate_by_topic", return_value=({}, 0)),
    ):
        m._ingest_market_frames("za", frames, "rid", datetime(2026, 6, 2, tzinfo=UTC))

    # raw_content load must not see the column the table lacks
    assert "search_velocity_score" not in captured["raw_content"].columns
    # but the score still reaches enrichment via the raw rows
    enr_frame = enr.call_args.args[0]
    assert "search_velocity_score" in enr_frame.columns


def test_ingest_market_frames_propagates_insert_error(load_run_module):
    """A failure inside the body MUST propagate so the run() loop can record a
    partial failure and continue, rather than being swallowed."""
    m = load_run_module
    frames = [m.pd.DataFrame([{"title": "t", "published_at": None, "market": "za"}])]
    with (
        patch.object(m, "insert_dataframe", side_effect=RuntimeError("BQ down")),
        patch.object(m, "enrich_dataframe", return_value=m.pd.DataFrame([{}])),
        patch.object(m, "_aggregate_by_topic", return_value=({}, 0)),
        pytest.raises(RuntimeError, match="BQ down"),
    ):
        m._ingest_market_frames("za", frames, "rid", datetime(2026, 6, 2, tzinfo=UTC))


def test_all_markets_failed_logic(load_run_module):
    """Raise only when something errored AND no market produced data; never on
    an error that landed beside a market that still succeeded, and never on a
    quiet (no-error, no-data) day."""
    f = load_run_module._all_markets_failed
    assert f([{"market": "za", "source": "pipeline", "error": "boom"}], set()) is True
    assert f([{"market": "za", "source": "pipeline", "error": "boom"}], {"ng", "ke"}) is False
    assert f([], set()) is False
    assert f([], {"za", "ng", "ke"}) is False


# --- log_pipeline_run status logic ----------------------------------------


def test_log_pipeline_run_status_success(load_run_module):
    rows = _capture_log_pipeline_rows(
        load_run_module,
        market_source_counts={"za": {"rss": 10}, "ng": {"rss": 20}, "ke": {"rss": 5}},
        score_rows=[{"market": "za"}, {"market": "ng"}, {"market": "ke"}],
        errors=[],
    )
    assert {r["status"] for r in rows} == {"success"}


def test_log_pipeline_run_status_empty(load_run_module):
    rows = _capture_log_pipeline_rows(
        load_run_module,
        market_source_counts={"za": {"rss": 0}, "ng": {"rss": 0}, "ke": {"rss": 0}},
        score_rows=[],
        errors=[],
    )
    assert {r["status"] for r in rows} == {"empty"}


def test_log_pipeline_run_status_failed_when_zero_rows_and_errors(load_run_module):
    rows = _capture_log_pipeline_rows(
        load_run_module,
        market_source_counts={"za": {"rss": 0}, "ng": {"rss": 10}, "ke": {"rss": 5}},
        score_rows=[{"market": "ng"}, {"market": "ke"}],
        errors=[{"market": "za", "source": "rss", "error": "boom"}],
    )
    by_market = {r["market"]: r for r in rows}
    assert by_market["za"]["status"] == "failed"
    assert by_market["ng"]["status"] == "success"
    assert by_market["ke"]["status"] == "success"


def test_log_pipeline_run_trends_scored_per_market(load_run_module):
    """Regression guard: trends_scored must be per-market, not the global total."""
    rows = _capture_log_pipeline_rows(
        load_run_module,
        market_source_counts={"za": {"rss": 10}, "ng": {"rss": 20}, "ke": {"rss": 5}},
        score_rows=[
            {"market": "za", "query_group": "a"},
            {"market": "za", "query_group": "b"},
            {"market": "ng", "query_group": "a"},
        ],
        errors=[],
    )
    by_market = {r["market"]: r for r in rows}
    assert by_market["za"]["trends_scored"] == 2
    assert by_market["ng"]["trends_scored"] == 1
    assert by_market["ke"]["trends_scored"] == 0


def test_log_pipeline_run_errors_scoped_per_market(load_run_module):
    """Regression guard: errors must be scoped per-market, not broadcast."""
    rows = _capture_log_pipeline_rows(
        load_run_module,
        market_source_counts={"za": {"rss": 10}, "ng": {"rss": 20}, "ke": {"rss": 5}},
        score_rows=[],
        errors=[
            {"market": "za", "source": "ensemble", "error": "495 quota"},
            {"market": "ng", "source": "gdelt", "error": "timeout"},
        ],
    )
    by_market = {r["market"]: r for r in rows}
    assert by_market["za"]["errors"] == ["ensemble: 495 quota"]
    assert by_market["ng"]["errors"] == ["gdelt: timeout"]
    assert by_market["ke"]["errors"] == []


def test_log_pipeline_run_degraded_feed_stays_success(load_run_module):
    """A per-feed fetch failure tagged fatal=False must not downgrade the
    market. Dead RSS feeds are expected churn, so the market stays success and
    the errors column still lists the feed for visibility and pruning."""
    rows = _capture_log_pipeline_rows(
        load_run_module,
        market_source_counts={"za": {"rss": 10}, "ng": {"rss": 20}, "ke": {"rss": 5}},
        score_rows=[{"market": "za"}],
        errors=[{"market": "za", "source": "rss", "error": "rss feed Htxt: 403", "fatal": False}],
    )
    by_market = {r["market"]: r for r in rows}
    assert by_market["za"]["status"] == "success"
    assert by_market["za"]["errors"] == ["rss: rss feed Htxt: 403"]


def test_log_pipeline_run_fatal_error_is_partial(load_run_module):
    """A connector or pipeline crash tagged fatal=True still downgrades the
    market to partial."""
    rows = _capture_log_pipeline_rows(
        load_run_module,
        market_source_counts={"za": {"rss": 10}, "ng": {"rss": 20}, "ke": {"rss": 5}},
        score_rows=[{"market": "za"}],
        errors=[
            {"market": "za", "source": "ensemble", "error": "connector crashed", "fatal": True}
        ],
    )
    assert {r["market"]: r["status"] for r in rows}["za"] == "partial"


def test_log_pipeline_run_untagged_error_stays_fatal(load_run_module):
    """Backward-compat: an error dict with no fatal key is treated as fatal,
    so pre-fix callers keep the old partial behaviour."""
    rows = _capture_log_pipeline_rows(
        load_run_module,
        market_source_counts={"za": {"rss": 10}, "ng": {"rss": 20}, "ke": {"rss": 5}},
        score_rows=[{"market": "za"}],
        errors=[{"market": "za", "source": "ensemble", "error": "495 quota"}],
    )
    assert {r["market"]: r["status"] for r in rows}["za"] == "partial"


def test_log_pipeline_run_degraded_only_zero_rows_is_empty(load_run_module):
    """Zero rows with only non-fatal degradations is empty, not failed."""
    rows = _capture_log_pipeline_rows(
        load_run_module,
        market_source_counts={"za": {"rss": 0}, "ng": {"rss": 10}, "ke": {"rss": 5}},
        score_rows=[],
        errors=[{"market": "za", "source": "rss", "error": "rss feed X: 404", "fatal": False}],
    )
    assert {r["market"]: r["status"] for r in rows}["za"] == "empty"


def test_log_pipeline_run_includes_bigquery_trends_rows(load_run_module):
    rows = _capture_log_pipeline_rows(
        load_run_module,
        market_source_counts={"za": {"bigquery_trends": 100}, "ng": {}, "ke": {}},
        score_rows=[],
        errors=[],
    )
    by_market = {r["market"]: r for r in rows}
    assert by_market["za"]["bigquery_trends_rows"] == 100
    assert by_market["ng"]["bigquery_trends_rows"] == 0


def test_log_pipeline_run_includes_apple_music_rows(load_run_module):
    """Apple Music (8th connector, LIVE 28 May 2026) must surface in
    pipeline_runs.apple_music_rows when src_counts carries it."""
    rows = _capture_log_pipeline_rows(
        load_run_module,
        market_source_counts={
            "za": {"apple_music": 42},
            "ng": {"apple_music": 18},
            "ke": {"apple_music": 7},
        },
        score_rows=[],
        errors=[],
    )
    by_market = {r["market"]: r for r in rows}
    assert by_market["za"]["apple_music_rows"] == 42
    assert by_market["ng"]["apple_music_rows"] == 18
    assert by_market["ke"]["apple_music_rows"] == 7


def test_log_pipeline_run_apple_music_rows_defaults_to_zero(load_run_module):
    """When the connector returns nothing for a market, apple_music_rows is 0."""
    rows = _capture_log_pipeline_rows(
        load_run_module,
        market_source_counts={"za": {"rss": 10}, "ng": {}, "ke": {}},
        score_rows=[],
        errors=[],
    )
    by_market = {r["market"]: r for r in rows}
    assert by_market["za"]["apple_music_rows"] == 0
    assert by_market["ng"]["apple_music_rows"] == 0
    assert by_market["ke"]["apple_music_rows"] == 0


def test_log_pipeline_run_includes_wave2_row_columns(load_run_module):
    """Wave 2 per-source rollups must surface in pipeline_runs when the
    connectors populate them. Flags still off in sources.yaml, so these
    fields stay 0 in production until the live-probe + flag-flip lands;
    this test guards the wiring so the flag flip itself does not regress.
    """
    rows = _capture_log_pipeline_rows(
        load_run_module,
        market_source_counts={
            "za": {
                "top_terms": 25,
                "brand24_mention_sentiment": 11,
                "brand24_mention_reach": 9,
                "brand24_daily_metric": 3,
                "youtube_playlist_items": 14,
            },
            "ng": {},
            "ke": {},
        },
        score_rows=[],
        errors=[],
    )
    by_market = {r["market"]: r for r in rows}
    assert by_market["za"]["top_terms_rows"] == 25
    assert by_market["za"]["brand24_mention_sentiment_rows"] == 11
    assert by_market["za"]["brand24_mention_reach_rows"] == 9
    assert by_market["za"]["brand24_daily_metric_rows"] == 3
    assert by_market["za"]["youtube_playlist_items_rows"] == 14
    # Markets without these counts default to 0, never NULL.
    assert by_market["ng"]["top_terms_rows"] == 0
    assert by_market["ng"]["brand24_mention_sentiment_rows"] == 0
    assert by_market["ng"]["brand24_mention_reach_rows"] == 0
    assert by_market["ng"]["brand24_daily_metric_rows"] == 0
    assert by_market["ng"]["youtube_playlist_items_rows"] == 0


def test_sub_source_counts_derives_per_feed(load_run_module):
    """Populate-side fix: sub-feed counts are derived from the concatenated
    frame's query_group / content_type, so pipeline_runs no longer writes 0
    for feeds that actually landed rows."""
    pd = load_run_module.pd
    df = pd.DataFrame(
        [
            {"query_group": "google_trends_top", "content_type": "top_term"},
            {"query_group": "google_trends_top", "content_type": "top_term"},
            {"query_group": "youtube_playlist_items", "content_type": "youtube_channel_upload"},
            {
                "query_group": "brand24_mention_sentiment",
                "content_type": "brand24_mention_sentiment",
            },
            {"query_group": "brand24_mention_reach", "content_type": "brand24_mention_reach"},
            # query_group is the plural "brand24_daily_metrics"; the join must
            # use content_type (singular) or this row is missed.
            {"query_group": "brand24_daily_metrics", "content_type": "brand24_daily_metric"},
            # Non-matching parent-connector rows must not be counted.
            {"query_group": "youtube_trending", "content_type": "video/10"},
        ]
    )
    counts = load_run_module._sub_source_counts(df)
    assert counts["top_terms"] == 2
    assert counts["youtube_playlist_items"] == 1
    assert counts["brand24_mention_sentiment"] == 1
    assert counts["brand24_mention_reach"] == 1
    assert counts["brand24_daily_metric"] == 1


def test_sub_source_counts_missing_columns_return_zero(load_run_module):
    """A frame with no query_group / content_type columns yields 0 for every
    sub-source, never a KeyError."""
    pd = load_run_module.pd
    df = pd.DataFrame([{"title": "x"}, {"title": "y"}])
    counts = load_run_module._sub_source_counts(df)
    assert counts["youtube_playlist_items"] == 0
    assert counts["top_terms"] == 0
    assert set(counts.values()) == {0}


def test_log_pipeline_run_total_excludes_sub_source_counts(load_run_module):
    """Sub-source rollups share src_counts with their parent connector key, so
    total_rows must sum connector keys only and not double-count the playlist /
    top_terms / brand24 sub-feeds that ride inside them."""
    rows = _capture_log_pipeline_rows(
        load_run_module,
        market_source_counts={
            "za": {
                "youtube": 100,
                "youtube_playlist_items": 20,
                "bigquery_trends": 50,
                "top_terms": 25,
            },
            "ng": {},
            "ke": {},
        },
        score_rows=[],
        errors=[],
    )
    by_market = {r["market"]: r for r in rows}
    # 100 (youtube) + 50 (bigquery_trends); the 20 + 25 sub-feed rows ride
    # inside those parents and must not be added again.
    assert by_market["za"]["total_rows"] == 150
    assert by_market["za"]["youtube_playlist_items_rows"] == 20
    assert by_market["za"]["top_terms_rows"] == 25
    assert by_market["za"]["status"] == "success"


def test_log_pipeline_run_briefs_generated_per_market(load_run_module):
    """Regression guard for the 28 May fix: briefs_generated must reflect the
    per-market brief count, not be hardcoded to 0.

    3 NG briefs + 1 ZA brief + 0 KE briefs should land as briefs_generated
    counts of 3, 1, 0 in the respective pipeline_runs rows.
    """
    rows = _capture_log_pipeline_rows(
        load_run_module,
        market_source_counts={"za": {"rss": 10}, "ng": {"rss": 20}, "ke": {"rss": 5}},
        score_rows=[],
        errors=[],
        briefs_by_market={"za": 1, "ng": 3, "ke": 0},
    )
    by_market = {r["market"]: r for r in rows}
    assert by_market["ng"]["briefs_generated"] == 3
    assert by_market["za"]["briefs_generated"] == 1
    assert by_market["ke"]["briefs_generated"] == 0


def test_log_pipeline_run_briefs_generated_defaults_to_zero(load_run_module):
    """Backwards compat: callers that omit briefs_by_market still get 0."""
    rows = _capture_log_pipeline_rows(
        load_run_module,
        market_source_counts={"za": {"rss": 10}, "ng": {"rss": 20}, "ke": {"rss": 5}},
        score_rows=[],
        errors=[],
    )
    by_market = {r["market"]: r for r in rows}
    assert by_market["za"]["briefs_generated"] == 0
    assert by_market["ng"]["briefs_generated"] == 0
    assert by_market["ke"]["briefs_generated"] == 0


def test_briefs_per_market_counts_by_first_tuple_element(load_run_module):
    """_briefs_per_market keys on tuple[0] (market) so 3 NG briefs + 0 KE
    briefs produce {ng: 3, ke: 0}."""
    briefs_by_topic = {
        ("ng", "music_afrobeats"): {"description_rationale": "..."},
        ("ng", "diaspora_japa"): {"description_rationale": "..."},
        ("ng", "fintech_naira"): {"description_rationale": "..."},
        ("za", "music_amapiano"): {"description_rationale": "..."},
    }
    counts = load_run_module._briefs_per_market(briefs_by_topic)
    assert counts["ng"] == 3
    assert counts["za"] == 1
    assert counts["ke"] == 0


def test_briefs_per_market_handles_none(load_run_module):
    counts = load_run_module._briefs_per_market(None)
    assert counts == {"za": 0, "ng": 0, "ke": 0}


def test_briefs_per_market_ignores_unknown_market(load_run_module):
    """Defensive: a brief keyed on an unrecognised market does not crash."""
    counts = load_run_module._briefs_per_market({("ng", "x"): {}, ("xx", "y"): {}, ("za", "z"): {}})
    assert counts == {"za": 1, "ng": 1, "ke": 0}


# --- CONNECTORS registry integrity ----------------------------------------


def test_connectors_registry_has_unique_source_keys(load_run_module):
    keys = [k for k, _ in load_run_module.CONNECTORS]
    assert len(keys) == len(set(keys))


# --- helpers ---------------------------------------------------------------


def _capture_log_pipeline_rows(
    module, market_source_counts, score_rows, errors, briefs_by_market=None
):
    """Call log_pipeline_run with insert_dataframe stubbed, return the row list."""
    captured: list = []

    def _capture(df, table):
        captured.extend(df.to_dict(orient="records"))
        return len(df)

    with patch.object(module, "insert_dataframe", side_effect=_capture):
        module.log_pipeline_run(
            run_id="test-run",
            started_at=datetime(2026, 4, 21, tzinfo=UTC),
            finished_at=datetime(2026, 4, 21, tzinfo=UTC),
            market_source_counts=market_source_counts,
            score_rows=score_rows,
            errors=errors,
            briefs_by_market=briefs_by_market,
        )
    return captured


# --- _log_email_status (email outcome marker row) -------------------------


def _capture_email_status_rows(module, email_status):
    """Call _log_email_status with insert_dataframe stubbed, return the rows.

    The module logger is patched to a MagicMock because pytest's log-capture
    handler segfaults on Windows + Python 3.13 when a real LogRecord is
    emitted mid-test (known environmental crash noted in DEVELOPMENT.md). The
    coerce-unknown path emits logger.warning, so the patch is required, not
    cosmetic.
    """
    captured: list = []

    def _capture(df, table):
        captured.append((table, df.to_dict(orient="records")))
        return len(df)

    with (
        patch.object(module, "insert_dataframe", side_effect=_capture),
        patch.object(module, "logger", MagicMock()),
    ):
        module._log_email_status(
            run_id="test-run",
            env="dev",
            started_at=datetime(2026, 5, 31, tzinfo=UTC),
            email_status=email_status,
        )
    return captured


def test_log_email_status_writes_marker_row_with_audit_status(load_run_module):
    """A send_failed outcome lands a single pipeline_runs marker row whose
    status is 'email_audit' (NOT 'success') so the idempotency guard ignores
    it, market is 'ALL', and email_status carries the failure verbatim."""
    captured = _capture_email_status_rows(load_run_module, "send_failed")

    assert len(captured) == 1
    table, rows = captured[0]
    assert table == "pipeline_runs"
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "email_audit"
    assert row["market"] == "ALL"
    assert row["email_status"] == "send_failed"
    assert row["run_id"] == "test-run"


def test_log_email_status_marker_never_uses_success_status(load_run_module):
    """Cron-safety guard: the marker row must never be status='success' for
    ANY outcome, or check_already_ran_today.py would count it and skip the
    next fire. Assert across every allowed outcome."""
    for outcome in ("sent", "send_failed", "skipped_nothing_notable", "render_error"):
        captured = _capture_email_status_rows(load_run_module, outcome)
        row = captured[0][1][0]
        assert row["status"] == "email_audit"
        assert row["status"] != "success"
        assert row["email_status"] == outcome


def test_log_email_status_render_error_recorded(load_run_module):
    """The render-crash path passes 'render_error'; it must persist as-is so
    bq-snapshot flags the no-email day."""
    captured = _capture_email_status_rows(load_run_module, "render_error")
    assert captured[0][1][0]["email_status"] == "render_error"


def test_log_email_status_coerces_unknown_value_to_render_error(load_run_module):
    """An unexpected status string must never reach the column the morning
    check reads; it is coerced to render_error (fail loud, not silent)."""
    captured = _capture_email_status_rows(load_run_module, "totally_unexpected")
    assert captured[0][1][0]["email_status"] == "render_error"


def test_log_email_status_write_failure_is_non_fatal(load_run_module):
    """A BQ blip writing the marker must be swallowed, not raised, so the run
    still exits 0 even when the email outcome cannot be recorded."""

    def _boom(df, table):
        raise RuntimeError("BQ unavailable")

    with (
        patch.object(load_run_module, "insert_dataframe", side_effect=_boom),
        patch.object(load_run_module, "logger", MagicMock()),
    ):
        # Must not raise.
        load_run_module._log_email_status(
            run_id="test-run",
            env="dev",
            started_at=datetime(2026, 5, 31, tzinfo=UTC),
            email_status="send_failed",
        )


def test_log_email_status_skipped_quiet_day_is_healthy_value(load_run_module):
    """A legitimately quiet day records 'skipped_nothing_notable', which is a
    healthy value and must NOT be coerced or flagged as a failure."""
    captured = _capture_email_status_rows(load_run_module, "skipped_nothing_notable")
    row = captured[0][1][0]
    assert row["email_status"] == "skipped_nothing_notable"
    assert row["email_status"] in load_run_module._EMAIL_STATUS_HEALTHY


def test_markets_already_ingested_includes_partial_status(load_run_module, monkeypatch):
    """Partial markets must be skipped on fallback to avoid double-append ingest."""
    captured: list[str] = []

    class _Rows:
        def __iter__(self):
            return iter([])

    class _Client:
        project = "test-proj"

        def query(self, sql):
            captured.append(sql)
            return _Rows()

    monkeypatch.setattr("src.utils.bigquery.get_client", lambda: _Client())
    monkeypatch.setattr("src.utils.bigquery.get_dataset", lambda: "trends_v2_dev")

    load_run_module._markets_already_ingested_today()

    assert captured
    assert "partial" in captured[0]
    assert "success" in captured[0]


def test_safe_full_read_url_accepts_https_only():
    import src.alerts.email_digest as ed

    assert ed._safe_full_read_url("") == ""
    assert ed._safe_full_read_url("#") == ""
    assert ed._safe_full_read_url("javascript:alert(1)") == ""
    good = "https://storage.googleapis.com/bucket/pulse/x.html"
    assert ed._safe_full_read_url(good) == good


def test_safe_href_url_accepts_https_only():
    import src.alerts.email_digest as ed

    assert ed._safe_href_url("http://example.com") == ""
    assert ed._safe_href_url("javascript:alert(1)") == ""
    assert ed._safe_href_url("https://example.com/x") == "https://example.com/x"


def test_multi_assign_aggregation_distributes_per_topic_with_weight(load_run_module):
    """One row with 3 topics should contribute 1/3 to each topic's item_count."""
    import pandas as pd

    run = load_run_module

    df = pd.DataFrame(
        [
            {
                "market": "za",
                "query_group": "news",
                "source": "rss",
                "author_handle_norm": "author-a",
                "regional_score": 0.5,
                "genz_score": 0.3,
                "slang_score": 0.1,
                "creator_watchlist_score": 0.0,
                "search_velocity_score": 0.0,
                "engagement_weighted": 300.0,
                "tone_avg": None,
                "topic_groups": [
                    "music_amapiano",
                    "finance_stokvel",
                    "education_matric_nsfas",
                ],
            }
        ]
    )

    market_counts, unclassified = run._aggregate_by_topic(df, market="za")

    # Three topic buckets, each with item_count 1/3
    assert len(market_counts) == 3
    for _key, stats in market_counts.items():
        assert stats["item_count"] == pytest.approx(1.0 / 3.0, rel=1e-6)
        assert stats["engagement_sum"] == pytest.approx(300.0 / 3.0, rel=1e-6)
    assert unclassified == 0


def test_semantic_corroboration_counts_same_entity_across_families(load_run_module):
    """Forward Phase 2 shadow: when the SAME entity (hashtag) is named on two
    different channel families for a topic, semantic_corroboration_families is 2
    and one entity spans the boundary. trend_score is untouched (not asserted
    here, it is shadow)."""
    import pandas as pd

    run = load_run_module
    df = pd.DataFrame(
        [
            {
                "market": "za",
                "query_group": "sports",
                "source": "rss",  # -> news family
                "platform": "",
                "author_handle_norm": "",
                "hashtags": "#bafana",
                "topic_groups": ["sports_football"],
            },
            {
                "market": "za",
                "query_group": "sports",
                "source": "reddit",  # -> reddit family
                "platform": "reddit",
                "author_handle_norm": "",
                "hashtags": "#bafana #orlandopirates",
                "topic_groups": ["sports_football"],
            },
        ]
    )
    market_counts, _ = run._aggregate_by_topic(df, market="za")
    stats = market_counts[("za", "sports_football")]
    assert stats["channel_diversity"] == 2
    assert stats["semantic_corroboration_families"] == 2
    assert stats["semantic_corroboration_entities"] == 1


def test_semantic_corroboration_zero_when_entities_do_not_cross_families(load_run_module):
    """Two families carry the topic, but each names a DIFFERENT entity, so no
    single entity is corroborated across families: families stays 1, entities 0.
    This is the case the structural channel_diversity (2) overcounts."""
    import pandas as pd

    run = load_run_module
    df = pd.DataFrame(
        [
            {
                "market": "za",
                "query_group": "sports",
                "source": "rss",
                "platform": "",
                "author_handle_norm": "",
                "hashtags": "#bafana",
                "topic_groups": ["sports_football"],
            },
            {
                "market": "za",
                "query_group": "sports",
                "source": "reddit",
                "platform": "reddit",
                "author_handle_norm": "",
                "hashtags": "#chiefs",
                "topic_groups": ["sports_football"],
            },
        ]
    )
    market_counts, _ = run._aggregate_by_topic(df, market="za")
    stats = market_counts[("za", "sports_football")]
    assert stats["channel_diversity"] == 2
    assert stats["semantic_corroboration_families"] == 1
    assert stats["semantic_corroboration_entities"] == 0


def test_semantic_corroboration_ignores_missing_entity_columns(load_run_module):
    """Rows with no entity columns at all must not crash or inject a 'nan' token;
    the shadow metrics fall to zero."""
    import pandas as pd

    run = load_run_module
    df = pd.DataFrame(
        [
            {
                "market": "za",
                "query_group": "news",
                "source": "rss",
                "platform": "",
                "author_handle_norm": "",
                "topic_groups": ["finance_stokvel"],
            }
        ]
    )
    market_counts, _ = run._aggregate_by_topic(df, market="za")
    stats = market_counts[("za", "finance_stokvel")]
    assert stats["semantic_corroboration_families"] == 0
    assert stats["semantic_corroboration_entities"] == 0


def test_watchlist_max_agg_uses_max_when_flag_on(load_run_module, monkeypatch):
    """WATCHLIST_MAX_AGG on: a topic's watchlist signal is the single best
    creator match (max), not the per-row mean that dilutes one real hit
    across thousands of zero-score rows (root cause of watchlist_score ~ 0)."""
    import pandas as pd

    run = load_run_module
    monkeypatch.setenv("WATCHLIST_MAX_AGG", "true")
    df = pd.DataFrame(
        [
            _agg_row(
                market="ng",
                topic_groups=["music_afrobeats"],
                creator_watchlist_score=0.0,
                author_handle_norm="fan_a",
            ),
            _agg_row(
                market="ng",
                topic_groups=["music_afrobeats"],
                creator_watchlist_score=0.0,
                author_handle_norm="fan_b",
            ),
            _agg_row(
                market="ng",
                topic_groups=["music_afrobeats"],
                creator_watchlist_score=0.7,
                author_handle_norm="burnaboyofficial",
            ),
        ]
    )
    market_counts, _ = run._aggregate_by_topic(df, market="ng")
    stats = market_counts[("ng", "music_afrobeats")]
    assert stats["watchlist_avg"] == pytest.approx(0.7)


def test_watchlist_mean_agg_unchanged_when_flag_off(load_run_module, monkeypatch):
    """Default (flag off): watchlist stays the weighted mean, so the OFF path
    stays byte-identical to the live cron."""
    import pandas as pd

    run = load_run_module
    monkeypatch.delenv("WATCHLIST_MAX_AGG", raising=False)
    df = pd.DataFrame(
        [
            _agg_row(
                market="ng",
                topic_groups=["music_afrobeats"],
                creator_watchlist_score=0.0,
                author_handle_norm="fan_a",
            ),
            _agg_row(
                market="ng",
                topic_groups=["music_afrobeats"],
                creator_watchlist_score=0.0,
                author_handle_norm="fan_b",
            ),
            _agg_row(
                market="ng",
                topic_groups=["music_afrobeats"],
                creator_watchlist_score=0.7,
                author_handle_norm="burnaboyofficial",
            ),
        ]
    )
    market_counts, _ = run._aggregate_by_topic(df, market="ng")
    stats = market_counts[("ng", "music_afrobeats")]
    assert stats["watchlist_avg"] == pytest.approx(0.7 / 3.0)


def test_multi_assign_unclassified_rows_counted_and_skipped(load_run_module):
    """Rows with empty topic_groups bump unclassified counter, skip scoring."""
    import pandas as pd

    run = load_run_module

    df = pd.DataFrame(
        [
            {
                "market": "za",
                "query_group": "news",
                "source": "rss",
                "author_handle_norm": "author-a",
                "regional_score": 0.0,
                "genz_score": 0.0,
                "slang_score": 0.0,
                "creator_watchlist_score": 0.0,
                "search_velocity_score": 0.0,
                "engagement_weighted": 100.0,
                "tone_avg": None,
                "topic_groups": [],
            },
            {
                "market": "za",
                "query_group": "news",
                "source": "rss",
                "author_handle_norm": "author-b",
                "regional_score": 0.0,
                "genz_score": 0.0,
                "slang_score": 0.0,
                "creator_watchlist_score": 0.0,
                "search_velocity_score": 0.0,
                "engagement_weighted": 200.0,
                "tone_avg": None,
                "topic_groups": ["music_amapiano"],
            },
        ]
    )

    market_counts, unclassified = run._aggregate_by_topic(df, market="za")

    assert unclassified == 1
    assert len(market_counts) == 1
    key, stats = next(iter(market_counts.items()))
    assert key == ("za", "music_amapiano")
    assert stats["item_count"] == pytest.approx(1.0, rel=1e-6)


def test_aggregate_by_topic_mixed_published_at_types(load_run_module):
    """Regression for the 2026-06-27 cron outage. published_at arrives as a mix
    of str and pandas Timestamp across connectors; the freshest-row max compared
    them directly, so "str > Timestamp" raised and aborted every market. The
    accumulator must coerce to one UTC Timestamp first."""
    import pandas as pd

    run = load_run_module
    df = pd.DataFrame(
        [
            _agg_row(
                source="rss",
                platform="web",
                topic_groups=["music_amapiano"],
                published_at="2026-06-26T08:00:00Z",
            ),
            _agg_row(
                source="gdelt",
                platform="news",
                topic_groups=["music_amapiano"],
                published_at=pd.Timestamp("2026-06-27T09:00:00Z"),
            ),
        ]
    )

    # Must not raise (the str vs Timestamp comparison was the crash).
    market_counts, _ = run._aggregate_by_topic(df, market="ng")

    key, stats = next(iter(market_counts.items()))
    assert key == ("ng", "music_amapiano")
    freshest = stats["freshest_published_at"]
    assert freshest is not None
    # The later Timestamp wins the max, coerced to a tz-aware UTC Timestamp.
    assert pd.Timestamp(freshest).tz_convert("UTC").hour == 9


def _agg_row(**overrides):
    """Build a minimal enriched row dict for _aggregate_by_topic tests."""
    base = {
        "market": "ng",
        "query_group": "news",
        "source": "brand24",
        "platform": "web",
        "author_handle_norm": "",
        "regional_score": 0.0,
        "genz_score": 0.0,
        "slang_score": 0.0,
        "creator_watchlist_score": 0.0,
        "search_velocity_score": 0.0,
        "engagement_weighted": 0.0,
        "tone_avg": None,
        "topic_groups": [],
    }
    base.update(overrides)
    return base


def test_aggregate_by_topic_excludes_brand24_aggregate_rows(load_run_module):
    """Workstream E: platform='aggregate' rows (Brand24 Wave-2 surfaces) are
    excluded from trend_scores AND do not count as unclassified. A normal
    classified row still scores."""
    import pandas as pd

    run = load_run_module
    df = pd.DataFrame(
        [
            _agg_row(content_type="brand24_mention_sentiment", platform="aggregate"),
            _agg_row(content_type="brand24_mention_reach", platform="aggregate"),
            _agg_row(content_type="brand24_daily_metric", platform="aggregate"),
            _agg_row(
                source="rss",
                platform="news",
                engagement_weighted=100.0,
                topic_groups=["music_afrobeats"],
            ),
        ]
    )
    market_counts, unclassified = run._aggregate_by_topic(df, market="ng")

    # Aggregate rows neither score nor count as unclassified.
    assert unclassified == 0
    assert len(market_counts) == 1
    key, _stats = next(iter(market_counts.items()))
    assert key == ("ng", "music_afrobeats")


def test_aggregate_exclusion_runs_before_unclassified_bump(load_run_module):
    """A lone platform='aggregate' row yields empty counts AND unclassified=0,
    proving the guard sits before the `if not topics` bump."""
    import pandas as pd

    run = load_run_module
    df = pd.DataFrame([_agg_row(content_type="brand24_mention_reach", platform="aggregate")])
    market_counts, unclassified = run._aggregate_by_topic(df, market="ng")
    assert market_counts == {} or len(market_counts) == 0
    assert unclassified == 0


def test_aggregate_by_topic_rejects_nan_tone(load_run_module):
    """NaN tone_avg must not poison tone_sum and null the trend_score.

    Regression guard: pandas stores missing tone as NaN on non-GDELT rows.
    Earlier bug let NaN slip past a plain ``is not None`` check into
    tone_sum, producing a NaN tone_avg_mean, NaN trend_score, and a
    BadRequest from BigQuery on MERGE.
    """
    import pandas as pd

    run = load_run_module

    df = pd.DataFrame(
        [
            {
                "market": "za",
                "query_group": "news",
                "source": "rss",
                "author_handle_norm": "author-a",
                "regional_score": 0.0,
                "genz_score": 0.0,
                "slang_score": 0.0,
                "creator_watchlist_score": 0.0,
                "search_velocity_score": 0.0,
                "engagement_weighted": 100.0,
                "tone_avg": float("nan"),
                "topic_groups": ["music_amapiano"],
            }
        ]
    )

    market_counts, _ = run._aggregate_by_topic(df, market="za")
    stats = market_counts[("za", "music_amapiano")]
    assert stats["tone_rows"] == 0
    assert stats["tone_avg_mean"] is None


def test_aggregate_tone_mean_is_weight_weighted_not_row_count(load_run_module):
    """tone_avg_mean must be the weight-weighted mean (tone_sum / sum-of-weights),
    not tone_sum / row_count. A row split across N topics contributes 1/N weight
    to each topic, so its tone must be down-weighted in the mean too, matching
    the regional/genz/slang averages which already divide by the weighted
    item_count."""
    import pandas as pd

    run = load_run_module
    df = pd.DataFrame(
        [
            _agg_row(source="gdelt", platform="news", tone_avg=0.8, topic_groups=["news"]),
            _agg_row(
                source="gdelt",
                platform="news",
                tone_avg=0.2,
                topic_groups=["news", "politics_governance"],
            ),
        ]
    )
    market_counts, _ = run._aggregate_by_topic(df, market="ng")
    stats = market_counts[("ng", "news")]
    # row1 weight 1.0 tone 0.8; row2 weight 0.5 tone 0.2
    # weighted mean = (0.8*1.0 + 0.2*0.5) / (1.0 + 0.5) = 0.9 / 1.5 = 0.6
    assert stats["tone_avg_mean"] == pytest.approx(0.6, rel=1e-6)
    assert stats["tone_rows"] == 2


def test_compute_trend_scores_engagement_divisor_matches_siblings(load_run_module):
    """engagement_score uses max(item_count, 1e-9), the same sub-1 divisor as
    the aggregator's sibling averages, not max(item_count, 1). A bucket whose
    rows are all multi-topic-split has item_count<1 and was suppressed by the
    /1 floor relative to its peers."""
    from datetime import UTC as _UTC
    from datetime import date as _date
    from datetime import datetime as _dt

    run = load_run_module
    stats = {
        "item_count": 0.5,
        "source_diversity": 0,
        "platform_diversity": 0,
        "regional_avg": 0.0,
        "genz_avg": 0.0,
        "slang_avg": 0.0,
        "watchlist_avg": 0.0,
        "search_velocity_avg": 0.0,
        "engagement_sum": 2000.0,
        "creator_spread": 0,
        "tone_rows": 0,
        "tone_avg_mean": None,
    }
    rows = run.compute_trend_scores(
        {("ng", "news"): stats}, _date(2026, 6, 16), _dt(2026, 6, 16, tzinfo=_UTC)
    )
    # per_row_engagement = 2000 / 0.5 = 4000 -> 4000/5000 = 0.8
    assert rows[0]["engagement_score"] == pytest.approx(0.8, rel=1e-6)


# --- platform_diversity + cross-source multiplier --------------------------


def _scoring_yaml_with_multiplier(load_run_module):
    """Patch load_scoring so compute_trend_scores reads our multiplier knobs."""
    return patch.object(
        load_run_module,
        "load_scoring",
        return_value={
            "weights": {
                "velocity": 0.20,
                "diversity": 0.16,
                "engagement": 0.10,
                "creator_spread": 0.12,
                "regional_score": 0.12,
                "genz_score": 0.00,
                "watchlist_score": 0.00,
                "search_velocity_score": 0.15,
                "slang_score": 0.10,
                "tone_score": 0.05,
            },
            "cross_source_bonus_per_channel": 0.05,
            "cross_source_max_bonus": 0.15,
        },
    )


def test_aggregate_by_topic_collects_distinct_platforms(load_run_module):
    """platform_diversity counts unique platform values across rows in a topic."""
    import pandas as pd

    run = load_run_module

    df = pd.DataFrame(
        [
            {
                "market": "ng",
                "query_group": "music_afrobeats",
                "source": "EnsembleData",
                "platform": "tiktok",
                "author_handle_norm": "creator-a",
                "regional_score": 0.0,
                "genz_score": 0.0,
                "slang_score": 0.0,
                "creator_watchlist_score": 0.0,
                "search_velocity_score": 0.0,
                "engagement_weighted": 100.0,
                "tone_avg": None,
                "topic_groups": ["music_afrobeats"],
            },
            {
                "market": "ng",
                "query_group": "music_afrobeats",
                "source": "YouTube",
                "platform": "youtube",
                "author_handle_norm": "creator-b",
                "regional_score": 0.0,
                "genz_score": 0.0,
                "slang_score": 0.0,
                "creator_watchlist_score": 0.0,
                "search_velocity_score": 0.0,
                "engagement_weighted": 200.0,
                "tone_avg": None,
                "topic_groups": ["music_afrobeats"],
            },
            {
                "market": "ng",
                "query_group": "music_afrobeats",
                "source": "PunchNG",
                "platform": "web",
                "author_handle_norm": "byline-c",
                "regional_score": 0.0,
                "genz_score": 0.0,
                "slang_score": 0.0,
                "creator_watchlist_score": 0.0,
                "search_velocity_score": 0.0,
                "engagement_weighted": 0.0,
                "tone_avg": None,
                "topic_groups": ["music_afrobeats"],
            },
        ]
    )

    market_counts, _ = run._aggregate_by_topic(df, market="ng")
    stats = market_counts[("ng", "music_afrobeats")]
    assert stats["platform_diversity"] == 3
    # EnsembleData -> ensemble, YouTube -> youtube, PunchNG -> news: three
    # independent channel families drive the graded cross-source multiplier.
    assert stats["channel_diversity"] == 3


def test_compute_trend_scores_grades_cross_source_by_channels(load_run_module):
    """The cross-source bonus scales with channel_diversity: n=1 -> 1.00,
    n=2 -> 1.05, n=4 -> 1.15 (capped). One channel gets no bump."""
    run = load_run_module
    trend_date = datetime(2026, 5, 27, tzinfo=UTC).date()
    scored_at = datetime(2026, 5, 27, 6, 35, tzinfo=UTC)

    base = {
        "item_count": 10.0,
        "source_diversity": 5,
        "engagement_item_count": 10.0,
        "regional_avg": 0.5,
        "genz_avg": 0.4,
        "slang_avg": 0.3,
        "watchlist_avg": 0.2,
        "search_velocity_avg": 0.1,
        "engagement_sum": 500.0,
        "creator_spread": 3,
        "tone_avg_mean": None,
        "tone_rows": 0,
    }
    one = dict(base, channel_diversity=1)
    two = dict(base, channel_diversity=2)
    four = dict(base, channel_diversity=4)

    market_counts = {
        ("za", "one_channel"): one,
        ("za", "two_channel"): two,
        ("za", "four_channel"): four,
    }

    with _scoring_yaml_with_multiplier(run):
        rows = run.compute_trend_scores(market_counts, trend_date, scored_at)

    by_topic = {r["query_group"]: r for r in rows}
    one_score = by_topic["one_channel"]["trend_score"]
    two_score = by_topic["two_channel"]["trend_score"]
    four_score = by_topic["four_channel"]["trend_score"]
    # one_channel: no corroboration, multiplier 1.0 (composite unchanged).
    # two_channel: composite * 1.05. four_channel: composite * 1.15 (capped).
    base_composite = one_score  # multiplier 1.0 at n=1
    # abs tolerance absorbs the double-rounding (one_score is already rounded).
    assert two_score == pytest.approx(base_composite * 1.05, abs=1e-3)
    assert four_score == pytest.approx(base_composite * 1.15, abs=1e-3)
    assert one_score < two_score < four_score


def test_compute_trend_scores_cross_source_caps_at_one(load_run_module):
    """A composite that would exceed 1.0 after multiplication clamps to 1.0."""
    run = load_run_module
    trend_date = datetime(2026, 5, 27, tzinfo=UTC).date()
    scored_at = datetime(2026, 5, 27, 6, 35, tzinfo=UTC)

    high = {
        "item_count": 100.0,
        "source_diversity": 20,  # saturates diversity_score at 1.0
        "channel_diversity": 4,  # max graded multiplier 1.15
        "regional_avg": 1.0,
        "genz_avg": 1.0,
        "slang_avg": 1.0,
        "watchlist_avg": 1.0,
        "search_velocity_avg": 1.0,
        "engagement_sum": 1_000_000.0,
        "creator_spread": 100,
        "tone_avg_mean": None,
        "tone_rows": 0,
    }

    with _scoring_yaml_with_multiplier(run):
        rows = run.compute_trend_scores(
            {("za", "saturated_topic"): high},
            trend_date,
            scored_at,
            # Max velocity pushes the unscaled composite to ~0.95; the tone
            # weight-redistribution then scales it close to 1.0 and the
            # 1.15x cross-source multiplier overflows. Cap to 1.0 expected.
            velocity_scores={("za", "saturated_topic"): 1.0},
        )

    assert rows[0]["trend_score"] == 1.0


def test_channel_family_youtube_falls_back_to_platform(load_run_module):
    run = load_run_module
    # YouTube rows carry source=channelTitle (e.g. "MrBeast"), so the family must
    # resolve from the platform string, not silently default to "news" (which
    # dropped YouTube as an independent cross-source channel).
    assert run._channel_family("MrBeast", "youtube") == "youtube"
    assert run._channel_family("Burna Boy VEVO", "youtube") == "youtube"
    # A known source still wins regardless of platform.
    assert run._channel_family("brand24", "facebook") == "brand24"
    # Unknown source AND unknown platform must NOT manufacture a factual news
    # family (phantom corroboration); it lands in "other".
    assert run._channel_family("MrBeast", "") == "other"


def test_channel_family_wave2_connectors_are_their_own_family(load_run_module):
    run = load_run_module
    # Wikipedia and Bluesky must not fall through to "news", or they would inflate
    # news corroboration in the cross-source multiplier when their flags flip.
    assert run._channel_family("wikipedia", "wikipedia") == "wikipedia"
    assert run._channel_family("bluesky", "bluesky") == "bluesky"


def test_channel_family_platform_fallback_never_defaults_to_news(load_run_module):
    run = load_run_module
    # RSS news domains (source=feed_name, platform=web) stay news by design.
    assert run._channel_family("Daily Maverick", "web") == "news"
    assert run._channel_family("gdelt", "news") == "news"
    # bigquery_trends top_terms rows are search, not news (was a live leak).
    assert run._channel_family("bigquery_trends", "google_search") == "search"
    # SocialCrawl rows (arbitrary source, social platform) collapse to the
    # ensemble family: same-platform content from a second vendor is not an
    # independent corroborating channel (was a live leak to news).
    assert run._channel_family("some_account", "tiktok") == "ensemble"
    assert run._channel_family("socialcrawl", "instagram") == "ensemble"
    # A wholly unknown connector is one channel, never a factual family.
    assert run._channel_family("future_vendor", "future_platform") == "other"


# --- GCAM-reader: aggregation + composite ----------------------------------


def _scoring_yaml_with_gcam(load_run_module):
    """Patch load_scoring with a LIVE gcam weight (0.03), carved from tone (0.02),
    so the gcam term is exercised. Mirrors _scoring_yaml_with_multiplier."""
    return patch.object(
        load_run_module,
        "load_scoring",
        return_value={
            "weights": {
                "velocity": 0.20,
                "diversity": 0.16,
                "engagement": 0.10,
                "creator_spread": 0.12,
                "regional_score": 0.12,
                "genz_score": 0.00,
                "watchlist_score": 0.00,
                "search_velocity_score": 0.15,
                "slang_score": 0.10,
                "tone_score": 0.02,
                "gcam_score": 0.03,
            },
            "cross_source_bonus_per_channel": 0.05,
            "cross_source_max_bonus": 0.15,
        },
    )


def test_aggregate_by_topic_accumulates_gcam(load_run_module):
    """Two GDELT rows carry gcam_intensity; one social row carries NaN. The topic
    gets gcam_rows=2 and gcam_avg_mean = weighted mean of the two GDELT rows."""
    import pandas as pd

    run = load_run_module

    def _row(gcam, source, platform):
        return {
            "market": "ke",
            "query_group": "economy_sapa_hustle",
            "source": source,
            "platform": platform,
            "author_handle_norm": "",
            "regional_score": 0.0,
            "genz_score": 0.0,
            "slang_score": 0.0,
            "creator_watchlist_score": 0.0,
            "search_velocity_score": 0.0,
            "engagement_weighted": 0.0,
            "tone_avg": None,
            "gcam_intensity": gcam,
            "content_type": "gdelt_gkg",
            "topic_groups": ["economy_sapa_hustle"],
        }

    df = pd.DataFrame(
        [
            _row(0.4, "news24.com", "news"),
            _row(0.6, "nation.africa", "news"),
            _row(float("nan"), "EnsembleData", "tiktok"),
        ]
    )
    out, _ = run._aggregate_by_topic(df, "ke")
    stats = out[("ke", "economy_sapa_hustle")]
    assert stats["gcam_rows"] == 2
    assert stats["gcam_avg_mean"] == pytest.approx(0.5, abs=1e-9)


def test_compute_trend_scores_gcam_weight_zero_is_byte_identical(load_run_module):
    """Ship-safe invariant: with scoring.yaml gcam_score at 0.00, a topic carrying
    gcam signal scores identically to the same topic with no gcam fields."""
    from datetime import date as _date
    from datetime import datetime as _dt

    run = load_run_module
    base = {
        "item_count": 0.5,
        "source_diversity": 2,
        "platform_diversity": 1,
        "channel_diversity": 1,
        "engagement_item_count": 1.0,
        "regional_avg": 0.3,
        "genz_avg": 0.4,
        "slang_avg": 0.2,
        "watchlist_avg": 0.1,
        "search_velocity_avg": 0.0,
        "engagement_sum": 1000.0,
        "creator_spread": 1,
        "tone_avg_mean": 0.6,
        "tone_rows": 3,
    }
    with_gcam = dict(base, gcam_avg_mean=0.9, gcam_rows=5)
    without = dict(base)
    rows = run.compute_trend_scores(
        {("ng", "with_gcam"): with_gcam, ("ng", "without"): without},
        _date(2026, 6, 25),
        _dt(2026, 6, 25, tzinfo=UTC),
    )
    by_topic = {r["query_group"]: r for r in rows}
    assert by_topic["with_gcam"]["trend_score"] == by_topic["without"]["trend_score"]
    # The score is still persisted on the row even at weight 0.
    assert by_topic["with_gcam"]["gcam_score"] == 0.9
    assert by_topic["with_gcam"]["gcam_rows"] == 5
    assert by_topic["without"]["gcam_score"] == 0.0
    assert by_topic["without"]["gcam_rows"] == 0


def test_compute_trend_scores_is_invariant_to_historical_genz_measurement(load_run_module):
    from datetime import date as _date
    from datetime import datetime as _dt

    base = {
        "item_count": 2.0,
        "source_diversity": 2,
        "platform_diversity": 1,
        "channel_diversity": 1,
        "engagement_item_count": 2.0,
        "regional_avg": 0.3,
        "slang_avg": 0.2,
        "watchlist_avg": 0.0,
        "search_velocity_avg": 0.1,
        "engagement_sum": 1000.0,
        "creator_spread": 1,
        "tone_avg_mean": 0.6,
        "tone_rows": 2,
    }
    rows = load_run_module.compute_trend_scores(
        {
            ("za", "low"): {**base, "genz_avg": 0.0},
            ("za", "high"): {**base, "genz_avg": 1.0},
        },
        _date(2026, 9, 9),
        _dt(2026, 9, 9, tzinfo=UTC),
    )
    by_topic = {row["query_group"]: row for row in rows}

    assert by_topic["low"]["trend_score"] == by_topic["high"]["trend_score"]
    assert by_topic["low"]["seed_score"] == by_topic["high"]["seed_score"]
    assert by_topic["low"]["genz_score"] == 0.0
    assert by_topic["high"]["genz_score"] == 1.0


def test_compute_trend_scores_gcam_contributes_when_weighted(load_run_module):
    """With a live gcam weight, a topic with high gcam intensity outscores the same
    topic with no gcam signal."""
    from datetime import date as _date
    from datetime import datetime as _dt

    run = load_run_module
    base = {
        "item_count": 0.5,
        "source_diversity": 2,
        "platform_diversity": 1,
        "channel_diversity": 1,
        "engagement_item_count": 1.0,
        "regional_avg": 0.3,
        "genz_avg": 0.4,
        "slang_avg": 0.2,
        "watchlist_avg": 0.1,
        "search_velocity_avg": 0.0,
        "engagement_sum": 1000.0,
        "creator_spread": 1,
        "tone_avg_mean": 0.6,
        "tone_rows": 3,
    }
    hot = dict(base, gcam_avg_mean=1.0, gcam_rows=5)
    cold = dict(base, gcam_avg_mean=None, gcam_rows=0)
    with _scoring_yaml_with_gcam(run):
        rows = run.compute_trend_scores(
            {("ng", "hot"): hot, ("ng", "cold"): cold},
            _date(2026, 6, 25),
            _dt(2026, 6, 25, tzinfo=UTC),
        )
    by_topic = {r["query_group"]: r for r in rows}
    assert by_topic["hot"]["trend_score"] > by_topic["cold"]["trend_score"]


# --- Phase 2 enrichment tail skip (duplicate-run cost guard) ---------------


_TAIL_STAGE_MARKERS = (
    "FORECAST: 7-DAY",
    "PAN-AFRICAN: CROSS-MARKET",
    "COMMENT SENTIMENT: ROOM",
    "DRIVING HASHTAGS: CONVERSATION",
    "WAVE 1: CONTINUITY",
    "SEED SCORE BADGES",
    "WAVE 2: TONE SPLIT",
    "RECONCILE: SHADOW",
    "persist_render_payloads",
)


def _tail_stage_guard_map(source):
    """Map each Phase 2 tail stage to whether its `if` tests enrichment_tail_enabled."""
    import ast

    tree = ast.parse(source)
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        guarded = any(
            isinstance(n, ast.Name) and n.id == "enrichment_tail_enabled"
            for n in ast.walk(node.test)
        )
        for marker in _TAIL_STAGE_MARKERS:
            if marker in body and marker not in found:
                found[marker] = guarded
    return found


def test_every_phase2_tail_stage_is_guarded(load_run_module):
    """Each enrichment stage must skip when a sibling run already sent the digest.

    The 02:30 fallback re-ran the whole tail after the 00:30 primary had already
    shipped, spending a second full set of Gemini calls every day for output that
    no email could carry, and writing the reconcile audit trail twice. A stage
    added later without the guard silently reintroduces that cost, so this asserts
    the guard structurally rather than trusting review to catch it.
    """
    import inspect

    guards = _tail_stage_guard_map(inspect.getsource(load_run_module))

    missing = [m for m in _TAIL_STAGE_MARKERS if m not in guards]
    assert not missing, f"tail stage markers not found in run_rss_now: {missing}"

    unguarded = sorted(m for m, ok in guards.items() if not ok)
    assert not unguarded, (
        "Phase 2 tail stages missing the enrichment_tail_enabled guard: "
        f"{unguarded}. Add `and enrichment_tail_enabled` to the stage's condition."
    )


def test_send_site_guard_stays_an_independent_late_check(load_run_module):
    """The duplicate-email guard keeps its own late call, it does not reuse the flag.

    enrichment_tail_enabled is computed before the enrichment stages run. Reusing
    that value at the send site would widen the window in which a sibling's send
    goes unnoticed, which is exactly the duplicate digest the send-site guard is
    there to prevent.
    """
    import inspect

    assert "if _email_already_sent_today():" in inspect.getsource(load_run_module)


def test_r3_seed_manifest_accepts_a_window_with_no_tiktok_music_seed(load_run_module):
    # 4 Sep 2026: the released runs carried no TikTok music seed; the manifest
    # reads that as an empty list and the other lists still seed the pilot.
    row = {
        "replacement_run_id": "run_20260903_dynamic_apply_v2_r16",
        "row_set_digest": "a" * 64,
        "source_sha": "b" * 40,
        "tiktok_music_identifiers": [],
        "reddit_urls": [],
        "instagram_search_terms": [{"term": "culture", "market": "za"}],
        "youtube_short_urls": [{"url": "https://youtube.invalid/short", "market": "za"}],
    }

    class Job:
        def result(self, **_kwargs):
            return (row,)

    class Client:
        def query(self, *_args, **_kwargs):
            return Job()

    manifest = load_run_module._r3_seed_manifest(Client(), datetime(2026, 9, 4, tzinfo=UTC))
    assert manifest["tiktok_music_ids"] == []
    assert manifest["reddit_urls"] == []
    assert manifest["instagram_search_terms"] == [{"term": "culture", "market": "za"}]


# D05 boundary telemetry on the collection only path


def _bound_boundary_telemetry():
    from src.analysis.open_intelligence.coverage_telemetry_sink import (
        BoundaryTelemetry,
        MemoryDispositionSink,
    )

    telemetry = BoundaryTelemetry(MemoryDispositionSink())
    telemetry.bind(
        operation_id="exec-1",
        source_binding_digest="a" * 64,
        observed_at=datetime(2026, 6, 2, tzinfo=UTC),
    )
    return telemetry


def test_dedup_youtube_scrape_frames_records_each_drop_as_a_decision(load_run_module):
    m = load_run_module
    url = "https://www.youtube.com/watch?v=abc123def45"
    api = m.pd.DataFrame([{"platform": "youtube", "query_group": "music", "url": url}])
    scrape = m.pd.DataFrame(
        [
            {"platform": "youtube", "query_group": m.SCRAPE_QUERY_GROUP, "url": url},
            {
                "platform": "youtube",
                "query_group": m.SCRAPE_QUERY_GROUP,
                "url": "https://www.youtube.com/watch?v=zzz99zzz999",
            },
        ]
    )
    decisions = []
    deduped = m.dedup_youtube_scrape_frames([api, scrape], decisions)
    assert len(deduped[1]) == 1
    assert decisions == [
        {"frame_index": 1, "row_index": 0, "reason_code": "youtube_scrape_duplicate"}
    ]
    # The default call keeps its shape: no decision list, no change in behaviour.
    assert len(m.dedup_youtube_scrape_frames([api, scrape])[1]) == 1


def test_ingest_market_frames_emits_dispositions_from_its_own_decision_records(load_run_module):
    m = load_run_module
    url = "https://www.youtube.com/watch?v=abc123def45"
    frames = [
        m.pd.DataFrame(
            [
                {
                    "source": "socialcrawl",
                    "platform": "tiktok",
                    "native_id": "post:1",
                    "url": "https://t.example/1",
                    "title": "t",
                    "text": "x",
                    "published_at": None,
                    "market": "za",
                },
                {
                    "source": "youtube",
                    "platform": "youtube",
                    "query_group": "music",
                    "url": url,
                    "title": "v",
                    "text": "",
                    "published_at": None,
                    "market": "za",
                },
            ]
        ),
        m.pd.DataFrame(
            [
                {
                    "source": "youtube_scrape",
                    "platform": "youtube",
                    "query_group": m.SCRAPE_QUERY_GROUP,
                    "url": url,
                    "title": "v",
                    "text": "",
                    "published_at": None,
                    "market": "za",
                }
            ]
        ),
    ]
    enriched = m.pd.DataFrame(
        [
            {"topic_groups": ["music_amapiano"], "query_group": "music_amapiano"},
            {"topic_groups": [], "query_group": "other"},
        ]
    )
    telemetry = _bound_boundary_telemetry()
    with (
        patch.object(m, "insert_dataframe", side_effect=[2, 2]),
        patch.object(m, "enrich_dataframe", return_value=enriched),
        patch.object(m, "_aggregate_by_topic", return_value=({("za", "music_amapiano"): {}}, 1)),
    ):
        rows_raw, *_rest = m._ingest_market_frames(
            "za", frames, "rid", datetime(2026, 6, 2, tzinfo=UTC), telemetry
        )
    assert rows_raw == 2
    by_boundary = {}
    for record in telemetry.records:
        by_boundary.setdefault(record["boundary"], []).append(record)
    assert [r["collection_event_id"] for r in by_boundary["producer"]] == [
        "exec-1:za:socialcrawl:0:0",
        "exec-1:za:youtube:0:1",
        "exec-1:za:youtube_scrape:1:0",
    ]
    assert [(r["outcome"], r["reason_code"]) for r in by_boundary["dedup"]] == [
        ("admitted", None),
        ("admitted", None),
        ("rejected", "youtube_scrape_duplicate"),
    ]
    assert [r["identity_kind"] for r in by_boundary["producer"]] == [
        "native",
        "inferred",
        "inferred",
    ]
    assert by_boundary["producer"][0]["native_id"] == "post:1"
    assert [(r["outcome"], r["reason_code"]) for r in by_boundary["classification"]] == [
        ("admitted", None),
        ("rejected", "unclassified"),
    ]
    assert len(by_boundary["enrichment"]) == 2
    persisted = [r["source_row_id"] for r in by_boundary["enrichment"]]
    assert len(set(persisted)) == 2
    assert None not in persisted
    counts = {(n["unit"], n["market"]): n["value"] for n in telemetry.notes if n["kind"] == "count"}
    assert counts == {
        ("physical_persisted_rows", "za"): 2,
        ("topic_day_scores", "za"): 1,
    }


def test_ingest_market_frames_writes_raw_evidence_before_and_despite_telemetry(load_run_module):
    """A malformed link cannot take the market's raw rows with it: the inserts still
    run, every emission happens after the write it describes, and the boundary is
    marked unavailable with a named reason."""
    from src.analysis.open_intelligence.coverage_telemetry_sink import MemoryDispositionSink

    m = load_run_module
    frames = [
        m.pd.DataFrame(
            [
                {
                    "source": "rss",
                    "platform": "web",
                    "url": "http://[bad/x",
                    "title": "t",
                    "text": "x",
                    "published_at": None,
                    "market": "za",
                },
                {
                    "source": "rss",
                    "platform": "web",
                    "url": "https://news.example/a",
                    "title": "u",
                    "text": "y",
                    "published_at": None,
                    "market": "za",
                },
            ]
        )
    ]
    enriched = m.pd.DataFrame(
        [
            {"topic_groups": [], "query_group": "other"},
            {"topic_groups": ["news_politics"], "query_group": "news_politics"},
        ]
    )
    inserts = []

    def insert(_frame, table):
        inserts.append(table)
        return 2

    class OrderSink(MemoryDispositionSink):
        def emit(self, record):
            record = {**record, "inserts_before": tuple(inserts)}
            super().emit(record)

    telemetry = _bound_boundary_telemetry()
    telemetry._sink = OrderSink()
    with (
        patch.object(m, "insert_dataframe", side_effect=insert),
        patch.object(m, "enrich_dataframe", return_value=enriched),
        patch.object(m, "_aggregate_by_topic", return_value=({}, 1)),
    ):
        rows_raw, rows_enriched, *_rest = m._ingest_market_frames(
            "za", frames, "rid", datetime(2026, 6, 2, tzinfo=UTC), telemetry
        )
    assert (rows_raw, rows_enriched) == (2, 2)
    assert inserts == ["raw_content", "enriched_content"]
    records = telemetry.records
    assert [r["boundary"] for r in records] == ["producer", "dedup", "enrichment", "classification"]
    assert all("raw_content" in r["inserts_before"] for r in records)
    assert all(r["inserts_before"] == ("raw_content", "enriched_content") for r in records[2:])
    assert {r["source_row_id"] for r in records} == {records[0]["source_row_id"]}
    unavailable = [n for n in telemetry.notes if n["kind"] == "unavailable"]
    assert [(n["boundary"], n["market"], n["reason_code"]) for n in unavailable] == [
        ("producer", "za", "emission_failed"),
        ("dedup", "za", "emission_failed"),
        ("enrichment", "za", "collection_identity_missing"),
    ]


def test_ingest_market_frames_without_telemetry_is_unchanged(load_run_module):
    m = load_run_module
    frames = [m.pd.DataFrame([{"title": "t", "text": "x", "published_at": None, "market": "za"}])]
    enriched = m.pd.DataFrame(
        [{"topic_groups": ["music_amapiano"], "query_group": "music_amapiano"}]
    )
    with (
        patch.object(m, "insert_dataframe", side_effect=[1, 1]),
        patch.object(m, "enrich_dataframe", return_value=enriched),
        patch.object(m, "_aggregate_by_topic", return_value=({}, 0)),
    ):
        result = m._ingest_market_frames("za", frames, "rid", datetime(2026, 6, 2, tzinfo=UTC))
    assert result[:2] == (1, 1)


def test_collection_only_terminal_path_records_route_outcomes(load_run_module):
    m = load_run_module
    telemetry = _bound_boundary_telemetry()
    counts = {"za": {"rss": 3, "gdelt": 0}, "ng": {"rss": 0}}
    errors = [{"market": "za", "source": "gdelt", "error": "HTTP 503", "fatal": True}]
    m._record_collection_route_outcomes(telemetry, counts, errors)
    routes = {(n["market"], n["route"]): n for n in telemetry.notes if n["kind"] == "route"}
    assert routes[("za", "gdelt")]["reason_code"] == "fetch_failed"
    assert routes[("za", "rss")]["emissions"] == 3
    assert routes[("ng", "rss")]["reason_code"] is None
    assert m._record_collection_route_outcomes(None, counts, errors) is None
