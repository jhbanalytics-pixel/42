"""The release paste (W8-REL 5.2, PS-01 to PS-23), run as a pwsh process with the real paste text and doubled commands. No
external command runs. A missing pwsh fails the tests: it is never a reason to skip them."""
import hashlib
import json
import os
import re
import shutil
import dataclasses
from pathlib import Path

import pytest

from core.setup.release import plan
from core.setup.tests.paste_world import (A80, API_TAG_URL, COMMIT, FREEZE_HASH, INHERITED, RID, TYPED, PasteWorld, ROOT)

ACTIONS = ("Candidate", "Promote", "Rollback", "Retire")
GUARD_REFUSED = "RESULT:NOT EXECUTABLE: the command '"


@pytest.fixture(autouse=True)
def pwsh_is_present():
    if shutil.which("pwsh") is None:
        pytest.fail("pwsh is not installed; the paste tests cannot run (install PowerShell 7)")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def no_external_call(result):
    return result.external == []


def prompts(result):
    return [c for c in result.calls if c["kind"] == "prompt"]


def ctx_for(world, run_dir="<run dir>"):
    return plan.Ctx(release_id=RID, commit=COMMIT, helper="core/setup/release/bound_readback.py", bindings=str(world.bindings), evidence=run_dir,
                    manifest=str(world.paths["manifest"]), manifest_sha256=FREEZE_HASH, api_tag_url=API_TAG_URL, a80=A80,
                    image_tag=f"us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/f42-web:{COMMIT[:12]}-01")


def candidate_names(durable_schema=False):
    names = ["helper-BeforeAnyWrite-0", "archive", "extract", "build", "helper-Freeze-0", "durable-validate"]
    if durable_schema:
        names.append("schema-apply")
    return names + ["pin-f42-agent", "pin-f42-api", "helper-BeforeCandidate-0", "deploy-candidate", "helper-BeforeSmoke-0", "smoke", "helper-AfterSmoke-0"]


def promote_world(tmp_path, **kw):
    world = PasteWorld(tmp_path, "Promote", **kw)
    world.after_smoke()
    return world


# PS-01: the lock binds every file the paste runs

def locked_files(world):
    lock = json.loads(world.lock.read_text(encoding="utf-8"))
    repo = [(world.repo / rel) for rel in lock["repo_files"]]
    bound = {"bindings": world.bindings, "baseline": world.baseline, "durable_manifest": world.durable, "compat_receipt": world.compat, "old_reader_receipt": world.old}
    return repo + [bound[name] for name in lock["bound_files"]]


def test_ps01_the_unchanged_packet_passes_every_check_before_the_first_call(tmp_path):
    result = PasteWorld(tmp_path).run()
    assert result.returncode == 0, result.stderr
    assert result.external[0]["kind"] == "read"


def test_ps01_flipping_one_byte_in_any_locked_file_stops_the_paste_before_the_first_external_call(tmp_path):
    reference = PasteWorld(tmp_path / "ref")
    count = len(locked_files(reference))
    assert count >= 15
    for index in range(count):
        world = PasteWorld(tmp_path / f"w{index}")
        target = locked_files(world)[index]
        target.write_bytes(target.read_bytes() + b" ")
        result = world.run()
        assert result.returncode != 0 and no_external_call(result), (target.name, result.stderr[-300:])
        assert "NOT EXECUTABLE" in result.stderr, target.name


def test_ps01_a_review_that_binds_another_lock_or_target_or_has_another_verdict_stops(tmp_path):
    for change in ({"verdict": "REVISE"}, {"target": "0" * 40}, {"lock_sha256": "0" * 64}, {"schema_version": 2}):
        world = PasteWorld(tmp_path / "-".join(change))
        review = json.loads(world.review.read_text(encoding="utf-8"))
        world.write(world.review, {**review, **change})
        result = world.run()
        assert result.returncode != 0 and no_external_call(result), change


def test_ps01_bindings_that_are_not_the_reviewed_ones_stop(tmp_path):
    for change in ({"status": "DRAFT"}, {"mode": "full"}, {"release_id": "rel-0000000-01"}):
        world = PasteWorld(tmp_path / "-".join(change), bindings=change)
        result = world.run()
        assert result.returncode != 0 and no_external_call(result), change


# PS-02, PS-19: actions and the receipt

def test_ps02_an_unknown_action_is_a_binding_error_before_any_call(tmp_path):
    result = PasteWorld(tmp_path).run(action="Deploy")
    assert result.returncode != 0 and no_external_call(result)


def test_ps02_an_action_the_receipt_does_not_declare_stops_before_any_call(tmp_path):
    result = PasteWorld(tmp_path, declared_steps=["Promote"]).run(action="Candidate")
    assert result.returncode != 0 and no_external_call(result) and "declared steps" in result.stderr


@pytest.mark.parametrize("receipt", [{"schema_version": 2}, {"schema_version": None}, {"later_explicit_user_instruction": "true"},
                                     {"quiet_window_confirmed": False}, {"no_new_manual_starts_until_execution_ends": 1},
                                     {"single_T1_smoke_authorized": False}, {"max_live_asks": 2}, {"max_live_asks": "1"}, {"release_id": "rel-0000000-02"},
                                     {"target": "0" * 40}])
def test_ps19_a_receipt_without_a_known_version_or_with_malformed_confirmations_stops_before_any_call(tmp_path, receipt):
    world = PasteWorld(tmp_path, receipt=receipt)
    if "schema_version" in receipt and receipt["schema_version"] is None:
        data = json.loads(world.receipt.read_text(encoding="utf-8"))
        data.pop("schema_version")
        world.write(world.receipt, data)
    result = world.run()
    assert result.returncode != 0 and no_external_call(result), receipt


# PS-03, PS-04: Candidate

def test_ps03_candidate_runs_the_contract_order_and_moves_traffic_only_for_the_two_pins(tmp_path):
    world = PasteWorld(tmp_path)
    result = world.run()
    assert result.returncode == 0, result.stderr
    assert result.names == candidate_names()
    traffic = [c for c in result.runs if "update-traffic" in c["argv"]]
    assert [c["name"] for c in traffic] == ["pin-f42-agent", "pin-f42-api"]
    assert [a for c in traffic for a in c["argv"] if a.startswith("--to-revisions=")] == [f"--to-revisions={A80['f42-agent']}=100", f"--to-revisions={A80['f42-api']}=100"]
    reads = [" ".join(c["argv"]) for c in result.calls if c["kind"] == "read"]
    assert reads[0] == "git rev-parse HEAD" and any(r.startswith("gcloud config list") for r in reads)


def test_ps03_every_step_matches_the_plan_the_contract_tests_pin(tmp_path):
    world = PasteWorld(tmp_path)
    result = world.run()
    expected = {s.name: list(s.argv) for s in plan.candidate(ctx_for(world, result.run("helper-BeforeAnyWrite-0")["argv"][-1]))}
    for name, key in (("pin-f42-agent", "pin:f42-agent"), ("pin-f42-api", "pin:f42-api"), ("build", "build"), ("smoke", "smoke")):
        got = result.run(name)["argv"]
        want = expected[key]
        if name == "build":
            want = [a if not a.startswith("_IMAGE=") else a for a in want]
        assert got == want, name
    assert result.run("deploy-candidate")["argv"][:2] == ["bash", "core/api/deploy_candidate.sh"]
    assert result.run("deploy-candidate")["argv"][3] == FREEZE_HASH


def test_ps04_the_smoke_url_is_the_manifest_api_tag_url_and_not_the_public_api(tmp_path):
    result = PasteWorld(tmp_path).run()
    argv = result.run("smoke")["argv"]
    assert argv[3] == API_TAG_URL and "---" in argv[3] and argv[4:] == ["--market", "ZA", "--mode", "live", "--ask-timeout", "900"]
    assert "https://f42-api-fibxg5ynpq-uc.a.run.app" not in argv


# PS-05, PS-08, PS-09: Promote

def test_ps05_promote_moves_the_agent_then_the_api_by_name_and_never_to_latest(tmp_path):
    result = promote_world(tmp_path).run()
    assert result.returncode == 0, result.stderr
    assert [n for n in result.names if not n.startswith("inflight")] == ["helper-BeforePromotion-0", "promote-f42-agent", "helper-AfterAgentPromotion-0",
                                                                           "promote-f42-api", "helper-AfterPromotion-0"]
    assert f"--to-revisions=f42-agent-{RID}=100" in result.run("promote-f42-agent")["argv"]
    assert f"--to-revisions=f42-api-{RID}=100" in result.run("promote-f42-api")["argv"]
    assert not any("--to-latest" in c["argv"] or "jobs" in " ".join(c["argv"]) for c in result.runs)


def test_ps05_promote_records_the_inflight_ask_count_as_an_observation_without_failing_on_it(tmp_path):
    result = promote_world(tmp_path).run(exits={"inflight-asks": 1})
    assert result.returncode == 0 and "inflight-asks" in result.names
    assert result.run("inflight-asks")["argv"][3:5] == ["--check", "inflight-asks"]


def test_ps08_the_api_is_never_promoted_when_the_agent_readback_fails(tmp_path):
    result = promote_world(tmp_path).run(exits={"helper-AfterAgentPromotion-0": 1})
    assert result.returncode != 0 and "promote-f42-api" not in result.names and result.names[-1] == "helper-AfterAgentPromotion-0"


def test_ps08_the_api_is_never_promoted_when_the_agent_readback_file_is_missing(tmp_path):
    result = promote_world(tmp_path).run(no_readback=["AfterAgentPromotion"])
    assert result.returncode != 0 and "promote-f42-api" not in result.names


def test_ps09_a_quiet_window_verification_older_than_fifteen_minutes_or_absent_stops_promote_before_any_call(tmp_path):
    import datetime as dt

    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=16)).isoformat()
    for quiet in (old, ""):
        result = promote_world(tmp_path / ("old" if quiet else "none")).run(quiet=quiet)
        assert result.returncode != 0 and no_external_call(result), quiet
    result = promote_world(tmp_path / "ok").run(quiet=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=14)).isoformat())
    assert result.returncode == 0


def test_ps09_promote_stops_without_an_aftersmoke_readback(tmp_path):
    world = promote_world(tmp_path)
    for f in (world.release_dir / "readbacks").glob("AfterSmoke-*.json"):
        f.unlink()
    result = world.run()
    assert result.returncode != 0 and "AfterSmoke never ran" in result.stderr
    assert not [c for c in result.runs if "promote" in c["name"]]


# PS-06: Rollback

def test_ps06_rollback_restores_the_api_then_the_agent_to_the_baseline_revisions_in_order(tmp_path):
    result = PasteWorld(tmp_path, "Rollback").run()
    assert result.returncode == 0, result.stderr
    assert result.names == ["helper-BeforeRollback-0", "restore-f42-api", "restore-f42-agent", "helper-AfterRollback-0"]
    assert f"--to-revisions={A80['f42-api']}=100" in result.run("restore-f42-api")["argv"]
    assert f"--to-revisions={A80['f42-agent']}=100" in result.run("restore-f42-agent")["argv"]


def test_ps06_the_rollback_targets_are_read_from_the_bound_baseline(tmp_path):
    world = PasteWorld(tmp_path, "Rollback")
    world.write(world.baseline, {"schema_version": 1, "kind": "baseline-A", "services": {
        "f42-agent": {"serving": {"name": "f42-agent-00099-aaa"}}, "f42-api": {"serving": {"name": "f42-api-00099-bbb"}}}})
    world.relock()
    result = world.run()
    assert "--to-revisions=f42-api-00099-bbb=100" in result.run("restore-f42-api")["argv"]
    assert "--to-revisions=f42-agent-00099-aaa=100" in result.run("restore-f42-agent")["argv"]


@pytest.mark.parametrize("bindings", [{"rollbackRevision": "f42-agent-00046-wks"}, {"rollbackImageDigest": "sha256:" + "6c" * 32},
                                      {"services": [{"note": "f42-api-00040-cw5"}]}, {"jobs": [{"digest": "sha256:17ad022e3f5e"}]},
                                      {"note": "sha256:6c7b718bb15d"}])
def test_ps06_bindings_that_carry_the_pre_a80_targets_stop_the_paste_before_any_call(tmp_path, bindings):
    result = PasteWorld(tmp_path, "Rollback", bindings=bindings).run()
    assert result.returncode != 0 and no_external_call(result) and "pre-a80" in result.stderr or "rollback keys" in result.stderr


# PS-07: a failing step stops everything after it, with no automatic rollback

@pytest.mark.parametrize("name", candidate_names())
def test_ps07_a_failing_candidate_step_leaves_every_later_step_unissued_and_issues_no_rollback(tmp_path, name):
    result = PasteWorld(tmp_path).run(exits={name: 1})
    assert result.returncode != 0
    assert result.names[-1] == name
    assert result.names == candidate_names()[:candidate_names().index(name) + 1]
    assert not any(n.startswith(("restore", "remove-tag")) for n in result.names)


@pytest.mark.parametrize("name", ["helper-BeforePromotion-0", "promote-f42-agent", "helper-AfterAgentPromotion-0", "promote-f42-api", "helper-AfterPromotion-0"])
def test_ps07_a_failing_promote_step_leaves_every_later_step_unissued(tmp_path, name):
    result = promote_world(tmp_path).run(exits={name: 1})
    order = ["inflight-asks", "helper-BeforePromotion-0", "promote-f42-agent", "helper-AfterAgentPromotion-0", "promote-f42-api", "helper-AfterPromotion-0"]
    assert result.returncode != 0 and result.names == order[:order.index(name) + 1]


