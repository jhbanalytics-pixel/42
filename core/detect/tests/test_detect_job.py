"""The detect Cloud Run job (DATA.md section 3 opening, BUILD.md 1.6 and 1.13): aggregate, stats and state for
one day, a runs row for each step, and the chain helper called at the start and the end.

The job runs end to end on DuckDB fixture tables through duck.Client. JobClient adds what the job needs
beyond query(): the view DDL, the state.sql script (its temp function becomes a DuckDB macro, the same
rewrite test_detect_states.py uses), insert_rows_json for runs rows and the load job run_stats uses.
FakeChain has the API of lane L1's core/collect/chain.py and writes its runs rows to the same DuckDB table.
"""

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from google.cloud import bigquery

from .. import breakout, centroids, forecasts, job, runs, seeds, sqlrun, stats, watches
from ..items import KINDS, TOPIC_KIND, item_id
from . import duck
from .fixtures import D, at, cmap, day, obs, post, run
from .duck import run_duck, temp_macro
from .duck import strip_leading_comments as _strip_leading_comments
from .test_detect_breakout import SOUND as BREAKOUT_SOUND
from .test_detect_breakout import World as BreakoutWorld
from .test_detect_breakout import three_breakers
from .test_detect_seeds import EXTRA as SEED_COLUMNS
from .test_detect_seeds import LEDGER, qrow
from .test_detect_states import World, fresh

HEX_ID = re.compile(r"^(aggregate|stats|breakout|watch|seeds|forecast|detect)-20260920-[0-9a-f]{12}$")
CATCH_UP_ID = re.compile(r"^aggregate-20260919-[0-9a-f]{12}$")


class JobClient(duck.Client):
    def __init__(self, con):
        super().__init__(con)
        self.inserted = []

    def query(self, sql, job_config=None):
        text = _strip_leading_comments(sql)
        if text.upper().startswith("CREATE OR REPLACE"):
            self.sql.append(sql)
            self.con.execute(duck.create_statement(text))
            return duck._Job([])
        if text.upper().startswith("CREATE TEMP FUNCTION"):
            self.sql.append(sql)
            temp, insert = [_strip_leading_comments(s) for s in sqlrun.split(sql)]
            self.con.execute(temp_macro(temp))
            run_duck(self.con, insert, {p.name: duck._value(p) for p in job_config.query_parameters})
            return duck._Job([])
        return super().query(sql, job_config)

    def insert_rows_json(self, table, rows):
        self.inserted.append((table, rows))
        duck.load(self.con, table, rows)
        return []

    def get_table(self, ref):
        return bigquery.Table("p." + ref, schema=[])

    def load_table_from_json(self, rows, table, job_config=None):
        duck.load(self.con, f"{table.dataset_id}.{table.table_id}", rows)
        return duck._Job([])


class AlreadyDone(Exception):
    def __init__(self, message, run):
        super().__init__(message)
        self.run = run


class UpstreamNotReady(Exception):
    def __init__(self, message, run):
        super().__init__(message)
        self.run = run


class FakeChain:
    AlreadyDone = AlreadyDone
    UpstreamNotReady = UpstreamNotReady

    def __init__(self, con=None, day=D, refuse=None):
        self.con, self.day, self.refuse = con, day, refuse
        self.calls = []

    def today(self):
        return self.day

    def _append(self, run, status, finished_at, counts, error):
        if self.con is not None:
            duck.load(self.con, "agent.runs", [{
                "run_id": run.run_id, "stage": run.stage, "run_date": run.run_date, "status": status,
                "started_at": run.started_at, "finished_at": finished_at,
                "counts": None if counts is None else json.dumps(counts), "error": error}])

    def begin(self, stage, run_date=None):
        self.calls.append(("begin", stage, run_date))
        run = SimpleNamespace(run_id=f"{stage}-{run_date:%Y%m%d}-{uuid.uuid4().hex[:12]}", stage=stage,
                              run_date=run_date, started_at=datetime.now(timezone.utc))
        if self.refuse:
            raise self.refuse("refused", run)
        self._append(run, "running", None, None, None)
        return run

    def finish(self, run, status, counts, error=None):
        self.calls.append(("finish", run.run_id, status, counts, error))
        self._append(run, status, datetime.now(timezone.utc), counts, error)

    def start_next(self, stage, run_date=None):
        self.calls.append(("start_next", stage, run_date))
        return "f42-brief"

    def restart_next(self, stage, run_date=None):
        """No runs store here, so there is never a lost start to restart."""
        self.calls.append(("restart_next", stage, run_date))
        return None

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


@pytest.fixture
def con():
    c = duck.connect()
    c.execute(LEDGER)
    for table, col, typ in SEED_COLUMNS:
        c.execute(f"ALTER TABLE core.{table} ADD COLUMN IF NOT EXISTS {col} {typ}")
    yield c
    c.close()


def world(con):
    """Two fresh items with collect runs for every day; no aggregate, stats or detect rows yet."""
    w = World()
    fresh(w, "new")
    fresh(w, "two")
    w.load(con)
    return w


def step_rows(con, stage):
    return duck.query(con, "SELECT * FROM {agent}.runs r WHERE r.stage = @s AND r.run_date = @d "
                           "ORDER BY r.finished_at", {"s": stage, "d": D})


def current(con, view, date_col="metric_date"):
    return duck.query(con, f"SELECT * FROM {{core}}.{view} v WHERE v.{date_col} = @d", {"d": D})


# runs.append


class RecordingClient:
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.calls = []

    def insert_rows_json(self, table, rows):
        self.calls.append((table, rows))
        return self.errors


# The columns of intelligence_42_agent.runs on staging, read from the live schema on 29 September 2026
STAGING_RUNS_COLUMNS = {"run_id", "stage", "run_date", "status", "started_at", "finished_at", "counts", "error",
                        "question", "tier", "plan", "calls", "credits", "tokens", "seconds", "outcome", "answer",
                        "record"}


def test_runs_append_streams_one_row_with_json_counts_carrying_model_usd():
    client = RecordingClient()
    counts = {"posts": 3}
    row = runs.append(client, "aggregate-20260920-abc", "aggregate", D, "ok",
                      "2026-09-20T01:00:00+00:00", "2026-09-20T01:05:00+00:00", counts)
    assert len(client.calls) == 1
    table, rows = client.calls[0]
    assert table == "intelligence_42_agent.runs" and rows == [row]
    assert row == {"run_id": "aggregate-20260920-abc", "stage": "aggregate", "run_date": "2026-09-20",
                   "status": "ok", "started_at": "2026-09-20T01:00:00+00:00",
                   "finished_at": "2026-09-20T01:05:00+00:00", "counts": '{"model_usd": 0.0, "posts": 3}',
                   "error": None}
    assert "model_usd" not in row and set(row) <= STAGING_RUNS_COLUMNS
    assert counts == {"posts": 3}


def test_runs_append_without_counts_writes_only_model_usd_in_counts():
    client = RecordingClient()
    row = runs.append(client, "seeds-20260920-abc", "seeds", D, "failed", "a", "b", error="ValueError: x")
    assert json.loads(row["counts"]) == {"model_usd": 0.0}
    assert "model_usd" not in row and set(row) <= STAGING_RUNS_COLUMNS


