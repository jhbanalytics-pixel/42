"""baseline-J capture (W8-REL-B v2.1 section 3.8, JB-05): the 14 job views, the two service states, the post-A serving state
and Release A's terminal readback, captured read only after Release A. No cloud access: the services-only fake world."""
import copy
import json
from pathlib import Path

import pytest

from core.setup.release import jobs_baseline as jb
from core.setup.release import services_only as so
from core.setup.tests import release_world as rw

A_ID = rw.RID
AT = "2026-10-12T09:41:00+00:00"
CAPTURED = "2026-10-12T10:30:00+00:00"
ROLLBACK = rw.JOBS_DIGEST


def promoted(retired=False, tag=A_ID):
    world = rw.World()
    for name in so.SERVICES:
        world.deploy_candidate(name, rid=tag)
        world.promote(name, rid=tag)
        if retired:
            world.svc[name]["traffic"] = [{"revisionName": f"{name}-{tag}", "percent": 100}]
    return world


def rolled_back(retired=False):
    world = rw.World()
    for name in so.SERVICES:
        world.deploy_candidate(name)
        world.promote(name)
        world.restore(name, keep_tag=not retired)
    return world


def terminal(kind="AfterPromotion", a_id=A_ID, at=AT):
    return {"kind": kind, "at_utc": at, "a_release_id": a_id}


A80 = dict(rw.A80_REV)


def capture(world, *, kind="AfterPromotion", retire="waits_for_b", a80=A80, rollback=ROLLBACK, a_id=A_ID, at=AT, captured=CAPTURED):
    reader = rw.FakeReader(world)
    baseline = jb.capture_baseline_j(reader, a_terminal=terminal(kind, a_id, at), a80_serving=a80, rollback_digest=rollback,
                                     a_retire=retire, captured_utc=captured)
    return reader, baseline


def refused(world, code="BASELINE", **kw):
    with pytest.raises(so.Stop) as stop:
        capture(world, **kw)
    assert stop.value.code == code, stop.value.message
    return stop.value


def test_jb05_after_a_promotion_the_baseline_holds_fourteen_jobs_two_services_the_serving_state_and_a_terminal():
    reader, baseline = capture(promoted())
    assert baseline["schema_version"] == 1 and baseline["kind"] == "baseline-J" and baseline["captured_utc"] == CAPTURED
    assert set(baseline["jobs"]) == set(so.JOB_NAMES) and len(baseline["jobs"]) == 14
    for name in so.JOB_NAMES:
        assert baseline["jobs"][name] == so.job_view(rw.FakeReader(promoted()).job(name))
    assert set(baseline["services"]) == set(so.SERVICES)
    for name in so.SERVICES:
        assert baseline["services"][name]["serving"]["name"] == f"{name}-{A_ID}"
        assert set(baseline["services"][name]) == {"annotations", "template", "traffic", "revisions", "serving"}
    assert baseline["aTerminal"] == {"kind": "AfterPromotion", "at_utc": AT, "a_release_id": A_ID}
    assert baseline["aRetire"] == "waits_for_b" and baseline["rollbackJobsDigest"] == ROLLBACK
    assert all(call[0] in ("job", "service", "revision", "revisions") for call in reader.calls)  # read only


def test_jb05_the_service_part_equals_what_the_release_b_readbacks_compare_against():
    # the same shape services_only.capture_baseline stores for baseline-A, so a later live read compares equal
    world = promoted()
    _, baseline = capture(world)
    reader = rw.FakeReader(world)
    for name in so.SERVICES:
        view = so.service_view(reader.service(name))
        serving = [e for e in view["traffic"]["status"] if e["percent"] > 0][0]
        assert baseline["services"][name] == {"annotations": view["annotations"], "template": view["template"],
                                              "traffic": view["traffic"], "revisions": sorted(reader.revisions(name)),
                                              "serving": so.revision_view(reader.revision(serving["revision"]))}


def test_jb05_after_a_rollback_the_baseline_holds_the_a80_pair_by_name():
    _, baseline = capture(rolled_back(), kind="AfterRollback")
    for name in so.SERVICES:
        assert baseline["services"][name]["serving"]["name"] == A80[name]
    assert baseline["aTerminal"]["kind"] == "AfterRollback" and baseline["a80Serving"] == A80


def test_jb05_a_retired_tag_set_is_captured_as_retired_and_a_pending_retire_as_waiting_for_b():
    _, retired = capture(promoted(retired=True), retire="retired")
    assert retired["aRetire"] == "retired"
    _, waiting = capture(promoted(), retire="waits_for_b")
    assert waiting["aRetire"] == "waits_for_b"
    _, rolled = capture(rolled_back(retired=True), kind="AfterRollback", retire="retired")
    assert rolled["aRetire"] == "retired"