# PS-10: no jobs

def test_ps10_the_paste_has_no_jobs_scheduler_or_iam_command_and_issues_none(tmp_path):
    text = (ROOT / "core/setup/release/SERVICES-PASTE.ps1").read_text(encoding="utf-8")
    for word in ("jobs update", "deploy_jobs", "jobs execute", "scheduler", "set-iam-policy", "add-iam-policy-binding", "gcloud iam", "allow-unauthenticated"):
        assert word not in text.lower(), word
    for action in ACTIONS:
        world = promote_world(tmp_path / action) if action == "Promote" else PasteWorld(tmp_path / action, action)
        if action == "Retire":
            world.readback("AfterRollback")
        result = world.run()
        assert result.returncode == 0, (action, result.stderr)
        joined = " ".join(" ".join(c["argv"]) for c in result.external)
        assert not re.search(r"\bjobs\b|scheduler|gcloud iam|iam-policy", joined), action


# PS-11: the smoke credential

def test_ps11_a_typed_passcode_reaches_the_smoke_and_an_inherited_one_never_reaches_any_child(tmp_path):
    result = PasteWorld(tmp_path).run(inherited=INHERITED)
    assert result.returncode == 0, result.stderr
    assert [(c["secure"], c["prompt"]) for c in prompts(result)] == [(False, "Type DEPLOY to continue"), (True, "Smoke passcode")]
    assert all(c["passcode_sha"] == "" for c in result.calls if c["kind"] == "read")  # no git child ever sees it
    seen = {c["name"]: c["passcode_sha"] for c in result.runs}
    assert seen["smoke"] == sha(TYPED)
    assert all(v in ("", sha(TYPED)) for v in seen.values()) and sha(INHERITED) not in seen.values()
    assert [n for n, v in seen.items() if v] == ["smoke"]
    assert result.calls[-1] == {"kind": "end", "passcode_sha": sha(INHERITED)}
    assert INHERITED not in json.dumps(result.calls) and TYPED not in json.dumps(result.calls)


def test_ps11_with_no_inherited_value_the_prompt_is_still_the_only_source_and_nothing_is_left_behind(tmp_path):
    result = PasteWorld(tmp_path).run()
    assert result.run("smoke")["passcode_sha"] == sha(TYPED)
    assert result.calls[-1]["passcode_sha"] == ""
    assert not (tmp_path / "x").exists()


def test_ps11_a_passcode_parameter_is_refused_by_the_parameter_binding_and_the_paste_declares_none(tmp_path):
    text = (ROOT / "core/setup/release/SERVICES-PASTE.ps1").read_text(encoding="utf-8")
    assert not re.search(r"\[(Security\.)?SecureString\]\s*\$\w*[Pp]asscode", text) and "ConvertTo-SecureString" not in text
    assert "param(" in text and "SmokePasscode" not in text
    world = PasteWorld(tmp_path)
    world.run()  # builds the driver
    driver = (tmp_path / "driver.ps1").read_text(encoding="utf-8").replace("DefinitionsOnly = $true }", "DefinitionsOnly = $true; SmokePasscode = 'x' }")
    (tmp_path / "driver.ps1").write_text(driver, encoding="utf-8")
    (tmp_path / "calls.jsonl").write_text("", encoding="utf-8")
    import subprocess

    proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(tmp_path / "driver.ps1"), "-Config", str(tmp_path / "config.json")],
                          capture_output=True, encoding="utf-8", timeout=120)
    assert proc.returncode != 0 and "SmokePasscode" in proc.stderr
    assert (tmp_path / "calls.jsonl").read_text(encoding="utf-8").strip() == ""


@pytest.mark.parametrize("failing", ["build", "helper-BeforeCandidate-0", "pin-f42-agent"])
def test_ps11_a_failure_before_the_smoke_means_no_smoke_and_no_passcode_in_any_earlier_child(tmp_path, failing):
    result = PasteWorld(tmp_path).run(exits={failing: 1}, inherited=INHERITED)
    assert "smoke" not in result.names and all(c["passcode_sha"] == "" for c in result.runs)
    assert result.calls[-1] == {"kind": "end", "passcode_sha": sha(INHERITED)}


def test_ps11_rollback_never_prompts_and_never_smokes(tmp_path):
    result = PasteWorld(tmp_path, "Rollback").run()
    assert not [c for c in result.calls if c["kind"] == "prompt"] and "smoke" not in result.names


def test_ps11_without_an_interactive_host_the_paste_stops_before_any_call_with_no_fallback(tmp_path):
    result = PasteWorld(tmp_path).run(interactive=False, inherited=INHERITED)
    assert result.returncode != 0 and no_external_call(result) and not prompts(result)
    assert "NOT INTERACTIVE" in result.stderr
    assert not (tmp_path / "releases" / RID / "runs").exists()
    assert result.calls[-1] == {"kind": "end", "passcode_sha": sha(INHERITED)}


def test_ps11_the_prompt_is_asked_only_after_the_receipt_authorisation_validates(tmp_path):
    for index, receipt in enumerate(({"single_T1_smoke_authorized": False}, {"max_live_asks": 2})):
        result = PasteWorld(tmp_path / str(index), receipt=receipt).run()
        assert not prompts(result) and no_external_call(result)


# PS-12: logs, exit files and readback files

def test_ps12_each_step_leaves_a_log_and_an_exit_file_and_each_helper_phase_a_readback(tmp_path):
    world = PasteWorld(tmp_path)
    result = world.run()
    run_dir = Path(result.run("helper-BeforeAnyWrite-0")["argv"][-1])
    for name in result.names:
        assert (run_dir / f"{name}.log").exists() and (run_dir / f"{name}.exit.txt").read_text(encoding="utf-8").strip() == "0", name
    phases = {f.name.split("-")[0] for f in (world.release_dir / "readbacks").glob("*.json")}
    assert phases == {"BeforeAnyWrite", "Freeze", "BeforeCandidate", "BeforeSmoke", "AfterSmoke"}


def test_ps12_a_helper_phase_that_exits_zero_without_its_readback_file_stops_the_paste(tmp_path):
    result = PasteWorld(tmp_path).run(no_readback=["BeforeCandidate"])
    assert result.returncode != 0 and "readback file does not exist" in result.stderr
    assert "deploy-candidate" not in result.names


# PS-13, PS-16, PS-23: the release directory and the frozen manifest

def test_ps13_a_manifest_that_already_exists_stops_candidate_before_freeze(tmp_path):
    world = PasteWorld(tmp_path)
    world.frozen_manifest()
    result = world.run()
    assert result.returncode != 0 and "already exists" in result.stderr and "helper-Freeze-0" not in result.names


@pytest.mark.parametrize("key", ["manifestPath", "sidecarPath", "smokeReceiptPath", "compatReceiptPath", "oldReaderReceiptPath"])
def test_ps16_a_durable_path_outside_the_release_directory_is_refused_before_any_call(tmp_path, key):
    world = PasteWorld(tmp_path, bindings={key: str(tmp_path / "elsewhere" / "file.json")})
    result = world.run()
    assert result.returncode != 0 and no_external_call(result) and "outside the release directory" in result.stderr


def test_ps16_promote_reads_only_the_bound_files_and_stops_when_one_is_missing(tmp_path):
    for missing in ("manifest", "sidecar", "smoke"):
        world = promote_world(tmp_path / missing)
        world.paths[missing].unlink()
        result = world.run()
        assert result.returncode != 0 and not [c for c in result.runs if c["name"].startswith(("promote", "helper"))], missing


def test_ps16_a_manifest_in_another_folder_is_not_read(tmp_path):
    world = promote_world(tmp_path)
    other = world.tmp / "other"
    other.mkdir()
    world.paths["manifest"].replace(other / "release-manifest.json")
    result = world.run()
    assert result.returncode != 0 and not [c for c in result.runs if c["name"].startswith("helper")]


def test_ps23_a_manifest_and_sidecar_rewritten_together_after_freeze_are_refused_before_any_call(tmp_path):
    world = promote_world(tmp_path)
    world.frozen_manifest(manifest_hash="cd" * 32)
    result = world.run()
    assert result.returncode != 0 and "differs from the one the Freeze readback recorded" in result.stderr
    assert not [c for c in result.runs if c["name"].startswith(("helper", "promote"))]


def test_ps23_a_sidecar_that_disagrees_with_the_manifest_is_refused(tmp_path):
    world = promote_world(tmp_path)
    world.frozen_manifest(sidecar="cd" * 32)
    assert world.run().returncode != 0


def test_ps23_the_hash_handed_to_the_deploy_script_is_the_one_the_freeze_readback_recorded(tmp_path):
    result = PasteWorld(tmp_path).run()
    assert result.run("deploy-candidate")["argv"][3] == FREEZE_HASH


def test_ps23_candidate_refuses_a_manifest_on_disk_whose_hash_is_not_the_one_the_freeze_readback_recorded(tmp_path):
    result = PasteWorld(tmp_path).run(extra={"manifest_hash_on_freeze": "cd" * 32})
    assert result.returncode != 0 and "deploy-candidate" not in result.names
    assert "differs from the one the Freeze readback recorded" in result.stderr


# PS-14: pinned values come from locked files

def test_ps14_the_paste_holds_no_literal_digest_tag_or_revision_but_the_pre_a80_refusal_list():
    text = (ROOT / "core/setup/release/SERVICES-PASTE.ps1").read_text(encoding="utf-8")
    literals = re.findall(r"sha256:[0-9a-f]{6,}|rel-[0-9a-f]{7}-[0-9]{2}|f42-(?:agent|api)-[0-9]{5}-[a-z0-9]{3}", text)
    assert sorted(literals) == sorted(["sha256:6c7b718b", "sha256:17ad022e", "f42-agent-00046-wks", "f42-api-00040-cw5"])


# PS-15: PROBE retries cover reads only

def test_ps15_a_probe_once_then_success_retries_once_with_a_pause(tmp_path):
    result = PasteWorld(tmp_path, "Rollback").run(exits={"helper-BeforeRollback-0": 3})
    assert result.returncode == 0
    assert result.names[:2] == ["helper-BeforeRollback-0", "helper-BeforeRollback-1"]
    assert [c["seconds"] for c in result.calls if c["kind"] == "sleep"] == [10]


def test_ps15_four_probes_in_a_row_stop_after_three_retries(tmp_path):
    result = PasteWorld(tmp_path, "Rollback").run(exits={f"helper-BeforeRollback-{i}": 3 for i in range(5)})
    assert result.returncode != 0 and result.names == [f"helper-BeforeRollback-{i}" for i in range(4)]
    assert [c["seconds"] for c in result.calls if c["kind"] == "sleep"] == [10, 10, 10]
    assert "restore-f42-api" not in result.names


def test_ps15_a_failed_check_is_never_retried_and_a_write_is_never_retried(tmp_path):
    result = PasteWorld(tmp_path, "Rollback").run(exits={"helper-BeforeRollback-0": 1})
    assert result.names == ["helper-BeforeRollback-0"] and not [c for c in result.calls if c["kind"] == "sleep"]
    result = PasteWorld(tmp_path / "write", "Rollback").run(exits={"restore-f42-api": 3})
    assert result.names == ["helper-BeforeRollback-0", "restore-f42-api"] and result.returncode != 0
    result = PasteWorld(tmp_path / "build").run(exits={"build": 3})
    assert result.names[-1] == "build" and result.names.count("build") == 1


# PS-17: the validator is on the acting path

def test_ps17_the_validator_runs_on_the_real_manifest_before_anything_is_applied(tmp_path):
    result = PasteWorld(tmp_path, durable={"schema_version": 1, "effects": [{"apply_kind": "schema"}]}).run()
    names = result.names
    assert names.index("durable-validate") < names.index("schema-apply") < names.index("pin-f42-agent")
    assert result.run("schema-apply")["argv"] == ["py", "-3.13", "-m", "core.schema.apply", "--apply"]
    assert result.run("durable-validate")["argv"][3:7] == ["--manifest", str(tmp_path / "durable.json"), "--check", "validate"]


def test_ps17_a_manifest_the_validator_refuses_applies_nothing(tmp_path):
    result = PasteWorld(tmp_path, durable={"schema_version": 1, "effects": [{"apply_kind": "schema"}]}).run(exits={"durable-validate": 1})
    assert result.returncode != 0 and result.names[-1] == "durable-validate"
    assert not any(n in result.names for n in ("schema-apply", "pin-f42-agent", "deploy-candidate"))


def test_ps17_an_apply_kind_outside_the_allowlist_is_refused_after_validation_and_before_any_apply(tmp_path):
    result = PasteWorld(tmp_path, durable={"schema_version": 1, "effects": [{"apply_kind": "gcloud run services update"}]}).run()
    assert result.returncode != 0 and "allowlist" in result.stderr
    assert result.names[-1] == "durable-validate" and "schema-apply" not in result.names


def test_ps17_without_schema_effects_the_schema_apply_is_not_run(tmp_path):
    assert "schema-apply" not in PasteWorld(tmp_path).run().names


# PS-18: rollback and retire do not assert a clean checkout

DIRTY = {"git status --porcelain=v1": " M core/api/smoke.py"}
MOVED = {"git rev-parse HEAD": "0" * 40}


