"""Tests for the repair run (COLLECT_ONLY_ROUTES with FORCE_RERUN=1, core/collect/job.py) and its health carry-forward
(core/collect/writers.py repair_base and carry_forward).

A base run is made first through job.main with every youtube/videos/trending call failing, as the vendor failed on
2 October. The repair run then reads only that route, and both runs' rows are loaded into the DuckDB harness of the
detection SQL (core/detect/tests/duck.py), so the detect and brief readers can be compared before and after.
"""

import json
from datetime import date, timedelta

import pytest

from core.brief import gatectx
from core.collect import chain, job, writers
from core.collect.socialcrawl_client import PRICED, quote_for
from core.collect.tests.test_job import (TUESDAY, FakeBQ, FakeClient, FakeJob, no_page, no_public_feed,
                                         run_main)
from core.detect import aggregate, sqlrun, stats
from core.detect.tests import duck

YT = "youtube/videos/trending"
DAY = TUESDAY.isoformat()
REPAIR_ENV = {"FORCE_RERUN": "1", job.ONLY_ROUTES: YT}


class RepairBQ(FakeBQ):
    """FakeBQ that also answers the repair run's two reads of its base: the day's good collect run and its health
    rows. Records every query, so the tests can see what the repair read."""

    def __init__(self, base_run=None, base_health=(), **kw):
        super().__init__(**kw)
        self.base_run, self.base_health = base_run, [dict(r) for r in base_health]

    def query(self, sql, job_config=None, **kw):
        params = {p.name: p.value for p in (job_config.query_parameters if job_config else [])
                  if hasattr(p, "value")}
        if sql == writers.REPAIR_RUN_SQL.format(runs=writers.table("runs", writers.AGENT)):
            self.queries.append((sql, params))
            if self.base_run is None:
                return FakeJob([])
            return FakeJob([{"run_id": self.base_run["run_id"], "counts": json.dumps(self.base_run["counts"])}])
        if sql == writers.REPAIR_HEALTH_SQL.format(health=writers.table("collection_health")):
            self.queries.append((sql, params))
            return FakeJob([dict(r, day=date.fromisoformat(r["day"])) for r in self.base_health
                            if r["day"] == params["d"].isoformat() and r["run_id"] == params["run_id"]])
        return super().query(sql, job_config, **kw)


def fail_youtube(n, route, market):
    return "error" if route == YT else None