def test_runs_append_keeps_a_model_usd_the_caller_gives():
    row = runs.append(RecordingClient(), "x-20260920-abc", "detect", D, "ok", "a", "b", {"model_usd": 5.0})
    assert json.loads(row["counts"]) == {"model_usd": 5.0}


def test_runs_append_raises_when_the_insert_reports_errors():
    with pytest.raises(RuntimeError, match="runs"):
        runs.append(RecordingClient([{"index": 0, "errors": ["bad"]}]), "x", "stats", D, "ok", "a", "b")


def test_step_run_ids_carry_stage_date_and_hex():
    assert HEX_ID.match(runs.new_run_id("aggregate", D))
    assert runs.new_run_id("stats", D) != runs.new_run_id("stats", D)


# The job end to end


def test_run_end_to_end_writes_steps_and_state_for_the_detect_run(con):
    world(con)
    client, chain = JobClient(con), FakeChain(con)
    counts = job.run(client, D, chain=chain, core="core", agent="agent")

    assert [c[0] for c in chain.calls] == ["begin", "finish", "start_next"]
    assert chain.of("begin") == [("begin", "detect", D)] and chain.of("start_next") == [("start_next", "detect", D)]
    _, detect_id, status, finish_counts, error = chain.of("finish")[0]
    assert status == "ok" and error is None and finish_counts == counts
    assert HEX_ID.match(detect_id) and detect_id.startswith("detect-")

    agg, st = step_rows(con, "aggregate"), step_rows(con, "stats")
    assert len(agg) == 1 and len(st) == 1
    for row, stage in ((agg[0], "aggregate"), (st[0], "stats")):
        assert HEX_ID.match(row["run_id"]) and row["run_id"].startswith(stage + "-")
        assert row["status"] == "ok" and row["finished_at"] is not None and row["error"] is None
        assert row["started_at"] <= row["finished_at"]
    assert json.loads(agg[0]["counts"]) == {**counts["aggregate"], "model_usd": 0.0}
    assert json.loads(st[0]["counts"]) == {"series_test": counts["series_test"], "model_usd": 0.0}

    state = {r["item_id"]: r for r in current(con, "v_item_state_current")}
    assert set(state) == {"new", "two"}
    assert {r["run_id"] for r in state.values()} == {detect_id}
    assert {r["rule_version"] for r in state.values()} == {job.RULE_VERSION} == {"warmup-1"}
    assert state["new"]["state"] == "new_to_42"
    assert counts["item_state"] == 2

    daily = current(con, "v_item_daily_current")
    assert daily and {r["run_id"] for r in daily} == {agg[0]["run_id"]}
    tested = current(con, "v_series_test_current")
    assert len(tested) == counts["series_test"] > 0 and {r["run_id"] for r in tested} == {st[0]["run_id"]}


def test_run_applies_views_then_waves_and_never_creates_or_removes_a_table(con):
    world(con)
    duck.load(con, "agent.runs", [run("aggregate", day(i)) for i in (1, 2, 3)])   # prior days already aggregated
    client = JobClient(con)
    job.run(client, D, chain=FakeChain(con), core="core", agent="agent")
    creates = [_strip_leading_comments(s) for s in client.sql if _strip_leading_comments(s).upper().startswith("CREATE OR")]
    names = [sqlrun.object_name(s) for s in creates]
    assert names == ([sqlrun.object_name(s) for s in sqlrun.statements("core", "agent")] + ["core.v_item_waves"]
                     + ["core.v_item_spread"]
                     + [sqlrun.object_name(s) for s in sqlrun.agent_statements("core", "agent")]
                     + [sqlrun.object_name(s) for s in sqlrun.news_statements("core", "agent")]
                     + ["core.v_forecasts_current"])
    for sql in client.sql:
        upper = _strip_leading_comments(sql).upper()
        for word in ("DELETE", "DROP", "TRUNCATE", "ALTER"):
            assert word not in upper
        assert not re.search(r"CREATE\s+(OR\s+REPLACE\s+)?TABLE\s+(?!FUNCTION)", upper)
    # aggregate, stats, coaction, breakout, watch, seeds and forecast
    assert [t for t, _ in client.inserted] == ["agent.runs"] * 7
    assert [r["stage"] for _, rs in client.inserted for r in rs] == [
        "aggregate", "stats", "coaction", "breakout", "watch", "seeds", "forecast"]


def test_the_agent_views_exist_and_read_after_a_job_run(con):
    world(con)
    job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    views = {f"{s}.{n}" for s, n in con.execute(
        "SELECT schema_name, view_name FROM duckdb_views() WHERE NOT internal").fetchall()}
    macros = {f"{s}.{n}" for s, n in con.execute(
        "SELECT schema_name, function_name FROM duckdb_functions() WHERE function_type = 'table_macro'").fetchall()}
    assert {"agent.v_item_gate_current", "agent.v_item_evidence", "core.v_sensitive_items"} <= views
    assert "agent.tvf_item_timeseries" in macros
    for view in ("agent.v_item_gate_current", "agent.v_item_evidence", "core.v_sensitive_items"):
        con.execute(f"SELECT * FROM {view}").fetchall()
    con.execute("SELECT * FROM agent.tvf_item_timeseries('new', 'ZA', 7)").fetchall()


# Seeds: tomorrow's seed_queue rows are appended after state, and a seeds failure never stops detect


def test_run_appends_tomorrows_seeds_after_state_and_puts_their_counts_in_the_detect_counts(con, monkeypatch):
    world(con)
    seen, run_seeds = [], seeds.run_seeds

    def after_state(client, d, detect_run_id, core, agent):
        seen.append(duck.query(con, "SELECT COUNT(*) n FROM {core}.item_state s WHERE s.metric_date = @d", {"d": d}))
        return run_seeds(client, d, detect_run_id, core, agent)

    monkeypatch.setattr(seeds, "run_seeds", after_state)
    # an anchor that ran today and yielded posts, so tomorrow's queue gets one row
    duck.load(con, "core.seed_queue", [qrow("anc", D, "anchor"),
                                       qrow("anc", D, "anchor", priority=None, credits_estimate=None, yield_posts=3)])
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    assert set(counts["seeds"]) >= {"budget", "expansion", "exploration", "appended", "credits"}
    assert chain.of("finish")[0][3]["seeds"] == counts["seeds"]
    rows = step_rows(con, "seeds")
    assert len(rows) == 1 and rows[0]["status"] == "ok" and rows[0]["error"] is None
    assert HEX_ID.match(rows[0]["run_id"]) and rows[0]["run_id"].startswith("seeds-")
    assert json.loads(rows[0]["counts"]) == {**counts["seeds"], "model_usd": 0.0}
    queued = duck.query(con, "SELECT COUNT(*) n FROM {core}.seed_queue q WHERE q.seed_date = @s",
                        {"s": D.replace(day=21)})
    assert counts["seeds"]["appended"] == 1 and queued == [{"n": 1}]
    assert seen == [[{"n": 2}]]


def test_the_first_detect_of_the_day_seeds_expansion_from_its_own_states_while_it_is_still_running(con):
    # 24 items give state.sql a cohort of 20 or more, so worth_pct is set; tag19 to tag23 rank p80 or above
    w = World()
    for i in range(24):
        fresh(w, f"tag{i:02}", creators=3 + i)
    w.load(con)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    detect_id = chain.of("finish")[0][1]
    top = duck.query(con, "SELECT s.item_id FROM {core}.item_state s WHERE s.run_id = @r AND s.worth_pct >= 0.8",
                     {"r": detect_id})
    assert {r["item_id"] for r in top} == {f"tag{i}" for i in range(19, 24)}
    expansion = duck.query(con, "SELECT q.item_id, q.seed_date FROM {core}.seed_queue q WHERE q.lane = 'expansion'")
    assert {r["item_id"] for r in expansion} == {f"tag{i}" for i in range(19, 24)}
    assert {r["seed_date"] for r in expansion} == {D.replace(day=21)}
    assert counts["seeds"]["credits"]["expansion"] == 5.0


