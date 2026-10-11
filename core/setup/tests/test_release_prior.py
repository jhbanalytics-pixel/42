"""Release C: the prior release is a bound input, not a constant (lead ruling of 11 Oct 2026). No cloud, no network.

Release A is live when Release C starts, so the rollback target is A's pair (rel-3710c5e-01, tagged, the API naming A's agent tag URL
with the canonical audience), not the a80 pair on the canonical URL. The bindings carry a prior-release record; the helper judges live
against it, retains the prior release's tags and restores the prior release exactly. The a80 behaviour is the default and is tested in
the other files; here every test starts from the state Release A leaves (World(after="A")) with the jobs on an image other than e77c3819.
"""
import copy
import json

import pytest

from core.setup.release import services_only as so
from core.setup.tests.release_world import (
    A80_REV, A_COMMIT, A_REV, A_RID, A_TAG_URL, B_JOBS_DIGEST, CAND_REV, CANON, JOBS_DIGEST, OTHER_DIGEST, REPO, RID, FakeReader, Scenario,
    World, prior_record_a, retained_for, write_json)

SERVICES = so.SERVICES
AGENT, API = SERVICES


def after_a(jobs_digest=B_JOBS_DIGEST):
    return World(after="A", jobs_digest=jobs_digest)


def capture(world, record=None):
    return so.capture_baseline(FakeReader(world), "2026-10-08T10:00:00+00:00", prior=record or prior_record_a())


def rolled_back_to_a(tmp_path):
    s = Scenario(tmp_path, after="A")
    s.to("AfterPromotion")
    for name in reversed(SERVICES):
        s.world.restore(name)
    return s


# edits to a world

def set_api_env(world, name, value):
    world.set_env(A_REV[API], name, value)
    world.svc[API]["env"][name] = value


def drop_api_env(world, name):
    world.drop_env(A_REV[API], name)
    world.svc[API]["env"].pop(name, None)


def strip_tag(world, service, tag):
    world.svc[service]["traffic"] = [{k: v for k, v in e.items() if not (k == "tag" and v == tag)} for e in world.svc[service]["traffic"]]


def retag(world, service, tag, revision):
    strip_tag(world, service, tag)
    world.svc[service]["traffic"].append({"revisionName": revision, "percent": 0, "tag": tag})


def job_image(world, name, digest):
    container = world.jobs[name]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
    container["image"] = f"{so.REGION}-docker.pkg.dev/{so.PROJECT}/intelligence-42/jobs{digest}"


def serve_a80_agent(world):
    world.svc[AGENT]["traffic"] = [{"revisionName": A80_REV[AGENT], "percent": 100}, {"revisionName": A_REV[AGENT], "percent": 0, "tag": A_RID}]


def serve_digest(world, service, digest):
    world.revisions[A_REV[service]]["status"]["imageDigest"] = f"{REPO}@{digest}"


# the state A leaves is a state a release bound to A accepts, from the baseline to the promotion

def test_the_state_release_a_leaves_is_accepted_from_the_baseline_to_the_promotion(tmp_path):
    s = Scenario(tmp_path, after="A")
    baseline = s.baseline_path.read_text(encoding="utf-8")
    assert A_TAG_URL[AGENT] in baseline and CANON[AGENT] != A_TAG_URL[AGENT]
    assert s.to("AfterPromotion")["phase"] == "AfterPromotion"
    for name in SERVICES:
        serving = [e for e in s.world.svc[name]["traffic"] if e["percent"]]
        assert [(e["revisionName"], e["tag"]) for e in serving] == [(CAND_REV[name], RID)]
        assert [(e["revisionName"], e["percent"]) for e in s.world.svc[name]["traffic"] if e.get("tag") == A_RID] == [(A_REV[name], 0)]


