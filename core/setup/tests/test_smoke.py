"""Unit tests for core/setup/smoke.py. No cloud access: a fake cloud stands in for BigQuery, the bucket, the
Cloud Run API and the model, a fake gcloud records the builder's calls, and sleeps are recorded, not slept."""
import json
import re
from pathlib import Path

import pytest

from core.setup import smoke

SETUP = Path(smoke.__file__).resolve().parent
SECRET_VALUE = "sc-fake-key-value-never-printed"
EXECUTION = "f42-smoke-x-abc12"
CORE_TABLE = "ogilvy-trends-v2.intelligence_42_core.smoke_checks"
AGENT_TABLE = "ogilvy-trends-v2.intelligence_42_agent.smoke_checks"
JOB_ROLES = ("collector", "enricher", "agent", "brief", "web")
OLD_EXECUTION = "f42-smoke-web-old01"
NEW_EXECUTION = "f42-smoke-web-new01"


class FakeCloud:
    """Records every call. fail names the methods that raise; affected is what a DML statement reports."""

    def __init__(self, fail=(), affected=1, model=False, embedding=None, tokens=3, obj=b"42 smoke object"):
        self.fail = set(fail)
        self.affected = affected
        self.model = model
        self.embedding = embedding if embedding is not None else [{"dims": 768, "status": ""}]
        self.tokens = tokens
        self.obj = obj
        self.calls = []

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if name in self.fail:
            raise RuntimeError(f"{name} refused: HTTP 403")

    def dml(self, sql, params=None):
        self._call("dml", sql, params)
        return None if sql.lstrip().upper().startswith("CREATE") else self.affected

    def query(self, sql, params=None):
        self._call("query", sql, params)
        if "ML.GENERATE_EMBEDDING" in sql:
            return self.embedding
        return [{"n": 0}]

    def model_exists(self, ref):
        self._call("model_exists", ref)
        return self.model

    def read_object(self, bucket, name):
        self._call("read_object", bucket, name)
        return self.obj

    def start_job(self, job, env):
        self._call("start_job", job, env)

    def complete_gemini(self, model, prompt, max_tokens):
        self._call("complete_gemini", model, prompt, max_tokens)
        return self.tokens

    def names(self):
        return [c[0] for c in self.calls]


class FakeGcloud:
    """fail holds argv prefixes that raise before the server acts; late_fail holds prefixes the server applies
    before the client sees an error (a timeout after the change landed). After a Scheduler run-now, the noop job
    shows a new execution on the delay-th executions list, unless dispatch is False."""

    def __init__(self, existing=(), state="PAUSED", fail=(), listed=True, dispatch=True, delay=1, late_fail=()):
        self.existing = set(existing)
        self.state = state
        self.fail = [tuple(f) for f in fail]
        self.late_fail = [tuple(f) for f in late_fail]
        self.listed = listed
        self.dispatch = dispatch
        self.delay = delay
        self.executions = [OLD_EXECUTION]
        self.dispatched = False
        self.lists_after_run = 0
        self.calls = []

    def run(self, args):
        self.calls.append(list(args))
        if any(tuple(args[:len(f)]) == f for f in self.fail):
            raise smoke.schedule.GcloudError(f"gcloud {' '.join(args[:4])} failed: PERMISSION_DENIED")
        if args[:2] == ["scheduler", "jobs"] and args[2] in ("describe", "pause", "resume", "run") \
                and args[3] not in self.existing:
            raise smoke.schedule.GcloudError(f"gcloud {' '.join(args[:4])} failed: NOT_FOUND")
        out = self.apply(args)
        if any(tuple(args[:len(f)]) == f for f in self.late_fail):
            raise smoke.schedule.GcloudError(f"gcloud {' '.join(args[:4])} failed: DEADLINE_EXCEEDED")
        return out

    def apply(self, args):
        if args[:4] == ["run", "jobs", "executions", "list"]:
            if self.dispatched:
                self.lists_after_run += 1
                if self.lists_after_run == self.delay and NEW_EXECUTION not in self.executions:
                    self.executions.append(NEW_EXECUTION)
            return "".join(e + "\n" for e in self.executions)
        if args[:3] == ["scheduler", "jobs", "resume"]:
            self.state = "ENABLED"
        if args[:3] == ["scheduler", "jobs", "run"]:
            self.dispatched = self.dispatch
        if args[:3] == ["scheduler", "jobs", "list"]:
            return "".join(f"projects/ogilvy-trends-v2/locations/us-central1/jobs/{n}\n" for n in self.existing)
        if args[:3] == ["scheduler", "jobs", "create"]:
            self.existing.add(args[4])
            self.state = "ENABLED"
        if args[:3] == ["scheduler", "jobs", "pause"]:
            self.state = "PAUSED"
        if args[:3] == ["scheduler", "jobs", "describe"]:
            return self.state + "\n"
        if args[:3] == ["storage", "ls", smoke.OBJECT_URL]:
            return smoke.OBJECT_URL + "\n" if self.listed else ""
        return ""


