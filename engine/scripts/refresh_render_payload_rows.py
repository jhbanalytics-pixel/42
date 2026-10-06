"""One-off row-by-row render_payload refresh when persist MERGE subquery fails."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, date, datetime
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

logging.disable(logging.CRITICAL)

from google.cloud import bigquery as bq
from src.analysis.generate_briefs import (
    DEFAULT_SAMPLE_ROWS,
    _build_display,
    _platform_counts_for_topic,
    _prior_day_scores,
    _sample_rows_for_topic,
)
from src.utils.bigquery import get_client, get_dataset


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    td = date.fromisoformat(args.date)
    client = get_client()
    dataset = get_dataset()
    table = f"{client.project}.{dataset}.trend_analysis"
    sql = f"""
        SELECT a.market, a.query_group, a.render_payload, a.analyzed_at,
               s.trend_score, s.velocity_score, s.search_velocity_score
        FROM `{table}` a
        JOIN `{client.project}.{dataset}.trend_scores` s
          ON s.trend_date = a.trend_date
         AND s.market = a.market
         AND s.query_group = a.query_group
        WHERE a.trend_date = @d
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY a.market, a.query_group ORDER BY a.analyzed_at DESC
        ) = 1
    """
    rows = list(
        client.query(
            sql,
            job_config=bq.QueryJobConfig(
                query_parameters=[bq.ScalarQueryParameter("d", "DATE", td)]
            ),
        )
    )
    prior = _prior_day_scores(client, dataset, td)
    updated = 0
    for r in rows:
        m, tg = r.market, r.query_group
        pc = _platform_counts_for_topic(client, dataset, m, tg, td)
        sample = _sample_rows_for_topic(client, dataset, m, tg, td, DEFAULT_SAMPLE_ROWS)
        display = _build_display(
            market=m,
            topic_group=tg,
            trend_score=float(r.trend_score or 0),
            velocity=float(r.velocity_score or 0),
            prior_score=prior.get((m, tg)),
            platform_count_rows=pc,
            sample_rows=sample,
            search_velocity=(
                float(r.search_velocity_score) if r.search_velocity_score is not None else None
            ),
        )
        entry: dict = {"display": display}
        if r.render_payload:
            try:
                old = json.loads(r.render_payload)
                for key in ("comment_sentiment", "comment_themes", "driving_hashtags"):
                    if old.get(key):
                        entry[key] = old[key]
            except (ValueError, TypeError):
                pass
        payload = json.dumps(entry, default=str)
        client.query(
            f"""
            UPDATE `{table}`
            SET render_payload = @payload
            WHERE trend_date = @d AND market = @m AND query_group = @tg
              AND analyzed_at = @at
            """,
            job_config=bq.QueryJobConfig(
                query_parameters=[
                    bq.ScalarQueryParameter("payload", "STRING", payload),
                    bq.ScalarQueryParameter("d", "DATE", td),
                    bq.ScalarQueryParameter("m", "STRING", m),
                    bq.ScalarQueryParameter("tg", "STRING", tg),
                    bq.ScalarQueryParameter("at", "TIMESTAMP", r.analyzed_at),
                ]
            ),
        ).result()
        ch = len(display.get("channels") or [])
        print(f"  {m}/{tg}: channels={ch}")
        updated += 1
    print(f"Updated {updated} briefs for {td}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
