"""Release B end to end, offline (the assembled branch): the packet files come from the packet command line, JobsCandidate, JobsUpdate and
JobsRollback are the real JOBS-PASTE.ps1 run as pwsh processes, and every command the paste starts is played by the real python program
over a fake world: deploy_jobs.py for the build, bound_readback.py for FreezeJobs and the readbacks, durable_effects_check.py for the
validate and the schema readbacks, jobs_run.py for the snapshot, the 14 updates and the rollback. pwsh is required; a missing pwsh fails."""
import json
import shutil

import pytest

from core.setup.release import jobs_only as jo
from core.setup.release import plan
from core.setup.tests import jobs_world as jw
from core.setup.tests.jobs_e2e_world import EndToEnd


@pytest.fixture(autouse=True)
def pwsh_is_present():
    if shutil.which("pwsh") is None:
        pytest.fail("pwsh is not installed; the end to end tests cannot run (install PowerShell 7)")


def ok(result):
    assert result.returncode == 0, (result.stderr, result.stdout)
    return result


def test_the_whole_release_candidate_update_and_rollback_runs_through_the_real_paste_to_each_readback(tmp_path, monkeypatch):
    run = EndToEnd(tmp_path, monkeypatch).make_packet()
    bench = run.bench
    assert [name for name, _ in run.cli_calls] == ["capture-baseline-j", "bind", "bind", "lock", "review", "receipt"]
    before = run.job_images()
    assert set(before.values()) == {jw.ROLLBACK_DIGEST}

    # JobsCandidate: build, freeze, validate, snapshot, schema apply, readbacks, receipt
    candidate = ok(run.paste("JobsCandidate"))
    assert candidate.names == ["helper-BeforeAnyWrite-0", "jobs-build", "helper-FreezeJobs-0", "durable-validate", "snapshot", "schema-apply",
                               "durable-readbacks", "schema-receipt"]
    state = run.state()
    assert state["schema_applied"] is True
    manifest = run.read("release-manifest.json")
    assert manifest["image"]["digest"] == jw.NEW_DIGEST == manifest["image"]["registry_digest"] and manifest["source"]["commit"] == bench.target
    receipt = run.read("schema-readback-receipt.json")
    assert receipt["receipt_sha256"] == bench.bindings_value()["schemaReadbackReceiptSha256"] and receipt["effects"]
    assert sorted(p.name for p in (bench.packet / "readbacks").glob("*.json")) == ["BeforeAnyWrite-01.json", "FreezeJobs-01.json"]
    assert run.job_images() == before, "JobsCandidate changes no job"

    # JobsUpdate: IDLE, DEPLOY, the 14 definitions, the readback
    update = ok(run.paste("JobsUpdate"))
    assert update.names == ["helper-BeforeJobsUpdate-0", "jobs-run-update", "helper-AfterJobsUpdate-0"]
    assert [p["prompt"] for p in update.prompts] == ["Type IDLE to continue", "Type DEPLOY to continue"]
    assert set(run.job_images().values()) == {jw.NEW_DIGEST}
    log = json.loads(next((bench.packet / "runs").glob("*-JobsUpdate/jobs-update-01.json")).read_text(encoding="utf-8"))
    assert log["complete"] is True and log["updated"] == list(jo.UPDATE_ORDER) and log["stopped"] is None
    assert (bench.packet / "readbacks" / "AfterJobsUpdate-01.json").exists()

    # JobsRollback: nothing typed, the blockers checker, the 14 back, the readback
    rollback = ok(run.paste("JobsRollback"))
    assert rollback.names == ["rollback-blockers", "helper-BeforeJobsRollback-0", "jobs-run-rollback", "helper-AfterJobsRollback-0"]
    assert rollback.prompts == []
    assert run.job_images() == before
    assert json.loads((bench.packet / "readbacks" / "AfterJobsRollback-01.json").read_text(encoding="utf-8"))["observations"]["residue_probed"] is False

    # Q4: jobs only. No service byte changed, and every command the paste started is on the jobs positive list, exactly once.
    assert run.state()["cloud"].services_bytes() == run.services_before
    for result in (candidate, update, rollback):
        for call in result.external:
            assert len(plan.jobs_matching_entries(call["argv"])) == 1, call["argv"]
    forbidden = ("update-traffic", "scheduler", "set-iam-policy", "--set-secrets", "--update-env-vars", "--remove-env-vars", "execute", "delete")
    for result in (candidate, update, rollback):
        assert not [c for c in result.external if any(word in c["argv"] for word in forbidden)]


