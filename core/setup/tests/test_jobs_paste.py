"""The jobs release paste (W8-REL-B v2.1 3.10, JU-02 over the paste world, JU-08, JU-09, JR-01's retry rule), run as a pwsh process with
the real paste text and doubled commands. No external command runs. A missing pwsh fails the tests: it is never a reason to skip them."""
import json
import shutil
from pathlib import Path

import pytest

from core.setup.release import plan
from core.setup.tests.jobs_paste_world import JobsPasteWorld

ACTIONS = ("JobsCandidate", "JobsUpdate", "JobsRollback")


@pytest.fixture(autouse=True)
def pwsh_is_present():
    if shutil.which("pwsh") is None:
        pytest.fail("pwsh is not installed; the paste tests cannot run (install PowerShell 7)")


def no_external_call(result):
    return result.external == []


# the packet checks, before the first external call

def test_ju08_the_unchanged_packet_runs_each_action_and_the_first_external_call_is_a_read(tmp_path):
    for action in ACTIONS:
        result = JobsPasteWorld(tmp_path / action, action).run()
        assert result.returncode == 0, (action, result.stderr)
        assert result.external[0]["kind"] == "read"


@pytest.mark.parametrize("name", ["JOBS-PASTE.ps1", "jobs_run.py", "chain_evidence.py", "jobs_only.py", "bound_readback.py", "plan.py"])
def test_ju08_flipping_one_byte_in_a_locked_file_stops_the_paste_before_the_first_external_call(tmp_path, name):
    world = JobsPasteWorld(tmp_path)
    target = world.repo / "core/setup/release" / name
    target.write_bytes(target.read_bytes() + b" ")
    result = world.run()
    assert result.returncode != 0 and no_external_call(result) and "NOT EXECUTABLE" in result.stderr + result.stdout


def test_ju08_a_bound_file_that_changed_after_the_lock_stops_the_paste_before_the_first_external_call(tmp_path):
    for attr in ("baseline", "durable", "chain", "dry_run"):
        world = JobsPasteWorld(tmp_path / attr)
        path = getattr(world, attr)
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        result = world.run()
        assert result.returncode != 0 and no_external_call(result), attr


def test_ju08_a_review_that_does_not_bind_this_lock_or_a_bindings_file_for_another_mode_stops_before_any_call(tmp_path):
    world = JobsPasteWorld(tmp_path / "review")
    world.write(world.review, {"schema_version": 1, "verdict": "ACCEPT", "target": "0" * 40, "lock_sha256": "0" * 64})
    assert world.run().returncode != 0
    world2 = JobsPasteWorld(tmp_path / "mode", bindings={"mode": "services-only"})
    result = world2.run()
    assert result.returncode != 0 and no_external_call(result)


# JU-08: the typed words

def test_ju08_jobs_candidate_asks_deploy_once_after_the_checks_and_before_any_command(tmp_path):
    result = JobsPasteWorld(tmp_path, "JobsCandidate").run()
    assert result.returncode == 0, result.stderr
    assert [p["prompt"] for p in result.prompts] == ["Type DEPLOY to continue"]
    assert result.prompts[0]["runs_so_far"] == 0 and result.prompts[0]["run_dir_existed"] is False
    reads = [c["argv"] for c in result.calls[:result.calls.index(result.prompts[0])] if c["kind"] == "read"]
    assert ["git", "rev-parse", "HEAD"] in reads and any(r[:3] == ["gcloud", "config", "list"] for r in reads)


def test_ju08_jobs_update_asks_idle_then_deploy_after_the_before_readback_and_before_the_runner(tmp_path):
    result = JobsPasteWorld(tmp_path, "JobsUpdate").run()
    assert result.returncode == 0, result.stderr
    assert [p["prompt"] for p in result.prompts] == ["Type IDLE to continue", "Type DEPLOY to continue"]
    assert all(p["runs_so_far"] == 1 for p in result.prompts)
    assert result.names == ["helper-BeforeJobsUpdate-0", "jobs-run-update", "helper-AfterJobsUpdate-0"]
    assert result.calls.index(result.prompts[1]) < result.calls.index(result.run("jobs-run-update"))


def test_ju08_jobs_rollback_asks_nothing_and_does_not_need_a_console(tmp_path):
    for interactive in (True, False):
        result = JobsPasteWorld(tmp_path / str(interactive), "JobsRollback").run(interactive=interactive)
        assert result.returncode == 0, result.stderr
        assert result.prompts == []
        assert result.names == ["helper-BeforeJobsRollback-0", "jobs-run-rollback", "helper-AfterJobsRollback-0"]


