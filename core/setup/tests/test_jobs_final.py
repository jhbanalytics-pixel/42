"""Release B end to end, offline (the assembled branch): the packet files come from the packet command line, JobsCandidate, JobsUpdate and
JobsRollback are the real JOBS-PASTE.ps1 run as pwsh processes, and every command the paste starts is played by the real python program
over a fake world: deploy_jobs.py for the build, bound_readback.py for FreezeJobs and the readbacks, durable_effects_check.py for the
validate and the schema readbacks, jobs_run.py for the snapshot, the 14 updates and the rollback. pwsh is required; a missing pwsh fails.

These tests are in the deploy partition (release_test_ids.json). They run on Windows only, as Release A's end to end test does, because
the stand-ins for py and gcloud are .cmd files, which pwsh starts on Windows only."""
import contextlib
import datetime as dt
import io
import json
import re
import shutil

import pytest

from core.setup.release import chain_evidence as ce
from core.setup.release import jobs_only as jo
from core.setup.release import plan
from core.setup.release import services_only as so
from core.setup.tests import jobs_world as jw
from core.setup.tests.e2e_world import ON_WINDOWS
from core.setup.tests.jobs_e2e_world import EndToEnd, derive_prior, release_b_live, release_d

pytestmark = pytest.mark.skipif(not ON_WINDOWS, reason="the stand-ins for py and gcloud are .cmd files, which exist on Windows only")


@pytest.fixture(autouse=True)
def pwsh_is_present():
    if shutil.which("pwsh") is None:
        pytest.fail("pwsh is not installed; the end to end tests cannot run (install PowerShell 7)")


FORBIDDEN_WORDS = ("update-traffic", "scheduler", "set-iam-policy", "--set-secrets", "--update-env-vars", "--remove-env-vars", "execute", "delete")


def forbidden_commands(commands):
    """The commands that hold a forbidden word in any argument, whether the word is the whole argument or only part of it (a flag with its value attached)."""
    return [argv for argv in commands if any(word in token for token in argv for word in FORBIDDEN_WORDS)]


def test_the_forbidden_word_check_sees_a_word_inside_an_argument_as_well_as_a_whole_argument():
    assert forbidden_commands([["gcloud", "run", "jobs", "update", "f42-x", "--update-env-vars=A=b"]])
    assert forbidden_commands([["gcloud", "run", "jobs", "update", "f42-x", "--update-env-vars", "A=b"]])
    assert forbidden_commands([["gcloud", "run", "jobs", "update", "f42-x", "--image=img@sha256:ab", "--remove-env-vars=A"]])
    assert not forbidden_commands([["gcloud", "run", "jobs", "update", "f42-x", "--image=img@sha256:ab", "--region=europe-west1"]])


def ok(result):
    assert result.returncode == 0, (result.stderr, result.stdout)
    return result


# What a test may put in the packet folder before the paste runs (everything else there comes from a tool). The release manifest, the schema
# receipt, the readbacks and the run folders are written by the paste and its helpers, so none of them may exist before the action that
# writes it. SEAMS-B.md lists the writer and the reader of each.
MADE_BY_TOOLS = {"producer-bindings.json", "live-bindings.json", "independent-review.json", "execution-receipt.json",
                 "durable-effects.json", "apply-dry-run-receipt.txt"}
WRITTEN_BY_THE_RELEASE = ("release-manifest.json", "release-manifest.sha256", "schema-readback-receipt.json", "readbacks", "runs")


def packet_files(run):
    return {path.name for path in run.bench.packet.iterdir()}


def test_nothing_the_paste_or_a_helper_writes_exists_before_its_action_and_everything_a_tool_makes_does(tmp_path, monkeypatch):
    run = EndToEnd(tmp_path, monkeypatch).make_packet()
    names = packet_files(run)
    assert not [name for name in WRITTEN_BY_THE_RELEASE if name in names], names
    assert {n for n in names if n.startswith("chain-evidence-")} == {f"chain-evidence-{jw.RUN_DATE.isoformat()}.json"}
    assert MADE_BY_TOOLS <= names, MADE_BY_TOOLS - names
    assert run.tools_run == ["chain_evidence", "core.schema.apply"]
    assert [name for name, _ in run.cli_calls] == ["capture-baseline-j", "bind", "bind", "lock", "review", "receipt"]
    # the dry run log is what core.schema.apply printed, written as `| Set-Content -Encoding utf8` writes it
    log = run.bench.dry_run.read_bytes()
    assert b"\r\n" in log and log.decode("utf-8").splitlines()[-1] == "dry run only; nothing created"


