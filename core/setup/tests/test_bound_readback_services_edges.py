"""Edges of the services-only readback that the first pass of mutants showed nothing pinned (W8-REL 5.3). Each test names
the one thing it holds; the codes asserted are exact, so a check that stops for another reason does not satisfy it."""
import json

import pytest

from core.setup.release import services_only as so
from core.setup.tests.release_world import (
    A80_REV, API_ENV, CAND_DIGEST, CAND_REV, CANON, CONFIG_SHA, HEALTHY, OTHER_DIGEST, REPO, RID, SHORT12, TAG_URL, World, Scenario,
    write_json)
from core.setup.tests.test_bound_readback_services import rolled_back, stops_with

SERVICES = so.SERVICES


def test_a_mode_other_than_services_only_in_the_bindings_is_a_mode_stop(tmp_path):
    s = Scenario(tmp_path)
    s.bound["mode"] = "full"
    stops_with(s, "BeforeAnyWrite", "MODE")


def test_a_compat_or_old_reader_receipt_with_an_unknown_version_is_a_schema_stop_even_when_every_hash_agrees(tmp_path):
    for kind, key in (("compat", "compatReceiptSha256"), ("old-reader", "oldReaderReceiptSha256")):
        s = Scenario(tmp_path / kind)
        s.bound[key] = write_json(s.release_dir / f"{kind}-receipt.json", {"schema_version": 2, "verdict": "pass"})
        s.to("Freeze")
        s.step("BeforeCandidate")
        stops_with(s, "BeforeCandidate", "SCHEMA_VERSION")


def test_the_manifest_sidecar_must_hold_the_manifest_hash(tmp_path):
    s = Scenario(tmp_path)
    s.to("BeforeCandidate")
    sidecar = s.release_dir / "release-manifest.sha256"
    sidecar.write_text("0" * 64 + "\n", encoding="utf-8")
    s.step("BeforeSmoke")
    stops_with(s, "BeforeSmoke", "MANIFEST_HASH", "sidecar")
    sidecar.unlink()
    stops_with(s, "BeforeSmoke", "MANIFEST_HASH", "sidecar")


