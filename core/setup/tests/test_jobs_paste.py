"""The jobs release paste (W8-REL-B v2.1 3.10, JU-02 over the paste world, JU-08, JU-09, JR-01's retry rule), run as a pwsh process with
the real paste text and doubled commands. No external command runs. A missing pwsh fails the tests: it is never a reason to skip them."""
import json
import re
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
                            "durable-readbacks", "schema-receipt"]
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


# RB-T1 (F1): the typed words are Albert's. The paste resolves every command name it invokes, taken from its own syntax tree, and
# refuses a session in which one is an alias, a function of the caller, a lookup hook's answer or a cmdlet from elsewhere. The
# attacks are the reviewer's B1 to B7j and R2, run against a valid packet.

ROOT = Path(__file__).resolve().parents[3]
PASTE = ROOT / "core/setup/release/JOBS-PASTE.ps1"
SERVICES_PASTE = ROOT / "core/setup/release/SERVICES-PASTE.ps1"
GUARD_REFUSED = "RESULT:NOT EXECUTABLE: the command '"
GUARD_TEXT = "does not resolve to a function of this paste"

FAKES = r"""
function global:FakeTyped { param([string]$Prompt, [switch]$Secure) if ($Secure) { return (ConvertTo-SecureString 'x' -AsPlainText -Force) }; return [regex]::Match($Prompt, 'Type (\w+)').Groups[1].Value }
function global:FakeTrue { $true }
function global:Nothing { }
"""
TWO = "Set-Alias -Scope Global -Name Read-Typed -Value FakeTyped; Set-Alias -Scope Global -Name Test-Interactive -Value FakeTrue\n"
HOOK = ("$ExecutionContext.InvokeCommand.PreCommandLookupAction = { param($n, $e) if ($n -eq 'Read-Typed') { $e.Command = Get-Command FakeTyped; $e.StopSearch = $true }; "
        "if ($n -eq 'Test-Interactive') { $e.Command = Get-Command FakeTrue; $e.StopSearch = $true } }\n")
POST_HOOK = ("$ExecutionContext.InvokeCommand.PostCommandLookupAction = { param($n, $e) if ($n -eq 'Read-Typed') { $e.Command = Get-Command FakeTyped }; "
             "if ($n -eq 'Test-Interactive') { $e.Command = Get-Command FakeTrue } }\n")
# label -> (attack, real console gate?, expected text)
ATTACKS = {
    "B1 alias over a guard function, plus the two": ("Set-Alias -Scope Global -Name Assert-NoShadowAlias -Value Nothing\n" + TWO, True, GUARD_TEXT),
    "B2 alias over Confirm-Action": ("Set-Alias -Scope Global -Name Confirm-Action -Value Nothing\n", True, GUARD_TEXT),
    "B3 function named with the module prefix over Get-Alias": ("Set-Item -Path 'function:global:Microsoft.PowerShell.Utility\\Get-Alias' -Value { }\n" + TWO, True, GUARD_TEXT),
    "B4 alias named with the module prefix over Get-Alias": ("Set-Alias -Scope Global -Name 'Microsoft.PowerShell.Utility\\Get-Alias' -Value Nothing\n" + TWO, True, GUARD_TEXT),
    "B5 a command lookup hook and no alias": (HOOK, True, "command lookup hook"),
    "B5post a post lookup hook": (POST_HOOK, True, "command lookup hook"),
    "B6 a function over Test-Path that makes the aliases later": (
        "$global:Armed = $false\nfunction global:Test-Path { if (-not $global:Armed) { $global:Armed = $true; "
        "Set-Alias -Scope Global -Name Read-Typed -Value FakeTyped; Set-Alias -Scope Global -Name Test-Interactive -Value FakeTrue }; "
        "Microsoft.PowerShell.Management\\Test-Path @args }\n", True, GUARD_TEXT),
    "B7j an alias over Test-Receipt with the words present": ("Set-Alias -Scope Global -Name Test-Receipt -Value Nothing\n", False, GUARD_TEXT),
    "B7j an alias over Assert-Identity and Assert-Source with the words present": (
        "Set-Alias -Scope Global -Name Assert-Identity -Value Nothing; Set-Alias -Scope Global -Name Assert-Source -Value Nothing\n", False, GUARD_TEXT),
    "R2 a module-prefixed function over Read-Host plus a hook": (
        "function global:FakeRH { param($Prompt, [switch]$AsSecureString) [regex]::Match($Prompt, 'Type (\\w+)').Groups[1].Value }\n"
        "Set-Item -Path 'function:global:Microsoft.PowerShell.Utility\\Read-Host' -Value ${function:FakeRH}\n"
        "$ExecutionContext.InvokeCommand.PreCommandLookupAction = { param($n, $e) if ($n -eq 'Test-Interactive') { $e.Command = Get-Command FakeTrue; $e.StopSearch = $true } }\n",
        True, "command lookup hook"),
    "C1 the two aliases alone": (TWO, True, GUARD_TEXT),
    "C2 the two aliases with AllScope": (
        "Set-Alias -Scope Global -Name Read-Typed -Value FakeTyped -Option AllScope\nSet-Alias -Scope Global -Name Test-Interactive -Value FakeTrue -Option AllScope\n",
        True, GUARD_TEXT),
}


