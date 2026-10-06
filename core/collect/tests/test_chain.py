"""Unit tests for core/collect/chain.py. No cloud access: a memory runs store and a fake HTTP session."""
from datetime import date, datetime, timedelta, timezone

import pytest

from core.collect import chain

DAY = date(2026, 9, 28)
URL = "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/jobs/"
SAST = timezone(timedelta(hours=2))


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, deployed=("f42-collect", "f42-understand", "f42-detect", "f42-brief"), get_status=None,
                 post_status=None):
        self.deployed = set(deployed)
        self.get_status = get_status or {}
        self.post_status = post_status or {}
        self.gets = []
        self.posts = []

    def get(self, url, **kw):
        self.gets.append(url)
        job = url.rsplit("/", 1)[1]
        return FakeResponse(self.get_status.get(job, 200 if job in self.deployed else 404))

    def post(self, url, json=None, **kw):
        self.posts.append((url, json))
        job = url.rsplit("/", 1)[1].split(":")[0]
        return FakeResponse(self.post_status.get(job, 200), {"name": "operations/fake"})


def jobs(**kw):
    return chain.CloudRunJobs(session=FakeSession(**kw))


def seed(runs, stage, *statuses, day=DAY):
    """Append a run of stage for day whose rows carry these statuses in order."""
    start = datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc)
    run_id = f"{stage}-seed-{len(runs.rows)}"
    first = (start + timedelta(minutes=len(runs.rows))).isoformat()
    for i, status in enumerate(statuses):
        at = (start + timedelta(minutes=len(runs.rows) + i)).isoformat()
        runs.append({"run_id": run_id, "stage": stage, "run_date": day.isoformat(),
                     "status": status, "started_at": first, "finished_at": None if status == "running" else at,
                     "counts": None, "error": None})


def seed_running(runs, stage, ago, day=DAY, execution=None):
    """A run of stage for day that started ago before now and has not finished."""
    at = (datetime.now(timezone.utc) - ago).isoformat()
    counts = {"execution": execution} if execution else None
    runs.append({"run_id": f"{stage}-live-{len(runs.rows)}", "stage": stage, "run_date": day.isoformat(),
                 "status": "running", "started_at": at, "finished_at": None, "counts": counts, "error": None})


@pytest.fixture(autouse=True)
def no_run_date(monkeypatch):
    monkeypatch.delenv("RUN_DATE", raising=False)
    monkeypatch.delenv("FORCE_RERUN", raising=False)
    monkeypatch.delenv("CLOUD_RUN_EXECUTION", raising=False)
    monkeypatch.delenv("CHAIN_UNDERSTAND", raising=False)


@pytest.fixture
def understand_on(monkeypatch):
    """The chain jobs deployed with CHAIN_UNDERSTAND=1: understand sits between collect and detect."""
    monkeypatch.setenv("CHAIN_UNDERSTAND", "1")


# begin


def test_collect_has_no_upstream_and_appends_a_running_row():
    runs = chain.MemoryRunsStore()
    run = chain.begin("collect", DAY, runs=runs, jobs=jobs())
    assert len(runs.rows) == 1
    row = runs.rows[0]
    assert row["run_id"] == run.run_id and row["stage"] == "collect" and row["run_date"] == "2026-09-28"
    assert row["status"] == "running" and row["started_at"] and row["finished_at"] is None
    assert row["counts"] is None


def test_the_running_row_records_the_cloud_run_execution(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "f42-collect-abc12")
    runs = chain.MemoryRunsStore()
    chain.begin("collect", DAY, runs=runs, jobs=jobs())
    assert runs.rows[0]["status"] == "running" and runs.rows[0]["counts"] == {"execution": "f42-collect-abc12"}


