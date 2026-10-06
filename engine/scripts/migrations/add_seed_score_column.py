"""Idempotent BigQuery migration: add seed_score to trend_scores.

run_rss_now.py writes a ``seed_score`` field (Jo, 22 Jun 2026: a 0..1
worth-seeding-for-Nanobanana/Lyria signal built from audience + creative-format
fit, independent of trend_score) into every trend_scores MERGE. This migration
adds the column so a fresh-environment or disaster-recovery rebuild gets it the
same way every other post-cutover column did, and so the live MERGE has a target
column to write into.

Safe to re-run; uses ADD COLUMN IF NOT EXISTS. Per the DEVELOPMENT.md gotcha,
ADD COLUMN does NOT carry a DEFAULT clause because BigQuery rejects DEFAULT on
populated tables. Existing rows read seed_score as NULL until the next run; the
brief bridge and the card chip both treat NULL/absent as no-chip.

Run modes:

    python scripts/migrations/add_seed_score_column.py --dry-run
    python scripts/migrations/add_seed_score_column.py --apply
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
        "trend_scores.seed_score",
        "ALTER TABLE `{project}.{dataset}.trend_scores` "
        "ADD COLUMN IF NOT EXISTS seed_score FLOAT64",
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