def test_a_seeds_failure_is_recorded_and_detect_and_brief_carry_on(con, monkeypatch):
    world(con)

    def boom(*a, **k):
        raise ValueError("seed_queue append failed")

    monkeypatch.setattr(seeds, "run_seeds", boom)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    assert counts["seeds"] == {"status": "failed", "error": "ValueError: seed_queue append failed"}
    assert counts["item_state"] == 2
    assert {r["item_id"] for r in current(con, "v_item_state_current")} == {"new", "two"}
    rows = step_rows(con, "seeds")
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert rows[0]["error"] == "ValueError: seed_queue append failed"
    assert json.loads(rows[0]["counts"]) == {"model_usd": 0.0}
    _, _, status, finish_counts, error = chain.of("finish")[0]
    assert status == "ok" and error is None and finish_counts["seeds"] == counts["seeds"]
    assert chain.of("start_next") == [("start_next", "detect", D)]


def test_detect_step_runs_rows_carry_model_usd_zero_in_counts_and_only_staging_columns(con, monkeypatch):
    world(con)
    client = JobClient(con)
    job.run(client, D, chain=FakeChain(con), core="core", agent="agent")
    monkeypatch.setattr(seeds, "run_seeds", lambda *a, **k: 1 / 0)
    job.run(client, D, chain=FakeChain(con), core="core", agent="agent")
    inserted = [r for _, rs in client.inserted for r in rs]
    assert inserted and all(set(r) <= STAGING_RUNS_COLUMNS for r in inserted)
    rows = duck.query(con, "SELECT r.stage, r.status, r.counts FROM {agent}.runs r "
                           "WHERE r.stage IN ('aggregate', 'stats', 'coaction', 'breakout', 'watch', 'seeds', "
                           "'forecast')")
    assert {r["stage"] for r in rows} == {"aggregate", "stats", "coaction", "breakout", "watch", "seeds", "forecast"}
    assert {r["status"] for r in rows if r["stage"] == "seeds"} == {"ok", "failed"}
    assert {json.loads(r["counts"])["model_usd"] for r in rows} == {0.0}


# Watches: today's watch_matches rows are appended after state, and a watch failure never stops detect


def test_run_matches_watches_after_state_and_puts_their_counts_in_the_detect_counts(con, monkeypatch):
    world(con)
    seen, run_watches = [], watches.run_watches

    def after_state(client, d, detect_run_id, run_id, core, agent):
        seen.append(duck.query(con, "SELECT COUNT(*) n FROM {core}.item_state s WHERE s.run_id = @r",
                               {"r": detect_run_id}))
        return run_watches(client, d, detect_run_id, run_id, core, agent)

    monkeypatch.setattr(watches, "run_watches", after_state)
    for watch_id, target, rule in (("w_new", {"kind": "hashtag", "value": "#new"}, {"state_in": ["new_to_42"]}),
                                   ("w_two", {"kind": "item", "item_id": "two"}, {"reach_over": 4})):
        duck.load(con, "agent.watches", [{"watch_id": watch_id, "created_at": at(D), "status_at": at(D),
                                          "who": "passcode", "target": json.dumps(target), "market": "ZA",
                                          "rule": json.dumps(rule), "label": watch_id, "status": "active"}])
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    assert counts["watch"] == {"watches": 2, "waiting": 0, "matches": 1, "appended": 1}
    assert chain.of("finish")[0][3]["watch"] == counts["watch"]
    rows = step_rows(con, "watch")
    assert len(rows) == 1 and rows[0]["status"] == "ok" and rows[0]["error"] is None
    assert HEX_ID.match(rows[0]["run_id"]) and rows[0]["run_id"].startswith("watch-")
    assert json.loads(rows[0]["counts"]) == {**counts["watch"], "model_usd": 0.0}
    matched = duck.query(con, "SELECT * FROM {agent}.watch_matches m")
    assert matched == [{"watch_id": "w_new", "match_date": D, "item_id": "new", "market": "ZA",
                        "method": "canonical_key", "run_id": rows[0]["run_id"]}]
    assert seen == [[{"n": 2}]]


def test_a_watch_failure_is_recorded_and_detect_and_brief_carry_on(con, monkeypatch):
    world(con)

    def boom(*a, **k):
        raise ValueError("v_watches_current not found")

    monkeypatch.setattr(watches, "run_watches", boom)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    error = "ValueError: v_watches_current not found"
    assert counts["watch"] == {"status": "failed", "error": error}
    assert counts["item_state"] == 2 and set(counts["seeds"]) >= {"appended"}
    rows = step_rows(con, "watch")
    assert len(rows) == 1 and rows[0]["status"] == "failed" and rows[0]["error"] == error
    assert HEX_ID.match(rows[0]["run_id"]) and rows[0]["run_id"].startswith("watch-")
    assert json.loads(rows[0]["counts"]) == {"model_usd": 0.0}
    _, _, status, finish_counts, finish_error = chain.of("finish")[0]
    assert status == "ok" and finish_error is None and finish_counts["watch"] == counts["watch"]
    assert chain.of("start_next") == [("start_next", "detect", D)]


# Breakouts: the day's breakout signals are appended after state, and a breakout failure never stops detect


def test_run_appends_breakout_signals_after_state_under_their_own_run(con, monkeypatch):
    world(con)
    b = BreakoutWorld()
    evidence = three_breakers(b)
    b.load(con)
    seen, append_signals = [], breakout.append_signals

    def after_state(client, d, run_id, rule_version, core, agent):
        seen.append(duck.query(con, "SELECT COUNT(*) n FROM {core}.item_state s WHERE s.metric_date = @d", {"d": d}))
        return append_signals(client, d, run_id, rule_version, core, agent)

    monkeypatch.setattr(breakout, "append_signals", after_state)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    breakout_counts = counts["breakout"]
    assert (breakout_counts["breakouts"], breakout_counts["signals"], breakout_counts["appended"]) == (3, 1, 1)
    eligibility = breakout_counts["eligibility"]
    assert eligibility["empty_reason"] is None
    assert eligibility["posts"]["breakouts"] == 3
    assert eligibility["posts"]["small_breakouts"] == 3
    assert eligibility["sightings"]["qualifying_item_groups"] == 1
    assert chain.of("finish")[0][3]["breakout"] == counts["breakout"]
    rows = step_rows(con, "breakout")
    assert len(rows) == 1 and rows[0]["status"] == "ok" and rows[0]["error"] is None
    assert HEX_ID.match(rows[0]["run_id"]) and rows[0]["run_id"].startswith("breakout-")
    assert json.loads(rows[0]["counts"]) == {**counts["breakout"], "model_usd": 0.0}
    written = duck.query(con, "SELECT * FROM {core}.breakout_signals s")
    assert [(r["metric_date"], r["market"], r["item_id"], r["run_id"], r["rule_version"], r["evidence_post_ids"])
            for r in written] == [(D, "ZA", BREAKOUT_SOUND, rows[0]["run_id"], job.RULE_VERSION, sorted(evidence))]
    assert len(seen) == 1 and seen[0][0]["n"] > 0

    job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    assert [json.loads(r["counts"])["appended"] for r in step_rows(con, "breakout")] == [1, 0]
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.breakout_signals s") == [{"n": 1}]