def test_fixture_installer_source_requires_the_actual_clean_bound_commit(tmp_path, monkeypatch):
    from core.setup.tests.e2e_shim import fixture_source

    run = EndToEnd(tmp_path, monkeypatch).make_packet()
    bound = run.bench.bindings_value()
    monkeypatch.chdir(run.bench.repo)
    assert dict(fixture_source(bound))["core/schema/agent.sql"] == (run.bench.repo / "core/schema/agent.sql").read_text(encoding="utf-8")
    for field in ("target", "tree"):
        with pytest.raises(so.Stop) as stopped:
            fixture_source({**bound, field: "0" * 40})
        assert stopped.value.code == "SOURCE"
    (run.bench.repo / "unreviewed.txt").write_text("synthetic dirty source", encoding="utf-8")
    with pytest.raises(so.Stop) as stopped:
        fixture_source(bound)
    assert stopped.value.code == "SOURCE"


def test_the_whole_release_candidate_update_and_rollback_runs_through_the_real_paste_to_each_readback(tmp_path, monkeypatch):
    run = EndToEnd(tmp_path, monkeypatch).make_packet()
    bench = run.bench
    before = run.job_images()
    assert set(before.values()) == {jw.ROLLBACK_DIGEST}

    # JobsCandidate: build, freeze, validate, snapshot, schema apply, readbacks, receipt
    candidate = ok(run.paste("JobsCandidate"))
    assert candidate.played == ["gcloud config list", "bound_readback.py BeforeAnyWrite", "deploy_jobs.py --build", "bound_readback.py FreezeJobs",
                                "durable_effects_check.py validate", "jobs_run.py snapshot", "jobs_run.py warehouse-install",
                                "durable_effects_check.py readbacks", "jobs_run.py schema-receipt"]
    state = run.state()
    assert state["schema_applied"] is True
    folder = run.run_folder("JobsCandidate")
    assert sorted(path.name for path in folder.glob("*.exit.txt")) == sorted(
        f"{name}.exit.txt" for name in ("helper-BeforeAnyWrite-0", "jobs-build", "helper-FreezeJobs-0", "durable-validate", "snapshot", "warehouse-install",
                                        "durable-readbacks", "schema-receipt"))
    assert all(path.read_text(encoding="utf-8-sig").strip() == "0" for path in folder.glob("*.exit.txt"))
    assert re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\r?\n?", (folder / "build-id.txt").read_text(encoding="utf-8"))
    assert (folder / "quiet-snapshot-01.json").is_file() and (folder / "readbacks.json").is_file()
    manifest = run.read("release-manifest.json")
    assert manifest["image"]["digest"] == jw.NEW_DIGEST == manifest["image"]["registry_digest"] and manifest["source"]["commit"] == bench.target
    receipt = run.read("schema-readback-receipt.json")
    assert receipt["receipt_sha256"] == bench.bindings_value()["schemaReadbackReceiptSha256"] and receipt["effects"]
    assert sorted(p.name for p in (bench.packet / "readbacks").glob("*.json")) == ["BeforeAnyWrite-01.json", "FreezeJobs-01.json"]
    assert run.job_images() == before, "JobsCandidate changes no job"

    # JobsUpdate: IDLE, DEPLOY, the 14 definitions, the readback
    update = ok(run.paste("JobsUpdate"))
    assert update.played == ["gcloud config list", "bound_readback.py BeforeJobsUpdate", "jobs_run.py update", "bound_readback.py AfterJobsUpdate"]
    assert [p["prompt"] for p in update.prompts] == ["Type IDLE to continue", "Type DEPLOY to continue"]
    assert set(run.job_images().values()) == {jw.NEW_DIGEST}
    log = json.loads(next((bench.packet / "runs").glob("*-JobsUpdate/jobs-update-01.json")).read_text(encoding="utf-8"))
    assert log["complete"] is True and log["updated"] == list(jo.UPDATE_ORDER) and log["stopped"] is None
    after = json.loads((bench.packet / "readbacks" / "AfterJobsUpdate-01.json").read_text(encoding="utf-8"))
    assert after["observations"]["first_b_chain"]["run_date"] == "2026-10-12"
    assert not (run.run_folder("JobsUpdate") / "update-token.json").exists(), "the token is used up and the paste leaves nothing"

    # JobsRollback: nothing typed, the blockers checker, the 14 back, the readback
    rollback = ok(run.paste("JobsRollback"))
    assert rollback.played == ["gcloud config list", "durable_effects_check.py rollback-blockers", "bound_readback.py BeforeJobsRollback",
                               "jobs_run.py rollback", "bound_readback.py AfterJobsRollback"]
    assert rollback.prompts == []
    assert run.job_images() == before
    assert json.loads((bench.packet / "readbacks" / "AfterJobsRollback-01.json").read_text(encoding="utf-8"))["observations"]["residue_probed"] is False

    # Q4: jobs only. No service byte changed, and every command the paste started is on the jobs positive list, exactly once.
    assert run.state()["cloud"].services_bytes() == run.services_before
    for result in (candidate, update, rollback):
        for argv in result.commands:
            assert len(plan.jobs_matching_entries(argv)) == 1, argv
    for result in (candidate, update, rollback):
        assert not forbidden_commands(result.commands)