def job_env(**extra):
    return {"CLOUD_RUN_JOB": "f42-smoke-x", "CLOUD_RUN_EXECUTION": EXECUTION, smoke.SECRET: SECRET_VALUE, **extra}


def run(role, cloud=None, env=None, gcloud=None):
    cloud = cloud or FakeCloud()
    env = job_env() if env is None else env
    code = smoke.main(["--role", role], cloud=cloud, gcloud=gcloud or FakeGcloud(), env=env)
    return code, cloud


def lines(out):
    return {ln.split()[0]: ln.split()[1] for ln in out.splitlines() if len(ln.split()) > 1
            and ln.split()[1] in ("PASS", "FAIL", "SKIPPED")}


# Roles and their checks


def test_every_identity_has_a_role_and_the_brief_checks():
    assert smoke.JOB_ROLES == JOB_ROLES
    assert smoke.CHECKS == {
        "collector": ("merge", "secret", "start_noop"),
        "enricher": ("merge", "secret", "bucket_read", "model_call", "embedding"),
        "agent": ("select_core", "append_agent", "model_call", "secret"),
        "brief": ("merge", "secret", "model_call", "start_noop"),
        "web": ("select_core", "bucket_read"),
        "builder": ("bucket_write", "scheduler_create", "scheduler_dispatch"),
    }


@pytest.mark.parametrize("role", JOB_ROLES)
def test_each_role_passes_with_a_healthy_cloud_and_prints_one_line_per_check(role, capsys):
    code, _ = run(role, cloud=FakeCloud(model=True))
    out = capsys.readouterr().out
    assert code == 0
    assert lines(out) == {name: "PASS" for name in smoke.CHECKS[role]}


def test_collector_calls_only_its_three_checks(capsys):
    _, cloud = run("collector")
    assert cloud.names() == ["dml", "dml", "start_job"]


# merge


def test_merge_creates_the_scratch_table_if_missing_then_merges_one_row_for_this_execution(capsys):
    _, cloud = run("collector")
    create, merge = [c for c in cloud.calls if c[0] == "dml"]
    assert create[1].startswith(f"CREATE TABLE IF NOT EXISTS `{CORE_TABLE}`")
    assert merge[1].lstrip().startswith(f"MERGE `{CORE_TABLE}`")
    assert "WHEN NOT MATCHED THEN INSERT" in merge[1]
    assert merge[2] == {"role": "collector", "execution": EXECUTION}
    assert lines(capsys.readouterr().out)["merge"] == "PASS"


def test_merge_fails_when_no_row_lands(capsys):
    code, _ = run("collector", cloud=FakeCloud(affected=0))
    out = capsys.readouterr().out
    assert code == 1
    assert lines(out)["merge"] == "FAIL"
    assert "expected 1" in out


def test_merge_fails_when_bigquery_refuses(capsys):
    code, _ = run("brief", cloud=FakeCloud(fail={"dml"}))
    out = capsys.readouterr().out
    assert code == 1
    assert lines(out)["merge"] == "FAIL"
    assert "403" in out


# secret


def test_secret_passes_on_a_non_empty_mount_and_never_prints_the_value_or_its_length(capsys):
    code, _ = run("collector")
    captured = capsys.readouterr()
    assert code == 0
    assert SECRET_VALUE not in captured.out + captured.err
    line = next(ln for ln in captured.out.splitlines() if ln.startswith("secret"))
    assert "PASS" in line and not re.search(r"\d", line)


