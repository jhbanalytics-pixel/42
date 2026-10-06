"""Idempotent BigQuery migration: add corroboration columns to trend_scores.

Phase 0 of the PULSE Intelligence Core adds a deterministic corroboration
scorer. run_rss_now.py now writes four shadow columns into every trend_scores
MERGE: two honest corroboration numbers (factual vs social, never combined), a
confidence tier, and a recency in hours. The columns are inert (nothing reads
them yet) and trend_score is unchanged; this migration only makes the table able
to hold them.

Safe to re-run; uses ADD COLUMN IF NOT EXISTS. Per the DEVELOPMENT.md gotcha,
ADD COLUMN does NOT carry a DEFAULT clause because BigQuery rejects DEFAULT on
populated tables. Existing rows read the new columns as NULL until the next run
(any reader must COALESCE the NULL).

Run modes:

    python scripts/migrations/add_corroboration_columns.py --dry-run
    python scripts/migrations/add_corroboration_columns.py --apply
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
        "trend_scores.factual_corroboration",
        "ALTER TABLE `{project}.{dataset}.trend_scores` "
        "ADD COLUMN IF NOT EXISTS factual_corroboration FLOAT64",
    ),
    (
        "trend_scores.social_corroboration",
        "ALTER TABLE `{project}.{dataset}.trend_scores` "
        "ADD COLUMN IF NOT EXISTS social_corroboration FLOAT64",
    ),
    (
        "trend_scores.confidence_tier",
        "ALTER TABLE `{project}.{dataset}.trend_scores` "
        "ADD COLUMN IF NOT EXISTS confidence_tier STRING",
    ),
    (
        "trend_scores.corroboration_recency_hours",
        "ALTER TABLE `{project}.{dataset}.trend_scores` "
        "ADD COLUMN IF NOT EXISTS corroboration_recency_hours INT64",
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
