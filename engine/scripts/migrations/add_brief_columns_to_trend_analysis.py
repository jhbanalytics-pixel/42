"""Idempotent BigQuery migration: extend trend_analysis with brief fields.

Adds three columns the Phase 2 trend brief renderer needs that the
existing schema does not yet carry:

  * platforms ARRAY<STRING>          -- primary platforms per topic
  * sentiment_summary STRING         -- short tone read
  * status_tag STRING                -- "Key" or "Rising"

Three brief fields map onto existing columns:
  * description_rationale -> trend_synthesis
  * activation_idea       -> cultural_context
  * key_metrics           -> campaign_angles (already ARRAY<STRING>)

Safe to re-run; uses ADD COLUMN IF NOT EXISTS. Per DEVELOPMENT.md gotcha
the statements omit DEFAULT because BigQuery rejects DEFAULT on
populated tables.

Run modes::

    python scripts/migrations/add_brief_columns_to_trend_analysis.py --dry-run
    python scripts/migrations/add_brief_columns_to_trend_analysis.py --apply
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
        "trend_analysis.platforms",
        "ALTER TABLE `{project}.{dataset}.trend_analysis` "
        "ADD COLUMN IF NOT EXISTS platforms ARRAY<STRING>",
    ),
    (
        "trend_analysis.sentiment_summary",
        "ALTER TABLE `{project}.{dataset}.trend_analysis` "
        "ADD COLUMN IF NOT EXISTS sentiment_summary STRING",
    ),
    (
        "trend_analysis.status_tag",
        "ALTER TABLE `{project}.{dataset}.trend_analysis` "
        "ADD COLUMN IF NOT EXISTS status_tag STRING",
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
