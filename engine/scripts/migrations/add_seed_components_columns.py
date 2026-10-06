"""Idempotent BigQuery migration: add the seed-score component columns.

The seeding journey's clickable breakdown shows WHY a trend scores what it does:
audience fit, format fit, brand safety, and the visual/audio share. The engine
now persists those components on trend_scores so the tool reads the same numbers
the score was built from. This migration adds the four FLOAT64 columns.

Safe to re-run; ADD COLUMN IF NOT EXISTS, no DEFAULT (BigQuery rejects DEFAULT on
populated tables). Existing rows read NULL until backfilled or the next cron.

Run modes:
    python scripts/migrations/add_seed_components_columns.py --dry-run
    python scripts/migrations/add_seed_components_columns.py --apply
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

_COLS = ["seed_audience_fit", "seed_format_fit", "seed_safety_gate", "visual_audio_share"]
STATEMENTS = [
    (
        f"trend_scores.{col}",
        "ALTER TABLE `{project}.{dataset}.trend_scores` "
        f"ADD COLUMN IF NOT EXISTS {col} FLOAT64",
    )
    for col in _COLS
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