@pytest.mark.parametrize("digest", [B_JOBS_DIGEST, JOBS_DIGEST])
def test_the_jobs_baseline_is_whatever_the_fresh_baseline_captured_not_a_constant(tmp_path, digest):
    s = Scenario(tmp_path, after="A", world=after_a(digest))
    assert s.bound["priorRelease"]["jobs_image_digest"] == digest
    assert s.run("BeforeAnyWrite")["phase"] == "BeforeAnyWrite"


def test_a_job_that_moves_after_the_baseline_stops_the_release(tmp_path):
    s = Scenario(tmp_path, after="A")
    job_image(s.world, "f42-learn", "@" + OTHER_DIGEST)
    assert s.stop("BeforeAnyWrite").code == "JOB_CHANGED"


def test_the_manifest_carries_the_prior_release_and_a_manifest_that_names_another_is_refused(tmp_path):
    s = Scenario(tmp_path, after="A")
    s.to("BeforeCandidate")
    assert s.manifest()["prior_release"] == s.bound["priorRelease"]
    other = copy.deepcopy(s.bound["priorRelease"])
    other["image_digest"] = OTHER_DIGEST
    s.rewrite_manifest(prior_release=other)
    assert s.stop("BeforeCandidate").code == "MANIFEST_STAGE1"


def test_a_release_bound_to_a80_writes_no_prior_release_into_its_manifest(tmp_path):
    s = Scenario(tmp_path)
    s.to("BeforeCandidate")
    assert "prior_release" not in s.manifest()


# the receipts: made against the prior commit at the release commit, judged again where the helper acts

RECEIPTS = (("compat-receipt.json", "compatReceiptSha256", "COMPAT"), ("old-reader-receipt.json", "oldReaderReceiptSha256", "OLD_READER"))
BAD_RECEIPT_COMMITS = {
    "a base that is a80be1d": lambda r: r.update(base_commit="a80be1d"),
    "no base": lambda r: r.pop("base_commit"),
    "a base of six characters": lambda r: r.update(base_commit="3710c5"),
    "a base that only looks like the prior commit": lambda r: r.update(base_commit="3710c5e" + "0" * 33),
    "a candidate that is another commit": lambda r: r.update(candidate_commit="b" * 40),
    "no candidate": lambda r: r.pop("candidate_commit"),
}


@pytest.mark.parametrize("name,key,code", RECEIPTS)
@pytest.mark.parametrize("label", sorted(BAD_RECEIPT_COMMITS))
def test_the_helper_refuses_a_receipt_that_was_not_made_against_the_prior_commit_at_the_release_commit(tmp_path, name, key, code, label):
    s = Scenario(tmp_path, after="A")
    path = s.release_dir / name
    receipt = json.loads(path.read_text(encoding="utf-8"))
    BAD_RECEIPT_COMMITS[label](receipt)
    s.bound[key] = write_json(path, receipt)
    s.ready("BeforeCandidate")
    error = s.stop("BeforeCandidate")
    assert error.code == code and "receipt" in error.message, (label, error.code, error.message)


def test_the_helper_accepts_the_prior_commit_in_its_full_form_as_the_base(tmp_path):
    s = Scenario(tmp_path, after="A")
    for name, key, _ in RECEIPTS:
        path = s.release_dir / name
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt["base_commit"] = A_COMMIT
        s.bound[key] = write_json(path, receipt)
    s.ready("BeforeCandidate")
    assert s.run("BeforeCandidate")["phase"] == "BeforeCandidate"


# the baseline capture judges live against the record, and the record is the pin: nothing is read from the payload being proved

def test_the_capture_that_knows_only_a80_still_refuses_the_state_a_leaves():
    with pytest.raises(so.Stop) as stopped:
        so.capture_baseline(FakeReader(after_a()), "2026-10-08T10:00:00+00:00")
    assert stopped.value.code == "BASELINE" and "AGENT_URL" in stopped.value.message


