"""The smoke job per identity (task 0.4, SETUP.md "The bootstrap script"): each role does its real work in miniature.

Deployed by core/setup/deploy_jobs.py --smoke as f42-smoke-<role>, one per runtime identity except f42-scheduler,
and run with gcloud run jobs execute (deploy_jobs.py --run-smoke prints the commands). The builder role runs from
the PC first, once the smoke jobs are deployed, as f42-builder through gcloud:

    py -3.13 core/setup/smoke.py --role builder

It writes the bucket smoke object, creates the paused smoke Scheduler job if missing, and proves f42-scheduler:
it runs that Scheduler job now, whose OAuth token is minted as f42-scheduler, and waits for the new execution
of the noop job. f42-scheduler never calls Scheduler itself, so it gets no smoke job of its own.

Each check prints one line: its name, PASS, FAIL or SKIPPED, and a short reason, never a value. The job exits 0
only if every check that ran passed. With SMOKE_NOOP=1 in the environment it exits 0 at once: that is the
override the start_noop check and the smoke Scheduler job send to the noop job. The scratch tables only ever get a
CREATE TABLE IF NOT EXISTS, a MERGE that inserts or an INSERT; no model call spends more than 16 output tokens,
and nothing here calls SocialCrawl: the secret check reads only whether the mounted key is non-empty. The model
call is Gemini on Vertex as the job identity with no API key; MODEL_PROVIDER may be unset or gemini.
"""
import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.collect import chain  # noqa: E402
from core.setup import schedule  # noqa: E402

PROJECT = chain.PROJECT
REGION = chain.REGION
CORE = "intelligence_42_core"
AGENT = "intelligence_42_agent"
SECRET = "SOCIALCRAWL_OGILVY_API_KEY"
BUCKET = "ogilvy-trends-v2-f42-media-staging"
OBJECT = "smoke/smoke.txt"
OBJECT_URL = f"gs://{BUCKET}/{OBJECT}"
# The noop job is the web smoke job: it never starts a job itself, so a broken noop mode cannot loop.
NOOP_JOB = "f42-smoke-web"
NOOP_ENV = {"SMOKE_NOOP": "1"}
SCHEDULE_JOB = "f42-smoke-noop"
# Once a year at most, and kept paused: the builder resumes it only around its own run-now.
SCHEDULE = "0 0 1 1 *"
POLL_SECONDS = 10
POLL_LIMIT_SECONDS = 120
GEMINI_MODEL = "gemini-3.8-flash"
MODEL_REGION = "global"
MAX_TOKENS = 16
# The remote embedding model lane L3 creates for task 1.8 on gemini-embedding-001 through connection f42-vertex.
EMBED_MODEL = f"{PROJECT}.{CORE}.embed_gemini"

JOB_ROLES = ("collector", "enricher", "agent", "brief", "web")
CHECKS = {
    "collector": ("merge", "secret", "start_noop"),
    "enricher": ("merge", "secret", "bucket_read", "model_call", "embedding"),
    "agent": ("select_core", "append_agent", "model_call", "secret"),
    "brief": ("merge", "secret", "model_call", "start_noop"),
    "web": ("select_core", "bucket_read"),
    "builder": ("bucket_write", "scheduler_create", "scheduler_dispatch"),
}


class Skip(Exception):
    pass


def scratch_table(dataset):
    return f"{PROJECT}.{dataset}.smoke_checks"


def create_sql(dataset):
    return (f"CREATE TABLE IF NOT EXISTS `{scratch_table(dataset)}` "
            "(role STRING NOT NULL, execution STRING NOT NULL, checked_at TIMESTAMP NOT NULL)")


def check_merge(ctx):
    table = scratch_table(CORE)
    ctx.cloud.dml(create_sql(CORE))
    n = ctx.cloud.dml(
        f"MERGE `{table}` T USING (SELECT @role AS role, @execution AS execution) S "
        "ON T.role = S.role AND T.execution = S.execution "
        "WHEN NOT MATCHED THEN INSERT (role, execution, checked_at) VALUES (S.role, S.execution, CURRENT_TIMESTAMP())",
        ctx.params())
    if n != 1:
        raise AssertionError(f"MERGE into {table} affected {n} rows, expected 1")
    return f"one row merged into {table}"