@pytest.mark.parametrize("value", [None, ""])
def test_secret_fails_when_the_mount_is_missing_or_empty(value, capsys):
    env = job_env()
    if value is None:
        env.pop(smoke.SECRET)
    else:
        env[smoke.SECRET] = value
    code, _ = run("enricher", env=env)
    assert code == 1
    assert lines(capsys.readouterr().out)["secret"] == "FAIL"


def test_no_secret_value_in_output_for_any_role_even_when_every_check_fails(capsys):
    fail = {"dml", "query", "model_exists", "read_object", "start_job", "complete_gemini"}
    for role in JOB_ROLES:
        run(role, cloud=FakeCloud(fail=fail))
    captured = capsys.readouterr()
    assert SECRET_VALUE not in captured.out + captured.err


# start_noop


def test_start_noop_starts_the_noop_job_with_the_override(capsys):
    _, cloud = run("brief")
    starts = [c for c in cloud.calls if c[0] == "start_job"]
    assert starts == [("start_job", smoke.NOOP_JOB, {"SMOKE_NOOP": "1"})]
    assert smoke.NOOP_JOB == "f42-smoke-web"


def test_start_noop_fails_when_the_job_cannot_be_started(capsys):
    code, _ = run("collector", cloud=FakeCloud(fail={"start_job"}))
    out = capsys.readouterr().out
    assert code == 1 and lines(out)["start_noop"] == "FAIL"


def test_noop_target_starts_nothing_itself_so_a_broken_noop_cannot_loop():
    assert "start_noop" not in smoke.CHECKS["web"]


def test_no_job_role_runs_scheduler_itself_the_builder_proves_the_scheduler_token():
    assert "scheduler" not in smoke.JOB_ROLES and "scheduler" not in smoke.CHECKS
    assert not any("scheduler" in check for role in JOB_ROLES for check in smoke.CHECKS[role])
    assert not hasattr(smoke.Cloud, "run_scheduler")


# bucket_read


def test_bucket_read_reads_the_builder_object(capsys):
    _, cloud = run("web")
    assert ("read_object", "ogilvy-trends-v2-f42-media-staging", "smoke/smoke.txt") in cloud.calls
    assert lines(capsys.readouterr().out)["bucket_read"] == "PASS"


@pytest.mark.parametrize("cloud", [FakeCloud(fail={"read_object"}), FakeCloud(obj=b"")])
def test_bucket_read_fails_on_a_refusal_or_an_empty_object(cloud, capsys):
    code, _ = run("web", cloud=cloud)
    assert code == 1 and lines(capsys.readouterr().out)["bucket_read"] == "FAIL"


# model_call


def test_model_call_defaults_to_gemini_flash_with_16_tokens(capsys):
    _, cloud = run("agent")
    calls = [c for c in cloud.calls if c[0] == "complete_gemini"]
    assert len(calls) == 1
    assert calls[0][1] == "gemini-3.8-flash" and calls[0][3] == 16
    assert lines(capsys.readouterr().out)["model_call"] == "PASS"


@pytest.mark.parametrize("cloud", [FakeCloud(fail={"complete_gemini"}), FakeCloud(tokens=0)])
def test_model_call_fails_on_a_refusal_or_an_empty_reply(cloud, capsys):
    code, _ = run("brief", cloud=cloud)
    assert code == 1 and lines(capsys.readouterr().out)["model_call"] == "FAIL"


@pytest.mark.parametrize("role", ["enricher", "agent", "brief"])
def test_model_call_with_model_provider_gemini_calls_gemini_flash_with_16_tokens(role, capsys):
    _, cloud = run(role, env=job_env(MODEL_PROVIDER="gemini"))
    assert [c[1:] for c in cloud.calls if c[0] == "complete_gemini"] == [
        ("gemini-3.8-flash", "Reply with the single word ok.", 16)]
    assert cloud.names().count("complete_gemini") == 1
    out = capsys.readouterr().out
    assert lines(out)["model_call"] == "PASS" and "gemini-3.8-flash on Vertex replied" in out


