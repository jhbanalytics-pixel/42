"""Idempotent BigQuery migration: add enriched_content near-miss columns (A7).

python scripts/migrations/add_near_miss_columns.py --dry-run
python scripts/migrations/add_near_miss_columns.py --apply
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

COLUMNS = [
    ("enriched_content.near_topic", "enriched_content", "near_topic", "STRING"),
    ("enriched_content.near_cosine", "enriched_content", "near_cosine", "FLOAT64"),
]

STATEMENT_TEMPLATE = (
    "ALTER TABLE `{project}.{dataset}.{table}` ADD COLUMN IF NOT EXISTS {column} {col_type}"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    apply = args.apply and not args.dry_run

    client = get_client()
    dataset = get_dataset()
    project = client.project

    print(f"Target: {project}.{dataset}")
    print(f"Mode:   {'APPLY' if apply else 'dry-run'}")
    print()

    for label, table, column, col_type in COLUMNS:
        sql = STATEMENT_TEMPLATE.format(
            project=project,
            dataset=dataset,
            table=table,
            column=column,
            col_type=col_type,
        )
        print(f"--- {label} ---")
        print(sql)
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