@pytest.mark.parametrize("reads", [DIRTY, MOVED], ids=["dirty", "moved"])
def test_ps18_rollback_and_retire_proceed_and_record_the_source_state_as_an_observation(tmp_path, reads):
    result = PasteWorld(tmp_path / "rb", "Rollback").run(reads=reads)
    assert result.returncode == 0, result.stderr
    retire = PasteWorld(tmp_path / "rt", "Retire")
    retire.readback("AfterRollback")
    result = retire.run(reads=reads)
    assert result.returncode == 0, result.stderr
    run_dirs = list((tmp_path / "rb" / "releases" / RID / "runs").iterdir())
    assert "differs" in (run_dirs[0] / "source-observation.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("reads", [DIRTY, MOVED], ids=["dirty", "moved"])
def test_ps18_candidate_and_promote_stop_on_a_dirty_or_moved_checkout(tmp_path, reads):
    result = PasteWorld(tmp_path / "c").run(reads=reads)
    assert result.returncode != 0 and result.runs == []
    result = promote_world(tmp_path / "p").run(reads=reads)
    assert result.returncode != 0 and result.runs == []


# PS-20: the smoke statement

@pytest.mark.parametrize("lines", [
    ["PASS health: ok", "6 of 6 checks passed"],
    ["6 of 6 checks passed", "SMOKE-RESULT base=https://f42-api-fibxg5ynpq-uc.a.run.app checks=6 passed=6"],
    ["6 of 6 checks passed", "SMOKE-RESULT base={url} checks=5 passed=5"],
    ["6 of 6 checks passed", "SMOKE-RESULT base={url}/ checks=6 passed=6"],
    ["SMOKE-RESULT base={url} checks=six passed=6"],
])
def test_ps20_a_smoke_that_does_not_state_this_url_and_the_locked_count_writes_no_receipt_and_blocks_promote(tmp_path, lines):
    world = PasteWorld(tmp_path)
    result = world.run(smoke_lines=lines)
    assert result.returncode != 0 and not world.paths["smoke"].exists()
    assert "helper-AfterSmoke-0" not in result.names


def test_ps20_a_good_smoke_writes_one_exclusive_receipt_with_the_locked_count_and_the_url(tmp_path):
    world = PasteWorld(tmp_path)
    result = world.run()
    receipt = json.loads(world.paths["smoke"].read_text(encoding="utf-8"))
    assert receipt["schema_version"] == 1 and receipt["argv_url"] == API_TAG_URL and receipt["checks_total"] == 6 == receipt["checks_passed"]
    assert receipt["exit_code"] == 0 and receipt["manifest_sha256"] == FREEZE_HASH and len(receipt["log_sha256"]) == 64
    assert receipt["candidates"] == {"f42-agent": f"f42-agent-{RID}", "f42-api": f"f42-api-{RID}"}
    assert result.returncode == 0


def test_ps20_a_second_smoke_cannot_overwrite_the_receipt(tmp_path):
    world = PasteWorld(tmp_path)
    world.frozen_manifest()
    world.write(world.paths["smoke"], {"schema_version": 1, "old": True})
    world.paths["manifest"].unlink()
    result = world.run()
    assert result.returncode != 0
    assert json.loads(world.paths["smoke"].read_text(encoding="utf-8")) == {"schema_version": 1, "old": True}


def test_ps20_a_failed_smoke_is_recorded_in_a_receipt_and_stops_the_paste_before_aftersmoke(tmp_path):
    world = PasteWorld(tmp_path)
    result = world.run(exits={"smoke": 1}, smoke_lines=["FAIL ask: x", "5 of 6 checks passed", "SMOKE-RESULT base={url} checks=6 passed=5"])
    receipt = json.loads(world.paths["smoke"].read_text(encoding="utf-8"))
    assert result.returncode != 0 and receipt["exit_code"] == 1 and receipt["checks_passed"] == 5
    assert "helper-AfterSmoke-0" not in result.names


# PS-21: retire ordering

def test_ps21_retire_is_refused_on_a_failure_branch_until_aftersrollback_has_run(tmp_path):
    world = PasteWorld(tmp_path, "Retire")
    world.readback("AfterAgentPromotion")
    result = world.run()
    assert result.returncode != 0 and "Retire is refused" in result.stderr and result.runs == []


def test_ps21_retire_after_a_rollback_reads_before_it_removes_and_reads_after(tmp_path):
    world = PasteWorld(tmp_path, "Retire")
    world.readback("AfterAgentPromotion")
    world.readback("AfterRollback")
    result = world.run()
    assert result.names == ["helper-BeforeRetire-0", "remove-tag-f42-api", "remove-tag-f42-agent", "helper-AfterRetire-0"]
    assert result.run("helper-BeforeRetire-0")["argv"][-2:] == ["--tag", RID]
    assert f"{RID}" in result.run("remove-tag-f42-api")["argv"] and "--remove-tags" in result.run("remove-tag-f42-agent")["argv"]


def test_ps21_retire_after_a_failed_smoke_needs_no_rollback_because_traffic_never_moved(tmp_path):
    world = PasteWorld(tmp_path, "Retire")
    world.readback("AfterSmoke")
    assert world.run().returncode == 0
    nothing = PasteWorld(tmp_path / "none", "Retire")
    assert nothing.run().returncode != 0


# PS-22: the positive allowlist

def all_runs(tmp_path):
    out = []
    for action in ACTIONS:
        if action == "Promote":
            world = promote_world(tmp_path / action)
        else:
            world = PasteWorld(tmp_path / action, action, durable={"schema_version": 1, "effects": [{"apply_kind": "schema"}, {"apply_kind": "tag_add"}]})
            if action == "Retire":
                world.readback("AfterRollback")
        result = world.run()
        assert result.returncode == 0, (action, result.stderr)
        out += [(action, c) for c in result.runs]
    return out


def test_ps22_every_external_invocation_of_every_action_matches_exactly_one_allowlist_entry(tmp_path):
    runs = all_runs(tmp_path)
    assert len(runs) > 25
    for action, call in runs:
        assert len(plan.matching_entries(call["argv"])) == 1, (action, call["name"], call["argv"])


def test_ps22_the_paste_makes_no_bigquery_or_network_call_of_its_own():
    text = (ROOT / "core/setup/release/SERVICES-PASTE.ps1").read_text(encoding="utf-8")
    for word in ("bq ", "bq.exe", "curl", "Invoke-WebRequest", "Invoke-RestMethod", "listening-post-staging", "--to-latest", "--update-tags", "--set-tags",
                 "--clear-tags", "--to-tags", "gcloud run deploy", "gcloud run services update ", "gcloud run jobs"):
        assert word not in text, word


@pytest.mark.parametrize("mutation, action", [
    (lambda t: t.replace("'--quiet'))\n}\n\nfunction Remove-Tag", "'--quiet', '--to-latest'))\n}\n\nfunction Remove-Tag"), "Candidate"),
    (lambda t: t.replace("'--remove-tags', $Script:Bound.release_id", "'--update-tags', $Script:Bound.release_id"), "Retire"),
    (lambda t: t.replace("Step -Name 'archive'", "Step -Name 'bq' -Argv @('bq', 'query', 'select 1') | Out-Null\n    Step -Name 'archive'"), "Candidate"),
    (lambda t: t.replace("Step -Name 'archive'", "Step -Name 'x' -Argv @('gcloud', 'run', 'services', 'update', 'f42-api') | Out-Null\n    Step -Name 'archive'"), "Candidate"),
    (lambda t: t.replace("Step -Name 'archive'", "Step -Name 'x' -Argv @('gcloud', 'iam', 'service-accounts', 'list') | Out-Null\n    Step -Name 'archive'"), "Candidate"),
    (lambda t: t.replace("Step -Name 'archive'", "Step -Name 'x' -Argv @('curl', 'https://example.invalid') | Out-Null\n    Step -Name 'archive'"), "Candidate"),
    (lambda t: t.replace("Step -Name 'archive'", "Step -Name 'x' -Argv @('gcloud', 'run', 'services', 'describe', 'listening-post-staging', '--format=json') | Out-Null\n    Step -Name 'archive'"), "Candidate"),
    (lambda t: t.replace("Step -Name 'archive'", "Step -Name 'x' -Argv @('gcloud', 'run', 'deploy', 'f42-api', '--image', 'x') | Out-Null\n    Step -Name 'archive'"), "Candidate"),
], ids=["to-latest", "update-tags", "bq", "services-update", "iam", "curl", "describe-lp", "direct-deploy"])
def test_ps22_a_command_added_to_the_paste_matches_no_allowlist_entry(tmp_path, mutation, action):
    world = PasteWorld(tmp_path, action, mutate_paste=mutation)
    if action == "Retire":
        world.readback("AfterRollback")
    world.relock()
    original = (ROOT / "core/setup/release/SERVICES-PASTE.ps1").read_text(encoding="utf-8")
    assert mutation(original) != original
    result = world.run()
    bad = [c for c in result.runs if len(plan.matching_entries(c["argv"])) != 1]
    assert bad, "the mutated command was not issued or matched the allowlist"


# edges the first mutant pass found nothing pinned

def test_the_paste_that_is_running_must_be_the_locked_one_even_when_it_runs_from_another_place(tmp_path):
    world = PasteWorld(tmp_path)
    elsewhere = tmp_path / "elsewhere" / "SERVICES-PASTE.ps1"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(world.paste.read_bytes() + b"\n# changed\n")
    result = world.run(extra={"paste": str(elsewhere)})
    assert result.returncode != 0 and no_external_call(result) and "this paste differs from the lock" in result.stderr


def test_an_empty_quiet_window_verification_is_named_as_missing(tmp_path):
    result = promote_world(tmp_path).run(quiet="")
    assert result.returncode != 0 and "quiet-window verification time" in result.stderr


@pytest.mark.parametrize("action", ["Candidate", "Promote"])
def test_candidate_and_promote_assert_the_checkout_before_every_command_not_only_at_the_start(tmp_path, action):
    world = PasteWorld(tmp_path) if action == "Candidate" else promote_world(tmp_path)
    dirty = {"git status --porcelain=v1": {"after_runs": 3, "value": " M core/api/smoke.py"}}
    result = world.run(extra={"reads_after": dirty})
    assert result.returncode != 0 and len(result.runs) == 3 and "not clean" in result.stderr


def test_a_caller_other_than_the_bound_one_stops_before_any_step(tmp_path):
    other = json.dumps({"core": {"account": "someone.else@example.invalid", "project": "ogilvy-trends-v2"}})
    result = PasteWorld(tmp_path).run(extra={"config_json": other})
    assert result.returncode != 0 and result.runs == [] and "Unexpected caller" in result.stderr
    impersonated = json.dumps({"core": {"account": "jhb.analytics@gmail.com", "project": "ogilvy-trends-v2"}, "auth": {"impersonate_service_account": "x@y.iam.gserviceaccount.com"}})
    result = PasteWorld(tmp_path / "imp").run(extra={"config_json": impersonated})
    assert result.returncode != 0 and result.runs == []


def test_a_freeze_readback_that_holds_no_manifest_hash_is_refused_by_promote(tmp_path):
    world = promote_world(tmp_path)
    world.readback("Freeze", None)
    result = world.run()
    assert result.returncode != 0 and "holds no manifest hash" in result.stderr


def test_retire_after_a_promotion_needs_a_rollback_even_when_a_smoke_readback_exists(tmp_path):
    world = PasteWorld(tmp_path, "Retire")
    world.readback("AfterSmoke")
    world.readback("AfterAgentPromotion")
    result = world.run()
    assert result.returncode != 0 and "Retire is refused" in result.stderr and result.runs == []


# Finding 2: the extract directory exists before tar and the build runs inside it

def test_f2_the_extract_directory_is_made_before_tar_and_the_build_runs_inside_it(tmp_path):
    result = PasteWorld(tmp_path).run()
    assert result.returncode == 0, result.stderr
    extract = result.run("extract")["argv"]
    target = Path(extract[extract.index("-C") + 1])
    assert target.name == "source" and target.is_dir() and list(target.iterdir()) == []
    assert result.run("build")["workdir"] == str(target)
    assert [c["workdir"] for c in result.runs if c["name"] != "build"] == [""] * (len(result.runs) - 1)


def test_f2_a_tar_whose_target_directory_is_missing_stops_the_paste_at_the_extract_step(tmp_path):
    world = PasteWorld(tmp_path, mutate_paste=lambda t: t.replace("    New-Item -ItemType Directory -Path $extract | Out-Null\n", ""))
    world.relock()
    result = world.run()
    assert result.returncode != 0 and result.names[-1] == "extract" and "build" not in result.names


def test_f2_the_extract_directory_is_not_reused_if_it_already_exists(tmp_path):
    world = PasteWorld(tmp_path, mutate_paste=lambda t: t.replace("$extract = Join-Path $Script:RunDir 'source'", "$extract = $Script:RunDir"))
    world.relock()
    result = world.run()
    assert result.returncode != 0 and "archive" in result.names and "extract" not in result.names


# Finding 5: DEPLOY, IDLE and the passcode are typed at the start, before any write

def typed_world(tmp_path, action="Candidate"):
    return promote_world(tmp_path) if action == "Promote" else PasteWorld(tmp_path, action)


def test_f5_candidate_asks_for_deploy_then_the_passcode_before_any_command_and_before_the_run_directory_exists(tmp_path):
    result = PasteWorld(tmp_path).run()
    assert result.returncode == 0, result.stderr
    asked = prompts(result)
    assert [(c["secure"], c["prompt"]) for c in asked] == [(False, "Type DEPLOY to continue"), (True, "Smoke passcode")]
    assert all(c["runs_so_far"] == 0 and c["run_dir_existed"] is False for c in asked)
    first_run = next(i for i, c in enumerate(result.calls) if c["kind"] == "run")
    assert max(i for i, c in enumerate(result.calls) if c["kind"] == "prompt") < first_run


def test_f5_promote_asks_for_idle_then_deploy_before_any_command_and_never_for_the_passcode(tmp_path):
    result = promote_world(tmp_path).run()
    assert result.returncode == 0, result.stderr
    asked = prompts(result)
    assert [(c["secure"], c["prompt"]) for c in asked] == [(False, "Type IDLE to continue"), (False, "Type DEPLOY to continue")]
    assert all(c["runs_so_far"] == 0 and c["run_dir_existed"] is False for c in asked)


@pytest.mark.parametrize("action", ["Rollback", "Retire"])
def test_f5_rollback_and_retire_ask_for_nothing(tmp_path, action):
    world = PasteWorld(tmp_path, action)
    if action == "Retire":
        world.readback("AfterRollback")
    result = world.run()
    assert result.returncode == 0, result.stderr
    assert prompts(result) == []


@pytest.mark.parametrize("typed", ["", "deploy", "DEPLOY ", " DEPLOY", "Deploy", "yes", "DEPLOY!"])
def test_f5_anything_but_the_exact_word_deploy_stops_candidate_before_any_command_or_file(tmp_path, typed):
    result = PasteWorld(tmp_path).run(extra={"words": {"DEPLOY": typed}})
    assert result.returncode != 0 and result.runs == [] and "NOT CONFIRMED" in result.stderr
    assert [c["prompt"] for c in prompts(result)] == ["Type DEPLOY to continue"]
    assert not (tmp_path / "releases" / RID / "runs").exists()


@pytest.mark.parametrize("word,typed", [("IDLE", "idle"), ("IDLE", ""), ("IDLE", "IDLE "), ("DEPLOY", "deploy"), ("DEPLOY", "")])
def test_f5_anything_but_the_exact_words_stops_promote_before_any_command_or_file(tmp_path, word, typed):
    result = promote_world(tmp_path).run(extra={"words": {word: typed}})
    assert result.returncode != 0 and result.runs == [] and "NOT CONFIRMED" in result.stderr
    assert not (tmp_path / "releases" / RID / "runs").exists()
    if word == "IDLE":
        assert [c["prompt"] for c in prompts(result)] == ["Type IDLE to continue"]


def test_f5_an_empty_passcode_stops_candidate_before_any_command(tmp_path):
    result = PasteWorld(tmp_path).run(typed="")
    assert result.returncode != 0 and result.runs == [] and "passcode" in result.stderr.lower()
    assert not (tmp_path / "releases" / RID / "runs").exists()


@pytest.mark.parametrize("action", ["Candidate", "Promote"])
def test_f5_without_an_interactive_console_candidate_and_promote_stop_before_any_external_call(tmp_path, action):
    result = typed_world(tmp_path, action).run(interactive=False)
    assert result.returncode != 0 and no_external_call(result) and prompts(result) == [] and "NOT INTERACTIVE" in result.stderr


def test_f5_a_receipt_with_every_boolean_true_is_not_enough_without_the_typed_words(tmp_path):
    result = PasteWorld(tmp_path).run(extra={"words": {"DEPLOY": "no"}})
    assert result.returncode != 0 and result.runs == []
    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["later_explicit_user_instruction"] is True and receipt["single_T1_smoke_authorized"] is True


def test_f5_the_passcode_is_never_echoed_or_written_anywhere(tmp_path):
    result = PasteWorld(tmp_path).run(inherited=INHERITED)
    files = "".join(p.read_text(encoding="utf-8", errors="replace") for p in tmp_path.rglob("*") if p.is_file() and p.name not in ("config.json", "driver.ps1"))
    for value in (TYPED, INHERITED):
        assert value not in result.stdout and value not in result.stderr and value not in files
    assert all(c["secure"] for c in prompts(result) if c["prompt"] == "Smoke passcode")


def paste_function(call, tmp_path, *, stdin=None, flags=("-NonInteractive",)):
    """Run the real Test-Interactive or Assert-Interactive of the paste in a real pwsh, with no double. The frame is written to
    a .ps1 in the test's temporary folder and run with -File, so the offline guard admits it whatever the frame holds."""
    import subprocess

    paste = ROOT / "core/setup/release/SERVICES-PASTE.ps1"
    frame = Path(tmp_path) / "frame.ps1"
    frame.write_text(f"$ErrorActionPreference = 'Stop'; . '{paste}' -Action Rollback -Lock x -Review x -Bindings x -Receipt x -DefinitionsOnly; "
                     f"try {{ {call}; 'RESULT:ok' }} catch {{ 'RESULT:' + $_.Exception.Message }}", encoding="utf-8", newline="\n")
    return subprocess.run(["pwsh", "-NoProfile", *flags, "-File", str(frame)], input=stdin, capture_output=True, encoding="utf-8", timeout=120)


def test_f5_the_real_interactivity_check_refuses_a_redirected_stdin_and_a_noninteractive_host(tmp_path):
    piped = paste_function("Assert-Interactive", tmp_path, stdin="DEPLOY\n", flags=())
    assert "RESULT:NOT INTERACTIVE" in piped.stdout, (piped.stdout, piped.stderr)
    noninteractive = paste_function("Read-Word 'DEPLOY'", tmp_path)
    assert "RESULT:" in noninteractive.stdout and "RESULT:ok" not in noninteractive.stdout, (noninteractive.stdout, noninteractive.stderr)


# Finding 3 at the paste: the declared removal list is read live and shown before the DEPLOY prompt

NAMES = ("F42_SYNTHETIC_DECLARED_ONE", "F42_SYNTHETIC_DECLARED_TWO")
DECLARE_CMD = "py -3.13 -B -m core.setup.release.declared_env_removals --service f42-agent"
DESCRIBE_CMD = "gcloud run services describe f42-agent --project ogilvy-trends-v2 --region us-central1 --format=json"


def with_names():
    env = [{"name": "F42_DATA", "value": "bigquery"}, *({"name": n, "value": "x"} for n in NAMES)]
    return {"describe_json": json.dumps({"spec": {"template": {"spec": {"containers": [{"env": env}]}}}}), "declared_matches": "\n".join(NAMES)}


def test_f3_candidate_reads_the_live_f42_agent_and_shows_the_matched_names_before_the_deploy_prompt(tmp_path):
    result = PasteWorld(tmp_path).run(extra=with_names())
    assert result.returncode == 0, result.stderr
    reads = [" ".join(c["argv"]) for c in result.calls if c["kind"] == "read"]
    assert reads.index(DESCRIBE_CMD) < reads.index(DECLARE_CMD)
    matcher = next(c for c in result.calls if c["kind"] == "read" and " ".join(c["argv"]) == DECLARE_CMD)
    assert json.loads(matcher["input"])["spec"]["template"]["spec"]["containers"][0]["env"][1]["name"] == NAMES[0]
    first_prompt = next(i for i, c in enumerate(result.calls) if c["kind"] == "prompt")
    last_read = max(i for i, c in enumerate(result.calls) if c["kind"] == "read" and " ".join(c["argv"]) in (DESCRIBE_CMD, DECLARE_CMD))
    assert last_read < first_prompt
    for name in NAMES:
        assert name in result.stdout
    assert "2 environment variable" in result.stdout


def test_f3_the_names_are_shown_on_the_screen_only_and_reach_no_file_or_log_of_the_release(tmp_path):
    PasteWorld(tmp_path).run(extra=with_names())
    kept = "".join(p.read_text(encoding="utf-8", errors="replace") for p in (tmp_path / "releases").rglob("*") if p.is_file())
    for name in NAMES:
        assert name not in kept


def test_f3_when_none_of_the_declared_names_is_live_the_paste_says_so_and_goes_on(tmp_path):
    result = PasteWorld(tmp_path).run()
    assert result.returncode == 0 and "no declared environment variable" in result.stdout


def test_f3_a_matcher_that_fails_stops_before_any_prompt_or_command(tmp_path):
    result = PasteWorld(tmp_path).run(reads={DECLARE_CMD: "!fail"}, extra=with_names())
    assert result.returncode != 0 and prompts(result) == [] and result.runs == []


@pytest.mark.parametrize("action", ["Promote", "Rollback", "Retire"])
def test_f3_only_candidate_reads_the_declared_removal(tmp_path, action):
    world = promote_world(tmp_path) if action == "Promote" else PasteWorld(tmp_path, action)
    if action == "Retire":
        world.readback("AfterRollback")
    result = world.run()
    assert result.returncode == 0, result.stderr
    assert not [c for c in result.calls if c["kind"] == "read" and " ".join(c["argv"]) in (DESCRIBE_CMD, DECLARE_CMD)]


def test_f3_the_candidate_deploy_step_is_still_the_one_script_and_carries_no_name(tmp_path):
    result = PasteWorld(tmp_path).run(extra=with_names())
    argv = result.run("deploy-candidate")["argv"]
    assert argv[:2] == ["bash", "core/api/deploy_candidate.sh"] and not any(n in " ".join(argv) for n in NAMES)


# Finding 7: the passcode reaches the smoke child and nothing else

def test_f7_the_checkout_is_asserted_before_the_passcode_is_set_so_no_git_child_inherits_it(tmp_path):
    result = PasteWorld(tmp_path).run()
    smoke_at = next(i for i, c in enumerate(result.calls) if c["kind"] == "run" and c["name"] == "smoke")
    before = result.calls[smoke_at - 1]
    assert before["kind"] == "read" and before["argv"] == ["git", "status", "--porcelain=v1"] and before["passcode_sha"] == ""
    assert result.calls[smoke_at]["passcode_sha"] == sha(TYPED)
    text = (ROOT / "core/setup/release/SERVICES-PASTE.ps1").read_text(encoding="utf-8")
    assert "never passed to a child" not in text and "the smoke child alone" in text


# Round 2 review of the release paste (R-2, R-3, R-5, R-7, R-9, R-10)

def test_r2_a_function_named_read_host_cannot_stand_in_for_the_typed_word(tmp_path):
    # Read-Host is called by its plain name and the resolution check runs before every prompt, so a function of that name, or a
    # function named with the module prefix, is refused before the word is asked for.
    shadowed = paste_function("function global:Read-Host { 'DEPLOY' }; Read-Word 'DEPLOY'", tmp_path)
    assert "RESULT:ok" not in shadowed.stdout and GUARD_REFUSED + "Read-Host'" in shadowed.stdout, (shadowed.stdout, shadowed.stderr)
    passcode = paste_function("function global:Read-Host { ConvertTo-SecureString 'x' -AsPlainText -Force }; Read-Passcode", tmp_path)
    assert "RESULT:ok" not in passcode.stdout and GUARD_REFUSED + "Read-Host'" in passcode.stdout, (passcode.stdout, passcode.stderr)


# CC-1: an alias is resolved before any function, in every scope, so an alias over a paste function or over Read-Host could
# stand in for a typed word with no console. Invoke-Release refuses first, whatever the alias form and whatever the target.

SHADOWABLE = ("Read-Typed", "Read-Word", "Read-Passcode", "Test-Interactive", "Assert-Interactive", "Get-UtcNow", "Test-QuietWindow",
              "Get-InflightCount", "Read-Native", "Run-Logged", "Step", "Read-Host")
ALIAS_FORMS = {
    "global": "Set-Alias -Scope Global -Name {n} -Value Get-Date; Invoke-Release",
    "script": "Set-Alias -Scope Script -Name {n} -Value Get-Date; Invoke-Release",
    "child scope": "& {{ Set-Alias -Name {n} -Value Get-Date; Invoke-Release }}",
    "new-alias read-only": "New-Alias -Scope Global -Name {n} -Value Get-Date -Option ReadOnly; Invoke-Release",
    "alias over a function": "function global:Fake {{ 'DEPLOY' }}; Set-Alias -Scope Global -Name {n} -Value Fake; Invoke-Release",
}
REFUSED = GUARD_REFUSED


@pytest.mark.parametrize("form", sorted(ALIAS_FORMS))
@pytest.mark.parametrize("name", SHADOWABLE)
def test_cc1_an_alias_over_a_paste_function_or_read_host_refuses_the_run(tmp_path, name, form):
    done = paste_function(ALIAS_FORMS[form].format(n=name), tmp_path)
    assert REFUSED in done.stdout, (name, form, done.stdout, done.stderr)


@pytest.mark.parametrize("name", ["Read-Typed", "Read-Host"])
def test_cc1_the_refusal_holds_when_get_alias_is_itself_replaced_by_a_function_or_an_alias(tmp_path, name):
    blinded = paste_function(f"function global:Get-Alias {{ }}; Set-Alias -Scope Global -Name {name} -Value Get-Date; Invoke-Release", tmp_path)
    assert REFUSED in blinded.stdout, (blinded.stdout, blinded.stderr)
    renamed = paste_function(f"Set-Alias -Scope Global -Name Get-Alias -Value Get-Date; Set-Alias -Scope Global -Name {name} -Value Get-Date; Invoke-Release", tmp_path)
    assert REFUSED in renamed.stdout, (renamed.stdout, renamed.stderr)


def test_cc1_an_alias_that_shadows_nothing_of_the_paste_is_not_refused(tmp_path):
    done = paste_function("Set-Alias -Scope Global -Name Show-Elsewhere -Value Get-Date; Set-Alias -Scope Global -Name ll -Value Get-ChildItem; Invoke-Release", tmp_path)
    assert "does not resolve" not in done.stdout and "RESULT:NOT EXECUTABLE: the packet lock is missing" in done.stdout, (done.stdout, done.stderr)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("name", ["Read-Typed", "Test-Interactive", "Get-UtcNow", "Read-Host"])
def test_cc1_with_a_valid_packet_an_alias_stops_every_action_before_any_call_prompt_or_file(tmp_path, action, name):
    world = promote_world(tmp_path) if action == "Promote" else PasteWorld(tmp_path, action)
    if action == "Retire":
        world.readback("AfterRollback")
    result = world.run(extra={"aliases": [{"name": name, "value": "Get-Date"}]})
    assert result.returncode != 0 and "does not resolve to a function of this paste" in result.stderr, (result.stdout, result.stderr)
    assert result.external == [] and prompts(result) == []
    assert not (world.release_dir / "runs").exists()


def test_cc1_the_same_packet_without_the_alias_runs_to_the_end(tmp_path):
    result = PasteWorld(tmp_path).run(extra={"aliases": []})
    assert result.returncode == 0, result.stderr


def test_r2_the_paste_calls_the_prompt_cmdlet_by_its_plain_name_and_no_command_by_a_qualified_name():
    # A name with a backslash is refused by the resolution check, so the paste must not use one.
    text = (ROOT / "core/setup/release/SERVICES-PASTE.ps1").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert len(re.findall(r"(?<![\\\w'-])Read-Host -", code)) == 2
    assert not re.findall(r"[A-Za-z]\\[A-Za-z]+-[A-Za-z]+", code.replace("System32\\tar.exe", ""))


# RV-1: the typed words are Albert's. The paste resolves every command name it invokes, taken from its own syntax tree, and
# refuses a session in which one is an alias, a function of the caller, a lookup hook's answer or a cmdlet from elsewhere.

FAKES = r"""
function global:FakeTyped { param([string]$Prompt, [switch]$Secure) if ($Secure) { return (ConvertTo-SecureString 'x' -AsPlainText -Force) }; return [regex]::Match($Prompt, 'Type (\w+)').Groups[1].Value }
function global:FakeTrue { $true }
function global:Nothing { }
"""
TWO = "Set-Alias -Scope Global -Name Read-Typed -Value FakeTyped; Set-Alias -Scope Global -Name Test-Interactive -Value FakeTrue\n"
HOOK = ("$ExecutionContext.InvokeCommand.PreCommandLookupAction = { param($n, $e) if ($n -eq 'Read-Typed') { $e.Command = Get-Command FakeTyped; $e.StopSearch = $true }; "
        "if ($n -eq 'Test-Interactive') { $e.Command = Get-Command FakeTrue; $e.StopSearch = $true } }\n")
ATTACKS = {
    "B1 alias over a guard function, plus the two": "Set-Alias -Scope Global -Name Assert-NoShadowAlias -Value Nothing\n" + TWO,
    "B2 alias over Confirm-Action": "Set-Alias -Scope Global -Name Confirm-Action -Value Nothing\n",
    "B3 function named with the module prefix over Get-Alias": "Set-Item -Path 'function:global:Microsoft.PowerShell.Utility\\Get-Alias' -Value { }\n" + TWO,
    "B4 alias named with the module prefix over Get-Alias": "Set-Alias -Scope Global -Name 'Microsoft.PowerShell.Utility\\Get-Alias' -Value Nothing\n" + TWO,
    "B5 a command lookup hook and no alias": HOOK,
    "B6 a function over Test-Path that makes the aliases later": ("$global:Armed = $false\nfunction global:Test-Path { if (-not $global:Armed) { $global:Armed = $true; "
        "Set-Alias -Scope Global -Name Read-Typed -Value FakeTyped; Set-Alias -Scope Global -Name Test-Interactive -Value FakeTrue }; Microsoft.PowerShell.Management\\Test-Path @args }\n"),
    "C2 the two aliases with AllScope": "Set-Alias -Scope Global -Name Read-Typed -Value FakeTyped -Option AllScope\nSet-Alias -Scope Global -Name Test-Interactive -Value FakeTrue -Option AllScope\n",
    "C1 the two aliases alone": TWO,
}
GUARD_TEXT = "does not resolve to a function of this paste"


def attack_world(tmp_path, action):
    world = promote_world(tmp_path) if action == "Promote" else PasteWorld(tmp_path, action)
    if action == "Retire":
        world.readback("AfterRollback")
    return world


@pytest.mark.parametrize("action", ["Promote", "Candidate"])
@pytest.mark.parametrize("label", sorted(ATTACKS))
def test_rv1_an_attack_on_the_real_console_gate_is_refused_before_any_call_prompt_or_file(tmp_path, label, action):
    # The Read-Typed and Test-Interactive doubles are absent, so the real console gate is what the attack has to get past.
    world = attack_world(tmp_path, action)
    result = world.run(extra={"real_console": True, "attack": FAKES + ATTACKS[label]})
    expected = "command lookup hook" if label.startswith("B5") else GUARD_TEXT
    assert result.returncode != 0 and expected in result.stderr, (label, result.stdout, result.stderr)
    assert result.external == [] and prompts(result) == [], label
    assert not (world.release_dir / "runs").exists()


@pytest.mark.parametrize("action", ["Promote", "Candidate"])
def test_rv1_the_control_without_an_attack_is_refused_at_the_console_gate_not_by_the_resolution_check(tmp_path, action):
    result = attack_world(tmp_path, action).run(extra={"real_console": True})
    assert result.returncode != 0 and "NOT INTERACTIVE" in result.stderr and GUARD_TEXT not in result.stderr, (result.stdout, result.stderr)
    assert result.external == [] or all(c["kind"] == "read" for c in result.external)


@pytest.mark.parametrize("name", ["Test-PromoteInputs", "Assert-ManifestIsTheFrozenOne"])
def test_rv1_b7_an_alias_over_a_promote_gate_stops_promote_before_any_traffic_move(tmp_path, name):
    world = PasteWorld(tmp_path, "Promote")
    world.frozen_manifest()
    control = world.run()
    assert control.returncode != 0 and "A bound file for Promote is missing" in control.stderr and control.external == []
    attacked = world.run(extra={"attack": FAKES + f"Set-Alias -Scope Global -Name {name} -Value Nothing\n"})
    assert attacked.returncode != 0 and GUARD_TEXT in attacked.stderr, (attacked.stdout, attacked.stderr)
    assert attacked.external == [] and not any(n.startswith("promote-") for n in attacked.names)


@pytest.mark.parametrize("hook", ["PreCommandLookupAction", "PostCommandLookupAction", "CommandNotFoundAction"])
def test_rv1_a_command_lookup_hook_of_any_kind_refuses_the_run(tmp_path, hook):
    world = attack_world(tmp_path, "Promote")
    result = world.run(extra={"attack": f"$ExecutionContext.InvokeCommand.{hook} = {{ param($n, $e) }}\n"})
    assert result.returncode != 0 and "lookup hook" in result.stderr and hook in result.stderr, (result.stdout, result.stderr)
    assert result.external == [] and prompts(result) == []


STEP_ATTACK = {"Candidate": (9, "Assert-Source", "deploy-candidate", "Candidate"), "Promote": (4, "Assert-Source", "promote-f42-api", "Promote"),
               "Rollback": (1, "Get-A80", "restore-f42-api", "Rollback"), "Candidate smoke": (11, "Assert-Source", "smoke", "Candidate")}


@pytest.mark.parametrize("label", sorted(STEP_ATTACK))
def test_rv1_an_alias_made_during_the_run_stops_the_next_traffic_move_or_deploy_step(tmp_path, label):
    at, alias, step, action = STEP_ATTACK[label]
    world = attack_world(tmp_path, action)
    result = world.run(extra={"inject_at_run": at, "inject": FAKES + f"Set-Alias -Scope Global -Name {alias} -Value Nothing\n"})
    assert result.returncode != 0 and GUARD_TEXT in result.stderr, (result.stdout, result.stderr)
    assert step not in result.names and len(result.runs) == at, result.names


def check_frame(call, tmp_path):
    return paste_function(call, tmp_path)


def checked_names(tmp_path):
    done = check_frame("& $Script:ResolutionCheck; 'NAMES:' + (@($Script:CheckedNames) + @($Script:NativeNames) -join ',')", tmp_path)
    assert "RESULT:ok" in done.stdout, (done.stdout, done.stderr)
    return next(line for line in done.stdout.splitlines() if line.startswith("NAMES:"))[6:].split(",")


def test_rv1_the_names_come_from_the_paste_syntax_tree_and_cover_what_it_calls(tmp_path):
    names = checked_names(tmp_path)
    for expected in ("Test-Path", "Get-Content", "Read-Host", "Get-Alias", "Confirm-Action", "Invoke-Release", "Read-Typed", "Traffic",
                     "Assert-ManifestIsTheFrozenOne", "Start-Sleep", "Tee-Object", "gcloud", "git", "py", "bash"):
        assert expected in names, expected
    assert len(set(names)) == len(names) and not [n for n in names if "\\" in n]


@pytest.mark.parametrize("kind", ["alias", "function"])
def test_rv1_every_name_the_paste_invokes_is_refused_when_an_alias_or_a_caller_function_takes_it(tmp_path, kind):
    names = checked_names(tmp_path)
    make = "Set-Alias -Name $n -Value Get-Command" if kind == "alias" else "Set-Item -Path ('function:' + $n) -Value { }"
    body = "& { param($n) " + make + "; try { & $Script:ResolutionCheck; $script:missed += $n } catch { } } $n"
    call = "$script:missed = @(); foreach ($n in @(" + ",".join("'" + n + "'" for n in names) + ")) { " + body + " }; if ($script:missed.Count) { throw ('NOT REFUSED: ' + ($script:missed -join ',')) }"
    done = check_frame(call, tmp_path)
    assert "RESULT:ok" in done.stdout, (kind, done.stdout, done.stderr)


def test_rv1_a_function_named_with_the_module_prefix_over_a_checked_name_is_refused(tmp_path):
    done = check_frame("Set-Item -Path 'function:global:Microsoft.PowerShell.Utility\\Read-Host' -Value { 'DEPLOY' }; Read-Word 'DEPLOY'", tmp_path)
    assert "RESULT:ok" not in done.stdout and GUARD_REFUSED in done.stdout, (done.stdout, done.stderr)
    done = check_frame("Set-Item -Path 'function:global:Microsoft.PowerShell.Utility\\Read-Host' -Value { 'DEPLOY' }; & $Script:ResolutionCheck", tmp_path)
    assert GUARD_REFUSED + "Microsoft.PowerShell.Utility\\Read-Host'" in done.stdout, (done.stdout, done.stderr)


def test_rv1_the_default_backslash_function_of_a_clean_console_is_not_refused(tmp_path):
    done = check_frame("& $Script:ResolutionCheck", tmp_path)
    assert "RESULT:ok" in done.stdout, (done.stdout, done.stderr)


def test_rv1_a_function_of_the_caller_is_refused_unless_it_is_a_declared_test_double_of_a_definitions_only_load(tmp_path):
    refused = check_frame("function Test-Interactive { $true }; & $Script:ResolutionCheck", tmp_path)
    assert GUARD_REFUSED + "Test-Interactive'" in refused.stdout, (refused.stdout, refused.stderr)
    declared = check_frame("function Test-Interactive { $true }; $Script:TestDoubles = @('Test-Interactive'); & $Script:ResolutionCheck", tmp_path)
    assert "RESULT:ok" in declared.stdout, (declared.stdout, declared.stderr)


def test_rv1_a_name_that_resolves_to_nothing_or_is_written_with_a_module_prefix_is_refused(tmp_path):
    unknown = PasteWorld(tmp_path / "u", mutate_paste=lambda t: t + "\nfunction Show-Extra { Frobnicate-Widget }\n")
    result = unknown.run()
    assert result.returncode != 0 and "'Frobnicate-Widget'" in result.stderr and result.external == [], (result.stdout, result.stderr)
    foreign = PasteWorld(tmp_path / "f", mutate_paste=lambda t: t + "\nfunction Show-Extra { Microsoft.PowerShell.Utility\\Get-Date }\n")
    qualified = foreign.run()
    assert qualified.returncode != 0 and "Microsoft.PowerShell.Utility\\Get-Date" in qualified.stderr and qualified.external == [], (qualified.stdout, qualified.stderr)


def test_rv1_a_command_added_to_the_paste_is_checked_without_any_list_being_edited(tmp_path):
    world = PasteWorld(tmp_path, mutate_paste=lambda t: t + "\nfunction Show-Extra { Get-Date }\n")
    clean = world.run()
    assert clean.returncode == 0, clean.stderr
    again = world.run(extra={"aliases": [{"name": "Get-Date", "value": "Get-Process"}]})
    assert again.returncode != 0 and "'Get-Date'" in again.stderr and again.external == [], (again.stdout, again.stderr)


def test_rv1_a_function_the_paste_defines_but_never_calls_is_checked_too(tmp_path):
    world = PasteWorld(tmp_path, mutate_paste=lambda t: t + "\nfunction Show-Extra { 1 }\n")
    assert world.run().returncode == 0
    taken = world.run(extra={"aliases": [{"name": "Show-Extra", "value": "Get-Process"}]})
    assert taken.returncode != 0 and "'Show-Extra'" in taken.stderr and taken.external == [], (taken.stdout, taken.stderr)


def test_rv1_get_alias_is_checked_although_the_paste_no_longer_calls_it(tmp_path):
    done = check_frame("Set-Alias -Name Get-Alias -Value Get-Process; & $Script:ResolutionCheck", tmp_path)
    assert GUARD_REFUSED + "Get-Alias'" in done.stdout, (done.stdout, done.stderr)


def test_rv1_declared_test_doubles_count_only_in_a_definitions_only_load(tmp_path):
    import subprocess

    paste = ROOT / "core/setup/release/SERVICES-PASTE.ps1"
    frame = Path(tmp_path) / "real_load.ps1"
    frame.write_text(f"$ErrorActionPreference = 'Stop'; try {{ . '{paste}' -Action Rollback -Lock x -Review x -Bindings x -Receipt x }} catch {{ }}; "
                     "function Test-Interactive { $true }; $Script:TestDoubles = @('Test-Interactive'); "
                     "try { & $Script:ResolutionCheck; 'RESULT:ok' } catch { 'RESULT:' + $_.Exception.Message }", encoding="utf-8", newline="\n")
    done = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(frame)], stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", timeout=120)
    assert GUARD_REFUSED + "Test-Interactive'" in done.stdout, (done.stdout, done.stderr)


