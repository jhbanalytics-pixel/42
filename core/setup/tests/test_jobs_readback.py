"""The readback phases of mode jobs (W8-REL-B v2.1 section 3.8): JR-01 to JR-06, the per job readback JU-03, the A_STATE rule
JU-10 and the bindings and ages of JU-09. A fake Cloud Run world and a fake BigQuery client only."""
import copy
import datetime as dt
import json

import pytest

from core.setup.release import bound_readback as helper
from core.setup.release import jobs_only as jo
from core.setup.release import services_only as so
from core.setup.tests import jobs_world as jw
from core.setup.tests import release_world as rw

SAST = dt.timezone(dt.timedelta(hours=2), "SAST")


def world(tmp_path, **kw):
    w = jw.ReleaseWorld(tmp_path, **kw)
    w.prepare()
    return w


def readbacks(w, phase):
    return sorted((w.release_dir / "readbacks").glob(f"{phase}-*.json"))


def candidate_ran(w):
    """JobsCandidate left its BeforeAnyWrite readback in the release directory."""
    w.run("BeforeAnyWrite")
    w.built()


def job_env(w, name):
    return w.world.jobs[name]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"]


# JR-01

def test_jr01_the_six_phases_of_3_8_exist_in_mode_jobs():
    assert jo.PHASES == ("BeforeAnyWrite", "FreezeJobs", "BeforeJobsUpdate", "AfterJobsUpdate", "BeforeJobsRollback", "AfterJobsRollback")
    assert set(jo.PHASES) & set(so.PHASES) == {"BeforeAnyWrite"}
    assert set(jo.PHASE_FUNCTIONS) == set(jo.PHASES)


def test_jr01_every_phase_passes_on_the_world_it_expects_and_writes_a_numbered_readback(tmp_path):
    w = world(tmp_path)
    assert w.run("BeforeAnyWrite")["file"] == "BeforeAnyWrite-01.json"
    candidate_ran(w)
    assert w.run("BeforeJobsUpdate")["phase"] == "BeforeJobsUpdate"
    w.update_prefix(14)
    assert w.run("AfterJobsUpdate")["phase"] == "AfterJobsUpdate"
    assert w.run("BeforeJobsRollback")["phase"] == "BeforeJobsRollback"
    w.update(jo.UPDATE_ORDER, jw.ROLLBACK_DIGEST)
    assert w.run("AfterJobsRollback")["phase"] == "AfterJobsRollback"
    assert len(readbacks(w, "BeforeAnyWrite")) == 2


def test_jr01_the_freeze_phase_is_registered_and_fails_closed_until_the_build_requirements_supply_it(tmp_path):
    w = world(tmp_path)
    stop = w.stop("FreezeJobs")
    assert stop.code == "NOT_BUILT"


def test_jr01_exit_codes_are_0_for_pass_1_for_a_stop_and_3_for_a_read_that_could_not_complete(tmp_path, capsys):
    w = world(tmp_path)
    bindings = w.tmp / "jobs-bindings.json"
    bindings.write_text(json.dumps(w.bound), encoding="utf-8")

    def go(phase, reader=None, mode="jobs"):
        return helper.main(["--mode", mode, "--phase", phase, "--bindings", str(bindings), "--evidence", str(w.evidence)],
                           reader_factory=lambda timeouts: reader or w.fake_reader, bq_factory=lambda bound: w.bq_client(),
                           now=lambda: w.now)

    assert go("BeforeAnyWrite") == 0
    w.world.principals_ok = False
    assert go("BeforeAnyWrite") == 1 and "STOP: IDENTITY" in capsys.readouterr().err
    w.world.principals_ok = True
    w.world.executions_unreadable = True
    assert go("BeforeJobsRollback") == 3 and "PROBE" in capsys.readouterr().err


def test_jr01_mode_full_is_still_refused_and_the_two_modes_do_not_share_phases(tmp_path, capsys):
    w = world(tmp_path)
    bindings = w.tmp / "jobs-bindings.json"
    bindings.write_text(json.dumps(w.bound), encoding="utf-8")

    def go(*extra):
        return helper.main([*extra, "--bindings", str(bindings), "--evidence", str(w.evidence)],
                           reader_factory=lambda timeouts: w.fake_reader, bq_factory=lambda bound: w.bq_client(), now=lambda: w.now)

    assert go("--mode", "full", "--phase", "AfterPromotion") == 1 and "STOP: MODE" in capsys.readouterr().err
    assert go("--mode", "full", "--phase", "AfterJobsUpdate") == 1 and "STOP: MODE" in capsys.readouterr().err
    assert go("--mode", "jobs", "--phase", "BeforeCandidate") == 1 and "STOP: MODE" in capsys.readouterr().err
    assert go("--mode", "services-only", "--phase", "AfterJobsUpdate") == 1 and "STOP: MODE" in capsys.readouterr().err