@pytest.mark.parametrize("cloud", [FakeCloud(fail={"complete_gemini"}), FakeCloud(tokens=0)])
def test_gemini_model_call_fails_on_a_refusal_or_an_empty_reply(cloud, capsys):
    code, _ = run("brief", cloud=cloud, env=job_env(MODEL_PROVIDER="gemini"))
    assert code == 1 and lines(capsys.readouterr().out)["model_call"] == "FAIL"


def test_an_unknown_model_provider_fails_the_model_call_naming_it_and_calls_no_model(capsys):
    code, cloud = run("agent", env=job_env(MODEL_PROVIDER="openai"))
    out = capsys.readouterr().out
    assert code == 1 and lines(out)["model_call"] == "FAIL" and "openai" in out
    assert "complete_gemini" not in cloud.names()


# embedding


def test_embedding_is_skipped_while_the_remote_model_is_missing_and_the_job_still_passes(capsys):
    code, cloud = run("enricher", cloud=FakeCloud(model=False))
    out = capsys.readouterr().out
    assert code == 0
    assert lines(out)["embedding"] == "SKIPPED"
    assert "1.8" in out
    assert not any(c[0] == "query" and "ML.GENERATE_EMBEDDING" in c[1] for c in cloud.calls)


def test_embedding_model_is_the_one_lane_l3_created():
    assert smoke.EMBED_MODEL == "ogilvy-trends-v2.intelligence_42_core.embed_gemini"


def test_embedding_runs_on_one_row_once_the_model_exists(capsys):
    code, cloud = run("enricher", cloud=FakeCloud(model=True))
    assert code == 0
    sql = next(c[1] for c in cloud.calls if c[0] == "query" and "ML.GENERATE_EMBEDDING" in c[1])
    assert f"MODEL `{smoke.EMBED_MODEL}`" in sql
    assert lines(capsys.readouterr().out)["embedding"] == "PASS"


@pytest.mark.parametrize("cloud", [
    FakeCloud(model=True, embedding=[{"dims": 0, "status": ""}]),
    FakeCloud(model=True, embedding=[{"dims": None, "status": "Permission denied on connection"}]),
    FakeCloud(model=True, embedding=[]),
    FakeCloud(model=True, fail={"query"}),
])
def test_embedding_fails_on_an_empty_vector_a_status_no_row_or_a_refusal(cloud, capsys):
    code, _ = run("enricher", cloud=cloud)
    assert code == 1 and lines(capsys.readouterr().out)["embedding"] == "FAIL"


# select_core and append_agent


def test_select_core_reads_a_core_table(capsys):
    _, cloud = run("web")
    sql = next(c[1] for c in cloud.calls if c[0] == "query")
    assert "`ogilvy-trends-v2.intelligence_42_core.posts`" in sql and sql.lstrip().upper().startswith("SELECT")
    assert lines(capsys.readouterr().out)["select_core"] == "PASS"


def test_select_core_fails_when_bigquery_refuses(capsys):
    code, _ = run("agent", cloud=FakeCloud(fail={"query"}))
    assert code == 1 and lines(capsys.readouterr().out)["select_core"] == "FAIL"


def test_agent_appends_one_row_to_its_own_dataset_and_never_writes_core(capsys):
    _, cloud = run("agent")
    dml = [c for c in cloud.calls if c[0] == "dml"]
    assert dml[0][1].startswith(f"CREATE TABLE IF NOT EXISTS `{AGENT_TABLE}`")
    assert dml[1][1].lstrip().startswith(f"INSERT INTO `{AGENT_TABLE}`")
    assert dml[1][2] == {"role": "agent", "execution": EXECUTION}
    assert not any("intelligence_42_core" in c[1] for c in dml)
    assert lines(capsys.readouterr().out)["append_agent"] == "PASS"


@pytest.mark.parametrize("cloud", [FakeCloud(affected=0), FakeCloud(fail={"dml"})])
def test_append_agent_fails_when_no_row_lands_or_bigquery_refuses(cloud, capsys):
    code, _ = run("agent", cloud=cloud)
    assert code == 1 and lines(capsys.readouterr().out)["append_agent"] == "FAIL"