@pytest.mark.parametrize("action", ["JobsCandidate", "JobsUpdate"])
def test_ju08_redirected_input_stops_not_interactive_before_any_read(tmp_path, action):
    result = JobsPasteWorld(tmp_path, action).run(interactive=False)
    assert result.returncode != 0 and "NOT INTERACTIVE" in result.stderr + result.stdout
    assert no_external_call(result) and result.prompts == []


@pytest.mark.parametrize("typed", ["deploy", "Deploy", "DEPLOY ", "yes", ""])
def test_ju08_a_candidate_word_in_the_wrong_case_or_form_stops_not_confirmed_with_nothing_written(tmp_path, typed):
    result = JobsPasteWorld(tmp_path, "JobsCandidate").run(words={"DEPLOY": typed})
    assert result.returncode != 0 and "NOT CONFIRMED" in result.stderr + result.stdout
    assert result.runs == []


@pytest.mark.parametrize("words,prompts_seen", [({"IDLE": "idle"}, 1), ({"IDLE": "IDLE", "DEPLOY": "deploy"}, 2), ({"IDLE": "IDLE", "DEPLOY": "DEPLOY "}, 2)])
def test_ju08_a_wrong_idle_or_deploy_stops_not_confirmed_before_the_runner(tmp_path, words, prompts_seen):
    result = JobsPasteWorld(tmp_path, "JobsUpdate").run(words=words)
    assert result.returncode != 0 and "NOT CONFIRMED" in result.stderr + result.stdout
    assert len(result.prompts) == prompts_seen and "jobs-run-update" not in result.names and "helper-AfterJobsUpdate-0" not in result.names


def test_ju08_there_is_no_passcode_parameter_or_prompt_in_the_jobs_actions(tmp_path):
    text = (Path(__file__).resolve().parents[3] / "core/setup/release/JOBS-PASTE.ps1").read_text(encoding="utf-8").lower()
    for word in ("passcode", "securestring", "smoke", "f42_smoke"):
        assert word not in text, word
    for action in ACTIONS:
        result = JobsPasteWorld(tmp_path / action, action).run()
        assert all("secure" not in p for p in result.prompts) and all("asscode" not in p["prompt"] for p in result.prompts)


def test_ju08_a_quiet_snapshot_older_than_15_minutes_when_deploy_is_typed_stops_before_the_runner(tmp_path):
    fresh = JobsPasteWorld(tmp_path / "fresh", "JobsUpdate").run(snapshot_age=14.0)
    assert fresh.returncode == 0, fresh.stderr
    stale = JobsPasteWorld(tmp_path / "stale", "JobsUpdate").run(snapshot_age=16.0)
    assert stale.returncode != 0 and "older than 15 minutes" in stale.stderr + stale.stdout
    assert "jobs-run-update" not in stale.names
    slow = JobsPasteWorld(tmp_path / "slow", "JobsUpdate").run(snapshot_age=1.0, minutes_per_prompt=8.0)
    assert slow.returncode != 0 and "jobs-run-update" not in slow.names


def test_ju08_an_alias_shadowing_a_paste_function_or_the_prompt_cmdlet_is_refused_before_any_call(tmp_path):
    for alias in ("Read-Word", "Get-UtcNow", "Test-Interactive", "Read-Host"):
        result = JobsPasteWorld(tmp_path / alias, "JobsUpdate").run(aliases=[{"name": alias, "value": "Write-Output"}])
        assert result.returncode != 0 and no_external_call(result), alias


# JU-09: the receipt

@pytest.mark.parametrize("receipt_change", [
    {"schema_version": 2},
    {"schema_version": "1"},
    {"release_id": "rel-0000000-01"},
    {"mode": "services-only"},
    {"mode": None},
    {"target": "0" * 40},
    {"declared_steps": ["JobsCandidate", "JobsRollback"]},
    {"declared_steps": ["JobsUpdate", "Candidate"]},
    {"declared_steps": "JobsUpdate"},
    {"later_explicit_user_instruction": "true"},
    {"quiet_window_confirmed": False},
    {"no_new_manual_starts_until_execution_ends": 1},
], ids=["version", "version_text", "other_release", "other_mode", "no_mode", "other_target", "action_not_declared", "services_step",
        "steps_not_a_list", "text_true", "false", "one"])
