"""Unit tests for scripts/migrations/backfill_topic_trend_scores.py.

Covers pure helpers only: date iteration, classify_dataframe, build_score_rows.
The BQ-facing wrappers (run_query, merge_dataframe) are exercised by the
``--dry-run`` CLI mode in production.
"""

from __future__ import annotations

import importlib
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture
def backfill_module():
    """Import backfill module fresh and expose run_rss_now on sys.path."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    scripts_dir = repo_root / "scripts"
    migrations_dir = scripts_dir / "migrations"

    for p in (str(repo_root), str(scripts_dir), str(migrations_dir)):
        if p not in sys.path:
            sys.path.insert(0, p)

    if "backfill_topic_trend_scores" in sys.modules:
        del sys.modules["backfill_topic_trend_scores"]

    return importlib.import_module("backfill_topic_trend_scores")


def test_list_target_days_excludes_today_and_orders_chronologically(backfill_module):
    days = backfill_module.list_target_days(days=3, today=date(2026, 4, 25))
    assert days == [date(2026, 4, 22), date(2026, 4, 23), date(2026, 4, 24)]


def test_list_target_days_returns_empty_when_zero(backfill_module):
    assert backfill_module.list_target_days(days=0, today=date(2026, 4, 25)) == []


def test_classify_dataframe_adds_topic_groups_per_row(backfill_module):
    df = pd.DataFrame(
        [
            {
                "market": "za",
                "title": "Amapiano dance challenge taking over",
                "text": "",
                "hashtags": "",
            },
            {
                "market": "ng",
                "title": "New Burna Boy track drops",
                "text": "",
                "hashtags": "",
            },
            {
                "market": "ke",
                "title": "Pelle glow capelli perfetti",
                "text": "",
                "hashtags": "",
            },
        ]
    )
    out = backfill_module.classify_dataframe(df)
    assert "topic_groups" in out.columns
    assert "music_amapiano" in out.iloc[0]["topic_groups"]
    assert "music_afrobeats" in out.iloc[1]["topic_groups"]
    assert out.iloc[2]["topic_groups"] == []


def test_classify_dataframe_does_not_mutate_input(backfill_module):
    df = pd.DataFrame(
        [
            {
                "market": "za",
                "title": "amapiano",
                "text": "",
                "hashtags": "",
            }
        ]
    )
    _ = backfill_module.classify_dataframe(df)
    assert "topic_groups" not in df.columns


def test_build_score_rows_empty_dataframe_returns_empty(backfill_module):
    df = pd.DataFrame(columns=["market", "title", "text", "hashtags", "topic_groups"])
    rows = backfill_module.build_score_rows(df, day=date(2026, 4, 24))
    assert rows == []


def test_build_score_rows_velocity_zero_for_backfill(backfill_module):
    """Backfill rows must carry velocity_score=0 so they don't seed themselves."""
    df = pd.DataFrame(
        [
            {
                "market": "za",
                "source": "rss",
                "title": "amapiano dance challenge",
                "text": "",
                "hashtags": "",
                "author_handle_norm": "a",
                "regional_score": 0.5,
                "genz_score": 0.0,
                "slang_score": 0.0,
                "creator_watchlist_score": 0.0,
                "search_velocity_score": 0.0,
                "tone_avg": None,
            }
        ]
    )
    df_classified = backfill_module.classify_dataframe(df)
    rows = backfill_module.build_score_rows(df_classified, day=date(2026, 4, 20))

    assert len(rows) >= 1
    for row in rows:
        assert row["velocity_score"] == 0.0
        assert row["trend_date"] == date(2026, 4, 20)
        assert row["market"] == "za"
        # query_group must be a topic name, not a source name
        assert not row["query_group"].startswith("tiktok_")
        assert not row["query_group"].startswith("brand24_")
