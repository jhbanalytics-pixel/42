"""Phase 2 brief v2: extend trend_analysis with paste-ready prompt fields.

Adds four columns that the brief-aligned digest needs (Workstream E,
2026-05-05) and that the Phase 2 v1 schema does not yet carry:

  * nano_banana_prompt STRING       - paste-ready Gemini image prompt
  * lyria_prompt STRING             - paste-ready Lyria audio prompt
  * top_creators ARRAY<STRING>      - up to 5 entries, '@handle | platform | mentions'
  * social_refs ARRAY<STRING>       - up to 5 entries, 'url | platform | title'
  * b24_sentiment_trajectory STRING - per-project Brand24 sentiment trend label

Safe to re-run; uses ADD COLUMN IF NOT EXISTS. Per DEVELOPMENT.md gotcha
the statements omit DEFAULT because BigQuery rejects DEFAULT on
populated tables.

Run modes::

    python scripts/migrations/add_brief_v2_columns_to_trend_analysis.py --dry-run
    python scripts/migrations/add_brief_v2_columns_to_trend_analysis.py --apply
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
        "trend_analysis.nano_banana_prompt",
        "ALTER TABLE `{project}.{dataset}.trend_analysis` "
        "ADD COLUMN IF NOT EXISTS nano_banana_prompt STRING",
    ),
    (
        "trend_analysis.lyria_prompt",
        "ALTER TABLE `{project}.{dataset}.trend_analysis` "
        "ADD COLUMN IF NOT EXISTS lyria_prompt STRING",
    ),
    (
        "trend_analysis.top_creators",
        "ALTER TABLE `{project}.{dataset}.trend_analysis` "
        "ADD COLUMN IF NOT EXISTS top_creators ARRAY<STRING>",
    ),
    (
        "trend_analysis.social_refs",
        "ALTER TABLE `{project}.{dataset}.trend_analysis` "
        "ADD COLUMN IF NOT EXISTS social_refs ARRAY<STRING>",
    ),
    (
        "trend_analysis.b24_sentiment_trajectory",
        "ALTER TABLE `{project}.{dataset}.trend_analysis` "
        "ADD COLUMN IF NOT EXISTS b24_sentiment_trajectory STRING",
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
