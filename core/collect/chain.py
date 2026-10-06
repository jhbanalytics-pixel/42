"""The morning job chain: collect, understand, detect, brief.

Each job calls begin() first, finish() at the end, then start_next(), which starts the next Cloud Run
job through the Admin API as the job's own identity with a RUN_DATE override. begin() refuses to run
unless the upstream stage's latest runs row for the run date is ok. understand is in the chain only
when the chain jobs are deployed with CHAIN_UNDERSTAND=1; otherwise detect follows collect. Nothing
probes the jobs, since job identities lack run.jobs.get. The side stage reconcile uses begin() and
finish() for its runs rows and duplicate guard only: no upstream, no next job. The watchdog is not a
stage: it runs every 15 minutes and writes its own runs rows. begin() also raises AlreadyDone when the
stage already ran ok that day or is still running inside its task timeout, so a Scheduler retry or the
06:15 brief never repeats work (FORCE_RERUN=1 bypasses this); jobs treat AlreadyDone as a clean exit 0, after
restart_next() starts the next stage when this one is ok and the next has no runs row for the day (a start_next
that raised after finish(ok)).
The running row records the Cloud Run execution name in counts, so Cloud Run's own task retry after a
crash, which shares that name, is let through as a fresh run.
The runs table is append-only: every call adds a row and the latest row per run_id wins. Nothing
here updates, merges, replaces or removes a row.
"""
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

PROJECT = "ogilvy-trends-v2"
REGION = "us-central1"
DATASET = "intelligence_42_agent"
STAGES = ("collect", "understand", "detect", "brief")
JOBS = {"collect": "f42-collect", "understand": "f42-understand", "detect": "f42-detect", "brief": "f42-brief"}
SIDE = ("reconcile",)  # begin() and finish() only: never an upstream, never a next job
# South Africa keeps UTC+2 all year, so a fixed offset needs no tz database in the container.
SAST = timezone(timedelta(hours=2), "SAST")
DEADLINE = time(6, 15)
# Each job's explicit task timeout: a running row older than this is a dead run, not a live one.
TIMEOUTS = {"collect": timedelta(hours=3), "understand": timedelta(hours=2),
            "detect": timedelta(hours=1), "brief": timedelta(hours=1),
            "reconcile": timedelta(minutes=15)}
SKIPPED = "skipped_duplicate"
API = "https://run.googleapis.com/v2"


class UpstreamNotReady(Exception):
    """The upstream stage has no ok row for the run date. .run is this stage's run, already marked blocked,
    so the brief job can still finish it and publish whatever passed once past_deadline() is true."""

    def __init__(self, message, run):
        super().__init__(message)
        self.run = run


class AlreadyDone(Exception):
    """The stage already ran ok for the run date, or a run of it is still live. .run is the refused
    attempt, already recorded as skipped_duplicate. Jobs exit 0 on it."""

    def __init__(self, message, run):
        super().__init__(message)
        self.run = run


@dataclass
class Run:
    run_id: str
    stage: str
    run_date: date
    started_at: str
    runs: object = field(repr=False, default=None)


def today(now=None):
    """RUN_DATE from the environment if set, else today in Africa/Johannesburg."""
    if os.environ.get("RUN_DATE"):
        return date.fromisoformat(os.environ["RUN_DATE"])
    return (now or datetime.now(timezone.utc)).astimezone(SAST).date()


def past_deadline(now, run_date=None):
    """True from 06:15 SAST on the run date: the brief then publishes whatever has passed the gate."""
    day = run_date or today(now)
    return now.astimezone(SAST) >= datetime.combine(day, DEADLINE, SAST)


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _stage(stage):
    if stage not in STAGES and stage not in SIDE:
        raise ValueError(f"unknown chain stage {stage!r}; expected one of {', '.join(STAGES + SIDE)}")


def in_chain(stage):
    """True when stage runs in the morning chain: understand only with CHAIN_UNDERSTAND=1."""
    return stage in STAGES and (stage != "understand" or os.environ.get("CHAIN_UNDERSTAND") == "1")


def upstream(stage):
    """The stage whose ok row this stage needs, or None for collect and the side stages."""
    _stage(stage)
    if stage in SIDE:
        return None
    before = [s for s in STAGES[:STAGES.index(stage)] if in_chain(s)]
    return before[-1] if before else None


def begin(stage, run_date=None, *, runs=None, jobs=None):
    """Start a run of stage. Raises AlreadyDone, after appending a skipped_duplicate row, when the stage
    is already done or live for the day. Otherwise appends a running row, then checks the upstream and
    raises UpstreamNotReady, after appending a blocked row for the same run_id, unless it is ok.
    jobs is accepted for the callers that pass it; begin() makes no Cloud Run call."""
    _stage(stage)
    day = run_date or today()
    runs = runs or default_runs()
    run = Run(f"{stage}-{day:%Y%m%d}-{uuid.uuid4().hex[:12]}", stage, day, _utc_now(), runs)
    execution = os.environ.get("CLOUD_RUN_EXECUTION") or None
    duplicate = None if os.environ.get("FORCE_RERUN") == "1" else _duplicate(stage, day, runs, execution)
    if duplicate:
        runs.append(_row(run, SKIPPED, _utc_now(), None, duplicate))
        raise AlreadyDone(duplicate, run)
    runs.append(_row(run, "running", None, {"execution": execution} if execution else None, None))
    need = upstream(stage)
    if need is None:
        return run
    latest = runs.latest(need, day)
    if latest is not None and latest["status"] == "ok":
        return run
    seen = "no runs row" if latest is None else f"latest status {latest['status']}"
    reason = f"upstream {need} for {day.isoformat()} is not ok ({seen})"
    runs.append(_row(run, "blocked", _utc_now(), None, reason))
    raise UpstreamNotReady(reason, run)


