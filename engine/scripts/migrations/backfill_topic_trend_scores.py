"""Backfill topic-level trend_scores for the past N days.

Why this exists
---------------
The topic-clustering refactor (commit 3fe15ef) was a hard cutover. Today's
``trend_scores`` rows carry topic-level ``query_group`` keys
(music_amapiano, politics_maandamano, etc.); rows older than the cutover
carry source-level keys (tiktok_hashtag, brand24_topics, news, etc.).

``src/scoring/velocity.py`` reads a 14-day rolling baseline from
``trend_scores`` keyed on (market, query_group). Because old and new keys
never join, every topic_group hits the empty-baseline branch every day and
returns ``NEW_TOPIC_SCORE = 1.0`` for the velocity component. That
saturates the 0.17 velocity weight permanently for the first 14 days
post-cutover.

This script reads historical ``enriched_content`` for each day in the
backfill window, re-runs the pure topic classifier
(``src.enrichment.topic_classifier``) on each row, aggregates per
``(market, topic_group)`` via the same ``_aggregate_by_topic`` function
the daily pipeline uses, runs ``compute_trend_scores`` with an empty
velocity input (so the backfill rows themselves get
``velocity_score = 0``), and MERGEs the resulting topic-level rows into
``trend_scores``. Existing source-level rows for the same dates are left
untouched because the MERGE keys ``(trend_date, market, query_group)``
do not collide with topic-level keys.

After the backfill runs, ``compute_velocity_scores_for_today`` sees
real per-topic ``item_count`` history in the 14-day window and produces
genuine velocity scores rather than the saturated NEW_TOPIC fallback.

Note on column completeness
---------------------------
Historical ``enriched_content`` rows do not store ``engagement_weighted``
(only the runtime DataFrame from ``enrich_dataframe`` carries it). The
aggregator treats missing engagement as 0.0, so backfilled trend_scores
rows have ``engagement_score = 0`` and ``engagement_sum = 0``. This is
acceptable: the daily digest reads only today's ``trend_scores``, so the
backfilled values never display, and ``velocity.py`` reads only
``item_count`` from the baseline window.

Modes
-----
``--dry-run`` (default): print plan + per-day row counts. No writes.
``--apply``: write to BQ.
``--days N``: window length (default 14).

Examples::

    python scripts/migrations/backfill_topic_trend_scores.py --dry-run
    python scripts/migrations/backfill_topic_trend_scores.py --apply
    python scripts/migrations/backfill_topic_trend_scores.py --apply --days 7
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# Load .env before importing BQ client.
env_path = Path(__file__).resolve().parent.parent.parent / ".env"
try:
    from dotenv import load_dotenv

    if env_path.exists():
        load_dotenv(env_path, override=False)
except ImportError:
    pass

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
# scripts/ on path so importlib can find run_rss_now (top-level script,
# not packaged under src/).
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import pandas as pd
from src.enrichment.topic_classifier import classify_topics
from src.utils.bigquery import (
    get_client,
    get_dataset,
    merge_dataframe,
    run_query,
)
from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

MARKETS = ("za", "ng", "ke")

# Pulls every column the aggregator reads. engagement_weighted is not in
# the BQ schema, so it stays absent and defaults to 0.0 in the aggregator.
_ENRICHED_QUERY = """
SELECT
  market,
  source,
  platform,
  title,
  text,
  hashtags,
  author_handle_norm,
  regional_score,
  genz_score,
  slang_score,
  creator_watchlist_score,
  search_velocity_score,
  tone_avg
FROM `{project}.{dataset}.enriched_content`
WHERE DATE(collected_at) = DATE(@day)
"""


def list_target_days(days: int, today: date | None = None) -> list[date]:
    """Return dates [today - days, ..., today - 1] in chronological order.

    Today is excluded because the daily cron already wrote topic-level
    rows for today; backfilling today would no-op via MERGE on identical
    keys but is wasteful.
    """
    today = today or datetime.now(UTC).date()
    return [today - timedelta(days=offset) for offset in range(days, 0, -1)]


def classify_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Add a ``topic_groups`` column to ``df`` via classify_topics().

    Pure: same input produces same output. Operates on a copy.
    """
    out = df.copy()
    out["topic_groups"] = [
        classify_topics(
            title=str(row.get("title") or ""),
            text=str(row.get("text") or ""),
            hashtags=str(row.get("hashtags") or ""),
            market=str(row.get("market") or ""),
        )
        for row in out.to_dict(orient="records")
    ]
    return out


def build_score_rows(df_classified: pd.DataFrame, day: date) -> list[dict]:
    """Aggregate per (market, topic_group), score, return row dicts.

    Calls into ``scripts.run_rss_now._aggregate_by_topic`` and
    ``compute_trend_scores`` with empty velocity input so backfill rows
    get ``velocity_score = 0`` (they are the seed history, not a current
    measurement).
    """
    # Lazy import: scripts/ is on sys.path via the bootstrap above.
    import importlib

    run = importlib.import_module("run_rss_now")

    market_counts: dict[tuple[str, str], dict] = {}
    for market in MARKETS:
        df_m = df_classified[df_classified["market"] == market]
        if df_m.empty:
            continue
        topic_counts, _ = run._aggregate_by_topic(df_m, market)
        market_counts.update(topic_counts)

    if not market_counts:
        return []

    scored_at = datetime.now(UTC)
    return run.compute_trend_scores(
        market_counts,
        day,
        scored_at,
        velocity_scores={},
    )


def backfill_day(day: date, *, apply: bool) -> tuple[int, int]:
    """Backfill one day. Returns (rows_read, score_rows_written)."""
    df = run_query(_ENRICHED_QUERY, params={"day": day.isoformat()})
    if df.empty:
        return 0, 0

    df_classified = classify_dataframe(df)
    score_rows = build_score_rows(df_classified, day)

    if not score_rows:
        return len(df), 0

    if apply:
        merge_dataframe(
            pd.DataFrame(score_rows),
            "trend_scores",
            merge_keys=["trend_date", "market", "query_group"],
        )
    return len(df), len(score_rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Execute against BQ")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan only (default)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Number of days to backfill (default 14)",
    )
    args = parser.parse_args()

    apply = args.apply and not args.dry_run

    client = get_client()
    dataset = get_dataset()
    print(f"Target: {client.project}.{dataset}")
    print(f"Mode:   {'APPLY' if apply else 'dry-run'}")
    print(f"Window: last {args.days} days (excluding today)")
    print()

    days = list_target_days(args.days)
    total_read = 0
    total_written = 0
    for day in days:
        rows_read, rows_written = backfill_day(day, apply=apply)
        marker = "APPLIED" if apply and rows_written else "(dry-run)" if not apply else "no rows"
        print(f"  {day} read={rows_read:6d} score_rows={rows_written:3d}  {marker}")
        total_read += rows_read
        total_written += rows_written

    print()
    print(f"Totals: read={total_read} score_rows={total_written}")
    if not apply:
        print("Dry run complete. Re-run with --apply to write to BQ.")
    else:
        print("Backfill applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
