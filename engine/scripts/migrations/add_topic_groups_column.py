"""Idempotent BigQuery migration: add topic_groups + unclassified_rows columns.

Adds:
  * enriched_content.topic_groups ARRAY<STRING>
  * pipeline_runs.unclassified_rows INT64

Safe to re-run; uses ADD COLUMN IF NOT EXISTS. Per DEVELOPMENT.md gotcha the
statements omit DEFAULT because BigQuery rejects DEFAULT on populated
tables.

Run mode flags:
  --dry-run    Print statements without executing (DEFAULT)
  --apply      Actually execute against the live dataset

Example:
    python scripts/migrations/add_topic_groups_column.py --dry-run
    python scripts/migrations/add_topic_groups_column.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Load .env before importing BQ client
env_path = Path(__file__).resolve().parent.parent.parent / ".env"
try:
    from dotenv import load_dotenv

    if env_path.exists():
        load_dotenv(env_path, override=False)
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.utils.bigquery import get_client, get_dataset

STATEMENTS = [
    (
        "enriched_content",
        "ALTER TABLE `{project}.{dataset}.enriched_content` "
        "ADD COLUMN IF NOT EXISTS topic_groups ARRAY<STRING>",
    ),
    (
        "pipeline_runs",
        "ALTER TABLE `{project}.{dataset}.pipeline_runs` "
        "ADD COLUMN IF NOT EXISTS unclassified_rows INT64",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Execute against BQ")
    parser.add_argument("--dry-run", action="store_true", help="Print statements only (default)")
    args = parser.parse_args()

    apply = args.apply and not args.dry_run

    client = get_client()
    dataset = get_dataset()
    project = client.project

    print(f"Target: {project}.{dataset}")
    print()

    for table, sql_template in STATEMENTS:
        sql = sql_template.format(project=project, dataset=dataset)
        print(f"--- {table} ---")
        print(sql)
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