def test_the_paste_orders_the_schema_apply_before_its_readbacks_and_the_receipt_and_jobs_update_needs_that_receipt(tmp_path, monkeypatch):
    run = EndToEnd(tmp_path, monkeypatch).make_packet()
    candidate = ok(run.paste("JobsCandidate"))
    played = candidate.played
    assert played.index("jobs_run.py warehouse-install") < played.index("durable_effects_check.py readbacks") < played.index("jobs_run.py schema-receipt")
    writes = run.state()["ddl_calls"]
    first_warehouse = next(i for i, (kind, _) in enumerate(writes) if kind == "warehouse")
    assert first_warehouse > 0 and all(kind == "schema" for kind, _ in writes[:first_warehouse])
    assert all(kind == "warehouse" for kind, _ in writes[first_warehouse:])
    durable = json.loads(run.bench.durable.read_text(encoding="utf-8"))
    assert {e["statement_ref"] for e in durable["effects"] if e["apply_kind"] == "schema"} <= {sql for _, sql in writes[:first_warehouse]}
    assert [sql for _, sql in writes[first_warehouse:]] == [e["statement_ref"] for e in durable["effects"] if e["apply_kind"] == "warehouse_install"]
    (run.bench.packet / "schema-readback-receipt.json").unlink()
    update = run.paste("JobsUpdate")
    assert update.returncode != 0 and update.played == ["gcloud config list", "bound_readback.py BeforeJobsUpdate"]
    assert set(run.job_images().values()) == {jw.ROLLBACK_DIGEST}


def test_a_schema_apply_that_leaves_the_columns_missing_stops_at_the_readbacks_and_leaves_no_receipt(tmp_path, monkeypatch):
    run = EndToEnd(tmp_path, monkeypatch, apply_noop=True).make_packet()
    candidate = run.paste("JobsCandidate")
    assert candidate.returncode != 0 and candidate.played[-1] == "jobs_run.py warehouse-install" and "jobs_run.py schema-receipt" not in candidate.played
    assert "stamp" not in run.state()["live"]["columns"]["ogilvy-trends-v2.intelligence_42_agent.runs"]
    assert "ogilvy-trends-v2.intelligence_42_core.item_locality" not in run.state()["live"]["tables"]
    assert not any(kind == "warehouse" for kind, _ in run.state()["ddl_calls"])
    assert not (run.bench.packet / "schema-readback-receipt.json").exists()
    update = run.paste("JobsUpdate")
    assert update.returncode != 0 and set(run.job_images().values()) == {jw.ROLLBACK_DIGEST}


def test_a_registry_that_disagrees_with_the_build_result_stops_at_freezejobs_before_the_validator_and_the_schema_apply(tmp_path, monkeypatch):
    run = EndToEnd(tmp_path, monkeypatch, registry_digest="sha256:" + "ab" * 32).make_packet()
    candidate = run.paste("JobsCandidate")
    assert candidate.returncode != 0 and candidate.played == ["gcloud config list", "bound_readback.py BeforeAnyWrite", "deploy_jobs.py --build",
                                                              "bound_readback.py FreezeJobs"]
    assert run.state()["schema_applied"] is False and not (run.bench.packet / "release-manifest.json").exists()


def test_a_dry_run_receipt_changed_after_the_lock_stops_the_paste_before_the_build(tmp_path, monkeypatch):
    run = EndToEnd(tmp_path, monkeypatch).make_packet()
    path = run.bench.dry_run
    path.write_bytes(path.read_bytes() + b" ")
    candidate = run.paste("JobsCandidate")
    assert candidate.returncode != 0 and candidate.played == [] and "a bound file differs from the lock: dry_run_receipt" in candidate.stderr + candidate.stdout


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


