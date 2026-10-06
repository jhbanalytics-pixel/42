"""The ingest run reader expects the digests the ingest job definition stamps.

``infra/runtime/ingest-staging.json`` at the repository root is the job definition the
reader's expected policy and profile digests are pinned to, and the engine build context
does not carry it. The check therefore lives here, where the publish build runs it over
the checkout, rather than in ``tests/unit``, which the engine test image runs over its
own copy of the engine tree.
"""

import json
from pathlib import Path

from scripts.staging import read_ingest_run as tool

REPO = Path(__file__).resolve().parents[3]


# Expectations pinned to the job definition. The entry point profile and the
# reconciled policy are held to the same digests in tests/unit/test_read_ingest_run.py.


def test_expected_digests_are_the_ingest_job_definition_and_entry_point_profile():
    job = json.loads((REPO / "infra/runtime/ingest-staging.json").read_text())
    env = {e["name"]: e["value"] for e in job["jobs"]["intelligence-42-ingest-staging"]["env"]}
    assert env["COLLECTION_POLICY_SHA256"] == tool.EXPECTED_POLICY_SHA256
    assert env["COLLECTION_PROFILE_SHA256"] == tool.EXPECTED_PROFILE_SHA256
    assert tool.EXPECTED_POLICY_SHA256.startswith("ff9c8656")
    assert tool.EXPECTED_PROFILE_SHA256.startswith("c300e0f6")
    assert tool.RECEIPT_TABLE_ID == (
        "ogilvy-trends-v2.intelligence_42_sources_staging.collection_receipts"
    )
    assert tool.RAW_TABLE_ID == "ogilvy-trends-v2.intelligence_42_sources_staging.raw_content"
