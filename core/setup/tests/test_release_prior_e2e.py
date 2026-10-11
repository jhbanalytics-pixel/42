"""The release paste run end to end for Release C: the prior release is Release A, not a80, and the jobs run an image other than e77c3819.

The lesson of rel-7122f58-01 is the method. That Candidate stopped at Freeze because the paste never wrote freeze-inputs.json, and no
test had run the real paste against the real helper. So here, as in test_release_e2e.py, the packet is made only by the packet command
line (`prior-release` from a rebuilt Release A packet, `capture-baseline` and `bind` with it, then lock, review and receipt), and the
real SERVICES-PASTE.ps1 runs Candidate, Promote, Rollback and Retire in pwsh processes against the real bound_readback.py,
services_only.py, smoke.py, durable_effects_check.py, deploy_candidate.sh, git, tar, bash and py. The cloud is the only fake
(e2e_gcloud.py, e2e_shim). Nothing the paste or the helper writes is seeded.

These tests are in the deploy partition (release_test_ids.json). A missing pwsh fails them; nothing here skips for a missing
prerequisite except on a system that is not Windows, where the paste cannot find gcloud.cmd and py.exe.
"""
import json
import shutil

import pytest

from core.setup.release import services_only as so
from core.setup.tests import release_prereq
from core.setup.tests import release_world as rw
from core.setup.tests.e2e_world import E2E, ON_WINDOWS, ROOT
from core.setup.tests.test_release_e2e import PACKET_FILES, gcloud_writes, need, tagged, tail, traffic

pytestmark = pytest.mark.skipif(not ON_WINDOWS, reason="the paste finds gcloud as gcloud.cmd and the launcher as py.exe, which exist on Windows only")

AGENT, API = so.SERVICES
PHASES = ("BeforeAnyWrite", "Freeze", "BeforeCandidate", "BeforeSmoke", "AfterSmoke")


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    missing = release_prereq.prerequisites(ROOT)
    if shutil.which("pwsh") is None:
        pytest.fail("pwsh is not installed; the end to end release test cannot run (install PowerShell 7)")
    assert "bash" not in missing, "Git for Windows' bash is not installed"
    with pytest.MonkeyPatch.context() as patch:
        world = E2E(tmp_path_factory.mktemp("e2e-after-a"), patch, after="A")
        world.packet_files = sorted(p.name for p in world.release_dir.iterdir())
        yield world


@pytest.fixture(scope="module")
def candidate(e2e):
    return e2e.run("Candidate")


@pytest.fixture(scope="module")
def promote(e2e, candidate):
    need(candidate)
    return e2e.run("Promote")


@pytest.fixture(scope="module")
def rollback(e2e, promote):
    need(promote)
    return e2e.run("Rollback")


@pytest.fixture(scope="module")
def retire(e2e, rollback):
    need(rollback)
    return e2e.run("Retire")


