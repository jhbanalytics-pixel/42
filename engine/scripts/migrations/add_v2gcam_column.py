"""Idempotent BigQuery migration: add v2gcam to raw_content and enriched_content.

Wave 0.2 (GDELT GCAM emotion). Adds the STRING column that holds the parsed
GCAM emotional-cognitive dimensions (the "code:value" string returned by the
GDELT connector's _parse_gcam path) so that when gdelt.gcam_enabled flips to
true the value has somewhere to land. Ships DARK: the flag stays false, the
connector keeps writing empty/None into the field, and adding the column now
is the safety net that stops the WRITE_APPEND load from failing the day the
flag flips. insert_dataframe does no schema coercion, so a DataFrame column
with no matching BQ column fails the load outright; the column must exist on
both tables first.

The column lands on BOTH raw_content and enriched_content because the live
loader (scripts/run_rss_now.py) carries the raw row dict into the enriched
frame via ``**base``, so v2gcam reaches both tables on the same run.

Safe to re-run; uses ADD COLUMN IF NOT EXISTS.

Per DEVELOPMENT.md gotcha, ADD COLUMN does NOT carry a DEFAULT clause because
BigQuery rejects DEFAULT on populated tables. Existing rows read v2gcam as
NULL until the next run writes a value.

Run modes:

    python scripts/migrations/add_v2gcam_column.py --dry-run
    python scripts/migrations/add_v2gcam_column.py --apply
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
        "raw_content.v2gcam",
        "ALTER TABLE `{project}.{dataset}.raw_content` ADD COLUMN IF NOT EXISTS v2gcam STRING",
    ),
    (
        "enriched_content.v2gcam",
        "ALTER TABLE `{project}.{dataset}.enriched_content` ADD COLUMN IF NOT EXISTS v2gcam STRING",
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