class Trap:
    """A local getter or public feed transport that must never be called."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append(url)
        return no_page(url) if not kw else no_public_feed(url, **kw)


@pytest.fixture
def base(monkeypatch):
    """The 02:00 run of TUESDAY with every YouTube trending call failing: (runs, bq, client, ok row)."""
    runs, bq, client = chain.MemoryRunsStore(), RepairBQ(), FakeClient(fail_youtube)
    assert run_main(["--run-date", DAY], runs=runs, bq=bq, client=client) == 0
    ok = runs.rows[-1]
    assert ok["status"] == "ok"
    return runs, bq, client, ok


def repair(base, monkeypatch, *, env=None, health=None, get=None, transport=None):
    runs, base_bq, _, ok = base
    monkeypatch.setenv("FORCE_RERUN", "1")  # chain.begin reads it from the process environment
    bq = RepairBQ(ok, base_bq.loaded("collection_health") if health is None else health)
    client = FakeClient()
    code = run_main(["--run-date", DAY], runs=runs, bq=bq, client=client, env=env or REPAIR_ENV, get=get,
                    public_feed_transport=transport)
    return code, bq, client, runs.rows[-1]


def key(row):
    return row["day"], row["market"], row["series"], row["protocol"]


# The env var


def test_unset_or_blank_is_a_normal_run():
    assert job.only_routes({}) is None
    assert job.only_routes({job.ONLY_ROUTES: "  "}) is None


def test_accepted_only_with_force_rerun_and_rank_or_board_routes():
    assert job.only_routes(REPAIR_ENV) == frozenset({YT})
    assert job.only_routes({"FORCE_RERUN": "1", job.ONLY_ROUTES: f" {YT} , apple_music/charts"}) == \
        frozenset({YT, "apple_music/charts"})
    with pytest.raises(ValueError, match="FORCE_RERUN=1"):
        job.only_routes({job.ONLY_ROUTES: YT})
    with pytest.raises(ValueError, match="FORCE_RERUN=1"):
        job.only_routes({"FORCE_RERUN": "0", job.ONLY_ROUTES: YT})


@pytest.mark.parametrize("routes", [",", ",,", " , ,", ", "])
def test_refused_when_set_to_only_commas(routes):
    with pytest.raises(ValueError, match="names no route"):
        job.only_routes({"FORCE_RERUN": "1", job.ONLY_ROUTES: routes})


def test_main_refuses_only_commas_before_any_runs_row_or_call(monkeypatch, capsys):
    monkeypatch.setenv("FORCE_RERUN", "1")
    runs, client = chain.MemoryRunsStore(), FakeClient()
    assert run_main(["--run-date", DAY], runs=runs, client=client, env={"FORCE_RERUN": "1", job.ONLY_ROUTES: ",,"}) == 2
    assert runs.rows == [] and client.calls == []
    assert "COLLECT_ONLY_ROUTES is set but names no route" in capsys.readouterr().err


@pytest.mark.parametrize("routes", ["tiktok/search/top", "search/multi", "tiktok/hashtag", "tiktok/song/videos",
                                    "facebook/profile/posts", "prism/profiles", "twitter/user/tweets",
                                    "prism/post-stats", "web/scrape", "youtube/videos", f"{YT},search/multi"])
def test_refused_for_routes_that_are_not_rank_lists_or_boards(routes):
    with pytest.raises(ValueError, match="rank list and board routes"):
        job.only_routes({"FORCE_RERUN": "1", job.ONLY_ROUTES: routes})


def test_repair_routes_are_exactly_the_rank_lists_and_boards():
    from core.collect.parse import ROUTES

    assert job.REPAIR_ROUTES == {r for r, e in ROUTES.items() if e[0] in ("rank", "board")}
    assert YT in job.REPAIR_ROUTES


@pytest.mark.parametrize("env", [{job.ONLY_ROUTES: YT}, {"FORCE_RERUN": "1", job.ONLY_ROUTES: "search/multi"}])
def test_main_refuses_before_any_runs_row_or_call(env, monkeypatch):
    monkeypatch.setenv("FORCE_RERUN", "1")
    runs, client = chain.MemoryRunsStore(), FakeClient()
    assert run_main(["--run-date", DAY], runs=runs, client=client, env=env) == 2
    assert runs.rows == [] and client.calls == []


def test_main_refuses_a_repair_on_a_day_with_no_ok_collect_run(monkeypatch, capsys):
    monkeypatch.setenv("FORCE_RERUN", "1")
    runs, client = chain.MemoryRunsStore(), FakeClient()
    assert run_main(["--run-date", DAY], runs=runs, client=client, bq=RepairBQ(None), env=REPAIR_ENV) == 2
    assert runs.rows == [] and client.calls == []
    assert "has no ok collect run" in capsys.readouterr().err


def test_plan_under_the_env_var_lists_only_that_route(capsys):
    assert run_main(["--plan", "--run-date", DAY], env=REPAIR_ENV) == 0
    out = capsys.readouterr().out
    rows = [line.split() for line in out.splitlines() if line.startswith("  ") and line.split()[0].isdigit()]
    assert rows and {r[2] for r in rows} == {YT}
    assert len(rows) == len(job.MARKETS) * len(job.YOUTUBE_CATEGORIES)
    assert "repair run" in out and "LOCAL sources" not in out


# The calls


def test_only_the_listed_routes_calls_are_made_live_and_nothing_else_runs(base, monkeypatch):
    get, transport = Trap(), Trap()
    code, bq, client, final = repair(base, monkeypatch, get=get, transport=transport)
    assert code == 0 and final["status"] == "ok"
    assert {c["route"] for c in client.calls} == {YT}
    assert sorted((c["market"], c["params"].get("category", "")) for c in client.calls) == sorted(
        (m, cat) for m in job.MARKETS for cat in job.YOUTUBE_CATEGORIES)
    assert all(c["use_cache"] is False for c in client.calls)  # the stored 02:00 response is what is repaired
    assert get.calls == [] and transport.calls == []                # no local sources, no public feeds
    sqls = [sql for sql, _ in bq.queries]
    assert not any("seed_queue" in s or "v_watches_current" in s or "INFORMATION_SCHEMA" in s for s in sqls)
    assert bq.loaded("seed_queue") == [] and bq.loaded("google_search_signals") == []
    assert bq.loaded("raw_responses") == []
    assert {o["route"] for o in bq.loaded("post_observations")} == {YT}
    assert {c["route"] for c in bq.loaded("item_counter_daily")} == {YT}


def test_credits_are_spent_only_on_the_listed_route(base, monkeypatch):
    code, _, client, final = repair(base, monkeypatch)
    assert code == 0
    assert {c["route"] for c in client.charged} == {YT}
    spent = sum(c["credits"] for c in client.charged)
    assert spent == sum(quote_for(YT, PRICED[YT].method, c["params"]) for c in client.calls) == 15
    own = final["counts"]["repair"]
    assert own["credits_charged"] == spent and own["local_credits"] == 0
    assert "TRENDS" not in own["credits_by_share"] and own["credits_by_share"]["LOCAL"] == 0


def test_a_protocol_already_valid_in_the_base_run_is_not_read_again(base, monkeypatch):
    health = [dict(r) for r in base[1].loaded("collection_health")]
    za_chart = next(r for r in health if r["market"] == "ZA" and r["protocol"] == f"{YT}?region=ZA")
    za_chart.update(valid=True, invalid_reason=None, calls_ok=1)
    code, bq, client, final = repair(base, monkeypatch, health=health)
    assert code == 0
    assert len(client.calls) == 14
    assert ("ZA", "") not in {(c["market"], c["params"].get("category", "")) for c in client.calls}
    written = {key(r): r for r in bq.loaded("collection_health")}
    assert written[key(za_chart)] == dict(za_chart, run_id=written[key(za_chart)]["run_id"])
    assert final["counts"]["repair"]["kept_valid"] == sum(r["valid"] for r in health)


# The carry-forward


def test_carry_forward_rows_equal_the_base_runs_rows_for_every_series_not_read(base, monkeypatch):
    base_rows = base[1].loaded("collection_health")
    code, bq, _, final = repair(base, monkeypatch)
    assert code == 0
    rid = final["run_id"]
    written = bq.loaded("collection_health")
    assert {r["run_id"] for r in written} == {rid}
    own = [r for r in written if r["route"] == YT]
    carried = [r for r in written if r["route"] != YT]
    assert len(own) == 15 and all(r["valid"] for r in own)
    assert sorted(map(key, written)) == sorted(map(key, base_rows))  # one row per key, none lost, none added
    by_key = {key(r): r for r in base_rows}
    for row in carried:
        assert row == dict(by_key[key(row)], run_id=rid)
    assert len(carried) == len(base_rows) - 15
    assert {r["platform"] for r in carried} >= {"tiktok", "reddit", "apple_music", "facebook"}


def test_carry_forward_keeps_only_keys_the_repair_did_not_write():
    base_rows = [{"day": DAY, "market": "ZA", "series": s, "protocol": p, "valid": v, "run_id": "base"}
                 for s, p, v in (("board_youtube", "a", False), ("feed_tiktok", "b", True))]
    own = [{"day": DAY, "market": "ZA", "series": "board_youtube", "protocol": "a", "valid": True, "run_id": "fix"}]
    assert writers.carry_forward(base_rows, own, "fix") == [dict(base_rows[1], run_id="fix")]
    assert writers.carry_forward(base_rows, [], "fix") == [dict(r, run_id="fix") for r in base_rows]


def test_repair_counts_restate_the_base_and_hold_their_own_under_repair(base, monkeypatch):
    ok = base[3]
    code, _, _, final = repair(base, monkeypatch)
    assert code == 0
    counts = final["counts"]
    assert {k: v for k, v in counts.items() if k != "repair"} == ok["counts"]  # Today's receipt, Fieldwork's states
    own = counts["repair"]
    assert own["base_run_id"] == ok["run_id"] and own["routes"] == [YT]
    assert own["calls"] == 15 and own["health_written"] == 15
    assert own["health_carried"] == len(base[1].loaded("collection_health")) - 15


def test_a_failed_repair_keeps_the_base_as_the_good_run(base, monkeypatch):
    runs, base_bq, _, ok = base
    monkeypatch.setenv("FORCE_RERUN", "1")

    class Broken(RepairBQ):
        def load_table_from_json(self, rows, table_id, job_config=None):
            raise RuntimeError("load failed")

    bq = Broken(ok, base_bq.loaded("collection_health"))
    assert run_main(["--run-date", DAY], runs=runs, bq=bq, env=REPAIR_ENV) == 1
    assert runs.rows[-1]["status"] == "failed"
    assert [r["run_id"] for r in runs.rows if r["status"] == "ok"] == [ok["run_id"]]


# Detect and brief readers before and after the repair (the DuckDB harness of the detection SQL)


def _columns(con, table):
    schema, name = table.split(".")
    return {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns "
                                      "WHERE table_schema = ? AND table_name = ?", [schema, name]).fetchall()}


def _load(con, table, rows):
    cols = _columns(con, table)
    duck.load(con, table, [{k: v for k, v in r.items() if k in cols} for r in rows])


def _load_run(con, runs_rows, bq, client_posts):
    """Loads one collect run's rows; client_posts holds the post ids and runs rows already loaded."""
    fresh = [r for r in runs_rows if r["status"] in ("ok", "failed") and ("run", r["run_id"]) not in client_posts]
    client_posts.update({("run", r["run_id"]): r for r in fresh})
    _load(con, "agent.runs", [dict(r, counts=json.dumps(r["counts"]) if r.get("counts") else None) for r in fresh])
    _load(con, "core.collection_health", bq.loaded("collection_health"))
    _load(con, "core.post_observations", bq.loaded("post_observations"))
    _load(con, "core.item_counter_daily", bq.loaded("item_counter_daily"))
    new = [p for pid, p in bq.posts.items() if pid not in client_posts]  # posts MERGE: a known post stays one row
    client_posts.update(bq.posts)
    _load(con, "core.posts", [dict(p, hashtags=None, vendor_labels=None) for p in new])
    # One item per post stands in for detect's itemisation, so a post's item is on its platform alone.
    _load(con, "core.post_items", [{"post_id": p["post_id"], "item_id": f"it|{p['post_id']}", "via": "test"}
                                   for p in new])