def jobs_images(world):
    return {job["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["image"].rsplit("@", 1)[1] for job in world.jobs.values()}


def tag_calls(e2e):
    return [call["argv"][call["argv"].index("--remove-tags") + 1] for call in e2e.logged("gcloud") if "--remove-tags" in call["argv"]]


# The packet: made by the command line from Release A's own files

def test_the_packet_was_made_by_the_command_line_and_binds_release_a_as_the_prior_release(e2e):
    assert e2e.packet_files == PACKET_FILES
    bound = e2e.json_file("live-bindings.json")
    assert bound["priorRelease"]["release_id"] == rw.A_RID and bound["priorRelease"]["commit"] == rw.A_COMMIT
    assert bound["priorRelease"]["jobs_image_digest"] == rw.B_JOBS_DIGEST != "sha256:" + "e77c3819" + "0" * 56
    assert bound["retainedTags"] == {name: {rw.A_RID: rw.A_REV[name]} for name in so.SERVICES}
    assert bound["retainedAgentUrls"] == [rw.A_TAG_URL[AGENT]] and bound["releaseLetter"] == "C"
    assert e2e.bench.line.read_text(encoding="utf-8").startswith(f"RELEASE C {e2e.rid}:")
    assert e2e.json_file("independent-review.json")["verdict"] == "ACCEPT"


# Candidate

def test_candidate_runs_through_after_smoke_with_nothing_seeded_and_a_as_the_prior_release(e2e, candidate):
    assert candidate.returncode == 0, tail(candidate)
    assert [p["prompt"] for p in candidate.prompts] == ["Type DEPLOY to continue", "Smoke passcode"]
    for phase in PHASES:
        assert len(e2e.readbacks(phase)) == 1, phase
    assert (candidate.run_dir / "freeze-inputs.json").is_file()
    manifest = e2e.json_file("release-manifest.json")
    assert so.manifest_hash(manifest) == manifest["manifest_sha256"] == e2e.readbacks("Freeze")[0]["manifest_sha256"]
    assert manifest["prior_release"] == e2e.json_file("live-bindings.json")["priorRelease"]
    assert manifest["rollback"][API]["revision"] == rw.A_REV[API] and manifest["rollback"][AGENT]["revision"] == rw.A_REV[AGENT]
    assert manifest["canonical"]["api_agent_url_literal"] == rw.A_TAG_URL[AGENT]


def test_candidate_pinned_to_a_by_name_and_left_a_serving_with_its_tag_where_it_was(e2e, candidate):
    assert candidate.returncode == 0, tail(candidate)
    pins = [c["argv"] for c in e2e.logged("gcloud") if c["argv"][:3] == ["run", "services", "update-traffic"]]
    assert sorted(a[3] for a in pins) == [AGENT, API]
    assert {a[3]: [x for x in a if x.startswith("--to-revisions=")] for a in pins} == {name: [f"--to-revisions={rw.A_REV[name]}=100"] for name in so.SERVICES}
    world = e2e.world_now()
    for name in so.SERVICES:
        assert traffic(world, name) == {rw.A_REV[name]: 100}
        assert [(e["revisionName"], e["percent"]) for e in tagged(world, name, rw.A_RID)] == [(rw.A_REV[name], 100)]
        (entry,) = tagged(world, name, e2e.rid)
        assert entry["revisionName"] == f"{name}-{e2e.rid}" and entry["percent"] == 0


def test_candidate_changed_the_cloud_only_by_the_declared_commands_and_touched_no_job(e2e, candidate):
    assert candidate.returncode == 0, tail(candidate)
    assert gcloud_writes(e2e) == [("builds submit", ""), ("update-traffic", AGENT), ("update-traffic", API), ("run deploy", AGENT), ("run deploy", API)]
    assert not [c for c in e2e.logged("gcloud") if c["exit"] == 97 or c["argv"][:2] == ["run", "jobs"] and c["argv"][2] != "describe"]
    assert jobs_images(e2e.world_now()) == {rw.B_JOBS_DIGEST}


def test_candidate_deployed_the_api_to_the_agent_tag_url_of_c_with_the_canonical_audience_and_left_the_a_api_alone(e2e, candidate):
    assert candidate.returncode == 0, tail(candidate)
    world = e2e.world_now()
    env = {e["name"]: e.get("value") for e in world.revisions[f"{API}-{e2e.rid}"]["spec"]["containers"][0]["env"]}
    assert env["AGENT_URL"] == so.tag_url(e2e.rid, rw.CANON[AGENT]) and env["AGENT_AUDIENCE"] == rw.CANON[AGENT]
    kept = {e["name"]: e.get("value") for e in world.revisions[rw.A_REV[API]]["spec"]["containers"][0]["env"]}
    assert kept["AGENT_URL"] == rw.A_TAG_URL[AGENT] and kept["AGENT_AUDIENCE"] == rw.CANON[AGENT]


# Promote

def test_promote_moves_the_agent_then_the_api_and_the_a_tag_stays_where_a_needs_it(e2e, promote):
    assert promote.returncode == 0, tail(promote)
    assert [p["prompt"] for p in promote.prompts] == ["Type IDLE to continue", "Type DEPLOY to continue"]
    world = e2e.world_now()
    for name in so.SERVICES:
        assert traffic(world, name) == {f"{name}-{e2e.rid}": 100}
        assert [(e["revisionName"], e["percent"]) for e in tagged(world, name, rw.A_RID)] == [(rw.A_REV[name], 0)]
    for phase in ("BeforePromotion", "AfterAgentPromotion", "AfterPromotion"):
        assert len(e2e.readbacks(phase)) == 1, phase
    assert jobs_images(world) == {rw.B_JOBS_DIGEST}


# Rollback: the safety net, now to A

def test_rollback_after_a_promotion_restores_a_by_name_with_its_literal_and_audience_and_the_readback_says_so(e2e, rollback):
    assert rollback.returncode == 0, tail(rollback)
    assert rollback.prompts == []
    assert e2e.readbacks("BeforeRollback")[0]["state"] == "agent candidate, api candidate"
    restores = [c["argv"] for c in e2e.logged("gcloud") if c["argv"][:3] == ["run", "services", "update-traffic"] and c["argv"][3] in so.SERVICES][-2:]
    assert [(a[3], [x for x in a if x.startswith("--to-revisions=")]) for a in restores] == [
        (API, [f"--to-revisions={rw.A_REV[API]}=100"]), (AGENT, [f"--to-revisions={rw.A_REV[AGENT]}=100"])]
    world = e2e.world_now()
    for name in so.SERVICES:
        assert traffic(world, name) == {rw.A_REV[name]: 100}
        assert f"{name}-{e2e.rid}" in world.revision_names[name]
        assert [(e["revisionName"], e["percent"]) for e in tagged(world, name, rw.A_RID)] == [(rw.A_REV[name], 100)]
    env = {e["name"]: e.get("value") for e in world.revisions[rw.A_REV[API]]["spec"]["containers"][0]["env"]}
    assert env["AGENT_URL"] == rw.A_TAG_URL[AGENT] and env["AGENT_AUDIENCE"] == rw.CANON[AGENT]
    after = e2e.readbacks("AfterRollback")[0]
    assert all(after["residue"][n]["candidate_revision_present"] for n in so.SERVICES)
    assert after["residue"][API]["tags"] == sorted([rw.A_RID, e2e.rid])
    assert jobs_images(world) == {rw.B_JOBS_DIGEST}


# Retire: the release tag goes, A's tag stays

def test_retire_removes_only_the_release_tag_and_the_prior_tag_is_never_asked_for(e2e, retire):
    assert retire.returncode == 0, tail(retire)
    assert tag_calls(e2e) == [e2e.rid, e2e.rid]
    world = e2e.world_now()
    for name in so.SERVICES:
        assert tagged(world, name, e2e.rid) == []
        assert traffic(world, name) == {rw.A_REV[name]: 100}
        assert [(e["revisionName"], e["percent"]) for e in tagged(world, name, rw.A_RID)] == [(rw.A_REV[name], 100)]
    for phase in ("BeforeRetire", "AfterRetire"):
        assert len(e2e.readbacks(phase)) == 1, phase
    assert [event["state"] for event in e2e.json_file("tag-ledger.json")["events"]] == ["CANDIDATE", "CANDIDATE", "SERVING", "SERVING", "RETIRED", "RETIRED"]


# The rollback from a half promoted release, to A

@pytest.fixture(scope="module")
def half(tmp_path_factory):
    """Candidate completes, then the API's promotion fails after the agent was promoted: the state the rollback is for."""
    with pytest.MonkeyPatch.context() as patch:
        world = E2E(tmp_path_factory.mktemp("e2e-after-a-half"), patch, after="A")
        need(world.run("Candidate"))
        world.set_world(fail_traffic_to=f"{API}-{world.rid}")
        promote_run = world.run("Promote")
        early_retire = world.run("Retire")
        rollback_run = world.run("Rollback")
        retire_run = world.run("Retire")
        yield world, promote_run, early_retire, rollback_run, retire_run


def test_a_half_promoted_release_is_rolled_back_to_a_and_retired_without_touching_a_tag(half):
    e2e, promote_run, early_retire, rollback_run, retire_run = half
    assert promote_run.returncode != 0 and "promote-f42-api exited" in promote_run.stderr.replace("\n", " ")
    assert early_retire.returncode != 0 and "Retire is refused" in early_retire.stderr.replace("\n", " ")
    assert rollback_run.returncode == 0, tail(rollback_run)
    assert e2e.readbacks("BeforeRollback")[0]["state"] == "agent candidate, api prior"
    assert retire_run.returncode == 0, tail(retire_run)
    world = e2e.world_now()
    for name in so.SERVICES:
        assert tagged(world, name, e2e.rid) == [] and traffic(world, name) == {rw.A_REV[name]: 100}
        assert [(e["revisionName"], e["percent"]) for e in tagged(world, name, rw.A_RID)] == [(rw.A_REV[name], 100)]
    assert tag_calls(e2e) == [e2e.rid, e2e.rid]


# The edges: a prior release that is not what the record says is refused before any write

def test_a_candidate_started_after_the_prior_tag_was_removed_by_someone_stops_before_the_first_write(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        world = E2E(tmp_path_factory.mktemp("e2e-after-a-tag-gone"), patch, after="A")
        data = json.loads(world.state.read_text(encoding="utf-8"))
        for name in so.SERVICES:
            data["svc"][name]["traffic"] = [{k: v for k, v in e.items() if k != "tag"} for e in data["svc"][name]["traffic"]]
        world.state.write_text(json.dumps(data), encoding="utf-8")
        run = world.run("Candidate")
        assert run.returncode != 0 and "helper-BeforeAnyWrite-0 exited 1" in run.stderr.replace("\n", " ")
        assert (run.run_dir / "helper-BeforeAnyWrite-0.log").read_text(encoding="utf-8").startswith("STOP: TAG_MAPPING:")
        assert sorted(p.name for p in run.run_dir.iterdir() if p.suffix == ".log") == ["helper-BeforeAnyWrite-0.log"]
        assert gcloud_writes(world) == []