def test_a_breakout_failure_is_recorded_and_detect_and_brief_carry_on(con, monkeypatch):
    world(con)

    def boom(*a, **k):
        raise ValueError("breakout_signals not found")

    monkeypatch.setattr(breakout, "append_signals", boom)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    error = "ValueError: breakout_signals not found"
    assert counts["breakout"] == {"status": "failed", "error": error}
    assert counts["item_state"] == 2
    assert {r["item_id"] for r in current(con, "v_item_state_current")} == {"new", "two"}
    assert set(counts["watch"]) >= {"appended"} and set(counts["seeds"]) >= {"appended"}
    rows = step_rows(con, "breakout")
    assert len(rows) == 1 and rows[0]["status"] == "failed" and rows[0]["error"] == error
    assert HEX_ID.match(rows[0]["run_id"]) and rows[0]["run_id"].startswith("breakout-")
    assert json.loads(rows[0]["counts"]) == {"model_usd": 0.0}
    _, _, status, finish_counts, finish_error = chain.of("finish")[0]
    assert status == "ok" and finish_error is None and finish_counts["breakout"] == counts["breakout"]
    assert chain.of("start_next") == [("start_next", "detect", D)]


# Forecasts: issued and resolved after seeds, and a forecast failure never stops detect


def test_run_issues_forecasts_after_seeds_from_the_detect_runs_states(con, monkeypatch):
    world(con)
    seen, run_forecasts = [], forecasts.run_forecasts

    def after_seeds(client, d, detect_run_id, core, agent):
        seen.append((detect_run_id, [r["status"] for r in step_rows(con, "seeds")]))
        return run_forecasts(client, d, detect_run_id, core, agent)

    monkeypatch.setattr(forecasts, "run_forecasts", after_seeds)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    detect_id = chain.of("finish")[0][1]
    assert seen == [(detect_id, ["ok"])]
    control = {i for i in ("new", "two") if forecasts.in_control(i, D)}
    assert counts["forecast"] == {"candidates": 2, "watched": 0, "control": len(control),
                                  "issued": 6 * len(control), "resolved": 0}
    assert chain.of("finish")[0][3]["forecast"] == counts["forecast"]
    rows = step_rows(con, "forecast")
    assert len(rows) == 1 and rows[0]["status"] == "ok" and rows[0]["error"] is None
    assert HEX_ID.match(rows[0]["run_id"]) and rows[0]["run_id"].startswith("forecast-")
    assert json.loads(rows[0]["counts"]) == {**counts["forecast"], "model_usd": 0.0}
    assert duck.query(con, "SELECT COUNT(*) n FROM {agent}.forecasts f") == [{"n": 6 * len(control)}]


def test_a_forecast_failure_is_recorded_and_detect_and_brief_carry_on(con, monkeypatch):
    world(con)

    def boom(*a, **k):
        raise ValueError("forecasts table not found")

    monkeypatch.setattr(forecasts, "run_forecasts", boom)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    error = "ValueError: forecasts table not found"
    assert counts["forecast"] == {"status": "failed", "error": error}
    assert counts["item_state"] == 2 and set(counts["seeds"]) >= {"appended"}
    rows = step_rows(con, "forecast")
    assert len(rows) == 1 and rows[0]["status"] == "failed" and rows[0]["error"] == error
    assert HEX_ID.match(rows[0]["run_id"]) and rows[0]["run_id"].startswith("forecast-")
    assert json.loads(rows[0]["counts"]) == {"model_usd": 0.0}
    _, _, status, finish_counts, finish_error = chain.of("finish")[0]
    assert status == "ok" and finish_error is None and finish_counts["forecast"] == counts["forecast"]
    assert chain.of("start_next") == [("start_next", "detect", D)]


# Agent views: a failure there never stops detect or brief


def test_agent_views_ok_is_recorded_in_the_detect_counts(con):
    world(con)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    assert counts["agent_views"] == {"status": "ok"}
    assert chain.of("finish")[0][3]["agent_views"] == {"status": "ok"}


def test_an_agent_views_failure_is_recorded_and_detect_and_brief_carry_on(con, monkeypatch):
    world(con)

    def boom(*a, **k):
        raise ValueError("v_item_gate_current create failed")

    monkeypatch.setattr(sqlrun, "apply_agent_views", boom)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    error = "ValueError: v_item_gate_current create failed"
    assert counts["agent_views"] == {"status": "failed", "error": error}
    assert counts["item_state"] == 2
    assert {r["item_id"] for r in current(con, "v_item_state_current")} == {"new", "two"}
    _, _, status, finish_counts, finish_error = chain.of("finish")[0]
    assert status == "ok" and finish_error is None
    assert finish_counts["agent_views"] == {"status": "failed", "error": error}
    assert chain.of("start_next") == [("start_next", "detect", D)]


def test_spread_ok_is_recorded_and_the_spread_view_reads_after_a_job_run(con):
    world(con)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    assert counts["spread"] == {"status": "ok"}
    assert chain.of("finish")[0][3]["spread"] == {"status": "ok"}
    con.execute("SELECT * FROM core.v_item_spread").fetchall()


def test_a_spread_failure_is_recorded_and_detect_and_brief_carry_on(con, monkeypatch):
    world(con)

    def boom(*a, **k):
        raise ValueError("v_item_spread create failed")

    monkeypatch.setattr(sqlrun, "apply_spread", boom)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    error = "ValueError: v_item_spread create failed"
    assert counts["spread"] == {"status": "failed", "error": error}
    assert counts["agent_views"] == {"status": "ok"}
    assert counts["item_state"] == 2
    assert {r["item_id"] for r in current(con, "v_item_state_current")} == {"new", "two"}
    _, _, status, finish_counts, finish_error = chain.of("finish")[0]
    assert status == "ok" and finish_error is None
    assert finish_counts["spread"] == {"status": "failed", "error": error}
    assert chain.of("start_next") == [("start_next", "detect", D)]


def test_a_clean_run_records_no_view_failures(con, capsys):
    world(con)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    assert counts["view_failures"] == {}
    assert chain.of("finish")[0][3]["view_failures"] == {}
    assert "view builds failed" not in capsys.readouterr().err


def test_view_build_failures_are_named_together_in_the_detect_runs_row_and_the_chain_carries_on(
        con, monkeypatch, capsys):
    world(con)

    def boom(message):
        def fail(*a, **k):
            raise ValueError(message)
        return fail

    monkeypatch.setattr(sqlrun, "apply_agent_views", boom("v_item_gate_current create failed"))
    monkeypatch.setattr(sqlrun, "apply_news", boom("v_item_origin create failed"))
    monkeypatch.setattr(centroids, "run_item_centroids", boom("cultural_map merge failed"))
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    expected = {"agent_views": "ValueError: v_item_gate_current create failed",
                "news": "ValueError: v_item_origin create failed",
                "item_centroids": "ValueError: cultural_map merge failed"}
    assert counts["view_failures"] == expected
    assert counts["item_state"] == 2
    _, detect_id, status, finish_counts, _ = chain.of("finish")[0]
    assert status == "ok" and finish_counts["view_failures"] == expected
    assert chain.of("start_next") == [("start_next", "detect", D)]
    row = con.execute("SELECT status, counts FROM agent.runs WHERE run_id = ? AND finished_at IS NOT NULL",
                      [detect_id]).fetchall()
    assert len(row) == 1 and row[0][0] == "ok" and json.loads(row[0][1])["view_failures"] == expected
    err = capsys.readouterr().err
    assert f"detect {D.isoformat()}: view builds failed: agent_views, news, item_centroids" in err