NEXT = TUESDAY + timedelta(days=1)


def _readers(con, platforms):
    """Every reader the repair could move, keyed by name: the rows each gives, with the run ids left out."""
    def q(sql, params=None):
        return sorted((json.dumps({k: v for k, v in r.items() if k != "run_id"}, default=str, sort_keys=True)
                       for r in duck.query(con, sql, params) if r.get("platform") != "youtube"))

    out = {
        "health_current": q("SELECT * FROM {core}.v_collection_health_current h WHERE h.day = @d "
                            "AND IFNULL(h.platform, '') != 'youtube'", {"d": TUESDAY}),
        "counters_current": q("SELECT * FROM {core}.v_item_counter_daily_current c WHERE c.platform != 'youtube'"),
        "series_daily": q("SELECT * FROM {core}.v_series_daily s WHERE s.platform != 'youtube'"),
        "item_window": q("SELECT w.* FROM {core}.tvf_item_window(@d) w JOIN {core}.posts p "
                         "ON w.item_id = CONCAT('it|', p.post_id) WHERE p.platform != 'youtube'", {"d": TUESDAY}),
        "stats_weekday": q(stats.TOTALS_SQL, {"d": NEXT}),
    }
    for m in job.MARKETS:
        for p in platforms:
            out[f"g1 {m} {p}"] = q(gatectx.QUERIES["health"], {"market": m, "platform": p, "d": TUESDAY,
                                                                   "item_id": "", "series_id": ""})
    return out


