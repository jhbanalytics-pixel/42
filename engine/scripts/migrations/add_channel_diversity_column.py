"""Idempotent BigQuery migration: add channel_diversity to trend_scores.

run_rss_now.py writes a ``channel_diversity`` field (the count of distinct
channel families backing a topic, feeding the cross-source multiplier) into
every trend_scores MERGE, but the column was added to the live table via an
unscripted ALTER and never captured in a committed migration. This script
closes that gap so a fresh-environment or disaster-recovery rebuild gets the
column the same way every other post-cutover column did.

The six Wave 1 trend_scores columns already ship via add_wave1_columns.py;
this migration adds only channel_diversity, which had no migration anywhere.

Safe to re-run; uses ADD COLUMN IF NOT EXISTS. Per the DEVELOPMENT.md gotcha,
ADD COLUMN does NOT carry a DEFAULT clause because BigQuery rejects DEFAULT on
populated tables. Existing rows read channel_diversity as NULL until the next
run (accuracy_watchdog and momentum_calibration already COALESCE the NULL).

Run modes:

    python scripts/migrations/add_channel_diversity_column.py --dry-run
    python scripts/migrations/add_channel_diversity_column.py --apply
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
        "trend_scores.channel_diversity",
        "ALTER TABLE `{project}.{dataset}.trend_scores` "
        "ADD COLUMN IF NOT EXISTS channel_diversity INT64",
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