def test_a_later_failure_still_records_the_view_failures_before_it(con, monkeypatch):
    world(con)

    def boom(*a, **k):
        raise ValueError("v_item_spread create failed")

    monkeypatch.setattr(sqlrun, "apply_spread", boom)
    monkeypatch.setattr(job, "run_state", lambda *a, **k: 1 / 0)
    chain = FakeChain(con)
    with pytest.raises(ZeroDivisionError):
        job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    _, _, status, finish_counts, _ = chain.of("finish")[0]
    assert status == "failed"
    assert finish_counts["view_failures"] == {"spread": "ValueError: v_item_spread create failed"}


def test_a_views_failure_still_fails_the_detect_run(con, monkeypatch):
    world(con)
    monkeypatch.setattr(sqlrun, "apply_views", lambda *a, **k: 1 / 0)
    chain = FakeChain(con)
    with pytest.raises(ZeroDivisionError):
        job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    assert [c[2] for c in chain.of("finish")] == ["failed"]
    assert chain.of("start_next") == []


def test_second_run_after_new_collect_appends_and_current_views_show_the_newest(con):
    w = world(con)
    client = JobClient(con)
    first_chain = FakeChain(con)
    job.run(client, D, chain=first_chain, core="core", agent="agent")
    first_detect = first_chain.of("finish")[0][1]

    # a later collect run for the same day writes its own health rows and brings one more post for "new"
    duck.load(con, "agent.runs", [{"run_id": "collect-20260920-b", "stage": "collect", "run_date": D,
                                   "status": "ok", "started_at": datetime.now(timezone.utc),
                                   "finished_at": datetime.now(timezone.utc)}])
    duck.load(con, "core.collection_health", [{**h, "run_id": "collect-20260920-b"}
                                              for h in w.health.values() if h["day"] == D])
    duck.load(con, "core.posts", [{**post("new-late", "new-late-c", D), "published_at": at(D, 15)}])
    duck.load(con, "core.post_observations", [{**obs("new-late", D, "unbiased_rank", "sweep", hour=15),
                                               "run_id": "collect-20260920-b"}])
    duck.load(con, "core.post_items", [{"post_id": "new-late", "item_id": "new", "via": "hashtag"}])

    second_chain = FakeChain(con)
    job.run(client, D, chain=second_chain, core="core", agent="agent")
    second_detect = second_chain.of("finish")[0][1]
    assert second_detect != first_detect

    agg, st = step_rows(con, "aggregate"), step_rows(con, "stats")
    assert len(agg) == 2 and len(st) == 2 and agg[0]["run_id"] != agg[1]["run_id"]
    assert all(r["status"] == "ok" for r in agg + st)

    base = duck.query(con, "SELECT DISTINCT s.run_id FROM {core}.item_state s WHERE s.metric_date = @d", {"d": D})
    assert {r["run_id"] for r in base} == {first_detect, second_detect}
    assert {r["run_id"] for r in current(con, "v_item_state_current")} == {second_detect}
    daily = current(con, "v_item_daily_current")
    assert {r["run_id"] for r in daily} == {agg[1]["run_id"]}
    anyrow = [r for r in daily if r["item_id"] == "new" and r["lane_class"] == "_any"]
    assert [r["posts"] for r in anyrow] == [2]
    assert {r["run_id"] for r in current(con, "v_series_test_current")} == {st[1]["run_id"]}
    base_daily = duck.query(con, "SELECT DISTINCT i.run_id FROM {core}.item_daily i WHERE i.metric_date = @d",
                            {"d": D})
    assert {r["run_id"] for r in base_daily} == {agg[0]["run_id"], agg[1]["run_id"]}


def test_a_stoplisted_hashtag_keeps_its_state_but_is_never_eligible(con):
    fyp, ama = item_id("hashtag", "fyp"), item_id("hashtag", "amapiano")
    w = World()
    fresh(w, fyp)
    fresh(w, ama)
    for p in w.t["core.posts"]:
        p["hashtags"] = ["#FYP"] if p["post_id"].startswith(fyp) else ["#Amapiano"]
    w.load(con)
    job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    cm = {r["item_id"]: (r["kind"], r["status"])
          for r in duck.query(con, "SELECT c.item_id, c.kind, c.status FROM {core}.cultural_map c")}
    assert cm.pop(fyp) == ("hashtag", "generic") and cm.pop(ama) == ("hashtag", "active")
    assert cm and set(cm.values()) == {("creator", "active")}      # the posts' creators
    state = {r["item_id"]: r for r in current(con, "v_item_state_current")}
    assert set(state) == {fyp, ama}
    assert state[fyp]["state"] == state[ama]["state"] == "new_to_42"
    assert state[fyp]["eligible"] is False
    assert state[ama]["eligible"] is True


# Catch-up: a day whose aggregate never ran ok is aggregated before d


def agg_rows(con, d):
    return duck.query(con, "SELECT * FROM {agent}.runs r WHERE r.stage = 'aggregate' AND r.run_date = @d "
                           "ORDER BY r.finished_at", {"d": d})


def test_a_missed_day_is_aggregated_during_the_next_run_and_read_through_the_current_view(con):
    world(con)
    counts = job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    missed = agg_rows(con, day(1))
    assert len(missed) == 1 and missed[0]["status"] == "ok" and CATCH_UP_ID.match(missed[0]["run_id"])
    assert json.loads(missed[0]["counts"]) == {**counts["catch_up"][day(1).isoformat()], "model_usd": 0.0}
    daily = duck.query(con, "SELECT * FROM {core}.v_item_daily_current v WHERE v.metric_date = @d", {"d": day(1)})
    assert daily and {r["run_id"] for r in daily} == {missed[0]["run_id"]}
    assert [r["posts"] for r in daily if r["item_id"] == "new" and r["lane_class"] == "_any"] == [1]


def test_a_day_with_an_ok_aggregate_run_is_not_aggregated_again_but_a_failed_one_is(con):
    world(con)
    duck.load(con, "agent.runs", [run("aggregate", day(2)), run("aggregate", day(3), "aggregate-bad", "failed")])
    job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    assert [r["run_id"] for r in agg_rows(con, day(2))] == ["aggregate-20260918"]
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.item_daily i WHERE i.metric_date = @d",
                      {"d": day(2)}) == [{"n": 0}]
    assert [r["status"] for r in agg_rows(con, day(3))] == ["failed", "ok"]