def test_the_paste_orders_the_schema_apply_before_its_readbacks_and_the_receipt_and_jobs_update_needs_that_receipt(tmp_path, monkeypatch):
    run = EndToEnd(tmp_path, monkeypatch).make_packet()
    candidate = ok(run.paste("JobsCandidate"))
    played = run.state()["log"]
    apply_at = next(i for i, entry in enumerate(played) if entry[2:4] == ["-m", "core.schema.apply"])
    readbacks_at = next(i for i, entry in enumerate(played) if "readbacks" in entry)
    assert apply_at < readbacks_at
    assert candidate.names.index("schema-apply") < candidate.names.index("durable-readbacks") < candidate.names.index("schema-receipt")
    (run.bench.packet / "schema-readback-receipt.json").unlink()
    update = run.paste("JobsUpdate")
    assert update.returncode != 0 and update.names == ["helper-BeforeJobsUpdate-0"]
    assert set(run.job_images().values()) == {jw.ROLLBACK_DIGEST}


def test_a_schema_apply_that_leaves_the_columns_missing_stops_at_the_readbacks_and_leaves_no_receipt(tmp_path, monkeypatch):
    run = EndToEnd(tmp_path, monkeypatch, apply_noop=True).make_packet()
    candidate = run.paste("JobsCandidate")
    assert candidate.returncode != 0 and candidate.names[-1] == "durable-readbacks" and "schema-receipt" not in candidate.names
    assert not (run.bench.packet / "schema-readback-receipt.json").exists()
    update = run.paste("JobsUpdate")
    assert update.returncode != 0 and set(run.job_images().values()) == {jw.ROLLBACK_DIGEST}


def test_a_registry_that_disagrees_with_the_build_result_stops_at_freezejobs_before_the_validator_and_the_schema_apply(tmp_path, monkeypatch):
    run = EndToEnd(tmp_path, monkeypatch, registry_digest="sha256:" + "ab" * 32).make_packet()
    candidate = run.paste("JobsCandidate")
    assert candidate.returncode != 0 and candidate.names == ["helper-BeforeAnyWrite-0", "jobs-build", "helper-FreezeJobs-0"]
    assert run.state()["schema_applied"] is False and not (run.bench.packet / "release-manifest.json").exists()


def test_a_dry_run_receipt_changed_after_the_lock_stops_the_paste_before_the_build(tmp_path, monkeypatch):
    run = EndToEnd(tmp_path, monkeypatch).make_packet()
    path = run.bench.dry_run
    path.write_bytes(path.read_bytes() + b" ")
    candidate = run.paste("JobsCandidate")
    assert candidate.returncode != 0 and candidate.external == [] and "a bound file differs from the lock: dry_run_receipt" in candidate.stderr + candidate.stdout


def test_a_jobs_update_after_a_chain_stop_resumes_and_the_rollback_then_restores_the_rest(tmp_path, monkeypatch):
    run = EndToEnd(tmp_path, monkeypatch).make_packet()
    ok(run.paste("JobsCandidate"))
    state = run.state()
    state["cloud"].fail["f42-detect"] = 1
    run.save(state)
    stopped = run.paste("JobsUpdate")
    assert stopped.returncode != 0
    log = json.loads(next((run.bench.packet / "runs").glob("*-JobsUpdate/jobs-update-01.json")).read_text(encoding="utf-8"))
    assert log["stopped"]["code"] == "UPDATE_FAILED" and log["stopped"]["job"] == "f42-detect" and log["branch"] == "chain_group_restored"
    images = run.job_images()
    assert all(images[job] == jw.NEW_DIGEST for job in jo.UPDATE_ORDER[:10]) and all(images[job] == jw.ROLLBACK_DIGEST for job in jo.CHAIN_GROUP)
    ok(run.paste("JobsRollback"))
    assert set(run.job_images().values()) == {jw.ROLLBACK_DIGEST}
