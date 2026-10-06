"""Idempotent BigQuery migration: add gcam_score + gcam_rows to trend_scores.

GCAM-reader feature. The GDELT GKG connector already persists the raw emotional
dimensions on raw_content.v2gcam (Wave 0.2, live 2026-06-25). This migration
adds the two scored columns the reader writes:

  gcam_score FLOAT64   per-topic emotional-arousal intensity 0..1 (mean of the
                       Lexicoder positive/negative emotion dims, see
                       enrichment._parse_gcam_intensity)
  gcam_rows  INT64     count of GDELT rows that contributed gcam_intensity to
                       the topic, the present/absent gate compute_trend_scores
                       uses for weight redistribution

Ships with scoring.yaml gcam_score weight at 0.00, so the stored value is
computed but contributes nothing to trend_score until the weight is raised. The
columns must exist before the reader code ships: insert_dataframe does no schema
coercion, so a DataFrame column with no matching BQ column fails the load.

Per DEVELOPMENT.md gotcha, ADD COLUMN carries no DEFAULT (BigQuery rejects
DEFAULT on populated tables). Existing rows read both columns as NULL until the
next run writes a value. Safe to re-run; uses ADD COLUMN IF NOT EXISTS.

Run modes:

    python scripts/migrations/add_gcam_score_column.py --dry-run
    python scripts/migrations/add_gcam_score_column.py --apply
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
        "trend_scores.gcam_score",
        "ALTER TABLE `{project}.{dataset}.trend_scores` "
        "ADD COLUMN IF NOT EXISTS gcam_score FLOAT64",
    ),
    (
        "trend_scores.gcam_rows",
        "ALTER TABLE `{project}.{dataset}.trend_scores` ADD COLUMN IF NOT EXISTS gcam_rows INT64",
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
