"""The release steps and the recovery table as data (W8-REL 3.1, 3.3, 3.5, 2.11 and PS-03 to PS-08, PS-21, PS-22 at plan
level). No command is run; the paste renders these lists, and these tests hold them to the contract."""
import dataclasses
import re

import pytest

from core.setup.release import plan

RID = "rel-d666ef6-01"
A80 = {"f42-agent": "f42-agent-00047-677", "f42-api": "f42-api-00041-lns"}
API_TAG = f"https://{RID}---f42-api-fibxg5ynpq-uc.a.run.app"
COMMIT = "d666ef64cd7a0123456789abcdef0123456789ab"
CTX = plan.Ctx(release_id=RID, commit=COMMIT, helper="core/setup/release/bound_readback.py", bindings="releases/bindings.json",
               evidence="runs/20261009-Candidate", manifest="releases/rel/release-manifest.json", manifest_sha256="ab" * 32,
               api_tag_url=API_TAG, a80=A80, image_tag=f"us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/f42-web:d666ef64cd7a-01",
               archive="tmp/source.tar", extract_dir="tmp/source")


def names(steps):
    return [s.name for s in steps]


def text(step):
    return " ".join(step.argv)


def all_plans(ctx=CTX):
    plans = {a: f(ctx) for a, f in plan.ACTIONS.items()}
    plans["Candidate+schema"] = plan.candidate(dataclasses.replace(ctx, schema_effects=True))
    plans.update({f"row {k}": v for k, v in plan.recovery_rows(ctx).items()})
    return plans


def test_plan_ps03_candidate_runs_in_the_contract_order_and_moves_no_traffic_but_the_two_pins():
    assert names(plan.candidate(CTX)) == [
        "assert:head", "assert:tree", "assert:status", "assert:identity", "declared_env:describe", "declared_env:match", "helper:BeforeAnyWrite", "archive", "extract", "build",
        "helper:Freeze", "checker:validate", "pin:f42-agent", "pin:f42-api", "helper:BeforeCandidate", "deploy_candidate", "helper:BeforeSmoke", "smoke",
        "helper:AfterSmoke"]
    assert names(plan.candidate(dataclasses.replace(CTX, schema_effects=True))).index("schema_apply") == names(plan.candidate(CTX)).index("pin:f42-agent")
    traffic = [s for s in plan.candidate(CTX) if "update-traffic" in s.argv]
    assert [s.name for s in traffic] == ["pin:f42-agent", "pin:f42-api"]


def test_plan_ps04_the_smoke_runs_against_the_manifest_api_tag_url_and_never_the_public_api():
    [smoke] = [s for s in plan.candidate(CTX) if s.name == "smoke"]
    assert smoke.argv[3] == API_TAG and "run.app" in smoke.argv[3] and "---" in smoke.argv[3]
    assert smoke.argv[4:] == ("--market", "ZA", "--mode", "live", "--ask-timeout", "900")


def test_plan_ps05_promote_moves_the_agent_then_the_api_by_name_and_never_to_latest():
    steps = plan.promote(CTX)
    assert names(steps) == ["helper:BeforePromotion", "promote:f42-agent", "helper:AfterAgentPromotion", "promote:f42-api", "helper:AfterPromotion"]
    agent, api = steps[1], steps[3]
    assert f"--to-revisions=f42-agent-{RID}=100" in agent.argv and f"--to-revisions=f42-api-{RID}=100" in api.argv
    assert api.requires == "helper:AfterAgentPromotion" and agent.requires is None
    assert not any("jobs" in text(s) or "scheduler" in text(s) for s in steps)


def test_plan_ps06_rollback_restores_the_api_first_then_the_agent_to_the_baseline_revisions():
    steps = plan.rollback(CTX)
    assert names(steps) == ["helper:BeforeRollback", "restore:f42-api", "restore:f42-agent", "helper:AfterRollback"]
    assert "--to-revisions=f42-api-00041-lns=100" in steps[1].argv and "--to-revisions=f42-agent-00047-677=100" in steps[2].argv


