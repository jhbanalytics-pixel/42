"""Phase 2 brief v2.2: add visual_anchor column to trend_analysis.

Visual anchor is the one-sentence visual fingerprint extracted by Gemini
from the sample post text in the same call as the rest of the brief
fields. It grounds the nano_banana_prompt in concrete visual elements
(setting, outfit, prop, framing) rather than abstract topic concepts,
improving the Zero-Edit ready quality of paste-ready prompts the brief
mandates.

Idempotent ADD COLUMN IF NOT EXISTS, no DEFAULT (BQ rejects DEFAULT on
populated tables).

Run modes::

    python scripts/migrations/add_visual_anchor_to_trend_analysis.py --dry-run
    python scripts/migrations/add_visual_anchor_to_trend_analysis.py --apply
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
        "trend_analysis.visual_anchor",
        "ALTER TABLE `{project}.{dataset}.trend_analysis` "
        "ADD COLUMN IF NOT EXISTS visual_anchor STRING",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
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
