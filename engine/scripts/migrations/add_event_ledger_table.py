"""Idempotent BigQuery migration: create the event_ledger table.

The Intelligence Core's EVENT-STATE LEDGER module lands one row per
(trend_date, market, entity_key) carrying the current resolved state of a
watched event. The table is new, so this migration creates it from the
committed schema file rather than altering an existing table. It reads
infra/bigquery_schemas/event_ledger.sql so the CREATE statement has a
single source of truth shared with scripts/setup_bigquery.py.

Safe to re-run; the schema uses CREATE TABLE IF NOT EXISTS, so a second
apply is a no-op and never drops or rewrites an existing table.

Run modes:

    python scripts/migrations/add_event_ledger_table.py --dry-run
    python scripts/migrations/add_event_ledger_table.py --apply
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

SCHEMA_FILE = (
    Path(__file__).resolve().parent.parent.parent
    / "infra"
    / "bigquery_schemas"
    / "event_ledger.sql"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Execute against BQ")
    parser.add_argument("--dry-run", action="store_true", help="Print statements only (default)")
    args = parser.parse_args()

    apply = args.apply and not args.dry_run

    client = get_client()
    dataset = get_dataset()
    project = client.project

    sql = SCHEMA_FILE.read_text().replace("{project}", project).replace("{dataset}", dataset)

    print(f"Target: {project}.{dataset}")
    print(f"Mode:   {'APPLY' if apply else 'dry-run'}")
    print()
    print("--- event_ledger (CREATE TABLE IF NOT EXISTS) ---")
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
