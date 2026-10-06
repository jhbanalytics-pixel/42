"""Backfill trend_analysis.render_payload for one date, no Gemini calls.

A recovery day can leave stored briefs without their render bundle: the
display dict is computed in generate_briefs at insert time, and the
conversation fields are tagged by the producers inside the live pipeline.
When a run dies before those steps persist (or the rows predate the
render_payload column), a resend renders without Seen-on channels, state
badges, or chips.

This script rebuilds the display part for every stored brief on the date,
through the same assembly functions the live path uses
(_platform_counts_for_topic, _sample_rows_for_topic, _prior_day_scores,
_build_display), then writes render_payload via persist_render_payloads.
Brief text is untouched; no Vertex calls. Conversation fields are written
only if they are already present in an existing render_payload (they cannot
be rebuilt without the producers).

Run::

    python scripts/backfill_render_payload.py --date 2026-06-10 --apply
"""

from dotenv import load_dotenv

load_dotenv()

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from google.cloud import bigquery as bq
from src.analysis.generate_briefs import (
    DEFAULT_SAMPLE_ROWS,
    _build_display,
    _platform_counts_for_topic,
    _prior_day_scores,
    _sample_rows_for_topic,
    persist_render_payloads,
)
from src.utils.bigquery import get_client, get_dataset


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="", help="Trend date YYYY-MM-DD (default today UTC)")
    parser.add_argument("--apply", action="store_true", help="Write to BigQuery")
    args = parser.parse_args()

    trend_date = date.fromisoformat(args.date) if args.date else datetime.now(UTC).date()
    client = get_client()
    dataset = get_dataset()
    ds_path = f"{client.project}.{dataset}"

    briefs_sql = f"""
    SELECT a.market, a.query_group, a.render_payload,
           s.trend_score, s.velocity_score, s.search_velocity_score
    FROM `{ds_path}.trend_analysis` a
    JOIN `{ds_path}.trend_scores` s
      ON s.trend_date = a.trend_date
     AND s.market = a.market
     AND s.query_group = a.query_group
    WHERE a.trend_date = @trend_date
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
    )
    rows = list(client.query(briefs_sql, job_config=job_config).result())
    print(f"{len(rows)} stored briefs for {trend_date}")
    if not rows:
        return 2

    prior_scores = _prior_day_scores(client, dataset, trend_date)
    payload_briefs: dict[tuple[str, str], dict] = {}
    for r in rows:
        market = str(r.market)
        topic_group = str(r.query_group)
        platform_counts_rows = _platform_counts_for_topic(
            client, dataset, market, topic_group, trend_date
        )
        sample = _sample_rows_for_topic(
            client, dataset, market, topic_group, trend_date, DEFAULT_SAMPLE_ROWS
        )
        display = _build_display(
            market=market,
            topic_group=topic_group,
            trend_score=float(r.trend_score or 0.0),
            velocity=float(r.velocity_score or 0.0),
            prior_score=prior_scores.get((market, topic_group)),
            platform_count_rows=platform_counts_rows,
            sample_rows=sample,
            search_velocity=(
                float(r.search_velocity_score) if r.search_velocity_score is not None else None
            ),
        )
        entry: dict = {"display": display}
        # Preserve conversation fields if a prior payload carried them.
        if r.render_payload:
            try:
                old = json.loads(r.render_payload)
                for key in ("comment_sentiment", "comment_themes", "driving_hashtags"):
                    if old.get(key):
                        entry[key] = old[key]
            except (ValueError, TypeError):
                pass
        payload_briefs[(market, topic_group)] = entry
        ch = len(display.get("channels") or [])
        print(f"  {market}/{topic_group}: channels={ch} state={display.get('state', {})}")

    if not args.apply:
        print("Dry run complete. Re-run with --apply to write.")
        return 0

    updated = persist_render_payloads(trend_date, payload_briefs)
    print(f"render_payload written for {updated}/{len(payload_briefs)} briefs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