@pytest.mark.parametrize("native", ["gcloud", "git", "py", "bash"])
def test_rv1_a_function_or_an_alias_over_a_program_the_paste_runs_is_refused(tmp_path, native):
    world = attack_world(tmp_path, "Rollback")
    as_function = world.run(extra={"attack": f"function global:{native} {{ }}\n"})
    assert as_function.returncode != 0 and f"'{native}'" in as_function.stderr and as_function.external == [], (as_function.stdout, as_function.stderr)
    as_alias = world.run(extra={"attack": FAKES + f"Set-Alias -Scope Global -Name {native} -Value Nothing\n"})
    assert as_alias.returncode != 0 and f"'{native}'" in as_alias.stderr and as_alias.external == [], (as_alias.stdout, as_alias.stderr)


def test_rv1_a_prompt_is_never_made_in_a_session_where_read_host_is_not_the_cmdlet(tmp_path):
    prompt = check_frame("function global:Read-Host { 'DEPLOY' }; Read-Typed 'Type DEPLOY to continue'", tmp_path)
    assert "RESULT:ok" not in prompt.stdout and GUARD_REFUSED + "Read-Host'" in prompt.stdout, (prompt.stdout, prompt.stderr)


def test_rv1_the_resolution_check_calls_no_powershell_command_so_none_can_be_replaced(tmp_path):
    done = check_frame("$found = $Script:ResolutionCheck.Ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true); 'COMMANDS:' + $found.Count", tmp_path)
    assert "COMMANDS:0" in done.stdout, (done.stdout, done.stderr)