def test_plan_ps06_the_rollback_targets_come_from_the_baseline_not_from_the_plan():
    other = dataclasses.replace(CTX, a80={"f42-agent": "f42-agent-00099-aaa", "f42-api": "f42-api-00099-bbb"})
    joined = " ".join(text(s) for s in plan.rollback(other))
    assert "f42-api-00099-bbb" in joined and "f42-agent-00099-aaa" in joined
    assert "00041" not in joined and "00047" not in joined and "00046" not in joined and "00040" not in joined


def test_plan_ps21_tags_are_removed_only_after_the_readback_that_proves_where_traffic_is():
    for action, before in (("Retire", "helper:BeforeRetire"),):
        order = names(plan.ACTIONS[action](CTX))
        assert order.index(before) < order.index("remove_tag:f42-api") < order.index("remove_tag:f42-agent") < order.index("helper:AfterRetire")
    rows = plan.recovery_rows(CTX)
    for key in ("C4", "2", "3"):
        order = names(rows[key])
        assert order.index("helper:AfterRollback") < order.index("remove_tag:f42-api")
    order = names(rows["1"])
    assert order.index("helper:AfterSmoke") < order.index("remove_tag:f42-api")
    assert names(rows["C3"])[0] == "helper:BeforeRollback"


def test_plan_ps21_the_retire_phases_carry_the_release_id_as_their_tag_and_no_other_phase_does():
    for label, steps in all_plans().items():
        for s in steps:
            if s.name.startswith("helper:"):
                has_tag = "--tag" in s.argv
                assert has_tag == s.name.endswith(("BeforeRetire", "AfterRetire")), (label, s.name)
                if has_tag:
                    assert s.argv[s.argv.index("--tag") + 1] == RID


def test_recovery_row_c1_is_a_before_any_write_readback_only():
    assert names(plan.recovery_rows(CTX)["C1"]) == ["helper:BeforeAnyWrite"]


def test_recovery_row_c2_reruns_the_pin_and_reads_before_candidate():
    assert names(plan.recovery_rows(CTX)["C2"]) == ["pin:f42-agent", "pin:f42-api", "helper:BeforeCandidate"]


def test_recovery_row_c3_reads_before_rollback_and_removes_the_api_tag_only_when_the_readback_says_it_exists():
    steps = plan.recovery_rows(CTX)["C3"]
    assert names(steps) == ["helper:BeforeRollback", "remove_tag:f42-api", "remove_tag:f42-agent", "helper:AfterRetire"]
    assert steps[1].conditional and "API tag exists" in steps[1].conditional and steps[2].conditional is None
    assert "helper:BeforeSmoke" not in names(steps)
    assert not any("update-traffic" in s.argv and "--to-revisions" in text(s) for s in steps)


def test_recovery_row_c4_puts_traffic_back_api_first_then_reads_then_removes_tags():
    steps = plan.recovery_rows(CTX)["C4"]
    assert names(steps) == ["restore:f42-api", "restore:f42-agent", "helper:AfterRollback", "remove_tag:f42-api", "remove_tag:f42-agent", "helper:AfterRetire"]


def test_recovery_row_1_proves_traffic_never_moved_then_removes_both_tags():
    assert names(plan.recovery_rows(CTX)["1"]) == ["helper:AfterSmoke", "remove_tag:f42-api", "remove_tag:f42-agent", "helper:AfterRetire"]


def test_recovery_row_2_restores_the_agent_only():
    steps = plan.recovery_rows(CTX)["2"]
    assert names(steps) == ["helper:BeforeRollback", "restore:f42-agent", "helper:AfterRollback", "remove_tag:f42-api", "remove_tag:f42-agent", "helper:AfterRetire"]
    assert not any(s.name == "restore:f42-api" for s in steps)


def test_recovery_row_3_restores_the_api_before_the_agent():
    steps = plan.recovery_rows(CTX)["3"]
    assert names(steps)[:4] == ["helper:BeforeRollback", "restore:f42-api", "restore:f42-agent", "helper:AfterRollback"]