def test_catch_up_never_reaches_before_d_minus_3_and_stats_and_state_run_for_d_only(con):
    world(con)
    job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    steps = duck.query(con, "SELECT r.stage, r.run_date FROM {agent}.runs r "
                            "WHERE r.stage IN ('aggregate', 'stats') ORDER BY r.stage, r.run_date")
    assert [(r["stage"], r["run_date"]) for r in steps] == [
        ("aggregate", day(3)), ("aggregate", day(2)), ("aggregate", day(1)), ("aggregate", D), ("stats", D)]
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.item_daily i WHERE i.metric_date < @d",
                      {"d": day(3)}) == [{"n": 0}]
    assert duck.query(con, "SELECT DISTINCT s.metric_date FROM {core}.series_test s") == [{"metric_date": D}]
    assert duck.query(con, "SELECT DISTINCT s.metric_date FROM {core}.item_state s") == [{"metric_date": D}]


def test_late_cluster_replays_original_date_after_aggregate_start_and_stops_after_replay(con):
    world(con)
    client = JobClient(con)
    job.run(client, D, chain=FakeChain(con), core="core", agent="agent")

    first = agg_rows(con, D)[-1]
    old = agg_rows(con, day(3))[-1]
    con.execute("UPDATE agent.runs SET started_at = ?, finished_at = ? WHERE run_id = ?",
                [at(D, 10), at(D, 12), first["run_id"]])
    con.execute("UPDATE agent.runs SET started_at = ?, finished_at = ? WHERE run_id = ?",
                [at(day(3), 10), at(day(3), 12), old["run_id"]])
    duck.load(con, "core.cultural_map", [cmap("late-topic", "topic"), cmap("old-topic", "topic"),
                                         cmap("future-topic", "topic"), cmap("failed-topic", "topic")])
    duck.load(con, "core.clusters", [
        {"cluster_date": D, "cluster_id": "late-d", "market": "za", "item_id": "late-topic",
         "match_kind": "new"},
        {"cluster_date": day(3), "cluster_id": "late-old", "market": "za", "item_id": "old-topic",
         "match_kind": "new"},
        {"cluster_date": day(1), "cluster_id": "late-failed", "market": "za", "item_id": "failed-topic",
         "match_kind": "new"},
        {"cluster_date": D + timedelta(days=1), "cluster_id": "future", "market": "za",
         "item_id": "future-topic", "match_kind": "new"},
    ])
    duck.load(con, "core.cluster_members", [
        {"cluster_id": "late-d", "post_id": "new-p1", "probability": 0.9},
        {"cluster_id": "late-old", "post_id": "new-p1", "probability": 0.9},
        {"cluster_id": "late-failed", "post_id": "new-p1", "probability": 0.9},
        {"cluster_id": "future", "post_id": "new-p1", "probability": 0.9},
    ])
    duck.load(con, "agent.runs", [
        {"run_id": "understand-late", "stage": "understand", "run_date": D, "status": "ok",
         "started_at": at(D, 10), "finished_at": at(D, 11)},
        {"run_id": "understand-old", "stage": "understand", "run_date": day(3), "status": "ok",
         "started_at": at(day(3), 12), "finished_at": at(day(3), 13)},
        {"run_id": "understand-failed", "stage": "understand", "run_date": day(1), "status": "failed",
         "started_at": at(day(1), 12), "finished_at": at(day(1), 13)},
    ])

    next_day = D + timedelta(days=1)
    counts = job.run(client, next_day, chain=FakeChain(con, day=next_day), core="core", agent="agent")
    assert set(counts["catch_up"]) == {D.isoformat()}
    assert len(agg_rows(con, D)) == 2
    latest = agg_rows(con, D)[-1]
    topic = duck.query(con, "SELECT i.run_id, i.posts FROM {core}.v_item_daily_current i "
                           "WHERE i.metric_date = @d AND i.item_id = 'late-topic' AND i.lane_class = '_any'",
                       {"d": D})
    assert latest["status"] == "ok" and topic == [{"run_id": latest["run_id"], "posts": 1}]
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.post_items p WHERE p.post_id = 'new-p1' "
                           "AND p.item_id = 'late-topic'") == [{"n": 1}]
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.post_items p WHERE p.post_id = 'new-p1' "
                           "AND p.item_id = 'future-topic'") == [{"n": 0}]
    assert len(agg_rows(con, day(3))) == len(agg_rows(con, day(1))) == 1

    repeated = job.run(client, next_day, chain=FakeChain(con, day=next_day), core="core", agent="agent")
    assert repeated["catch_up"] == {} and len(agg_rows(con, D)) == 2
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.post_items p WHERE p.item_id = 'late-topic'") == [
        {"n": 1}]
    assert duck.query(con, "SELECT i.run_id, i.posts FROM {core}.v_item_daily_current i "
                           "WHERE i.metric_date = @d AND i.item_id = 'late-topic' AND i.lane_class = '_any'",
                       {"d": D}) == [{"run_id": latest["run_id"], "posts": 1}]


def test_failed_late_cluster_replay_keeps_old_success_current_and_retries_after_link_insert(con, monkeypatch):
    world(con)
    client = JobClient(con)
    job.run(client, D, chain=FakeChain(con), core="core", agent="agent")
    first = agg_rows(con, D)[-1]
    con.execute("UPDATE agent.runs SET started_at = ?, finished_at = ? WHERE run_id = ?",
                [at(D, 10), at(D, 12), first["run_id"]])
    duck.load(con, "core.cultural_map", [cmap("late-topic", "topic")])
    duck.load(con, "core.clusters", [{"cluster_date": D, "cluster_id": "late-d", "market": "za",
                                      "item_id": "late-topic", "match_kind": "new"}])
    duck.load(con, "core.cluster_members", [{"cluster_id": "late-d", "post_id": "new-p1", "probability": 0.9}])
    duck.load(con, "agent.runs", [{"run_id": "understand-late", "stage": "understand", "run_date": D,
                                    "status": "ok", "started_at": at(D, 10), "finished_at": at(D, 11)}])

    original_run = job.aggregate._run

    def fail_before_daily(client_, sql, params, core, agent):
        if sql == job.aggregate.aggregate_sql():
            raise RuntimeError("daily insert failed")
        return original_run(client_, sql, params, core, agent)

    monkeypatch.setattr(job.aggregate, "_run", fail_before_daily)
    next_day = D + timedelta(days=1)
    with pytest.raises(RuntimeError, match="daily insert failed"):
        job.run(client, next_day, chain=FakeChain(con, day=next_day), core="core", agent="agent")
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.post_items p WHERE p.post_id = 'new-p1' "
                           "AND p.item_id = 'late-topic'") == [{"n": 1}]
    current_before_retry = duck.query(con, "SELECT i.run_id FROM {core}.v_item_daily_current i "
                                           "WHERE i.metric_date = @d AND i.item_id = 'new' AND i.lane_class = '_any'",
                                      {"d": D})
    assert current_before_retry == [{"run_id": first["run_id"]}]
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.v_item_daily_current i WHERE i.metric_date = @d "
                           "AND i.item_id = 'late-topic'", {"d": D}) == [{"n": 0}]

    monkeypatch.setattr(job.aggregate, "_run", original_run)
    retry = job.run(client, next_day, chain=FakeChain(con, day=next_day), core="core", agent="agent")
    assert set(retry["catch_up"]) == {D.isoformat()}
    latest = agg_rows(con, D)[-1]
    assert latest["status"] == "ok"
    assert duck.query(con, "SELECT i.run_id, i.posts FROM {core}.v_item_daily_current i "
                           "WHERE i.metric_date = @d AND i.item_id = 'late-topic' AND i.lane_class = '_any'",
                       {"d": D}) == [{"run_id": latest["run_id"], "posts": 1}]
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.post_items p WHERE p.item_id = 'late-topic'") == [
        {"n": 1}]


