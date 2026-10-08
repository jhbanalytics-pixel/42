"""The services-only readback helper (W8-REL 5.3, HR-01 to HR-54, RT-01 to RT-05). No cloud, no network.

A Scenario walks a simulated Cloud Run world through the release phases; a test breaks one thing and runs the next
phase. The helper is judged on the code it prints after STOP, so a stop for the wrong reason fails.
"""
import ast
import copy
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.setup.release import bound_readback as helper
from core.setup.release import services_only as so
from core.setup.tests.release_world import (
    A80_DIGEST, A80_REV, API_ENV, BUILD_ID, BUILD_SA, CAND_DIGEST, CAND_REV, CANON, COMMIT, HEALTHY, JOBS_DIGEST,
    OLDER_REV, OTHER_DIGEST, REPO, RID, SECRET, SHORT12, TAG_URL, Scenario, World, FakeReader, write_json)

SERVICES = so.SERVICES
RID2 = "rel-d666ef6-02"


def container(world, revision):
    return world.revisions[revision]["spec"]["containers"][0]


def stops_with(scenario, phase, code, fragment=None):
    error = scenario.stop(phase)
    assert error.code == code, f"{phase} stopped with {error.code}: {error.message}"
    if fragment:
        assert fragment in error.message, error.message
    return error


# baseline, mode, versions

def test_hr01_before_any_write_compares_with_the_bound_baseline_and_creates_no_expectation(tmp_path):
    s = Scenario(tmp_path)
    before = s.baseline_path.read_bytes()
    result = s.run("BeforeAnyWrite")
    assert result["phase"] == "BeforeAnyWrite" and result["mode"] == "services-only"
    assert s.baseline_path.read_bytes() == before
    assert not (s.release_dir / "release-manifest.json").exists()


def test_hr02_a_mode_that_is_not_services_only_stops_in_the_bindings_and_on_the_flag(tmp_path, capsys):
    s = Scenario(tmp_path)
    bound_full = {**s.bound, "mode": "full"}
    path = write_json(tmp_path / "full-bindings.json", bound_full) and tmp_path / "full-bindings.json"
    assert helper.main(["--phase", "BeforeAnyWrite", "--bindings", str(path), "--evidence", str(s.evidence)],
                       reader_factory=lambda t: s.reader) == 1
    assert "STOP: MODE" in capsys.readouterr().err
    good = tmp_path / "bindings.json"
    write_json(good, s.bound)
    assert helper.main(["--mode", "full", "--phase", "BeforeAnyWrite", "--bindings", str(good), "--evidence", str(s.evidence)],
                       reader_factory=lambda t: s.reader) == 1
    assert "STOP: MODE" in capsys.readouterr().err
    assert helper.main(["--phase", "BeforeAnyWrite", "--bindings", str(good), "--evidence", str(s.evidence)],
                       reader_factory=lambda t: s.reader) == 0


def test_hr04_an_unknown_schema_version_is_refused_in_every_durable_shape(tmp_path):
    s = Scenario(tmp_path / "bindings")
    s.bound["schema_version"] = 2
    assert s.stop("BeforeAnyWrite").code == "SCHEMA_VERSION"

    s = Scenario(tmp_path / "baseline")
    baseline = json.loads(s.baseline_path.read_text(encoding="utf-8"))
    baseline["schema_version"] = 2
    s.bound["baselineSha256"] = write_json(s.baseline_path, baseline)
    assert s.stop("BeforeAnyWrite").code == "SCHEMA_VERSION"

    for label, kind in (("manifest", "release-manifest"), ("compat", "compat"), ("oldreader", "old-reader")):
        s = Scenario(tmp_path / label)
        s.to("BeforeCandidate")
        s.step("BeforeSmoke")
        if kind == "release-manifest":
            s.rewrite_manifest(schema_version=2)
        else:
            path = s.release_dir / f"{kind}-receipt.json"
            key = "compatReceiptSha256" if kind == "compat" else "oldReaderReceiptSha256"
            s.bound[key] = write_json(path, {"schema_version": 2, "verdict": "pass"})
        assert s.stop("BeforeSmoke").code in ("SCHEMA_VERSION", "MANIFEST_STAGE1"), label

    s = Scenario(tmp_path / "ledger")
    s.to("BeforeSmoke")
    ledger = s.release_dir / "tag-ledger.json"
    value = json.loads(ledger.read_text(encoding="utf-8"))
    value["schema_version"] = 2
    write_json(ledger, value)
    s.step("AfterSmoke")
    s.run("AfterSmoke")
    s.step("BeforePromotion")
    s.run("BeforePromotion")
    s.step("AfterAgentPromotion")
    s.run("AfterAgentPromotion")
    s.step("AfterPromotion")
    assert s.stop("AfterPromotion").code == "SCHEMA_VERSION"


def test_hr05_an_image_tag_that_already_exists_in_the_registry_stops_before_any_write(tmp_path):
    s = Scenario(tmp_path)
    s.world.registry[s.tag()] = CAND_DIGEST
    stops_with(s, "BeforeAnyWrite", "TAG_MOVED")


def test_hr06_a_baseline_whose_api_literal_is_not_the_agent_status_url_is_never_captured(tmp_path):
    world = World()
    world.set_env("f42-api-00041-lns", "AGENT_URL", "https://f42-agent-590353929363.us-central1.run.app")
    with pytest.raises(so.Stop) as stopped:
        so.capture_baseline(FakeReader(world), "2026-10-08T10:00:00+00:00")
    assert stopped.value.code == "BASELINE"


def test_hr06_a_bound_baseline_edited_to_that_state_is_refused_at_the_first_phase(tmp_path):
    s = Scenario(tmp_path)
    baseline = json.loads(s.baseline_path.read_text(encoding="utf-8"))
    baseline["api_agent_url_literal"] = "https://f42-agent-590353929363.us-central1.run.app"
    s.bound["baselineSha256"] = write_json(s.baseline_path, baseline)
    error = s.stop("BeforeAnyWrite")
    assert error.code == "BASELINE" and "would not restore the canonical URL" in error.message


def test_hr07_a_change_between_review_and_execution_is_a_baseline_stop(tmp_path):
    s = Scenario(tmp_path)
    s.world.set_env(A80_REV["f42-api"], "F42_PROJECT", "another-project")
    stops_with(s, "BeforeAnyWrite", "BASELINE")
    s = Scenario(tmp_path / "second")
    s.world.svc["f42-agent"]["annotations"]["run.googleapis.com/ingress"] = "internal"
    assert s.stop("BeforeAnyWrite").code in ("BASELINE", "POLICY")
    s = Scenario(tmp_path / "third")
    s.world.svc["f42-api"]["traffic"] = [{"revisionName": OLDER_REV["f42-api"], "percent": 100}]
    stops_with(s, "BeforeAnyWrite", "TRAFFIC")


# Freeze and digests

def test_hr08_freeze_writes_the_manifest_once_when_the_build_and_the_registry_agree(tmp_path):
    s = Scenario(tmp_path)
    s.to("Freeze")
    manifest = s.manifest()
    assert manifest["image"]["digest"] == manifest["image"]["registry_digest"] == manifest["build"]["result_digest"] == CAND_DIGEST
    assert manifest["image"]["reference"] == f"{REPO}@{CAND_DIGEST}"
    assert manifest["manifest_sha256"] == so.manifest_hash(manifest)
    assert (s.release_dir / "release-manifest.sha256").read_text(encoding="utf-8").strip() == manifest["manifest_sha256"]
    assert manifest["candidates"]["f42-api"]["tag_url"] == TAG_URL["f42-api"] and manifest["schema_version"] == 1