def test_recovery_row_4_checks_for_blockers_offline_first_and_a_block_runs_no_traffic_command():
    rows = plan.recovery_rows(CTX)
    assert names(rows["4"]) == ["checker:rollback-blockers"] and names(rows["4:blocked"]) == ["helper:BeforeRollback"]
    assert not any("update-traffic" in s.argv for s in rows["4:blocked"])


def test_recovery_row_c5_runs_the_readbacks_checker():
    assert names(plan.recovery_rows(CTX)["C5"]) == ["checker:readbacks"]


def test_no_step_in_any_plan_or_row_uses_a_latest_following_or_tag_setting_traffic_command():
    for label, steps in all_plans().items():
        for s in steps:
            for flag in plan.FORBIDDEN_TRAFFIC_FLAGS:
                assert flag not in s.argv, (label, s.name)
            if "update-traffic" in s.argv:
                assert any(a.startswith("--to-revisions=") and a.endswith("=100") for a in s.argv) or "--remove-tags" in s.argv, (label, s.name)
            assert not (s.argv[:2] == ("gcloud", "run") and s.argv[2:4] == ("services", "update")), (label, s.name)
            for word in ("iam", "scheduler", "bq", "curl", "listening-post-staging", "set-iam-policy"):
                assert word not in s.argv, (label, s.name, word)


def test_plan_ps22_every_external_invocation_matches_exactly_one_allowlist_entry():
    for label, steps in all_plans().items():
        assert plan.check_allowlist(steps) is None, (label, plan.check_allowlist(steps))
    kinds = {plan.matching_entries(s.argv)[0] for steps in all_plans().values() for s in steps}
    assert kinds == set(plan.ALLOWED)


