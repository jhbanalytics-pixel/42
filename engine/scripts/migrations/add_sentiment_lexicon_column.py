"""Idempotent BigQuery migration: add enriched_content.sentiment_lexicon_score.

Wave 2 (19 Jun 2026) ships the social sentiment lexicon scorer dark. This
migration adds the one column it writes, ahead of the code that populates it,
per the Wave 2 contract
(the 2026-06-19 wave 2 contract). Mirrors the
add_wave1_columns.py pattern: one ALTER, ADD COLUMN IF NOT EXISTS, safe to
re-run.

Column added:

  enriched_content
    sentiment_lexicon_score  FLOAT64  per-row social sentiment, -1.0..1.0.
                                      NULL when SENTIMENT_LEXICON_ENABLED is off
                                      or the row is not a scored social row.

No view change. v_pipeline_health surfaces connector row counts only, not
per-row enrichment signals, so it does not read this column.

Per DEVELOPMENT.md gotcha, ADD COLUMN does NOT carry a DEFAULT clause because
BigQuery rejects DEFAULT on populated tables. Existing rows read the new column
as NULL until the next run writes it.

Run modes:

    python scripts/migrations/add_sentiment_lexicon_column.py --dry-run
    python scripts/migrations/add_sentiment_lexicon_column.py --apply
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
    (
        "enriched_content.sentiment_lexicon_score",
        "enriched_content",
        "sentiment_lexicon_score",
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