def test_hr09_a_build_result_and_a_registry_that_disagree_stop_and_leave_no_manifest(tmp_path):
    s = Scenario(tmp_path)
    s.step("Freeze")
    s.world.registry[s.tag()] = "sha256:" + "bb" * 32
    stops_with(s, "Freeze", "DIGEST")
    assert not (s.release_dir / "release-manifest.json").exists()


@pytest.mark.parametrize("change", ["status", "image", "service_account", "created_before", "source", "no_image_result", "no_registry"])
def test_hr10_build_checks_name_the_field_that_differs(tmp_path, change):
    s = Scenario(tmp_path)
    s.step("Freeze")
    build = s.world.builds[BUILD_ID]
    if change == "status":
        build["status"] = "FAILURE"
    elif change == "image":
        build["substitutions"]["_IMAGE"] = f"{REPO}:other-01"
    elif change == "service_account":
        build["serviceAccount"] = "projects/ogilvy-trends-v2/serviceAccounts/someone@ogilvy-trends-v2.iam.gserviceaccount.com"
    elif change == "created_before":
        build["createTime"] = "2026-10-08T20:00:00+00:00"
    elif change == "source":
        build["source"]["storageSource"]["object"] = "build-source/other.tgz"
    elif change == "no_image_result":
        build["results"]["images"] = []
    elif change == "no_registry":
        s.world.registry.pop(s.tag())
    error = s.stop("Freeze")
    assert error.code in ("BUILD", "DIGEST"), (change, error.code)
    assert not (s.release_dir / "release-manifest.json").exists()


def test_hr11_a_second_freeze_refuses_and_the_first_manifest_is_unchanged(tmp_path):
    s = Scenario(tmp_path)
    s.to("Freeze")
    first = (s.release_dir / "release-manifest.json").read_bytes()
    stops_with(s, "Freeze", "MANIFEST_HASH", "already exists")
    assert (s.release_dir / "release-manifest.json").read_bytes() == first


def test_hr12_freeze_makes_both_the_build_lookup_and_the_registry_lookup(tmp_path):
    s = Scenario(tmp_path)
    s.to("Freeze")
    kinds = [call[0] for call in s.reader.calls]
    assert "build" in kinds and kinds.count("registry_digest") >= 2  # once at BeforeAnyWrite (absent) and once at Freeze
    assert ("source_file_sha256", COMMIT, "core/api/cloudbuild.yaml") in s.reader.calls


def test_hr13_a_manifest_field_changed_after_freeze_with_its_sidecar_rewritten_is_still_refused(tmp_path):
    s = Scenario(tmp_path)
    s.to("BeforeCandidate")
    candidates = s.manifest()["candidates"]
    candidates["f42-api"]["tag"] = "rel-d666ef6-99"
    s.rewrite_manifest(candidates=candidates)
    s.step("BeforeSmoke")
    stops_with(s, "BeforeSmoke", "MANIFEST_STAGE1")


def test_hr13_a_manifest_edited_without_its_hash_is_refused(tmp_path):
    s = Scenario(tmp_path)
    s.to("Freeze")
    path = s.release_dir / "release-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["image"]["digest"] = OTHER_DIGEST
    path.write_text(json.dumps(manifest), encoding="utf-8")
    stops_with(s, "BeforeCandidate", "MANIFEST_HASH")


# the image tag moves after the build

def test_hr14_a_registry_digest_that_changes_after_freeze_stops_before_the_candidate(tmp_path):
    s = Scenario(tmp_path)
    s.to("Freeze")
    s.world.registry[s.tag()] = OTHER_DIGEST
    s.step("BeforeCandidate")
    stops_with(s, "BeforeCandidate", "TAG_MOVED")


@pytest.mark.parametrize("phase", ["BeforeSmoke", "BeforePromotion"])
def test_hr15_a_registry_digest_that_changes_later_stops_at_each_candidate_phase(tmp_path, phase):
    s = Scenario(tmp_path)
    s.ready(phase)
    s.world.registry[s.tag()] = OTHER_DIGEST
    stops_with(s, phase, "TAG_MOVED")


def test_hr16_a_moved_tag_stops_even_when_the_revision_digest_still_equals_the_manifest(tmp_path):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    assert s.world.revisions[CAND_REV["f42-agent"]]["status"]["imageDigest"] == f"{REPO}@{CAND_DIGEST}"
    s.world.registry[s.tag()] = OTHER_DIGEST
    stops_with(s, "BeforeSmoke", "TAG_MOVED")


# candidate checks

def test_hr17_an_all_good_candidate_passes_and_the_observations_are_kept_apart(tmp_path):
    s = Scenario(tmp_path)
    result = s.to("BeforeSmoke")
    assert result["observations"]["health"]["api_tag"]["version"] == SHORT12
    assert result["observations"]["health"]["api_tag"]["version_matches"] is True


def test_hr18_a_deployed_revision_other_than_the_manifest_candidate_is_not_taken_for_it(tmp_path):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    world = s.world
    world.revisions["f42-agent-00048-abc"] = world.revisions.pop(CAND_REV["f42-agent"])
    world.revisions["f42-agent-00048-abc"]["metadata"]["name"] = "f42-agent-00048-abc"
    world.revision_names["f42-agent"] = [n if n != CAND_REV["f42-agent"] else "f42-agent-00048-abc" for n in world.revision_names["f42-agent"]]
    for entry in world.svc["f42-agent"]["traffic"]:
        if entry.get("tag"):
            entry["revisionName"] = "f42-agent-00048-abc"
    world.svc["f42-agent"].update(latest_created="f42-agent-00048-abc", latest_ready="f42-agent-00048-abc")
    assert s.stop("BeforeSmoke").code in ("TAG_MAPPING", "UNRELATED_REVISION", "CANDIDATE_REVISION")


@pytest.mark.parametrize("case", ["to_a80", "third", "swapped", "wrong_host", "extra_tag", "missing", "retained"])
def test_hr19_tag_mapping_cases(tmp_path, case):
    retained = {"f42-agent": {"keep-me": A80_REV["f42-agent"]}} if case == "retained" else None
    s = Scenario(tmp_path, retained=retained)
    s.ready("BeforeSmoke")
    world = s.world
    agent_tag = next(e for e in world.svc["f42-agent"]["traffic"] if e.get("tag") == RID)
    api_tag = next(e for e in world.svc["f42-api"]["traffic"] if e.get("tag") == RID)
    if case == "to_a80":
        agent_tag["revisionName"] = A80_REV["f42-agent"]
    elif case == "third":
        agent_tag["revisionName"] = OLDER_REV["f42-agent"]
    elif case == "swapped":
        agent_tag["revisionName"], api_tag["revisionName"] = api_tag["revisionName"], agent_tag["revisionName"]
    elif case == "wrong_host":
        agent_tag["tag"] = "rel-d666ef6-77"
    elif case == "extra_tag":
        world.svc["f42-api"]["traffic"].append({"revisionName": OLDER_REV["f42-api"], "percent": 0, "tag": "stray"})
    elif case == "missing":
        world.svc["f42-agent"]["traffic"] = [e for e in world.svc["f42-agent"]["traffic"] if not e.get("tag")]
    elif case == "retained":
        world.svc["f42-agent"]["traffic"].append({"revisionName": A80_REV["f42-agent"], "percent": 0, "tag": "keep-me"})
    if case == "retained":
        assert s.run("BeforeSmoke")["phase"] == "BeforeSmoke"
    else:
        assert s.stop("BeforeSmoke").code == "TAG_MAPPING", case