@pytest.mark.parametrize("argv", [
    ["bq", "query", "select 1"],
    ["gcloud", "run", "services", "update", "f42-api", "--project", "ogilvy-trends-v2"],
    ["gcloud", "run", "services", "update-traffic", "f42-api", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--to-latest", "--quiet"],
    ["gcloud", "run", "services", "update-traffic", "f42-api", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--update-tags", "x=y", "--quiet"],
    ["gcloud", "run", "services", "update-traffic", "f42-api", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--clear-tags", "--quiet"],
    ["gcloud", "run", "services", "update-traffic", "f42-api", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--to-revisions=f42-api-x=50", "--quiet"],
    ["gcloud", "run", "services", "update-traffic", "f42-api", "--project", "other-project", "--region", "us-central1", "--to-revisions=f42-api-x=100", "--quiet"],
    ["gcloud", "run", "services", "update-traffic", "other-service", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--to-revisions=x=100", "--quiet"],
    ["gcloud", "iam", "service-accounts", "list"],
    ["gcloud", "run", "services", "add-iam-policy-binding", "f42-agent"],
    ["curl", "https://example.invalid"],
    ["gcloud", "run", "services", "describe", "listening-post-staging", "--format=json"],
    ["gcloud", "run", "deploy", "f42-agent", "--image", "x"],
    ["gcloud", "run", "jobs", "update", "f42-collect"],
    ["py", "-3.13", "core/setup/release/bound_readback.py", "--mode", "full", "--phase", "AfterRollback", "--bindings", "b", "--evidence", "e"],
    ["py", "-3.13", "core/setup/release/bound_readback.py", "--mode", "services-only", "--phase", "AfterEverything", "--bindings", "b", "--evidence", "e"],
    ["py", "-3.13", "core/api/smoke.py", "https://f42-api-fibxg5ynpq-uc.a.run.app", "--market", "ZA", "--mode", "live", "--ask-timeout", "900"],
    ["gcloud", "run", "services", "update-traffic", "f42-api", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--remove-tags", "stray", "--quiet"],
    ["gcloud", "run", "services", "update-traffic", "f42-api", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--to-revisions=f42-api-x=100", "--to-latest", "--quiet"],
    ["gcloud", "builds", "submit", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--config", "core/api/cloudbuild.yaml", "--substitutions",
     "_IMAGE=us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/f42-web:d666ef64cd7a-01", "--gcs-source-staging-dir", "gs://ogilvy-trends-v2-f42-media-staging/build-source",
     "--service-account", "projects/ogilvy-trends-v2/serviceAccounts/someone@ogilvy-trends-v2.iam.gserviceaccount.com", "."],
    ["gcloud", "builds", "submit", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--config", "core/api/cloudbuild.yaml", "--substitutions",
     "_IMAGE=us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/f42-web:d666ef64cd7a-01", "--gcs-source-staging-dir", "gs://another-bucket/build-source",
     "--service-account", "projects/ogilvy-trends-v2/serviceAccounts/f42-deployer@ogilvy-trends-v2.iam.gserviceaccount.com", "."],
    ["bash", "core/api/deploy.sh", "--no-build"],
    ["py", "-3.13", "core/setup/deploy_jobs.py"],
    ["py", "-3.13", "core/setup/durable_effects_check.py", "--check", "anything", "--evidence", "e", "--manifest", "m"],
    ["gcloud", "run", "services", "describe", "f42-api", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--format=json"],
    ["gcloud", "run", "services", "describe", "f42-agent", "--project", "other-project", "--region", "us-central1", "--format=json"],
    ["gcloud", "run", "services", "describe", "f42-agent", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--format=yaml"],
    ["py", "-3.13", "-m", "core.setup.release.declared_env_removals", "--service", "f42-api"],
    ["py", "-3.13", "-m", "core.setup.release.declared_env_removals"],
    ["py", "-3.13", "-m", "core.schema.apply"],
])
def test_plan_ps22_anything_outside_the_allowlist_matches_nothing(argv):
    assert plan.matching_entries(argv) == []


def test_plan_ps22_no_entry_overlaps_another_on_the_real_steps():
    for steps in all_plans().values():
        for s in steps:
            assert len(plan.matching_entries(s.argv)) == 1, s.name


def test_the_build_command_pins_the_deployer_the_staging_bucket_and_the_attempt_numbered_image():
    [build] = [s for s in plan.candidate(CTX) if s.name == "build"]
    assert build.argv[-1] == "." and plan.BUILD_ACCOUNT in build.argv and plan.GCS_STAGING in build.argv
    broken = dataclasses.replace(build, argv=tuple(a if "_IMAGE" not in a else "_IMAGE=us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/f42-web:abc" for a in build.argv))
    assert plan.matching_entries(broken.argv) == []


def test_deploy_candidate_takes_the_manifest_and_the_hash_the_freeze_readback_recorded():
    [deploy] = [s for s in plan.candidate(CTX) if s.name == "deploy_candidate"]
    assert deploy.argv == ("bash", "core/api/deploy_candidate.sh", CTX.manifest, "ab" * 32)
    assert plan.matching_entries(["bash", "core/api/deploy_candidate.sh", CTX.manifest, "not-a-hash"]) == []


def test_the_pins_use_the_baseline_revisions_for_both_services_in_order():
    other = dataclasses.replace(CTX, a80={"f42-agent": "f42-agent-00099-aaa", "f42-api": "f42-api-00099-bbb"})
    steps = plan.pin(other)
    assert names(steps) == ["pin:f42-agent", "pin:f42-api"]
    assert "--to-revisions=f42-agent-00099-aaa=100" in steps[0].argv and "--to-revisions=f42-api-00099-bbb=100" in steps[1].argv


def test_plan_the_declared_removal_is_read_live_and_matched_before_the_first_write():
    steps = names(plan.candidate(CTX))
    assert steps.index("declared_env:describe") + 1 == steps.index("declared_env:match") < steps.index("helper:BeforeAnyWrite")
    [describe, match] = [s for s in plan.candidate(CTX) if s.name.startswith("declared_env:")]
    assert plan.matching_entries(describe.argv) == plan.matching_entries(match.argv) == ["declared_env_read"]
    assert not any(s.name.startswith("declared_env:") for action in ("Promote", "Rollback", "Retire") for s in plan.ACTIONS[action](CTX))


def test_plan_f8_the_validate_check_is_an_allowlist_entry_because_it_judges_the_manifest_on_the_acting_path():
    assert "validate" in plan.CHECKER_KINDS
    [validate] = [s for s in plan.candidate(CTX) if s.name == "checker:validate"]
    assert plan.matching_entries(validate.argv) == ["checker"]
    assert plan.matching_entries(["py", "-3.13", "core/setup/durable_effects_check.py", "--check", "validate", "--manifest", "m"]) == []  # no evidence dir


# The calls deploy_candidate.sh makes are judged by their own allowlist: the one deploy of each service, and --remove-env-vars
# only on f42-agent (finding 3).

def deploy_call(service="f42-agent", *, remove=None, extra=()):
    argv = ["run", "deploy", service, "--project", "ogilvy-trends-v2", "--region", "us-central1", "--image", "r/f42-web@sha256:" + "ab" * 32,
            "--revision-suffix", RID, "--tag", RID, "--no-traffic", "--service-account", f"{service}@ogilvy-trends-v2.iam.gserviceaccount.com",
            "--min-instances", "1", "--update-env-vars", "A=1,B=2"]
    if remove is not None:
        argv += ["--remove-env-vars", remove]
    return argv + ["--set-secrets", "S=S:latest", *extra]


def test_plan_ds_the_deploy_script_calls_are_allowed_with_the_remove_flag_on_the_agent_only():
    assert plan.deploy_call_allowed(deploy_call("f42-agent"))
    assert plan.deploy_call_allowed(deploy_call("f42-agent", remove="ONE_NAME,TWO_NAME"))
    assert plan.deploy_call_allowed(deploy_call("f42-api"))
    assert not plan.deploy_call_allowed(deploy_call("f42-api", remove="ONE_NAME"))


@pytest.mark.parametrize("call", [
    deploy_call("f42-agent", remove=""), deploy_call("f42-agent", remove="BAD NAME"), deploy_call("f42-agent", remove="A,,B"),
    deploy_call("f42-agent", remove="A;touch x"), deploy_call("f42-agent", remove="A", extra=("--remove-env-vars", "B")),
    deploy_call("f42-agent", extra=("--allow-unauthenticated",)), deploy_call("f42-agent", extra=("--set-env-vars", "A=1")),
    deploy_call("f42-agent", extra=("--clear-env-vars",)), deploy_call("f42-agent", extra=("--to-latest",)),
    deploy_call("other-service"), ["run", "services", "update", "f42-agent"], ["run", "deploy", "f42-agent", "--image", "x"],
])
def test_plan_ds_a_deploy_call_outside_the_form_is_refused(call):
    assert not plan.deploy_call_allowed(call)


def test_plan_r3_the_extract_uses_the_system_tar_by_full_path_and_a_tar_from_path_is_not_allowed():
    [extract] = [s for s in plan.candidate(CTX) if s.name == "extract"]
    assert re.fullmatch(r"[A-Za-z]:\\[^\\]+\\System32\\tar\.exe", extract.argv[0]), extract.argv[0]
    assert plan.matching_entries(extract.argv) == ["archive"]
    assert plan.matching_entries(["tar", *extract.argv[1:]]) == []
    assert plan.matching_entries([r"C:\tools\tar.exe", *extract.argv[1:]]) == []
    assert plan.matching_entries([extract.argv[0], "-xf", "a.tar", "-C", "d", "--to-command=x"]) == []


def test_plan_r9_the_module_read_runs_without_bytecode_and_the_form_without_it_is_not_allowed():
    [match] = [s for s in plan.candidate(CTX) if s.name == "declared_env:match"]
    assert match.argv == ("py", "-3.13", "-B", "-m", "core.setup.release.declared_env_removals", "--service", "f42-agent")
    assert plan.matching_entries(match.argv) == ["declared_env_read"]
    assert plan.matching_entries(["py", "-3.13", "-m", "core.setup.release.declared_env_removals", "--service", "f42-agent"]) == []


AGENT_REAL = ["run", "deploy", "f42-agent", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--image", "r/f42-web@sha256:" + "ab" * 32,
              "--revision-suffix", RID, "--tag", RID, "--no-traffic", "--service-account", "f42-agent@ogilvy-trends-v2.iam.gserviceaccount.com",
              "--min-instances", "1", "--max-instances", "1", "--no-cpu-throttling", "--timeout", "3600", "--memory", "1Gi",
              "--update-env-vars", "A=1,B=2", "--set-secrets", "S=S:latest"]
API_REAL = ["run", "deploy", "f42-api", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--image", "r/f42-web@sha256:" + "ab" * 32,
            "--revision-suffix", RID, "--tag", RID, "--no-traffic", "--service-account", "f42-web@ogilvy-trends-v2.iam.gserviceaccount.com",
            "--no-invoker-iam-check", "--min-instances", "1", "--max-instances", "3", "--timeout", "3600",
            "--update-env-vars", "A=1,B=2", "--set-secrets", "S=S:latest"]


def equals_form(call, flags):
    out, i = list(call[:3]), 3
    while i < len(call):
        if call[i] in flags and i + 1 < len(call):
            out.append(f"{call[i]}={call[i + 1]}")
            i += 2
        else:
            out.append(call[i])
            i += 1
    return out


def test_plan_r4_the_two_real_deploy_forms_are_allowed_and_equals_forms_parse():
    assert plan.deploy_call_allowed(AGENT_REAL) and plan.deploy_call_allowed(API_REAL)
    assert plan.deploy_call_allowed(AGENT_REAL + ["--remove-env-vars", "ONE_NAME,TWO_NAME"])
    assert plan.deploy_call_allowed(AGENT_REAL + ["--remove-env-vars=ONE_NAME,TWO_NAME"])
    valued = {"--min-instances", "--max-instances", "--timeout", "--memory", "--image", "--tag", "--update-env-vars"}
    assert plan.deploy_call_allowed(equals_form(AGENT_REAL, valued)) and plan.deploy_call_allowed(equals_form(API_REAL, valued))


@pytest.mark.parametrize("call", [
    [x if x != "--no-cpu-throttling" else "--no-cpu-throttling=false" for x in AGENT_REAL], [x if x != "--no-invoker-iam-check" else "--no-invoker-iam-check=true" for x in API_REAL],
    API_REAL + ["--remove-env-vars=X"], API_REAL + ["--remove-env-vars", "X"], AGENT_REAL + ["--remove-env-vars="], AGENT_REAL + ["--remove-env-vars=A B"],
    AGENT_REAL + ["--remove-env-vars=A", "--remove-env-vars=B"], AGENT_REAL + ["--remove-env-vars=A", "--remove-env-vars", "B"],
    AGENT_REAL + ["--set-env-vars=A=1"], AGENT_REAL + ["--set-env-vars", "A=1"], AGENT_REAL + ["--env-vars-file", "f.yaml"],
    AGENT_REAL + ["--env-vars-file=f.yaml"], AGENT_REAL + ["--clear-secrets"], AGENT_REAL + ["--remove-secrets", "S"],
    AGENT_REAL + ["--remove-secrets=S"], AGENT_REAL + ["--clear-env-vars"], AGENT_REAL + ["--no-invoker-iam-check"],
    AGENT_REAL + ["--allow-unauthenticated"], AGENT_REAL + ["--allow-unauthenticated=true"], AGENT_REAL + ["--to-latest"],
    AGENT_REAL + ["--no-traffic=false"], AGENT_REAL + ["--cpu=8"], AGENT_REAL + ["--vpc-connector", "x"], AGENT_REAL + ["--min-instances", "1"],
    AGENT_REAL + ["--update-env-vars", "C=3"], AGENT_REAL + ["stray-positional"], API_REAL + ["--no-cpu-throttling"], API_REAL + ["--memory", "1Gi"],
    [a for a in AGENT_REAL if a != "--no-traffic"], AGENT_REAL + ["--min-instances"],
    [a.replace(RID, "latest") if a == RID else a for a in AGENT_REAL],
])
def test_plan_r4_a_flag_outside_the_positive_list_or_used_twice_or_on_the_wrong_service_is_refused(call):
    assert not plan.deploy_call_allowed(call)