def check_append_agent(ctx):
    table = scratch_table(AGENT)
    ctx.cloud.dml(create_sql(AGENT))
    n = ctx.cloud.dml(f"INSERT INTO `{table}` (role, execution, checked_at) "
                      "VALUES (@role, @execution, CURRENT_TIMESTAMP())", ctx.params())
    if n != 1:
        raise AssertionError(f"INSERT into {table} affected {n} rows, expected 1")
    return f"one row appended to {table}"


def check_select_core(ctx):
    table = f"{PROJECT}.{CORE}.posts"
    rows = ctx.cloud.query(f"SELECT COUNT(*) AS n FROM `{table}`")
    if len(rows) != 1:
        raise AssertionError(f"SELECT on {table} returned {len(rows)} rows, expected 1")
    return f"SELECT on {table} answered"


def check_secret(ctx):
    # Only whether the mounted key is non-empty; neither the value nor its length is ever printed.
    if not ctx.env.get(SECRET):
        raise AssertionError(f"{SECRET} is not mounted or is empty")
    return f"{SECRET} mounted and non-empty"


def check_start_noop(ctx):
    ctx.cloud.start_job(NOOP_JOB, NOOP_ENV)
    return f"started {NOOP_JOB} with SMOKE_NOOP=1"


def check_bucket_read(ctx):
    data = ctx.cloud.read_object(BUCKET, OBJECT)
    if not data:
        raise AssertionError(f"{OBJECT_URL} is empty")
    return f"read {OBJECT_URL}"


def check_model_call(ctx):
    provider = ctx.env.get("MODEL_PROVIDER") or "gemini"
    if provider != "gemini":
        raise AssertionError(f"MODEL_PROVIDER is {provider}, expected gemini")
    model = GEMINI_MODEL
    tokens = ctx.cloud.complete_gemini(model, "Reply with the single word ok.", MAX_TOKENS)
    if not tokens:
        raise AssertionError(f"{model} returned no output tokens")
    return f"{model} on Vertex replied"


def check_embedding(ctx):
    if not ctx.cloud.model_exists(EMBED_MODEL):
        raise Skip(f"remote model {EMBED_MODEL} does not exist yet (task 1.8 creates it)")
    rows = ctx.cloud.query(
        "SELECT ARRAY_LENGTH(ml_generate_embedding_result) AS dims, ml_generate_embedding_status AS status "
        f"FROM ML.GENERATE_EMBEDDING(MODEL `{EMBED_MODEL}`, (SELECT 'smoke' AS content), "
        "STRUCT(TRUE AS flatten_json_output))")
    if len(rows) != 1:
        raise AssertionError(f"ML.GENERATE_EMBEDDING returned {len(rows)} rows, expected 1")
    if rows[0].get("status"):
        raise AssertionError(f"ML.GENERATE_EMBEDDING status: {rows[0]['status']}")
    if not rows[0].get("dims"):
        raise AssertionError("ML.GENERATE_EMBEDDING returned an empty vector")
    return "one row embedded"


def check_bucket_write(ctx):
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "smoke.txt"
        source.write_text("42 smoke object, written by the builder role of core/setup/smoke.py\n", encoding="utf-8")
        ctx.gcloud.run(["storage", "cp", str(source), OBJECT_URL, "--no-clobber", f"--project={PROJECT}"])
    if OBJECT_URL not in ctx.gcloud.run(["storage", "ls", OBJECT_URL, f"--project={PROJECT}"]):
        raise AssertionError(f"{OBJECT_URL} is not listed after the copy")
    return f"{OBJECT_URL} present (written if missing, never overwritten)"


