"""The ingest job definition runs the collection entry point this tree reconciles.

``infra/runtime/ingest-staging.json`` at the repository root is the job definition
``ops/deploy/runtime_jobs.py`` creates for ``intelligence-42-ingest-staging``. The two
digests it stamps are checked here against the values this tree derives, the entry point
it names against the module that exists, and its timeout against the wait the daily
collect stage gives it, so a definition and an image that have parted fail in the build.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from scripts.staging.collect_42_sources import (
    INGEST_JOB,
    entry_point_profile,
    reconciled_policy_digest,
)
from src.analysis.open_intelligence import daily_native_clients as native
from src.utils.bigquery import get_dataset

ENGINE_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIRECTORY = ENGINE_ROOT.parent / "infra" / "runtime"


def _entry():
    config = json.loads((RUNTIME_DIRECTORY / "ingest-staging.json").read_bytes())
    assert list(config["jobs"]) == [INGEST_JOB]
    return config["jobs"][INGEST_JOB]


def test_the_ingest_job_stamps_the_digests_this_tree_derives():
    env = {item["name"]: item["value"] for item in _entry()["env"]}
    assert env["COLLECTION_POLICY_SHA256"] == reconciled_policy_digest()
    assert env["COLLECTION_PROFILE_SHA256"] == entry_point_profile()["profile_sha256"]
    assert "COLLECTION_SOURCE_SHA" not in env
    assert "COLLECTION_IMAGE_URI" not in env


def test_the_ingest_job_resolves_the_staging_source_dataset(monkeypatch):
    for item in _entry()["env"]:
        monkeypatch.setenv(item["name"], item["value"])
    assert get_dataset() == "intelligence_42_sources_staging"


def test_the_ingest_job_runs_the_collection_entry_point():
    entry = _entry()
    assert entry["command"] == ["python"]
    assert entry["args"][0] == "-m"
    module = importlib.import_module(entry["args"][1])
    assert callable(module.entry)
    assert entry["service_account"] == (
        "intelligence-42-ingest@ogilvy-trends-v2.iam.gserviceaccount.com"
    )


def test_the_daily_collect_stage_waits_longer_than_the_ingest_job_runs():
    daily = json.loads((RUNTIME_DIRECTORY / "daily-staging.json").read_bytes())
    budget = native.INGEST_POLLS * native.INGEST_POLL_INTERVAL_SECONDS
    assert _entry()["timeout_seconds"] < budget
    assert budget < daily["jobs"]["intelligence-42-daily-staging"]["timeout_seconds"]
