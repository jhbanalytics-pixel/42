"""The checks the jobs readbacks carry for the update path (W8-REL-B v2.1 JU-03, JU-05, JU-06, JU-09, JU-10): the per job readback, the update prefix, the
window, the bindings and ages, and the A_STATE rule."""
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


def test_ju07_the_quiet_snapshot_templates_are_recomputed_against_the_hashes_the_bindings_bind(tmp_path, monkeypatch):
    from core.setup.release import jobs_run as jr
    from core.setup.tests import cloud_world as cw

    w = world(tmp_path)
    candidate_ran(w)
    changed = dict(jo.QUIET_TEMPLATES)
    changed["chain_rows"] = changed["chain_rows"] + " LIMIT 1"
    monkeypatch.setattr(jo, "QUIET_TEMPLATES", changed)
    assert w.stop("BeforeJobsUpdate").code == "BINDINGS"
    cloud = cw.FakeCloudRun(w.world, cw.Clock(w.now))
    with pytest.raises(so.Stop) as stop:
        jr.run_snapshot(w.bound, cloud, w.bq_client(), w.evidence, now=lambda: w.now)
    assert stop.value.code == "BINDINGS" and cloud.argv_calls == []


def test_ju07_a_bindings_file_without_the_quiet_template_hashes_is_refused(tmp_path):
    w = world(tmp_path)
    broken = {k: v for k, v in w.bound.items() if k != "quietTemplateHashes"}
    with pytest.raises(so.Stop) as stop:
        jo.validate_jobs_bindings(broken)
    assert stop.value.code == "BINDINGS" and "quietTemplateHashes" in stop.value.message


# RB-T3 (F3): JobsUpdate needs the schema readback receipt JobsCandidate's durable readbacks step leaves in the release directory. The
# bindings name its path and its sha256, and BeforeJobsUpdate recomputes what the receipt must say from the pinned durable manifest
# instead of believing what the receipt says about itself.

def receipt_path(w):
    from pathlib import Path

    return Path(w.bound["schemaReadbackReceiptPath"])


def candidate_without_receipt(w):
    w.run("BeforeAnyWrite")
    w.built(receipt=False)


def test_rb_t3_before_jobs_update_passes_with_the_receipt_and_stops_without_it(tmp_path):
    w = world(tmp_path / "with")
    candidate_ran(w)
    assert w.run("BeforeJobsUpdate")["phase"] == "BeforeJobsUpdate"
    bare = world(tmp_path / "without")
    candidate_without_receipt(bare)
    assert bare.stop("BeforeJobsUpdate").code == "SCHEMA_RECEIPT"


def test_rb_t3_a_candidate_that_stopped_at_its_snapshot_leaves_no_receipt_and_the_update_is_refused(tmp_path):
    w = world(tmp_path)
    w.run("BeforeAnyWrite")  # JobsCandidate stopped before the schema apply and its readbacks
    w.world.registry[jo.image_tag(w.bound)] = jw.NEW_DIGEST
    assert not receipt_path(w).exists()
    assert w.stop("BeforeJobsUpdate").code == "SCHEMA_RECEIPT"


@pytest.mark.parametrize("over", [
    {"effects": []},
    {"effects": [{"effect_id": "E-RUNS-DDL", "result_sha256": jw.sha("runs columns")}]},
    {"effects": [{"effect_id": "E-RUNS-DDL", "result_sha256": jw.sha("runs columns")}, {"effect_id": "E-CLAIM-DDL", "result_sha256": jw.sha("other")}]},
    {"effects": [{"effect_id": "E-RUNS-DDL", "result_sha256": jw.sha("runs columns")}, {"effect_id": "E-CLAIM-DDL", "result_sha256": jw.sha("claim_checks columns")},
                 {"effect_id": "E-EXTRA", "result_sha256": jw.sha("x")}]},
    {"release_id": "rel-0000000-01"},
    {"durable_manifest_sha256": "0" * 64},
    {"kind": "chain-evidence"},
    {"schema_version": 2},
], ids=["no_effects", "one_effect_missing", "wrong_readback_hash", "extra_effect", "other_release", "other_manifest", "other_kind", "other_version"])
def test_rb_t3_a_receipt_that_is_self_consistent_but_differs_from_what_the_pinned_manifest_expects_is_refused(tmp_path, over):
    # schema_receipt() writes the receipt_sha256 of the altered body itself, so only a recomputation from the manifest can refuse it.
    w = world(tmp_path)
    w.run("BeforeAnyWrite")
    w.built(receipt=False)
    w.schema_receipt(**over)
    assert w.stop("BeforeJobsUpdate").code == "SCHEMA_RECEIPT"