def schedule_create_argv():
    body = {"overrides": {"containerOverrides": [{"env": [{"name": k, "value": v} for k, v in NOOP_ENV.items()]}]}}
    return ["scheduler", "jobs", "create", "http", SCHEDULE_JOB, *schedule.where(),
            f"--schedule={SCHEDULE}", f"--time-zone={schedule.TIME_ZONE}",
            f"--uri={chain.API}/projects/{PROJECT}/locations/{REGION}/jobs/{NOOP_JOB}:run", "--http-method=POST",
            f"--oauth-service-account-email={schedule.SERVICE_ACCOUNT}", f"--oauth-token-scope={schedule.SCOPE}",
            "--headers=Content-Type=application/json", f"--message-body={json.dumps(body)}",
            f"--attempt-deadline={schedule.ATTEMPT_DEADLINE}", "--max-retry-attempts=0",
            f"--description=42 smoke: starts {NOOP_JOB} with SMOKE_NOOP=1, run-now only, kept paused"]


def schedule_argv(verb, *extra):
    return ["scheduler", "jobs", verb, SCHEDULE_JOB, *schedule.where(), *extra]


def ensure_paused(g, cause=None):
    """Pause the smoke Scheduler job and read PAUSED back; if that cannot be confirmed, fail naming ENABLED and the
    exact pause command. cause is the error that led here, reported after the command."""
    try:
        g.run(schedule_argv("pause"))
        state = g.run(schedule_argv("describe", "--format=value(state)")).strip()
    except Exception as e:
        state, cause = None, cause or e
    if state != "PAUSED":
        after = f" after {short(cause)}" if cause else ""
        raise AssertionError(f"{SCHEDULE_JOB} may be ENABLED, pause it: gcloud {' '.join(schedule_argv('pause'))}"
                             + after)


def check_scheduler_create(ctx):
    g = ctx.gcloud
    if SCHEDULE_JOB in schedule.existing(g):
        cause = None
        try:
            if g.run(schedule_argv("describe", "--format=value(state)")).strip() == "PAUSED":
                return f"{SCHEDULE_JOB} already exists and is paused"
        except Exception as e:
            cause = e
        ensure_paused(g, cause)
        return f"{SCHEDULE_JOB} already exists, paused now"
    try:
        g.run(schedule_create_argv())
    except Exception as e:
        # The server may have made the job before the client saw the error, so it is paused all the same.
        ensure_paused(g, e)
        raise AssertionError(f"{SCHEDULE_JOB} confirmed PAUSED after {short(e)}") from None
    ensure_paused(g)
    return f"{SCHEDULE_JOB} created and paused"


def executions_argv():
    # The full list, never a --limit: a new execution is found as a name missing from the list taken before.
    return ["run", "jobs", "executions", "list", f"--job={NOOP_JOB}", f"--project={PROJECT}", f"--region={REGION}",
            "--format=value(metadata.name)"]


def noop_executions(g):
    return {line.strip() for line in g.run(executions_argv()).splitlines() if line.strip()}


def check_scheduler_dispatch(ctx):
    g = ctx.gcloud
    before = noop_executions(g)
    # Resume, run now, pause again, so the dispatch does not rest on how run-now treats a paused job. The resume
    # sits inside the try, so one the server applied while the client saw an error is paused again too.
    try:
        g.run(schedule_argv("resume"))
        g.run(schedule_argv("run"))
    except Exception as e:
        ensure_paused(g, e)
        raise AssertionError(f"{SCHEDULE_JOB} confirmed PAUSED after {short(e)}") from None
    ensure_paused(g)
    waited = 0
    while waited < POLL_LIMIT_SECONDS:
        ctx.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
        new = noop_executions(g) - before
        if new:
            return f"f42-scheduler token started {NOOP_JOB}: new execution {', '.join(sorted(new))}"
    raise AssertionError(f"no new {NOOP_JOB} execution within {POLL_LIMIT_SECONDS} s of the run-now")


RUNNERS = {
    "merge": check_merge, "append_agent": check_append_agent, "select_core": check_select_core,
    "secret": check_secret, "start_noop": check_start_noop, "bucket_read": check_bucket_read,
    "model_call": check_model_call, "embedding": check_embedding, "bucket_write": check_bucket_write,
    "scheduler_create": check_scheduler_create, "scheduler_dispatch": check_scheduler_dispatch,
}