def test_a_candidate_is_judged_by_its_own_description_even_when_the_latest_ready_revision_is_another(tmp_path):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    # The candidate exists but is not Ready, so the service still reports the a80 revision as latest ready.
    s.world.revisions[CAND_REV["f42-agent"]]["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
    s.world.svc["f42-agent"]["latest_ready"] = A80_REV["f42-agent"]
    stops_with(s, "BeforeSmoke", "CANDIDATE_REVISION", "not Ready")


def test_a_candidate_with_a_second_container_is_a_candidate_revision_stop(tmp_path):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    s.world.revisions[CAND_REV["f42-agent"]]["spec"]["containers"].append({"image": "sidecar", "env": []})
    stops_with(s, "BeforeSmoke", "CANDIDATE_REVISION", "container")


def test_a_candidate_missing_from_the_revision_list_is_a_candidate_revision_stop(tmp_path):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    s.world.revision_names["f42-api"].remove(CAND_REV["f42-api"])
    stops_with(s, "BeforeSmoke", "CANDIDATE_REVISION", "does not exist")


@pytest.mark.parametrize("names", [("f42-agent", "f42-api"), ("f42-agent",), ("f42-api",)])
def test_a_candidate_digest_that_differs_from_the_manifest_stops_whichever_services_carry_it(tmp_path, names):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    for name in names:
        s.world.revisions[CAND_REV[name]]["status"]["imageDigest"] = f"{REPO}@{OTHER_DIGEST}"
    stops_with(s, "BeforeSmoke", "DIGEST", "digest")


def test_a_tag_url_that_is_not_the_expected_one_is_a_tag_mapping_stop(tmp_path):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    for entry in s.world.svc["f42-api"]["traffic"]:
        if entry.get("tag"):
            entry["_url"] = "https://rel-d666ef6-01---f42-api-elsewhere-uc.a.run.app"
    stops_with(s, "BeforeSmoke", "TAG_MAPPING", "URL")


def test_a_template_environment_that_differs_from_the_expected_one_is_a_preservation_stop(tmp_path):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    s.world.svc["f42-agent"]["env"]["EXTRA"] = "x"
    stops_with(s, "BeforeSmoke", "PRESERVATION", "environment")


def test_a_latest_created_revision_that_is_not_the_candidate_is_a_preservation_stop(tmp_path):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    s.world.svc["f42-api"]["latest_created"] = A80_REV["f42-api"]
    stops_with(s, "BeforeSmoke", "PRESERVATION", "latest created")


def test_a_service_url_that_is_not_the_bound_canonical_one_is_a_baseline_stop(tmp_path):
    s = Scenario(tmp_path)
    s.world.svc["f42-api"]["url"] = "https://f42-api-590353929363.us-central1.run.app"
    stops_with(s, "BeforeAnyWrite", "BASELINE", "URL")


@pytest.mark.parametrize("health", [(401, {"ok": True, "checks": {"agent": "ok"}}), (500, {"ok": True, "checks": {"agent": "ok"}}),
                                    (204, {"ok": True, "checks": {"agent": "ok"}})])
def test_a_health_body_that_says_ok_does_not_pass_without_http_200(tmp_path, health):
    s = Scenario(tmp_path)
    s.ready("BeforeSmoke")
    s.world.health[TAG_URL["f42-api"]] = [health]
    stops_with(s, "BeforeSmoke", "API_AGENT")
    assert s.sleeps == []


def test_an_api_serving_an_agent_url_outside_the_declared_set_is_a_tag_reference_stop(tmp_path):
    s = Scenario(tmp_path)
    s.ready("AfterPromotion")
    s.world.set_env(CAND_REV["f42-api"], "AGENT_URL", "https://rel-aaaaaaa-01---f42-agent-fibxg5ynpq-uc.a.run.app")
    stops_with(s, "AfterPromotion", "TAG_REFERENCE")


def test_the_api_tag_host_is_not_a_reference_to_the_agent_tag(tmp_path):
    s = Scenario(tmp_path)
    s.ready("AfterPromotion")
    s.world.set_env(CAND_REV["f42-api"], "AGENT_URL", TAG_URL["f42-api"])
    stops_with(s, "AfterPromotion", "TAG_REFERENCE")


def test_a_retained_agent_url_declared_in_the_bindings_is_a_declared_reference(tmp_path):
    retained = "https://rel-aaaaaaa-01---f42-agent-fibxg5ynpq-uc.a.run.app"
    s = Scenario(tmp_path, bound_over={"retainedAgentUrls": [retained]})
    s.ready("AfterPromotion")
    s.world.set_env(CAND_REV["f42-api"], "AGENT_URL", retained)
    assert s.stop("AfterPromotion").code != "TAG_REFERENCE"


@pytest.mark.parametrize("case, fragment", [("tag_url_literal", "AGENT_URL"), ("deterministic_form", "AGENT_URL"),
                                            ("trailing_slash", "AGENT_URL"), ("audience_present", "AGENT_AUDIENCE")])
def test_a_rolled_back_api_must_carry_the_canonical_url_and_no_audience_and_the_stop_names_which(tmp_path, case, fragment):
    s = rolled_back(tmp_path)
    w, rev = s.world, A80_REV["f42-api"]
    if case == "tag_url_literal":
        w.set_env(rev, "AGENT_URL", TAG_URL["f42-agent"])
    elif case == "deterministic_form":
        w.set_env(rev, "AGENT_URL", "https://f42-agent-590353929363.us-central1.run.app")
    elif case == "trailing_slash":
        w.set_env(rev, "AGENT_URL", CANON["f42-agent"] + "/")
    else:
        w.set_env(rev, "AGENT_AUDIENCE", CANON["f42-agent"])
    stops_with(s, "AfterRollback", "ENV", fragment)


@pytest.mark.parametrize("case", ["older_api", "older_agent", "one_not_rolled_back", "latest_following", "candidate_traffic"])
def test_a_rollback_that_is_not_the_a80_pair_by_name_is_a_traffic_stop(tmp_path, case):
    s = rolled_back(tmp_path)
    w = s.world
    if case == "older_api":
        w.svc["f42-api"]["traffic"] = [{"revisionName": "f42-api-00040-cw5", "percent": 100}]
    elif case == "older_agent":
        w.svc["f42-agent"]["traffic"] = [{"revisionName": "f42-agent-00046-wks", "percent": 100}]
    elif case == "one_not_rolled_back":
        w.promote("f42-agent")
    elif case == "latest_following":
        w.svc["f42-api"]["traffic"] = [{"latestRevision": True, "percent": 100}]
    else:
        w.svc["f42-api"]["traffic"] = [{"revisionName": A80_REV["f42-api"], "percent": 90}, {"revisionName": CAND_REV["f42-api"], "percent": 10, "tag": RID}]
    stops_with(s, "AfterRollback", "TRAFFIC")


def test_a_baseline_api_literal_that_its_own_revision_view_contradicts_is_a_baseline_stop(tmp_path):
    s = Scenario(tmp_path)
    other = "https://f42-agent-elsewhere-uc.a.run.app"
    baseline = json.loads(s.baseline_path.read_text(encoding="utf-8"))
    baseline["services"]["f42-api"]["serving"]["env"]["AGENT_URL"] = so.literal(other)
    s.bound["baselineSha256"] = write_json(s.baseline_path, baseline)
    s.world.set_env(A80_REV["f42-api"], "AGENT_URL", other)
    stops_with(s, "BeforeAnyWrite", "BASELINE", "baseline literal")


def test_an_a80_api_revision_that_already_carries_an_audience_variable_is_a_baseline_stop(tmp_path):
    world = World()
    world.set_env(A80_REV["f42-api"], "AGENT_AUDIENCE", CANON["f42-agent"])
    s = Scenario(tmp_path, world=world)
    stops_with(s, "BeforeAnyWrite", "BASELINE", "audience")


def test_a_build_config_that_differs_from_the_bound_hash_stops_freeze(tmp_path):
    s = Scenario(tmp_path)
    s.step("Freeze")
    s.world.source_sha = "0" * 64
    stops_with(s, "Freeze", "BUILD", "config")
    assert CONFIG_SHA != "0" * 64


def test_before_rollback_records_a_public_invoker_binding_without_blocking(tmp_path):
    s = Scenario(tmp_path)
    s.to("BeforeCandidate")
    s.world.policy["bindings"].append({"role": "roles/run.invoker", "members": ["allUsers"]})
    result = s.run("BeforeRollback")
    assert "public_invoker" in result["blocking"]["policy"]["drift"] and result["blocking"]["policy"]["blocking"] is False


def test_before_rollback_records_an_access_annotation_that_changed_without_blocking(tmp_path):
    s = Scenario(tmp_path)
    s.to("BeforeCandidate")
    s.world.svc["f42-agent"]["annotations"]["run.googleapis.com/invoker-iam-disabled"] = "true"
    result = s.run("BeforeRollback")
    assert result["blocking"]["policy"]["drift"] == ["f42-agent:invoker-iam-disabled"]


def test_a_before_rollback_agent_that_answers_an_anonymous_caller_is_recorded_without_blocking(tmp_path):
    s = Scenario(tmp_path)
    s.to("BeforeCandidate")
    s.world.anonymous[CANON["f42-agent"]] = 200
    assert s.run("BeforeRollback")["blocking"]["agent_public"] == {"blocking": False, "statuses": [200]}


def test_a_baseline_that_does_not_hold_the_fourteen_jobs_is_a_baseline_stop(tmp_path):
    s = Scenario(tmp_path)
    baseline = json.loads(s.baseline_path.read_text(encoding="utf-8"))
    baseline["jobs"].pop("f42-digest")
    s.bound["baselineSha256"] = write_json(s.baseline_path, baseline)
    stops_with(s, "BeforeAnyWrite", "BASELINE", "fourteen")


@pytest.mark.parametrize("bad", ["rel-d666ef6-1", "rel-aaaaaaa-01", "latest", "rel-D666EF6-01", "rel-d666ef6-001"])
def test_a_release_id_off_the_grammar_or_naming_another_commit_is_a_bindings_stop_even_with_consistent_deltas(tmp_path, bad):
    s = Scenario(tmp_path)
    agent = CANON["f42-agent"]
    s.bound["release_id"] = bad
    s.bound["envDeltas"]["f42-api"].update(AGENT_URL=so.tag_url(bad, agent))
    error = s.stop("BeforeAnyWrite")
    assert error.code == "BINDINGS" and "release id" in error.message, error.message


def test_an_f42_version_delta_that_is_not_the_short_commit_is_a_bindings_stop(tmp_path):
    s = Scenario(tmp_path)
    s.bound["envDeltas"]["f42-agent"]["F42_VERSION"] = "000000000000"
    stops_with(s, "BeforeAnyWrite", "BINDINGS", "F42_VERSION")


@pytest.mark.parametrize("inputs", [None, {}, {"build_id": "x"}, {"build_id": "x", "uploaded_source": "gs://b/o"}, {"build_id": "", "uploaded_source": "gs://b/o", "paste_started_utc": "2026-10-08T20:55:00+00:00"}])
def test_freeze_inputs_the_paste_did_not_write_are_a_bindings_stop(tmp_path, inputs):
    s = Scenario(tmp_path)
    s.step("Freeze")
    path = s.evidence / "freeze-inputs.json"
    if inputs is None:
        path.unlink()
    else:
        path.write_text(json.dumps(inputs), encoding="utf-8")
    stops_with(s, "Freeze", "BINDINGS")


@pytest.mark.parametrize("version", [None, 0, 2, "1", True])
def test_freeze_inputs_of_an_unknown_version_are_a_schema_version_stop(tmp_path, version):
    s = Scenario(tmp_path)
    s.step("Freeze")
    path = s.evidence / "freeze-inputs.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value.pop("schema_version")
    if version is not None:
        value["schema_version"] = version
    path.write_text(json.dumps(value), encoding="utf-8")
    stops_with(s, "Freeze", "SCHEMA_VERSION")


def test_the_stale_a80_rollback_targets_are_refused_in_the_bindings(tmp_path):
    for key, value in (("rollbackRevision", "f42-agent-00046-wks"), ("rollbackImageDigest", "sha256:" + "6c" * 32)):
        s = Scenario(tmp_path / key)
        s.bound[key] = value
        stops_with(s, "BeforeAnyWrite", "BINDINGS", key)
    s = Scenario(tmp_path / "nested")
    s.bound["services"] = [{"name": "f42-agent", "rollbackRevision": "f42-agent-00046-wks"}]
    stops_with(s, "BeforeAnyWrite", "BINDINGS", "rollbackRevision")
    for text in ("f42-agent-00046-wks", "f42-api-00040-cw5"):
        s = Scenario(tmp_path / text)
        s.bound["note"] = text
        stops_with(s, "BeforeAnyWrite", "BINDINGS", "pre-a80")