def test_a_day_without_health_rows_is_not_aggregated(con):
    w = World()
    fresh(w, "new")
    fresh(w, "two")
    w.health = {k: h for k, h in w.health.items() if h["day"] != day(2)}
    w.load(con)
    job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    assert agg_rows(con, day(2)) == []
    assert [len(agg_rows(con, d)) for d in (day(3), day(1), D)] == [1, 1, 1]


# main: exit codes and the chain


def test_main_success_exits_zero_and_starts_the_next_job(con):
    world(con)
    chain = FakeChain(con)
    assert job.main(client=JobClient(con), chain=chain, core="core", agent="agent") == 0
    assert chain.of("begin") == [("begin", "detect", D)]
    assert chain.of("finish")[0][2] == "ok"
    assert chain.of("start_next") == [("start_next", "detect", D)]


def test_stats_failure_finishes_failed_exits_nonzero_and_starts_nothing(con, monkeypatch):
    world(con)

    def boom(*a, **k):
        raise ValueError("series_test load failed")

    monkeypatch.setattr(stats, "run_stats", boom)
    chain = FakeChain(con)
    assert job.main(client=JobClient(con), chain=chain, core="core", agent="agent") == 1
    finishes = chain.of("finish")
    assert len(finishes) == 1
    _, run_id, status, counts, error = finishes[0]
    assert status == "failed" and "series_test load failed" in error
    assert chain.of("start_next") == []
    assert step_rows(con, "stats") == []
    assert [r["status"] for r in step_rows(con, "aggregate")] == ["ok"]
    assert current(con, "v_item_state_current") == []
    rows = duck.query(con, "SELECT r.status, r.error FROM {agent}.runs r WHERE r.run_id = @r AND r.finished_at IS NOT NULL",
                      {"r": run_id})
    assert rows == [{"status": "failed", "error": error}]


def test_run_reraises_after_finishing_failed(con, monkeypatch):
    world(con)
    monkeypatch.setattr(stats, "run_stats", lambda *a, **k: 1 / 0)
    chain = FakeChain(con)
    with pytest.raises(ZeroDivisionError):
        job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    assert [c[2] for c in chain.of("finish")] == ["failed"]
    assert chain.of("start_next") == []


def test_already_done_exits_zero_without_work(con):
    client, chain = JobClient(con), FakeChain(con, refuse=AlreadyDone)
    assert job.main(client=client, chain=chain, core="core", agent="agent") == 0
    assert client.sql == [] and client.inserted == []
    assert chain.of("finish") == [] and chain.of("start_next") == []



