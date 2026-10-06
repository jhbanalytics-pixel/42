from __future__ import annotations

import importlib
import importlib.util
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import funded_lane_runtime
from src.analysis.open_intelligence.funded_lane import WAVE1_PHASE_CAPS, WAVE1_STAGE
from src.analysis.open_intelligence.funded_lane_runtime import Wave1PhaseUsage
from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value
from src.contracts.bigquery_ddl import parse_table_ddl

from tests.unit.test_funded_lane_runtime import NOW, prepare
from tests.unit.test_source_wave1_contract import _wave1_capability

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "infra" / "bigquery_schemas" / "socialcrawl_funded_terminal_events_v1.sql"
VIEW = ROOT / "infra" / "bigquery_views" / "v_socialcrawl_funded_budget_v2.sql"

EXPECTED_FIELDS = (
    ("terminal_contract_version", "STRING", "REQUIRED"),
    ("terminal_id", "STRING", "REQUIRED"),
    ("execution_id", "STRING", "REQUIRED"),
    ("run_id", "STRING", "REQUIRED"),
    ("credential_lane", "STRING", "REQUIRED"),
    ("funding_account", "STRING", "REQUIRED"),
    ("recorded_at", "TIMESTAMP", "REQUIRED"),
    ("balance_read_status", "STRING", "REQUIRED"),
    ("last_measured_balance", "NUMERIC", "REQUIRED"),
    ("last_balance_observed_at", "TIMESTAMP", "REQUIRED"),
    ("authorized_quoted_debit", "NUMERIC", "REQUIRED"),
    ("vendor_reported_debit", "NUMERIC", "REQUIRED"),
    ("overage_debit", "NUMERIC", "REQUIRED"),
    ("ledger_debit", "NUMERIC", "REQUIRED"),
    ("attribution_state", "STRING", "REQUIRED"),
    ("kill_state", "STRING", "REQUIRED"),
    ("reason_codes", "STRING", "REPEATED"),
    ("source_sha", "STRING", "REQUIRED"),
    ("manifest_sha256", "STRING", "REQUIRED"),
    ("created_at", "TIMESTAMP", "REQUIRED"),
)


@pytest.fixture(autouse=True)
def _wave1_identity_from_fake_catalog(monkeypatch):
    # The fakes carry the approved digests directly; the real helper is proven
    # against the committed catalog in test_source_lab_inventory.
    monkeypatch.setattr(
        funded_lane_runtime,
        "_wave1_identity",
        lambda catalog: (catalog.catalog_digest, catalog.normalized_content_digest),
    )


def _terminal_api():
    return importlib.import_module("src.analysis.open_intelligence.funded_control_terminal")


def _event(**overrides):
    values = {
        "execution_id": "execution_001",
        "run_id": "run_001",
        "recorded_at": NOW + timedelta(minutes=2),
        "balance_read_status": "unavailable",
        "last_measured_balance": Decimal("250100"),
        "last_balance_observed_at": NOW,
        "authorized_quoted_debit": Decimal("1"),
        "vendor_reported_debit": Decimal("3"),
        "attribution_state": "gap_detected",
        "reason_codes": ("post_balance_unavailable", "vendor_overage"),
        "source_sha": "a" * 40,
        "manifest_sha256": "b" * 64,
    }
    values.update(overrides)
    return _terminal_api().build_funded_terminal_event(**values)


def _active_results():
    active = evaluate_wave1_source_value(
        unique_observations=1,
        marginal_candidates=1,
        marginal_evidence=1,
    )
    routes = {
        "/v1/tiktok/song",
        "/v1/tiktok/song/videos",
        "/v1/instagram/music/trending",
        "/v1/instagram/audio/reels",
        "/v1/instagram/search/reels",
        "/v1/youtube/shorts/trending",
        "/v1/youtube/video/comments",
        "/v1/reddit/post/comments",
    }
    return dict.fromkeys(routes, active)


def _gdelt_proof():
    return SimpleNamespace(
        query_job_ids=(("events", "job-events"), ("gcam", "job-gcam")),
        query_bytes_processed=(("events", 100), ("gcam", 200)),
        persistence=SimpleNamespace(complete=True),
    )


def test_terminal_schema_identity_and_budget_v2_contract_are_exact() -> None:
    assert importlib.util.find_spec("src.analysis.open_intelligence.funded_control_terminal")
    terminal_api = _terminal_api()
    parsed = parse_table_ddl(
        SCHEMA.read_text(encoding="utf-8")
        .replace("{project}", "ogilvy-trends-v2")
        .replace("{dataset}", "trends_v2_staging_funded")
    )
    assert (
        tuple((name, kind, mode) for name, kind, mode, _, _ in parsed["fields"]) == EXPECTED_FIELDS
    )
    assert parsed["partition"] == "DATE(recorded_at)"
    assert parsed["cluster"] == ("credential_lane", "kill_state", "execution_id")

    event = _event()
    assert event.terminal_contract_version == terminal_api.FUNDED_TERMINAL_CONTRACT_VERSION
    assert event.terminal_id.startswith("scte_")
    assert len(event.terminal_id) == 69
    assert event.overage_debit == Decimal("2")
    assert event.ledger_debit == Decimal("3")
    assert _event() == event
    assert _event(vendor_reported_debit=Decimal("4")).terminal_id != event.terminal_id
    with pytest.raises(ValueError):
        replace(event, last_measured_balance=Decimal("0"))
    with pytest.raises(ValueError):
        replace(event, reason_codes=())
    with pytest.raises(ValueError):
        replace(event, overage_debit=Decimal("0"))

    sql = VIEW.read_text(encoding="utf-8")
    assert "v_socialcrawl_funded_budget_v1" in sql
    assert "socialcrawl_funded_terminal_events_v1" in sql
    assert "v_socialcrawl_funded_budget_v2" in sql
    assert "terminal_recorded_at" in sql
    assert "ROW_NUMBER() OVER" in sql
    assert "terminal.recorded_at > control.recorded_at" in sql
    assert "state = 'ready'" not in sql
    assert "jhb_core" not in sql