def prompts_of(result):
    return [c for c in result.calls if c["kind"] == "prompt"]


@pytest.mark.parametrize("action", ["JobsCandidate", "JobsUpdate"])
@pytest.mark.parametrize("label", sorted(ATTACKS))
def test_rb_t1_an_attack_on_the_console_gate_or_a_check_is_refused_before_any_call_prompt_or_file(tmp_path, label, action):
    attack, real_console, expected = ATTACKS[label]
    world = JobsPasteWorld(tmp_path, action)
    result = world.run(extra={"real_console": real_console, "attack": FAKES + attack})
    assert result.returncode != 0 and expected in result.stderr, (label, result.stdout, result.stderr)
    assert result.external == [] and prompts_of(result) == [], label
    assert not (world.release_dir / "runs").exists()


@pytest.mark.parametrize("action", ["JobsCandidate", "JobsUpdate"])
def test_rb_t1_the_control_without_an_attack_is_refused_at_the_console_gate_not_by_the_resolution_check(tmp_path, action):
    result = JobsPasteWorld(tmp_path, action).run(extra={"real_console": True})
    assert result.returncode != 0 and "NOT INTERACTIVE" in result.stderr and GUARD_TEXT not in result.stderr, (result.stdout, result.stderr)
    assert all(c["kind"] == "read" for c in result.external)


@pytest.mark.parametrize("hook", ["PreCommandLookupAction", "PostCommandLookupAction", "CommandNotFoundAction"])
@pytest.mark.parametrize("action", ACTIONS)
def test_rb_t1_a_command_lookup_hook_of_any_kind_refuses_every_action(tmp_path, hook, action):
    result = JobsPasteWorld(tmp_path, action).run(extra={"attack": f"$ExecutionContext.InvokeCommand.{hook} = {{ param($n, $e) }}\n"})
    assert result.returncode != 0 and "lookup hook" in result.stderr and hook in result.stderr, (result.stdout, result.stderr)
    assert result.external == [] and prompts_of(result) == []


@pytest.mark.parametrize("action", ACTIONS)
def test_rb_t1_an_alias_over_a_function_of_the_paste_refuses_every_action_including_rollback(tmp_path, action):
    result = JobsPasteWorld(tmp_path, action).run(extra={"attack": FAKES + "Set-Alias -Scope Global -Name Invoke-Runner -Value Nothing\n"})
    assert result.returncode != 0 and GUARD_TEXT in result.stderr and result.external == [], (result.stdout, result.stderr)


# the check runs again before each step, before each prompt, and inside Confirm-Action and Confirm-Update

STEP_ATTACK = {"JobsCandidate": (1, "Assert-Source", "archive"), "JobsUpdate": (1, "Invoke-Runner", "jobs-run-update")}


@pytest.mark.parametrize("action", sorted(STEP_ATTACK))
def test_rb_t1_an_alias_made_during_the_run_stops_the_next_step(tmp_path, action):
    at, alias, step = STEP_ATTACK[action]
    result = JobsPasteWorld(tmp_path, action).run(extra={"inject_at_run": at, "inject": FAKES + f"Set-Alias -Scope Global -Name {alias} -Value Nothing\n"})
    assert result.returncode != 0 and GUARD_TEXT in result.stderr, (result.stdout, result.stderr)
    assert step not in result.names and len(result.runs) == at, result.names


