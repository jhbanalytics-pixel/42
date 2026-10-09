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


def paste_function(call, *, stdin=None, flags=("-NonInteractive",)):
    """Run the real Test-Interactive or Assert-Interactive of the paste in a real pwsh, with no double."""
    import subprocess

    paste = ROOT / "core/setup/release/SERVICES-PASTE.ps1"
    script = (f"$ErrorActionPreference = 'Stop'; . '{paste}' -Action Rollback -Lock x -Review x -Bindings x -Receipt x -DefinitionsOnly; "
              f"try {{ {call}; 'RESULT:ok' }} catch {{ 'RESULT:' + $_.Exception.Message }}")
    return subprocess.run(["pwsh", "-NoProfile", *flags, "-Command", script], input=stdin, capture_output=True, encoding="utf-8", timeout=120)


def test_f5_the_real_interactivity_check_refuses_a_redirected_stdin_and_a_noninteractive_host():
    piped = paste_function("Assert-Interactive", stdin="DEPLOY\n", flags=())
    assert "RESULT:NOT INTERACTIVE" in piped.stdout, (piped.stdout, piped.stderr)
    noninteractive = paste_function("Read-Word 'DEPLOY'")
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

def test_r2_a_function_named_read_host_cannot_stand_in_for_the_typed_word():
    # Read-Host is called by its module-qualified name, so a function of that name defined by the caller is never run. The
    # real prompt is refused here (no console), so the word is never accepted.
    shadowed = paste_function("function global:Read-Host { 'DEPLOY' }; Read-Word 'DEPLOY'")
    assert "RESULT:ok" not in shadowed.stdout and "RESULT:NOT INTERACTIVE" in shadowed.stdout, (shadowed.stdout, shadowed.stderr)
    passcode = paste_function("function global:Read-Host { ConvertTo-SecureString 'x' -AsPlainText -Force }; Read-Passcode")
    assert "RESULT:ok" not in passcode.stdout, (passcode.stdout, passcode.stderr)


def test_r2_the_paste_calls_the_prompt_cmdlet_only_by_its_module_qualified_name():
    text = (ROOT / "core/setup/release/SERVICES-PASTE.ps1").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert len(re.findall(r"Microsoft\.PowerShell\.Utility\\Read-Host", code)) == 2
    assert not re.findall(r"(?<![\\\w-])Read-Host", code)


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
