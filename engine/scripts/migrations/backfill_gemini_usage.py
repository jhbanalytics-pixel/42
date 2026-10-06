"""Idempotent backfill: fold historical Gemini spend into the gemini_usage ledger.

scripts/vertex_cost_watchdog.py costs off the gemini_usage ledger alone. Three
consumers used to be summed off token columns on their own tables instead, so
without this backfill the first watchdog run after deploy reports near-zero for
them until a full window of cron has written ledger rows.

What each source needs, and why they differ
===========================================
  trend_analysis  one row per Gemini call (one brief per market per topic), and
                  a retry's tokens are already summed onto the row it produced.
                  SUM is correct.
  daily_summary   one row per day, one call, tokens for both attempts already
                  summed onto it. SUM is correct.
  seed_insights   ONE call's tokens stamped onto EVERY seed row it writes
                  (generate_seed_intelligence._rows). ANY_VALUE, not SUM: over
                  2026-07-25..08-24 a SUM reports 533,373 prompt tokens against
                  a true 177,791, exactly 3.0x, because the pass writes three
                  seed rows a day. That is the over-count the old watchdog
                  carried, and it must not be reproduced in the new ledger.

The other three ledger consumers are not backfilled. comment_sentiment and
driving_hashtags always wrote to the ledger and have no table to recover from,
and reconcile's event_ledger rows carry gemini_model but no token columns, which
is why its spend used to be an estimate. Their history starts where their
writers did.

One honest limit: the ``calls`` column here is the source table's ROW count, not
the true call count, because a retry that was folded onto one row is invisible
from the table. The token totals and therefore the cost are exact; only the call
count on backfilled days can read low.

Run modes:
    python scripts/migrations/backfill_gemini_usage.py --dry-run [--days 30]
    python scripts/migrations/backfill_gemini_usage.py --apply [--days 30]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent.parent

env_path = repo_root / ".env"
try:
    from dotenv import load_dotenv

    if env_path.exists():
        load_dotenv(env_path, override=False)
except ImportError:
    pass

sys.path.insert(0, str(repo_root))

from src.utils.gemini_usage import GEMINI_USAGE_TABLE

DEFAULT_DAYS = 30


@dataclass(frozen=True)
class BackfillSource:
    """One table to recover ledger rows from.

    ``column_aggregate`` is the aggregate applied to the prompt and completion
    columns, and it is the field that matters: it is ANY_VALUE where one call's
    counts are stamped onto many rows, and SUM where one row is one call.

    Not named with a "token" prefix on purpose. bandit's B106 heuristic reads
    any keyword argument whose name contains "token" as a password, so
    a name like ``token_agg="SUM"`` is reported as a hardcoded credential at
    HIGH severity and fails the qlty gate on push.
    """

    consumer: str
    table: str
    per_market: bool
    column_aggregate: str
    calls_expr: str


SOURCES: tuple[BackfillSource, ...] = (
    BackfillSource(
        consumer="trend_analysis",
        table="trend_analysis",
        per_market=True,
        column_aggregate="SUM",
        calls_expr="COUNT(*)",
    ),
    BackfillSource(
        consumer="daily_summary",
        table="daily_summary",
        per_market=False,
        column_aggregate="SUM",
        calls_expr="COUNT(*)",
    ),
    BackfillSource(
        consumer="seed_insights",
        table="seed_insights",
        per_market=False,
        # One call, many seed rows. See the module docstring.
        column_aggregate="ANY_VALUE",
        calls_expr="1",
    ),
)


def source_for(consumer: str) -> BackfillSource:
    for s in SOURCES:
        if s.consumer == consumer:
            return s
    raise KeyError(f"no backfill source for consumer {consumer!r}")


def build_delete_sql(
    project: str, dataset: str, consumers: list[str], start: date, end: date
) -> str:
    """Clear this backfill's own rows in the window, so a re-run replaces them.

    Scoped to the named consumers: the live writers' rows for every other
    consumer must survive a re-run untouched. An empty consumer list is refused
    rather than widened into an unscoped DELETE over the whole ledger.
    """
    if not consumers:
        raise ValueError("refusing to build an unscoped DELETE over the gemini_usage ledger")
    names = ", ".join(f"'{c}'" for c in consumers)
    return f"""
DELETE FROM `{project}.{dataset}.{GEMINI_USAGE_TABLE}`
WHERE consumer IN ({names})
  AND trend_date >= DATE '{start.isoformat()}'
  AND trend_date <= DATE '{end.isoformat()}'
""".strip()


def build_insert_sql(
    source: BackfillSource, project: str, dataset: str, start: date, end: date
) -> str:
    """Fold one source table's history into ledger rows.

    Bounded on BOTH ends. An open upper bound would sweep in rows the cron is
    writing right now and double them against the live writer.
    """
    market_select = "market AS market" if source.per_market else "CAST(NULL AS STRING) AS market"
    group_by = (
        "GROUP BY day, market, gemini_model" if source.per_market else "GROUP BY day, gemini_model"
    )
    return f"""
INSERT INTO `{project}.{dataset}.{GEMINI_USAGE_TABLE}`
  (usage_id, trend_date, consumer, market, gemini_model,
   calls, prompt_tokens, completion_tokens, recorded_at)
SELECT
  GENERATE_UUID() AS usage_id,
  day AS trend_date,
  '{source.consumer}' AS consumer,
  market,
  gemini_model,
  calls,
  prompt_tokens,
  completion_tokens,
  CURRENT_TIMESTAMP() AS recorded_at
FROM (
  SELECT
    trend_date AS day,
    {market_select},
    gemini_model,
    {source.calls_expr} AS calls,
    {source.column_aggregate}(prompt_tokens) AS prompt_tokens,
    {source.column_aggregate}(completion_tokens) AS completion_tokens
  FROM `{project}.{dataset}.{source.table}`
  WHERE trend_date >= DATE '{start.isoformat()}'
    AND trend_date <= DATE '{end.isoformat()}'
    AND gemini_model IS NOT NULL
  {group_by}
)
""".strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill the gemini_usage ledger")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS, help=f"lookback days (default {DEFAULT_DAYS})"
    )
    args = parser.parse_args()
    apply = args.apply and not args.dry_run

    from src.utils.bigquery import get_client, get_dataset

    client = get_client()
    dataset = get_dataset()
    project = client.project
    end = datetime.now(UTC).date()
    start = end - timedelta(days=args.days)
    consumers = [s.consumer for s in SOURCES]

    print(f"Target: {project}.{dataset}.{GEMINI_USAGE_TABLE}")
    print(f"Window: {start.isoformat()} .. {end.isoformat()}  ({args.days}d)")
    print(f"Mode:   {'APPLY' if apply else 'dry-run'}")
    print()

    statements = [("DELETE", build_delete_sql(project, dataset, consumers, start, end))]
    statements += [
        (f"INSERT {s.consumer}", build_insert_sql(s, project, dataset, start, end)) for s in SOURCES
    ]

    for label, sql in statements:
        print(f"--- {label} ---")
        print(sql)
        if apply:
            job = client.query(sql)
            job.result()
            affected = job.num_dml_affected_rows
            print(f"APPLIED ({affected} rows)")
        else:
            print("(dry-run, not executed)")
        print()

    print("Backfill applied." if apply else "Dry run complete. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
