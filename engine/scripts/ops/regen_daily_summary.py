# Standalone daily_summary regeneration for a Cloud Run job.
#
# Extracted from the regen-daily-summary.yml inline heredoc. Skips ingestion,
# scoring, brief generation, and email send. Reads today's existing
# trend_analysis briefs from BigQuery, runs generate_daily_summary with
# force=True (which auto-deletes any empty placeholder row first), and writes
# the fresh non-empty row.
#
# Exit codes: 0 ok, 2 no briefs, 3 summary still empty after retry.
# ADC on Cloud Run, no key file handling.

import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

# Cloud Run launches this as `python scripts/ops/<name>.py`, so the repo root is
# not on sys.path by default. Put it there before importing src.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from google.cloud import bigquery as bq
from src.analysis.generate_daily_summary import generate_daily_summary
from src.utils.bigquery import get_client, get_dataset


def main() -> int:
    raw = os.environ.get("TREND_DATE_INPUT", "").strip()
    trend_date = date.fromisoformat(raw) if raw else datetime.now(UTC).date()

    bq_client = get_client()
    dataset = get_dataset()
    ds_path = f"{bq_client.project}.{dataset}"

    # Pull today's briefs from trend_analysis as the input set.
    briefs_sql = f"""
    SELECT market, query_group, status_tag, trend_synthesis, cultural_context
    FROM `{ds_path}.trend_analysis`
    WHERE trend_date = @trend_date
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY market, query_group ORDER BY analyzed_at DESC
    ) = 1
    """
    briefs_job = bq_client.query(
        briefs_sql,
        job_config=bq.QueryJobConfig(
            query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
        ),
    )
    briefs_by_topic = {}
    for r in briefs_job.result():
        briefs_by_topic[(str(r.market), str(r.query_group))] = {
            "status_tag": r.status_tag or "Rising",
            "description_rationale": r.trend_synthesis or "",
        }
    print(f"Pulled {len(briefs_by_topic)} briefs for {trend_date}")

    if not briefs_by_topic:
        print("No briefs found; nothing to summarise. Aborting.")
        return 2

    # Pull trend_scores so the summary prompt has velocity + item_count + the
    # seed_score the verdict block reads. seed_score must match the live cron read
    # (run_rss_now), or a regenerated summary silently drops the Nano Banana /
    # Lyria seed recommendation.
    scores_sql = f"""
    SELECT market, query_group, trend_score, velocity_score, item_count, seed_score
    FROM `{ds_path}.trend_scores`
    WHERE trend_date = @trend_date
    """
    scores_job = bq_client.query(
        scores_sql,
        job_config=bq.QueryJobConfig(
            query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
        ),
    )
    trend_scores_by_topic = {
        (str(r.market), str(r.query_group)): {
            "trend_score": float(r.trend_score or 0.0),
            "velocity_score": float(r.velocity_score or 0.0),
            "item_count": int(r.item_count or 0),
            "seed_score": float(getattr(r, "seed_score", 0.0) or 0.0),
        }
        for r in scores_job.result()
    }

    # force=True so the entry guard does not skip this even if a non-empty row
    # somehow already exists; the persist branch auto-clears empty placeholders
    # regardless.
    summary = generate_daily_summary(
        trend_date=trend_date,
        briefs_by_topic=briefs_by_topic,
        trend_scores_by_topic=trend_scores_by_topic,
        force=True,
    )
    if summary is None:
        print("Summary generation returned None (still empty after retry).")
        return 3

    print(f"RESULT: trend_date={trend_date}")
    print(f"  through_line: {summary.through_line[:120]}")
    print(f"  summary_text head: {summary.summary_text[:160]}")
    print(f"  call_to_action head: {summary.call_to_action[:120]}")
    print(
        f"  brief_count={summary.brief_count} key={len(summary.key_topics)} rising={len(summary.rising_topics)}"
    )
    print(f"  tokens prompt={summary.prompt_tokens} completion={summary.completion_tokens}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