@pytest.mark.parametrize("statuses", [(), ("running",), ("running", "failed"), ("running", "blocked")])
def test_begin_refuses_when_upstream_missing_running_or_failed(statuses, understand_on):
    runs = chain.MemoryRunsStore()
    if statuses:
        seed(runs, "understand", *statuses)
    before = len(runs.rows)
    with pytest.raises(chain.UpstreamNotReady) as err:
        chain.begin("detect", DAY, runs=runs, jobs=jobs())
    assert "understand" in str(err.value)
    mine = runs.rows[before:]
    assert [r["status"] for r in mine] == ["running", "blocked"]
    assert mine[0]["run_id"] == mine[1]["run_id"] == err.value.run.run_id
    assert mine[1]["finished_at"] and "understand" in mine[1]["error"]


def test_begin_passes_when_upstream_latest_row_is_ok(understand_on):
    runs = chain.MemoryRunsStore()
    seed(runs, "understand", "running", "ok")
    run = chain.begin("detect", DAY, runs=runs, jobs=jobs())
    assert runs.rows[-1]["run_id"] == run.run_id and runs.rows[-1]["status"] == "running"


def test_begin_uses_the_latest_upstream_row_not_any_ok():
    runs = chain.MemoryRunsStore()
    seed(runs, "detect", "running", "ok")
    seed(runs, "detect", "running", "failed")
    with pytest.raises(chain.UpstreamNotReady):
        chain.begin("brief", DAY, runs=runs, jobs=jobs())


def test_a_retry_that_succeeds_after_a_failure_counts_as_ok():
    runs = chain.MemoryRunsStore()
    seed(runs, "detect", "running", "failed")
    seed(runs, "detect", "running", "ok")
    chain.begin("brief", DAY, runs=runs, jobs=jobs())


