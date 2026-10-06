"""Unit tests for the reconcile shadow digest reporter.

The bq client is mocked; NO live calls. Two paths matter: the empty / missing-
table path must print the no-data line and exit 0 without raising, and the
populated path must summarise the ledger and the reconcile actions.
"""

from __future__ import annotations

import datetime

import pytest
import scripts.reconcile_shadow_digest as digest
from google.api_core.exceptions import NotFound

TREND_DATE = datetime.date(2026, 6, 26)


class _FakeQueryJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return list(self._rows)


class FakeClient:
    """Stand-in for the bq client.

    Routes each query to a per-table row list keyed on a substring of the SQL.
    A table whose name maps to the NotFound sentinel raises NotFound (the pre-
    activation state). project is a plain attribute the SQL f-strings read.
    """

    project = "test-project"

    def __init__(self, tables):
        self._tables = tables
        self.queries: list[str] = []

    def query(self, sql, job_config=None, **kwargs):
        self.queries.append(sql)
        for token, rows in self._tables.items():
            if token in sql:
                if rows is NotFound:
                    raise NotFound("table not found")
                return _FakeQueryJob(rows)
        return _FakeQueryJob([])


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(digest, "get_client", lambda: client)
    monkeypatch.setattr(digest, "get_dataset", lambda: "trends_v2")
    monkeypatch.setattr(
        "sys.argv", ["reconcile_shadow_digest.py", "--date", TREND_DATE.isoformat()]
    )


def test_missing_tables_prints_no_data_and_exits_zero(monkeypatch, capsys):
    """Pre-activation: both tables raise NotFound. No-data line, exit 0, no raise."""
    client = FakeClient({"event_ledger": NotFound, "reconcile_actions": NotFound})
    _patch_client(monkeypatch, client)

    rc = digest.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert "no shadow data" in out
    assert str(TREND_DATE) in out


def test_empty_tables_prints_no_data_and_exits_zero(monkeypatch, capsys):
    """Tables exist but are empty (quiet day). Same no-data line, exit 0."""
    client = FakeClient({"event_ledger": [], "reconcile_actions": []})
    _patch_client(monkeypatch, client)

    rc = digest.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert "no shadow data" in out


def test_populated_summarises_ledger_and_actions(monkeypatch, capsys):
    """A populated day prints ledger events and reconcile action counts."""
    ledger_rows = [
        {
            "market": "za",
            "entity_key": "south korea",
            "event_kind": "entity",
            "state_label": "resolved",
            "state_text": "South Africa beat South Korea 2-0",
            "confidence": 0.82,
            "resolved_by": "gemini",
            "corroborating_sources": ["news", "search"],
        },
        {
            "market": "ng",
            "entity_key": "super eagles",
            "event_kind": "entity",
            "state_label": "scheduled",
            "state_text": "Super Eagles to face Ghana this weekend",
            "confidence": 0.5,
            "resolved_by": "deterministic",
            "corroborating_sources": ["news"],
        },
    ]
    action_rows = [
        {
            "market": "za",
            "action": "stale",
            "claim_before": "South Africa face South Korea this weekend",
            "claim_after": "South Africa beat South Korea 2-0",
            "matched_entity_key": "south korea",
            "receipt_ids": ["news", "search"],
            "confidence_tier": "high",
        },
        {
            "market": "ng",
            "action": "no_match",
            "claim_before": "Some unrelated claim",
            "claim_after": "Some unrelated claim",
            "matched_entity_key": "",
            "receipt_ids": [],
            "confidence_tier": "none",
        },
    ]
    client = FakeClient({"event_ledger": ledger_rows, "reconcile_actions": action_rows})
    _patch_client(monkeypatch, client)

    rc = digest.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert "no shadow data" not in out
    # Ledger surfaced.
    assert "ledger events: 1" in out  # one ledger row per market (za, ng)
    assert "South Africa beat South Korea 2-0" in out
    # Reconcile counts surfaced.
    assert "claims checked: 1" in out
    assert "stale 1" in out
    # The corrected claim and its receipts are eyeballed.
    assert "corrected:" in out
    assert "news" in out
    assert "search" in out
    # Totals line.
    assert "2 ledger events, 2 reconcile actions" in out
    # Phase 4 prep: state coverage (resolved/scheduled/unknown), the metric
    # that tracks the observation-window's blocker to promotion.
    assert "state coverage" in out
    assert "resolved=1 scheduled=0 unknown=0" in out  # za: one resolved row
    assert "resolved=0 scheduled=1 unknown=0" in out  # ng: one scheduled row


def test_other_read_error_degrades_to_no_data(monkeypatch, capsys):
    """A non-NotFound read failure degrades to no-data, never raises."""

    class BoomClient(FakeClient):
        def query(self, sql, job_config=None, **kwargs):
            raise RuntimeError("permission denied")

    client = BoomClient({})
    _patch_client(monkeypatch, client)

    rc = digest.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert "no shadow data" in out
