"""Idempotent BigQuery migration: n_factual / n_social on trend_scores (B1).

python scripts/migrations/add_corroboration_counts_columns.py --dry-run
python scripts/migrations/add_corroboration_counts_columns.py --apply
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

STATEMENTS = [
    (
        "trend_scores.n_factual",
        "ALTER TABLE `{project}.{dataset}.trend_scores` ADD COLUMN IF NOT EXISTS n_factual INT64",
    ),
    (
        "trend_scores.n_social",
        "ALTER TABLE `{project}.{dataset}.trend_scores` ADD COLUMN IF NOT EXISTS n_social INT64",
    ),
]


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

    for label, sql_template in STATEMENTS:
        sql = sql_template.format(project=project, dataset=dataset)
        print(f"--- {label} ---")
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
