"""Idempotent BigQuery migration: add trend_analysis.headline.

The Gemini brief already returns a one-line editorial headline (required in
RESPONSE_SCHEMA) and generate_briefs reads it into TopicBrief.headline, but
to_bq_row never persisted it. So a stored brief carried no headline and any
reader off trend_analysis (the PULSE preview, the test send, a dashboard) fell
back to the humanised topic slug, even though the live cron v2 email already
renders the real headline in-memory. This adds the column so the headline is
first-class, auditable, and visible in a preview.

Safe to re-run; ADD COLUMN IF NOT EXISTS, no DEFAULT (BigQuery rejects DEFAULT
on populated tables).

Run modes::

    python scripts/migrations/add_headline_column_to_trend_analysis.py --dry-run
    python scripts/migrations/add_headline_column_to_trend_analysis.py --apply
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
        "trend_analysis.headline",
        "ALTER TABLE `{project}.{dataset}.trend_analysis` ADD COLUMN IF NOT EXISTS headline STRING",
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
