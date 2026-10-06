#!/usr/bin/env python3
"""Phase 1a gate: verify the 42 service account can write research_artifacts in BigQuery.

Dry insert + delete of one test row. Exits 0 on success.

If local credentials cannot reach BQ or lack IAM, prints the GCS fallback path
(RESEARCH_PERSIST_BACKEND=gcs) and exits 1 so Phase 2 deploy notes the blocker.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

env_path = Path(__file__).resolve().parents[1] / ".env"
try:
    from dotenv import load_dotenv

    if env_path.exists():
        load_dotenv(env_path, override=False)
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api import bq  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify research_artifacts BQ IAM")
    parser.add_argument("--apply", action="store_true", help="Run insert/delete (default dry-run)")
    args = parser.parse_args()

    project = os.environ.get("GCP_PROJECT", "ogilvy-trends-v2")
    dataset = os.environ.get("RESEARCH_BQ_DATASET", os.environ.get("BQ_DATASET", "trends_v2_dev"))
    table = os.environ.get("RESEARCH_ARTIFACTS_TABLE", "research_artifacts")

    print(f"Target: {project}.{dataset}.{table}")
    print(f"Mode: {'apply' if args.apply else 'dry-run'}")

    if not args.apply:
        print()
        print("Pass --apply with active Application Default Credentials to test insert/delete.")
        print("GCS fallback: set RESEARCH_PERSIST_BACKEND=gcs (listening-post-cache bucket).")
        return 0

    test_id = "iam_probe_" + uuid.uuid4().hex[:12]
    row = {
        "artifact_id": test_id,
        "persona_id": "audience_neutral",
        "markets": ["ke"],
        "trend_date": datetime.now(UTC).date().isoformat(),
        "status": "probe",
        "quality": {"level": "thin"},
        "product_frame": "probe",
        "evidence": [],
        "synthesis": {},
        "markdown": "iam probe",
        "degraded_reason": "",
    }

    try:
        bq.insert_research_artifact(row)
        deleted = bq.delete_research_artifact(test_id)
    except Exception as exc:
        print(f"IAM probe failed: {exc}")
        print("Use GCS fallback: RESEARCH_PERSIST_BACKEND=gcs")
        return 1

    if not deleted:
        print("Insert succeeded but delete failed; check bigquery.tables.updateData IAM")
        return 1

    print(f"OK: inserted and deleted {test_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