def test_scratch_writes_only_create_if_missing_merge_and_insert():
    for role in JOB_ROLES:
        cloud = FakeCloud(model=True)
        smoke.main(["--role", role], cloud=cloud, gcloud=FakeGcloud(), env=job_env())
        for call in cloud.calls:
            if call[0] in ("dml", "query"):
                words = set(re.findall(r"[A-Z]+", call[1].upper()))
                assert not words & {"DELETE", "DROP", "TRUNCATE", "REPLACE", "EXPIRATION", "ALTER"}, call[1]
                if call[0] == "dml":
                    assert call[1].lstrip().split()[0] in ("CREATE", "MERGE", "INSERT"), call[1]


# noop and the Cloud Run guard


@pytest.mark.parametrize("role", JOB_ROLES + ("builder",))
def test_noop_mode_exits_0_at_once_and_touches_nothing(role, capsys):
    cloud, gcloud = FakeCloud(), FakeGcloud()
    code = smoke.main(["--role", role], cloud=cloud, gcloud=gcloud, env={"SMOKE_NOOP": "1"})
    assert code == 0
    assert cloud.calls == [] and gcloud.calls == []
    assert "SMOKE_NOOP" in capsys.readouterr().out


def test_noop_mode_needs_no_arguments():
    assert smoke.main([], cloud=FakeCloud(), gcloud=FakeGcloud(), env={"SMOKE_NOOP": "1"}) == 0


def test_a_job_role_outside_cloud_run_refuses_because_it_would_test_the_wrong_identity(capsys):
    env = job_env()
    env.pop("CLOUD_RUN_JOB")
    code, cloud = run("collector", env=env)
    assert code == 2 and cloud.calls == []
    assert "Cloud Run" in capsys.readouterr().out


def test_an_unknown_role_is_rejected():
    with pytest.raises(SystemExit):
        smoke.main(["--role", "deployer"], cloud=FakeCloud(), gcloud=FakeGcloud(), env=job_env())


def test_the_summary_counts_passes_failures_and_skips(capsys):
    run("enricher", cloud=FakeCloud(fail={"complete_gemini"}))
    out = capsys.readouterr().out
    assert "enricher: 3 passed, 1 failed, 1 skipped" in out


# builder


def builder(gcloud, sleeps=None):
    sleeps = [] if sleeps is None else sleeps
    return smoke.main(["--role", "builder"], cloud=FakeCloud(), gcloud=gcloud, env={}, sleep=sleeps.append)


def scheduler_verbs(gcloud):
    return [c[2] for c in gcloud.calls if c[:2] == ["scheduler", "jobs"]]


def test_builder_writes_the_object_without_overwriting_and_creates_the_scheduler_job_paused(capsys):
    gcloud = FakeGcloud()
    code = builder(gcloud)
    out = capsys.readouterr().out
    assert code == 0
    assert lines(out) == {"bucket_write": "PASS", "scheduler_create": "PASS", "scheduler_dispatch": "PASS"}
    cp = next(c for c in gcloud.calls if c[:2] == ["storage", "cp"])
    assert cp[3] == "gs://ogilvy-trends-v2-f42-media-staging/smoke/smoke.txt"
    assert "--no-clobber" in cp
    kinds = [c[:3] for c in gcloud.calls if c[0] == "scheduler"]
    assert kinds.index(["scheduler", "jobs", "create"]) < kinds.index(["scheduler", "jobs", "pause"])
    assert "created" in out


def test_builder_scheduler_job_targets_the_noop_job_as_f42_scheduler_with_the_override():
    argv = smoke.schedule_create_argv()
    assert argv[:5] == ["scheduler", "jobs", "create", "http", smoke.SCHEDULE_JOB]

    def flag(name):
        hits = [a.split("=", 1)[1] for a in argv if a.startswith(name + "=")]
        assert len(hits) == 1, name
        return hits[0]

    assert flag("--uri") == ("https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/"
                             "jobs/f42-smoke-web:run")
    assert flag("--http-method") == "POST"
    assert flag("--oauth-service-account-email") == "f42-scheduler@ogilvy-trends-v2.iam.gserviceaccount.com"
    body = json.loads(flag("--message-body"))
    assert body == {"overrides": {"containerOverrides": [{"env": [{"name": "SMOKE_NOOP", "value": "1"}]}]}}
    assert flag("--headers") == "Content-Type=application/json"
    assert flag("--project") == "ogilvy-trends-v2" and flag("--location") == "us-central1"
    assert flag("--max-retry-attempts") == "0"