@pytest.mark.parametrize("action", ["Promote", "Candidate"])
def test_rv1_the_check_runs_again_inside_confirm_action_just_before_the_console_gate(tmp_path, action):
    # Moving to the repository raises LocationChangedAction, which here makes the two aliases after the first check has passed.
    world = attack_world(tmp_path, action)
    attack = FAKES + "$ExecutionContext.InvokeCommand.LocationChangedAction = { " + TWO.strip() + " }\n"
    result = world.run(extra={"real_console": True, "attack": attack})
    assert result.returncode != 0 and GUARD_TEXT in result.stderr, (result.stdout, result.stderr)
    assert result.external == [] and prompts(result) == [] and not (world.release_dir / "runs").exists()


def paste_run(prelude, tmp_path):
    """The paste run for real (not a definitions-only load) from a frame script, after a prelude of the caller's own making."""
    import subprocess

    paste = ROOT / "core/setup/release/SERVICES-PASTE.ps1"
    frame = Path(tmp_path) / "real_frame.ps1"
    frame.write_text(f"$ErrorActionPreference = 'Stop'; {prelude}; try {{ & '{paste}' -Action Rollback -Lock x -Review x -Bindings x -Receipt x; 'RESULT:ok' }} "
                     f"catch {{ 'RESULT:' + $_.Exception.Message }}", encoding="utf-8", newline="\n")
    return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(frame)], stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", timeout=120)


