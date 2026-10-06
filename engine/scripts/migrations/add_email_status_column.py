"""Idempotent BigQuery migration: add email_status to pipeline_runs.

The daily email digest can fail (SMTP outage, Gmail auth blip, render
crash) AFTER the per-market status='success' rows are already written
and the failure is swallowed non-fatally, so the run exits 0. Until now
the email outcome lived only in stdout and the logger, so a no-email day
left no signal in BigQuery and the morning-check could not see it.

This column records the email outcome. run_rss_now.py writes one extra
thin marker row (market='ALL', status='email_audit') AFTER the send
resolves, carrying the outcome in email_status. The marker row uses
status='email_audit' (not 'success') on purpose so the idempotency guard
in check_already_ran_today.py (WHERE status='success') ignores it and the
cron skip behaviour is unchanged. The per-market 'success' rows keep
email_status NULL.

Allowed values written by the pipeline: 'sent', 'send_failed',
'skipped_nothing_notable', 'render_error'. A non-'sent'/'skipped' value
is what the bq-snapshot skill flags.

Safe to re-run; uses ADD COLUMN IF NOT EXISTS. View update uses
CREATE OR REPLACE so email_status appears in v_pipeline_health
immediately for the morning-check skill.

Per DEVELOPMENT.md gotcha, ADD COLUMN does NOT carry a DEFAULT clause because
BigQuery rejects DEFAULT on populated tables. Existing rows read
email_status as NULL until the next run writes a marker row.

Run modes:

    python scripts/migrations/add_email_status_column.py --dry-run
    python scripts/migrations/add_email_status_column.py --apply
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

STATEMENTS = [
    (
        "pipeline_runs.email_status",
        "ALTER TABLE `{project}.{dataset}.pipeline_runs` "
        "ADD COLUMN IF NOT EXISTS email_status STRING",
    ),
    (
        "v_pipeline_health",
        # Matches the canonical infra/bigquery_views/v_pipeline_health.sql
        # (30-day window, COALESCE finished_at fallback, freshness thresholds
        # 8h / 12h, error_summary ' | ' separator, all 7 per-source row
        # columns wrapped in COALESCE) and adds email_status. Both this
        # migration and the create_looker_views.py deploy path therefore emit
        # an identical view, so re-running either cannot drop the other's
        # columns. email_status is surfaced raw (no COALESCE) so a NULL on a
        # per-market row reads as None and a marker row's value reads through;
        # morning-check flags any non-'sent' / non-'skipped_nothing_notable'
        # value for the day. Keep this body in sync with the canonical .sql.
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
    ),
]


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

    for label, sql_template in STATEMENTS:
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