def test_a_rollback_whose_candidate_readback_has_no_usable_hour_still_restores_every_job_through_the_real_paste(tmp_path, monkeypatch):
    # Finding 1: the rollback is the only way back, so it logs an unknown hour and goes on.
    run = EndToEnd(tmp_path, monkeypatch).make_packet()
    ok(run.paste("JobsCandidate"))
    ok(run.paste("JobsUpdate"))
    path = run.bench.packet / "readbacks" / "BeforeAnyWrite-01.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    del value["at_utc"]
    path.write_text(json.dumps(value), encoding="utf-8")
    rollback = ok(run.paste("JobsRollback"))
    assert rollback.played[-2:] == ["jobs_run.py rollback", "bound_readback.py AfterJobsRollback"]
    assert set(run.job_images().values()) == {jw.ROLLBACK_DIGEST}
    log = json.loads(next((run.bench.packet / "runs").glob("*-JobsRollback/jobs-rollback-01.json")).read_text(encoding="utf-8"))
    assert log["past_rollback_deadline"] is None and log["complete"] is True


def test_the_first_b_chain_read_the_lead_runs_next_morning_works_from_the_live_bindings_and_the_manifest_the_paste_froze(tmp_path, monkeypatch):
    # Step 4 of the packet: chain_evidence.py --expect first-b on the bindings the packet command wrote and the release manifest FreezeJobs wrote.
    run = EndToEnd(tmp_path, monkeypatch).make_packet()
    ok(run.paste("JobsCandidate"))
    ok(run.paste("JobsUpdate"))
    due = json.loads((run.bench.packet / "readbacks" / "AfterJobsUpdate-01.json").read_text(encoding="utf-8"))["observations"]["first_b_chain"]
    day = dt.date.fromisoformat(due["run_date"])
    chain = jw.ChainFixture(tmp_path / "first-b-world", digest=jw.NEW_DIGEST, role="first-b", day=day)
    jw.set_all_jobs_image(chain.world, jw.NEW_DIGEST)
    evidence = tmp_path / "step4-run"
    argv = ["--date", day.isoformat(), "--bindings", str(run.bench.bindings), "--evidence", str(evidence), "--expect", "first-b"]
    moment = dt.datetime.fromisoformat(due["read_not_before_utc"])
    early = io.StringIO()
    with contextlib.redirect_stderr(early):
        assert ce.main(argv, reader_factory=lambda t: chain.reader(), bq_factory=lambda b: chain.bq(), now=lambda: moment - dt.timedelta(seconds=1)) == 1
    assert "TOO_EARLY" in early.getvalue()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = ce.main(argv, reader_factory=lambda t: chain.reader(), bq_factory=lambda b: chain.bq(), now=lambda: moment)
    assert code == 0, err.getvalue()
    written = json.loads((run.bench.packet / f"chain-evidence-{day.isoformat()}.json").read_text(encoding="utf-8"))
    assert written["role"] == "first-b" and written["jobs_image"]["expected_digest"] == jw.NEW_DIGEST and written["verdict"]["qualifies"] is True


# Release D on top of the live Release B: the same tools, the same real paste, and the prior release is Release B's own packet.

def image_args(argvs):
    """The image reference of every `jobs update` among the gcloud commands the fake Cloud Run played, by job."""
    return {argv[4]: next(a.split("=", 1)[1] if a.startswith("--image=") else argv[i + 1] for i, a in enumerate(argv) if a.startswith("--image"))
            for argv in argvs}


