"""Idempotent BigQuery migration: create the system_events table.

The system observability feature writes one append-only row per notable system
event (cron runs, digest failures, connector failures, Listening Post chat
turns + errors) so there is one queryable history across the stack. This
migration creates the table from infra/bigquery_schemas/system_events.sql,
which uses CREATE TABLE IF NOT EXISTS, so it is safe to re-run.

Run modes:

    python scripts/migrations/create_system_events_table.py --dry-run
    python scripts/migrations/create_system_events_table.py --apply
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

SCHEMA_FILE = repo_root / "infra" / "bigquery_schemas" / "system_events.sql"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Execute against BQ")
    parser.add_argument("--dry-run", action="store_true", help="Print statements only (default)")
    args = parser.parse_args()

    apply = args.apply and not args.dry_run

    client = get_client()
    dataset = get_dataset()
    project = client.project

    sql = SCHEMA_FILE.read_text(encoding="utf-8").format(project=project, dataset=dataset)

    print(f"Target: {project}.{dataset}")
    print(f"Mode:   {'APPLY' if apply else 'dry-run'}")
    print()
    print("--- system_events (CREATE TABLE IF NOT EXISTS) ---")
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
