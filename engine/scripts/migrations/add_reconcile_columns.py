"""Idempotent BigQuery migration: add reconcile columns to trend_analysis.

When the reconcile shadow path is wired in, each brief carries the reconciled
read of its own claims back onto the trend_analysis row: the corrected text (or
the original when nothing changed), the engine's confidence, the corroboration
sources that backed any correction, and a free-text note trail. These let the
dashboard and the email render the reconciled line without re-running the
engine.

Safe to re-run: ADD COLUMN IF NOT EXISTS. Per the DEVELOPMENT.md gotcha, no
DEFAULT clause (BigQuery rejects DEFAULT on populated tables); existing rows
read the new columns as NULL until the next reconcile run.

Run modes:

    python scripts/migrations/add_reconcile_columns.py --dry-run
    python scripts/migrations/add_reconcile_columns.py --apply
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
        "trend_analysis.reconciled_read",
        "ALTER TABLE `{project}.{dataset}.trend_analysis` "
        "ADD COLUMN IF NOT EXISTS reconciled_read STRING",
    ),
    (
        "trend_analysis.reconcile_confidence",
        "ALTER TABLE `{project}.{dataset}.trend_analysis` "
        "ADD COLUMN IF NOT EXISTS reconcile_confidence FLOAT64",
    ),
    (
        "trend_analysis.corroboration_sources",
        "ALTER TABLE `{project}.{dataset}.trend_analysis` "
        "ADD COLUMN IF NOT EXISTS corroboration_sources STRING",
    ),
    (
        "trend_analysis.reconcile_notes",
        "ALTER TABLE `{project}.{dataset}.trend_analysis` "
        "ADD COLUMN IF NOT EXISTS reconcile_notes STRING",
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