@pytest.mark.parametrize("case", ["agent", "api", "differ", "tag_form"])
def test_hr20_digest_mismatches_stop(tmp_path, case):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    w = s.world
    if case == "agent":
        w.revisions[CAND_REV["f42-agent"]]["status"]["imageDigest"] = f"{REPO}@{OTHER_DIGEST}"
    elif case == "api":
        w.revisions[CAND_REV["f42-api"]]["status"]["imageDigest"] = f"{REPO}@{OTHER_DIGEST}"
    elif case == "differ":
        for name in SERVICES:
            w.revisions[CAND_REV[name]]["status"]["imageDigest"] = f"{REPO}@{OTHER_DIGEST if name == 'f42-api' else CAND_DIGEST}"
    elif case == "tag_form":
        container(w, CAND_REV["f42-agent"])["image"] = f"{REPO}:{SHORT12}-01"
    assert s.stop("BeforeSmoke").code == "DIGEST", case


def test_hr21_a_newer_ready_revision_is_unrelated_and_is_never_judged_as_the_candidate(tmp_path):
    for name in SERVICES:
        s = Scenario(tmp_path / name)
        s.ready("BeforeSmoke")
        w = s.world
        newer = f"{name}-00042-zzz"
        w.revisions[newer] = copy.deepcopy(w.revisions[CAND_REV[name]])
        w.revisions[newer]["metadata"]["name"] = newer
        w.revision_names[name].append(newer)
        w.svc[name].update(latest_created=newer, latest_ready=newer)
        stops_with(s, "BeforeSmoke", "UNRELATED_REVISION")
        described = [call[1] for call in s.reader.calls if call[0] == "revision"]
        assert newer not in described


@pytest.mark.parametrize("case", ["latest_following", "candidate_5", "two_nonzero", "serving_names_candidate"])
def test_hr22_traffic_at_the_candidate_phases_is_exactly_the_a80_revision(tmp_path, case):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    traffic = s.world.svc["f42-api"]["traffic"]
    if case == "latest_following":
        s.world.svc["f42-api"]["traffic"] = [{"latestRevision": True, "percent": 100}, *[e for e in traffic if e.get("tag")]]
    elif case == "candidate_5":
        traffic[0]["percent"], traffic[1]["percent"] = 95, 5
    elif case == "two_nonzero":
        traffic.append({"revisionName": OLDER_REV["f42-api"], "percent": 10})
    elif case == "serving_names_candidate":
        traffic[:] = [{"revisionName": CAND_REV["f42-api"], "percent": 100, "tag": RID}]
    assert s.stop("BeforeSmoke").code in ("TRAFFIC", "TAG_MAPPING"), case


def candidate_env_cases():
    return {
        "extra": lambda w, r, n: w.set_env(r, "EXTRA_SETTING", "x"),
        "missing": lambda w, r, n: w.drop_env(r, "F42_DATA"),
        "secret_ref": lambda w, r, n: w.set_env(r, "UI_PASSCODE" if n == "f42-api" else "SOCIALCRAWL_OGILVY_API_KEY",
                                                {"valueFrom": {"secretKeyRef": {"name": "OTHER", "key": "latest"}}}),
        "project": lambda w, r, n: w.set_env(r, "F42_PROJECT", "elsewhere"),
        "model": lambda w, r, n: w.set_env(r, "GEMINI_MODEL" if n == "f42-agent" else "F42_DATA", "other"),
        "version": lambda w, r, n: w.set_env(r, "F42_VERSION", "000000000000"),
    }


@pytest.mark.parametrize("name", SERVICES)
@pytest.mark.parametrize("case", sorted(candidate_env_cases()))
def test_hr23_a_changed_candidate_environment_stops(tmp_path, name, case):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    candidate_env_cases()[case](s.world, CAND_REV[name], name)
    stops_with(s, "BeforeSmoke", "ENV")


@pytest.mark.parametrize("case", ["canonical_agent_url", "tag_url_as_audience", "missing_audience"])
def test_hr23_the_api_agent_link_values_are_pinned(tmp_path, case):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    rev = CAND_REV["f42-api"]
    if case == "canonical_agent_url":
        s.world.set_env(rev, "AGENT_URL", CANON["f42-agent"])
    elif case == "tag_url_as_audience":
        s.world.set_env(rev, "AGENT_AUDIENCE", TAG_URL["f42-agent"])
    else:
        s.world.drop_env(rev, "AGENT_AUDIENCE")
    stops_with(s, "BeforeSmoke", "ENV")


@pytest.mark.parametrize("name", SERVICES)
@pytest.mark.parametrize("field", ["service_account", "memory", "cpu", "timeout_seconds", "minScale", "maxScale", "cpu-throttling",
                                   "ingress", "invoker-iam-disabled"])
