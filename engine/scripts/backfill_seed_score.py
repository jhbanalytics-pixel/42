"""Backfill trend_scores.seed_score for one date from enriched_content.

seed_score shipped after the day's cron had already scored, so existing
trend_scores rows carry seed_score = NULL. This recomputes it for a date by
re-aggregating that day's enriched_content and running the same scoring helper,
then UPDATEs only the seed_score column (trend_score and everything else are
left untouched).

One approximation: enriched_content stores engagement_total (the raw sum), not
the category-weighted, days-normalised engagement_weighted the live scorer
aggregates, so the engagement input is proxied by engagement_total. Slang,
creator spread, channel-family mix and tone are exact, so seed_score is close
and self-corrects from the next cron forward (which writes exact values for new
dates). Intended for backfilling a historical day so the desk can show it now.

Run modes:
    python scripts/backfill_seed_score.py --date 2026-06-22 --dry-run
    python scripts/backfill_seed_score.py --date 2026-06-22 --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
env_path = repo_root / ".env"
try:
    from dotenv import load_dotenv

    if env_path.exists():
        load_dotenv(env_path, override=False)
except ImportError:
    pass

sys.path.insert(0, str(repo_root))

import pandas as pd
from google.cloud import bigquery
from scripts.run_rss_now import _aggregate_by_topic, compute_trend_scores
from src.utils.bigquery import get_client, get_dataset

MARKETS = ["za", "ng", "ke"]

_SELECT = """
SELECT
  topic_groups,
  platform,
  engagement_total AS engagement_weighted,
  regional_score,
  slang_score,
  creator_watchlist_score,
  search_velocity_score,
  tone_avg,
  source,
  author_handle_norm,
  content_type
FROM `{project}.{dataset}.enriched_content`
WHERE market = @market AND DATE(collected_at) = @date
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="trend_date YYYY-MM-DD")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    apply = args.apply and not args.dry_run
    trend_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    client = get_client()
    dataset = get_dataset()
    project = client.project
    now = datetime.now(UTC)

    print(
        f"Target: {project}.{dataset}.trend_scores  date={trend_date}  mode={'APPLY' if apply else 'dry-run'}"
    )
    print()

    seed_by_key: dict[tuple[str, str], float] = {}
    for market in MARKETS:
        sql = _SELECT.format(project=project, dataset=dataset)
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("market", "STRING", market),
                    bigquery.ScalarQueryParameter("date", "DATE", trend_date),
                ]
            ),
        )
        df = job.to_dataframe()
        if df.empty:
            print(f"  {market}: no enriched_content rows, skipping")
            continue
        # BigQuery ARRAY columns come back as numpy arrays; the aggregator does
        # truthiness checks (`or []`, `== ["__drop__"]`) that are ambiguous on a
        # numpy array, so coerce topic_groups to plain Python lists like the live
        # in-memory frame carries.
        df["topic_groups"] = df["topic_groups"].apply(lambda a: list(a) if a is not None else [])
        counts, _ = _aggregate_by_topic(df, market)
        rows = compute_trend_scores(counts, trend_date, now, velocity_scores={})
        for r in rows:
            seed_by_key[(r["market"], r["query_group"])] = {
                "seed_score": float(r["seed_score"]),
                "seed_audience_fit": float(r.get("seed_audience_fit") or 0.0),
                "seed_format_fit": float(r.get("seed_format_fit") or 0.0),
                "seed_safety_gate": float(r.get("seed_safety_gate") or 0.0),
                "visual_audio_share": float(r.get("visual_audio_share") or 0.0),
            }
        print(f"  {market}: {len(rows)} topics scored")

    if not seed_by_key:
        print("Nothing to backfill.")
        return 0

    sample = sorted(seed_by_key.items(), key=lambda kv: kv[1]["seed_score"], reverse=True)[:8]
    print("\nTop seed scores:")
    for (mkt, tg), v in sample:
        print(
            f"  {mkt}/{tg}: {v['seed_score']:.3f} "
            f"(aud {v['seed_audience_fit']:.2f}, fmt {v['seed_format_fit']:.2f}, "
            f"safe {v['seed_safety_gate']:.2f})"
        )

    if not apply:
        print("\nDry run. Re-run with --apply to write.")
        return 0

    table = f"`{project}.{dataset}.trend_scores`"
    updated = 0
    for (market, query_group), v in seed_by_key.items():
        client.query(
            f"UPDATE {table} SET seed_score = @s, seed_audience_fit = @aud, "
            "seed_format_fit = @fmt, seed_safety_gate = @safe, visual_audio_share = @va "
            "WHERE trend_date = @d AND market = @m AND query_group = @q",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("s", "FLOAT64", v["seed_score"]),
                    bigquery.ScalarQueryParameter("aud", "FLOAT64", v["seed_audience_fit"]),
                    bigquery.ScalarQueryParameter("fmt", "FLOAT64", v["seed_format_fit"]),
                    bigquery.ScalarQueryParameter("safe", "FLOAT64", v["seed_safety_gate"]),
                    bigquery.ScalarQueryParameter("va", "FLOAT64", v["visual_audio_share"]),
                    bigquery.ScalarQueryParameter("d", "DATE", trend_date),
                    bigquery.ScalarQueryParameter("m", "STRING", market),
                    bigquery.ScalarQueryParameter("q", "STRING", query_group),
                ]
            ),
        ).result()
        updated += 1
    print(f"\nApplied seed_score + components to {updated} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
