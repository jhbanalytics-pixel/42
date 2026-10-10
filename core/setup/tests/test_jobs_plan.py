"""The steps of the three Release B actions as data (W8-REL-B v2.1 3.4, 3.6, JU-01, JU-02) and the positive allowlist that judges
every external invocation. The same allowlist judges what the update orchestrator really runs in test_jobs_update.py."""
import dataclasses

import pytest

from core.setup.release import plan
from core.setup.release import services_only as so
from core.setup.tests import jobs_world as jw

ORDER = ["f42-watchdog", "f42-probe", "f42-gdelt", "f42-gdelt-daily", "f42-reconcile", "f42-drift", "f42-learn", "f42-calendar",
         "f42-digest", "f42-scheduled-asks", "f42-brief", "f42-detect", "f42-understand", "f42-collect"]
REPO = "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/jobs"


def ctx(**over):
    base = dict(release_id=jw.B_RID, commit=jw.B_COMMIT, helper="core/setup/release/bound_readback.py", runner="core/setup/release/jobs_run.py",
                bindings="C:/release/bindings.json", evidence="C:/release/run", digest=jw.NEW_DIGEST, rollback_digest=jw.ROLLBACK_DIGEST,
                image_tag=f"{REPO}:{jw.B_COMMIT[:12]}-01")
    base.update(over)
    return plan.JobsCtx(**base)


def updates(steps):
    return [s for s in steps if s.argv[:4] == ("gcloud", "run", "jobs", "update")]


# JU-01

def test_ju01_jobs_update_renders_exactly_14_update_steps_in_the_order_of_3_6_each_followed_by_its_readback():
    steps = plan.JOBS_ACTIONS["JobsUpdate"](ctx())
    ups = updates(steps)
    assert len(ups) == 14
    assert [s.argv[4] for s in ups] == ORDER
    for index, step in enumerate(steps):
        if step in ups:
            after = steps[index + 1]
            assert after.argv == ("gcloud", "run", "jobs", "describe", step.argv[4], "--project", "ogilvy-trends-v2", "--region",
                                  "us-central1", "--format=json"), step.argv[4]


def test_ju01_each_update_is_exactly_the_image_the_project_the_region_and_quiet_and_nothing_else():
    for step in updates(plan.JOBS_ACTIONS["JobsUpdate"](ctx())):
        assert step.argv == ("gcloud", "run", "jobs", "update", step.argv[4], "--image", f"{REPO}@{jw.NEW_DIGEST}", "--project",
                             "ogilvy-trends-v2", "--region", "us-central1", "--quiet")
        assert len(step.argv[6].split("@")[1]) == 71


def test_ju01_jobs_rollback_renders_the_same_14_in_reverse_with_the_rollback_digest():
    ups = updates(plan.JOBS_ACTIONS["JobsRollback"](ctx()))
    assert [s.argv[4] for s in ups] == ORDER[::-1]
    assert all(s.argv[6] == f"{REPO}@{jw.ROLLBACK_DIGEST}" and s.argv[7:] == ("--project", "ogilvy-trends-v2", "--region", "us-central1", "--quiet")
               for s in ups)


def test_ju01_the_helper_phases_bracket_each_action_and_the_chain_group_is_last_and_contiguous():
    update = plan.JOBS_ACTIONS["JobsUpdate"](ctx())
    helpers = [s.name for s in update if s.name.startswith("helper:")]
    assert helpers == ["helper:BeforeJobsUpdate", "helper:AfterJobsUpdate"]
    assert update[0].name == "helper:BeforeJobsUpdate" and update[-1].name == "helper:AfterJobsUpdate"
    names = [s.argv[4] for s in updates(update)]
    assert names[-4:] == ["f42-brief", "f42-detect", "f42-understand", "f42-collect"]
    rollback = plan.JOBS_ACTIONS["JobsRollback"](ctx())
    assert [s.name for s in rollback if s.name.startswith("helper:")] == ["helper:BeforeJobsRollback", "helper:AfterJobsRollback"]