def test_builder_does_not_recreate_an_existing_paused_scheduler_job_and_pauses_only_after_its_resume(capsys):
    gcloud = FakeGcloud(existing={smoke.SCHEDULE_JOB}, state="PAUSED")
    code = builder(gcloud)
    assert code == 0
    verbs = scheduler_verbs(gcloud)
    assert "create" not in verbs
    assert verbs.count("resume") == 1 and verbs.count("pause") == 1
    assert verbs.index("resume") < verbs.index("pause")
    assert "already exists and is paused" in capsys.readouterr().out


def test_builder_pauses_an_existing_enabled_smoke_scheduler_job(capsys):
    gcloud = FakeGcloud(existing={smoke.SCHEDULE_JOB}, state="ENABLED")
    code = builder(gcloud)
    assert code == 0
    assert not any(c[:3] == ["scheduler", "jobs", "create"] for c in gcloud.calls)
    assert any(c[:3] == ["scheduler", "jobs", "pause"] for c in gcloud.calls)


@pytest.mark.parametrize("gcloud, check", [
    (FakeGcloud(fail=[("storage", "cp")]), "bucket_write"),
    (FakeGcloud(listed=False), "bucket_write"),
    (FakeGcloud(fail=[("scheduler", "jobs", "create")]), "scheduler_create"),
    (FakeGcloud(fail=[("scheduler", "jobs", "pause")]), "scheduler_create"),
])
def test_builder_checks_fail_when_gcloud_refuses_or_the_object_is_absent(gcloud, check, capsys):
    code = builder(gcloud)
    assert code == 1
    assert lines(capsys.readouterr().out)[check] == "FAIL"


def test_builder_never_touches_the_cloud_seam_and_never_removes_or_grants():
    gcloud, cloud = FakeGcloud(), FakeCloud()
    smoke.main(["--role", "builder"], cloud=cloud, gcloud=gcloud, env={}, sleep=lambda s: None)
    assert cloud.calls == []
    assert gcloud.calls
    for call in gcloud.calls:
        assert not set(call[:4]) & {"delete", "rm", "update", "add-iam-policy-binding"}, call
        assert not any("delete" in a.lower() or "roles/" in a for a in call), call
        assert "--project=ogilvy-trends-v2" in call, call
    state_changes = [v for v in scheduler_verbs(gcloud) if v in ("resume", "pause")]
    assert state_changes[-1] == "pause"


# scheduler_dispatch: the builder runs the Scheduler job now, the job's token (f42-scheduler) starts the noop
# job, and the builder waits for the new execution.


def test_dispatch_resumes_runs_now_and_pauses_again_then_sees_a_new_noop_execution(capsys):
    gcloud, sleeps = FakeGcloud(delay=3), []
    code = builder(gcloud, sleeps)
    out = capsys.readouterr().out
    assert code == 0 and lines(out)["scheduler_dispatch"] == "PASS"
    assert NEW_EXECUTION in out and "f42-scheduler" in out
    calls = [c[:4] for c in gcloud.calls]
    listing = ["run", "jobs", "executions", "list"]
    resume = calls.index(["scheduler", "jobs", "resume", smoke.SCHEDULE_JOB])
    run_now = calls.index(["scheduler", "jobs", "run", smoke.SCHEDULE_JOB])
    pause = max(i for i, c in enumerate(calls) if c[:3] == ["scheduler", "jobs", "pause"])
    assert calls.index(listing) < resume < run_now < pause
    assert sleeps == [10, 10, 10]
    assert gcloud.state == "PAUSED"


def test_dispatch_lists_executions_of_the_noop_job_in_full():
    gcloud = FakeGcloud()
    builder(gcloud)
    listing = next(c for c in gcloud.calls if c[:4] == ["run", "jobs", "executions", "list"])
    assert "--job=f42-smoke-web" in listing and "--region=us-central1" in listing
    assert not any(a.startswith("--limit") for a in listing)


