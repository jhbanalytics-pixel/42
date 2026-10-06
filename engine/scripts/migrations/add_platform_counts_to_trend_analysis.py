"""Phase 2 brief v2.1: surface per-platform content counts on each brief.

Adds one column requested by Thapelo (2026-05-05) so the email card can
render 'TikTok (124) | Instagram Reels (87)' instead of a bare platform
list. The data already exists in enriched_content; the orchestrator now
groups it per (market, topic_group, platform) and stores it alongside
the Gemini-generated brief.

  * platform_counts ARRAY<STRING>   - 'platform | N items' entries

Safe to re-run; uses ADD COLUMN IF NOT EXISTS. Per DEVELOPMENT.md gotcha
the statements omit DEFAULT because BigQuery rejects DEFAULT on
populated tables.

Run modes::

    python scripts/migrations/add_platform_counts_to_trend_analysis.py --dry-run
    python scripts/migrations/add_platform_counts_to_trend_analysis.py --apply
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
        "trend_analysis.platform_counts",
        "ALTER TABLE `{project}.{dataset}.trend_analysis` "
        "ADD COLUMN IF NOT EXISTS platform_counts ARRAY<STRING>",
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
