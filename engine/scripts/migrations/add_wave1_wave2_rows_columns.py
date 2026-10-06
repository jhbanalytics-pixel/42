"""Idempotent BigQuery migration: add Wave 1 + Wave 2 per-source row columns.

Closes the same per-source rollup gap that `add_reddit_rows_column.py`
closed for Reddit on 27 May 2026, this time for the 28 May 2026 batch
of new surfaces.

Wave 1 (LIVE today, commit 66abb7a):

* `apple_music_rows`  -- 8th connector ships counts to raw_content fine
  but `pipeline_runs.apple_music_rows` does not exist, so the per-source
  rollup silently lumps Apple Music into nothing.

Wave 2 (shipped today, commit 8ad51c7, flags still off pending flag
flip after live-probe per the validate-vendor-before-flag-flip rule):

* `top_terms_rows`                       -- BigQuery Trends top_terms
* `brand24_mention_sentiment_rows`       -- Brand24 per-mention sentiment
* `brand24_mention_reach_rows`           -- Brand24 per-mention reach
* `brand24_daily_metric_rows`            -- Brand24 daily metric rollups
* `youtube_playlist_items_rows`          -- YouTube playlistItems

Safe to re-run. Uses `ADD COLUMN IF NOT EXISTS` for every new column.
View update uses `CREATE OR REPLACE` so all new columns surface in
`v_pipeline_health` immediately for the morning-check skill and
Thapelo's Looker dashboard.

Per DEVELOPMENT.md gotcha, `ADD COLUMN` does NOT carry a `DEFAULT` clause
because BigQuery rejects `DEFAULT` on populated tables. Pre-existing
`pipeline_runs` rows read the new columns as NULL until the next run
populates them. The view wraps each new column in `COALESCE(<col>, 0)`
so historical rows surface as 0 not NULL on the dashboard.

The view definition preserves the production behaviour byte-for-byte
(30-day window, COALESCE finished_at fallback, freshness thresholds
8h / 12h, error_summary ' | ' separator, reddit_rows from the 27 May
migration). Only the new Wave 1 + Wave 2 columns are added.

Run modes:

    python scripts/migrations/add_wave1_wave2_rows_columns.py --dry-run
    python scripts/migrations/add_wave1_wave2_rows_columns.py --apply
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
    "apple_music_rows",
    "top_terms_rows",
    "brand24_mention_sentiment_rows",
    "brand24_mention_reach_rows",
    "brand24_daily_metric_rows",
    "youtube_playlist_items_rows",
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
    # Production view contract preserved verbatim:
    #   * 30-day rolling window on started_at
    #   * COALESCE finished_at fallback in the duration calculation
    #   * 8h / 12h freshness thresholds keyed on last_successful_run
    #   * ARRAY_TO_STRING(errors, ' | ') for the error_summary column
    #   * reddit_rows COALESCE wrap from the 27 May migration
    # The only deltas vs the 27 May version are the new Wave 1 + Wave 2
    # columns, each COALESCEd to 0 so pre-migration pipeline_runs rows
    # surface as 0 not NULL on Looker.
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
        total_rows,
        trends_scored,
        briefs_generated,
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
    """Column ALTERs first so the view's COALESCE references resolve."""
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
