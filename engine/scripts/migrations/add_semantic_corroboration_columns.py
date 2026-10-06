"""Idempotent BigQuery migration: add semantic-corroboration shadow columns.

Forward Phase 2 (semantic corroboration). Today the cross-source multiplier is a
pure count of distinct channel families (channel_diversity), so a keyword that
splatters across families by coincidence scores the same as a genuinely
corroborated entity. This migration adds two shadow columns the aggregator
writes:

  semantic_corroboration_families INT64   the most channel families that any
                                          single shared entity (a hashtag, GDELT
                                          person, or org named on the row)
                                          reached in the topic. 1 means no entity
                                          crossed a family boundary; >= 2 means a
                                          genuinely cross-family corroborated
                                          entity exists.
  semantic_corroboration_entities INT64   how many distinct entities spanned
                                          >= 2 channel families in the topic.

Both are shadow: nothing reads them and trend_score is unchanged. They are
stored so the structural channel_diversity multiplier can be compared against a
semantic measure over a roughly 10-day window before any promotion.

The columns must exist before the writer code ships: insert_dataframe does no
schema coercion, so a DataFrame column with no matching BQ column fails the load.

Per DEVELOPMENT.md gotcha, ADD COLUMN carries no DEFAULT (BigQuery rejects
DEFAULT on populated tables). Existing rows read both columns as NULL until the
next run writes a value. Safe to re-run; uses ADD COLUMN IF NOT EXISTS.

Run modes:

    python scripts/migrations/add_semantic_corroboration_columns.py --dry-run
    python scripts/migrations/add_semantic_corroboration_columns.py --apply
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
        "trend_scores.semantic_corroboration_families",
        "ALTER TABLE `{project}.{dataset}.trend_scores` "
        "ADD COLUMN IF NOT EXISTS semantic_corroboration_families INT64",
    ),
    (
        "trend_scores.semantic_corroboration_entities",
        "ALTER TABLE `{project}.{dataset}.trend_scores` "
        "ADD COLUMN IF NOT EXISTS semantic_corroboration_entities INT64",
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
