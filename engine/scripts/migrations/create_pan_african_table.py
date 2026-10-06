"""Wave 2 pan-African stories: create the pan_african_stories BQ table.

Holds one row per cross-market story for a trend_date: a topic family that
is rising in two or more of the za/ng/ke markets at once. Written by the
standalone post-scoring stage in src/analysis/pan_african.py, gated behind
PAN_AFRICAN_ENABLED. Persisted so the email + Pulse can render a
pan-African section and the operator can audit which families travelled.

Schema is locked by the Wave 2 contract
(the 2026-06-19 wave 2 contract):

    trend_date        DATE
    story_id          STRING
    story_label       STRING
    markets           ARRAY<STRING>
    topic_keys        ARRAY<STRING>
    total_item_count  INT64
    momentum_composite FLOAT64

Idempotent via CREATE TABLE IF NOT EXISTS. Dry-run default.

Run modes::

    python scripts/migrations/create_pan_african_table.py --dry-run
    python scripts/migrations/create_pan_african_table.py --apply
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

CREATE_SQL = """\
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.pan_african_stories` (
  trend_date DATE NOT NULL,
  story_id STRING NOT NULL,
  story_label STRING,
  markets ARRAY<STRING>,
  topic_keys ARRAY<STRING>,
  total_item_count INT64,
  momentum_composite FLOAT64,
  computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
)
PARTITION BY trend_date
CLUSTER BY story_id
OPTIONS (
  description = 'Pan-African stories: topic families rising in 2+ markets on a trend_date'
)\
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Execute against BQ")
    parser.add_argument("--dry-run", action="store_true", help="Print only (default)")
    args = parser.parse_args()

    apply = args.apply and not args.dry_run

    client = get_client()
    dataset = get_dataset()
    project = client.project

    sql = CREATE_SQL.format(project=project, dataset=dataset)

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
