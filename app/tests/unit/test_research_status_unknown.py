"""Audit F7D-APP-010: a batch or consolidated job's finished status was held for 600 seconds in one process only,
and a status that was gone read as pending, so a finished job looked as if it had not started. A finished job's status
is now kept like a single job's, and a missing status reads pending only while the job may still be running."""

import time

import pytest
from fastapi.testclient import TestClient

from src.api import main, synth


@pytest.fixture(autouse=True)
def _open(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    monkeypatch.setenv("CACHE_BUCKET", "")
    synth._CACHE.clear()
    yield
    synth._CACHE.clear()


def status(job_id):
    return TestClient(main.app).get("/api/research/status", params={"job_id": job_id}).json()


def test_a_job_id_carries_its_start():
    job_id = main.research_job_id("research_batch")
    assert job_id.startswith("research_batch_") and abs(int(job_id.split("_")[2]) - time.time()) < 5


def test_a_missing_status_of_an_old_job_is_unknown_not_pending():
    old = f"research_batch_{int(time.time()) - main.RESEARCH_JOB_MAX_AGE_S - 60}_{'a' * 32}"
    out = status(old)
    assert out["status"] == "unknown" and not out.get("pending") and out["error"]


def test_a_missing_status_of_a_young_job_or_one_this_process_runs_is_pending():
    young = main.research_job_id("research_batch")
    assert status(young)["pending"] is True
    old = f"research_{int(time.time()) - 5000}_{'b' * 32}"
    with main._research_jobs_lock:
        main._research_jobs.add(("research", old))
    try:
        assert status(old)["pending"] is True
    finally:
        with main._research_jobs_lock:
            main._research_jobs.discard(("research", old))


def test_an_id_from_before_ids_carried_their_start_still_reads_pending():
    assert status("research_batch_" + "c" * 32)["pending"] is True


def test_a_finished_batch_status_outlives_the_old_600_second_hold(monkeypatch):
    key = ("research", main.research_job_id("research_batch"))
    monkeypatch.setattr(main.research, "run_research_job",
                        lambda *a, **k: {"status": "completed", "doc": {"json": {}}, "signal_quality": "ok"})
    monkeypatch.setattr(main.research, "persist_job_result", lambda result, parent_artifact_id=None: "ra_1")
    monkeypatch.setattr(main.research, "focus_payload_for_behaviour", lambda row: ([], ""))
    monkeypatch.setattr(main, "_release_generation_capacity", lambda: None)
    main._run_research_batch_job(key, "audience_neutral", None, [{"market": "za", "behaviour": "b"}])
    later = time.time() + 601
    monkeypatch.setattr(synth.time, "time", lambda: later)
    out = status(key[1])
    assert out["status"] == "completed" and out["artifact_id"] == "ra_1"


def test_a_failed_batch_status_outlives_the_old_600_second_hold(monkeypatch):
    key = ("research", main.research_job_id("research_batch"))
    monkeypatch.setattr(main.research, "run_research_job", lambda *a, **k: {"status": "failed", "error": True})
    monkeypatch.setattr(main.research, "focus_payload_for_behaviour", lambda row: ([], ""))
    monkeypatch.setattr(main, "_release_generation_capacity", lambda: None)
    main._run_research_batch_job(key, "audience_neutral", None, [{"market": "za", "behaviour": "b"}])
    later = time.time() + 601
    monkeypatch.setattr(synth.time, "time", lambda: later)
    assert status(key[1])["status"] == "failed"
