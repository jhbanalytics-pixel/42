"""Unit tests for the gemini_usage backfill migration's SQL builders.

The watchdog costs off the ledger alone, so the three consumers that used to be
summed off their own token columns need their history folded in, or the first
run after deploy reports near-zero for them until a full window of cron has run.

The builders are pure and tested here; the BQ execution is validated by the
--dry-run then --apply on the live dataset (the BQ-RPC SDK segfaults on the
Windows runner, per the sibling suites).
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.migrations.backfill_gemini_usage import (
    SOURCES,
    build_delete_sql,
    build_insert_sql,
    source_for,
)

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_dev"
START = date(2026, 7, 25)
END = date(2026, 8, 24)


def _sql(consumer: str) -> str:
    return build_insert_sql(source_for(consumer), PROJECT, DATASET, START, END)


def test_every_backfilled_consumer_is_a_declared_ledger_consumer():
    from src.utils.gemini_usage import USAGE_CONSUMERS

    assert {s.consumer for s in SOURCES} <= set(USAGE_CONSUMERS)
    # The two stages that always wrote to the ledger have no table to backfill
    # from, and reconcile has no token columns to recover, so history for those
    # three starts where their writers did.
    assert {s.consumer for s in SOURCES} == {
        "trend_analysis",
        "daily_summary",
        "seed_insights",
    }


def test_seed_insights_takes_one_call_not_one_per_seed():
    """seed_insights stamps ONE call's tokens onto EVERY seed row it writes.

    A SUM over that table prices a single call once per seed: measured 3.0x
    over 2026-07-25..08-24 (533,373 prompt tokens reported against a true
    177,791). That is the over-count the old watchdog carried, and the backfill
    must not reproduce it in the new ledger.
    """
    sql = _sql("seed_insights")
    assert "ANY_VALUE(prompt_tokens)" in sql
    assert "ANY_VALUE(completion_tokens)" in sql
    assert "SUM(prompt_tokens)" not in sql
    # One Gemini call per (trend_date, model), whatever the seed count.
    assert "1 AS calls" in sql


def test_trend_analysis_sums_because_one_row_is_one_call():
    sql = _sql("trend_analysis")
    assert "SUM(prompt_tokens)" in sql
    assert "SUM(completion_tokens)" in sql
    assert "COUNT(*) AS calls" in sql
    # trend_analysis is the one source with a real per-market column.
    assert "market AS market" in sql
    assert "GROUP BY day, market, gemini_model" in sql


def test_cross_market_sources_write_a_null_market():
    # daily_summary and seed_insights carry a markets ARRAY, not one market.
    # A made-up market value would split their spend across rows that never
    # existed.
    for consumer in ("daily_summary", "seed_insights"):
        sql = _sql(consumer)
        assert "CAST(NULL AS STRING) AS market" in sql
        assert "GROUP BY day, gemini_model" in sql


def test_insert_is_bounded_on_both_ends_and_never_row_capped():
    sql = _sql("trend_analysis")
    assert "DATE '2026-07-25'" in sql
    assert "DATE '2026-08-24'" in sql
    # An open upper bound would sweep in rows the cron is writing right now and
    # double them against the live writer.
    assert sql.count("trend_date >=") == 1
    assert sql.count("trend_date <=") == 1
    assert "LIMIT" not in sql.upper()


def test_insert_targets_the_ledger_and_names_its_consumer():
    sql = _sql("daily_summary")
    assert f"`{PROJECT}.{DATASET}.gemini_usage`" in sql
    assert "'daily_summary' AS consumer" in sql
    assert "GENERATE_UUID() AS usage_id" in sql


def test_delete_scopes_to_the_backfilled_consumers_and_the_window():
    sql = build_delete_sql(PROJECT, DATASET, ["trend_analysis"], START, END)
    # Re-running the backfill must replace its own rows, never the rows the
    # live writers put there for other consumers.
    assert "DELETE FROM" in sql
    assert "consumer IN ('trend_analysis')" in sql
    assert "DATE '2026-07-25'" in sql
    assert "DATE '2026-08-24'" in sql
    assert "driving_hashtags" not in sql


def test_delete_of_no_consumers_is_refused():
    # An unscoped DELETE over the ledger would wipe the live writers' rows.
    import pytest

    with pytest.raises(ValueError):
        build_delete_sql(PROJECT, DATASET, [], START, END)