def test_hr24_a_changed_candidate_resource_names_its_field(tmp_path, name, field):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    w, rev = s.world, CAND_REV[name]
    r = w.revisions[rev]
    if field == "service_account":
        r["spec"]["serviceAccountName"] = "other@ogilvy-trends-v2.iam.gserviceaccount.com"
    elif field == "memory":
        r["spec"]["containers"][0]["resources"]["limits"]["memory"] = "4Gi"
    elif field == "cpu":
        r["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "4000m"
    elif field == "timeout_seconds":
        r["spec"]["timeoutSeconds"] = 60
    elif field in ("minScale", "maxScale"):
        r["metadata"]["annotations"][f"autoscaling.knative.dev/{field}"] = "9"
    elif field == "cpu-throttling":
        r["metadata"]["annotations"]["run.googleapis.com/cpu-throttling"] = "true"
    else:
        w.svc[name]["annotations"][f"run.googleapis.com/{field}"] = "changed"
    error = s.stop("BeforeSmoke")
    assert error.code in ("CANDIDATE_REVISION", "POLICY") and field in error.message, (error.code, error.message)


@pytest.mark.parametrize("case", ["public_policy", "agent_invoker_disabled", "policy_hash"])
def test_hr25_a_policy_change_stops_the_candidate_phases(tmp_path, case):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    if case == "public_policy":
        s.world.policy["bindings"].append({"role": "roles/run.invoker", "members": ["allUsers"]})
    elif case == "agent_invoker_disabled":
        s.world.svc["f42-agent"]["annotations"]["run.googleapis.com/invoker-iam-disabled"] = "true"
    else:
        s.world.policy["bindings"][0]["members"].append("serviceAccount:someone@example.invalid")
    assert s.stop("BeforeSmoke").code in ("POLICY", "CANDIDATE_REVISION")


@pytest.mark.parametrize("url_key, status", [("tag", 200), ("tag", 401), ("canonical", 200)])
def test_hr26_the_agent_must_refuse_an_anonymous_caller_at_both_urls(tmp_path, url_key, status):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    s.world.anonymous[TAG_URL["f42-agent"] if url_key == "tag" else CANON["f42-agent"]] = status
    stops_with(s, "BeforeSmoke", "AGENT_PUBLIC")


def test_hr26_both_agent_urls_are_checked_at_the_candidate_phases(tmp_path):
    s = Scenario(tmp_path)
    s.to("BeforeSmoke")
    asked = [call[1] for call in s.reader.calls if call[0] == "anonymous_status"]
    assert CANON["f42-agent"] in asked and TAG_URL["f42-agent"] in asked


def test_hr27_the_tag_url_health_cases(tmp_path):
    sleeps = []
    s = Scenario(tmp_path / "ok")
    s.ready("BeforeSmoke")
    assert s.run("BeforeSmoke")["observations"]["health"]["api_tag"]["attempts"] == [{"http": 200, "ok": True, "agent": "ok"}]

    s = Scenario(tmp_path / "once")
    s.ready("BeforeSmoke")
    s.world.health[TAG_URL["f42-api"]] = [(200, {"ok": True, "checks": {"agent": "unreachable"}}), HEALTHY]
    attempts = s.run("BeforeSmoke")["observations"]["health"]["api_tag"]["attempts"]
    assert [a["agent"] for a in attempts] == ["unreachable", "ok"] and s.sleeps == [10]

    s = Scenario(tmp_path / "thrice")
    s.ready("BeforeSmoke")
    s.world.health[TAG_URL["f42-api"]] = [(200, {"ok": True, "checks": {"agent": "unreachable"}})]
    stops_with(s, "BeforeSmoke", "API_AGENT")
    assert s.sleeps == [10, 10, 10]

    for label, health in (("401", (401, {})), ("not_ok", (200, {"ok": False, "checks": {"agent": "ok"}})),
                          ("agent_error", (200, {"ok": True, "checks": {"agent": "error"}}))):
        s = Scenario(tmp_path / label)
        s.ready("BeforeSmoke")
        s.world.health[TAG_URL["f42-api"]] = [health]
        stops_with(s, "BeforeSmoke", "API_AGENT")
        assert s.sleeps == []

    s = Scenario(tmp_path / "version")
    s.ready("BeforeSmoke")
    s.world.health[TAG_URL["f42-api"]] = [(200, {"ok": True, "checks": {"agent": "ok"}, "version": "000000000000"})]
    result = s.run("BeforeSmoke")
    assert result["observations"]["health"]["api_tag"]["version_matches"] is False


@pytest.mark.parametrize("phase", ["BeforeCandidate", "BeforeSmoke", "BeforePromotion"])
@pytest.mark.parametrize("kind", ["compat", "old-reader"])
@pytest.mark.parametrize("fault", ["missing", "hash", "verdict"])
def test_hr28_the_compat_and_old_reader_receipts_gate_every_phase_from_before_candidate(tmp_path, phase, kind, fault):
    s = Scenario(tmp_path)
    s.ready(phase)
    path = s.release_dir / f"{kind}-receipt.json"
    if fault == "missing":
        path.unlink()
    elif fault == "hash":
        path.write_text(json.dumps({"schema_version": 1, "verdict": "pass", "edited": True}), encoding="utf-8")
    else:
        key = "compatReceiptSha256" if kind == "compat" else "oldReaderReceiptSha256"
        s.bound[key] = write_json(path, {"schema_version": 1, "verdict": "fail"})
        s.rewrite_manifest(**{("compat" if kind == "compat" else "old_readers"): {"receipt_sha256": s.bound[key]}})
    assert s.stop(phase).code in ("COMPAT", "OLD_READER", "MANIFEST_STAGE1")


@pytest.mark.parametrize("fault", ["missing", "public_url", "manifest_hash", "names", "exit_code", "passed_lt_total", "total",
                                   "contradicting_log", "after_smoke_never_ran"])
def test_hr29_the_smoke_receipt_is_checked_before_promotion(tmp_path, fault):
    s = Scenario(tmp_path)
    s.ready("BeforePromotion")
    path = s.release_dir / "smoke-receipt.json"
    if fault == "missing":
        path.unlink()
    elif fault == "public_url":
        s.write_smoke_receipt(argv_url=CANON["f42-api"])
    elif fault == "manifest_hash":
        s.write_smoke_receipt(manifest_sha256="0" * 64)
    elif fault == "names":
        s.write_smoke_receipt(candidates={"f42-agent": "f42-agent-00048-abc", "f42-api": CAND_REV["f42-api"]})
    elif fault == "exit_code":
        s.write_smoke_receipt(exit_code=1)
    elif fault == "passed_lt_total":
        s.write_smoke_receipt(checks_passed=5)
    elif fault == "total":
        s.write_smoke_receipt(checks_total=5, checks_passed=5)
    elif fault == "contradicting_log":
        s.write_smoke_receipt(checks_total=6, checks_passed=5, log_sha256="6 of 6 checks passed")
    elif fault == "after_smoke_never_ran":
        for f in (s.release_dir / "readbacks").glob("AfterSmoke-*.json"):
            f.unlink()
    assert s.stop("BeforePromotion").code in ("SMOKE", "RECEIPT"), fault


def test_hr29_before_smoke_requires_that_no_smoke_receipt_exists(tmp_path):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    s.write_smoke_receipt()
    stops_with(s, "BeforeSmoke", "SMOKE")


def test_hr30_a_candidate_resource_that_changed_since_after_smoke_is_state_drift(tmp_path):
    s = Scenario(tmp_path)
    s.ready("BeforePromotion")
    s.world.revisions[CAND_REV["f42-api"]]["metadata"]["annotations"]["note"] = "added after the smoke"
    stops_with(s, "BeforePromotion", "STATE_DRIFT")


def test_hr31_an_after_smoke_older_than_the_bound_maximum_is_stale(tmp_path):
    s = Scenario(tmp_path)
    s.ready("BeforePromotion")
    s.now = s.now.replace(hour=14, minute=0)  # the smoke ended 07:30 UTC; 6 hours 30 minutes later
    stops_with(s, "BeforePromotion", "STALE")
    s.now = s.now.replace(hour=13, minute=30)  # exactly the ceiling passes
    assert s.run("BeforePromotion")["phase"] == "BeforePromotion"


@pytest.mark.parametrize("delta", ["serving_env", "template_service_account", "template_annotation", "serving_resources", "template_image"])
def test_hr32_a_delta_outside_the_allowed_list_is_a_preservation_stop(tmp_path, delta):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    w = s.world
    if delta == "serving_env":
        w.set_env(A80_REV["f42-api"], "F42_DATA", "changed")
    elif delta == "template_service_account":
        w.svc["f42-agent"]["sa"] = "other@ogilvy-trends-v2.iam.gserviceaccount.com"
    elif delta == "template_annotation":
        w.svc["f42-agent"]["tmpl_ann"]["unlisted"] = "x"
    elif delta == "serving_resources":
        w.revisions[A80_REV["f42-agent"]]["spec"]["containers"][0]["resources"]["limits"]["memory"] = "9Gi"
    elif delta == "template_image":
        w.svc["f42-api"]["image"] = f"{REPO}:somethingelse"
    stops_with(s, "BeforeSmoke", "PRESERVATION")


# jobs unchanged

JOB_CHANGES = {
    "image": lambda c, t: c.update(image=f"{REPO.replace('f42-web', 'jobs')}@sha256:{'ab' * 32}"),
    "env_value": lambda c, t: c["env"][0].update(value="changed"),
    "env_new": lambda c, t: c["env"].append({"name": "NEW_SETTING", "value": "1"}),
    "memory": lambda c, t: c["resources"]["limits"].update(memory="8Gi"),
    "cpu": lambda c, t: c["resources"]["limits"].update(cpu="4000m"),
    "timeout": lambda c, t: t.update(timeoutSeconds=60),
    "retries": lambda c, t: t.update(maxRetries=3),
    "service_account": lambda c, t: t.update(serviceAccountName="other@ogilvy-trends-v2.iam.gserviceaccount.com"),
    "secret": lambda c, t: c["env"][1].update(valueFrom={"secretKeyRef": {"name": "OTHER", "key": "latest"}}),
}


def mutate_job(world, name, kind):
    spec = world.jobs[name]["spec"]["template"]
    task = spec["spec"]["template"]["spec"]
    if kind == "parallelism":
        spec["spec"]["parallelism"] = 4
    elif kind == "task_count":
        spec["spec"]["taskCount"] = 4
    elif kind == "annotation":
        spec["spec"]["template"]["metadata"]["annotations"]["run.googleapis.com/note"] = "x"
    else:
        JOB_CHANGES[kind](task["containers"][0], task)


@pytest.mark.parametrize("name", so.JOB_NAMES)
@pytest.mark.parametrize("kind", [*JOB_CHANGES, "parallelism", "task_count", "annotation"])
def test_hr33_a_change_to_any_of_the_fourteen_jobs_stops_the_forward_phases(tmp_path, name, kind):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    mutate_job(s.world, name, kind)
    error = stops_with(s, "BeforeSmoke", "JOB_CHANGED")
    assert name in error.message


@pytest.mark.parametrize("phase", ["BeforeAnyWrite", "BeforeCandidate", "BeforeSmoke", "AfterSmoke", "BeforePromotion",
                                   "AfterAgentPromotion", "AfterPromotion"])
def test_hr33_the_job_check_runs_in_every_forward_phase(tmp_path, phase):
    s = Scenario(tmp_path)
    s.ready(phase)
    mutate_job(s.world, "f42-detect", "env_value")
    stops_with(s, phase, "JOB_CHANGED")


def test_hr34_a_job_on_the_candidate_services_image_stops(tmp_path):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    container(World(), A80_REV["f42-api"])  # shape check only
    s.world.jobs["f42-brief"]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["image"] = f"{REPO}@{CAND_DIGEST}"
    stops_with(s, "BeforeSmoke", "JOB_CHANGED")


# promotion

def test_hr35_agent_promotion_passes_with_the_old_api_still_serving_and_the_public_health_ok(tmp_path):
    s = Scenario(tmp_path)
    result = s.to("AfterAgentPromotion")
    assert result["observations"]["health"]["public"]["attempts"][0]["agent"] == "ok"
    assert [e["revision"] for e in so.service_view(s.world.service_raw("f42-api"))["traffic"]["status"] if e["percent"] > 0] == [A80_REV["f42-api"]]


def test_hr36_an_agent_candidate_at_90_percent_stops(tmp_path):
    s = Scenario(tmp_path)
    s.ready("AfterAgentPromotion")
    s.world.svc["f42-agent"]["traffic"] = [{"revisionName": A80_REV["f42-agent"], "percent": 10},
                                           {"revisionName": CAND_REV["f42-agent"], "percent": 90, "tag": RID}]
    stops_with(s, "AfterAgentPromotion", "TRAFFIC")


def test_hr37_a_tag_lost_after_the_traffic_command_stops(tmp_path):
    s = Scenario(tmp_path)
    s.ready("AfterAgentPromotion")
    s.world.svc["f42-agent"]["traffic"] = [{"revisionName": CAND_REV["f42-agent"], "percent": 100}]
    stops_with(s, "AfterAgentPromotion", "TAG_MAPPING")


def test_hr38_both_promoted_passes_and_the_ledger_records_the_serving_state(tmp_path):
    s = Scenario(tmp_path)
    result = s.to("AfterPromotion")
    assert result["observations"]["health"]["public"]["version_matches"] is True
    states = [e["state"] for e in json.loads((s.release_dir / "tag-ledger.json").read_text(encoding="utf-8"))["events"]]
    assert states == ["CANDIDATE", "CANDIDATE", "SERVING", "SERVING"]


def test_hr39_a_serving_api_that_names_a_tag_not_on_the_serving_agent_revision_stops(tmp_path):
    s = Scenario(tmp_path)
    s.ready("AfterPromotion")
    # the agent tag now sits on a different revision than the one serving canonical agent traffic
    for entry in s.world.svc["f42-agent"]["traffic"]:
        if entry.get("tag"):
            entry["tag"] = None
    s.world.svc["f42-agent"]["traffic"] = [{"revisionName": CAND_REV["f42-agent"], "percent": 100},
                                           {"revisionName": A80_REV["f42-agent"], "percent": 0, "tag": RID}]
    stops_with(s, "AfterPromotion", "API_AGENT")


def test_hr40_an_api_promoted_while_the_agent_candidate_does_not_serve_canonical_traffic_stops(tmp_path):
    s = Scenario(tmp_path)
    s.to("BeforePromotion")
    s.world.promote("f42-api")
    assert s.stop("AfterPromotion").code in ("TRAFFIC", "API_AGENT")


def test_hr41_a_tag_reference_outside_the_declared_set_stops_and_the_api_tag_host_is_not_a_reference(tmp_path):
    s = Scenario(tmp_path)
    s.ready("AfterPromotion")
    s.world.set_env(CAND_REV["f42-api"], "AGENT_URL", TAG_URL["f42-api"])  # the API's own tag host, same tag value
    assert s.stop("AfterPromotion").code in ("ENV", "TAG_REFERENCE", "API_AGENT")
    s = Scenario(tmp_path / "other")
    s.to("AfterPromotion")
    s.world.set_env(CAND_REV["f42-api"], "AGENT_URL", "https://rel-aaaaaaa-01---f42-agent-fibxg5ynpq-uc.a.run.app")
    assert s.stop("AfterPromotion").code in ("API_AGENT", "TAG_REFERENCE", "ENV")


def test_hr41_retention_matches_the_full_literal_and_holds_a_tag_a_serving_pair_still_uses(tmp_path):
    s = Scenario(tmp_path)
    s.to("AfterPromotion")
    error = s.stop("BeforeRetire", tag=RID)
    assert error.code == "TAG_REFERENCE"
    assert "agent_revision_serves_canonical_traffic" in error.message
    assert "api_revision_with_traffic_names_the_agent_tag_url" in error.message


# rollback and retirement

def states_world(tmp_path, agent, api, *, api_candidate=True):
    s = Scenario(tmp_path)
    if api_candidate:
        s.to("AfterSmoke")
        s.step("BeforePromotion")
        s.run("BeforePromotion")
    else:
        s.ready("BeforeSmoke")
        s.world.revisions.pop(CAND_REV["f42-api"])
        s.world.revision_names["f42-api"].remove(CAND_REV["f42-api"])
        s.world.svc["f42-api"]["traffic"] = [{"revisionName": A80_REV["f42-api"], "percent": 100}]
        s.world.svc["f42-api"].update(latest_created=A80_REV["f42-api"], latest_ready=A80_REV["f42-api"])
    if agent == "candidate":
        s.world.promote("f42-agent")
    if api == "candidate":
        s.world.promote("f42-api")
    return s


@pytest.mark.parametrize("agent, api, api_candidate, label", [
    ("a80", "a80", True, "agent a80, api a80"),
    ("candidate", "a80", True, "agent candidate, api a80"),
    ("candidate", "candidate", True, "agent candidate, api candidate"),
    ("a80", "candidate", True, "agent a80, api candidate"),
    ("a80", "a80", False, "agent a80, api a80"),
])
def test_hr42_before_rollback_accepts_each_allowed_state_and_reports_which(tmp_path, agent, api, api_candidate, label):
    s = states_world(tmp_path, agent, api, api_candidate=api_candidate)
    result = s.run("BeforeRollback")
    assert result["state"] == label
    assert result["candidates_present"]["f42-api"] is api_candidate


def test_hr42_the_agent_candidate_deployed_and_the_api_absent_is_a_state_the_readback_accepts(tmp_path):
    s = Scenario(tmp_path)
    s.to("BeforeCandidate")
    s.world.deploy_candidate("f42-agent")  # the API deploy then failed
    result = s.run("BeforeRollback")
    assert result["state"] == "agent a80, api a80"
    assert result["tags"] == {"f42-agent": True, "f42-api": False}
    assert result["candidates_present"] == {"f42-agent": True, "f42-api": False}


def test_hr43_an_unknown_revision_serving_or_a_target_with_another_digest_or_unverifiable_principals_stop(tmp_path):
    s = Scenario(tmp_path / "unknown")
    s.to("BeforeCandidate")
    s.world.svc["f42-agent"]["traffic"] = [{"revisionName": OLDER_REV["f42-agent"], "percent": 100}]
    assert s.stop("BeforeRollback").code == "TRAFFIC"
    s = Scenario(tmp_path / "digest")
    s.to("BeforeCandidate")
    s.world.revisions[A80_REV["f42-api"]]["status"]["imageDigest"] = f"{REPO}@{OTHER_DIGEST}"
    assert s.stop("BeforeRollback").code == "DIGEST"
    s = Scenario(tmp_path / "principals")
    s.to("BeforeCandidate")
    s.world.principals_ok = False
    assert s.stop("BeforeRollback").code == "IDENTITY"


def test_hr44_job_policy_and_window_drift_is_recorded_without_blocking_a_rollback(tmp_path):
    s = Scenario(tmp_path)
    s.to("AfterSmoke")
    mutate_job(s.world, "f42-collect", "env_value")
    s.world.policy["bindings"][0]["members"].append("serviceAccount:other@example.invalid")
    result = s.run("BeforeRollback")
    assert result["blocking"]["jobs"] == {"blocking": False, "changed": ["f42-collect"]}
    assert result["blocking"]["policy"]["blocking"] is False


def rolled_back(tmp_path, *, keep_tags=True):
    s = Scenario(tmp_path)
    s.to("AfterPromotion")
    for name in reversed(SERVICES):
        s.world.restore(name, keep_tag=keep_tags)
    return s


def test_hr45_the_a80_rollback_readback_passes_and_records_the_residue(tmp_path):
    s = rolled_back(tmp_path)
    result = s.run("AfterRollback")
    assert result["residue"]["f42-agent"] == {"candidate_revision_present": True, "tags": [RID], "template_matches_baseline": False}
    assert all(r["candidate_revision_present"] for r in result["residue"].values())


@pytest.mark.parametrize("case", ["older_api", "older_agent", "tag_url_literal", "deterministic_form", "trailing_slash",
                                  "audience_present", "one_not_rolled_back", "latest_following", "candidate_traffic", "jobs_changed"])
def test_hr46_a_rollback_that_is_not_the_a80_pair_stops_naming_the_field(tmp_path, case):
    s = rolled_back(tmp_path)
    w = s.world
    if case == "older_api":
        w.svc["f42-api"]["traffic"] = [{"revisionName": OLDER_REV["f42-api"], "percent": 100}]
    elif case == "older_agent":
        w.svc["f42-agent"]["traffic"] = [{"revisionName": OLDER_REV["f42-agent"], "percent": 100}]
    elif case == "tag_url_literal":
        w.set_env(A80_REV["f42-api"], "AGENT_URL", TAG_URL["f42-agent"])
    elif case == "deterministic_form":
        w.set_env(A80_REV["f42-api"], "AGENT_URL", "https://f42-agent-590353929363.us-central1.run.app")
    elif case == "trailing_slash":
        w.set_env(A80_REV["f42-api"], "AGENT_URL", CANON["f42-agent"] + "/")
    elif case == "audience_present":
        w.set_env(A80_REV["f42-api"], "AGENT_AUDIENCE", CANON["f42-agent"])
    elif case == "one_not_rolled_back":
        w.promote("f42-agent")
    elif case == "latest_following":
        w.svc["f42-api"]["traffic"] = [{"latestRevision": True, "percent": 100}]
    elif case == "candidate_traffic":
        w.svc["f42-api"]["traffic"] = [{"revisionName": A80_REV["f42-api"], "percent": 90}, {"revisionName": CAND_REV["f42-api"], "percent": 10, "tag": RID}]
    elif case == "jobs_changed":
        mutate_job(w, "f42-learn", "memory")
    error = s.stop("AfterRollback")
    assert error.code in ("TRAFFIC", "ENV", "PRESERVATION", "JOB_CHANGED"), (case, error.code)


def test_hr47_residual_candidate_tags_are_recorded_and_a_candidate_with_traffic_stops(tmp_path):
    s = rolled_back(tmp_path)
    assert s.run("AfterRollback")["residue"]["f42-api"]["tags"] == [RID]
    s = Scenario(tmp_path / "traffic")
    s.to("AfterPromotion")
    s.world.restore("f42-agent")
    assert s.stop("AfterRollback").code == "TRAFFIC"


def test_hr_rollback_health_retries_a_cold_a80_agent_up_to_three_times(tmp_path):
    s = rolled_back(tmp_path)
    s.world.health[CANON["f42-api"]] = [(200, {"ok": True, "checks": {"agent": "unreachable"}}), (200, {"ok": True, "checks": {"agent": "unreachable"}}), HEALTHY]
    s.run("AfterRollback")
    assert s.sleeps == [10, 10]


def test_hr_retirement_is_refused_while_the_tag_is_referenced_and_passes_after_a_rollback(tmp_path):
    s = rolled_back(tmp_path)
    s.run("AfterRollback")
    assert s.run("BeforeRetire", tag=RID)["tags"] == {"f42-agent": True, "f42-api": True}
    assert "still holds the release tag" in s.stop("AfterRetire", tag=RID).message
    for name in SERVICES:
        s.world.remove_tag(name)
    assert s.run("AfterRetire", tag=RID)["tags"] == {"f42-agent": False, "f42-api": False}
    states = [e["state"] for e in json.loads((s.release_dir / "tag-ledger.json").read_text(encoding="utf-8"))["events"]]
    assert states[-2:] == ["RETIRED", "RETIRED"]


def test_hr_the_retire_phases_take_the_release_id_as_their_tag_and_other_phases_take_none(tmp_path):
    s = Scenario(tmp_path)
    assert s.stop("BeforeRetire").code == "TAG_MAPPING"
    assert s.stop("BeforeRetire", tag="rel-0000000-01").code == "TAG_MAPPING"
    with pytest.raises(so.Stop):
        s.run("BeforeAnyWrite", tag=RID)


def test_hr_the_ledger_is_append_only(tmp_path):
    s = Scenario(tmp_path)
    s.to("BeforeSmoke")
    first = json.loads((s.release_dir / "tag-ledger.json").read_text(encoding="utf-8"))["events"]
    s.step("AfterSmoke")
    s.run("AfterSmoke")
    s.step("BeforePromotion")
    s.run("BeforePromotion")
    s.step("AfterAgentPromotion")
    s.run("AfterAgentPromotion")
    s.step("AfterPromotion")
    s.run("AfterPromotion")
    later = json.loads((s.release_dir / "tag-ledger.json").read_text(encoding="utf-8"))["events"]
    assert later[:len(first)] == first and len(later) == len(first) + 2


# timeouts, exit codes, hygiene

def bindings_file(tmp_path, scenario, **over):
    path = tmp_path / "bindings.json"
    write_json(path, {**scenario.bound, **over})
    return path


def run_main(tmp_path, scenario, reader, *, phase="BeforeAnyWrite", **over):
    path = bindings_file(tmp_path, scenario, **over)
    return helper.main(["--phase", phase, "--bindings", str(path), "--evidence", str(scenario.evidence)], reader_factory=lambda t: reader)


class RaisingReader(FakeReader):
    def __init__(self, world, error):
        super().__init__(world)
        self.error = error

    def config(self):
        raise self.error


def test_hr49_a_read_that_cannot_complete_exits_3_and_a_failed_check_exits_1(tmp_path, capsys):
    s = Scenario(tmp_path)
    assert run_main(tmp_path, s, RaisingReader(s.world, so.Probe("A read exceeded its bound timeout"))) == 3
    assert "PROBE" in capsys.readouterr().err
    s.world.config["core"]["account"] = "someone.else@example.invalid"
    assert run_main(tmp_path, s, FakeReader(s.world)) == 1
    assert "STOP: IDENTITY" in capsys.readouterr().err


def test_hr49_timeouts_expired_unreadable_output_and_a_nonzero_exit_all_map_to_probe(tmp_path, monkeypatch):
    reader = helper.GcloudReader({"gcloud": 5, "http": 5})

    def expired(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(helper.subprocess, "run", expired)
    with pytest.raises(so.Probe):
        reader.service("f42-agent")

    def garbage(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="not json", stderr="")

    monkeypatch.setattr(helper.subprocess, "run", garbage)
    with pytest.raises(so.Probe):
        reader.service("f42-agent")

    def failing(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stdout="", stderr="boom")

    monkeypatch.setattr(helper.subprocess, "run", failing)
    with pytest.raises(so.Probe):
        reader.service("f42-agent")

    def missing(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stdout="", stderr="ERROR: NOT_FOUND")

    monkeypatch.setattr(helper.subprocess, "run", missing)
    with pytest.raises(so.NotFound):
        reader.revision("f42-agent-gone")


def test_hr49_a_stalled_read_returns_within_the_bound_timeout_as_probe(monkeypatch):
    monkeypatch.setattr(helper, "gcloud_command", lambda: [sys.executable, "-c", "import time; time.sleep(60)"])
    reader = helper.GcloudReader({"gcloud": 0.5, "http": 0.5})
    started = time.monotonic()
    with pytest.raises(so.Probe):
        reader.service("f42-agent")
    assert time.monotonic() - started < 10


def test_hr49_every_subprocess_and_http_call_in_the_helper_carries_the_bound_timeout():
    tree = ast.parse(Path(helper.__file__).read_text(encoding="utf-8"))
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = ast.unparse(node.func)
        if target in ("subprocess.run", "session.get") or target.endswith(".open") and "build_opener" in target:
            keyword = next((k for k in node.keywords if k.arg == "timeout"), None)
            assert keyword is not None, f"{target} at line {node.lineno} has no timeout"
            assert not isinstance(keyword.value, ast.Constant), f"{target} at line {node.lineno} has a literal timeout"
            checked += 1
    assert checked >= 5


@pytest.mark.parametrize("over", [{"readTimeoutSeconds": None}, {"readTimeoutSeconds": {"gcloud": 120}},
                                  {"readTimeoutSeconds": {"gcloud": 0, "http": 30}}, {"readTimeoutSeconds": {"gcloud": 120, "http": -1}},
                                  {"readTimeoutSeconds": {"gcloud": "120", "http": 30}}])
def test_hr49_bindings_without_a_positive_read_timeout_stop(tmp_path, capsys, over):
    s = Scenario(tmp_path)
    bound = {k: v for k, v in s.bound.items() if k != "readTimeoutSeconds"}
    if over["readTimeoutSeconds"] is not None:
        bound.update(over)
    path = tmp_path / "b.json"
    write_json(path, bound)
    assert helper.main(["--phase", "BeforeAnyWrite", "--bindings", str(path), "--evidence", str(s.evidence)],
                       reader_factory=lambda t: s.reader) == 1
    assert "STOP: BINDINGS" in capsys.readouterr().err


def test_hr50_the_helper_issues_read_commands_only(monkeypatch):
    reader = helper.GcloudReader({"gcloud": 5, "http": 5})
    monkeypatch.setattr(helper.subprocess, "run", lambda *a, **k: pytest.fail("a refused command was run"))
    for args in (["run", "deploy", "f42-agent"], ["run", "services", "update-traffic", "f42-api"], ["run", "jobs", "update", "f42-collect"],
                 ["run", "services", "add-iam-policy-binding", "f42-agent"], ["builds", "submit"], ["run", "services", "delete", "x"]):
        with pytest.raises(so.Stop) as stopped:
            reader._gcloud(args)
        assert stopped.value.code == "WRITE_REFUSED"
    assert all(len(prefix) >= 2 for prefix in helper.READ_PREFIXES)
    assert not any(word in " ".join(p) for p in helper.READ_PREFIXES for word in ("deploy", "update", "delete", "create", "submit"))


def test_hr50_a_full_walk_issues_no_call_the_fake_reader_cannot_answer(tmp_path):
    s = Scenario(tmp_path)
    s.to("AfterPromotion")
    kinds = {call[0] for call in s.reader.calls}
    assert kinds <= {"config", "service", "revision", "revisions", "job", "policy", "build", "registry_digest",
                     "source_file_sha256", "anonymous_status", "health", "principals"}


def test_hr51_no_secret_value_or_token_reaches_stdout_stderr_or_any_file(tmp_path, capsys):
    sentinel = "SENTINEL-SECRET-VALUE-1f9c"
    world = World()
    world.revisions[A80_REV["f42-api"]]["spec"]["containers"][0]["env"].append({"name": "LEAK", "value": sentinel})
    world.svc["f42-api"]["env"]["LEAK"] = sentinel
    world.jobs["f42-probe"]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "TOKEN", "value": sentinel})
    s = Scenario(tmp_path, world=world)
    path = bindings_file(tmp_path, s)
    for phase in ("BeforeAnyWrite",):
        helper.main(["--phase", phase, "--bindings", str(path), "--evidence", str(s.evidence)], reader_factory=lambda t: s.reader)
    out = capsys.readouterr()
    assert sentinel not in out.out and sentinel not in out.err
    assert (s.release_dir / "readbacks" / "BeforeAnyWrite-01.json").exists()
    for file in tmp_path.rglob("*"):
        if file.is_file():
            assert sentinel.encode() not in file.read_bytes(), file


