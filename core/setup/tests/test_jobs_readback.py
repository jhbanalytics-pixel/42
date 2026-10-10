"""The readback phases of mode jobs (W8-REL-B v2.1 section 3.8, JR-01 to JR-06). A fake Cloud Run world and a fake BigQuery client only."""
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


def test_jr01_mode_full_with_services_only_bindings_is_refused_before_any_read(tmp_path, capsys):
    bindings = tmp_path / "services-bindings.json"
    bindings.write_text(json.dumps({"schema_version": 1, "mode": "services-only"}), encoding="utf-8")

    def no_reader(timeouts):
        raise AssertionError("a reader was built for a refused mode")

    code = helper.main(["--mode", "full", "--phase", "AfterRollback", "--bindings", str(bindings), "--evidence", str(tmp_path)],
                       reader_factory=no_reader)
    assert code == 1 and "STOP: MODE" in capsys.readouterr().err


# RB-T4 (F4): a rollback never refuses for service drift. A service that is not at one revision at 100% is recorded as not blocking by
# BeforeJobsRollback and AfterJobsRollback, and the rollback goes on.

def split_traffic(w, names=("f42-api",)):
    for name in names:
        w.world.svc[name]["traffic"] = [{"revisionName": rw.A80_REV[name], "percent": 50},
                                        {"revisionName": rw.CAND_REV[name], "percent": 50, "tag": rw.RID}]


def test_rb_t4_before_a_rollback_a_service_that_is_not_at_one_revision_is_recorded_not_blocking_and_the_phase_passes(tmp_path):
    w = world(tmp_path)
    w.update_prefix(14)
    split_traffic(w)
    result = w.run("BeforeJobsRollback")
    assert result["observations"]["services_changed"] == ["f42-api"]
    assert result["blocking"]["services"] == {"blocking": False, "changed": ["f42-api"], "not_one_revision": ["f42-api"]}


def test_rb_t4_both_services_split_and_one_service_with_no_traffic_at_all_are_recorded_too(tmp_path):
    both = world(tmp_path / "both")
    both.update_prefix(14)
    split_traffic(both, ("f42-agent", "f42-api"))
    assert both.run("BeforeJobsRollback")["blocking"]["services"]["not_one_revision"] == ["f42-agent", "f42-api"]
    none = world(tmp_path / "none")
    none.update_prefix(14)
    none.world.svc["f42-api"]["traffic"] = [{"revisionName": rw.A80_REV["f42-api"], "percent": 0}]
    result = none.run("BeforeJobsRollback")
    assert result["blocking"]["services"]["not_one_revision"] == ["f42-api"] and result["blocking"]["services"]["blocking"] is False


def test_rb_t4_a_service_that_changed_but_still_serves_one_revision_is_recorded_as_changed_and_not_as_split(tmp_path):
    w = world(tmp_path)
    w.update_prefix(14)
    w.world.svc["f42-api"]["env"]["F42_EXTRA"] = "1"
    result = w.run("BeforeJobsRollback")
    assert result["blocking"]["services"] == {"blocking": False, "changed": ["f42-api"], "not_one_revision": []}


def test_rb_t4_a_chain_execution_still_refuses_the_rollback_even_when_a_service_is_split(tmp_path):
    w = world(tmp_path)
    w.update_prefix(14)
    split_traffic(w)
    w.world.executions["f42-detect-live"] = jw.execution_raw("f42-detect-live", "f42-detect", (w.now - dt.timedelta(hours=1)).isoformat(), None,
                                                              jw.NEW_DIGEST, succeeded=0)
    assert w.stop("BeforeJobsRollback").code == "CHAIN_ACTIVE"


def test_rb_t4_after_a_rollback_with_a_split_service_the_phase_records_it_and_ends_clean(tmp_path):
    w = world(tmp_path)
    w.update_prefix(14)
    w.update(jo.UPDATE_ORDER, jw.ROLLBACK_DIGEST)
    split_traffic(w)
    result = w.run("AfterJobsRollback")
    assert result["blocking"]["services"] == {"blocking": False, "changed": ["f42-api"], "not_one_revision": ["f42-api"]}


def test_rb_t4_after_a_rollback_a_split_service_does_not_hide_a_job_that_is_not_baseline_j(tmp_path):
    w = world(tmp_path)
    w.update_prefix(14)
    w.update(jo.UPDATE_ORDER, jw.ROLLBACK_DIGEST)
    w.update(["f42-collect"], jw.NEW_DIGEST)
    split_traffic(w)
    assert w.stop("AfterJobsRollback").code == "JOB_IMAGE"


def test_rb_t4_after_a_rollback_the_other_service_still_stops_when_it_changed_while_one_is_split(tmp_path):
    w = world(tmp_path)
    w.update_prefix(14)
    w.update(jo.UPDATE_ORDER, jw.ROLLBACK_DIGEST)
    split_traffic(w, ("f42-api",))
    w.world.svc["f42-agent"]["env"]["F42_EXTRA"] = "1"
    assert w.stop("AfterJobsRollback").code == "SERVICE_CHANGED"


def test_rb_t4_the_reviewers_state_ends_clean_end_to_end_b_updated_then_a_service_split(tmp_path):
    from core.setup.tests.update_support import OLD, rollback, started, update

    w, cloud, clock = started(tmp_path)
    update(w, cloud, clock)
    split_traffic(w)
    before = w.run("BeforeJobsRollback", reader=cloud, now=clock.now())
    assert before["blocking"]["services"]["not_one_revision"] == ["f42-api"]
    outcome = rollback(w, cloud, clock)
    assert outcome["complete"] is True and all(cloud.digest_of(job) == OLD for job in so.JOB_NAMES)
    after = w.run("AfterJobsRollback", reader=cloud, now=clock.now())
    assert after["phase"] == "AfterJobsRollback" and after["blocking"]["services"]["blocking"] is False


# RB-T8 (B01): the jobs mode of the helper takes no --tag

def test_rb_t8_the_jobs_mode_of_the_helper_refuses_a_tag_before_any_read(tmp_path, capsys):
    w = world(tmp_path)
    bindings = w.tmp / "jobs-bindings.json"
    bindings.write_text(json.dumps(w.bound), encoding="utf-8")
    reader = w.fake_reader
    reader.calls.clear()
    code = helper.main(["--mode", "jobs", "--phase", "BeforeAnyWrite", "--tag", "rel-b5e1a2c-01", "--bindings", str(bindings), "--evidence", str(w.evidence)],
                       reader_factory=lambda timeouts: reader, bq_factory=lambda bound: w.bq_client(), now=lambda: w.now)
    assert code == 1 and "STOP: MODE" in capsys.readouterr().err and reader.calls == []
    assert helper.main(["--mode", "jobs", "--phase", "BeforeAnyWrite", "--bindings", str(bindings), "--evidence", str(w.evidence)],
                       reader_factory=lambda timeouts: reader, bq_factory=lambda bound: w.bq_client(), now=lambda: w.now) == 0


def test_rb_t4_only_a_split_service_is_recorded_and_every_other_refusal_or_failed_read_of_a_service_still_stops():
    class Raising:
        def __init__(self, error):
            self.error = error

        def service(self, name):
            raise self.error

    for error in (so.Stop("IDENTITY", "x"), so.Stop("WRITE_REFUSED", "x"), so.Probe("a read did not complete"), so.NotFound("f42-api")):
        with pytest.raises(type(error)):
            jo.tolerant_services(Raising(error))