def test_rb_t3_a_receipt_whose_own_hash_field_is_not_the_hash_of_its_body_is_refused(tmp_path):
    w = world(tmp_path)
    candidate_ran(w)
    path = receipt_path(w)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["receipt_sha256"] = "0" * 64
    path.write_text(json.dumps(body), encoding="utf-8")
    assert w.stop("BeforeJobsUpdate").code == "SCHEMA_RECEIPT"


def test_rb_t3_a_receipt_that_is_not_json_or_not_an_object_is_refused(tmp_path):
    for index, text in enumerate(("not json", "[]", "")):
        w = world(tmp_path / str(index))
        candidate_ran(w)
        receipt_path(w).write_text(text, encoding="utf-8")
        assert w.stop("BeforeJobsUpdate").code == "SCHEMA_RECEIPT", repr(text)


def test_rb_t3_the_bound_hash_is_what_the_receipt_must_match_and_a_different_bound_hash_refuses(tmp_path):
    w = world(tmp_path)
    candidate_ran(w)
    w.bound["schemaReadbackReceiptSha256"] = "0" * 64
    assert w.stop("BeforeJobsUpdate").code == "SCHEMA_RECEIPT"


def test_rb_t3_a_durable_manifest_that_changed_after_it_was_bound_stops_the_check_with_bindings(tmp_path):
    w = world(tmp_path)
    candidate_ran(w)
    path = w.release_dir / "durable-effects.json"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert w.stop("BeforeJobsUpdate").code == "BINDINGS"


@pytest.mark.parametrize("key", ["schemaReadbackReceiptPath", "schemaReadbackReceiptSha256"])
def test_rb_t3_bindings_without_the_receipt_path_or_hash_are_refused(tmp_path, key):
    w = world(tmp_path)
    w.bound.pop(key)
    with pytest.raises(so.Stop) as stop:
        jo.validate_jobs_bindings(w.bound)
    assert stop.value.code == "BINDINGS"


def test_rb_t3_a_receipt_path_outside_the_release_directory_or_a_malformed_hash_is_refused(tmp_path):
    w = world(tmp_path)
    for key, value in (("schemaReadbackReceiptPath", str(tmp_path / "elsewhere" / "schema-readback-receipt.json")),
                       ("schemaReadbackReceiptPath", str(w.release_dir / ".." / "schema-readback-receipt.json")),
                       ("schemaReadbackReceiptSha256", "abc"), ("schemaReadbackReceiptSha256", "G" * 64)):
        bound = dict(w.bound, **{key: value})
        with pytest.raises(so.Stop) as stop:
            jo.validate_jobs_bindings(bound)
        assert stop.value.code == "BINDINGS", (key, value)


def test_rb_t3_the_update_orchestrator_refuses_without_the_receipt_before_any_update_call(tmp_path):
    from core.setup.release import jobs_run as jr
    from core.setup.tests import cloud_world as cw

    w = world(tmp_path)
    clock = cw.Clock(w.now)
    cloud = cw.FakeCloudRun(w.world, clock)
    w.fake_reader = cloud
    w.run("BeforeAnyWrite", reader=cloud)
    w.built()
    w.run("BeforeJobsUpdate", reader=cloud)
    receipt_path(w).unlink()
    with pytest.raises(so.Stop) as stop:
        jr.run_update(w.bound, cloud, w.bq_client(), w.evidence, now=clock.now)
    assert stop.value.code == "SCHEMA_RECEIPT" and cloud.update_calls == 0


# the verb that writes the receipt: it reads the checker's output and the pinned manifest and writes only a receipt that agrees

def checker_output(w, **over):
    results = [{"effect_id": e["effect_id"], "result_sha256": e["native_readback"]["expected_result_sha256"], "matches": True}
               for e in jw.DURABLE_EFFECTS if e["apply_kind"] == "schema"]
    results.append({"effect_id": "E-JOBS-IMAGE", "helper_phase": "AfterJobsUpdate", "matches": True})
    body = {"schema_version": 1, "check": "readbacks", "ok": True, "results": results, **over}
    jw.write_json(w.evidence / "readbacks.json", body)
    return body


