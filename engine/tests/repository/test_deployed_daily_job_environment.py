"""The deployed daily job's environment resolves the staging collection target.

``infra/runtime/daily-staging.json`` at the repository root is what the managed
runtime applies to the Cloud Run jobs, and the engine build context does not
carry it. The check therefore lives here, where the publish build runs it over
the checkout, rather than in ``tests/unit``, which the engine test image runs
over its own copy of the engine tree.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.analysis.open_intelligence import daily_native_clients as native
from src.utils.bigquery import get_dataset

from tests.unit.test_daily_stages import POLICY_DIGEST

ENGINE_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_FILE = ENGINE_ROOT.parent / "infra" / "runtime" / "daily-staging.json"


def test_deployed_daily_job_environment_resolves_the_staging_collection_target(monkeypatch):
    config = json.loads(RUNTIME_FILE.read_bytes())
    entries = config["jobs"]["intelligence-42-daily-staging"]["env"]
    assert entries == [
        {"name": "TRENDS_ENV", "value": "staging"},
        {"name": "BIGQUERY_DATASET", "value": "intelligence_42_sources_staging"},
    ]
    assert config["jobs"]["intelligence-42-price-policy-staging"]["env"] == []
    monkeypatch.delenv("TRENDS_ENV")
    monkeypatch.delenv("BIGQUERY_DATASET")
    for entry in entries:
        monkeypatch.setenv(entry["name"], entry["value"])
    profile = native.collection_profile(policy_sha256=POLICY_DIGEST, profile_sha256="6" * 64)
    assert get_dataset() == profile["source_dataset"] == "intelligence_42_sources_staging"
    assert native.staging_target_refusal(profile) is None
