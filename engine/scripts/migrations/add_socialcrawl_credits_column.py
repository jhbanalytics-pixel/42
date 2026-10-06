"""Idempotent BigQuery migration: pipeline_runs.socialcrawl_credits.

Per-market SocialCrawl credit spend for one run. Until this column exists the
rollout proposer has no measured burn to read and falls back to the declared
`actual_units_target` in configs/engine_capacity.yaml, which understated real
spend by roughly a quarter (290 declared against ~346 actual on 27 Jul 2026)
and therefore overstated both runway and headroom.

Semantics match socialcrawl_rows exactly: per market, per run, so a day's true
burn is SUM over the market rows. The connector's own counter is cumulative
across the market loop, so the value written is that market's delta, not the
running total.

Must be applied BEFORE run_rss_now writes the key: an unknown column fails
WRITE_APPEND and takes the whole cron down with it.

Safe to re-run. The view is CREATE OR REPLACE so v_pipeline_health surfaces the
column immediately for morning-check.

Run modes:

    python scripts/migrations/add_socialcrawl_credits_column.py --dry-run
    python scripts/migrations/add_socialcrawl_credits_column.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

env_path = Path(__file__).resolve().parent.parent.parent / ".env"
try:
    from dotenv import load_dotenv

    if env_path.exists():
        load_dotenv(env_path, override=False)
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.utils.bigquery import get_client, get_dataset

NEW_COLUMNS = [
    "socialcrawl_credits",
]


def _column_statements() -> list[tuple[str, str]]:
    statements: list[tuple[str, str]] = []
    for column in NEW_COLUMNS:
        statements.append(
            (
                f"pipeline_runs.{column}",
                "ALTER TABLE `{project}.{dataset}.pipeline_runs` "
                f"ADD COLUMN IF NOT EXISTS {column} INT64",
            )
        )
    return statements


VIEW_STATEMENT = (
    "v_pipeline_health",
    """
    CREATE OR REPLACE VIEW `{project}.{dataset}.v_pipeline_health` AS
    WITH runs AS (
      SELECT
        run_id,
        environment,
        market,
        status,
        started_at,
        finished_at,
        TIMESTAMP_DIFF(
          COALESCE(finished_at, CURRENT_TIMESTAMP()),
          started_at,
          SECOND
        ) AS duration_seconds,
        rss_rows,
        youtube_rows,
        gdelt_rows,
        ensemble_rows,
        brand24_rows,
        bigquery_trends_rows,
        COALESCE(reddit_rows, 0) AS reddit_rows,
        COALESCE(apple_music_rows, 0) AS apple_music_rows,
        COALESCE(top_terms_rows, 0) AS top_terms_rows,
        COALESCE(brand24_mention_sentiment_rows, 0) AS brand24_mention_sentiment_rows,
        COALESCE(brand24_mention_reach_rows, 0) AS brand24_mention_reach_rows,
        COALESCE(brand24_daily_metric_rows, 0) AS brand24_daily_metric_rows,
        COALESCE(youtube_playlist_items_rows, 0) AS youtube_playlist_items_rows,
        COALESCE(wikipedia_rows, 0) AS wikipedia_rows,
        COALESCE(bluesky_rows, 0) AS bluesky_rows,
        COALESCE(google_trends_rss_rows, 0) AS google_trends_rss_rows,
        COALESCE(app_charts_rows, 0) AS app_charts_rows,
        COALESCE(audiomack_rows, 0) AS audiomack_rows,
        COALESCE(cloudflare_radar_rows, 0) AS cloudflare_radar_rows,
        COALESCE(youtube_scrape_rows, 0) AS youtube_scrape_rows,
        COALESCE(socialcrawl_rows, 0) AS socialcrawl_rows,
        COALESCE(socialcrawl_credits, 0) AS socialcrawl_credits,
        total_rows,
        trends_scored,
        briefs_generated,
        email_status,
        ARRAY_TO_STRING(errors, ' | ') AS error_summary,
        notes
      FROM `{project}.{dataset}.pipeline_runs`
      WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
    ),
    last_success AS (
      SELECT
        market,
        MAX(finished_at) AS last_successful_run
      FROM `{project}.{dataset}.pipeline_runs`
      WHERE status = 'success'
        AND finished_at IS NOT NULL
      GROUP BY market
    )
    SELECT
      r.*,
      ls.last_successful_run,
      TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), ls.last_successful_run, HOUR) AS hours_since_success,
      CASE
        WHEN ls.last_successful_run IS NULL THEN 'Never run'
        WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), ls.last_successful_run, HOUR) > 12 THEN 'Stale'
        WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), ls.last_successful_run, HOUR) > 8 THEN 'Late'
        ELSE 'Fresh'
      END AS freshness_status
    FROM runs r
    LEFT JOIN last_success ls ON r.market = ls.market
    ORDER BY r.started_at DESC
    """,
)


def build_statements() -> list[tuple[str, str]]:
    return [*_column_statements(), VIEW_STATEMENT]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Execute against BQ")
    parser.add_argument("--dry-run", action="store_true", help="Print statements only (default)")
    args = parser.parse_args()

    apply = args.apply and not args.dry_run

    client = get_client()
    dataset = get_dataset()
    project = client.project

    print(f"Target: {project}.{dataset}")
    print(f"Mode:   {'APPLY' if apply else 'dry-run'}")
    print()

    for label, sql_template in build_statements():
        sql = sql_template.format(project=project, dataset=dataset)
        print(f"--- {label} ---")
        print(sql.strip())
        if apply:
            client.query(sql).result()
            print("APPLIED")
        else:
            print("(dry-run, not executed)")
        print()

    if not apply:
        print("Dry run complete. Re-run with --apply to execute.")
    else:
        print("Migration applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