def test_the_capture_with_the_record_accepts_the_state_a_leaves_and_stores_the_literal_it_pinned():
    baseline = capture(after_a())
    assert baseline["api_agent_url_literal"] == A_TAG_URL[AGENT] == prior_record_a()["api_agent_url_literal"]
    assert baseline["canonical"][AGENT] == CANON[AGENT]
    assert baseline["services"][API]["serving"]["name"] == A_REV[API]


DRIFTS = {
    "the API names the canonical URL": lambda w: set_api_env(w, "AGENT_URL", CANON[AGENT]),
    "the API names another release's tag URL": lambda w: set_api_env(w, "AGENT_URL", so.tag_url("rel-aaaaaaa-01", CANON[AGENT])),
    "the API names the tag URL with a trailing slash": lambda w: set_api_env(w, "AGENT_URL", A_TAG_URL[AGENT] + "/"),
    "the API carries no audience": lambda w: drop_api_env(w, "AGENT_AUDIENCE"),
    "the API audience is another URL": lambda w: set_api_env(w, "AGENT_AUDIENCE", A_TAG_URL[AGENT]),
    "the agent serves the a80 revision": serve_a80_agent,
    "the API serves another digest": lambda w: serve_digest(w, API, OTHER_DIGEST),
    "the agent serves another digest": lambda w: serve_digest(w, AGENT, OTHER_DIGEST),
    "the agent tag is on another revision": lambda w: retag(w, AGENT, A_RID, A80_REV[AGENT]),
    "the agent holds no tag": lambda w: strip_tag(w, AGENT, A_RID),
    "the API holds no tag": lambda w: strip_tag(w, API, A_RID),
    "the jobs run two images": lambda w: job_image(w, "f42-learn", "@" + OTHER_DIGEST),
    "a job runs a tag and not a digest": lambda w: job_image(w, "f42-learn", ":latest"),
}


@pytest.mark.parametrize("label", sorted(DRIFTS))
def test_a_live_state_that_is_not_the_recorded_prior_release_is_refused_at_the_capture(label):
    world = after_a()
    DRIFTS[label](world)
    with pytest.raises(so.Stop) as stopped:
        capture(world)
    assert stopped.value.code == "BASELINE", (label, stopped.value.message)


@pytest.mark.parametrize("label", ["the jobs run two images", "a job runs a tag and not a digest"])
def test_the_fourteen_jobs_must_run_one_image_by_digest_whatever_the_record_says_about_them(label):
    world = after_a()
    DRIFTS[label](world)
    with pytest.raises(so.Stop) as stopped:
        capture(world, prior_record_a(jobs_digest=None))
    assert stopped.value.code == "BASELINE" and "job" in stopped.value.message


def test_the_record_is_the_pin_a_record_that_names_another_literal_refuses_a_live_state_that_agrees_with_itself():
    record = prior_record_a()
    record["tag_urls"][AGENT] = record["api_agent_url_literal"] = so.tag_url("rel-aaaaaaa-01", CANON[AGENT])
    with pytest.raises(so.Stop) as stopped:
        capture(after_a(), record)
    assert stopped.value.code == "BASELINE"


def test_a_baseline_that_is_not_the_recorded_release_is_refused_where_the_helper_reads_the_bindings(tmp_path):
    s = Scenario(tmp_path, after="A")
    s.bound["priorRelease"]["image_digest"] = OTHER_DIGEST
    assert s.stop("BeforeAnyWrite").code == "BASELINE"


def test_the_jobs_digest_the_bindings_record_must_be_the_one_the_baseline_holds(tmp_path):
    s = Scenario(tmp_path, after="A")
    s.bound["priorRelease"]["jobs_image_digest"] = OTHER_DIGEST
    assert s.stop("BeforeAnyWrite").code == "BASELINE"


# the bindings: the record is validated, and the tags it retains are derived from it, never typed beside it

def drop(*path):
    def change(bound):
        node = bound
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]
    return change