def test_release_d_binds_to_release_b_as_its_prior_updates_the_fourteen_jobs_and_rolls_back_to_b_exactly(tmp_path, monkeypatch):
    run_b = release_b_live(tmp_path, monkeypatch)
    assert set(run_b.job_images().values()) == {jw.B_LIVE_DIGEST}
    left = {job: so.fingerprint(so.job_view(view)) for job, view in run_b.state()["cloud"].world.jobs.items()}

    # the record is derived from Release B's own lock and packet, against the digest the lead names
    prior_file = tmp_path / "prior-jobs-release.json"
    record = derive_prior(run_b, prior_file)
    assert record["image_digest"] == jw.B_LIVE_DIGEST and record["release_id"] == run_b.bench.release_id
    assert record["job_definitions"] == left, "the fourteen definitions are the ones B's update left, as its JobsUpdate log recorded them"
    assert record["terminal"] == {"kind": "AfterJobsUpdate", "at_utc": run_b.read("readbacks/AfterJobsUpdate-01.json")["at_utc"]}

    # Release D's packet: every file from a tool, bound to that record
    run = release_d(tmp_path, monkeypatch, run_b, prior_file)
    bench = run.bench
    assert bench.release_id != run_b.bench.release_id and bench.letter == "D"
    assert [name for name, _ in run.cli_calls] == ["capture-baseline-j", "bind", "bind", "lock", "review", "receipt"]
    baseline = bench.baseline_value()
    assert baseline["rollbackJobsDigest"] == jw.B_LIVE_DIGEST and baseline["priorJobsRelease"] == record
    bindings = bench.bindings_value()
    assert bindings["rollbackJobsDigest"] == jw.B_LIVE_DIGEST and bindings["priorJobsRelease"] == record and bindings["releaseLetter"] == "D"
    chain = json.loads(bench.chain_path.read_text(encoding="utf-8"))
    assert chain["run_date"] == "2026-10-12" and chain["a_terminal"] == record["terminal"] and chain["jobs_image"]["expected_digest"] == jw.B_LIVE_DIGEST
    assert bench.line.read_text(encoding="utf-8").startswith(f"RELEASE D {bench.release_id}: ")
    assert bench.quiet_line_file.read_text(encoding="utf-8").startswith(f"RELEASE D {bench.release_id} QUIET: ")
    assert set(run.job_images().values()) == {jw.B_LIVE_DIGEST}

    # JobsCandidate and JobsUpdate: D's image is built and frozen, and the fourteen jobs move from B's image to it
    candidate = ok(run.paste("JobsCandidate"))
    assert candidate.played[:3] == ["gcloud config list", "bound_readback.py BeforeAnyWrite", "deploy_jobs.py --build"]
    assert run.read("release-manifest.json")["image"]["digest"] == jw.D_DIGEST
    assert set(run.job_images().values()) == {jw.B_LIVE_DIGEST}, "JobsCandidate changes no job"
    update = ok(run.paste("JobsUpdate"))
    assert update.played == ["gcloud config list", "bound_readback.py BeforeJobsUpdate", "jobs_run.py update", "bound_readback.py AfterJobsUpdate"]
    assert set(run.job_images().values()) == {jw.D_DIGEST}
    updates = run.state()["cloud"].update_argvs()
    assert len(updates) == 14 and set(image_args(updates).values()) == {f"{jo.JOBS_REPO}@{jw.D_DIGEST}"} and len(image_args(updates)) == 14

    # JobsRollback restores Release B's digest and Release B's definitions, and nothing of the release before it
    rollback = ok(run.paste("JobsRollback"))
    assert rollback.played[-2:] == ["jobs_run.py rollback", "bound_readback.py AfterJobsRollback"]
    assert run.job_images() == {job: jw.B_LIVE_DIGEST for job in so.JOB_NAMES}
    restores = run.state()["cloud"].update_argvs()[14:]
    assert len(restores) == 14 and set(image_args(restores).values()) == {f"{jo.JOBS_REPO}@{jw.B_LIVE_DIGEST}"} and len(image_args(restores)) == 14
    assert jw.ROLLBACK_DIGEST not in json.dumps(run.state()["cloud"].update_argvs())
    restored = {job: so.fingerprint(so.job_view(view)) for job, view in run.state()["cloud"].world.jobs.items()}
    assert restored == record["job_definitions"]
    after = json.loads((bench.packet / "readbacks" / "AfterJobsRollback-01.json").read_text(encoding="utf-8"))
    assert after["blocking"] == {} and after["observations"]["residue_probed"] is False

    # jobs only: no service byte changed, every command is on the jobs positive list, none holds a forbidden word
    assert run.state()["cloud"].services_bytes() == run.services_before
    for result in (candidate, update, rollback):
        assert not forbidden_commands(result.commands)
        for argv in result.commands:
            assert len(plan.jobs_matching_entries(argv)) == 1, argv


def test_release_d_is_refused_by_the_paste_when_the_jobs_are_not_the_ones_its_prior_left(tmp_path, monkeypatch):
    # A job changed after Release B and before Release D's baseline was captured is not the definition the prior release left. JobsCandidate
    # stops before the build, because the baseline D is bound to would restore something B never ran.
    run_b = release_b_live(tmp_path, monkeypatch)
    prior_file = tmp_path / "prior-jobs-release.json"
    derive_prior(run_b, prior_file)
    run = release_d(tmp_path, monkeypatch, run_b, prior_file)
    state = run.state()
    container = state["cloud"].world.jobs["f42-digest"]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
    container["args"] = ["-m", "elsewhere"]
    run.save(state)
    candidate = run.paste("JobsCandidate")
    assert candidate.returncode != 0 and "deploy_jobs.py --build" not in candidate.played
    assert set(run.job_images().values()) == {jw.B_LIVE_DIGEST}
