"""Idempotent BigQuery migration: add trend_analysis.render_payload.

The PULSE v2 email binds three artifacts the pipeline builds in memory and
never persisted: the per-brief display bundle (state badge, phase, window,
in-market share, the Seen-on channel weights, confidence, search read), the
comment-sentiment room fields, and the driving-hashtags list. Any send that
re-reads briefs from BigQuery (the resend workflow, a preview, a recovery
day) therefore rendered without them; on 10 Jun the recovery resend reached
the exec list with empty Seen-on strips and placeholder conversation slots.

render_payload is a single JSON string carrying ``{display,
comment_sentiment, comment_themes, driving_hashtags}``. One column keeps the
schema stable while the inner shapes evolve with the renderer. The display
part is written at insert time by generate_briefs; the conversation fields
are merged in by the pipeline after the producers run.

Safe to re-run; ADD COLUMN IF NOT EXISTS, no DEFAULT (BigQuery rejects
DEFAULT on populated tables).

Run modes::

    python scripts/migrations/add_render_payload_column.py --dry-run
    python scripts/migrations/add_render_payload_column.py --apply
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
        "trend_analysis.render_payload",
        "ALTER TABLE `{project}.{dataset}.trend_analysis` "
        "ADD COLUMN IF NOT EXISTS render_payload STRING",
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