def test_ju09_a_receipt_that_is_not_for_this_action_release_and_mode_is_refused_before_any_external_call(tmp_path, receipt_change):
    world = JobsPasteWorld(tmp_path, "JobsUpdate", receipt=receipt_change)
    result = world.run()
    assert result.returncode != 0 and no_external_call(result) and "NOT EXECUTABLE" in result.stderr + result.stdout


@pytest.mark.parametrize("missing", ["schema_version", "release_id", "mode", "declared_steps", "later_explicit_user_instruction"])
def test_ju09_a_receipt_without_a_required_field_is_refused_before_any_external_call(tmp_path, missing):
    world = JobsPasteWorld(tmp_path, "JobsUpdate")
    receipt = json.loads(world.receipt.read_text(encoding="utf-8"))
    receipt.pop(missing)
    world.write(world.receipt, receipt)
    result = world.run()
    assert result.returncode != 0 and no_external_call(result)


def test_ju09_a_receipt_that_declares_only_the_action_being_run_is_enough(tmp_path):
    for action in ACTIONS:
        assert JobsPasteWorld(tmp_path / action, action, declared_steps=[action]).run().returncode == 0


# The retry rule of the helper (JR-01's exit 3 half) lives in the paste, so its nodes are here, in the partition that runs the paste

def test_ju08_a_readback_that_could_not_complete_is_retried_three_times_ten_seconds_apart(tmp_path):
    result = JobsPasteWorld(tmp_path, "JobsRollback").run(exits={"helper-AfterJobsRollback-0": 3, "helper-AfterJobsRollback-1": 3, "helper-AfterJobsRollback-2": 3})
    assert result.returncode == 0, result.stderr
    assert [n for n in result.names if n.startswith("helper-AfterJobsRollback")] == [f"helper-AfterJobsRollback-{i}" for i in range(4)]
    assert [c["seconds"] for c in result.calls if c["kind"] == "sleep"] == [10, 10, 10]


def test_ju08_after_three_retries_the_action_stops_and_a_stop_is_never_retried(tmp_path):
    exits = {f"helper-BeforeJobsRollback-{i}": 3 for i in range(5)}
    result = JobsPasteWorld(tmp_path / "exhausted", "JobsRollback").run(exits=exits)
    assert result.returncode != 0 and "could not complete after 3 retries" in result.stderr + result.stdout
    assert result.names == [f"helper-BeforeJobsRollback-{i}" for i in range(4)]
    stop = JobsPasteWorld(tmp_path / "stop", "JobsRollback").run(exits={"helper-BeforeJobsRollback-0": 1})
    assert stop.returncode != 0 and stop.names == ["helper-BeforeJobsRollback-0"] and [c for c in stop.calls if c["kind"] == "sleep"] == []


def test_ju08_the_helper_is_asked_for_mode_jobs_and_the_bound_bindings_and_the_run_folder(tmp_path):
    world = JobsPasteWorld(tmp_path, "JobsUpdate")
    result = world.run()
    argv = result.run("helper-BeforeJobsUpdate-0")["argv"]
    assert argv[:2] == ["py", "-3.13"] and argv[argv.index("--mode") + 1] == "jobs" and argv[argv.index("--bindings") + 1] == str(world.bindings)
    assert argv[argv.index("--evidence") + 1].startswith(str(world.release_dir / "runs"))


# JobsCandidate

def test_ju08_jobs_candidate_runs_the_checks_the_build_seam_freeze_validate_snapshot_schema_apply_and_readbacks_in_order(tmp_path):
    result = JobsPasteWorld(tmp_path, "JobsCandidate").run()
    assert result.returncode == 0, result.stderr
    assert result.names == ["helper-BeforeAnyWrite-0", "archive", "extract", "helper-FreezeJobs-0", "durable-validate", "snapshot", "schema-apply",
                            "durable-readbacks"]
    assert [c for c in result.calls if c["kind"] == "build"]
    assert result.names.index("snapshot") < result.names.index("schema-apply")


def test_ju08_without_the_build_requirements_jobs_candidate_stops_at_the_seam_and_writes_nothing_after_it(tmp_path):
    result = JobsPasteWorld(tmp_path, "JobsCandidate").run(build_double=False)
    assert result.returncode != 0 and "NOT BUILT" in result.stderr + result.stdout
    assert result.names == ["helper-BeforeAnyWrite-0", "archive", "extract"]


