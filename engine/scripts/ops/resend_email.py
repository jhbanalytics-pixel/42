"""Read-only resend of the daily PULSE digest email from existing BigQuery state.

Re-renders and re-sends the daily digest for a target date using data already in
BigQuery. Zero connector calls, zero Vertex Gemini calls, zero new BQ writes.
Cloud Run job carries the cron feature flags (configs/cron_flags.env) as env vars,
so this script does not source that file itself. ADC handles GCP auth on Cloud Run.

Exit codes:
  0  email sent
  2  no trend_scores rows for the date (cannot rebuild)
  3  send_daily_digest returned a non-sent status
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

# Cloud Run launches this as `python scripts/ops/<name>.py`, so the repo root is
# not on sys.path by default. Put it there before importing src.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from google.cloud import bigquery as bq
from src.alerts.brief_loader import load_briefs_by_topic_from_bq
from src.alerts.detector import send_daily_digest
from src.utils.bigquery import get_client, get_dataset


def main() -> int:
    raw = os.environ.get("TREND_DATE_INPUT", "").strip()
    trend_date = date.fromisoformat(raw) if raw else datetime.now(UTC).date()

    bq_client = get_client()
    dataset = get_dataset()
    ds_path = f"{bq_client.project}.{dataset}"

    # Pull today's trend_scores: send_daily_digest uses these to detect upgrades
    # + movers + render per-card scoring signals.
    score_rows_sql = f"""
    SELECT *
    FROM `{ds_path}.trend_scores`
    WHERE trend_date = @trend_date
    """
    score_rows_job = bq_client.query(
        score_rows_sql,
        job_config=bq.QueryJobConfig(
            query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
        ),
    )
    score_rows = [dict(r.items()) for r in score_rows_job.result()]
    print(f"Loaded {len(score_rows)} trend_scores rows for {trend_date}")

    if not score_rows:
        print("No trend_scores rows; cannot rebuild email. Aborting.")
        return 2

    # Rebuild every persisted brief for the date into the email dict shape (with
    # render_payload badges re-applied). Shared with the cron supplement path.
    briefs_by_topic = load_briefs_by_topic_from_bq(trend_date)
    print(f"Loaded {len(briefs_by_topic)} briefs for {trend_date}")

    # Pull today's daily_summary row.
    ds_sql = f"""
    SELECT summary_text, through_line, call_to_action,
           key_topics, rising_topics, seed_recommend
    FROM `{ds_path}.daily_summary`
    WHERE trend_date = @trend_date
      AND COALESCE(summary_text, '') != ''
    ORDER BY generated_at DESC
    LIMIT 1
    """
    ds_job = bq_client.query(
        ds_sql,
        job_config=bq.QueryJobConfig(
            query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
        ),
    )
    ds_rows = list(ds_job.result())
    daily_summary_dict = None
    if ds_rows:
        r = ds_rows[0]
        daily_summary_dict = {
            "summary_text": r.summary_text or "",
            "through_line": r.through_line or "",
            "call_to_action": r.call_to_action or "",
            "key_topics": list(r.key_topics or []),
            "rising_topics": list(r.rising_topics or []),
            # Seed score topline (Jo, 22 Jun): read back so the resend renders
            # the same "Seed to drive Nanobanana + Lyria" panel.
            "seed_recommend": getattr(r, "seed_recommend", "") or "",
        }
        print(f"Loaded daily_summary; through_line: {r.through_line[:80]}")
    else:
        print("No non-empty daily_summary row; email will render without the top block.")

    # Build minimal stats from today's pipeline_runs aggregate. pipeline_runs
    # carries total_rows / trends_scored / unclassified_rows (there is no
    # enriched_rows or elapsed_seconds column).
    stats_sql = f"""
    SELECT
        SUM(total_rows) AS raw_rows,
        SUM(trends_scored) AS scored_rows,
        SUM(unclassified_rows) AS unclassified_rows
    FROM `{ds_path}.pipeline_runs`
    WHERE DATE(started_at) = @trend_date
    """
    try:
        stats_job = bq_client.query(
            stats_sql,
            job_config=bq.QueryJobConfig(
                query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
            ),
        )
        stats_row = list(stats_job.result())[0]
        stats = {
            "raw_rows": int(stats_row.raw_rows or 0),
            "scored_rows": int(stats_row.scored_rows or 0),
            "unclassified_rows": int(stats_row.unclassified_rows or 0),
            "errors_count": 0,  # not retroactively recoverable
            "briefs_skipped": 0,
        }
    except Exception as exc:
        print(f"Stats pull failed (non-fatal): {exc}")
        stats = {}

    markets_with_upgrades, total_upgrades, total_movers, status = send_daily_digest(
        score_rows=score_rows,
        stats=stats,
        issues=None,
        healed=None,
        briefs_by_topic=briefs_by_topic,
        daily_summary=daily_summary_dict,
    )

    print()
    print(f"RESULT: status={status}")
    print(f"  upgrades: {total_upgrades} across {markets_with_upgrades} market(s)")
    print(f"  movers: {total_movers}")
    if status != "sent":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