def receipt_verb(w, clock=None):
    from core.setup.release import jobs_run as jr

    path = w.tmp / "bindings-file.json"
    jw.write_json(path, w.bound)
    return jr.main(["schema-receipt", "--bindings", str(path), "--evidence", str(w.evidence)], now=(clock or (lambda: w.now)))


def test_rb_t3_the_receipt_verb_writes_the_receipt_the_bindings_name_and_beforejobsupdate_then_accepts_it(tmp_path):
    w = world(tmp_path)
    w.run("BeforeAnyWrite")
    w.world.registry[jo.image_tag(w.bound)] = jw.NEW_DIGEST
    checker_output(w)
    assert receipt_verb(w) == 0
    written = json.loads(receipt_path(w).read_text(encoding="utf-8"))
    expected = w.schema_receipt_body(w.bound["release_id"], w.bound["durableManifestSha256"])
    assert {k: v for k, v in written.items() if k not in ("receipt_sha256", "written_at")} == expected
    assert written["receipt_sha256"] == w.bound["schemaReadbackReceiptSha256"] and written["written_at"]
    assert w.run("BeforeJobsUpdate")["phase"] == "BeforeJobsUpdate"


def test_rb_t3_the_receipt_verb_writes_it_again_unchanged_but_never_replaces_a_different_one(tmp_path):
    w = world(tmp_path)
    checker_output(w)
    assert receipt_verb(w) == 0
    first = receipt_path(w).read_bytes()
    assert receipt_verb(w) == 0 and receipt_path(w).read_bytes() == first
    receipt_path(w).write_text(json.dumps({"kind": "other"}), encoding="utf-8")
    assert receipt_verb(w) == 1


@pytest.mark.parametrize("make", [
    lambda w: checker_output(w, ok=False),
    lambda w: checker_output(w, check="rollback-blockers"),
    lambda w: checker_output(w, results=[]),
    lambda w: checker_output(w, results=[{"effect_id": "E-RUNS-DDL", "result_sha256": jw.sha("runs columns"), "matches": True}]),
    lambda w: checker_output(w, results=[{"effect_id": "E-RUNS-DDL", "result_sha256": jw.sha("runs columns"), "matches": True},
                                         {"effect_id": "E-CLAIM-DDL", "result_sha256": jw.sha("something else"), "matches": True}]),
    lambda w: checker_output(w, results=[{"effect_id": "E-RUNS-DDL", "result_sha256": jw.sha("runs columns"), "matches": True},
                                         {"effect_id": "E-CLAIM-DDL", "result_sha256": jw.sha("claim_checks columns"), "matches": False}]),
    lambda w: (w.evidence / "readbacks.json").write_text("not json", encoding="utf-8"),
    lambda w: None,
], ids=["not_ok", "other_check", "no_results", "one_effect_missing", "a_different_hash_that_claims_a_match", "matches_false", "unreadable", "absent"])
def test_rb_t3_the_receipt_verb_refuses_a_checker_output_that_does_not_agree_with_the_pinned_manifest(tmp_path, make):
    w = world(tmp_path)
    make(w)
    assert receipt_verb(w) == 1 and not receipt_path(w).exists()


def test_rb_t3_the_receipt_verb_refuses_when_the_bound_hash_is_not_what_this_manifest_produces(tmp_path):
    w = world(tmp_path)
    checker_output(w)
    w.bound["schemaReadbackReceiptSha256"] = "0" * 64
    assert receipt_verb(w) == 1 and not receipt_path(w).exists()


def test_rb_t3_the_receipt_verb_needs_neither_a_console_nor_a_token(tmp_path):
    w = world(tmp_path)
    checker_output(w)
    assert receipt_verb(w) == 0


# RB-T7 (F7): when baseline-J says Release A was rolled back, the live revisions must still hold Release A's candidate revision
# f42-<service>-<a_release_id>. The a80 pair with Release A never run is neither state, whatever baseline-J claims about itself.