def test_a_rerun_after_brief_failed_to_start_starts_it_without_redoing_detect(con, monkeypatch):
    from core.collect import chain

    for name in ("FORCE_RERUN", "CLOUD_RUN_EXECUTION", "CHAIN_UNDERSTAND"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RUN_DATE", D.isoformat())
    world(con)
    store = chain.MemoryRunsStore()
    collect = chain.begin("collect", D, runs=store)
    chain.finish(collect, "ok", {}, runs=store)
    monkeypatch.setattr(chain, "default_runs", lambda: store)
    posts = []

    class AdminApi:
        def run(self, job_name, env):
            posts.append((job_name, env))
            if len(posts) == 1:
                raise RuntimeError("503 from the Admin API")
            return {}

    monkeypatch.setattr(chain, "CloudRunJobs", AdminApi)
    assert job.main(client=JobClient(con), chain=chain, core="core", agent="agent") == 1
    assert store.latest("detect", D)["status"] == "ok" and store.latest("brief", D) is None
    again = JobClient(con)
    assert job.main(client=again, chain=chain, core="core", agent="agent") == 0
    assert again.sql == [] and again.inserted == [], "detect already ran ok, so none of its steps runs again"
    assert posts == [("f42-brief", {"RUN_DATE": D.isoformat()})] * 2
    store.append({"run_id": "brief-x", "stage": "brief", "run_date": D.isoformat(), "status": "running",
                  "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None, "counts": None,
                  "error": None})
    assert job.main(client=JobClient(con), chain=chain, core="core", agent="agent") == 0
    assert len(posts) == 2, "a brief that has a runs row is never started again"


def test_upstream_not_ready_exits_nonzero_without_work(con):
    client, chain = JobClient(con), FakeChain(con, refuse=UpstreamNotReady)
    assert job.main(client=client, chain=chain, core="core", agent="agent") == 1
    assert client.sql == [] and client.inserted == []
    assert chain.of("finish") == [] and chain.of("start_next") == []


def test_main_reads_the_day_from_chain_today(con):
    other = D.replace(day=19)
    client, chain = JobClient(con), FakeChain(con, day=other, refuse=AlreadyDone)
    job.main(client=client, chain=chain, core="core", agent="agent")
    assert chain.of("begin") == [("begin", "detect", other)]


def test_run_writes_hashtag_and_sound_centroids_after_aggregate(con):
    world(con)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    assert counts["item_centroids"] == {"centroids": 0}
    assert chain.of("finish")[0][3]["item_centroids"] == counts["item_centroids"]


def test_a_centroid_failure_is_recorded_and_detect_and_brief_carry_on(con, monkeypatch):
    world(con)

    def boom(*a, **k):
        raise ValueError("cultural_map merge failed")

    monkeypatch.setattr(centroids, "run_item_centroids", boom)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    error = "ValueError: cultural_map merge failed"
    assert counts["item_centroids"] == {"status": "failed", "error": error}
    assert counts["item_state"] == 2
    _, _, status, finish_counts, finish_error = chain.of("finish")[0]
    assert status == "ok" and finish_error is None
    assert finish_counts["item_centroids"] == {"status": "failed", "error": error}
    assert chain.of("start_next") == [("start_next", "detect", D)]


# Lead ruling on a dead clusterer: understand records the day ok but partial (cluster_stack_failed), detect still runs
# for the non-topic items, and judges no topic items that day.


def understand_row(day=D, counts=None, status="ok", hour=12, run_id=None):
    return {**run("understand", day, run_id, status, hour), "error": None,
            "counts": None if counts is None else json.dumps(counts)}


PARTIAL = {"partial": True, "partial_reason": "cluster_stack_failed", "partial_error": "OSError: libllvmlite",
           "data_issue": "Data issue: topic grouping failed today"}


def topic_world(con, *understand):
    world(con)
    con.execute("UPDATE core.cultural_map SET kind = 'topic' WHERE item_id = 'two'")
    if understand:
        duck.load(con, "agent.runs", list(understand))


def judged(con):
    return {r["item_id"] for r in current(con, "v_item_state_current")}


def test_a_day_whose_clusterer_failed_judges_no_topic_items_but_still_judges_the_rest(con):
    topic_world(con, understand_row(counts=PARTIAL))
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    assert judged(con) == {"new"} and counts["item_state"] == 1
    assert counts["topics_failed"] == {"reason": "cluster_stack_failed", "topic_items_judged": 0,
                                       "data_issue": "Data issue: topic grouping failed today"}
    assert [c[0] for c in chain.calls] == ["begin", "finish", "start_next"]
    assert chain.of("finish")[0][2] == "ok", "detect itself is a good run: it ran for the non-cluster items"


def test_without_a_partial_understand_run_topic_items_are_judged_as_before(con):
    topic_world(con)
    counts = job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    assert judged(con) == {"new", "two"} and "topics_failed" not in counts


@pytest.mark.parametrize("row", [
    understand_row(counts={"enriched": 3}),
    understand_row(counts={"partial": True, "partial_reason": "something_else"}),
    understand_row(counts=PARTIAL, status="failed"),
    understand_row(day=D - timedelta(days=1), counts=PARTIAL),
    understand_row(counts=None),
], ids=["clean ok", "other reason", "failed run", "other day", "no counts"])
def test_only_an_ok_partial_run_for_the_day_with_the_reason_stops_topic_items(con, row):
    topic_world(con, row)
    counts = job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    assert judged(con) == {"new", "two"} and "topics_failed" not in counts


def test_a_repaired_rerun_of_understand_lifts_the_stop_and_an_older_clean_run_does_not_hide_a_newer_partial(con):
    topic_world(con, understand_row(counts=PARTIAL, hour=6, run_id="understand-early"),
                understand_row(counts={"enriched": 3}, hour=9, run_id="understand-repaired"))
    counts = job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    assert judged(con) == {"new", "two"} and "topics_failed" not in counts


def test_the_newest_ok_understand_run_decides_when_a_clean_one_came_first(con):
    topic_world(con, understand_row(counts={"enriched": 3}, hour=6, run_id="understand-early"),
                understand_row(counts=PARTIAL, hour=9, run_id="understand-late"))
    job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    assert judged(con) == {"new"}


def test_the_topic_filter_changes_only_the_cultural_map_join_of_state_sql_and_stops_if_the_shape_moves():
    text = (job.SQL / "state.sql").read_text(encoding="utf-8")
    patched = job.without_topics(text)
    stop = f" AND cm.kind != '{TOPIC_KIND}'"
    assert patched != text and patched.count(stop) == 1
    assert patched.replace(stop, "") == text
    with pytest.raises(RuntimeError):
        job.without_topics(text.replace("cm.item_id = a.item_id", "cm.item_id = a.item_id AND TRUE"))


def test_the_topic_kind_is_the_kind_the_understand_clusterer_writes_and_a_real_item_kind():
    assert TOPIC_KIND == "topic" and TOPIC_KIND in KINDS
    assert "cluster" not in KINDS


def test_topic_items_judged_is_counted_from_item_state_for_the_run_and_not_taken_as_zero(con):
    topic_world(con)
    job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    topic_rows = [r for r in current(con, "v_item_state_current") if r["item_id"] == "two"]
    assert len(topic_rows) == 1
    rid = con.execute("SELECT run_id FROM core.item_state WHERE item_id = 'two'").fetchone()[0]
    assert job.topic_items_judged(JobClient(con), D, rid, core="core", agent="agent") == 1
    assert job.topic_items_judged(JobClient(con), D, "some-other-run", core="core", agent="agent") == 0


def test_if_the_stop_ever_lets_a_topic_item_through_the_detect_counts_say_so(con, monkeypatch):
    topic_world(con, understand_row(counts=PARTIAL))
    monkeypatch.setattr(job, "without_topics", lambda script: script)
    counts = job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    assert judged(con) == {"new", "two"}
    assert counts["topics_failed"]["topic_items_judged"] == 1


def test_the_kind_understand_writes_for_a_new_topic_item_is_the_kind_detect_stops_on():
    from core.understand import cluster as understand_cluster

    c = {"cluster_id": "20260928-za-000", "label": "A topic", "centroid": [1.0, 0.0], "market": "za",
         "platform": "tiktok", "keywords": ["a"], "local_terms": [], "members": [("p1", 1.0)]}
    planned = understand_cluster.plan([c], [{"kind": "new", "item_id": None, "candidates": []}], [], D, "za")
    [row] = planned["map_rows"]
    assert row["change"] == "insert" and row["kind"] == TOPIC_KIND


def test_the_stderr_line_names_the_number_measured_after_state(con, capsys):
    topic_world(con, understand_row(counts=PARTIAL))
    job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    assert "0 topic items judged" in capsys.readouterr().err


def test_the_stderr_line_does_not_promise_that_none_were_judged_when_one_was(con, monkeypatch, capsys):
    topic_world(con, understand_row(counts=PARTIAL))
    monkeypatch.setattr(job, "without_topics", lambda script: script)
    job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    err = capsys.readouterr().err
    assert "1 topic items judged" in err and "no topic items judged" not in err


def test_a_failing_topic_count_is_carried_in_the_counts_and_never_fails_detect(con, monkeypatch, capsys):
    topic_world(con, understand_row(counts=PARTIAL))

    def broken(client, d, run_id, core="core", agent="agent"):
        raise RuntimeError("boom")

    monkeypatch.setattr(job, "topic_items_judged", broken)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    assert counts["topics_failed"]["topic_items_judged"] == {"status": "failed", "error": "RuntimeError: boom"}
    assert counts["topics_failed"]["reason"] == "cluster_stack_failed"
    assert judged(con) == {"new"} and "breakout" in counts and "forecast" in counts
    assert chain.of("finish")[0][2] == "ok" and chain.of("start_next")
    assert "topic count failed: RuntimeError: boom" in capsys.readouterr().err


def test_the_topic_count_is_items_judged_and_not_open_map_versions_times_rows(con, monkeypatch):
    topic_world(con, understand_row(counts=PARTIAL))
    con.execute("INSERT INTO core.cultural_map SELECT * FROM core.cultural_map WHERE item_id = 'two'")
    monkeypatch.setattr(job, "without_topics", lambda script: script)
    counts = job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    written = con.execute("SELECT COUNT(*) FROM core.item_state WHERE kind = 'topic'").fetchone()[0]
    assert counts["topics_failed"]["topic_items_judged"] == written


def test_the_topic_count_reads_the_kind_item_state_recorded_and_not_the_current_map(con):
    topic_world(con)
    job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    rid = con.execute("SELECT run_id FROM core.item_state WHERE item_id = 'two'").fetchone()[0]
    con.execute("UPDATE core.cultural_map SET valid_to = TIMESTAMP '2026-09-21 00:00:00' WHERE item_id = 'two'")
    assert job.topic_items_judged(JobClient(con), D, rid, core="core", agent="agent") == 1


def test_the_data_issue_is_the_understand_text_when_it_has_one_and_the_plain_words_when_it_does_not(con):
    topic_world(con, understand_row(counts={"partial": True, "partial_reason": "cluster_stack_failed"}))
    counts = job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    assert counts["topics_failed"]["data_issue"] == "Data issue: topic grouping failed today"


def test_a_data_issue_understand_wrote_is_carried_as_it_was_written(con):
    topic_world(con, understand_row(counts={**PARTIAL, "data_issue": "Data issue: written by understand"}))
    counts = job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    assert counts["topics_failed"]["data_issue"] == "Data issue: written by understand"


def test_an_understand_run_partial_on_the_vector_index_stops_no_topic_items(con):
    """understand marks an index the warehouse refused as partial (partial_reason index_failed) so the watchdog alerts;
    the topics were grouped, so detect judges them and records no topics_failed."""
    index_failed = {"partial": True, "partial_reason": "index_failed", "partial_error": "BadRequest: Column 'embedding'"}
    topic_world(con, understand_row(counts=index_failed))
    counts = job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    assert judged(con) == {"new", "two"} and "topics_failed" not in counts