def test_rv1_a_real_run_from_a_clean_console_passes_the_check_and_stops_only_at_the_missing_lock(tmp_path):
    done = paste_run("$null = 1", tmp_path)
    assert "RESULT:NOT EXECUTABLE: the packet lock is missing" in done.stdout and "does not resolve" not in done.stdout, (done.stdout, done.stderr)


@pytest.mark.parametrize("name", ["Invoke-Release", "Read-Host", "Confirm-Action", "Test-Path", "Step"])
def test_rv1_a_real_run_refuses_an_alias_over_a_name_it_invokes_before_it_reads_the_lock(tmp_path, name):
    done = paste_run(f"Set-Alias -Scope Global -Name {name} -Value Get-Date", tmp_path)
    assert GUARD_REFUSED + name + "'" in done.stdout and "packet lock" not in done.stdout, (done.stdout, done.stderr)


@pytest.mark.parametrize("module", ["fake", "Microsoft.PowerShell.Fake"])
def test_rv1_a_cmdlet_of_a_later_imported_module_over_a_checked_name_is_refused_even_when_its_module_name_looks_right(tmp_path, module):
    dll = (Path(tmp_path) / f"{module}.dll").as_posix()
    source = 'using System.Management.Automation; [Cmdlet("Test", "Path")] public class FakeTestPath : PSCmdlet { protected override void ProcessRecord() { WriteObject(true); } }'
    done = check_frame(f"Add-Type -TypeDefinition '{source}' -OutputAssembly '{dll}'; Import-Module '{dll}'; & $Script:ResolutionCheck", tmp_path)
    assert GUARD_REFUSED + "Test-Path'" in done.stdout and "(Cmdlet)" in done.stdout, (done.stdout, done.stderr)


def test_r3_the_extract_calls_the_system_tar_by_its_full_path_not_the_first_tar_on_path(tmp_path):
    result = PasteWorld(tmp_path).run()
    tar = result.run("extract")["argv"][0]
    if os.name == "nt":
        assert re.fullmatch(r"(?i)[a-z]:\\[^\\]+\\System32\\tar\.exe", tar), tar
    assert plan.matching_entries(result.run("extract")["argv"]) == ["archive"]


def logged_calls(result):
    return [c for c in result.calls if c["kind"] in ("read", "run")]


@pytest.mark.parametrize("action", ["Candidate", "Promote", "Rollback", "Retire"])
def test_r5_r9_every_child_runs_with_gcloud_file_logging_off_no_bytecode_and_no_git_index_lock(tmp_path, action):
    world = promote_world(tmp_path) if action == "Promote" else PasteWorld(tmp_path, action)
    if action == "Retire":
        world.readback("AfterRollback")
    result = world.run()
    assert result.returncode == 0, result.stderr
    calls = logged_calls(result)
    assert len(calls) > 3
    for call in calls:
        assert call["env"] == {"file_logging": "1", "bytecode": "1", "locks": "0"}, (call.get("name") or call["argv"], call["env"])
    assert [c for c in result.calls if c["kind"] == "env_at_end"] == [{"kind": "env_at_end", "env": {"file_logging": "", "bytecode": "", "locks": ""}}]


def test_r9_the_module_read_runs_without_writing_bytecode(tmp_path):
    result = PasteWorld(tmp_path).run()
    argv = next(c["argv"] for c in result.calls if c["kind"] == "read" and "core.setup.release.declared_env_removals" in c["argv"])
    assert argv == ["py", "-3.13", "-B", "-m", "core.setup.release.declared_env_removals", "--service", "f42-agent"]


# R-7: the in-flight count comes before IDLE, and a count that is refused or above zero asks for IDLE again

INFLIGHT = "core/setup/durable_effects_check.py"


def count_read(result):
    return next(i for i, c in enumerate(result.calls) if c["kind"] == "read" and c["argv"][2:3] == [INFLIGHT])


def test_r7_promote_shows_the_inflight_count_before_it_asks_for_idle(tmp_path):
    result = promote_world(tmp_path).run()
    assert result.returncode == 0, result.stderr
    first_prompt = next(i for i, c in enumerate(result.calls) if c["kind"] == "prompt")
    assert count_read(result) < first_prompt
    assert "In-flight Asks: 0" in result.stdout
    assert [c["prompt"] for c in prompts(result)] == ["Type IDLE to continue", "Type DEPLOY to continue"]


def test_r7_the_count_is_read_with_the_bound_checker_into_a_folder_outside_the_release_directory(tmp_path):
    world = promote_world(tmp_path)
    result = world.run()
    argv = result.calls[count_read(result)]["argv"]
    evidence = Path(argv[argv.index("--evidence") + 1])
    assert argv[argv.index("--bindings") + 1] == str(world.bindings) and "inflight-asks" in argv
    assert world.release_dir not in evidence.parents and not evidence.exists()  # nothing is left behind
    assert plan.matching_entries(argv) == ["checker"]


@pytest.mark.parametrize("extra, said", [({"inflight_running": 2}, "2 Ask(s) are running"), ({"inflight_refused": True}, "could not be counted")])
def test_r7_a_running_or_refused_count_asks_for_idle_a_second_time(tmp_path, extra, said):
    result = promote_world(tmp_path).run(extra=extra)
    assert result.returncode == 0, result.stderr
    assert said in result.stdout
    assert [c["prompt"] for c in prompts(result)] == ["Type IDLE to continue", "Type IDLE to continue", "Type DEPLOY to continue"]


@pytest.mark.parametrize("extra", [{"inflight_running": 1}, {"inflight_refused": True}])
def test_r7_the_second_idle_must_be_typed_exactly_too(tmp_path, extra):
    result = promote_world(tmp_path).run(extra={**extra, "words": {"IDLE": ["IDLE", "idle"]}})
    assert result.returncode != 0 and result.runs == [] and "NOT CONFIRMED" in result.stderr
    assert [c["prompt"] for c in prompts(result)] == ["Type IDLE to continue", "Type IDLE to continue"]


def test_r7_a_zero_count_asks_for_idle_once_and_the_candidate_does_not_count(tmp_path):
    result = PasteWorld(tmp_path).run()
    assert not [c for c in result.calls if c["kind"] == "read" and c["argv"][2:3] == [INFLIGHT]]


# R-10: the second quiet-window check after the typed words, and the read of a description that is not an object

def test_r10_a_quiet_window_that_goes_stale_while_the_words_are_typed_stops_promote_before_any_command(tmp_path):
    import datetime as dt

    verified = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=12)).isoformat()
    result = promote_world(tmp_path).run(quiet=verified, extra={"minutes_per_prompt": 1.6})
    assert result.returncode != 0 and result.runs == [] and "older than 15 minutes" in result.stderr
    assert [c["prompt"] for c in prompts(result)] == ["Type IDLE to continue", "Type DEPLOY to continue"]  # both words were typed first
    fresh = promote_world(tmp_path / "fresh").run(quiet=verified, extra={"minutes_per_prompt": 0.2})
    assert fresh.returncode == 0, fresh.stderr


# W4-R3: the programs the paste runs (gcloud, git, py, bash) are found as programs in their install folders, never by name on PATH,
# a script of the same name is never run, and the path that was found is the one that is called.

NATIVES = ("gcloud", "git", "py", "bash")
ON_WINDOWS = os.name == "nt"
ECHO_ARGV = "import json, sys\nsys.stdout.write(json.dumps(sys.argv[1:]))\n"
FORWARD_LINE = '"{python}" "%~dp0echo_argv.py" %* & goto lastline 2>NUL || "%COMSPEC%" /C exit 0'


def native_file(name):
    if not ON_WINDOWS:
        return name
    return "gcloud.cmd" if name == "gcloud" else name + ".exe"


def write_native(folder, name):
    """A stand-in for one program. On Windows gcloud is a real command file with the forwarding line of the Cloud SDK's own gcloud.cmd,
    so cmd.exe parses the arguments as it does for the real one; every other program is an empty file, enough to be found."""
    import sys

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / native_file(name)
    if name != "gcloud":
        target.write_bytes(b"")
    elif ON_WINDOWS:
        (folder / "echo_argv.py").write_text(ECHO_ARGV, encoding="utf-8")
        target.write_text("@echo off\nSETLOCAL\n" + FORWARD_LINE.format(python=sys.executable) + "\n:lastline\n\"%COMSPEC%\" /C exit %ERRORLEVEL%\n",
                          encoding="ascii", newline="\r\n")
    else:
        (folder / "echo_argv.py").write_text(ECHO_ARGV, encoding="utf-8")
        target.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{folder / "echo_argv.py"}" "$@"\n', encoding="utf-8", newline="\n")
    if not ON_WINDOWS:
        target.chmod(0o755)
    return target