def put(value, *path):
    def change(bound):
        node = bound
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return change


BAD_BINDINGS = {
    "no record key": drop("priorRelease", "image_digest"),
    "an unknown record key": put(1, "priorRelease", "extra"),
    "an unknown record version": put(2, "priorRelease", "schema_version"),
    "another kind": put("baseline-A", "priorRelease", "kind"),
    "a release id that is not rel-sha7-nn": put("rel-3710c5e", "priorRelease", "release_id"),
    "a commit that is not 40 hexadecimal characters": put("3710c5e", "priorRelease", "commit"),
    "a commit that does not match the release id": put("b" * 40, "priorRelease", "commit"),
    "a tag that is not the release id": put("rel-aaaaaaa-01", "priorRelease", "tag"),
    "a revision of the other service": put(A_REV[API], "priorRelease", "revisions", AGENT),
    "an image digest that is a shape and not a digest": put("sha256:abc", "priorRelease", "image_digest"),
    "a jobs digest that is not a digest": put("e77c3819", "priorRelease", "jobs_image_digest"),
    "no jobs digest": drop("priorRelease", "jobs_image_digest"),
    "a tag URL that is not the release id on a host": put("https://example.com", "priorRelease", "tag_urls", API),
    "an API literal that is not the agent tag URL": put(CANON[AGENT], "priorRelease", "api_agent_url_literal"),
    "an agent tag URL on another host than the audience": lambda bound: [
        bound["priorRelease"]["tag_urls"].update({AGENT: so.tag_url(A_RID, "https://f42-agent-other-uc.a.run.app")}),
        bound["priorRelease"].update(api_agent_url_literal=so.tag_url(A_RID, "https://f42-agent-other-uc.a.run.app")),
        bound.update(retainedAgentUrls=[so.tag_url(A_RID, "https://f42-agent-other-uc.a.run.app")])],
    "an audience that is not a URL": put("agent", "priorRelease", "agent_audience"),
    "a source hash that is not a hash": put("zz", "priorRelease", "sources", "lock_sha256"),
    "no retained tags": drop("retainedTags"),
    "retained tags that omit the prior tag": put({AGENT: {}, API: {}}, "retainedTags"),
    "retained tags beside the prior tag": put({AGENT: {A_RID: A_REV[AGENT], "stray": A_REV[AGENT]}, API: {A_RID: A_REV[API]}}, "retainedTags"),
    "the prior tag on another revision": put({AGENT: {A_RID: A80_REV[AGENT]}, API: {A_RID: A_REV[API]}}, "retainedTags"),
    "no retained agent URL": drop("retainedAgentUrls"),
    "another retained agent URL": put([so.tag_url("rel-aaaaaaa-01", CANON[AGENT])], "retainedAgentUrls"),
    "the canonical URL as the retained agent URL": put([CANON[AGENT]], "retainedAgentUrls"),
    "no release letter": drop("releaseLetter"),
    "a release letter that is not a capital": put("c", "releaseLetter"),
    "a release letter of two characters": put("CC", "releaseLetter"),
    "a release that is the prior release": lambda bound: bound.update(release_id=A_RID, target=A_COMMIT),
}


@pytest.mark.parametrize("label", sorted(BAD_BINDINGS))
def test_bindings_with_a_malformed_or_inconsistent_prior_release_are_refused(tmp_path, label):
    bound = Scenario(tmp_path, after="A").bound
    BAD_BINDINGS[label](bound)
    with pytest.raises(so.Stop) as stopped:
        so.validate_bindings(bound)
    assert stopped.value.code == "BINDINGS", (label, stopped.value.message)


def test_bindings_that_carry_the_record_the_tags_and_a_letter_are_valid(tmp_path):
    bound = Scenario(tmp_path, after="A").bound
    assert bound["releaseLetter"] == "C" and so.validate_bindings(bound)
    tags, urls = retained_for(bound["priorRelease"])
    assert bound["retainedTags"] == tags == {n: {A_RID: A_REV[n]} for n in SERVICES}
    assert bound["retainedAgentUrls"] == urls == [A_TAG_URL[AGENT]]


