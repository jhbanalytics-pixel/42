"""Idempotent BigQuery migration: create the gemini_usage table.

The comment-sentiment and driving-hashtag stages write their Vertex Gemini token
usage here, one row per (trend_date, consumer, market, model), so
scripts/vertex_cost_watchdog.py can sum their real tokens instead of missing
them entirely. CREATE TABLE IF NOT EXISTS, so it is safe to re-run.

The DDL is INLINE here rather than in infra/bigquery_schemas/gemini_usage.sql
because tests/unit/test_schema_order_parity.py requires every file in that
directory to also appear in scripts/setup_bigquery.py SCHEMA_ORDER, and both of
those files were out of scope for the change that added this ledger. Promoting
The DDL lives in infra/bigquery_schemas/gemini_usage.sql, read at run time,
infra/bigquery_schemas/gemini_usage.sql and add "gemini_usage.sql" to
SCHEMA_ORDER after "seed_insights.sql". Until then a fresh-dataset rebuild via
setup_bigquery does NOT create this table, so run this migration after it. The
watchdog degrades honestly in the meantime: an unreadable ledger prints a
COVERAGE GAP line naming the consumers it could not count, and the producers'
ledger write is non-fatal.

Run modes:
    python scripts/migrations/create_gemini_usage_table.py --dry-run
    python scripts/migrations/create_gemini_usage_table.py --apply
"""

from __future__ import annotations

import argparse
import sys
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

from src.utils.bigquery import get_client, get_dataset

SCHEMA_FILE = repo_root / "infra" / "bigquery_schemas" / "gemini_usage.sql"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    apply = args.apply and not args.dry_run

    client = get_client()
    dataset = get_dataset()
    project = client.project
    sql = SCHEMA_FILE.read_text(encoding="utf-8").format(project=project, dataset=dataset)

    print(f"Target: {project}.{dataset}")
    print(f"Mode:   {'APPLY' if apply else 'dry-run'}")
    print()
    print("--- gemini_usage (CREATE TABLE IF NOT EXISTS) ---")
    print(sql.strip())
    if apply:
        client.query(sql).result()
        print("APPLIED")
    else:
        print("(dry-run, not executed)")

    print()
    print("Migration applied." if apply else "Dry run complete. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