def plant_scripts_and_programs(folder, marker):
    """Everything a hostile PATH could offer ahead of the install folders: a script for each program, and a program of the same name
    that records that it ran. Whatever runs leaves the marker file."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name in NATIVES:
        (folder / f"{name}.ps1").write_text(f"Set-Content -LiteralPath '{marker}' -Value ran\n", encoding="utf-8")
        if ON_WINDOWS:
            (folder / f"{name}.cmd").write_text(f'@echo off\necho ran> "{marker}"\n', encoding="ascii", newline="\r\n")
        else:
            (folder / name).write_text(f'#!/bin/sh\necho ran > "{marker}"\n', encoding="utf-8", newline="\n")
            (folder / name).chmod(0o755)


def quoted(path):
    return "'" + str(path).replace("'", "''") + "'"


def native_frame(tmp_path, folders, call, *, prefix=""):
    """A definitions-only load of the real paste with the install folders named by the test, then the call."""
    table = "@{" + "; ".join(f"{name} = @(" + ",".join(quoted(f) for f in dirs) + ")" for name, dirs in folders.items()) + "}"
    return paste_function(f"{prefix}$Script:TestNativeFolders = {table}; {call}", tmp_path)


def found_paths(done):
    return {line[5:].split("=", 1)[0]: line.split("=", 1)[1] for line in done.stdout.splitlines() if line.startswith("PATH:")}


def test_w4r3_each_program_is_found_as_a_program_in_its_install_folder_and_not_by_name_on_path(tmp_path):
    good, planted, marker = tmp_path / "good", tmp_path / "planted", tmp_path / "ran.txt"
    for name in NATIVES:
        write_native(good, name)
    plant_scripts_and_programs(planted, marker)
    prefix = f"$env:PATH = {quoted(planted)} + [IO.Path]::PathSeparator + $env:PATH; "
    done = native_frame(tmp_path, {name: [good] for name in NATIVES},
                        "foreach ($n in 'gcloud','git','py','bash') { 'PATH:' + $n + '=' + (Get-NativePath $n) }", prefix=prefix)
    assert "RESULT:ok" in done.stdout, (done.stdout, done.stderr)
    assert {k: os.path.normcase(v) for k, v in found_paths(done).items()} == {name: os.path.normcase(str(good / native_file(name))) for name in NATIVES}
    assert not marker.exists()


def test_w4r3_the_first_install_folder_that_holds_the_program_wins_and_a_later_one_is_not_used(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    write_native(second, "git")
    done = native_frame(tmp_path, {"git": [first, second]}, "'PATH:git=' + (Get-NativePath 'git')")
    assert os.path.normcase(found_paths(done)["git"]) == os.path.normcase(str(second / native_file("git"))), (done.stdout, done.stderr)
    write_native(first, "git")
    done = native_frame(tmp_path, {"git": [first, second]}, "'PATH:git=' + (Get-NativePath 'git')")
    assert os.path.normcase(found_paths(done)["git"]) == os.path.normcase(str(first / native_file("git"))), (done.stdout, done.stderr)


@pytest.mark.parametrize("kind", ["alias", "function"])
def test_w4r3_an_alias_or_function_named_by_the_full_install_path_is_ignored_and_the_program_is_returned(tmp_path, kind):
    """The lookup asks for CommandTypes Application only. Asked for All, the same name returns the alias or function and not the
    program, so a regression to All is caught here: the path returned must be the program's, and nothing the alias or function
    stands for may run."""
    good, marker = tmp_path / "good", tmp_path / "ran.txt"
    write_native(good, "gcloud")
    full = str(good / native_file("gcloud"))
    if kind == "alias":
        plant = f"Set-Alias -Scope Global -Name {quoted(full)} -Value Write-Output; "
    else:
        plant = (f"$n = {quoted(full)}; Set-Item -Path ('function:global:' + $n) "
                 f"-Value {{ Set-Content -LiteralPath {quoted(marker)} -Value ran }}; ")
    done = native_frame(tmp_path, {"gcloud": [good]}, "'PATH:gcloud=' + (Get-NativePath 'gcloud')", prefix=plant)
    assert "RESULT:ok" in done.stdout, (kind, done.stdout, done.stderr)
    assert os.path.normcase(found_paths(done)["gcloud"]) == os.path.normcase(full), (kind, done.stdout, done.stderr)
    assert not marker.exists()


@pytest.mark.parametrize("label", ["a script of the name and no program", "an empty folder", "a folder that does not exist", "a script on PATH and no program"])
def test_w4r3_a_script_is_never_taken_for_the_program_and_nothing_is_found_by_name_on_path(tmp_path, label):
    folder, planted, marker = tmp_path / "folder", tmp_path / "planted", tmp_path / "ran.txt"
    if label == "a script of the name and no program":
        plant_scripts_and_programs(folder, marker)
        for leftover in list(folder.iterdir()):
            if leftover.suffix != ".ps1":
                leftover.unlink()
    elif label == "an empty folder":
        folder.mkdir()
    elif label == "a script on PATH and no program":
        folder.mkdir()
        plant_scripts_and_programs(planted, marker)
    prefix = f"$env:PATH = {quoted(planted)} + [IO.Path]::PathSeparator + $env:PATH; " if planted.exists() else ""
    done = native_frame(tmp_path, {"gcloud": [folder]}, "Get-NativePath 'gcloud'", prefix=prefix)
    assert "RESULT:NOT EXECUTABLE" in done.stdout and "gcloud" in done.stdout, (label, done.stdout, done.stderr)
    assert not marker.exists()


def test_w4r3_a_name_that_is_not_one_of_the_four_programs_is_passed_through_untouched(tmp_path):
    done = native_frame(tmp_path, {}, "'PATH:tar=' + (Get-NativePath 'C:\\somewhere\\tar.exe')")
    assert found_paths(done)["tar"] == "C:\\somewhere\\tar.exe", (done.stdout, done.stderr)


def test_w4r3_the_paste_writes_no_user_name_and_no_drive_letter_path():
    text = (ROOT / "core/setup/release/SERVICES-PASTE.ps1").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"[A-Za-z]:\\", code) and "Users" not in code and "$env:USERNAME" not in code and "$env:USERPROFILE" not in code


def test_w4r3_the_install_folders_are_the_stated_ones_and_come_from_the_known_folders_of_the_machine(tmp_path):
    done = paste_function("foreach ($n in 'gcloud','git','py','bash') { foreach ($f in $Script:NativeFolders[$n]) { 'FOLDER:' + $n + '=' + $f } }", tmp_path)
    assert "RESULT:ok" in done.stdout, (done.stdout, done.stderr)
    found = {name: [] for name in NATIVES}
    for line in done.stdout.splitlines():
        if line.startswith("FOLDER:"):
            name, folder = line[7:].split("=", 1)
            found[name].append(os.path.normcase(folder))
    if not ON_WINDOWS:
        assert found == {name: [] for name in NATIVES}
        return
    local, files, files86, windows = (os.environ.get(n) for n in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)", "SystemRoot"))

    def under(base, *parts):
        return [os.path.normcase(os.path.join(base, *parts))] if base else []

    expected = {
        "gcloud": [f for base in (local, files86, files) for f in under(base, "Google", "Cloud SDK", "google-cloud-sdk", "bin")],
        "git": under(local, "Programs", "Git", "cmd") + under(files, "Git", "cmd") + under(files86, "Git", "cmd"),
        "bash": under(local, "Programs", "Git", "bin") + under(files, "Git", "bin") + under(files86, "Git", "bin"),
        "py": under(windows) + under(local, "Programs", "Python", "Launcher") + under(files, "Python Launcher"),
    }
    assert found == {name: [os.path.normcase(f) for f in folders] for name, folders in expected.items()}


def test_w4r3_the_test_folders_are_honoured_only_by_a_definitions_only_load(tmp_path):
    import subprocess

    good = tmp_path / "good"
    write_native(good, "gcloud")
    paste = ROOT / "core/setup/release/SERVICES-PASTE.ps1"
    frame = tmp_path / "real_load.ps1"
    frame.write_text(f"$ErrorActionPreference = 'Stop'; try {{ . '{paste}' -Action Rollback -Lock x -Review x -Bindings x -Receipt x }} catch {{ }}; "
                     f"$Script:TestNativeFolders = @{{ gcloud = @({quoted(good)}) }}; "
                     "try { 'PATH:gcloud=' + (Get-NativePath 'gcloud') } catch { 'RESULT:' + $_.Exception.Message }", encoding="utf-8", newline="\n")
    done = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(frame)], stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", timeout=120)
    assert str(good) not in done.stdout, (done.stdout, done.stderr)


def gcloud_argvs(tmp_path):
    """Every gcloud argument list the four actions issue, read from the calls the doubled world records."""
    found = []
    for action in ACTIONS:
        if action == "Promote":
            world = promote_world(tmp_path / action)
        else:
            world = PasteWorld(tmp_path / action, action, durable={"schema_version": 1, "effects": [{"apply_kind": "schema"}, {"apply_kind": "tag_add"}]})
            if action == "Retire":
                world.readback("AfterRollback")
        result = world.run()
        assert result.returncode == 0, (action, result.stderr)
        for call in result.external:
            if call["argv"][0] == "gcloud" and call["argv"][1:] not in found:
                found.append(call["argv"][1:])
    return found


def test_w4r3_every_argument_list_the_paste_issues_to_gcloud_reaches_the_command_file_unchanged(tmp_path):
    shapes = gcloud_argvs(tmp_path / "worlds")
    flat = [a for argv in shapes for a in argv]
    assert any(a.startswith("--format=json(") and "," in a for a in flat) and any(re.fullmatch(r"--to-revisions=[^=]+=100", a) for a in flat)
    assert any(a.startswith("_IMAGE=") for a in flat) and len(shapes) > 5
    shapes += [["x", "a b", "c=d,e(f)", "--g=h=100"], ["--substitutions", "_IMAGE=a b,K=(v)"], ["run", "services", "describe", "path with  two spaces"]]
    good = tmp_path / "good"
    write_native(good, "gcloud")
    data = tmp_path / "argvs.json"
    data.write_text(json.dumps(shapes), encoding="utf-8")
    out, logs = tmp_path / "read", tmp_path / "logged"
    out.mkdir()
    logs.mkdir()
    call = (f"$list = @(Get-Content -LiteralPath {quoted(data)} -Raw | ConvertFrom-Json); $i = 0; $Script:RunDir = {quoted(logs)}; "
            f"foreach ($argv in $list) {{ $a = [string[]]$argv; "
            f"Read-Native -Exe gcloud -Arguments $a | Set-Content -LiteralPath (Join-Path {quoted(out)} \"$i.json\") -Encoding utf8 -NoNewline; "
            f"Run-Logged -Name \"r$i\" -Argv (@('gcloud') + $a) | Out-Null; $i++ }}")
    done = native_frame(tmp_path, {"gcloud": [good]}, call)
    assert "RESULT:ok" in done.stdout, (done.stdout, done.stderr)
    for i, argv in enumerate(shapes):
        assert json.loads((out / f"{i}.json").read_text(encoding="utf-8-sig")) == argv, ("read", argv)
        assert json.loads((logs / f"r{i}.log").read_text(encoding="utf-8-sig").strip()) == argv, ("run", argv)


@pytest.mark.parametrize("action, expected", [("Candidate", ["gcloud", "git", "py", "bash"]), ("Promote", ["gcloud", "git", "py"]),
                                              ("Rollback", ["gcloud", "git", "py"]), ("Retire", ["gcloud", "git", "py"])])
def test_w4r3_the_programs_an_action_runs_are_all_resolved_before_any_call_prompt_or_file(tmp_path, action, expected):
    world = promote_world(tmp_path) if action == "Promote" else PasteWorld(tmp_path, action)
    if action == "Retire":
        world.readback("AfterRollback")
    result = world.run()
    assert result.returncode == 0, result.stderr
    natives = [c["name"] for c in result.calls if c["kind"] == "native"]
    assert natives == expected
    assert [c["kind"] for c in result.calls[:len(expected)]] == ["native"] * len(expected)


@pytest.mark.parametrize("action", ["Candidate", "Promote"])
def test_w4r3_a_program_that_cannot_be_found_stops_the_action_before_any_call_prompt_or_file(tmp_path, action):
    world = promote_world(tmp_path) if action == "Promote" else PasteWorld(tmp_path, action)
    result = world.run(extra={"native_missing": "gcloud"})
    assert result.returncode != 0 and "NOT EXECUTABLE" in result.stderr and "'gcloud'" in result.stderr
    assert result.external == [] and prompts(result) == [] and not (world.release_dir / "runs").exists()


def test_w4r3_a_candidate_stops_before_any_call_when_bash_is_the_missing_program(tmp_path):
    world = PasteWorld(tmp_path)
    result = world.run(extra={"native_missing": "bash"})
    assert result.returncode != 0 and "'bash'" in result.stderr and result.external == [] and prompts(result) == []


# W4-R5: the repository default, the comment, and the cause of a refused prompt

def test_w4r5_the_repo_default_is_resolved_after_the_first_resolution_check_and_not_as_a_parameter_default(tmp_path):
    marker = tmp_path / "location-called.txt"
    done = paste_run(f"function global:Get-Location {{ Set-Content -LiteralPath '{marker.as_posix()}' -Value ran; [pscustomobject]@{{ Path = '.' }} }}", tmp_path)
    assert not marker.exists(), "a function of the caller ran as the default of -Repo"
    assert GUARD_REFUSED + "Get-Location'" in done.stdout, (done.stdout, done.stderr)


def test_w4r5_without_a_repo_parameter_the_repo_is_the_current_location_once_the_check_has_passed(tmp_path):
    import subprocess

    here = tmp_path / "work tree"
    here.mkdir()
    paste = ROOT / "core/setup/release/SERVICES-PASTE.ps1"
    frame = tmp_path / "repo_default.ps1"
    frame.write_text(f"$ErrorActionPreference = 'Stop'; try {{ . '{paste}' -Action Rollback -Lock x -Review x -Bindings x -Receipt x }} catch {{ }}; "
                     "'REPO:' + $Repo", encoding="utf-8", newline="\n")
    done = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(frame)], cwd=here, stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", timeout=120)
    reported = next(line for line in done.stdout.splitlines() if line.startswith("REPO:"))[5:]
    assert os.path.normcase(os.path.realpath(reported)) == os.path.normcase(os.path.realpath(here)), (done.stdout, done.stderr)


def test_w4r5_the_comment_says_the_fresh_file_start_is_the_only_control_for_what_the_check_cannot_see():
    text = (ROOT / "core/setup/release/SERVICES-PASTE.ps1").read_text(encoding="utf-8")
    block = text[:text.index("$Script:ResolutionCheck = {")]
    comment = " ".join(line.lstrip("# ").strip() for line in block.splitlines() if line.lstrip().startswith("#"))
    for phrase in ("pwsh -NoProfile -File", "only control", "breakpoints", "engine events", "type data", "parameter defaults"):
        assert phrase in comment, phrase


SWAP_CHECK = "$script:n = 0; $Script:ResolutionCheck = { $script:n++; if ($script:n -ge 2) { throw 'NOT EXECUTABLE: planted refusal' } }; "


@pytest.mark.parametrize("call", ["Read-Word 'DEPLOY'", "$null = Read-Passcode"])
def test_w4r5_a_prompt_the_resolution_check_refuses_keeps_that_cause_and_is_not_reported_as_a_missing_console(tmp_path, call):
    done = paste_function(SWAP_CHECK + call, tmp_path)
    assert "RESULT:NOT EXECUTABLE: planted refusal" in done.stdout and "NOT INTERACTIVE" not in done.stdout, (done.stdout, done.stderr)


@pytest.mark.parametrize("call, said", [("Read-Word 'DEPLOY'", "NOT INTERACTIVE: the prompt for DEPLOY could not be shown."),
                                        ("$null = Read-Passcode", "NOT INTERACTIVE: the passcode prompt could not be shown.")])
def test_w4r5_a_prompt_that_fails_for_any_other_reason_is_still_reported_as_a_missing_console(tmp_path, call, said):
    done = paste_function("function Read-Typed { throw 'no console' }; $Script:TestDoubles = @('Read-Typed'); " + call, tmp_path)
    assert "RESULT:" + said in done.stdout, (done.stdout, done.stderr)


# F11: the children of the paste start gcloud and git from the same install folders the paste uses

NATIVE_ENV = {"gcloud": "F42_NATIVE_GCLOUD", "git": "F42_NATIVE_GIT", "py": "F42_NATIVE_PY"}


def roots_args(roots):
    return " ".join(quoted(value) for value in (roots.local, roots.program_files, roots.program_files_x86, roots.windows_folder))


def folder_lines(done):
    found = {name: [] for name in NATIVES}
    for line in done.stdout.splitlines():
        if line.startswith("FOLDER:"):
            name, folder = line[7:].split("=", 1)
            found[name].append(os.path.normcase(folder))
    return found


LIST_FOLDERS = "foreach ($n in 'gcloud','git','py','bash') { foreach ($f in $t[$n]) { 'FOLDER:' + $n + '=' + $f } }"


@pytest.mark.parametrize("empty", [None, "program_files_x86", "local", "windows_folder"])
def test_f11_the_resolver_lists_the_same_folders_as_the_paste_for_the_same_roots(tmp_path, empty):
    from core.setup.release import natives

    if not ON_WINDOWS:
        return
    roots = natives.Roots(local=str(tmp_path / "L"), program_files=str(tmp_path / "P"), program_files_x86=str(tmp_path / "X"),
                          windows_folder=str(tmp_path / "W"))
    if empty:
        roots = roots._replace(**{empty: ""})
    done = paste_function(f"$t = New-NativeFolders {roots_args(roots)}; {LIST_FOLDERS}", tmp_path)
    assert "RESULT:ok" in done.stdout, (done.stdout, done.stderr)
    found = folder_lines(done)
    assert found == {name: [os.path.normcase(f) for f in natives.install_folders(name, roots)] for name in NATIVES}
    assert all(found[name] for name in NATIVES)


def test_f11_the_resolver_reads_the_same_machine_folders_as_the_paste(tmp_path):
    from core.setup.release import natives

    if not ON_WINDOWS:
        return
    done = paste_function(f"$t = $Script:NativeFolders; {LIST_FOLDERS}", tmp_path)
    assert "RESULT:ok" in done.stdout, (done.stdout, done.stderr)
    found = folder_lines(done)
    assert found == {name: [os.path.normcase(f) for f in natives.install_folders(name)] for name in NATIVES}
    assert all(found[name] for name in NATIVES)


SCENES = {
    "first folder": {"gcloud": [0], "git": [0], "py": [0]},
    "later folder only": {"gcloud": [2], "git": [1], "py": [1]},
    "first and later": {"gcloud": [0, 1, 2], "git": [0, 2], "py": [2]},
    "script in the first folder, program in the later": {"gcloud": ["ps1", 1], "git": ["ps1", 2], "py": ["ps1", 1]},
    "scripts only": {"gcloud": ["ps1"], "git": ["ps1"], "py": ["ps1"]},
    "nothing": {},
    "directory of the name": {"gcloud": ["dir"], "git": ["dir"], "py": ["dir"]},
}


@pytest.mark.parametrize("scene", sorted(SCENES))
def test_f11_the_paste_and_the_resolver_find_the_same_program_in_the_same_fake_install_roots(tmp_path, scene):
    from core.setup.release import natives

    if not ON_WINDOWS:
        return
    roots = natives.Roots(local=str(tmp_path / "L"), program_files=str(tmp_path / "P"), program_files_x86=str(tmp_path / "X"),
                          windows_folder=str(tmp_path / "W"))
    for name, places in SCENES[scene].items():
        folders = natives.install_folders(name, roots)
        for place in places:
            if place == "ps1":
                Path(folders[0]).mkdir(parents=True, exist_ok=True)
                (Path(folders[0]) / f"{name}.ps1").write_text("x", encoding="utf-8")
            elif place == "dir":
                (Path(folders[0]) / native_file(name)).mkdir(parents=True, exist_ok=True)
            else:
                write_native(folders[place], name)
    planted = tmp_path / "planted"
    plant_scripts_and_programs(planted, tmp_path / "ran.txt")
    prefix = f"$env:PATH = {quoted(planted)} + [IO.Path]::PathSeparator + $env:PATH; "
    table = f"$Script:TestNativeFolders = New-NativeFolders {roots_args(roots)}; "
    call = "foreach ($n in 'gcloud','git','py') { try { 'PATH:' + $n + '=' + (Get-NativePath $n) } catch { 'PATH:' + $n + '=REFUSED' } }"
    done = paste_function(prefix + table + call, tmp_path)
    assert "RESULT:ok" in done.stdout, (done.stdout, done.stderr)
    from_paste = {k: v if v == "REFUSED" else os.path.normcase(v) for k, v in found_paths(done).items()}
    from_resolver = {}
    for name in ("gcloud", "git", "py"):
        try:
            from_resolver[name] = os.path.normcase(natives.resolve(name, roots))
        except natives.NativeRefused:
            from_resolver[name] = "REFUSED"
    assert from_paste == from_resolver, (scene, from_paste, from_resolver)
    for name, path in found_paths(done).items():
        if path != "REFUSED":
            assert natives.native(name, {NATIVE_ENV[name]: path}, roots) == path
    assert not (tmp_path / "ran.txt").exists()


def test_f11_every_child_of_the_paste_is_given_the_program_paths_it_found_and_they_are_put_back_at_the_end(tmp_path):
    expected = {name: f"native-test-folder\\{name}" for name in ("gcloud", "git", "py")}
    for action in ACTIONS:
        world = promote_world(tmp_path / action) if action == "Promote" else PasteWorld(tmp_path / action, action)
        if action == "Retire":
            world.readback("AfterRollback")
        result = world.run(extra={"attack": "$env:F42_NATIVE_GCLOUD = 'hostile-from-the-console'; $env:F42_NATIVE_GIT = 'hostile-from-the-console'"})
        assert result.returncode == 0, (action, result.stderr)
        calls = logged_calls(result)
        assert len(calls) > 3
        for call in calls:
            assert call["natives"] == expected, (action, call.get("name") or call["argv"], call["natives"])
        assert [c["natives"] for c in result.calls if c["kind"] == "natives_at_end"] == [
            {"gcloud": "hostile-from-the-console", "git": "hostile-from-the-console", "py": ""}]


def test_f11_a_program_the_paste_cannot_find_stops_it_before_any_child_is_given_a_path(tmp_path):
    result = PasteWorld(tmp_path).run(extra={"native_missing": "git"})
    assert result.returncode != 0 and "NOT EXECUTABLE" in result.stderr and result.external == []


def test_f11_the_paste_text_hands_over_only_the_resolved_paths():
    text = (ROOT / "core/setup/release/SERVICES-PASTE.ps1").read_text(encoding="utf-8")
    assert text.count("F42_NATIVE_") == 3 and "Get-Command" not in text and "where.exe" not in text
    for name in ("GCLOUD", "GIT", "PY"):
        assert f"F42_NATIVE_{name} = $resolved[" in text


def offline_guard_loaded():
    """True when an offline guard is loaded in this test process: a sitecustomize module, or the CI switch that requires one."""
    import sys

    return "sitecustomize" in sys.modules or bool(os.environ.get("F42_REQUIRE_OFFLINE_GUARD"))


def real_child(snippet, tmp_path, *, roots=None, planted=None, cwd=None, keep_guard=False):
    """A real python child, isolated so that no site hook or path variable of the test run reaches it, that runs the snippet with
    the resolver told to use the roots (when given) and with the planted programs first on PATH. With keep_guard the child starts
    with -s and never with -I, -E or -S, which would keep an offline guard from loading, and the one PYTHON setting it keeps is the
    guard directory (the entry of the parent's PYTHONPATH that holds a sitecustomize.py), the way core/api/tests/compat_harness.py does."""
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("F42_NATIVE_") and k != "PYTHONPATH"}
    if keep_guard:
        env = {k: v for k, v in env.items() if not k.upper().startswith("PYTHON")}
        guard = [entry for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep) if entry and (Path(entry) / "sitecustomize.py").is_file()]
        if guard:
            env["PYTHONPATH"] = os.pathsep.join(guard)
    if planted:
        env["PATH"] = str(planted) + os.pathsep + env["PATH"]
    if roots is not None:
        env["F11_ROOTS"] = json.dumps(list(roots))
    code = ("import json, os, sys\nsys.path.insert(0, " + repr(str(ROOT)) + ")\nfrom core.setup.release import natives\n"
            "if 'F11_ROOTS' in os.environ:\n    _roots = natives.Roots(*json.loads(os.environ['F11_ROOTS']))\n    natives.known_roots = lambda: _roots\n" + snippet)
    return subprocess.run([sys.executable, "-s" if keep_guard else "-I", "-c", code], cwd=cwd or tmp_path, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                          encoding="utf-8", timeout=120)