def test_hr52_the_preserved_state_is_written_once_and_a_different_value_stops(tmp_path):
    s = Scenario(tmp_path)
    s.run("BeforeAnyWrite")
    path = s.release_dir / "preservation-state.json"
    written = path.read_bytes()
    s.run("BeforeAnyWrite")
    assert path.read_bytes() == written
    state = json.loads(written)
    state["serving"]["f42-api"] = "0" * 64
    path.write_text(json.dumps(state), encoding="utf-8")
    stops_with(s, "BeforeAnyWrite", "PRESERVATION")


def test_hr53_every_readback_is_a_versioned_file_one_per_attempt(tmp_path):
    s = Scenario(tmp_path)
    s.run("BeforeAnyWrite")
    s.run("BeforeAnyWrite")
    files = sorted((s.release_dir / "readbacks").glob("BeforeAnyWrite-*.json"))
    assert [f.name for f in files] == ["BeforeAnyWrite-01.json", "BeforeAnyWrite-02.json"]
    value = json.loads(files[-1].read_text(encoding="utf-8"))
    assert value["schema_version"] == 1 and value["mode"] == "services-only" and value["release_id"] == RID
    assert {"manifest_sha256", "observations", "blocking", "at_utc", "phase"} <= set(value)


def test_hr54_a_durable_path_outside_the_release_directory_is_refused(tmp_path):
    for key in ("manifestPath", "smokeReceiptPath", "ledgerPath", "preservationPath", "compatReceiptPath"):
        s = Scenario(tmp_path / key)
        s.bound[key] = str(tmp_path / "elsewhere" / "file.json")
        assert s.stop("BeforeAnyWrite").code == "BINDINGS", key
    s = Scenario(tmp_path / "evidence")
    s.bound["manifestPath"] = str(s.evidence / "release-manifest.json")
    assert s.stop("BeforeAnyWrite").code == "BINDINGS"
    s = Scenario(tmp_path / "dotdot")
    s.bound["ledgerPath"] = str(s.release_dir / ".." / ".." / "tag-ledger.json")
    assert s.stop("BeforeAnyWrite").code == "BINDINGS"