def test_ju01_the_candidate_runs_the_checks_then_the_build_then_freeze_then_validate_then_the_snapshot_then_the_schema_apply():
    names = [s.name for s in plan.JOBS_ACTIONS["JobsCandidate"](ctx(schema_effects=True))]
    wanted = ["helper:BeforeAnyWrite", "build", "build:upload", "build:create", "helper:FreezeJobs", "checker:validate",
              "snapshot", "schema_apply", "checker:readbacks"]
    positions = [names.index(n) for n in wanted]
    assert positions == sorted(positions)
    assert names.index("snapshot") < names.index("schema_apply")
    assert "archive" not in names and "extract" not in names  # RJ-4: no archive and no extract, the build runs from the clean clone


def test_ju01_a_digest_that_is_not_a_sha256_is_refused_at_render_time():
    for bad in ("sha256:short", "latest", jw.NEW_DIGEST.upper().replace("SHA256", "sha256"), ""):
        with pytest.raises(ValueError):
            plan.JOBS_ACTIONS["JobsUpdate"](ctx(digest=bad))
    with pytest.raises(ValueError):
        plan.JOBS_ACTIONS["JobsRollback"](ctx(rollback_digest="sha256:short"))


def test_ju01_the_update_order_is_not_something_a_caller_can_change_through_the_context():
    assert "order" not in {f.name for f in dataclasses.fields(plan.JobsCtx)}


# JU-02

def all_steps(**over):
    return [s for action in plan.JOBS_ACTIONS.values() for s in action(ctx(schema_effects=True, **over))]


def test_ju02_every_step_of_every_jobs_action_matches_exactly_one_entry():
    steps = all_steps()
    assert len(steps) > 50 and plan.check_jobs_allowlist(steps) is None
    for step in steps:
        assert len(plan.jobs_matching_entries(step.argv)) == 1, step.name


def test_ju02_the_new_entries_exist_and_the_services_entries_are_unchanged():
    new = {"jobs_update", "jobs_build", "jobs_build_start", "executions_read", "chain_evidence", "jobs_read", "jobs_run"}
    assert new <= set(plan.JOBS_ALLOWED)
    assert not new & set(plan.ALLOWED)
    # RJ-4: the jobs list has no archive entry, because the jobs actions neither archive nor extract
    assert set(plan.JOBS_ALLOWED) - new == {"git_read", "identity", "checker", "schema_apply", "helper"}


def test_ju02_the_two_allowlists_do_not_lend_each_other_anything():
    jobs_update = ["gcloud", "run", "jobs", "update", "f42-collect", "--image", f"{REPO}@{jw.NEW_DIGEST}", "--project", "ogilvy-trends-v2",
                   "--region", "us-central1", "--quiet"]
    traffic = ["gcloud", "run", "services", "update-traffic", "f42-api", "--project", "ogilvy-trends-v2", "--region", "us-central1",
               "--to-revisions=f42-api-rel-d666ef6-01=100", "--quiet"]
    jobs_helper = ["py", "-3.13", "core/setup/release/bound_readback.py", "--mode", "jobs", "--phase", "AfterJobsUpdate", "--bindings", "b", "--evidence", "e"]
    services_helper = [*jobs_helper[:4], "services-only", "--phase", "AfterPromotion", *jobs_helper[7:]]
    assert plan.jobs_matching_entries(jobs_update) == ["jobs_update"] and plan.matching_entries(jobs_update) == []
    assert plan.matching_entries(traffic) == ["traffic"] and plan.jobs_matching_entries(traffic) == []
    assert plan.jobs_matching_entries(jobs_helper) == ["helper"] and plan.matching_entries(jobs_helper) == []
    assert plan.matching_entries(services_helper) == ["helper"] and plan.jobs_matching_entries(services_helper) == []