def test_jr01_the_retry_rule_is_the_pastes_and_the_helper_never_retries_a_probe_itself(tmp_path):
    w = world(tmp_path)
    w.world.executions_unreadable = True
    reader = w.fake_reader
    with pytest.raises(so.Probe):
        w.run("BeforeJobsRollback")
    assert [c for c in reader.calls if c[0] == "executions"].__len__() == 1


# JR-02

def test_jr02_after_jobs_update_passes_with_14_at_the_digest_and_lists_the_executions_started_inside_the_window(tmp_path):
    w = world(tmp_path)
    candidate_ran(w)
    w.run("BeforeJobsUpdate")
    inside = w.now + dt.timedelta(minutes=20)
    w.side_execution("f42-watchdog", "f42-watchdog-inside", inside, digest=jw.ROLLBACK_DIGEST)
    w.side_execution("f42-watchdog", "f42-watchdog-before", w.now - dt.timedelta(minutes=20), digest=jw.ROLLBACK_DIGEST)
    w.update_prefix(14)
    result = w.run("AfterJobsUpdate", now=w.now + dt.timedelta(minutes=30))
    listed = result["observations"]["executions_in_window"]
    assert [e["name"] for e in listed] == ["f42-watchdog-inside"]
    assert listed[0]["image_digest"] == jw.ROLLBACK_DIGEST and listed[0]["job"] == "f42-watchdog"
    assert result["observations"]["jobs_at_digest"] == 14


@pytest.mark.parametrize("count", [0, 13])
def test_jr02_a_job_not_at_the_digest_stops_after_jobs_update_with_job_image(tmp_path, count):
    w = world(tmp_path)
    candidate_ran(w)
    w.run("BeforeJobsUpdate")
    w.update_prefix(count) if count else None
    if count == 13:
        pass
    stop = w.stop("AfterJobsUpdate")
    assert stop.code == "JOB_IMAGE"


def test_jr02_a_changed_field_other_than_the_image_stops_with_job_drift(tmp_path):
    w = world(tmp_path)
    candidate_ran(w)
    w.run("BeforeJobsUpdate")
    w.update_prefix(14)
    job_env(w, "f42-digest").append({"name": "F42_NEW", "value": "1"})
    assert w.stop("AfterJobsUpdate").code == "JOB_DRIFT"


def test_jr02_the_registry_tag_must_still_resolve_to_the_manifest_digest(tmp_path):
    w = world(tmp_path)
    candidate_ran(w)
    w.run("BeforeJobsUpdate")
    w.update_prefix(14)
    w.world.registry[jo.image_tag(w.bound)] = jw.OLD_DIGEST
    assert w.stop("AfterJobsUpdate").code == "TAG_MOVED"


def test_jr02_14_jobs_that_agree_with_each_other_on_some_other_digest_still_stop_because_the_expectation_is_the_frozen_digest(tmp_path):
    w = world(tmp_path)
    candidate_ran(w)
    w.run("BeforeJobsUpdate")
    w.update_prefix(14, jw.OLD_DIGEST)
    assert w.stop("AfterJobsUpdate").code == "JOB_IMAGE"


# JR-03

def test_jr03_after_jobs_rollback_passes_when_the_14_views_equal_baseline_j_and_records_residue_and_active_executions(tmp_path):
    w = world(tmp_path)
    w.update_prefix(14)
    w.update(jo.UPDATE_ORDER, jw.ROLLBACK_DIGEST)
    w.world.executions["f42-detect-live"] = jw.execution_raw("f42-detect-live", "f42-detect", (w.now - dt.timedelta(hours=1)).isoformat(), None,
                                                              jw.NEW_DIGEST, succeeded=0)
    result = w.run("AfterJobsRollback")
    active = result["observations"]["active_executions"]
    assert [(e["job"], e["name"], e["image_digest"]) for e in active] == [("f42-detect", "f42-detect-live", jw.NEW_DIGEST)]
    residue = result["observations"]["residue"]
    assert {"tvf_post_items", "v_item_locality_checked", "v_item_locality_current", "core.early_signal", "core.item_locality",
            "core.post_items.linked_on", "core.post_items.link_market"} <= set(residue)
    assert result["observations"]["residue_probed"] is False