def test_rb_t1_the_check_runs_inside_confirm_update_before_the_first_word_is_asked(tmp_path):
    # The alias is made while the BeforeJobsUpdate readback runs, after the last step check and before IDLE.
    result = JobsPasteWorld(tmp_path, "JobsUpdate").run(extra={"inject_at_run": 1, "inject": FAKES + TWO})
    assert result.returncode != 0 and GUARD_TEXT in result.stderr, (result.stdout, result.stderr)
    assert prompts_of(result) == [] and result.names == ["helper-BeforeJobsUpdate-0"]


@pytest.mark.parametrize("action", ["JobsCandidate", "JobsUpdate"])
def test_rb_t1_the_check_runs_inside_confirm_action_just_before_the_console_gate(tmp_path, action):
    # Moving to the repository raises LocationChangedAction, which here makes the two aliases after the first check has passed.
    attack = FAKES + "$ExecutionContext.InvokeCommand.LocationChangedAction = { " + TWO.strip() + " }\n"
    result = JobsPasteWorld(tmp_path, action).run(extra={"real_console": True, "attack": attack})
    assert result.returncode != 0 and GUARD_TEXT in result.stderr, (result.stdout, result.stderr)
    assert result.external == [] and prompts_of(result) == []


# the check itself

def jobs_function(call, tmp_path):
    """The real functions of the jobs paste in a real pwsh with no double: a definitions-only load, then the call."""
    import subprocess

    frame = Path(tmp_path) / "frame.ps1"
    frame.write_text(f"$ErrorActionPreference = 'Stop'; . '{PASTE}' -Action JobsRollback -Lock x -Review x -Bindings x -Receipt x -DefinitionsOnly; "
                     f"try {{ {call}; 'RESULT:ok' }} catch {{ 'RESULT:' + $_.Exception.Message }}", encoding="utf-8", newline="\n")
    return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(frame)], stdin=subprocess.DEVNULL, capture_output=True,
                          encoding="utf-8", timeout=120)


def checked_names(tmp_path):
    done = jobs_function("& $Script:ResolutionCheck; 'NAMES:' + (@($Script:CheckedNames) + @($Script:NativeNames) -join ',')", tmp_path)
    assert "RESULT:ok" in done.stdout, (done.stdout, done.stderr)
    return next(line for line in done.stdout.splitlines() if line.startswith("NAMES:"))[6:].split(",")


def test_rb_t1_the_names_come_from_the_paste_syntax_tree_and_cover_what_it_calls(tmp_path):
    names = checked_names(tmp_path)
    for expected in ("Test-Path", "Get-Content", "Read-Host", "Get-Alias", "Confirm-Action", "Confirm-Update", "Invoke-Release", "Read-Typed",
                     "Test-Receipt", "Assert-Identity", "Assert-Source", "Test-SnapshotAge", "Invoke-Runner", "Invoke-JobsBuild", "Start-Sleep",
                     "Tee-Object", "gcloud", "git", "py", "bash"):
        assert expected in names, expected
    assert len(set(names)) == len(names) and not [n for n in names if "\\" in n]


@pytest.mark.parametrize("kind", ["alias", "function"])
def test_rb_t1_every_name_the_paste_invokes_is_refused_when_an_alias_or_a_caller_function_takes_it(tmp_path, kind):
    names = checked_names(tmp_path)
    make = "Set-Alias -Name $n -Value Get-Command" if kind == "alias" else "Set-Item -Path ('function:' + $n) -Value { }"
    body = "& { param($n) " + make + "; try { & $Script:ResolutionCheck; $script:missed += $n } catch { } } $n"
    call = "$script:missed = @(); foreach ($n in @(" + ",".join("'" + n + "'" for n in names) + ")) { " + body + " }; if ($script:missed.Count) { throw ('NOT REFUSED: ' + ($script:missed -join ',')) }"
    done = jobs_function(call, tmp_path)
    assert "RESULT:ok" in done.stdout, (kind, done.stdout, done.stderr)


def test_rb_t1_a_function_named_with_the_module_prefix_over_a_checked_name_is_refused(tmp_path):
    done = jobs_function("Set-Item -Path 'function:global:Microsoft.PowerShell.Utility\\Read-Host' -Value { 'DEPLOY' }; Read-Word 'DEPLOY'", tmp_path)
    assert "RESULT:ok" not in done.stdout and GUARD_REFUSED in done.stdout, (done.stdout, done.stderr)


