"""Phase 2.2 cross-trend summary: create the daily_summary BQ table.

Adds a new table that the second-pass Gemini cross-trend summary writes
to. One row per day. Persisted (not just in-memory) so the dashboard
can render a timeline of summaries and the operator can audit which
days produced strong narratives.

Idempotent via CREATE TABLE IF NOT EXISTS.

Run modes::

    python scripts/migrations/create_daily_summary_table.py --dry-run
    python scripts/migrations/create_daily_summary_table.py --apply
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

SCHEMA_FILE = (
    Path(__file__).resolve().parent.parent.parent
    / "infra"
    / "bigquery_schemas"
    / "daily_summary.sql"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Execute against BQ")
    parser.add_argument("--dry-run", action="store_true", help="Print only (default)")
    args = parser.parse_args()

    apply = args.apply and not args.dry_run

    client = get_client()
    dataset = get_dataset()
    project = client.project

    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    sql = sql.replace("{project}", project).replace("{dataset}", dataset)

    print(f"Target: {project}.{dataset}")
    print(f"Mode:   {'APPLY' if apply else 'dry-run'}")
    print()
    print(sql)
    print()

    if apply:
        client.query(sql).result()
        print("APPLIED")
    else:
        print("(dry-run, not executed)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