def test_jr03_one_job_still_on_the_new_digest_or_with_any_changed_field_stops_with_job_drift(tmp_path):
    w = world(tmp_path)
    w.update_prefix(14)
    w.update(jo.UPDATE_ORDER, jw.ROLLBACK_DIGEST)
    w.update(["f42-collect"], jw.NEW_DIGEST)
    assert w.stop("AfterJobsRollback").code == "JOB_IMAGE"
    w.update(["f42-collect"], jw.ROLLBACK_DIGEST)
    job_env(w, "f42-probe").append({"name": "F42_X", "value": "1"})
    assert w.stop("AfterJobsRollback").code == "JOB_DRIFT"


def test_jr03_the_rollback_target_is_the_bound_digest_not_whatever_the_baseline_views_say(tmp_path):
    w = world(tmp_path)
    w.bound["rollbackJobsDigest"] = jw.OLD_DIGEST
    w.update(jo.UPDATE_ORDER, jw.ROLLBACK_DIGEST)
    assert w.stop("AfterJobsRollback").code in ("BASELINE", "JOB_IMAGE")


def test_jr03_services_that_changed_stop_the_rollback_readback_too(tmp_path):
    w = world(tmp_path)
    w.update_prefix(14)
    w.update(jo.UPDATE_ORDER, jw.ROLLBACK_DIGEST)
    w.world.svc["f42-agent"]["env"]["F42_EXTRA"] = "1"
    assert w.stop("AfterJobsRollback").code == "SERVICE_CHANGED"


# JR-04

@pytest.mark.parametrize("phase", ["BeforeAnyWrite", "BeforeJobsUpdate", "AfterJobsUpdate", "AfterJobsRollback"])
def test_jr04_a_changed_service_stops_every_checking_phase_with_service_changed(tmp_path, phase):
    w = world(tmp_path)
    if phase != "BeforeAnyWrite":
        candidate_ran(w)
    if phase == "AfterJobsUpdate":
        w.run("BeforeJobsUpdate")
        w.update_prefix(14)
    if phase == "AfterJobsRollback":
        w.update(jo.UPDATE_ORDER, jw.ROLLBACK_DIGEST)
    w.world.svc["f42-api"]["env"]["F42_EXTRA"] = "1"
    assert w.stop(phase).code == "SERVICE_CHANGED"


def test_jr04_before_a_rollback_a_changed_service_is_recorded_and_never_a_reason_to_refuse_the_rollback(tmp_path):
    w = world(tmp_path)
    w.update_prefix(14)
    w.world.svc["f42-api"]["env"]["F42_EXTRA"] = "1"
    result = w.run("BeforeJobsRollback")
    assert result["observations"]["services_changed"] == ["f42-api"] and result["blocking"]["services"]["blocking"] is False


def test_jr04_a_changed_tag_set_or_traffic_entry_is_a_change_too(tmp_path):
    w = world(tmp_path)
    w.world.svc["f42-api"]["traffic"].append({"revisionName": rw.CAND_REV["f42-api"], "percent": 0, "tag": "extra-tag"})
    assert w.stop("BeforeAnyWrite").code == "SERVICE_CHANGED"


# JR-05

SECRET_VALUE = "SENTINEL-ENV-VALUE-5521"


