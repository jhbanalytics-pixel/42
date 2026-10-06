"""Velocity scoring for composite trend scores.

Compares today's per-topic volume against a rolling baseline from the
trend_scores table. Wires into compute_trend_scores in the RSS pipeline
to replace the hardcoded velocity_score=0.0.

The scalar and batch entry points share the same formula and edge-case
handling. The batch helper exists to keep the pipeline at one BigQuery
call per run instead of one per (market, query_group) pair.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pandas as pd

from src.utils.bigquery import run_query
from src.utils.log_redactor import get_logger

logger = get_logger(__name__)


# A 5x spike over baseline saturates the score at 1.0. Tuned so a modest
# 2x lift reads as 0.25 (still interesting but not trending-worthy) and a
# 3x lift reads as 0.5. Revisit once we have real historical data to fit.
VELOCITY_SATURATION_DIVISOR = 4.0

# New-topic velocity is scaled by volume, not awarded a flat maximum on any
# positive count. A topic with no baseline (brand new or lapsed 14+ days) and
# today_count >= NEW_TOPIC_FULL_VOLUME earns the full ceiling; a thin novelty
# earns proportionally less. NEW_TOPIC_FULL_VOLUME is the live median
# item_count (30-day trend_scores pull, 2026-06-16). Rationale: ranking by a
# rate with no denominator must scale confidence by sample size; a 5-row new
# topic should not peg the largest signal (velocity, weight 0.20) to 1.0.
NEW_TOPIC_SCORE = 1.0
NO_SIGNAL_SCORE = 0.0
NEW_TOPIC_FULL_VOLUME = 75


def _new_topic_score(today_count: int) -> float:
    """Volume-scaled velocity for a topic with no usable baseline."""
    if today_count <= 0:
        return NO_SIGNAL_SCORE
    return round(min(NEW_TOPIC_SCORE, today_count / NEW_TOPIC_FULL_VOLUME), 4)


def _normalise(today_count: int, baseline_avg: float) -> float:
    """Apply the velocity formula to a single (today, baseline) pair."""
    if baseline_avg <= 0:
        return _new_topic_score(today_count)
    velocity_raw = today_count / baseline_avg
    if velocity_raw <= 1.0:
        return 0.0
    return min(1.0, (velocity_raw - 1.0) / VELOCITY_SATURATION_DIVISOR)


def compute_velocity_score(
    market: str,
    query_group: str,
    today_count: int,
    baseline_days: int = 14,
    *,
    query_runner: Callable[..., pd.DataFrame] | None = None,
    trend_date: date | None = None,
) -> float:
    """Velocity score for one (market, query_group) against its rolling baseline.

    Queries trend_scores for the prior `baseline_days` (exclusive of today).
    If no history exists, a topic with today_count > 0 returns 1.0 (brand new
    surge) and today_count == 0 returns 0.0.

    Prefer compute_velocity_scores_for_today for pipeline batches so we do
    not issue one query per (market, query_group) pair.
    """
    sql = """
        SELECT AVG(item_count) AS baseline_avg
        FROM `{project}.{dataset}.trend_scores`
        WHERE market = @market
          AND query_group = @query_group
          AND trend_date BETWEEN
              DATE_SUB(CURRENT_DATE(), INTERVAL @baseline_days DAY)
              AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
    """
    params = {
        "market": market,
        "query_group": query_group,
        "baseline_days": int(baseline_days),
    }
    if query_runner is not None and trend_date is None:
        raise ValueError("explicit velocity query requires trend_date")
    if trend_date is not None:
        if type(trend_date) is not date:
            raise ValueError("trend_date must be a date")
        sql = sql.replace("CURRENT_DATE()", "@trend_date")
        params["trend_date"] = trend_date
    runner = run_query if query_runner is None else query_runner
    df = runner(sql, params=params)

    if df.empty or df["baseline_avg"].isna().all():
        logger.debug(
            "No velocity baseline for market=%s query_group=%s, today=%d",
            market,
            query_group,
            today_count,
        )
        return _new_topic_score(today_count)

    baseline_avg = float(df["baseline_avg"].iloc[0] or 0.0)
    score = _normalise(today_count, baseline_avg)
    logger.debug(
        "Velocity market=%s query_group=%s today=%d baseline_avg=%.2f score=%.4f",
        market,
        query_group,
        today_count,
        baseline_avg,
        score,
    )
    return round(score, 4)


def compute_velocity_scores_for_today(
    market_counts: dict[tuple[str, str], dict[str, Any]],
    baseline_days: int = 14,
    *,
    query_runner: Callable[..., pd.DataFrame] | None = None,
    trend_date: date | None = None,
) -> dict[tuple[str, str], float]:
    """Batch velocity scores for every (market, query_group) in market_counts.

    Input shape matches what compute_trend_scores in scripts/run_rss_now.py
    builds: a dict keyed by (market, query_group) with per-group stats.
    Only the "item_count" field is read; other stats are ignored.

    Pulls the full baseline window for all markets/query_groups in ONE
    query and joins client-side. Returns a dict keyed by the same tuples
    as the input, with a velocity score for every input key including
    ones that have no historical rows.
    """
    if not market_counts:
        return {}

    sql = """
        SELECT market, query_group, AVG(item_count) AS baseline_avg
        FROM `{project}.{dataset}.trend_scores`
        WHERE trend_date BETWEEN
              DATE_SUB(CURRENT_DATE(), INTERVAL @baseline_days DAY)
              AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
        GROUP BY market, query_group
    """
    params = {"baseline_days": int(baseline_days)}
    if query_runner is not None and trend_date is None:
        raise ValueError("explicit velocity query requires trend_date")
    if trend_date is not None:
        if type(trend_date) is not date:
            raise ValueError("trend_date must be a date")
        sql = sql.replace("CURRENT_DATE()", "@trend_date")
        params["trend_date"] = trend_date
    runner = run_query if query_runner is None else query_runner
    df = runner(sql, params=params)

    baseline_lookup: dict[tuple[str, str], float] = {}
    if not df.empty:
        for _, row in df.iterrows():
            val = row["baseline_avg"]
            baseline = 0.0 if pd.isna(val) else float(val)
            baseline_lookup[(str(row["market"]), str(row["query_group"]))] = baseline

    scores: dict[tuple[str, str], float] = {}
    for key, stats in market_counts.items():
        # round(), not int(): item_count is fractional (1/N topic-split
        # weights) and is STORED via round() in run_rss_now.py. Using int()
        # here would truncate, so velocity could score against a today_count
        # one below the item_count shown in the same trend_scores row on a
        # half-fraction topic. round() keeps the velocity input and the
        # displayed count consistent.
        today_count = round(float(stats.get("item_count", 0) or 0))
        baseline_avg = baseline_lookup.get(key, 0.0)
        scores[key] = round(_normalise(today_count, baseline_avg), 4)

    logger.info(
        "Computed velocity for %d (market, query_group) pairs in one query",
        len(scores),
    )
    return scores


# Wave 1 momentum: 7-day and 30-day baselines computed alongside the live
# 14-day score in ONE batch query. Display-only (momentum pill); the 14-day
# composite velocity is untouched. velocity_score_7d / velocity_score_30d are
# the normalised velocity reads against the short and long windows; the
# 7-day median item_count is returned for the lifecycle classifier (the only
# extra history it needs beyond today's count and the velocity trajectory).
MOMENTUM_SHORT_DAYS = 7
MOMENTUM_LONG_DAYS = 30


def compute_velocity_windows_for_today(
    market_counts: dict[tuple[str, str], dict[str, Any]],
    short_days: int = MOMENTUM_SHORT_DAYS,
    long_days: int = MOMENTUM_LONG_DAYS,
    *,
    query_runner: Callable[..., pd.DataFrame] | None = None,
    trend_date: date | None = None,
) -> dict[tuple[str, str], dict[str, float | None]]:
    """Per-(market, query_group) 7d/30d velocity reads plus 7d median item_count.

    Display-only momentum companion to compute_velocity_scores_for_today. Pulls
    both window baselines AND the 7-day median item_count in ONE query (one
    pass over the same trend_scores window the 14-day baseline already scans)
    and joins client-side. The 30-day window is the outer bound, so every
    needed aggregate fits a single ``trend_date >= long_days`` scan with
    conditional AVG/median per inner window.

    Returns a dict keyed by the same tuples as the input. Each value is
    ``{"velocity_score_7d": float, "velocity_score_30d": float,
    "median_item_count_7d": float | None}``. Keys with no history get
    new-topic-scaled velocity (same _normalise path as the 14-day score) and a
    None median.
    """
    if not market_counts:
        return {}

    # APPROX_QUANTILES over the short window gives the 7-day median item_count
    # the lifecycle classifier compares today's volume against. AVG per window
    # gives the two baselines. One scan of the 30-day window covers all three.
    sql = """
        SELECT
          market,
          query_group,
          AVG(IF(
            trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL @short_days DAY),
            item_count, NULL
          )) AS baseline_avg_short,
          AVG(item_count) AS baseline_avg_long,
          APPROX_QUANTILES(
            IF(
              trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL @short_days DAY),
              item_count, NULL
            ), 2
          )[OFFSET(1)] AS median_item_count_short
        FROM `{project}.{dataset}.trend_scores`
        WHERE trend_date BETWEEN
              DATE_SUB(CURRENT_DATE(), INTERVAL @long_days DAY)
              AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
        GROUP BY market, query_group
    """
    params = {"short_days": int(short_days), "long_days": int(long_days)}
    if query_runner is not None and trend_date is None:
        raise ValueError("explicit velocity query requires trend_date")
    if trend_date is not None:
        if type(trend_date) is not date:
            raise ValueError("trend_date must be a date")
        sql = sql.replace("CURRENT_DATE()", "@trend_date")
        params["trend_date"] = trend_date
    runner = run_query if query_runner is None else query_runner
    df = runner(sql, params=params)

    short_lookup: dict[tuple[str, str], float] = {}
    long_lookup: dict[tuple[str, str], float] = {}
    median_lookup: dict[tuple[str, str], float | None] = {}
    if not df.empty:
        for _, row in df.iterrows():
            key = (str(row["market"]), str(row["query_group"]))
            short_raw = row["baseline_avg_short"]
            long_raw = row["baseline_avg_long"]
            short_lookup[key] = 0.0 if pd.isna(short_raw) else float(short_raw)
            long_lookup[key] = 0.0 if pd.isna(long_raw) else float(long_raw)
            median_raw = row["median_item_count_short"]
            median_lookup[key] = None if pd.isna(median_raw) else float(median_raw)

    out: dict[tuple[str, str], dict[str, float | None]] = {}
    for key, stats in market_counts.items():
        # Same round() (not int()) the 14-day path uses so the window scores
        # are consistent with the stored item_count on half-fraction topics.
        today_count = round(float(stats.get("item_count", 0) or 0))
        out[key] = {
            "velocity_score_7d": round(_normalise(today_count, short_lookup.get(key, 0.0)), 4),
            "velocity_score_30d": round(_normalise(today_count, long_lookup.get(key, 0.0)), 4),
            "median_item_count_7d": median_lookup.get(key),
        }

    logger.info(
        "Computed 7d/30d momentum windows for %d (market, query_group) pairs in one query",
        len(out),
    )
    return out