def test_jb05_a_retire_that_is_not_declared_is_not_guessed_from_the_live_tags():
    for declared in (None, "", "pending", "unknown", True):
        refused(promoted(), retire=declared)
        refused(promoted(retired=True), retire=declared)


def test_jb05_a_declaration_that_the_live_tags_contradict_is_refused():
    refused(promoted(), retire="retired")  # A's tag is still on both services
    refused(promoted(retired=True), retire="waits_for_b")  # the tag is gone
    half = promoted()
    half.svc["f42-api"]["traffic"] = [{"revisionName": f"f42-api-{A_ID}", "percent": 100}]
    refused(half, retire="waits_for_b")
    refused(half, retire="retired")


def test_jb05_fourteen_images_that_are_not_one_digest_are_refused():
    world = promoted()
    other = "sha256:" + "ab" * 32
    spec = world.jobs["f42-drift"]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
    spec["image"] = spec["image"].split("@")[0] + "@" + other
    error = refused(world)
    assert "one digest" in error.message


def test_jb05_one_digest_that_is_not_the_bound_rollback_digest_is_refused():
    error = refused(promoted(), rollback="sha256:" + "ab" * 32)
    assert "rollback digest" in error.message
    for malformed in ("e77c", None, "sha256:" + "AB" * 32):
        assert "not a sha256 digest" in refused(promoted(), rollback=malformed).message


def test_jb05_a_service_that_does_not_serve_one_revision_at_100_percent_is_refused():
    world = promoted()
    world.svc["f42-api"]["traffic"] = [{"revisionName": f"f42-api-{A_ID}", "percent": 50, "tag": A_ID},
                                       {"revisionName": rw.A80_REV["f42-api"], "percent": 50}]
    refused(world)


def test_jb05_a_service_state_that_is_neither_the_promoted_candidates_nor_the_restored_a80_pair_is_refused():
    refused(rw.World())  # the a80 pair with A never run, declared as AfterPromotion
    refused(rw.World(), kind="AfterRollback")  # the a80 pair with A never run, declared as AfterRollback
    refused(promoted(), kind="AfterRollback")  # candidates serving, A said it rolled back
    refused(rolled_back(), kind="AfterPromotion")  # the a80 pair serving, A said it promoted
    mixed = rw.World()
    mixed.deploy_candidate("f42-agent")
    mixed.promote("f42-agent")
    refused(mixed)
    refused(mixed, kind="AfterRollback")


def test_jb05_the_expected_serving_names_come_from_the_bound_inputs_not_from_the_live_candidate_tag():
    world = promoted(tag="rel-d666ef6-02")  # the live candidates are another attempt's
    refused(world)  # the terminal says rel-d666ef6-01
    _, baseline = capture(world, a_id="rel-d666ef6-02")
    assert baseline["aTerminal"]["a_release_id"] == "rel-d666ef6-02"
    refused(promoted(), code="A_STATE", a_id="rel-xyz")
    refused(promoted(), code="A_STATE", a_id=None)


def test_jb05_after_a_rollback_the_a80_names_must_be_given_and_must_match_the_live_pair():
    refused(rolled_back(), kind="AfterRollback", a80=None)
    refused(rolled_back(), kind="AfterRollback", a80={"f42-agent": rw.A80_REV["f42-agent"]})
    refused(rolled_back(), kind="AfterRollback", a80={**A80, "f42-api": rw.OLDER_REV["f42-api"]})


def test_jb05_the_terminal_readback_must_be_a_known_kind_with_an_aware_time_that_is_not_after_the_capture():
    refused(promoted(), code="A_STATE", kind="AfterSmoke")
    refused(promoted(), code="A_STATE", kind=None)
    refused(promoted(), code="A_STATE", at="2026-10-12T09:41:00")
    refused(promoted(), code="A_STATE", at="yesterday")
    refused(promoted(), code="A_STATE", at="2026-10-12T11:00:00+00:00")
    refused(promoted(), code="A_STATE", at=None)


def test_jb05_a_missing_job_is_not_papered_over():
    world = promoted()
    del world.jobs["f42-learn"]
    with pytest.raises(KeyError):
        capture(world)


def test_jb05_the_baseline_is_written_once_and_its_hash_is_the_hash_of_the_bytes(tmp_path):
    _, baseline = capture(promoted())
    path = tmp_path / "baseline-J.json"
    digest = jb.write_once(path, baseline)
    assert digest == so.sha_bytes(path.read_bytes())
    assert json.loads(path.read_text(encoding="utf-8")) == baseline
    assert so.load_bound_json(path, digest, "baseline-J") == baseline
    with pytest.raises(so.Stop) as stop:
        jb.write_once(path, baseline)
    assert stop.value.code == "BASELINE"
    assert so.sha_bytes(path.read_bytes()) == digest