def _aggregate(con, run_id):
    duck.run_duck(con, sqlrun.render(aggregate.aggregate_sql(), core="core", agent="agent"),
                  {"d": TUESDAY, "run_id": run_id, "rule_version": "t"})
    rows = duck.query(con, "SELECT i.* FROM {core}.item_daily i WHERE i.run_id = @r AND i.platform != 'youtube' "
                           "AND i.item_id NOT IN (SELECT CONCAT('it|', p.post_id) FROM {core}.posts p "
                           "WHERE p.platform = 'youtube')", {"r": run_id})
    return sorted(json.dumps({k: v for k, v in r.items() if k not in ("run_id", "available_at")}, default=str,
                             sort_keys=True) for r in rows)


def _youtube_g1(con):
    return {m: duck.query(con, gatectx.QUERIES["health"], {"market": m, "platform": "youtube", "d": TUESDAY,
                                                                "item_id": "", "series_id": ""})
            for m in job.MARKETS}


def test_detect_and_brief_readers_see_the_same_non_youtube_data_before_and_after(base, monkeypatch):
    runs, base_bq, _, ok = base
    con = duck.connect()
    seen = {}
    _load_run(con, runs.rows, base_bq, seen)
    platforms = sorted({r["platform"] for r in base_bq.loaded("collection_health")
                        if r["platform"] and r["platform"] != "youtube"})
    assert {"tiktok", "reddit", "apple_music", "facebook", "instagram", "twitter"} <= set(platforms)
    before, agg_before = _readers(con, platforms), _aggregate(con, "agg-before")
    assert agg_before and all(before[k] for k in ("health_current", "counters_current", "series_daily",
                                                  "item_window", "stats_weekday"))
    assert _youtube_g1(con) == {m: [{"day": TUESDAY, "ok": False}] for m in job.MARKETS}

    code, bq, _, final = repair(base, monkeypatch)
    assert code == 0
    _load_run(con, runs.rows, bq, seen)
    good = duck.query(con, "SELECT g.run_id FROM {core}.v_good_runs g WHERE g.stage = 'collect' AND g.run_date = @d",
                      {"d": TUESDAY})
    assert good == [{"run_id": final["run_id"]}]  # the repair is now the day's good collect run

    assert _readers(con, platforms) == before
    assert _aggregate(con, "agg-after") == agg_before
    assert _youtube_g1(con) == {m: [{"day": TUESDAY, "ok": True}] for m in job.MARKETS}
    yt_obs = duck.query(con, "SELECT COUNT(*) n FROM {core}.post_observations o WHERE o.route = @r", {"r": YT})
    assert yt_obs[0]["n"] > 0


def test_without_the_carry_forward_every_other_platform_would_vanish_for_the_day(base, monkeypatch):
    """The control for the test above: the repair's own rows alone leave the good run with YouTube only."""
    runs, base_bq, _, _ = base
    code, bq, _, _ = repair(base, monkeypatch)
    assert code == 0
    con = duck.connect()
    _load(con, "agent.runs", [dict(r, counts=None) for r in runs.rows if r["status"] == "ok"])
    _load(con, "core.collection_health", base_bq.loaded("collection_health")
          + [r for r in bq.loaded("collection_health") if r["route"] == YT])
    left = duck.query(con, "SELECT DISTINCT h.platform FROM {core}.v_collection_health_current h WHERE h.day = @d",
                      {"d": TUESDAY})
    assert left == [{"platform": "youtube"}]
