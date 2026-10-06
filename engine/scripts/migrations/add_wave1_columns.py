"""Idempotent BigQuery migration: add all Wave 1 columns.

Wave 1 (19 Jun 2026) ships six compute features dark. This single migration
adds every new column the engine writes for them, ahead of the code that
populates them, per the Wave 1 contract
(the 2026-06-19 wave 1 contract). Mirrors the
add_reddit_rows_column.py pattern: one ALTER per column, ADD COLUMN IF NOT
EXISTS, safe to re-run.

Columns added:

  trend_scores
    velocity_score_7d     FLOAT64  7-day momentum read
    velocity_score_30d    FLOAT64  30-day momentum read
    momentum_label        STRING   rising / building / steady / cooling
    lifecycle_phase       STRING   birth / growth / maturity / decline
    continuity_day        INT64    1 on first appearance, increments daily
    continuity_state      STRING   new / day2 / day3plus / rebounding

  enriched_content
    classification_layer  STRING   winning layer: brand24 / regex / gdelt /
                                   slang / embedding / unclassified

  pipeline_runs
    classification_brand24_rows    INT64
    classification_regex_rows      INT64
    classification_gdelt_rows      INT64
    classification_slang_rows      INT64
    classification_embedding_rows  INT64
    labelling_rate_percent         FLOAT64

No view change. The platform heat-map adds no column (it aggregates the
existing per-brief platform_counts), and v_pipeline_health surfaces connector
row counts only, not classification instrumentation, so it does not need these
new pipeline_runs columns.

Per DEVELOPMENT.md gotcha, ADD COLUMN does NOT carry a DEFAULT clause because
BigQuery rejects DEFAULT on populated tables. Existing rows read every new
column as NULL until the next run writes them.

Run modes:

    python scripts/migrations/add_wave1_columns.py --dry-run
    python scripts/migrations/add_wave1_columns.py --apply
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

# (label, table, column, type) tuples. One ALTER each, ADD COLUMN IF NOT
# EXISTS so the migration is idempotent and partial re-runs are safe.
COLUMNS = [
    ("trend_scores.velocity_score_7d", "trend_scores", "velocity_score_7d", "FLOAT64"),
    ("trend_scores.velocity_score_30d", "trend_scores", "velocity_score_30d", "FLOAT64"),
    ("trend_scores.momentum_label", "trend_scores", "momentum_label", "STRING"),
    ("trend_scores.lifecycle_phase", "trend_scores", "lifecycle_phase", "STRING"),
    ("trend_scores.continuity_day", "trend_scores", "continuity_day", "INT64"),
    ("trend_scores.continuity_state", "trend_scores", "continuity_state", "STRING"),
    (
        "enriched_content.classification_layer",
        "enriched_content",
        "classification_layer",
        "STRING",
    ),
    (
        "pipeline_runs.classification_brand24_rows",
        "pipeline_runs",
        "classification_brand24_rows",
        "INT64",
    ),
    (
        "pipeline_runs.classification_regex_rows",
        "pipeline_runs",
        "classification_regex_rows",
        "INT64",
    ),
    (
        "pipeline_runs.classification_gdelt_rows",
        "pipeline_runs",
        "classification_gdelt_rows",
        "INT64",
    ),
    (
        "pipeline_runs.classification_slang_rows",
        "pipeline_runs",
        "classification_slang_rows",
        "INT64",
    ),
    (
        "pipeline_runs.classification_embedding_rows",
        "pipeline_runs",
        "classification_embedding_rows",
        "INT64",
    ),
    (
        "pipeline_runs.labelling_rate_percent",
        "pipeline_runs",
        "labelling_rate_percent",
        "FLOAT64",
    ),
]

STATEMENT_TEMPLATE = (
    "ALTER TABLE `{project}.{dataset}.{table}` ADD COLUMN IF NOT EXISTS {column} {col_type}"
)


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

    if not apply:
        print("Dry run complete. Re-run with --apply to execute.")
    else:
        print("Migration applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