def _duplicate(stage, day, runs, execution):
    """Why a new run of stage for day would repeat work, or None. A live run from this same Cloud Run
    execution is a crashed attempt being retried by Cloud Run, not a duplicate."""
    latest = runs.latest(stage, day)
    if latest is None:
        return None
    if latest["status"] == "ok":
        return f"{stage} for {day.isoformat()} already ran ok (run {latest['run_id']})"
    if latest["status"] == "running":
        if execution and (latest.get("counts") or {}).get("execution") == execution:
            return None
        started = datetime.fromisoformat(str(latest["started_at"]))
        if datetime.now(timezone.utc) - started < TIMEOUTS[stage]:
            return f"{stage} for {day.isoformat()} is still running (run {latest['run_id']}, started {started.isoformat()})"
    return None


def finish(run, status, counts, error=None, *, runs=None):
    """Append the final row for run: same run_id and started_at, with finished_at, counts and error."""
    (runs or run.runs or default_runs()).append(_row(run, status, _utc_now(), counts, error))


def start_next(stage, run_date=None, *, jobs=None):
    """Start the job after stage with RUN_DATE set. Returns the job name, or None after brief and for
    the side stages, which start nothing."""
    _stage(stage)
    if stage in SIDE:
        return None
    day = run_date or today()
    nxt = next_stage(stage)
    if nxt is None:
        return None
    (jobs or CloudRunJobs()).run(JOBS[nxt], {"RUN_DATE": day.isoformat()})
    return JOBS[nxt]


def next_stage(stage):
    """The chain stage start_next starts after stage, or None after brief and for the side stages."""
    _stage(stage)
    if stage in SIDE:
        return None
    return next((s for s in STAGES[STAGES.index(stage) + 1:] if in_chain(s)), None)


def restart_next(stage, run_date=None, *, runs=None, jobs=None):
    """After AlreadyDone: start the next stage when stage's latest row for the day is ok and the next stage has no
    runs row for it, so a start_next that failed after finish(ok) is not lost to a rerun. Returns the job name,
    or None when nothing needed starting. Raises what start_next raises. The next stage's own begin() still
    checks that stage is ok for the day."""
    nxt = next_stage(stage)
    if nxt is None:
        return None
    day = run_date or today()
    runs = runs or default_runs()
    latest = runs.latest(stage, day)
    if latest is None or latest["status"] != "ok" or runs.latest(nxt, day) is not None:
        return None
    return start_next(stage, day, jobs=jobs)


def _row(run, status, finished_at, counts, error):
    return {"run_id": run.run_id, "stage": run.stage, "run_date": run.run_date.isoformat(), "status": status,
            "started_at": run.started_at, "finished_at": finished_at, "counts": counts, "error": error}


def _order(row, index):
    at = row.get("finished_at") or row.get("started_at") or ""
    return (str(at), row.get("finished_at") is not None, index)


class MemoryRunsStore:
    def __init__(self):
        self.rows = []

    def append(self, row):
        self.rows.append(dict(row))

    def latest(self, stage, day):
        """Newest row of stage for day, ignoring skipped_duplicate rows, which record no work."""
        hits = [(r, i) for i, r in enumerate(self.rows)
                if r["stage"] == stage and r["run_date"] == day.isoformat() and r["status"] != SKIPPED]
        return max(hits, key=lambda h: _order(*h))[0] if hits else None


class BigQueryRunsStore:
    def __init__(self, client, project=PROJECT, dataset=DATASET):
        self._client = client
        self._table = f"{project}.{dataset}.runs"

    def append(self, row):
        row = dict(row)
        if row.get("counts") is not None:
            row["counts"] = json.dumps(row["counts"])
        errors = self._client.insert_rows_json(self._table, [row])
        if errors:
            raise RuntimeError(f"append to {self._table} failed: {errors}")

    def latest(self, stage, day):
        from google.cloud import bigquery

        sql = (f"SELECT run_id, status, started_at, counts, error FROM `{self._table}` "
               "WHERE stage = @stage AND run_date = @day AND status != @skip "
               "ORDER BY COALESCE(finished_at, started_at) DESC, finished_at IS NOT NULL DESC LIMIT 1")
        config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("stage", "STRING", stage),
            bigquery.ScalarQueryParameter("day", "DATE", day),
            bigquery.ScalarQueryParameter("skip", "STRING", SKIPPED)])
        rows = list(self._client.query(sql, job_config=config).result())
        if not rows:
            return None
        row = dict(rows[0].items())
        if isinstance(row.get("counts"), str):
            row["counts"] = json.loads(row["counts"])
        return row


def default_runs():
    from google.cloud import bigquery

    return BigQueryRunsStore(bigquery.Client(project=PROJECT))


class CloudRunJobs:
    """Cloud Run Admin API v2, authenticated with google.auth.default(): in a job, the job's own identity."""

    def __init__(self, project=PROJECT, region=REGION, session=None):
        self.base = f"{API}/projects/{project}/locations/{region}/jobs/"
        self._session = session

    def session(self):
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self._session = AuthorizedSession(creds)
        return self._session

    def run(self, job, env):
        body = {"overrides": {"containerOverrides": [{"env": [{"name": k, "value": v} for k, v in env.items()]}]}}
        resp = self.session().post(self.base + job + ":run", json=body, timeout=60)
        if resp.status_code in (403, 404):
            raise RuntimeError(f"could not start {job}: POST {job}:run returned {resp.status_code}")
        resp.raise_for_status()
        return resp.json()