def test_unavailable_post_balance_writes_one_killed_terminal_without_inventing_balance() -> None:
    terminals = []
    runtime, _written, values = prepare(
        stage_name=WAVE1_STAGE,
        execution_capability=_wave1_capability(),
        terminal_writer=terminals.append,
    )
    for phase in WAVE1_PHASE_CAPS:
        runtime.phase_close(Wave1PhaseUsage(None, phase, 0, 0, 0))

    receipt = runtime.close_unavailable(recorded_at=NOW + timedelta(minutes=2))

    assert receipt.balance_read_status == "unavailable"
    assert receipt.balance_delta is None
    assert receipt.terminal_id == terminals[0].terminal_id
    assert len(terminals) == 1
    terminal = terminals[0]
    assert terminal.kill_state == "killed"
    assert terminal.reason_codes == ("post_balance_unavailable",)
    assert terminal.last_measured_balance == Decimal("250100")
    assert terminal.last_balance_observed_at == NOW
    assert terminal.authorized_quoted_debit == Decimal("0")
    assert terminal.vendor_reported_debit == Decimal("0")
    assert terminal.ledger_debit == Decimal("0")
    assert [control.kill_state for control in values["_controls"]] == ["not_tested"]


def test_vendor_overage_writes_killed_terminal_and_preserves_quote_and_known_debit() -> None:
    terminals = []
    runtime, _written, values = prepare(
        stage_name=WAVE1_STAGE,
        execution_capability=_wave1_capability(),
        terminal_writer=terminals.append,
    )
    runtime._source_value_writer = lambda _values: None
    for phase in WAVE1_PHASE_CAPS:
        runtime.phase_close(
            Wave1PhaseUsage(
                None,
                phase,
                1 if phase == "wave1_tiktok_sound" else 0,
                3 if phase == "wave1_tiktok_sound" else 0,
                3 if phase == "wave1_tiktok_sound" else 0,
                authorized_quoted_debit=1 if phase == "wave1_tiktok_sound" else 0,
            )
        )

    receipt = runtime.close(
        post_balance=Decimal("250097"),
        recorded_at=NOW + timedelta(minutes=2),
        source_value_results=_active_results(),
        gdelt_runtime_proof=_gdelt_proof(),
    )

    assert receipt.balance_read_status == "measured"
    assert receipt.terminal_id == terminals[0].terminal_id
    terminal = terminals[0]
    assert terminal.kill_state == "killed"
    assert terminal.reason_codes == ("vendor_overage",)
    assert terminal.authorized_quoted_debit == Decimal("1")
    assert terminal.vendor_reported_debit == Decimal("3")
    assert terminal.overage_debit == Decimal("2")
    assert terminal.ledger_debit == Decimal("3")
    assert [control.kill_state for control in values["_controls"]] == ["not_tested"]


def test_terminal_writer_dry_runs_inserts_and_reads_back_exactly() -> None:
    from src.analysis.open_intelligence.funded_control_terminal_persistence import (
        persist_funded_terminal_event,
    )

    from tests.unit.test_funded_control_read_model import _ControlClient

    event = _event()
    client = _ControlClient(event)
    result = persist_funded_terminal_event(
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging_funded",
        client=client,
        event=event,
    )

    assert result.terminal_id == event.terminal_id
    assert result.inserted_count == 1
    assert result.unchanged_count == 0
    assert len(client.calls) == 3
    assert client.calls[0][2].dry_run is True
    assert "socialcrawl_funded_terminal_events_v1" in client.calls[1][0]
    assert "UPDATE " not in client.calls[1][0]
    assert "DELETE " not in client.calls[1][0]
    assert "WHERE terminal_id = @terminal_id" in client.calls[2][0]


def test_terminal_writer_reconciles_an_exact_row_after_transaction_response_loss() -> None:
    from src.analysis.open_intelligence.funded_control_terminal_persistence import (
        persist_funded_terminal_event,
    )

    from tests.unit.test_funded_control_read_model import _ControlClient

    event = _event()
    client = _ControlClient(event, response_loss=True)

    result = persist_funded_terminal_event(
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging_funded",
        client=client,
        event=event,
    )

    assert result.terminal_id == event.terminal_id
    assert result.inserted_count == 0
    assert result.unchanged_count == 1
    assert len(client.calls) == 3
    assert "WHERE terminal_id = @terminal_id" in client.calls[2][0]