def test_rb_t1_the_default_backslash_function_of_a_clean_console_is_not_refused(tmp_path):
    done = jobs_function("& $Script:ResolutionCheck", tmp_path)
    assert "RESULT:ok" in done.stdout, (done.stdout, done.stderr)


def test_rb_t1_a_function_of_the_caller_is_refused_unless_it_is_a_declared_test_double_of_a_definitions_only_load(tmp_path):
    refused = jobs_function("function Test-Interactive { $true }; & $Script:ResolutionCheck", tmp_path)
    assert GUARD_REFUSED + "Test-Interactive'" in refused.stdout, (refused.stdout, refused.stderr)
    declared = jobs_function("function Test-Interactive { $true }; $Script:TestDoubles = @('Test-Interactive'); & $Script:ResolutionCheck", tmp_path)
    assert "RESULT:ok" in declared.stdout, (declared.stdout, declared.stderr)


def test_rb_t1_declared_test_doubles_count_only_in_a_definitions_only_load(tmp_path):
    import subprocess

    frame = Path(tmp_path) / "real_load.ps1"
    frame.write_text(f"$ErrorActionPreference = 'Stop'; try {{ . '{PASTE}' -Action JobsRollback -Lock x -Review x -Bindings x -Receipt x }} catch {{ }}; "
                     "function Test-Interactive { $true }; $Script:TestDoubles = @('Test-Interactive'); "
                     "try { & $Script:ResolutionCheck; 'RESULT:ok' } catch { 'RESULT:' + $_.Exception.Message }", encoding="utf-8", newline="\n")
    done = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(frame)], stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", timeout=120)
    assert GUARD_REFUSED + "Test-Interactive'" in done.stdout, (done.stdout, done.stderr)


@pytest.mark.parametrize("native", ["gcloud", "git", "py", "bash"])
def test_rb_t1_a_function_or_an_alias_over_a_program_the_paste_runs_is_refused(tmp_path, native):
    as_function = JobsPasteWorld(tmp_path / "f", "JobsRollback").run(extra={"attack": f"function global:{native} {{ }}\n"})
    assert as_function.returncode != 0 and f"'{native}'" in as_function.stderr and as_function.external == [], (as_function.stdout, as_function.stderr)
    as_alias = JobsPasteWorld(tmp_path / "a", "JobsRollback").run(extra={"attack": FAKES + f"Set-Alias -Scope Global -Name {native} -Value Nothing\n"})
    assert as_alias.returncode != 0 and f"'{native}'" in as_alias.stderr and as_alias.external == [], (as_alias.stdout, as_alias.stderr)


def test_rb_t1_a_prompt_is_never_made_in_a_session_where_read_host_is_not_the_cmdlet(tmp_path):
    prompt = jobs_function("function global:Read-Host { 'DEPLOY' }; Read-Typed 'Type DEPLOY to continue'", tmp_path)
    assert "RESULT:ok" not in prompt.stdout and GUARD_REFUSED + "Read-Host'" in prompt.stdout, (prompt.stdout, prompt.stderr)


def test_rb_t1_the_resolution_check_calls_no_powershell_command_so_none_can_be_replaced(tmp_path):
    done = jobs_function("$found = $Script:ResolutionCheck.Ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true); 'COMMANDS:' + $found.Count", tmp_path)
    assert "COMMANDS:0" in done.stdout, (done.stdout, done.stderr)


def check_block(path):
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index("$Script:ResolutionCheck = {")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def test_rb_t1_the_resolution_check_is_a_copy_pinned_equal_to_the_services_paste():
    mine, theirs = check_block(PASTE), check_block(SERVICES_PASTE)
    assert len(mine) > 1500 and mine == theirs


def test_rb_t1_the_paste_calls_the_prompt_cmdlet_by_its_plain_name_and_no_command_by_a_qualified_name():
    text = PASTE.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert len(re.findall(r"(?<![\\\w'-])Read-Host -", code)) == 1
    assert not re.findall(r"[A-Za-z]\\[A-Za-z]+-[A-Za-z]+", code.replace("System32\\tar.exe", ""))
    assert "Assert-NoShadowAlias" not in text and "Shadowable" not in text