def test_dispatch_fails_after_two_minutes_without_a_new_execution_and_leaves_the_job_paused(capsys):
    gcloud, sleeps = FakeGcloud(dispatch=False), []
    code = builder(gcloud, sleeps)
    out = capsys.readouterr().out
    assert code == 1 and lines(out)["scheduler_dispatch"] == "FAIL"
    assert sum(sleeps) == 120 and set(sleeps) == {10}
    assert "120" in out
    assert gcloud.state == "PAUSED"


def test_an_execution_that_existed_before_the_run_now_does_not_count(capsys):
    gcloud = FakeGcloud(dispatch=False)
    gcloud.executions = [OLD_EXECUTION, "f42-smoke-web-old02"]
    assert builder(gcloud) == 1
    assert lines(capsys.readouterr().out)["scheduler_dispatch"] == "FAIL"


def test_a_refused_run_now_fails_and_still_pauses_the_job_again(capsys):
    gcloud, sleeps = FakeGcloud(fail=[("scheduler", "jobs", "run")]), []
    code = builder(gcloud, sleeps)
    out = capsys.readouterr().out
    assert code == 1 and lines(out)["scheduler_dispatch"] == "FAIL" and "PERMISSION_DENIED" in out
    verbs = scheduler_verbs(gcloud)
    assert verbs.index("resume") < max(i for i, v in enumerate(verbs) if v == "pause")
    assert gcloud.state == "PAUSED" and sleeps == []


def test_a_refused_resume_fails_never_runs_the_job_and_still_attempts_the_pause(capsys):
    gcloud = FakeGcloud(existing={smoke.SCHEDULE_JOB}, fail=[("scheduler", "jobs", "resume")])
    assert builder(gcloud) == 1
    assert lines(capsys.readouterr().out)["scheduler_dispatch"] == "FAIL"
    verbs = scheduler_verbs(gcloud)
    assert "run" not in verbs
    assert verbs.index("resume") < verbs.index("pause")


def test_a_resume_the_server_applied_but_the_client_saw_fail_is_paused_again(capsys):
    gcloud = FakeGcloud(existing={smoke.SCHEDULE_JOB}, late_fail=[("scheduler", "jobs", "resume")])
    assert builder(gcloud) == 1
    out = capsys.readouterr().out
    assert lines(out)["scheduler_dispatch"] == "FAIL" and "DEADLINE_EXCEEDED" in out
    assert "run" not in scheduler_verbs(gcloud)
    assert gcloud.state == "PAUSED"


def reason(out, check):
    return next(ln for ln in out.splitlines() if ln.split()[:1] == [check])


PAUSE_COMMAND = "gcloud scheduler jobs pause f42-smoke-noop --project=ogilvy-trends-v2 --location=us-central1"


def test_create_then_a_failed_describe_still_pauses_and_the_fail_names_enabled_and_the_command(capsys):
    gcloud = FakeGcloud(fail=[("scheduler", "jobs", "describe")])
    assert builder(gcloud) == 1
    line = reason(capsys.readouterr().out, "scheduler_create")
    verbs = scheduler_verbs(gcloud)
    assert verbs.index("create") < verbs.index("pause")
    assert gcloud.state == "PAUSED"
    assert "FAIL" in line and "ENABLED" in line and PAUSE_COMMAND in line


def test_a_create_the_server_made_but_the_client_saw_fail_is_paused(capsys):
    gcloud = FakeGcloud(late_fail=[("scheduler", "jobs", "create")])
    assert builder(gcloud) == 1
    line = reason(capsys.readouterr().out, "scheduler_create")
    verbs = scheduler_verbs(gcloud)
    assert verbs.index("create") < verbs.index("pause")
    assert gcloud.state == "PAUSED"
    assert "FAIL" in line and "DEADLINE_EXCEEDED" in line and "confirmed PAUSED" in line


def test_a_refused_create_still_attempts_the_pause_and_names_enabled(capsys):
    gcloud = FakeGcloud(fail=[("scheduler", "jobs", "create")])
    assert builder(gcloud) == 1
    line = reason(capsys.readouterr().out, "scheduler_create")
    verbs = scheduler_verbs(gcloud)
    assert verbs.index("create") < verbs.index("pause")
    assert "FAIL" in line and "ENABLED" in line and PAUSE_COMMAND in line


