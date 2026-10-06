"""Unit tests for scripts/ops/resend_email.py (read-only email resend).

The module loads from scripts/ops via the same sys.path + importlib pattern as
test_wave1_engine_core.py. get_client, get_dataset, send_daily_digest, and the
shared brief loader are patched. The fake BQ client serves rows in call order:
trend_scores, daily_summary, pipeline_runs stats (the briefs come from the
patched load_briefs_by_topic_from_bq, tested separately in test_brief_loader.py).
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def resend_module():
    sys.path.insert(0, "scripts")
    import ops.resend_email as resend_email  # type: ignore

    importlib.reload(resend_email)
    return resend_email


class ScoreRow:
    """trend_scores row: read via dict(r.items())."""

    def __init__(self, data: dict):
        self._data = data

    def items(self):
        return self._data.items()


class AttrRow:
    """summary / stats row: read via attribute access."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _summary_row(**overrides) -> AttrRow:
    base = {
        "summary_text": "Today's read.",
        "through_line": "One clear story across the markets today.",
        "call_to_action": "Seed it.",
        "key_topics": ["amapiano"],
        "rising_topics": ["afrobeats"],
        "seed_recommend": "amapiano",
    }
    base.update(overrides)
    return AttrRow(**base)


def _stats_row(**overrides) -> AttrRow:
    base = {"raw_rows": 1000, "scored_rows": 80, "unclassified_rows": 200}
    base.update(overrides)
    return AttrRow(**base)


def _make_client(score_rows, summary_rows, stats_rows):
    """A fake BQ client whose .query(...).result() returns the staged row lists
    in call order: trend_scores, daily_summary, pipeline_runs. The briefs query
    now lives in the patched loader, so it is not served here."""
    results = [score_rows, summary_rows, stats_rows]

    def query(sql, job_config=None):
        rows = results.pop(0)
        job = MagicMock()
        job.result.return_value = rows
        return job

    client = MagicMock()
    client.project = "ogilvy-trends-v2"
    client.query.side_effect = query
    return client


def _briefs_dict():
    return {
        ("za", "music_amapiano"): {
            "market": "za",
            "query_group": "music_amapiano",
            "topic": "music_amapiano",
            "trend_score": 0.8,
            "headline": "Amapiano surge",
            "status_tag": "Rising",
        }
    }


def test_happy_path_returns_zero_and_sends(resend_module, monkeypatch):
    monkeypatch.setenv("TREND_DATE_INPUT", "2026-06-28")
    client = _make_client(
        [ScoreRow({"market": "za", "query_group": "music_amapiano", "trend_score": 0.8})],
        [_summary_row()],
        [_stats_row()],
    )
    send = MagicMock(return_value=(1, 2, 3, "sent"))
    with (
        patch.object(resend_module, "get_client", return_value=client),
        patch.object(resend_module, "get_dataset", return_value="trends"),
        patch.object(resend_module, "load_briefs_by_topic_from_bq", return_value=_briefs_dict()),
        patch.object(resend_module, "send_daily_digest", send),
    ):
        rc = resend_module.main()

    assert rc == 0
    send.assert_called_once()
    briefs = send.call_args.kwargs["briefs_by_topic"]
    assert ("za", "music_amapiano") in briefs


def test_no_score_rows_returns_two_no_send(resend_module, monkeypatch):
    monkeypatch.setenv("TREND_DATE_INPUT", "2026-06-28")
    client = _make_client([], [], [])
    send = MagicMock(return_value=(1, 2, 3, "sent"))
    with (
        patch.object(resend_module, "get_client", return_value=client),
        patch.object(resend_module, "get_dataset", return_value="trends"),
        patch.object(resend_module, "load_briefs_by_topic_from_bq", return_value={}),
        patch.object(resend_module, "send_daily_digest", send),
    ):
        rc = resend_module.main()

    assert rc == 2
    send.assert_not_called()


def test_skipped_status_returns_three(resend_module, monkeypatch):
    monkeypatch.setenv("TREND_DATE_INPUT", "2026-06-28")
    client = _make_client(
        [ScoreRow({"market": "za", "query_group": "music_amapiano", "trend_score": 0.8})],
        [_summary_row()],
        [_stats_row()],
    )
    send = MagicMock(return_value=(0, 0, 0, "skipped"))
    with (
        patch.object(resend_module, "get_client", return_value=client),
        patch.object(resend_module, "get_dataset", return_value="trends"),
        patch.object(resend_module, "load_briefs_by_topic_from_bq", return_value=_briefs_dict()),
        patch.object(resend_module, "send_daily_digest", send),
    ):
        rc = resend_module.main()

    assert rc == 3