def test_bindings_without_a_record_keep_the_a80_meaning_and_may_still_retain_a_tag_of_their_own(tmp_path):
    s = Scenario(tmp_path, retained={AGENT: {"keep-me": A80_REV[AGENT]}})
    assert so.validate_bindings(s.bound) and "priorRelease" not in s.bound


# the retained tags: the prior release's tag is never moved, never retired and always where the prior API needs it

def test_a_prior_tag_moved_to_another_revision_stops_the_release_after_the_candidate_deploy(tmp_path):
    s = Scenario(tmp_path, after="A")
    s.ready("BeforeSmoke")
    retag(s.world, AGENT, A_RID, A80_REV[AGENT])
    assert s.stop("BeforeSmoke").code == "TAG_MAPPING"


@pytest.mark.parametrize("service", SERVICES)
def test_a_prior_tag_that_has_gone_stops_the_release_before_any_write_and_after_the_candidate(tmp_path, service):
    s = Scenario(tmp_path / "before", after="A")
    strip_tag(s.world, service, A_RID)
    assert s.stop("BeforeAnyWrite").code == "TAG_MAPPING"
    s = Scenario(tmp_path / "after", after="A")
    s.ready("BeforeSmoke")
    strip_tag(s.world, service, A_RID)
    assert s.stop("BeforeSmoke").code == "TAG_MAPPING"


def test_a_tag_nobody_retains_still_stops_a_release_bound_to_the_prior_release(tmp_path):
    s = Scenario(tmp_path, after="A")
    s.world.svc[API]["traffic"].append({"revisionName": A80_REV[API], "percent": 0, "tag": "stray"})
    assert s.stop("BeforeAnyWrite").code == "TAG_MAPPING"


def test_retire_refuses_the_prior_tag_as_the_tag_to_retire(tmp_path):
    s = rolled_back_to_a(tmp_path)
    s.run("AfterRollback")
    assert s.stop("BeforeRetire", tag=A_RID).code == "TAG_MAPPING"
    assert s.stop("AfterRetire", tag=A_RID).code == "TAG_MAPPING"


def retired(tmp_path):
    s = rolled_back_to_a(tmp_path)
    s.run("AfterRollback")
    s.run("BeforeRetire", tag=RID)
    for name in SERVICES:
        s.world.remove_tag(name)
    return s


def test_retiring_the_release_tag_leaves_the_prior_tag_on_the_prior_revisions(tmp_path):
    s = retired(tmp_path)
    assert s.run("AfterRetire", tag=RID)["tags"] == {AGENT: False, API: False}
    for name in SERVICES:
        assert [(e["revisionName"], e["percent"]) for e in s.world.svc[name]["traffic"] if e.get("tag") == A_RID] == [(A_REV[name], 100)]


@pytest.mark.parametrize("service", SERVICES)
def test_a_retire_that_also_removed_the_prior_tag_stops_at_the_readback(tmp_path, service):
    s = retired(tmp_path)
    strip_tag(s.world, service, A_RID)
    assert s.stop("AfterRetire", tag=RID).code == "TAG_MAPPING"


def test_the_release_tag_of_a_serving_candidate_is_kept_while_the_candidate_api_names_it(tmp_path):
    s = Scenario(tmp_path, after="A")
    s.to("AfterPromotion")
    assert s.stop("BeforeRetire", tag=RID).code == "TAG_REFERENCE"


# the rollback to A: the prior revisions by name, the prior literal and audience exactly, the prior tag where the prior API needs it