def test_a_failed_pause_after_create_names_enabled_and_the_command(capsys):
    gcloud = FakeGcloud(fail=[("scheduler", "jobs", "pause")])
    assert builder(gcloud) == 1
    out = capsys.readouterr().out
    for check in ("scheduler_create", "scheduler_dispatch"):
        line = reason(out, check)
        assert "FAIL" in line and "ENABLED" in line and PAUSE_COMMAND in line, line


def test_a_failed_pause_after_the_run_now_fails_and_says_the_job_is_left_enabled(capsys):
    gcloud = FakeGcloud(existing={smoke.SCHEDULE_JOB})
    gcloud.fail = [("scheduler", "jobs", "pause")]
    assert builder(gcloud) == 1
    out = capsys.readouterr().out
    assert lines(out)["scheduler_dispatch"] == "FAIL"
    assert "ENABLED" in out and "gcloud scheduler jobs pause f42-smoke-noop" in out


# the real cloud adapter, with a fake HTTP session


class FakeResponse:
    def __init__(self, status_code=200, content=b"", body=None):
        self.status_code = status_code
        self.content = content
        self.body = body or {}
        self.text = str(self.body)

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, status_code=200, content=b"marker"):
        self.status_code = status_code
        self.content = content
        self.gets, self.posts = [], []

    def get(self, url, timeout=None):
        self.gets.append(url)
        return FakeResponse(self.status_code, self.content)

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return FakeResponse(self.status_code, body={"name": "operations/op1"})


def test_cloud_reads_the_object_through_the_storage_api():
    session = FakeSession()
    assert smoke.Cloud(session=session).read_object("b", "smoke/smoke.txt") == b"marker"
    assert session.gets == ["https://storage.googleapis.com/storage/v1/b/b/o/smoke%2Fsmoke.txt?alt=media"]


def test_cloud_read_object_raises_on_a_refusal():
    with pytest.raises(RuntimeError, match="403"):
        smoke.Cloud(session=FakeSession(403)).read_object("b", "smoke/smoke.txt")


def test_cloud_starts_a_job_through_the_chain_client_with_the_env_override():
    session = FakeSession()
    smoke.Cloud(session=session).start_job("f42-smoke-web", {"SMOKE_NOOP": "1"})
    url, body = session.posts[0]
    assert url == "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/jobs/f42-smoke-web:run"
    assert body == {"overrides": {"containerOverrides": [{"env": [{"name": "SMOKE_NOOP", "value": "1"}]}]}}


class FakeGenaiClient:
    made = []

    def __init__(self, **kwargs):
        FakeGenaiClient.made.append(kwargs)
        self.models = self

    def generate_content(self, **kwargs):
        FakeGenaiClient.made.append(kwargs)
        usage = type("U", (), {"candidates_token_count": 1, "thoughts_token_count": 3})()
        return type("R", (), {"usage_metadata": usage})()


def test_cloud_calls_gemini_through_google_genai_on_vertex_global_with_the_job_identity(monkeypatch):
    from google import genai

    FakeGenaiClient.made = []
    monkeypatch.setattr(genai, "Client", FakeGenaiClient)
    # Billed output on Gemini 3 is the reply plus its thinking, and the limit caps both together.
    assert smoke.Cloud(session=FakeSession()).complete_gemini("gemini-3.8-flash", "Reply ok.", 16) == 4
    client, call = FakeGenaiClient.made
    assert client == {"vertexai": True, "project": "ogilvy-trends-v2", "location": "global"}
    assert call["model"] == "gemini-3.8-flash" and call["contents"] == "Reply ok."
    assert call["config"].max_output_tokens == 16


def test_no_banned_literals_or_prose_dashes_in_the_smoke_files():
    # Built with join so no banned literal survives compile time constant folding into the bytecode.
    banned = re.compile("|".join(["".join(["gen", "z"]), "".join(["gen", " z"]), "".join(["google", "_trends"])]), re.I)
    dashes = re.compile("[" + chr(0x2013) + chr(0x2014) + "]|\\s" + "-" * 2 + "\\s")
    for path in (SETUP / "smoke.py", SETUP / "tests" / "test_smoke.py"):
        text = path.read_text(encoding="utf-8")
        assert not banned.search(text), path
        assert not dashes.search(text), path
