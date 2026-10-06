"""Unit tests for scripts/ops/regen_daily_summary.py.

Pure-function tests; BigQuery and generate_daily_summary are patched. The BQ
mock returns a fresh result set per .query() call in call order: briefs first,
then scores.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mod():
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "scripts" / "ops" / "regen_daily_summary.py"
    spec = importlib.util.spec_from_file_location("regen_daily_summary", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _brief_row(market, query_group, status_tag, trend_synthesis, cultural_context):
    return SimpleNamespace(
        market=market,
        query_group=query_group,
        status_tag=status_tag,
        trend_synthesis=trend_synthesis,
        cultural_context=cultural_context,
    )


def _score_row(market, query_group, trend_score, velocity_score, item_count, seed_score=0.0):
    return SimpleNamespace(
        market=market,
        query_group=query_group,
        trend_score=trend_score,
        velocity_score=velocity_score,
        item_count=item_count,
        seed_score=seed_score,
    )


def _client_with_results(result_sets):
    """Build a fake bq client. .query(...).result() yields each list in order."""
    client = MagicMock()
    client.project = "test-project"

    def query_side_effect(*args, **kwargs):
        rows = result_sets.pop(0)
        job = MagicMock()
        job.result.return_value = rows
        return job

    client.query.side_effect = query_side_effect
    return client


def _fake_summary():
    return SimpleNamespace(
        through_line="A through line",
        summary_text="Summary body text",
        call_to_action="Do the thing",
        brief_count=2,
        key_topics=["a", "b"],
        rising_topics=["c"],
        prompt_tokens=1200,
        completion_tokens=800,
    )


def test_happy_path(mod):
    briefs = [
        _brief_row("ZA", "music_amapiano", "Rising", "synth one", "ctx one"),
        _brief_row("NG", "fashion_ankara", "Building", "synth two", "ctx two"),
    ]
    scores = [
        _score_row("ZA", "music_amapiano", 0.8, 0.5, 12, seed_score=0.91),
        _score_row("NG", "fashion_ankara", 0.6, 0.3, 7),
    ]
    client = _client_with_results([briefs, scores])

    with (
        patch.object(mod, "get_client", return_value=client),
        patch.object(mod, "get_dataset", return_value="trends"),
        patch.object(mod, "generate_daily_summary", return_value=_fake_summary()) as gds,
        patch.dict(os.environ, {"TREND_DATE_INPUT": "2026-06-28"}, clear=False),
    ):
        rc = mod.main()

    assert rc == 0
    gds.assert_called_once()
    assert gds.call_args.kwargs["force"] is True
    # seed_score must reach the summary input so a regen keeps the seed
    # recommendation the live cron produces (parity with run_rss_now).
    scores_by_topic = gds.call_args.kwargs["trend_scores_by_topic"]
    assert scores_by_topic[("ZA", "music_amapiano")]["seed_score"] == 0.91


def test_regen_scores_query_selects_seed_score(mod):
    import inspect

    src = inspect.getsource(mod.main)
    assert "seed_score" in src


def test_no_briefs_exits_2(mod):
    client = _client_with_results([[]])  # briefs query yields empty

    with (
        patch.object(mod, "get_client", return_value=client),
        patch.object(mod, "get_dataset", return_value="trends"),
        patch.object(mod, "generate_daily_summary") as gds,
        patch.dict(os.environ, {"TREND_DATE_INPUT": "2026-06-28"}, clear=False),
    ):
        rc = mod.main()

    assert rc == 2
    gds.assert_not_called()


def test_summary_none_exits_3(mod):
    briefs = [_brief_row("ZA", "music_amapiano", "Rising", "synth", "ctx")]
    scores = [_score_row("ZA", "music_amapiano", 0.8, 0.5, 12)]
    client = _client_with_results([briefs, scores])

    with (
        patch.object(mod, "get_client", return_value=client),
        patch.object(mod, "get_dataset", return_value="trends"),
        patch.object(mod, "generate_daily_summary", return_value=None),
        patch.dict(os.environ, {"TREND_DATE_INPUT": "2026-06-28"}, clear=False),
    ):
        rc = mod.main()

    assert rc == 3


def test_regen_briefs_query_dedupes_latest_row(mod):
    source = (
        Path(__file__).resolve().parents[2] / "scripts" / "ops" / "regen_daily_summary.py"
    ).read_text(encoding="utf-8")
    assert "QUALIFY ROW_NUMBER()" in source
    assert "ORDER BY analyzed_at DESC" in source
