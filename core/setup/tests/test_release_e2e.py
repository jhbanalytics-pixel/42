"""The release paste run end to end against the real readback helper (W8-REL, rulings CS-1 and CS-2 of 10 Oct 2026).

SERVICES-PASTE.ps1 runs in a pwsh process for each of the four actions, one after the other, against the real bound_readback.py,
services_only.py, smoke.py, durable_effects_check.py, deploy_candidate.sh, git, tar, bash and py. The cloud programs are the only
fakes: gcloud (e2e_gcloud.py), the network and the Google libraries (e2e_shim). No file that the paste or the helper is meant to
write is seeded, so a file the helper reads that nobody writes stops the run, as freeze-inputs.json stopped rel-7122f58-01.

These tests are in the deploy partition (release_test_ids.json). A missing pwsh fails them; nothing here skips for a missing
prerequisite. They run on Windows only, because the paste finds gcloud as gcloud.cmd and the launcher as py.exe there.
"""
import json
import re
import shutil
import subprocess

import pytest

from core.setup.release import services_only as so
from core.setup.tests import release_prereq
from core.setup.tests import release_world as rw
from core.setup.tests.e2e_world import E2E, ON_WINDOWS, ROOT

pytestmark = pytest.mark.skipif(not ON_WINDOWS, reason="the paste finds gcloud as gcloud.cmd and the launcher as py.exe, which exist on Windows only")

PACKET_FILES = ["compat-receipt.json", "execution-receipt.json", "independent-review.json", "live-bindings.json",
                "old-reader-receipt.json"]
CANDIDATE_PHASES = ("BeforeAnyWrite", "Freeze", "BeforeCandidate", "BeforeSmoke", "AfterSmoke")


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    missing = release_prereq.prerequisites(ROOT)
    if shutil.which("pwsh") is None:
        pytest.fail("pwsh is not installed; the end to end release test cannot run (install PowerShell 7)")
    assert "bash" not in missing, "Git for Windows' bash is not installed"
    with pytest.MonkeyPatch.context() as patch:
        world = E2E(tmp_path_factory.mktemp("e2e"), patch)
        world.packet_files = sorted(p.name for p in world.release_dir.iterdir())
        yield world


def tail(run):
    return f"{run.action} exited {run.returncode}\n== stdout ==\n{run.stdout[-3000:]}\n== stderr ==\n{run.stderr[-3000:]}"


def need(run):
    if run.returncode != 0:
        pytest.fail(f"the earlier action did not complete, so this one cannot run:\n{tail(run)}")
    return run


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


def traffic(world, name):
    return {e["revisionName"]: e["percent"] for e in world.svc[name]["traffic"] if e["percent"]}


def tagged(world, name, rid):
    return [e for e in world.svc[name]["traffic"] if e.get("tag") == rid]


def git_files(e2e):
    done = subprocess.run(["git", "-C", str(e2e.repo), "ls-tree", "-r", "--name-only", e2e.target], capture_output=True, encoding="utf-8")
    return sorted(done.stdout.split())


def gcloud_writes(e2e):
    writes = []
    for call in e2e.logged("gcloud"):
        argv = call["argv"]
        if argv[:2] in (["builds", "submit"], ["run", "deploy"]):
            writes.append((" ".join(argv[:2]), argv[2] if argv[0] == "run" else ""))
        elif argv[:3] == ["run", "services", "update-traffic"]:
            writes.append(("update-traffic", argv[3]))
    return writes


# Candidate: BeforeAnyWrite, build, Freeze, validate, pin, BeforeCandidate, deploy, BeforeSmoke, smoke, AfterSmoke

def test_the_release_directory_holds_only_the_packet_before_the_first_action(e2e):
    assert e2e.packet_files == PACKET_FILES


def test_candidate_runs_through_after_smoke_with_nothing_seeded(e2e, candidate):
    assert candidate.returncode == 0, tail(candidate)
    assert [p["prompt"] for p in candidate.prompts] == ["Type DEPLOY to continue", "Smoke passcode"]
    for phase in CANDIDATE_PHASES:
        assert len(e2e.readbacks(phase)) == 1, phase


def test_candidate_writes_freeze_inputs_from_the_build_it_ran_and_the_helper_checks_them_against_the_cloud(e2e, candidate):
    assert candidate.returncode == 0, tail(candidate)
    inputs = json.loads((candidate.run_dir / "freeze-inputs.json").read_text(encoding="utf-8"))
    world = e2e.world_now()
    (build_id, build), = world.builds.items()
    source = build["source"]["storageSource"]
    assert inputs["build_id"] == build_id
    assert inputs["uploaded_source"] == f"gs://{source['bucket']}/{source['object']}"
    # gcloud builds describe gives nine fractional digits and a Z, and the helper has to read that against the paste's start
    assert re.search(r"\.\d{9}Z$", build["createTime"])
    started = so.parse_utc(inputs["paste_started_utc"])
    assert candidate.started <= started <= so.parse_utc(build["createTime"])
    manifest = e2e.json_file("release-manifest.json")
    assert manifest["build"]["id"] == build_id and manifest["build"]["source_object"] == inputs["uploaded_source"]
    assert manifest["image"]["digest"] == rw.CAND_DIGEST and manifest["image"]["registry_digest"] == rw.CAND_DIGEST


