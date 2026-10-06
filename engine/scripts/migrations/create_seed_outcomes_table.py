"""Idempotent BigQuery migration: create seed_outcomes table.

Run modes:
    python scripts/migrations/create_seed_outcomes_table.py --dry-run
    python scripts/migrations/create_seed_outcomes_table.py --apply
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

SCHEMA_FILE = repo_root / "infra" / "bigquery_schemas" / "seed_outcomes.sql"


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
    print("--- seed_outcomes (CREATE TABLE IF NOT EXISTS) ---")
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