def test_upstream_ok_on_another_date_does_not_count():
    runs = chain.MemoryRunsStore()
    seed(runs, "detect", "running", "ok", day=DAY - timedelta(days=1))
    with pytest.raises(chain.UpstreamNotReady):
        chain.begin("brief", DAY, runs=runs, jobs=jobs())


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes"])
def test_without_chain_understand_1_collect_is_the_upstream_of_detect(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("CHAIN_UNDERSTAND", value)
    fake = FakeSession()
    runs = chain.MemoryRunsStore()
    seed(runs, "collect", "running", "ok")
    run = chain.begin("detect", DAY, runs=runs, jobs=chain.CloudRunJobs(session=fake))
    assert runs.rows[-1]["run_id"] == run.run_id and runs.rows[-1]["status"] == "running"
    assert fake.gets == [] and fake.posts == []


def test_with_chain_understand_1_detect_waits_on_understand(understand_on):
    fake = FakeSession()
    runs = chain.MemoryRunsStore()
    seed(runs, "collect", "running", "ok")
    with pytest.raises(chain.UpstreamNotReady) as err:
        chain.begin("detect", DAY, runs=runs, jobs=chain.CloudRunJobs(session=fake))
    assert "understand" in str(err.value)
    seed(runs, "understand", "running", "ok")
    chain.begin("detect", DAY, runs=runs, jobs=chain.CloudRunJobs(session=fake))
    assert fake.gets == [] and fake.posts == []


@pytest.mark.parametrize("on", [False, True])
def test_understand_itself_waits_on_collect(monkeypatch, on):
    if on:
        monkeypatch.setenv("CHAIN_UNDERSTAND", "1")
    runs = chain.MemoryRunsStore()
    with pytest.raises(chain.UpstreamNotReady) as err:
        chain.begin("understand", DAY, runs=runs, jobs=jobs())
    assert "collect" in str(err.value)


def test_unknown_stage_is_refused():
    with pytest.raises(ValueError):
        chain.begin("publish", DAY, runs=chain.MemoryRunsStore(), jobs=jobs())


def test_the_watchdog_is_not_a_stage_since_it_runs_every_15_minutes_and_writes_its_own_rows():
    runs = chain.MemoryRunsStore()
    with pytest.raises(ValueError):
        chain.begin("watchdog", DAY, runs=runs, jobs=jobs())
    assert runs.rows == [] and "watchdog" not in chain.TIMEOUTS


# finish


def test_finish_appends_a_final_row_and_never_changes_earlier_rows():
    runs = chain.MemoryRunsStore()
    run = chain.begin("collect", DAY, runs=runs, jobs=jobs())
    first = dict(runs.rows[0])
    chain.finish(run, "ok", {"posts": 12, "markets": 3})
    assert runs.rows[0] == first
    assert len(runs.rows) == 2
    last = runs.rows[1]
    assert last["run_id"] == run.run_id and last["stage"] == "collect" and last["run_date"] == "2026-09-28"
    assert last["status"] == "ok" and last["started_at"] == first["started_at"] and last["finished_at"]
    assert last["counts"] == {"posts": 12, "markets": 3} and last["error"] is None


def test_finish_records_the_error_and_the_latest_row_wins():
    runs = chain.MemoryRunsStore()
    seed(runs, "collect", "running", "ok")
    run = chain.begin("detect", DAY, runs=runs, jobs=jobs(deployed=("f42-collect", "f42-detect")))
    chain.finish(run, "failed", {}, error="baseline query failed")
    assert runs.rows[-1]["error"] == "baseline query failed"
    assert runs.latest("detect", DAY)["status"] == "failed"


def test_a_blocked_run_can_still_be_finished_past_the_deadline():
    runs = chain.MemoryRunsStore()
    with pytest.raises(chain.UpstreamNotReady) as err:
        chain.begin("brief", DAY, runs=runs, jobs=jobs())
    chain.finish(err.value.run, "ok", {"published": 1})
    assert runs.latest("brief", DAY)["status"] == "ok"


def test_the_runs_stores_have_no_update_path():
    for store in (chain.MemoryRunsStore, chain.BigQueryRunsStore):
        public = {n for n in dir(store) if not n.startswith("_")}
        assert public <= {"append", "latest"}, public


def test_bigquery_store_appends_with_insert_rows_json_and_json_encodes_counts():
    class FakeClient:
        def __init__(self):
            self.inserts = []
            self.queries = []

        def insert_rows_json(self, table, rows):
            self.inserts.append((table, rows))
            return []

        def query(self, sql, job_config=None):
            self.queries.append((sql, {p.name: p.value for p in job_config.query_parameters}))
            return type("Job", (), {"result": lambda self: []})()

    client = FakeClient()
    store = chain.BigQueryRunsStore(client)
    store.append({"run_id": "r1", "stage": "collect", "run_date": "2026-09-28", "status": "ok",
                  "started_at": "t", "finished_at": "t", "counts": {"posts": 3}, "error": None})
    table, rows = client.inserts[0]
    assert table == "ogilvy-trends-v2.intelligence_42_agent.runs"
    assert rows[0]["counts"] == '{"posts": 3}'
    assert store.latest("collect", DAY) is None
    sql, params = client.queries[0]
    assert params["skip"] == "skipped_duplicate" and "@skip" in sql
    sql = sql.upper()
    for word in ("MERGE", "UPDATE", "INSERT", "REPLACE", "TRUNCATE"):
        assert word not in sql


# duplicate guard


def test_task_timeouts_per_stage():
    assert chain.TIMEOUTS == {"collect": timedelta(hours=3), "understand": timedelta(hours=2),
                              "detect": timedelta(hours=1), "brief": timedelta(hours=1),
                              "reconcile": timedelta(minutes=15)}


def test_already_ok_stage_is_refused_as_already_done():
    runs = chain.MemoryRunsStore()
    seed(runs, "collect", "running", "ok")
    before = len(runs.rows)
    fake = FakeSession()
    with pytest.raises(chain.AlreadyDone) as err:
        chain.begin("collect", DAY, runs=runs, jobs=chain.CloudRunJobs(session=fake))
    mine = runs.rows[before:]
    assert len(mine) == 1 and mine[0]["status"] == "skipped_duplicate"
    assert mine[0]["run_id"] == err.value.run.run_id and mine[0]["finished_at"] and mine[0]["error"]
    assert fake.posts == []


@pytest.mark.parametrize("stage,ago,done", [
    ("collect", timedelta(hours=2, minutes=59), True),
    ("collect", timedelta(hours=3, minutes=1), False),
    ("understand", timedelta(minutes=59), True),
    ("understand", timedelta(minutes=61), True),
    ("understand", timedelta(hours=2), False),
    ("detect", timedelta(minutes=59), True),
    ("brief", timedelta(minutes=61), False),
    ("reconcile", timedelta(minutes=14), True),
    ("reconcile", timedelta(minutes=16), False),
])
def test_a_running_row_blocks_a_second_run_only_within_the_task_timeout(stage, ago, done):
    runs = chain.MemoryRunsStore()
    seed_running(runs, stage, ago)
    if done:
        with pytest.raises(chain.AlreadyDone):
            chain.begin(stage, DAY, runs=runs, jobs=jobs(deployed=()))
        assert runs.rows[-1]["status"] == "skipped_duplicate"
    else:
        try:
            chain.begin(stage, DAY, runs=runs, jobs=jobs(deployed=()))
        except chain.UpstreamNotReady:
            pass
        assert "skipped_duplicate" not in [r["status"] for r in runs.rows]


@pytest.mark.parametrize("ago,blocked", [
    (timedelta(hours=2) - timedelta(seconds=1), True),
    (timedelta(hours=2), False),
])
def test_understand_duplicate_guard_uses_the_exact_two_hour_boundary(monkeypatch, ago, blocked):
    fixed_now = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(chain, "datetime", FixedDatetime)
    runs = chain.MemoryRunsStore()
    runs.append({"run_id": "understand-live", "stage": "understand", "run_date": DAY.isoformat(),
                 "status": "running", "started_at": (fixed_now - ago).isoformat(), "finished_at": None,
                 "counts": None, "error": None})

    if blocked:
        with pytest.raises(chain.AlreadyDone):
            chain.begin("understand", DAY, runs=runs, jobs=jobs(deployed=()))
        assert [row["status"] for row in runs.rows] == ["running", "skipped_duplicate"]
    else:
        with pytest.raises(chain.UpstreamNotReady):
            chain.begin("understand", DAY, runs=runs, jobs=jobs(deployed=()))
        assert [row["status"] for row in runs.rows] == ["running", "running", "blocked"]


@pytest.mark.parametrize("last", ["failed", "blocked"])
def test_a_failed_or_blocked_stage_may_run_again(last):
    runs = chain.MemoryRunsStore()
    seed(runs, "collect", "running", last)
    chain.begin("collect", DAY, runs=runs, jobs=jobs())
    assert runs.rows[-1]["status"] == "running"


def test_an_ok_run_on_another_date_is_not_a_duplicate():
    runs = chain.MemoryRunsStore()
    seed(runs, "collect", "running", "ok", day=DAY - timedelta(days=1))
    chain.begin("collect", DAY, runs=runs, jobs=jobs())


def test_force_rerun_bypasses_the_guard(monkeypatch):
    monkeypatch.setenv("FORCE_RERUN", "1")
    runs = chain.MemoryRunsStore()
    seed(runs, "collect", "running", "ok")
    run = chain.begin("collect", DAY, runs=runs, jobs=jobs())
    assert runs.rows[-1]["run_id"] == run.run_id and runs.rows[-1]["status"] == "running"


def test_repeated_retries_stay_duplicates_while_the_first_run_is_live():
    runs = chain.MemoryRunsStore()
    seed_running(runs, "collect", timedelta(minutes=5))
    for _ in range(3):
        with pytest.raises(chain.AlreadyDone):
            chain.begin("collect", DAY, runs=runs, jobs=jobs())
    assert [r["status"] for r in runs.rows] == ["running"] + ["skipped_duplicate"] * 3


def test_a_skipped_duplicate_does_not_hide_an_ok_upstream():
    runs = chain.MemoryRunsStore()
    seed(runs, "collect", "running", "ok")
    with pytest.raises(chain.AlreadyDone):
        chain.begin("collect", DAY, runs=runs, jobs=jobs())
    run = chain.begin("detect", DAY, runs=runs, jobs=jobs(deployed=("f42-collect", "f42-detect")))
    assert runs.rows[-1]["run_id"] == run.run_id and runs.rows[-1]["status"] == "running"
    assert runs.latest("collect", DAY)["status"] == "ok"


def test_a_task_retry_of_the_same_execution_passes_with_a_fresh_run(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "f42-collect-abc12")
    runs = chain.MemoryRunsStore()
    seed_running(runs, "collect", timedelta(minutes=10), execution="f42-collect-abc12")
    crashed = runs.rows[0]["run_id"]
    run = chain.begin("collect", DAY, runs=runs, jobs=jobs())
    assert run.run_id != crashed
    assert runs.rows[-1]["run_id"] == run.run_id and runs.rows[-1]["status"] == "running"
    assert runs.rows[-1]["counts"] == {"execution": "f42-collect-abc12"}
    assert "skipped_duplicate" not in [r["status"] for r in runs.rows]


def test_a_different_execution_inside_the_timeout_is_already_done(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "f42-collect-xyz99")
    runs = chain.MemoryRunsStore()
    seed_running(runs, "collect", timedelta(minutes=10), execution="f42-collect-abc12")
    with pytest.raises(chain.AlreadyDone):
        chain.begin("collect", DAY, runs=runs, jobs=jobs())
    assert runs.rows[-1]["status"] == "skipped_duplicate"


@pytest.mark.parametrize("mine,theirs", [(None, None), (None, "f42-collect-abc12"), ("f42-collect-abc12", None)])
def test_without_a_shared_execution_name_the_guard_is_unchanged(monkeypatch, mine, theirs):
    if mine:
        monkeypatch.setenv("CLOUD_RUN_EXECUTION", mine)
    runs = chain.MemoryRunsStore()
    seed_running(runs, "collect", timedelta(minutes=10), execution=theirs)
    with pytest.raises(chain.AlreadyDone):
        chain.begin("collect", DAY, runs=runs, jobs=jobs())


def test_the_same_execution_does_not_rerun_a_stage_already_ok(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "f42-collect-abc12")
    runs = chain.MemoryRunsStore()
    seed(runs, "collect", "running", "ok")
    with pytest.raises(chain.AlreadyDone):
        chain.begin("collect", DAY, runs=runs, jobs=jobs())


def test_bigquery_store_reads_the_execution_from_json_counts():
    class Row(dict):
        pass

    class FakeClient:
        def query(self, sql, job_config=None):
            self.sql = sql
            row = Row(run_id="r1", status="running", started_at="2026-09-28T00:00:00+00:00", error=None,
                      counts='{"execution": "f42-collect-abc12"}')
            return type("Job", (), {"result": lambda self: [row]})()

    client = FakeClient()
    latest = chain.BigQueryRunsStore(client).latest("collect", DAY)
    assert "counts" in client.sql and latest["counts"] == {"execution": "f42-collect-abc12"}


def test_brief_at_0615_after_the_chain_published_is_already_done():
    runs = chain.MemoryRunsStore()
    seed(runs, "detect", "running", "ok")
    seed(runs, "brief", "running", "ok")
    with pytest.raises(chain.AlreadyDone):
        chain.begin("brief", DAY, runs=runs, jobs=jobs())


def test_brief_at_0615_after_detect_failed_is_upstream_not_ready_with_a_run_to_finish():
    runs = chain.MemoryRunsStore()
    seed(runs, "detect", "running", "failed")
    with pytest.raises(chain.UpstreamNotReady) as err:
        chain.begin("brief", DAY, runs=runs, jobs=jobs())
    chain.finish(err.value.run, "ok", {"published": 1, "data_issue": 1})
    assert runs.latest("brief", DAY)["status"] == "ok"


# side stage: reconcile


@pytest.mark.parametrize("stage", ["reconcile"])
def test_a_side_stage_begins_and_finishes_with_no_upstream(monkeypatch, stage):
    monkeypatch.setenv("CHAIN_UNDERSTAND", "1")
    fake = FakeSession()
    runs = chain.MemoryRunsStore()
    run = chain.begin(stage, DAY, runs=runs, jobs=chain.CloudRunJobs(session=fake))
    assert run.run_id.startswith(f"{stage}-20260928-")
    chain.finish(run, "ok", {"checked": 1})
    assert [(r["stage"], r["status"]) for r in runs.rows] == [(stage, "running"), (stage, "ok")]
    assert runs.rows[0]["run_id"] == runs.rows[1]["run_id"]
    assert fake.gets == [] and fake.posts == []


@pytest.mark.parametrize("stage", ["reconcile"])
def test_a_side_stage_never_reads_an_upstream(stage):
    class Watched(chain.MemoryRunsStore):
        def __init__(self):
            super().__init__()
            self.reads = []

        def latest(self, s, day):
            self.reads.append(s)
            return super().latest(s, day)

    runs = Watched()
    chain.begin(stage, DAY, runs=runs, jobs=jobs())
    assert runs.reads == [stage]
    assert chain.upstream(stage) is None


@pytest.mark.parametrize("stage", ["reconcile"])
def test_a_side_stage_already_ok_is_already_done(stage):
    runs = chain.MemoryRunsStore()
    seed(runs, stage, "running", "ok")
    with pytest.raises(chain.AlreadyDone) as err:
        chain.begin(stage, DAY, runs=runs, jobs=jobs())
    assert runs.rows[-1]["status"] == "skipped_duplicate" and runs.rows[-1]["stage"] == stage
    assert stage in str(err.value)


@pytest.mark.parametrize("stage", ["reconcile"])
def test_a_side_stage_ok_does_not_count_for_the_chain(stage):
    runs = chain.MemoryRunsStore()
    seed(runs, stage, "running", "ok")
    with pytest.raises(chain.UpstreamNotReady):
        chain.begin("detect", DAY, runs=runs, jobs=jobs())
    chain.begin("collect", DAY, runs=runs, jobs=jobs())


@pytest.mark.parametrize("stage", ["reconcile"])
@pytest.mark.parametrize("on", [False, True])
def test_a_side_stage_never_starts_a_next_job(monkeypatch, stage, on):
    if on:
        monkeypatch.setenv("CHAIN_UNDERSTAND", "1")
    fake = FakeSession()
    assert chain.start_next(stage, DAY, jobs=chain.CloudRunJobs(session=fake)) is None
    assert fake.posts == [] and fake.gets == []


# start_next


def test_start_next_posts_the_run_url_with_the_run_date_override(understand_on):
    fake = FakeSession()
    started = chain.start_next("collect", DAY, jobs=chain.CloudRunJobs(session=fake))
    assert started == "f42-understand"
    assert fake.posts == [(URL + "f42-understand:run",
                           {"overrides": {"containerOverrides": [{"env": [{"name": "RUN_DATE", "value": "2026-09-28"}]}]}})]
    assert fake.gets == []


@pytest.mark.parametrize("value", [None, "", "0", "true"])
def test_without_chain_understand_1_collect_starts_detect_with_no_get(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("CHAIN_UNDERSTAND", value)
    fake = FakeSession()
    assert chain.start_next("collect", DAY, jobs=chain.CloudRunJobs(session=fake)) == "f42-detect"
    assert [u for u, _ in fake.posts] == [URL + "f42-detect:run"]
    assert fake.gets == []


@pytest.mark.parametrize("stage,job", [("understand", "f42-detect"), ("detect", "f42-brief")])
def test_start_next_follows_the_chain_order(stage, job):
    fake = FakeSession()
    assert chain.start_next(stage, DAY, jobs=chain.CloudRunJobs(session=fake)) == job
    assert [u for u, _ in fake.posts] == [URL + job + ":run"]


@pytest.mark.parametrize("on,job", [(False, "f42-detect"), (True, "f42-understand")])
def test_the_chain_never_gets_a_job_so_a_403_on_get_cannot_matter(monkeypatch, on, job):
    # Job identities hold run.invoker and run.jobsExecutorWithOverrides only, so a GET would be 403.
    if on:
        monkeypatch.setenv("CHAIN_UNDERSTAND", "1")
    fake = FakeSession(get_status={j: 403 for j in chain.JOBS.values()})
    assert chain.start_next("collect", DAY, jobs=chain.CloudRunJobs(session=fake)) == job
    runs = chain.MemoryRunsStore()
    seed(runs, "collect", "running", "ok")
    seed(runs, "understand", "running", "ok")
    chain.begin("detect", DAY, runs=runs, jobs=chain.CloudRunJobs(session=fake))
    assert fake.gets == []


@pytest.mark.parametrize("status", [403, 404])
def test_a_403_or_404_on_the_detect_run_raises_naming_the_job_and_status(status):
    fake = FakeSession(deployed=("f42-collect", "f42-detect", "f42-brief"), post_status={"f42-detect": status})
    with pytest.raises(RuntimeError) as err:
        chain.start_next("collect", DAY, jobs=chain.CloudRunJobs(session=fake))
    assert "f42-detect" in str(err.value) and str(status) in str(err.value)


@pytest.mark.parametrize("stage", ["understand", "detect"])
def test_no_get_is_made_for_a_required_stage(stage):
    fake = FakeSession(get_status={"f42-detect": 403, "f42-brief": 403})
    chain.start_next(stage, DAY, jobs=chain.CloudRunJobs(session=fake))
    assert fake.gets == []
    runs = chain.MemoryRunsStore()
    seed(runs, "detect", "running", "ok")
    chain.begin("brief", DAY, runs=runs, jobs=chain.CloudRunJobs(session=fake))
    assert fake.gets == []


def test_brief_is_last_and_starts_nothing():
    fake = FakeSession()
    assert chain.start_next("brief", DAY, jobs=chain.CloudRunJobs(session=fake)) is None
    assert fake.posts == [] and fake.gets == []


# restart_next


class FlakySession(FakeSession):
    """A session whose first POST fails, as a transient Cloud Run Admin API error does."""

    def post(self, url, json=None, **kw):
        if not self.posts:
            self.posts.append((url, json))
            raise RuntimeError("503 from the Admin API")
        return super().post(url, json=json, **kw)


@pytest.mark.parametrize("stage,nxt,on", [("collect", "detect", False), ("collect", "understand", True),
                                          ("understand", "detect", True), ("detect", "brief", False)])
def test_restart_next_starts_a_successor_whose_dispatch_was_lost(monkeypatch, stage, nxt, on):
    if on:
        monkeypatch.setenv("CHAIN_UNDERSTAND", "1")
    runs, fake = chain.MemoryRunsStore(), FlakySession()
    for before in chain.STAGES[:chain.STAGES.index(stage)]:
        seed(runs, before, "running", "ok")
    run = chain.begin(stage, DAY, runs=runs)
    chain.finish(run, "ok", {}, runs=runs)
    with pytest.raises(RuntimeError):
        chain.start_next(stage, DAY, jobs=chain.CloudRunJobs(session=fake))
    with pytest.raises(chain.AlreadyDone):
        chain.begin(stage, DAY, runs=runs)
    assert chain.restart_next(stage, DAY, runs=runs, jobs=chain.CloudRunJobs(session=fake)) == chain.JOBS[nxt]
    assert [u for u, _ in fake.posts] == [URL + chain.JOBS[nxt] + ":run"] * 2
    assert fake.posts[1][1]["overrides"]["containerOverrides"][0]["env"] == [{"name": "RUN_DATE",
                                                                             "value": "2026-09-28"}]
    assert fake.gets == []


@pytest.mark.parametrize("successor", [("running",), ("running", "ok"), ("running", "failed"),
                                       ("running", "blocked")])
def test_restart_next_leaves_a_successor_that_has_a_runs_row(successor):
    runs, fake = chain.MemoryRunsStore(), FakeSession()
    seed(runs, "collect", "running", "ok")
    seed(runs, "detect", "running", "ok")
    seed(runs, "brief", *successor)
    assert chain.restart_next("detect", DAY, runs=runs, jobs=chain.CloudRunJobs(session=fake)) is None
    assert fake.posts == []


@pytest.mark.parametrize("own", [(), ("running",), ("running", "failed"), ("running", "blocked")])
def test_restart_next_starts_nothing_unless_the_stage_itself_is_ok_for_the_day(own):
    runs, fake = chain.MemoryRunsStore(), FakeSession()
    seed(runs, "collect", "running", "ok")
    if own:
        seed(runs, "detect", *own)
    seed(runs, "detect", "running", "ok", day=DAY - timedelta(days=1))
    assert chain.restart_next("detect", DAY, runs=runs, jobs=chain.CloudRunJobs(session=fake)) is None
    assert fake.posts == []


def test_restart_next_starts_nothing_after_brief_or_for_a_side_stage():
    runs, fake = chain.MemoryRunsStore(), FakeSession()
    seed(runs, "brief", "running", "ok")
    seed(runs, "reconcile", "running", "ok")
    for stage in ("brief", "reconcile"):
        assert chain.restart_next(stage, DAY, runs=runs, jobs=chain.CloudRunJobs(session=fake)) is None
    assert fake.posts == []


# run date and deadline


def test_run_date_defaults_to_today_in_johannesburg():
    # 23:30 UTC on the 27th is 01:30 SAST on the 28th.
    assert chain.today(now=datetime(2026, 9, 27, 23, 30, tzinfo=timezone.utc)) == DAY


def test_run_date_env_override(monkeypatch):
    monkeypatch.setenv("RUN_DATE", "2026-09-20")
    assert chain.today(now=datetime(2026, 9, 28, 3, 0, tzinfo=SAST)) == date(2026, 9, 20)
    runs = chain.MemoryRunsStore()
    chain.begin("collect", runs=runs, jobs=jobs())
    assert runs.rows[0]["run_date"] == "2026-09-20"
    fake = FakeSession()
    chain.start_next("detect", jobs=chain.CloudRunJobs(session=fake))
    assert fake.posts[0][1]["overrides"]["containerOverrides"][0]["env"] == [{"name": "RUN_DATE", "value": "2026-09-20"}]


def test_past_deadline_at_0614_and_0615_sast():
    assert not chain.past_deadline(datetime(2026, 9, 28, 6, 14, 59, tzinfo=SAST), DAY)
    assert chain.past_deadline(datetime(2026, 9, 28, 6, 15, tzinfo=SAST), DAY)
    assert chain.past_deadline(datetime(2026, 9, 28, 4, 15, tzinfo=timezone.utc), DAY)
    assert not chain.past_deadline(datetime(2026, 9, 28, 4, 14, tzinfo=timezone.utc), DAY)


def test_past_deadline_defaults_to_the_run_date(monkeypatch):
    assert chain.past_deadline(datetime(2026, 9, 28, 6, 15, tzinfo=SAST))
    monkeypatch.setenv("RUN_DATE", "2026-09-29")
    assert not chain.past_deadline(datetime(2026, 9, 28, 6, 15, tzinfo=SAST))
