"""Idempotent BigQuery migration: add seed_recommend to daily_summary.

generate_daily_summary.py writes a ``seed_recommend`` field (Jo, 22 Jun 2026:
the optional follow-vs-seed topline naming the topic best positioned to drive
Nanobanana/Lyria usage) into every daily_summary row. This migration adds the
column so a fresh-environment or disaster-recovery rebuild gets it the same way
every other post-cutover column did, and so the live insert has a target column.

Safe to re-run; uses ADD COLUMN IF NOT EXISTS. Per the DEVELOPMENT.md gotcha,
ADD COLUMN does NOT carry a DEFAULT clause because BigQuery rejects DEFAULT on
populated tables. Existing rows read seed_recommend as NULL; the render treats
NULL/empty as no topline line.

Run modes:

    python scripts/migrations/add_seed_recommend_column.py --dry-run
    python scripts/migrations/add_seed_recommend_column.py --apply
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
        "daily_summary.seed_recommend",
        "ALTER TABLE `{project}.{dataset}.daily_summary` "
        "ADD COLUMN IF NOT EXISTS seed_recommend STRING",
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

    if not apply:
        print("Dry run complete. Re-run with --apply to execute.")
    else:
        print("Migration applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