HELPER = ["py", "-3.13", "core/setup/release/bound_readback.py", "--mode", "jobs", "--phase", "AfterJobsUpdate", "--bindings", "b.json", "--evidence", "run"]


def test_ju02_the_helper_matcher_accepts_mode_jobs_with_the_phases_of_3_8_and_keeps_services_only_apart():
    for phase in ("BeforeAnyWrite", "FreezeJobs", "BeforeJobsUpdate", "AfterJobsUpdate", "BeforeJobsRollback", "AfterJobsRollback"):
        argv = [*HELPER[:6], phase, *HELPER[7:]]
        assert plan.jobs_matching_entries(argv) == ["helper"], phase
    services_phase = [*HELPER[:6], "AfterPromotion", *HELPER[7:]]
    assert plan.jobs_matching_entries(services_phase) == []
    mixed = [*HELPER[:3], "--mode", "services-only", "--phase", "AfterJobsUpdate", *HELPER[7:]]
    assert plan.jobs_matching_entries(mixed) == []
    assert plan.jobs_matching_entries([*HELPER[:3], "--mode", "full", "--phase", "AfterJobsUpdate", *HELPER[7:]]) == []
    assert plan.jobs_matching_entries([*HELPER, "--tag", jw.B_RID]) == []
    assert plan.jobs_matching_entries([*HELPER[:3], "--mode", "services-only", "--phase", "AfterPromotion", *HELPER[7:]]) == []