def test_candidate_uploaded_the_archive_of_the_bound_commit_and_nothing_else(e2e, candidate):
    assert candidate.returncode == 0, tail(candidate)
    (uploaded,) = e2e.world_now().__dict__["uploads"].values()
    assert uploaded == git_files(e2e)


def test_candidate_manifest_sidecar_and_freeze_readback_agree_by_recomputation(e2e, candidate):
    assert candidate.returncode == 0, tail(candidate)
    manifest = e2e.json_file("release-manifest.json")
    assert so.manifest_hash(manifest) == manifest["manifest_sha256"]
    assert (e2e.release_dir / "release-manifest.sha256").read_text(encoding="utf-8").strip() == manifest["manifest_sha256"]
    assert e2e.readbacks("Freeze")[0]["manifest_sha256"] == manifest["manifest_sha256"]
    for phase in ("BeforeSmoke", "AfterSmoke"):
        assert e2e.readbacks(phase)[0]["manifest_sha256"] == manifest["manifest_sha256"], phase


def test_candidate_smoke_receipt_is_one_the_helper_accepts_and_the_smoke_asked_once_with_the_typed_passcode(e2e, candidate):
    assert candidate.returncode == 0, tail(candidate)
    after = e2e.readbacks("AfterSmoke")[0]
    assert after["smoke_receipt"]["valid"] is True and after["smoke_receipt"]["problems"] == []
    receipt = e2e.json_file("smoke-receipt.json")
    assert receipt["exit_code"] == 0 and receipt["checks_total"] == receipt["checks_passed"] == 6
    api = [c for c in e2e.logged("http") if c.get("method")]
    assert api and all(c["passcode_accepted"] or c["url"].endswith("/api/health") or c["url"].endswith("/api/today") for c in api)
    asks = [c for c in api if c["method"] == "POST" and c["url"].endswith("/api/ask")]
    assert len(asks) == 1 and asks[0]["url"].startswith(f"https://{e2e.rid}---f42-api") and asks[0]["passcode_accepted"] is True
    assert asks[0]["body"]["tier"] == "T1" and asks[0]["body"]["market"] == "ZA" and asks[0]["body"]["mode"] == "live"
    assert receipt["argv_url"] == so.tag_url(e2e.rid, rw.CANON["f42-api"])


def test_candidate_changed_the_cloud_only_by_the_declared_commands_and_left_no_traffic_on_the_candidate(e2e, candidate):
    assert candidate.returncode == 0, tail(candidate)
    assert gcloud_writes(e2e) == [("builds submit", ""), ("update-traffic", "f42-agent"), ("update-traffic", "f42-api"),
                                  ("run deploy", "f42-agent"), ("run deploy", "f42-api")]
    assert not [c for c in e2e.logged("gcloud") if c["exit"] == 97]
    world = e2e.world_now()
    for name in so.SERVICES:
        assert traffic(world, name) == {rw.A80_REV[name]: 100}
        (entry,) = tagged(world, name, e2e.rid)
        assert entry["revisionName"] == f"{name}-{e2e.rid}" and entry["percent"] == 0


def test_the_fake_was_what_ran_not_an_installed_gcloud(e2e, candidate):
    assert candidate.returncode == 0, tail(candidate)
    calls = e2e.logged("gcloud")
    assert len(calls) > 40 and {tuple(c["argv"][:2]) for c in calls} >= {("builds", "submit"), ("run", "deploy"), ("builds", "describe")}
    assert any(c["argv"][:2] == ["builds", "describe"] for c in calls)


# Promote

def test_promote_moves_the_agent_then_the_api_and_the_helper_reads_both_back(e2e, promote):
    assert promote.returncode == 0, tail(promote)
    assert [p["prompt"] for p in promote.prompts] == ["Type IDLE to continue", "Type DEPLOY to continue"]
    world = e2e.world_now()
    for name in so.SERVICES:
        assert traffic(world, name) == {f"{name}-{e2e.rid}": 100}
    for phase in ("BeforePromotion", "AfterAgentPromotion", "AfterPromotion"):
        assert len(e2e.readbacks(phase)) == 1, phase
    assert json.loads((promote.run_dir / "inflight-asks.json").read_text(encoding="utf-8"))["running"] == 0
    assert [c for c in e2e.logged("bigquery")]


# Rollback: the safety net