def plant_real_looking(folder, marker):
    """A command file and a script of each name that leave the marker, and a real program (whoami) under the names gcloud.exe and
    git.exe, which fails every call the child makes, so a child that reached it could not report success."""
    plant_scripts_and_programs(folder, marker)
    for name in ("gcloud", "git"):
        shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32" / "whoami.exe", Path(folder) / f"{name}.exe")


@pytest.mark.skipif(offline_guard_loaded(), reason="a loaded offline guard refuses every gcloud by design, so this test, which runs a fake gcloud "
                    "in a real child, is release evidence that runs unguarded (the deploy partition of release_test_ids.json)")
def test_f11_a_real_readback_child_runs_the_install_gcloud_and_never_the_planted_one(tmp_path):
    if not ON_WINDOWS:
        return
    root_marker, planted_marker = tmp_path / "root-ran.txt", tmp_path / "planted-ran.txt"
    local = tmp_path / "L"
    sdk = local / "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin"
    sdk.mkdir(parents=True)
    (sdk / "gcloud.cmd").write_text('@echo off\r\necho ran> "' + str(root_marker) + '"\r\necho {"core": {"project": "from-the-install-folder"}}\r\n',
                                    encoding="ascii", newline="")
    plant_real_looking(tmp_path / "planted", planted_marker)
    done = real_child("from core.setup.release import bound_readback as h\n"
                      "print(json.dumps(h.GcloudReader({'gcloud': 60, 'http': 5}).config()))\n", tmp_path, roots=(str(local), "", "", ""),
                      planted=tmp_path / "planted")
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert json.loads(done.stdout) == {"core": {"project": "from-the-install-folder"}}
    assert root_marker.exists() and not planted_marker.exists()


def test_f11_real_git_children_run_the_install_git_and_never_the_planted_one(tmp_path):
    from core.setup.release import natives

    if not ON_WINDOWS:
        return
    planted_marker = tmp_path / "planted-ran.txt"
    plant_real_looking(tmp_path / "planted", planted_marker)
    expected = natives.resolve("git")
    done = real_child(
        "from core.setup import durable_effects_check as dec\n"
        "from core.setup.release import bound_readback as h, packet\n"
        "files = [path for path, text in dec.git_tree_files('HEAD', 'core/setup/release', cwd=" + repr(str(ROOT)) + ")]\n"
        "print(json.dumps({'git': natives.native('git'), 'files': files,\n"
        "                  'sha': h.GcloudReader({'gcloud': 60, 'http': 5}).source_file_sha256('HEAD', 'core/api/smoke.py'),\n"
        "                  'head': packet.git(" + repr(str(ROOT)) + ", 'rev-parse', 'HEAD')}))\n", tmp_path, planted=tmp_path / "planted", cwd=ROOT,
        keep_guard=True)
    assert done.returncode == 0, (done.stdout, done.stderr)
    out = json.loads(done.stdout)
    assert os.path.normcase(out["git"]) == os.path.normcase(expected)
    assert "core/setup/release/lock.py" in out["files"] and len(out["sha"]) == 64 and re.fullmatch(r"[0-9a-f]{40}", out["head"])
    assert not planted_marker.exists()