def test_ju08_a_schema_apply_runs_only_when_the_durable_manifest_declares_a_schema_effect(tmp_path):
    world = JobsPasteWorld(tmp_path, "JobsCandidate")
    world.write(world.durable, {"schema_version": 1, "effects": [{"apply_kind": "job_image"}]})
    world.relock()
    assert "schema-apply" not in world.run().names


def test_ju08_an_effect_with_an_apply_kind_outside_the_pastes_list_stops_the_candidate_before_the_schema_apply(tmp_path):
    world = JobsPasteWorld(tmp_path, "JobsCandidate")
    world.write(world.durable, {"schema_version": 1, "effects": [{"apply_kind": "traffic_pin"}]})
    world.relock()
    result = world.run()
    assert result.returncode != 0 and "schema-apply" not in result.names


def test_ju08_the_environment_the_children_inherit_is_set_for_the_run_and_put_back_afterwards(tmp_path):
    result = JobsPasteWorld(tmp_path, "JobsUpdate").run()
    inside = result.run("helper-BeforeJobsUpdate-0")["env"]
    assert inside == {"file_logging": "1", "bytecode": "1", "locks": "0"}
    end = [c for c in result.calls if c["kind"] == "env_at_end"][0]["env"]
    assert end == {"file_logging": "", "bytecode": "", "locks": ""}


def test_ju08_a_dirty_checkout_stops_candidate_and_update_but_not_a_rollback(tmp_path):
    for action, ok in (("JobsCandidate", False), ("JobsUpdate", False), ("JobsRollback", True)):
        result = JobsPasteWorld(tmp_path / action, action).run(status=" M core/x.py")
        assert (result.returncode == 0) is ok, action
        if not ok:
            assert result.runs == [] or "helper-BeforeJobsUpdate-0" not in result.names


# JU-02 over the paste world

@pytest.mark.parametrize("action", ACTIONS)
def test_ju02_every_external_call_the_jobs_paste_makes_matches_exactly_one_entry_of_the_positive_list(tmp_path, action):
    result = JobsPasteWorld(tmp_path, action).run()
    assert result.returncode == 0, result.stderr
    assert len(result.external) >= 5
    for call in result.external:
        assert len(plan.jobs_matching_entries(call["argv"])) == 1, call["argv"]


def test_ju02_a_paste_that_issued_a_job_execution_would_be_caught_by_the_same_judgement(tmp_path):
    def mutate(text):
        return text.replace("Invoke-Runner 'jobs-run-update' 'update' | Out-Null",
                            "Step -Name 'oops' -Argv @('gcloud', 'run', 'jobs', 'execute', 'f42-collect', '--project', 'ogilvy-trends-v2', '--region', 'us-central1') | Out-Null")

    world = JobsPasteWorld(tmp_path, "JobsUpdate", mutate_paste=mutate)
    world.relock()
    result = world.run()
    assert result.returncode == 0
    assert [c["argv"] for c in result.external if len(plan.jobs_matching_entries(c["argv"])) != 1] == [
        ["gcloud", "run", "jobs", "execute", "f42-collect", "--project", "ogilvy-trends-v2", "--region", "us-central1"]]


def test_ju08_a_copy_of_the_paste_that_differs_from_the_locked_one_is_refused_even_when_the_repository_is_untouched(tmp_path):
    world = JobsPasteWorld(tmp_path, "JobsUpdate")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    same = elsewhere / "JOBS-PASTE.ps1"
    same.write_bytes(world.paste.read_bytes())
    assert world.run(paste=same).returncode == 0
    changed = elsewhere / "changed.ps1"
    changed.write_bytes(world.paste.read_bytes() + b"\n# edited\n")
    result = world.run(paste=changed)
    assert result.returncode != 0 and no_external_call(result) and "this paste differs from the lock" in result.stderr + result.stdout


def test_ju08_the_checkout_is_asserted_before_every_command_of_candidate_and_update_and_only_observed_for_a_rollback(tmp_path):
    for action, first in (("JobsUpdate", "helper-BeforeJobsUpdate-0"), ("JobsCandidate", "helper-BeforeAnyWrite-0")):
        result = JobsPasteWorld(tmp_path / action, action).run(dirty_after_runs=1)
        assert result.returncode != 0 and result.names == [first], (action, result.names)
    rollback = JobsPasteWorld(tmp_path / "rollback", "JobsRollback").run(dirty_after_runs=1)
    assert rollback.returncode == 0 and rollback.names == ["helper-BeforeJobsRollback-0", "jobs-run-rollback", "helper-AfterJobsRollback-0"]