def test_the_rollback_to_a_is_verified_by_readback(tmp_path):
    s = rolled_back_to_a(tmp_path)
    result = s.run("AfterRollback")
    assert result["phase"] == "AfterRollback"
    assert all(r["candidate_revision_present"] for r in result["residue"].values())
    assert result["residue"][API]["tags"] == sorted([A_RID, RID])


ROLLBACK_DRIFTS = {
    "the agent still on the candidate": (lambda w: w.promote(AGENT), "TRAFFIC"),
    "the API still on the candidate": (lambda w: w.promote(API), "TRAFFIC"),
    "traffic following latest": (lambda w: w.svc[API].update(traffic=[{"latestRevision": True, "percent": 100}]), "TRAFFIC"),
    "the API naming the canonical URL": (lambda w: set_api_env(w, "AGENT_URL", CANON[AGENT]), "ENV"),
    "the API naming another tag URL": (lambda w: set_api_env(w, "AGENT_URL", so.tag_url("rel-aaaaaaa-01", CANON[AGENT])), "ENV"),
    "the API without its audience": (lambda w: drop_api_env(w, "AGENT_AUDIENCE"), "ENV"),
    "the API with another audience": (lambda w: set_api_env(w, "AGENT_AUDIENCE", A_TAG_URL[AGENT]), "ENV"),
    "the API on another digest": (lambda w: serve_digest(w, API, OTHER_DIGEST), "DIGEST"),
    "the agent tag removed": (lambda w: strip_tag(w, AGENT, A_RID), "TAG_MAPPING"),
    "the agent tag on another revision": (lambda w: retag(w, AGENT, A_RID, A80_REV[AGENT]), "TAG_MAPPING"),
    "a job changed": (lambda w: job_image(w, "f42-learn", "@" + OTHER_DIGEST), "JOB_CHANGED"),
}


@pytest.mark.parametrize("label", sorted(ROLLBACK_DRIFTS))
def test_a_rollback_that_is_not_the_prior_release_exactly_stops_naming_the_field(tmp_path, label):
    s = rolled_back_to_a(tmp_path)
    change, code = ROLLBACK_DRIFTS[label]
    change(s.world)
    error = s.stop("AfterRollback")
    assert error.code == code, (label, error.code, error.message)


def test_the_rollback_to_the_a80_pair_is_not_the_rollback_to_a_for_a_release_bound_to_a(tmp_path):
    s = rolled_back_to_a(tmp_path)
    for name in SERVICES:
        s.world.svc[name]["traffic"] = [{"revisionName": A80_REV[name], "percent": 100}]
    assert s.stop("AfterRollback").code == "TRAFFIC"


def test_before_rollback_names_the_states_of_a_release_bound_to_a_and_records_a_missing_prior_tag_without_blocking(tmp_path):
    s = Scenario(tmp_path, after="A")
    s.to("AfterPromotion")
    s.world.restore(API)
    assert s.run("BeforeRollback")["state"] == "agent candidate, api prior"
    for name in SERVICES:
        strip_tag(s.world, name, A_RID)
    result = s.run("BeforeRollback")
    assert result["blocking"]["retained_tags"] == {"blocking": False, "missing": sorted(f"{n}:{A_RID}" for n in SERVICES)}


def test_before_rollback_refuses_a_target_that_is_not_the_prior_revision_digest(tmp_path):
    s = Scenario(tmp_path, after="A")
    s.to("AfterPromotion")
    serve_digest(s.world, API, OTHER_DIGEST)
    assert s.stop("BeforeRollback").code == "DIGEST"


def test_the_packet_tool_still_names_the_a80_pair_for_the_release_that_reads_it_there():
    from core.setup.release import packet

    assert packet.A80_SERVING == {"f42-agent": "f42-agent-00047-677", "f42-api": "f42-api-00041-lns"} == so.A80_PRIOR["revisions"]
    assert packet.A80_JOBS_DIGEST_PREFIX == "sha256:e77c3819" == so.A80_PRIOR["jobs_digest_prefix"]