# retry on the same commit

def second_attempt(tmp_path, first, *, declare_prior=True):
    prior = [{"release_id": RID, "revisions": {n: [CAND_REV[n]] for n in SERVICES}, "ledger_sha256": "ab" * 32}] if declare_prior else None
    second = Scenario(tmp_path / "second", world=first.world, prior=prior, release_id=RID2, baseline_from=first)
    second.world.health[so.tag_url(RID2, CANON["f42-api"])] = [HEALTHY]
    return second


def test_rt01_a_second_attempt_on_the_commit_builds_its_own_image_tag(tmp_path):
    first = Scenario(tmp_path / "first")
    first.to("Freeze")  # the first attempt built and registered f42-web:<sha12>-01
    second = Scenario(tmp_path / "second", world=first.world, release_id=RID2, baseline_from=first)
    assert second.tag().endswith(f"{SHORT12}-02") and first.tag().endswith(f"{SHORT12}-01")
    assert second.run("BeforeAnyWrite")["phase"] == "BeforeAnyWrite"


def test_rt02_a_failed_attempt_with_its_tags_removed_and_declared_lets_the_next_pass_before_candidate(tmp_path):
    first = Scenario(tmp_path / "first")
    first.to("AfterSmoke")
    for name in SERVICES:
        first.world.remove_tag(name)
    second = second_attempt(tmp_path, first)
    second.to("BeforeCandidate")