@pytest.mark.parametrize("argv", [
    ["gcloud", "run", "jobs", "execute", "f42-collect", "--project", "ogilvy-trends-v2", "--region", "us-central1"],
    ["gcloud", "run", "jobs", "update", "f42-collect", "--image", f"{REPO}@{jw.NEW_DIGEST}", "--project", "ogilvy-trends-v2", "--region",
     "us-central1", "--quiet", "--update-env-vars", "A=1"],
    ["gcloud", "run", "jobs", "update", "f42-collect", "--image", f"{REPO}@{jw.NEW_DIGEST}", "--project", "ogilvy-trends-v2", "--region",
     "us-central1", "--quiet", "--set-secrets", "A=b:1"],
    ["gcloud", "run", "jobs", "update", "f42-collect", "--image", f"{REPO}:latest", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--quiet"],
    ["gcloud", "run", "jobs", "update", "f42-collect", "--image", f"{REPO}@sha256:abc", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--quiet"],
    ["gcloud", "run", "jobs", "update", "f42-agent", "--image", f"{REPO}@{jw.NEW_DIGEST}", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--quiet"],
    ["gcloud", "run", "jobs", "update", "f42-collect", "--image", f"{REPO}@{jw.NEW_DIGEST}", "--project", "other", "--region", "us-central1", "--quiet"],
    ["gcloud", "run", "jobs", "update", "f42-collect", "--image", f"{REPO}@{jw.NEW_DIGEST}", "--project", "ogilvy-trends-v2", "--region", "europe-west1", "--quiet"],
    ["gcloud", "run", "jobs", "update", "f42-collect", "--image", "evil.example/jobs@" + jw.NEW_DIGEST, "--project", "ogilvy-trends-v2", "--region", "us-central1", "--quiet"],
    ["gcloud", "run", "jobs", "delete", "f42-collect", "--project", "ogilvy-trends-v2", "--region", "us-central1"],
    ["gcloud", "run", "jobs", "executions", "cancel", "x", "--project", "ogilvy-trends-v2", "--region", "us-central1"],
    ["gcloud", "scheduler", "jobs", "pause", "f42-collect-0200"],
    ["gcloud", "run", "deploy", "f42-api"],
    ["gcloud", "projects", "add-iam-policy-binding", "ogilvy-trends-v2"],
    ["bq", "query", "select 1"],
], ids=["execute", "update_env", "set_secrets", "tag_image", "short_digest", "service_name", "other_project", "other_region", "other_repo",
        "delete", "cancel", "scheduler", "deploy", "iam", "bq"])
def test_ju02_a_call_that_is_not_in_the_positive_list_matches_no_entry(argv):
    assert plan.jobs_matching_entries(argv) == []


def test_ju02_the_executions_reads_and_the_chain_evidence_and_the_build_pseudo_calls_are_judged_by_shape():
    read = ["gcloud", "run", "jobs", "executions", "list", "--job", "f42-collect", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--format=json"]
    describe = ["gcloud", "run", "jobs", "executions", "describe", "f42-collect-abc12", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--format=json"]
    assert plan.jobs_matching_entries(read) == ["executions_read"] and plan.jobs_matching_entries(describe) == ["executions_read"]
    assert plan.jobs_matching_entries([*read[:5], "f42-agent", *read[6:]]) == []
    assert plan.jobs_matching_entries([*read, "--limit", "5"]) == []
    evidence = ["py", "-3.13", "core/setup/release/chain_evidence.py", "--date", "2026-10-10", "--bindings", "b.json", "--evidence", "run", "--expect", "baseline"]
    assert plan.jobs_matching_entries(evidence) == ["chain_evidence"]
    assert plan.jobs_matching_entries([*evidence[:9], "--expect", "anything"]) == []
    assert plan.jobs_matching_entries([*evidence[:3], "--date", "yesterday", *evidence[5:]]) == []
    upload = ["gcloud", "storage", "cp", "C:/run/source.tar.gz", "gs://ogilvy-trends-v2-f42-media-staging/build-source/jobs-b5e1a2c4d6f8-01.tar.gz",
              "--no-clobber", "--project=ogilvy-trends-v2"]
    assert plan.jobs_matching_entries(upload) == ["jobs_build"]
    assert plan.jobs_matching_entries([*upload[:6]]) == []
    assert plan.jobs_matching_entries([*upload[:4], "gs://other-bucket/x.tar.gz", *upload[5:]]) == []
    create = ["POST", "https://cloudbuild.googleapis.com/v1/projects/ogilvy-trends-v2/locations/us-central1/builds"]
    assert plan.jobs_matching_entries(create) == ["jobs_build"]
    assert plan.jobs_matching_entries(["POST", "https://example.com/builds"]) == []
    assert plan.jobs_matching_entries(["DELETE", create[1]]) == []


def test_ju02_the_orchestrator_entry_takes_only_its_three_actions_and_the_two_bound_paths():
    base = ["py", "-3.13", "core/setup/release/jobs_run.py"]
    for action in ("snapshot", "update", "rollback"):
        assert plan.jobs_matching_entries([*base, action, "--bindings", "b.json", "--evidence", "run"]) == ["jobs_run"]
    assert plan.jobs_matching_entries([*base, "execute", "--bindings", "b.json", "--evidence", "run"]) == []
    assert plan.jobs_matching_entries([*base, "update", "--bindings", "b.json", "--evidence", "run", "--force"]) == []
    assert plan.jobs_matching_entries([*base, "update", "--evidence", "run"]) == []


def test_ju02_the_services_actions_are_judged_as_before(tmp_path):
    from core.setup.tests.test_services_paste import ctx_for
    from core.setup.tests.paste_world import PasteWorld

    world = PasteWorld(tmp_path)
    for action in plan.ACTIONS.values():
        assert plan.check_allowlist(action(ctx_for(world))) is None
    assert plan.check_jobs_allowlist(plan.candidate(ctx_for(world))) is not None


# RB-T3: the schema readback receipt step follows the durable readbacks in JobsCandidate and is on the positive list

def test_rb_t3_the_candidate_writes_the_schema_readback_receipt_after_the_durable_readbacks():
    steps = plan.JOBS_ACTIONS["JobsCandidate"](ctx(schema_effects=True))
    names = [s.name for s in steps]
    assert names[-2:] == ["checker:readbacks", "jobs_run:schema-receipt"]
    receipt = steps[-1]
    assert receipt.argv == ("py", "-3.13", "core/setup/release/jobs_run.py", "schema-receipt", "--bindings", "C:/release/bindings.json", "--evidence",
                            "C:/release/run")
    assert plan.jobs_matching_entries(list(receipt.argv)) == ["jobs_run"] and plan.matching_entries(list(receipt.argv)) == []
    assert "schema-receipt" in plan.JOBS_RUN_ACTIONS


# RB-T8 (P03): the describe name shape is part of the positive list on the acting path

def test_rb_t8_an_execution_describe_whose_name_is_not_the_plans_shape_matches_no_entry():
    good = ["gcloud", "run", "jobs", "executions", "describe", "f42-collect-aaaaa", "--project", "ogilvy-trends-v2", "--region", "us-central1", "--format=json"]
    assert plan.jobs_matching_entries(good) == ["executions_read"]
    for name in ("--project=evil", "-x", "F42-collect-aaaaa", "f42-collect aaaaa", "other-collect-1", "f42-", ""):
        assert plan.jobs_matching_entries([*good[:5], name, *good[6:]]) == [], name


# JB-03 as amended by RJ-4: one build step, run from the clean clone, bound to the commit, the attempt and the run folder

BUILD = ["py", "-3.13", "core/setup/deploy_jobs.py", "--build", "--commit", jw.B_COMMIT, "--attempt", "01", "--build-id-file", "C:/release/run/build-id.txt"]


def test_ju02_the_candidate_renders_one_build_step_with_the_commit_the_attempt_of_the_release_id_and_the_run_folder_id_file():
    steps = plan.JOBS_ACTIONS["JobsCandidate"](ctx(build_id_file="C:/release/run/build-id.txt"))
    build = next(s for s in steps if s.name == "build")
    assert list(build.argv) == BUILD
    assert plan.jobs_matching_entries(BUILD) == ["jobs_build_start"] and plan.matching_entries(BUILD) == []
    inside = [s.name for s in steps if s.via == "deploy_jobs"]
    assert inside == ["build:upload", "build:create"]
    attempt = plan.JOBS_ACTIONS["JobsCandidate"](ctx(release_id="rel-b5e1a2c-07", build_id_file="C:/release/run/build-id.txt"))
    assert next(s for s in attempt if s.name == "build").argv[6:8] == ("--attempt", "07")


@pytest.mark.parametrize("change", [
    {5: jw.B_COMMIT[:12]}, {5: jw.B_COMMIT.upper()}, {7: "1"}, {7: "00"}, {7: "100"}, {7: "x1"}, {9: "C:/release/run/notes.txt"},
    {9: "C:/release/run/build-id.txt.bak"}, {1: "-3.12"}, {2: "core/setup/deploy_jobs.pyc"},
], ids=["short_commit", "upper_commit", "one_digit_attempt", "attempt_zero", "three_digit_attempt", "attempt_text", "other_file",
        "backup_file", "other_python", "other_script"])
def test_ju02_the_positive_list_refuses_a_build_command_with_a_wrong_value(change):
    argv = list(BUILD)
    for index, value in change.items():
        argv[index] = value
    assert plan.jobs_matching_entries(argv) == []


@pytest.mark.parametrize("extra", [["--apply"], ["--only", "f42-probe"], ["--smoke"], ["--model-provider", "gemini"], ["--run-smoke"], ["--from-archive"]])
def test_ju02_the_positive_list_refuses_every_flag_beyond_the_build_the_commit_the_attempt_and_the_id_file(extra):
    assert plan.jobs_matching_entries([*BUILD, *extra]) == []
    assert plan.jobs_matching_entries([*BUILD[:4], *extra, *BUILD[4:]]) == []
    without = [a for a in BUILD if a != "--build"]
    assert plan.jobs_matching_entries(without) == []
    no_commit = BUILD[:4] + BUILD[6:]
    assert plan.jobs_matching_entries(no_commit) == []
