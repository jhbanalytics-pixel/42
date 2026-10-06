#!/usr/bin/env python
"""Backfill seed_graph for a date range (local ADC, oldest to newest).

python scripts/backfill_seed_graph.py --start 2026-05-01 --end 2026-05-07
python scripts/backfill_seed_graph.py --start 2026-05-01 --end 2026-05-07 --dry-run
python scripts/backfill_seed_graph.py --start 2026-05-01 --end 2026-05-07 --resume-from 2026-05-03
python scripts/backfill_seed_graph.py --day 2026-06-18
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
import traceback
from pathlib import Path

logging.disable(logging.CRITICAL)

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "ogilvy-trends-v2")
os.environ.setdefault("GOOGLE_CLOUD_QUOTA_PROJECT", "ogilvy-trends-v2")
os.environ.setdefault("TRENDS_ENV", "dev")

env_path = Path(__file__).resolve().parent.parent / ".env"
try:
    from dotenv import load_dotenv

    if env_path.exists():
        load_dotenv(env_path, override=False)
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.cloud import bigquery as bq
from src.analysis.seed_graph import build_and_persist_seed_graph, fetch_enriched_for_seed_graph
from src.utils.bigquery import get_client, get_dataset


def _dry_run_bytes(start: dt.date, end: dt.date) -> None:
    client = get_client()
    dataset = get_dataset()
    sql = f"""
    SELECT COUNT(*) AS row_count
    FROM `{client.project}.{dataset}.enriched_content`
    WHERE DATE(collected_at) BETWEEN @start AND @end
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("start", "DATE", start),
            bq.ScalarQueryParameter("end", "DATE", end),
        ],
        dry_run=True,
        use_query_cache=False,
    )
    job = client.query(sql, job_config=job_config)
    print(f"Dry-run bytes processed: {job.total_bytes_processed or 0:,}")


def _process_day(day: dt.date) -> dict[str, int]:
    t0 = time.monotonic()
    print(f"--- {day.isoformat()} ---", flush=True)
    try:
        df = fetch_enriched_for_seed_graph(day)
        enriched_n = len(df)
        print(f"  enriched rows: {enriched_n}", flush=True)
        counts = build_and_persist_seed_graph(day)
        if counts == {}:
            raise RuntimeError(
                f"build_and_persist_seed_graph returned empty counts for {day.isoformat()} "
                "(exception was swallowed upstream)"
            )
    except Exception:
        print(f"FAILED {day.isoformat()}:", flush=True)
        traceback.print_exc()
        sys.exit(1)
    za = counts.get("za", 0)
    ng = counts.get("ng", 0)
    ke = counts.get("ke", 0)
    elapsed = time.monotonic() - t0
    print(
        f"  seed_graph: za={za} ng={ng} ke={ke} total={za + ng + ke} elapsed={elapsed:.1f}s",
        flush=True,
    )
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", help="YYYY-MM-DD")
    parser.add_argument("--end", help="YYYY-MM-DD")
    parser.add_argument("--day", help="YYYY-MM-DD single day (overrides start/end)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume-from", default="", help="YYYY-MM-DD skip dates before this")
    args = parser.parse_args()

    if args.day:
        start = end = dt.date.fromisoformat(args.day)
    else:
        if not args.start or not args.end:
            parser.error("--start and --end are required unless --day is set")
        start = dt.date.fromisoformat(args.start)
        end = dt.date.fromisoformat(args.end)
    resume = dt.date.fromisoformat(args.resume_from) if args.resume_from else start

    if args.dry_run:
        _dry_run_bytes(start, end)
        print("(dry-run: no rows written)")
        return 0

    day = max(start, resume)
    while day <= end:
        _process_day(day)
        day += dt.timedelta(days=1)

    return 0


if __name__ == "__main__":
    sys.exit(main())