def test_rt03_a_failed_attempt_after_the_pin_passes_and_the_pin_is_idempotent(tmp_path):
    first = Scenario(tmp_path / "first")
    first.to("BeforeCandidate")
    second = Scenario(tmp_path / "second", world=first.world, release_id=RID2, baseline_from=first)
    second.to("BeforeCandidate")
    second.world.pin()
    assert second.run("BeforeCandidate")["phase"] == "BeforeCandidate"


def test_rt04_the_first_attempts_revisions_without_priorAttempts_are_unrelated(tmp_path):
    first = Scenario(tmp_path / "first")
    first.to("AfterSmoke")
    for name in SERVICES:
        first.world.remove_tag(name)
    second = second_attempt(tmp_path, first, declare_prior=False)
    stops_with(second, "BeforeAnyWrite", "UNRELATED_REVISION")


def test_rt05_the_first_attempts_tags_still_present_stop_the_second(tmp_path):
    first = Scenario(tmp_path / "first")
    first.to("AfterSmoke")
    second = second_attempt(tmp_path, first)
    stops_with(second, "BeforeAnyWrite", "TAG_MAPPING")


# identity, pin, baseline template, anonymous boundary at the start

@pytest.mark.parametrize("phase", so.PRINCIPAL_PHASES)
def test_hr_unverifiable_principals_stop_the_four_phases_that_verify_them(tmp_path, phase):
    s = Scenario(tmp_path)
    s.to("BeforeCandidate") if phase == "BeforeRollback" else s.ready(phase)
    s.world.principals_ok = False
    stops_with(s, phase, "IDENTITY")


