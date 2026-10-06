"""Idempotent BigQuery migration: create the reconcile_actions table.

RECONCILE writes one row per claim decision (corroborated / stale / labelled /
no_match) into ``reconcile_actions``. A fresh-environment create gets the table
via setup_bigquery.py; this migration is the in-place path for the live dataset
so the flag-gated shadow run has a target table before it is wired in.

Safe to re-run: CREATE TABLE IF NOT EXISTS. The DDL is sourced from the
committed schema file so the migration and the fresh-create stay byte-identical.

Run modes:

    python scripts/migrations/add_reconcile_actions_table.py --dry-run
    python scripts/migrations/add_reconcile_actions_table.py --apply
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
    / "reconcile_actions.sql"
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

    sql = SCHEMA_FILE.read_text(encoding="utf-8").format(project=project, dataset=dataset)

    print(f"Target: {project}.{dataset}")
    print(f"Mode:   {'APPLY' if apply else 'dry-run'}")
    print()
    print("--- reconcile_actions ---")
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