def never_ran_world(tmp_path, *, a_kind="AfterRollback", a_release_id=jw.A_RID):
    """Both services on the a80 revisions with no candidate revision of any release, and a baseline-J captured from that world."""
    w = jw.ReleaseWorld(tmp_path)
    for name in so.SERVICES:
        w.world.revisions.pop(rw.CAND_REV[name], None)
        w.world.revision_names[name] = [r for r in w.world.revision_names[name] if r != rw.CAND_REV[name]]
        w.world.svc[name].update(latest_created=rw.A80_REV[name], latest_ready=rw.A80_REV[name], traffic=[{"revisionName": rw.A80_REV[name], "percent": 100}])
    baseline = w.baseline_j(a_kind=a_kind)
    baseline["aTerminal"]["a_release_id"] = a_release_id
    w.prepare(baseline=baseline)
    return w


def test_rb_t7_a_baseline_j_that_says_rolled_back_when_no_candidate_revision_of_a_ever_existed_stops_before_any_write(tmp_path):
    w = never_ran_world(tmp_path)
    assert w.stop("BeforeAnyWrite").code == "A_STATE"


def test_rb_t7_the_same_world_is_refused_at_beforejobsupdate_too(tmp_path):
    w = never_ran_world(tmp_path)
    # BeforeAnyWrite itself stops in this world, so the candidate readback BeforeJobsUpdate looks for is written by hand, in its real shape.
    jw.write_json(w.release_dir / "readbacks" / "BeforeAnyWrite-01.json", {"schema_version": 1, "mode": "jobs", "phase": "BeforeAnyWrite",
                                                                          "release_id": w.bound["release_id"], "at_utc": w.now.isoformat()})
    w.built()
    assert w.stop("BeforeJobsUpdate").code == "A_STATE"


