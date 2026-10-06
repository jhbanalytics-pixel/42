"""Twitter-only EnsembleData ingest for markets with twitter_handles_enabled.

Writes raw_content + enriched_content only. No scoring, briefs, or email.
Use after flipping handles or to backfill when the main cron exhausted budget
before /twitter/user/tweets (fixed by prepending twitter endpoints in ensemble.py).
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

import logging

logging.disable(logging.CRITICAL)

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "ogilvy-trends-v2")
os.environ.setdefault("TRENDS_ENV", "dev")

from scripts.run_rss_now import _ingest_market_frames
from src.ingestion.connectors.ensemble import EnsembleConnector

TWITTER_SURFACES = frozenset({"twitter_user"})
DEFAULT_MARKETS = ("za", "ng", "ke")


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensemble Twitter-only ingest")
    parser.add_argument(
        "--markets",
        default=",".join(DEFAULT_MARKETS),
        help="Comma-separated markets (default za,ng,ke)",
    )
    args = parser.parse_args()
    markets = [m.strip().lower() for m in args.markets.split(",") if m.strip()]

    EnsembleConnector.reset_global_budget()
    run_id = str(uuid.uuid4())
    started_at = datetime.now(UTC)
    total = 0

    for market in markets:
        print(f"=== twitter ingest market={market} ===")
        conn = EnsembleConnector(market=market)
        df = conn.fetch(surfaces=TWITTER_SURFACES)
        n = len(df)
        print(f"  fetched {n} rows, units={conn._units_spent}")
        if n == 0:
            continue
        df = df.copy()
        df["market"] = market
        rows_raw, rows_enriched, *_rest = _ingest_market_frames(market, [df], run_id, started_at)
        print(f"  wrote raw={rows_raw} enriched={rows_enriched}")
        total += rows_enriched

    print(f"Done. {total} enriched twitter rows across {len(markets)} markets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