def test_rb_t1_the_paste_states_that_a_fresh_console_and_pwsh_noprofile_file_are_required():
    header = "\n".join(line for line in PASTE.read_text(encoding="utf-8").splitlines()[:30] if line.startswith("#"))
    assert "pwsh -NoProfile -File" in header and "freshly opened console" in header


def paste_run(prelude, tmp_path):
    """The paste run for real (not a definitions-only load) from a frame script, after a prelude of the caller's own making."""
    import subprocess

    frame = Path(tmp_path) / "real_frame.ps1"
    frame.write_text(f"$ErrorActionPreference = 'Stop'; {prelude}; try {{ & '{PASTE}' -Action JobsRollback -Lock x -Review x -Bindings x -Receipt x; 'RESULT:ok' }} "
                     f"catch {{ 'RESULT:' + $_.Exception.Message }}", encoding="utf-8", newline="\n")
    return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(frame)], stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", timeout=120)


def test_rb_t1_a_real_run_from_a_clean_console_passes_the_check_and_stops_only_at_the_missing_lock(tmp_path):
    done = paste_run("$null = 1", tmp_path)
    assert "RESULT:NOT EXECUTABLE: the packet lock is missing" in done.stdout and "does not resolve" not in done.stdout, (done.stdout, done.stderr)


@pytest.mark.parametrize("name", ["Invoke-Release", "Read-Host", "Confirm-Action", "Test-Path", "Step", "Test-Receipt"])
def test_rb_t1_a_real_run_refuses_an_alias_over_a_name_it_invokes_before_it_reads_the_lock(tmp_path, name):
    done = paste_run(f"Set-Alias -Scope Global -Name {name} -Value Get-Date", tmp_path)
    assert GUARD_REFUSED + name + "'" in done.stdout and "packet lock" not in done.stdout, (done.stdout, done.stderr)



def test_rb_t1_confirm_update_asks_for_a_console_again_before_the_first_word(tmp_path):
    # Test-Interactive is a declared double of the driver; it turns false after the BeforeJobsUpdate readback, so only a second
    # console check inside Confirm-Update can stop the words from being asked.
    result = JobsPasteWorld(tmp_path, "JobsUpdate").run(extra={"inject_at_run": 1, "inject": "function global:Test-Interactive { $false }\n"})
    assert result.returncode != 0 and "NOT INTERACTIVE" in result.stderr, (result.stdout, result.stderr)
    assert prompts_of(result) == [] and "jobs-run-update" not in result.names


def test_rb_t1_a_name_written_with_a_module_prefix_inside_the_paste_is_refused_before_anything_runs(tmp_path):
    world = JobsPasteWorld(tmp_path, mutate_paste=lambda t: t + "\nfunction Show-Extra { Microsoft.PowerShell.Utility\\Get-Date }\n")
    world.relock()
    result = world.run()
    assert result.returncode != 0 and "Microsoft.PowerShell.Utility\\Get-Date" in result.stderr and result.external == [], (result.stdout, result.stderr)


def test_rb_t1_a_command_added_to_the_paste_is_checked_without_any_list_being_edited(tmp_path):
    world = JobsPasteWorld(tmp_path, mutate_paste=lambda t: t + "\nfunction Show-Extra { Get-Date }\n")
    world.relock()
    assert world.run().returncode == 0
    again = world.run(extra={"attack": "Set-Alias -Scope Global -Name Get-Date -Value Get-Process\n"})
    assert again.returncode != 0 and "'Get-Date'" in again.stderr and again.external == [], (again.stdout, again.stderr)


def test_rb_t1_a_function_the_paste_defines_but_never_calls_is_checked_too(tmp_path):
    world = JobsPasteWorld(tmp_path, mutate_paste=lambda t: t + "\nfunction Show-Extra { 1 }\n")
    world.relock()
    assert world.run().returncode == 0
    taken = world.run(extra={"attack": "Set-Alias -Scope Global -Name Show-Extra -Value Get-Process\n"})
    assert taken.returncode != 0 and "'Show-Extra'" in taken.stderr and taken.external == [], (taken.stdout, taken.stderr)