def test_jr05_the_readback_holds_names_and_hashes_and_no_description_or_environment_value(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    for name in so.JOB_NAMES:
        job_env(w, name).append({"name": "F42_SENTINEL", "value": SECRET_VALUE})
    w.prepare()
    candidate_ran(w)
    w.run("BeforeJobsUpdate")
    w.update_prefix(14)
    w.run("AfterJobsUpdate")
    w.run("BeforeJobsRollback")
    w.update(jo.UPDATE_ORDER, jw.ROLLBACK_DIGEST)
    w.run("AfterJobsRollback")
    files = list((w.release_dir / "readbacks").glob("*.json"))
    assert len(files) >= 5
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert SECRET_VALUE not in text, path.name
        assert "valueFrom" not in text and "secretKeyRef" not in text, path.name
        assert "serviceAccountName" not in text and '"containers"' not in text, path.name


# JR-06

@pytest.mark.parametrize("phase,expected", [("BeforeAnyWrite", True), ("BeforeJobsUpdate", True), ("BeforeJobsRollback", True),
                                            ("AfterJobsUpdate", False), ("AfterJobsRollback", False)])
def test_jr06_principals_are_verified_in_the_three_before_phases_and_only_there(tmp_path, phase, expected):
    w = world(tmp_path)
    if phase != "BeforeAnyWrite":
        candidate_ran(w)
    if phase == "AfterJobsUpdate":
        w.run("BeforeJobsUpdate")
        w.update_prefix(14)
    if phase == "AfterJobsRollback":
        w.update(jo.UPDATE_ORDER, jw.ROLLBACK_DIGEST)
    w.fake_reader.calls.clear()
    w.run(phase)
    assert (("principals",) in w.fake_reader.calls) is expected


@pytest.mark.parametrize("phase", ["BeforeAnyWrite", "BeforeJobsUpdate", "BeforeJobsRollback"])
def test_jr06_an_unverifiable_principal_stops_with_identity_and_writes_no_readback(tmp_path, phase):
    w = world(tmp_path)
    if phase != "BeforeAnyWrite":
        candidate_ran(w)
    folder = w.release_dir / "readbacks"
    before = len(list(folder.glob("*.json"))) if folder.exists() else 0
    w.world.principals_ok = False
    assert w.stop(phase).code == "IDENTITY"
    after = len(list(folder.glob("*.json"))) if folder.exists() else 0
    assert after == before


def test_jr06_a_changed_cli_caller_stops_with_identity(tmp_path):
    w = world(tmp_path)
    w.world.config = {"core": {"account": "someone.else@example.com", "project": rw.PROJECT}}
    assert w.stop("BeforeAnyWrite").code == "IDENTITY"


# JU-03

def updated_world(tmp_path):
    w = world(tmp_path)
    w.update(["f42-watchdog"])
    return w


def job_check(w, job="f42-watchdog", digest=jw.NEW_DIGEST):
    return jo.check_job_after_update(w.fake_reader, jo.load_baseline_j(w.bound), job, digest)


def test_ju03_a_job_that_differs_from_baseline_j_only_in_the_image_passes(tmp_path):
    w = updated_world(tmp_path)
    view = job_check(w)
    assert view["image"] == jo.image_reference(jw.NEW_DIGEST)


def mutate_containers(job, **changes):
    job["spec"]["template"]["spec"]["template"]["spec"]["containers"][0].update(changes)


def mutate_task(job, **changes):
    job["spec"]["template"]["spec"]["template"]["spec"].update(changes)


@pytest.mark.parametrize("change", [
    lambda j: mutate_containers(j, command=["python", "-m", "other"]),
    lambda j: mutate_containers(j, args=["--x"]),
    lambda j: mutate_containers(j, env=[{"name": "F42_NEW", "value": "1"}]),
    lambda j: mutate_containers(j, resources={"limits": {"cpu": "2000m", "memory": "2Gi"}}),
    lambda j: mutate_task(j, serviceAccountName="someone@ogilvy-trends-v2.iam.gserviceaccount.com"),
    lambda j: mutate_task(j, timeoutSeconds=60),
    lambda j: mutate_task(j, maxRetries=5),
    lambda j: j["spec"]["template"]["spec"].update(parallelism=3),
    lambda j: j["spec"]["template"]["spec"].update(taskCount=2),
    lambda j: j["spec"]["template"]["spec"]["template"]["metadata"]["annotations"].update({"run.googleapis.com/vpc-access-connector": "c"}),
    lambda j: j["metadata"]["annotations"].update({"a/b": "c"}),
], ids=["command", "args", "env", "resources", "service_account", "timeout", "max_retries", "parallelism", "task_count",
        "template_annotation", "job_annotation"])
def test_ju03_any_field_but_the_image_that_differs_stops_with_job_drift(tmp_path, change):
    w = updated_world(tmp_path)
    change(w.world.jobs["f42-watchdog"])
    with pytest.raises(so.Stop) as stop:
        job_check(w)
    assert stop.value.code == "JOB_DRIFT"


def test_ju03_a_managed_annotation_the_update_adds_is_not_a_drift(tmp_path):
    w = updated_world(tmp_path)
    w.world.jobs["f42-watchdog"]["metadata"]["annotations"].update({"run.googleapis.com/client-name": "gcloud", "run.googleapis.com/operation-id": "x"})
    assert job_check(w)["name"] == "f42-watchdog"


@pytest.mark.parametrize("digest", [jw.ROLLBACK_DIGEST, jw.OLD_DIGEST])
def test_ju03_an_image_that_is_not_the_manifest_digest_stops_with_job_image(tmp_path, digest):
    w = world(tmp_path)
    w.update(["f42-watchdog"], digest)
    with pytest.raises(so.Stop) as stop:
        job_check(w)
    assert stop.value.code == "JOB_IMAGE"


def test_ju03_the_image_is_judged_against_the_digest_asked_for_so_a_restore_can_use_the_rollback_digest(tmp_path):
    w = world(tmp_path)
    assert job_check(w, digest=jw.ROLLBACK_DIGEST)["image"] == jo.image_reference(jw.ROLLBACK_DIGEST)


# JU-10

def test_ju10_the_services_in_the_state_release_a_promoted_are_accepted_by_the_two_before_phases(tmp_path):
    w = world(tmp_path)
    w.run("BeforeAnyWrite")
    candidate_ran(w)
    w.run("BeforeJobsUpdate")


def rolled_back_world(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    for name in so.SERVICES:
        w.world.restore(name)
        w.world.svc[name].update(latest_created=rw.A80_REV[name], latest_ready=rw.A80_REV[name])
    return w


def test_ju10_the_a80_pair_restored_by_a_rollback_is_accepted_when_baseline_j_binds_after_rollback(tmp_path):
    w = rolled_back_world(tmp_path)
    w.prepare(baseline=w.baseline_j(a_kind="AfterRollback"))
    w.run("BeforeAnyWrite")
    candidate_ran(w)
    w.run("BeforeJobsUpdate")


def test_ju10_the_a80_pair_with_release_a_never_run_stops_even_when_baseline_j_was_captured_from_it(tmp_path):
    w = rolled_back_world(tmp_path)
    w.prepare(baseline=w.baseline_j(a_kind="AfterPromotion"))
    assert w.stop("BeforeAnyWrite").code == "A_STATE"


def test_ju10_a_baseline_j_that_binds_no_terminal_readback_of_release_a_is_refused(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    baseline = w.baseline_j()
    baseline.pop("aTerminal")
    w.write_bindings(baseline=baseline)
    with pytest.raises(so.Stop) as stop:
        jo.load_baseline_j(w.bound)
    assert stop.value.code == "A_STATE"


def leave_post_a_state(w):
    for name in so.SERVICES:
        w.world.restore(name)
        w.world.svc[name].update(latest_created=rw.A80_REV[name], latest_ready=rw.A80_REV[name])


def test_ju10_services_that_left_the_post_a_state_after_baseline_j_was_bound_stop_in_both_before_phases(tmp_path):
    w = world(tmp_path)
    leave_post_a_state(w)
    assert w.stop("BeforeAnyWrite").code == "A_STATE"
    w2 = world(tmp_path / "two")
    candidate_ran(w2)
    leave_post_a_state(w2)
    assert w2.stop("BeforeJobsUpdate").code == "A_STATE"


# JU-09: bindings, ages, the baseline chain manifest

@pytest.mark.parametrize("key", list(jo.RELEASE_BINDINGS))
def test_ju09_a_missing_bindings_field_is_a_bindings_stop(tmp_path, key):
    w = world(tmp_path)
    broken = {k: v for k, v in w.bound.items() if k != key}
    with pytest.raises(so.Stop) as stop:
        jo.validate_jobs_bindings(broken)
    assert stop.value.code == "BINDINGS" and key in stop.value.message


@pytest.mark.parametrize("key,value", [("baselineSha256", "xyz"), ("rollbackJobsDigest", "sha256:short"), ("maxBaselineAgeDays", 0),
                                       ("maxCandidateAgeHours", "12"), ("window", {"startSast": "8:05"}), ("priorAttempts", {}),
                                       ("collectStartToleranceMinutes", -1), ("templateHashes", {}), ("readTimeoutSeconds", {"gcloud": 0})])
def test_ju09_a_malformed_bindings_field_is_a_bindings_stop(tmp_path, key, value):
    w = world(tmp_path)
    with pytest.raises(so.Stop) as stop:
        jo.validate_jobs_bindings({**w.bound, key: value})
    assert stop.value.code == "BINDINGS"


def test_ju09_a_bindings_file_for_another_mode_or_version_is_refused(tmp_path):
    w = world(tmp_path)
    with pytest.raises(so.Stop) as stop:
        jo.validate_jobs_bindings({**w.bound, "mode": "services-only"})
    assert stop.value.code == "MODE"
    with pytest.raises(so.Stop) as stop:
        jo.validate_jobs_bindings({**w.bound, "schema_version": 2})
    assert stop.value.code == "SCHEMA_VERSION"


def test_ju09_an_expired_baseline_is_refused_with_baseline_age_naming_the_manifest_date(tmp_path):
    w = world(tmp_path)
    stop = w.stop("BeforeAnyWrite", now=w.now + dt.timedelta(days=3))
    assert stop.code == "BASELINE_AGE" and "2026-10-10" in stop.message
    w.run("BeforeAnyWrite", now=w.now + dt.timedelta(days=1, hours=2))


def test_ju09_a_candidate_older_than_the_bound_hours_is_refused_before_the_update(tmp_path):
    w = world(tmp_path)
    candidate_ran(w)
    assert w.stop("BeforeJobsUpdate", now=w.now + dt.timedelta(hours=12, minutes=1)).code == "CANDIDATE_AGE"


def test_ju09_a_candidate_from_another_sast_day_is_refused_even_inside_the_age(tmp_path):
    w = world(tmp_path)
    w.now = dt.datetime(2026, 10, 11, 19, 30, tzinfo=jw.UTC)  # 21:30 SAST
    candidate_ran(w)
    next_day = dt.datetime(2026, 10, 11, 22, 30, tzinfo=jw.UTC)  # 00:30 SAST on the 12th
    assert w.stop("BeforeJobsUpdate", now=next_day).code == "CANDIDATE_AGE"


def test_ju09_jobs_update_without_a_jobs_candidate_readback_is_refused(tmp_path):
    w = world(tmp_path)
    assert w.stop("BeforeJobsUpdate").code == "CANDIDATE_AGE"


def baseline_manifest(w):
    return json.loads(open(w.bound["baselineChainPath"], encoding="utf-8").read())


def rebind(w, manifest):
    import hashlib

    path = w.release_dir / "tampered-chain.json"
    jw.write_json(path, manifest)
    w.bound["baselineChainPath"] = str(path)
    w.bound["baselineChainSha256"] = hashlib.sha256(path.read_bytes()).hexdigest()


def test_ju09_the_baseline_chain_manifest_is_recomputed_against_its_bound_hash(tmp_path):
    w = world(tmp_path)
    path = w.release_dir / "chain-evidence-2026-10-10.json"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert w.stop("BeforeAnyWrite").code == "BINDINGS"


@pytest.mark.parametrize("edit", [
    lambda m: m["verdict"].update(qualifies=False),
    lambda m: m["verdict"].update(reasons=["WINDOW"], qualifies=False),
    lambda m: m.update(role="first-b"),
    lambda m: m["jobs_image"].update(expected_digest=jw.NEW_DIGEST),
    lambda m: m["a_terminal"].update(at_utc="2026-10-01T00:00:00+00:00"),
    lambda m: m["verdict"].update(qualifies=False, baseline_by_line=True, reasons=["DEGRADED"], failed_stage="understand"),
], ids=["not_qualifying", "window_reason", "wrong_role", "wrong_digest", "other_a_terminal", "by_line_without_signature"])
def test_ju09_a_baseline_chain_manifest_whose_verdict_the_rest_of_it_does_not_support_is_refused(tmp_path, edit):
    w = world(tmp_path)
    manifest = baseline_manifest(w)
    edit(manifest)
    rebind(w, manifest)
    assert w.stop("BeforeAnyWrite").code == "BASELINE"


def test_ju09_a_qualifying_by_line_baseline_with_the_signature_is_accepted(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    w.set_cluster_error("KE")
    w.set_cluster_error("NG")
    w.prepare()
    manifest = baseline_manifest(w)
    assert manifest["verdict"]["baseline_by_line"] is True
    w.run("BeforeAnyWrite")


# further pins found by the mutation pass

def test_jr03_a_chain_execution_that_is_running_or_queued_refuses_the_rollback_and_a_running_side_job_does_not(tmp_path):
    w = world(tmp_path)
    w.side_execution("f42-watchdog", "f42-watchdog-live", w.now - dt.timedelta(minutes=5))
    w.world.executions["f42-watchdog-live"]["status"].pop("completionTime")
    result = w.run("BeforeJobsRollback")
    assert result["observations"]["active_executions"] == [{"job": "f42-watchdog", "name": "f42-watchdog-live"}]
    w.side_execution("f42-understand", "f42-understand-live", w.now - dt.timedelta(minutes=5))
    w.world.executions["f42-understand-live"]["status"].pop("completionTime")
    stop = w.stop("BeforeJobsRollback")
    assert stop.code == "CHAIN_ACTIVE"


def test_ju06_the_before_update_phase_refuses_outside_0805_to_2100_sast_and_opens_at_the_boundary(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    w.now = dt.datetime(2026, 10, 11, 6, 4, tzinfo=jw.UTC)  # 08:04 SAST
    w.prepare()
    candidate_ran(w)
    assert w.stop("BeforeJobsUpdate", now=dt.datetime(2026, 10, 11, 6, 4, tzinfo=jw.UTC)).code == "WINDOW"
    assert w.run("BeforeJobsUpdate", now=dt.datetime(2026, 10, 11, 6, 5, tzinfo=jw.UTC))["phase"] == "BeforeJobsUpdate"
    late = world(tmp_path / "late")
    candidate_ran(late)
    assert late.run("BeforeJobsUpdate", now=dt.datetime(2026, 10, 11, 18, 59, tzinfo=jw.UTC))["phase"] == "BeforeJobsUpdate"
    assert late.stop("BeforeJobsUpdate", now=dt.datetime(2026, 10, 11, 19, 0, tzinfo=jw.UTC)).code == "WINDOW"


def test_ju05_the_jobs_already_updated_must_be_a_prefix_of_the_update_order(tmp_path):
    w = world(tmp_path)
    candidate_ran(w)
    w.update(["f42-collect"])
    assert w.stop("BeforeJobsUpdate").code == "JOB_ORDER"
    w.update(["f42-collect"], jw.ROLLBACK_DIGEST)
    w.update_prefix(3)
    assert w.run("BeforeJobsUpdate")["observations"]["jobs_already_updated"] == ["f42-watchdog", "f42-probe", "f42-gdelt"]


def test_jr01_baseline_j_whose_images_are_not_the_bound_rollback_digest_is_refused(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    jw.set_job_image(w.world, "f42-probe", jw.OLD_DIGEST)
    w.prepare(baseline=w.baseline_j())
    assert w.stop("BeforeAnyWrite").code == "BASELINE"


def test_jr02_a_release_manifest_whose_build_result_and_registry_digest_differ_is_refused(tmp_path):
    w = world(tmp_path)
    candidate_ran(w)
    image = {"repository": jo.JOBS_REPO, "registry_digest": jw.OLD_DIGEST, "digest": jw.NEW_DIGEST, "reference": jo.image_reference(jw.NEW_DIGEST)}
    import os

    os.remove(w.release_dir / "release-manifest.json")
    os.remove(w.release_dir / "release-manifest.sha256")
    jw.write_frozen_manifest(w.release_dir, jw.NEW_DIGEST, image=image)
    assert w.stop("BeforeJobsUpdate").code == "DIGEST"


def test_ju09_a_baseline_chain_verdict_that_says_qualifies_beside_a_reason_is_refused(tmp_path):
    w = world(tmp_path)
    manifest = baseline_manifest(w)
    manifest["verdict"]["reasons"] = ["IMAGE"]
    rebind(w, manifest)
    assert w.stop("BeforeAnyWrite").code == "BASELINE"


def test_ju10_a_baseline_j_captured_from_the_a80_pair_and_called_promoted_is_refused_by_name_when_the_live_services_are_promoted(tmp_path):
    rolled = jw.ReleaseWorld(tmp_path / "rolled")
    for name in so.SERVICES:
        rolled.world.restore(name)
        rolled.world.svc[name].update(latest_created=rw.A80_REV[name], latest_ready=rw.A80_REV[name])
    mislabelled = rolled.baseline_j(a_kind="AfterPromotion")
    live = jw.ReleaseWorld(tmp_path / "live")
    live.prepare(baseline=mislabelled)
    stop = live.stop("BeforeAnyWrite")
    assert stop.code == "A_STATE" and "baseline-J" in stop.message