def test_rollback_after_a_promotion_restores_both_services_to_the_baseline_revisions_by_name(e2e, rollback):
    assert rollback.returncode == 0, tail(rollback)
    assert rollback.prompts == []
    before = e2e.readbacks("BeforeRollback")[0]
    assert before["state"] == "agent candidate, api candidate"
    world = e2e.world_now()
    for name in so.SERVICES:
        assert traffic(world, name) == {rw.A80_REV[name]: 100}
        assert f"{name}-{e2e.rid}" in world.revision_names[name]
    after = e2e.readbacks("AfterRollback")[0]
    assert all(after["residue"][n]["candidate_revision_present"] for n in so.SERVICES)


# Retire

def test_retire_removes_the_release_tag_from_both_services_and_the_ledger_records_the_life_of_the_tag(e2e, retire):
    assert retire.returncode == 0, tail(retire)
    world = e2e.world_now()
    for name in so.SERVICES:
        assert tagged(world, name, e2e.rid) == []
        assert traffic(world, name) == {rw.A80_REV[name]: 100}
    for phase in ("BeforeRetire", "AfterRetire"):
        assert len(e2e.readbacks(phase)) == 1, phase
    states = [event["state"] for event in e2e.json_file("tag-ledger.json")["events"]]
    assert states == ["CANDIDATE", "CANDIDATE", "SERVING", "SERVING", "RETIRED", "RETIRED"]


# A failed build: the stop of 10 Oct 2026 was a Freeze stop; a build that fails must leave nothing and Freeze must not run

@pytest.fixture(scope="module")
def failed_build(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        world = E2E(tmp_path_factory.mktemp("e2e-failed-build"), patch)
        world.set_world(fail_build=True)
        yield world, world.run("Candidate")


def test_a_failed_build_leaves_no_freeze_inputs_no_freeze_and_nothing_deployed(failed_build):
    e2e, run = failed_build
    assert run.returncode != 0 and "build exited" in run.stderr.replace("\n", " ")
    assert not (run.run_dir / "freeze-inputs.json").exists()
    assert [p.name for p in (e2e.release_dir / "readbacks").iterdir()] == ["BeforeAnyWrite-01.json"]
    assert not (e2e.release_dir / "release-manifest.json").exists()
    assert gcloud_writes(e2e) == [("builds submit", "")]
    world = e2e.world_now()
    assert world.builds == {} and world.registry == {}
    for name in so.SERVICES:
        assert world.svc[name]["traffic"] == [{"latestRevision": True, "percent": 100}]


# The rollback from a half promoted release, and a Retire that is asked for too early

@pytest.fixture(scope="module")
def half(tmp_path_factory):
    """Candidate completes, then the API's promotion fails after the agent was promoted: the state the rollback is for."""
    with pytest.MonkeyPatch.context() as patch:
        world = E2E(tmp_path_factory.mktemp("e2e-half"), patch)
        candidate_run = world.run("Candidate")
        need(candidate_run)
        world.set_world(fail_traffic_to=f"f42-api-{world.rid}")
        promote_run = world.run("Promote")
        early_retire = world.run("Retire")
        after_early = world.world_now()
        after_early.readbacks_seen = world.readbacks("BeforeRetire")
        rollback_run = world.run("Rollback")
        retire_run = world.run("Retire")
        yield world, promote_run, early_retire, after_early, rollback_run, retire_run


def test_a_promotion_that_fails_on_the_api_stops_with_the_agent_promoted_and_issues_nothing_more(half):
    e2e, promote_run, _, _, _, _ = half
    assert promote_run.returncode != 0 and "promote-f42-api exited" in promote_run.stderr.replace("\n", " ")
    assert len(e2e.readbacks("AfterAgentPromotion")) == 1 and e2e.readbacks("AfterPromotion") == []


def test_retire_is_refused_after_a_promotion_until_a_rollback_has_been_read_back(half):
    e2e, _, early_retire, after_early, _, _ = half
    assert early_retire.returncode != 0 and "Retire is refused" in early_retire.stderr.replace("\n", " ")
    assert after_early.readbacks_seen == []
    for name in so.SERVICES:
        assert tagged(after_early, name, e2e.rid) != []


def test_rollback_from_a_half_promoted_release_returns_both_services_to_the_baseline_revisions(half):
    e2e, _, _, _, rollback_run, _ = half
    assert rollback_run.returncode == 0, tail(rollback_run)
    assert e2e.readbacks("BeforeRollback")[0]["state"] == "agent candidate, api a80"
    assert len(e2e.readbacks("AfterRollback")) == 1


def test_retire_after_that_rollback_removes_the_tags(half):
    e2e, _, _, _, _, retire_run = half
    assert retire_run.returncode == 0, tail(retire_run)
    world = e2e.world_now()
    for name in so.SERVICES:
        assert tagged(world, name, e2e.rid) == [] and traffic(world, name) == {rw.A80_REV[name]: 100}