# RB-T2 (F2): the paste writes a per-run token file after DEPLOY, the update verb needs it, and the paste removes it after use

def token_of(call):
    return json.loads(call["token"]) if call["token"] else None


def test_rb_t2_jobs_update_writes_the_token_after_both_words_and_before_the_runner_and_removes_it_after(tmp_path):
    from core.setup.tests.jobs_paste_world import RID

    world = JobsPasteWorld(tmp_path, "JobsUpdate")
    result = world.run()
    assert result.returncode == 0, result.stderr
    token = token_of(result.run("jobs-run-update"))
    assert token["schema_version"] == 1 and token["release_id"] == RID and re.fullmatch(r"[0-9a-f]{32}", token["token"])
    assert all(token_of(call) is None for call in result.runs if call["name"] != "jobs-run-update")
    assert not list(world.release_dir.glob("runs/*/update-token.json"))
    assert len(prompts_of(result)) == 2 and result.calls.index(result.prompts[1]) < result.calls.index(result.run("jobs-run-update"))


def test_rb_t2_the_token_is_removed_even_when_the_runner_stops_the_action(tmp_path):
    world = JobsPasteWorld(tmp_path, "JobsUpdate")
    result = world.run(exits={"jobs-run-update": 1})
    assert result.returncode != 0 and token_of(result.run("jobs-run-update")) is not None
    assert not list(world.release_dir.glob("runs/*/update-token.json"))


def test_rb_t2_each_run_writes_a_different_token(tmp_path):
    first = token_of(JobsPasteWorld(tmp_path / "a", "JobsUpdate").run().run("jobs-run-update"))
    second = token_of(JobsPasteWorld(tmp_path / "b", "JobsUpdate").run().run("jobs-run-update"))
    assert first["token"] != second["token"]


@pytest.mark.parametrize("action", ["JobsCandidate", "JobsRollback"])
def test_rb_t2_candidate_and_rollback_write_no_token(tmp_path, action):
    world = JobsPasteWorld(tmp_path, action)
    result = world.run()
    assert result.returncode == 0 and all(token_of(call) is None for call in result.runs)
    assert not list(world.release_dir.glob("runs/*/update-token.json"))


@pytest.mark.parametrize("words", [{"IDLE": "idle"}, {"IDLE": "IDLE", "DEPLOY": "deploy"}])
def test_rb_t2_a_wrong_word_leaves_no_token_behind(tmp_path, words):
    world = JobsPasteWorld(tmp_path, "JobsUpdate")
    result = world.run(words=words)
    assert result.returncode != 0 and "jobs-run-update" not in result.names
    assert not list(world.release_dir.glob("runs/*/update-token.json"))


def test_rb_t2_the_file_name_the_paste_writes_is_the_one_the_runner_reads():
    from core.setup.release import jobs_run as jr

    assert jr.TOKEN_NAME in PASTE.read_text(encoding="utf-8")


# RB-T3 (F3): the schema readback receipt is written by the step after the durable readbacks, and every durable path stays in the release directory

def test_rb_t3_the_receipt_step_follows_the_durable_readbacks_and_a_failed_readback_leaves_no_receipt_step(tmp_path):
    result = JobsPasteWorld(tmp_path / "ok", "JobsCandidate").run()
    assert result.names[-2:] == ["durable-readbacks", "schema-receipt"]
    argv = result.run("schema-receipt")["argv"]
    assert argv[:2] == ["py", "-3.13"] and argv[2].endswith("jobs_run.py") and argv[3] == "schema-receipt"
    failed = JobsPasteWorld(tmp_path / "failed", "JobsCandidate").run(exits={"durable-readbacks": 1})
    assert failed.returncode != 0 and "schema-receipt" not in failed.names


@pytest.mark.parametrize("key", ["baselineChainPath", "durableManifestPath", "dryRunReceiptPath", "schemaReadbackReceiptPath"])
@pytest.mark.parametrize("action", ACTIONS)
def test_rb_t3_a_durable_path_outside_the_release_directory_is_refused_before_any_external_call(tmp_path, key, action):
    world = JobsPasteWorld(tmp_path, action, bindings={key: str(tmp_path / "elsewhere" / "file.json")})
    result = world.run()
    assert result.returncode != 0 and result.external == [] and "outside the release directory" in result.stderr, (key, result.stdout, result.stderr)