def test_rb_t7_a_candidate_revision_of_another_release_does_not_stand_in_for_release_a(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    for name in so.SERVICES:
        w.world.restore(name)
        w.world.svc[name].update(latest_created=rw.A80_REV[name], latest_ready=rw.A80_REV[name])
    baseline = w.baseline_j(a_kind="AfterRollback")
    baseline["aTerminal"]["a_release_id"] = "rel-0badf00-02"
    w.prepare(baseline=baseline)
    assert w.stop("BeforeAnyWrite").code == "A_STATE"


def test_rb_t7_a_rolled_back_baseline_that_names_no_release_id_for_a_is_refused(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    for name in so.SERVICES:
        w.world.restore(name)
        w.world.svc[name].update(latest_created=rw.A80_REV[name], latest_ready=rw.A80_REV[name])
    baseline = w.baseline_j(a_kind="AfterRollback")
    baseline["aTerminal"].pop("a_release_id")
    w.prepare(baseline=baseline)
    assert w.stop("BeforeAnyWrite").code == "A_STATE"


def test_rb_t7_a_world_where_a_ran_and_was_rolled_back_is_still_accepted(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    for name in so.SERVICES:
        w.world.restore(name)
        w.world.svc[name].update(latest_created=rw.A80_REV[name], latest_ready=rw.A80_REV[name])
    w.prepare(baseline=w.baseline_j(a_kind="AfterRollback"))
    assert w.run("BeforeAnyWrite")["phase"] == "BeforeAnyWrite"


def test_rb_t7_the_promoted_branch_is_unchanged_a_serving_revision_named_for_a_and_nothing_else(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    w.prepare()
    assert w.run("BeforeAnyWrite")["phase"] == "BeforeAnyWrite"


# RB-T8 (F8, F11, F13, F14 and the reviewer's real mutant survivors O09, O12, O13, O29)

PINNED_WINDOW = {"startSast": "08:05", "endSast": "21:00", "rollbackDeadlineSast": "23:30"}


def test_rb_t8_the_window_the_bindings_carry_is_pinned_to_0805_2100_and_2330_sast(tmp_path):
    w = world(tmp_path)
    assert jo.PINNED_WINDOW == PINNED_WINDOW
    jo.validate_jobs_bindings({**w.bound, "window": dict(PINNED_WINDOW)})
    for change in ({"startSast": "00:00"}, {"startSast": "08:00"}, {"startSast": "08:06"}, {"endSast": "23:59"}, {"endSast": "21:30"},
                   {"endSast": "20:59"}, {"rollbackDeadlineSast": "23:59"}, {"rollbackDeadlineSast": "22:00"}, {"rollbackDeadlineSast": "00:00"}):
        with pytest.raises(so.Stop) as stop:
            jo.validate_jobs_bindings({**w.bound, "window": {**PINNED_WINDOW, **change}})
        assert stop.value.code == "BINDINGS", change
    for window in ({"startSast": "08:05", "endSast": "21:00"}, {**PINNED_WINDOW, "extra": "12:00"}):
        with pytest.raises(so.Stop) as stop:
            jo.validate_jobs_bindings({**w.bound, "window": window})
        assert stop.value.code == "BINDINGS"


def test_rb_t8_a_window_of_the_whole_day_no_longer_lets_jobs_update_complete_at_2130_sast(tmp_path):
    w = world(tmp_path)
    candidate_ran(w)
    w.bound["window"] = {"startSast": "00:00", "endSast": "23:59", "rollbackDeadlineSast": "23:30"}
    with pytest.raises(so.Stop) as stop:
        w.run("BeforeJobsUpdate", now=dt.datetime(2026, 10, 11, 19, 30, tzinfo=jw.UTC))
    assert stop.value.code == "BINDINGS"


@pytest.mark.parametrize("value", [31, 100000])
def test_rb_t8_the_collect_start_tolerance_is_capped_at_30_minutes(tmp_path, value):
    w = world(tmp_path)
    for producer in (False, True):
        with pytest.raises(so.Stop) as stop:
            jo.validate_jobs_bindings({**w.bound, "collectStartToleranceMinutes": value}, producer=producer)
        assert stop.value.code == "BINDINGS"


@pytest.mark.parametrize("value", [0, 10, 30])
def test_rb_t8_a_tolerance_up_to_30_minutes_is_accepted(tmp_path, value):
    w = world(tmp_path)
    jo.validate_jobs_bindings({**w.bound, "collectStartToleranceMinutes": value})
    jo.validate_jobs_bindings({**w.bound, "collectStartToleranceMinutes": value}, producer=True)


# O29: the release id names the target commit

@pytest.mark.parametrize("release_id", ["rel-0000000-01", "rel-b5e1a2d-01", "rel-B5E1A2C-01"])
def test_rb_t8_a_release_id_that_does_not_name_the_target_commit_is_refused(tmp_path, release_id):
    w = world(tmp_path)
    with pytest.raises(so.Stop) as stop:
        jo.validate_jobs_bindings({**w.bound, "release_id": release_id})
    assert stop.value.code == "BINDINGS"


# O09: the image tag of this attempt must not exist before the build

def test_rb_t8_an_image_tag_that_already_resolves_in_the_registry_stops_before_any_write(tmp_path):
    w = world(tmp_path)
    w.world.registry[jo.image_tag(w.bound)] = jw.NEW_DIGEST
    assert w.stop("BeforeAnyWrite").code == "TAG_MOVED"


# O12 and O13: a by-line baseline is rechecked against its own recorded degradation

def by_line_world(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    w.set_cluster_error("KE")
    w.set_cluster_error("NG")
    w.prepare()
    assert baseline_manifest(w)["verdict"]["baseline_by_line"] is True
    return w


def test_rb_t8_a_by_line_baseline_that_also_fails_another_rule_is_refused(tmp_path):
    for index, extra in enumerate(("ORDER", "IMAGE", "MANUAL", "LINEAGE")):
        w = by_line_world(tmp_path / str(index))
        manifest = baseline_manifest(w)
        manifest["verdict"]["reasons"] = ["DEGRADED", extra]
        rebind(w, manifest)
        assert w.stop("BeforeAnyWrite").code == "BASELINE", extra


@pytest.mark.parametrize("lines", [{"KE": "ValueError: something else broke", "NG": "ValueError: something else broke"},
                                   {"KE": "cannot round-trip through string representation", "NG": "PARSE_JSON in some other place"},
                                   {"KE": "PARSE_JSON", "NG": "cannot round-trip through string representation of a float"}])
def test_rb_t8_a_by_line_baseline_whose_recorded_first_lines_lack_the_signature_is_refused(tmp_path, lines):
    w = by_line_world(tmp_path)
    manifest = baseline_manifest(w)
    manifest["stages"]["understand"]["degradation"]["cluster_error_first_lines"] = lines
    rebind(w, manifest)
    assert w.stop("BeforeAnyWrite").code == "BASELINE"


def test_rb_t8_a_by_line_baseline_that_also_shows_an_embed_error_a_partial_run_or_no_recorded_line_is_refused(tmp_path):
    for index, change in enumerate(({"embed_error": True}, {"enrich_error": True}, {"partial": True}, {"cluster_error_first_lines": {}})):
        w = by_line_world(tmp_path / str(index))
        manifest = baseline_manifest(w)
        manifest["stages"]["understand"]["degradation"].update(change)
        rebind(w, manifest)
        assert w.stop("BeforeAnyWrite").code == "BASELINE", change


# F11: the readbacks a later action trusts must be this release's, in this mode, in this version, for this phase

def candidate_file(w):
    return sorted((w.release_dir / "readbacks").glob("BeforeAnyWrite-*.json"))[-1]


@pytest.mark.parametrize("change", [{"mode": "services-only"}, {"release_id": "rel-0badf00-02"}, {"schema_version": 2}, {"phase": "BeforeJobsUpdate"}])
def test_rb_t8_a_candidate_readback_of_another_mode_release_version_or_phase_is_not_taken_as_the_candidate(tmp_path, change):
    w = world(tmp_path)
    candidate_ran(w)
    path = candidate_file(w)
    body = json.loads(path.read_text(encoding="utf-8"))
    body.update(change)
    path.write_text(json.dumps(body), encoding="utf-8")
    assert w.stop("BeforeJobsUpdate").code == "READBACK_IDENTITY"


def test_rb_t8_a_newer_readback_file_of_another_release_in_the_same_directory_is_not_skipped_over(tmp_path):
    w = world(tmp_path)
    candidate_ran(w)
    body = json.loads(candidate_file(w).read_text(encoding="utf-8"))
    body["release_id"] = "rel-0badf00-02"
    jw.write_json(w.release_dir / "readbacks" / "BeforeAnyWrite-02.json", body)
    assert w.stop("BeforeJobsUpdate").code == "READBACK_IDENTITY"


@pytest.mark.parametrize("change", [{"mode": "services-only"}, {"release_id": "rel-0badf00-02"}, {"schema_version": 2}, {"phase": "BeforeAnyWrite"}])
def test_rb_t8_the_update_orchestrator_refuses_a_before_update_readback_of_another_mode_release_version_or_phase(tmp_path, change):
    from core.setup.release import jobs_run as jr
    from core.setup.tests.update_support import started

    w, cloud, clock = started(tmp_path)
    path = sorted((w.release_dir / "readbacks").glob("BeforeJobsUpdate-*.json"))[-1]
    body = json.loads(path.read_text(encoding="utf-8"))
    body.update(change)
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(so.Stop) as stop:
        jr.run_update(w.bound, cloud, w.bq_client(), w.evidence, now=clock.now)
    assert stop.value.code == "READBACK_IDENTITY" and cloud.update_calls == 0


# F13: the quiet snapshot and the rollback look at the same executions

def stale(w, job, name, days=5, **over):
    w.world.executions[name] = jw.execution_raw(name, job, (w.now - dt.timedelta(days=days)).isoformat(), None, jw.ROLLBACK_DIGEST, succeeded=0, **over)


def snapshot_of(w):
    from core.setup.release import jobs_run as jr

    return jo.quiet_snapshot(w.fake_reader, jr.runner_for(w.bound, w.bq_client()), w.now)


def test_rb_t8_a_five_day_old_chain_execution_with_no_completion_time_is_active_in_the_snapshot_and_in_the_list(tmp_path):
    w = world(tmp_path)
    stale(w, "f42-collect", "f42-collect-stale")
    snapshot = snapshot_of(w)
    assert snapshot["active"]["chain"] == ["f42-collect-stale"]
    assert {"name": "f42-collect-stale", "state": "active"} in snapshot["executions"]["f42-collect"]
    with pytest.raises(so.Stop) as stop:
        jo.assert_quiet(snapshot)
    assert stop.value.code == "CHAIN_ACTIVE"


def test_rb_t8_before_jobs_update_stops_on_that_execution_and_so_do_the_rollback_checks(tmp_path):
    w = world(tmp_path)
    candidate_ran(w)
    stale(w, "f42-detect", "f42-detect-stale")
    assert w.stop("BeforeJobsUpdate").code == "CHAIN_ACTIVE"
    w.update_prefix(14)
    assert w.stop("BeforeJobsRollback").code == "CHAIN_ACTIVE"


def test_rb_t8_an_execution_with_no_start_time_and_no_completion_time_is_active_too(tmp_path):
    w = world(tmp_path)
    raw = jw.execution_raw("f42-brief-queued", "f42-brief", w.now.isoformat(), None, jw.ROLLBACK_DIGEST, succeeded=0)
    raw["status"].pop("startTime")
    w.world.executions["f42-brief-queued"] = raw
    assert snapshot_of(w)["active"]["chain"] == ["f42-brief-queued"]


def test_rb_t8_a_stale_active_side_execution_is_recorded_and_allowed(tmp_path):
    w = world(tmp_path)
    stale(w, "f42-watchdog", "f42-watchdog-stale")
    snapshot = snapshot_of(w)
    assert snapshot["active"]["side"] == ["f42-watchdog-stale"] and snapshot["active"]["chain"] == []
    jo.assert_quiet(snapshot)


def test_rb_t8_the_listed_executions_keep_only_today_yesterday_and_the_active_ones(tmp_path):
    # O03: the date filter of the listing
    w = world(tmp_path)
    old = jw.execution_raw("f42-detect-old", "f42-detect", (w.now - dt.timedelta(days=5)).isoformat(), (w.now - dt.timedelta(days=5, hours=-1)).isoformat(),
                           jw.ROLLBACK_DIGEST)
    today = jw.execution_raw("f42-detect-today", "f42-detect", (w.now - dt.timedelta(hours=1)).isoformat(), w.now.isoformat(), jw.ROLLBACK_DIGEST)
    w.world.executions.update({"f42-detect-old": old, "f42-detect-today": today})
    names = [e["name"] for e in snapshot_of(w)["executions"]["f42-detect"]]
    assert "f42-detect-old" not in names and "f42-detect-today" in names


# F14: Q3, B's baseline is one of the first two chains after Release A

def chain_world(tmp_path, *, run_day, now_day, at_utc="2026-10-09T10:00:00+00:00"):
    w = jw.ReleaseWorld(tmp_path, day=dt.date(2026, 10, run_day))
    w.now = dt.datetime(2026, 10, now_day, 8, 0, tzinfo=jw.UTC)
    w.prepare(baseline=w.baseline_j(at_utc=at_utc))
    return w


@pytest.mark.parametrize("run_day,now_day,accepted", [(10, 11, True), (11, 12, True), (12, 13, False), (13, 13, False)])
def test_rb_t8_only_the_first_two_chains_after_a_terminal_readback_at_noon_sast_can_be_the_baseline(tmp_path, run_day, now_day, accepted):
    w = chain_world(tmp_path, run_day=run_day, now_day=now_day)
    if accepted:
        assert w.run("BeforeAnyWrite")["phase"] == "BeforeAnyWrite"
    else:
        stop = w.stop("BeforeAnyWrite")
        assert stop.code == "BASELINE" and "first two chains" in stop.message


@pytest.mark.parametrize("terminal,run_day,now_day,accepted", [
    ("2026-10-09T23:30:00+00:00", 10, 11, True),   # 01:30 SAST on the 10th, before the day's 02:00 collect: the chain of the 10th is the first
    ("2026-10-09T23:30:00+00:00", 11, 12, True),
    ("2026-10-09T23:30:00+00:00", 12, 12, False),
    ("2026-10-10T00:30:00+00:00", 12, 12, True),   # 02:30 SAST on the 10th, after that day's collect start: the 11th is the first
    ("2026-10-10T00:30:00+00:00", 13, 13, False),
])
def test_rb_t8_the_first_chain_is_the_one_of_the_same_sast_day_only_when_the_terminal_readback_came_before_0200(tmp_path, terminal, run_day, now_day, accepted):
    w = chain_world(tmp_path, run_day=run_day, now_day=now_day, at_utc=terminal)
    if accepted:
        assert w.run("BeforeAnyWrite")["phase"] == "BeforeAnyWrite"
    else:
        assert w.stop("BeforeAnyWrite").code == "BASELINE"


def test_rb_t8_a_by_line_baseline_outside_the_first_two_chains_is_refused_as_well(tmp_path):
    w = jw.ReleaseWorld(tmp_path, day=dt.date(2026, 10, 12))
    w.now = dt.datetime(2026, 10, 13, 8, 0, tzinfo=jw.UTC)
    w.set_cluster_error("KE")
    w.set_cluster_error("NG")
    w.prepare()
    assert baseline_manifest(w)["verdict"]["baseline_by_line"] is True
    assert w.stop("BeforeAnyWrite").code == "BASELINE"