class Context:
    def __init__(self, role, cloud, gcloud, env, sleep):
        self.role, self.cloud, self.gcloud, self.env, self.sleep = role, cloud, gcloud, env, sleep
        self.execution = env.get("CLOUD_RUN_EXECUTION") or f"local-{uuid.uuid4().hex[:12]}"

    def params(self):
        return {"role": self.role, "execution": self.execution}


def short(exc):
    lines = str(exc).strip().splitlines()
    text = lines[-1] if lines else ""
    return f"{type(exc).__name__}: {text[:200]}" if text else type(exc).__name__


def main(argv=None, cloud=None, gcloud=None, env=None, sleep=time.sleep):
    env = os.environ if env is None else env
    if env.get("SMOKE_NOOP") == "1":
        print("SMOKE_NOOP=1: noop run, exiting 0 at once")
        return 0
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--role", required=True, choices=list(CHECKS))
    role = parser.parse_args(argv).role
    if role != "builder" and not env.get("CLOUD_RUN_JOB"):
        print(f"Refusing: role {role} proves f42-{role} only inside its Cloud Run job f42-smoke-{role}; "
              "run it with gcloud run jobs execute (deploy_jobs.py --run-smoke prints the command).")
        return 2

    ctx = Context(role, cloud or Cloud(), gcloud or schedule.Gcloud(), env, sleep)
    print(f"Smoke {role} as f42-{role}, execution {ctx.execution}")
    counts = {"PASS": 0, "FAIL": 0, "SKIPPED": 0}
    for name in CHECKS[role]:
        try:
            status, reason = "PASS", RUNNERS[name](ctx)
        except Skip as e:
            status, reason = "SKIPPED", str(e)
        except Exception as e:  # every failure is one FAIL line; the remaining checks still run
            status, reason = "FAIL", short(e)
        counts[status] += 1
        print(f"{name:<16} {status:<7} {reason}")
    print(f"{role}: {counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIPPED']} skipped")
    return 1 if counts["FAIL"] else 0


class Cloud:
    """The job identity's own clients: BigQuery, the Storage and Cloud Run APIs, Gemini on Vertex."""

    def __init__(self, session=None):
        self._session = session
        self._bq = None

    def session(self):
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self._session = AuthorizedSession(creds)
        return self._session

    def bq(self):
        if self._bq is None:
            from google.cloud import bigquery

            self._bq = bigquery.Client(project=PROJECT)
        return self._bq

    def _job(self, sql, params):
        from google.cloud import bigquery

        config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter(k, "STRING", v) for k, v in (params or {}).items()])
        job = self.bq().query(sql, job_config=config)
        return job, job.result()

    def dml(self, sql, params=None):
        job, _ = self._job(sql, params)
        return job.num_dml_affected_rows

    def query(self, sql, params=None):
        _, rows = self._job(sql, params)
        return [dict(r.items()) for r in rows]

    def model_exists(self, ref):
        from google.api_core.exceptions import NotFound

        try:
            self.bq().get_model(ref)
            return True
        except NotFound:
            return False

    def read_object(self, bucket, name):
        url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(name, safe='')}?alt=media"
        resp = self.session().get(url, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"GET gs://{bucket}/{name} returned {resp.status_code}")
        return resp.content

    def start_job(self, job, env):
        chain.CloudRunJobs(session=self.session()).run(job, env)

    def complete_gemini(self, model, prompt, max_tokens):
        from google import genai
        from google.genai import types

        client = genai.Client(vertexai=True, project=PROJECT, location=MODEL_REGION)
        resp = client.models.generate_content(model=model, contents=prompt,
                                              config=types.GenerateContentConfig(max_output_tokens=max_tokens))
        # On Gemini 3 the limit caps the reply and its thinking together, and both are billed as output.
        usage = resp.usage_metadata
        return (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)


if __name__ == "__main__":
    sys.exit(main())