def test_hr_principals_are_read_in_exactly_the_four_phases_that_name_them(tmp_path):
    s = Scenario(tmp_path)
    counts = {}
    for phase in s.ORDER:
        s.step(phase)
        before = sum(1 for call in s.reader.calls if call[0] == "principals")
        s.run(phase)
        counts[phase] = sum(1 for call in s.reader.calls if call[0] == "principals") - before
    assert counts == {"BeforeAnyWrite": 1, "Freeze": 0, "BeforeCandidate": 1, "BeforeSmoke": 0, "AfterSmoke": 0,
                      "BeforePromotion": 1, "AfterAgentPromotion": 0, "AfterPromotion": 0}


@pytest.mark.parametrize("change", ["account", "project", "impersonation"])
def test_hr_the_cli_caller_project_and_impersonation_are_pinned(tmp_path, change):
    s = Scenario(tmp_path)
    if change == "account":
        s.world.config["core"]["account"] = "someone.else@example.invalid"
    elif change == "project":
        s.world.config["core"]["project"] = "another-project"
    else:
        s.world.config["auth"] = {"impersonate_service_account": "x@y.iam.gserviceaccount.com"}
    stops_with(s, "BeforeAnyWrite", "IDENTITY")


def test_hr_a_public_agent_at_the_start_stops_before_any_write(tmp_path):
    s = Scenario(tmp_path)
    s.world.anonymous[CANON["f42-agent"]] = 200
    stops_with(s, "BeforeAnyWrite", "AGENT_PUBLIC")


def test_hr_the_pin_is_required_at_before_candidate_but_not_before_it(tmp_path):
    s = Scenario(tmp_path)
    s.to("Freeze")
    assert all(any(e["latest"] for e in so.service_view(s.world.service_raw(n))["traffic"]["spec"]) for n in SERVICES)
    stops_with(s, "BeforeCandidate", "TRAFFIC", "LATEST")
    s.world.pin("f42-agent")
    stops_with(s, "BeforeCandidate", "TRAFFIC", "f42-api")
    s.world.pin("f42-api")
    assert s.run("BeforeCandidate")["phase"] == "BeforeCandidate"


@pytest.mark.parametrize("field", ["image", "env", "service_account", "annotation"])
def test_hr_the_service_template_must_equal_the_baseline_until_the_candidate_is_deployed(tmp_path, field):
    s = Scenario(tmp_path)
    w = s.world
    if field == "image":
        w.svc["f42-agent"]["image"] = f"{REPO}:other"
    elif field == "env":
        w.svc["f42-agent"]["env"]["EXTRA"] = "x"
    elif field == "service_account":
        w.svc["f42-agent"]["sa"] = "other@ogilvy-trends-v2.iam.gserviceaccount.com"
    else:
        w.svc["f42-agent"]["tmpl_ann"]["note"] = "x"
    stops_with(s, "BeforeAnyWrite", "BASELINE", "template")


def test_hr_a_prior_attempt_residue_loosens_only_the_template_image_and_environment(tmp_path):
    first = Scenario(tmp_path / "first")
    first.to("AfterSmoke")
    for name in SERVICES:
        first.world.remove_tag(name)
    second = second_attempt(tmp_path, first)
    first.world.svc["f42-agent"]["sa"] = "other@ogilvy-trends-v2.iam.gserviceaccount.com"
    stops_with(second, "BeforeAnyWrite", "BASELINE", "service_account")


def test_hr_the_release_id_must_name_the_target_commit_and_the_attempt_forms(tmp_path):
    for bad in ("rel-d666ef6-1", "rel-aaaaaaa-01", "latest", "rel-D666EF6-01"):
        s = Scenario(tmp_path / bad)
        s.bound["release_id"] = bad
        assert s.stop("BeforeAnyWrite").code == "BINDINGS", bad
