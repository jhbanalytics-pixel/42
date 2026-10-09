"""Known-answer tests for the detection views and table functions (DATA.md sections 3.1, 3.4 and 3.6).

The SQL under test is BigQuery Standard SQL; duck.py runs it on DuckDB against fixture tables.
Test queries are written in BigQuery dialect too, so they can later run on BigQuery fixtures.
"""

import math
import random

import pytest
import sqlglot

from .. import sqlrun
from . import duck
from .fixtures import D, at, cmap, counter, creator, day, health, item_daily, obs, post, rid, run


@pytest.fixture
def con():
    c = duck.connect()
    yield c
    c.close()


def by(rows, key):
    return {r[key]: r for r in rows}


# sqlrun.py


EXPECTED_OBJECTS = [
    "intelligence_42_core.v_good_runs",
    "intelligence_42_core.v_collection_health_current",
    "intelligence_42_core.v_item_daily_current",
    "intelligence_42_core.v_series_test_current",
    "intelligence_42_core.v_coord_signals_current",
    "intelligence_42_core.v_item_state_current",
    "intelligence_42_agent.v_briefs_current",
    "intelligence_42_core.v_item_counter_daily_current",
    "intelligence_42_core.v_series_daily",
    "intelligence_42_core.tvf_series_signal",
    "intelligence_42_core.tvf_item_window",
    "intelligence_42_core.tvf_placebo_base",
    "intelligence_42_core.v_item_market_scope",
    "intelligence_42_core.tvf_post_items",
]


def test_statements_render_default_datasets_in_dependency_order():
    stmts = sqlrun.statements()
    names = [sqlrun.object_name(s) for s in stmts]
    assert names == EXPECTED_OBJECTS
    for s in stmts:
        assert "{core}" not in s and "{agent}" not in s
        assert s.lstrip().upper().startswith("CREATE OR REPLACE ")
        assert not s.rstrip().endswith(";")


def test_statements_parse_as_bigquery():
    # sqlglot cannot parse a CREATE TABLE FUNCTION header, so each body is parsed on its own
    for s in sqlrun.statements():
        assert sqlglot.parse_one(duck.split_create(s)[3], read="bigquery") is not None


def test_statements_render_custom_datasets():
    stmts = sqlrun.statements(core="c42", agent="a42")
    assert sqlrun.object_name(stmts[0]) == "c42.v_good_runs"
    assert all("intelligence_42" not in s for s in stmts)


class FakeJob:
    def __init__(self, rows):
        self.rows = rows

    def result(self):
        return self.rows


class FakeRow(dict):
    pass


class FakeClient:
    def __init__(self, rows=()):
        self.calls = []
        self.rows = list(rows)

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return FakeJob(self.rows)


def test_apply_views_runs_every_create_in_order():
    client = FakeClient()
    sqlrun.apply_views(client)
    assert [sqlrun.object_name(sql) for sql, _ in client.calls] == EXPECTED_OBJECTS


def test_query_passes_typed_named_params():
    client = FakeClient([FakeRow(a=1)])
    rows = sqlrun.query(client, "SELECT * FROM {core}.tvf_item_window(@d) WHERE market = @m",
                        {"d": D, "m": "ZA", "n": 3, "x": 0.5, "b": True})
    sql, cfg = client.calls[0]
    assert "intelligence_42_core.tvf_item_window(@d)" in sql
    types = {p.name: (p.type_, p.value) for p in cfg.query_parameters}
    assert types == {"d": ("DATE", D), "m": ("STRING", "ZA"), "n": ("INT64", 3),
                     "x": ("FLOAT64", 0.5), "b": ("BOOL", True)}
    assert rows == [{"a": 1}]


# 3.1 Runs and current views


def test_good_runs_picks_newest_ok_run_and_ignores_failed(con):
    duck.load(con, "agent.runs", [
        run("detect", D, "det-old", hour=8),
        run("detect", D, "det-new", hour=10),
        run("detect", D, "det-failed", status="failed", hour=11),
        run("detect", day(1), "det-y"),
        run("stats", D, "stats-failed", status="failed"),
    ])
    rows = duck.query(con, "SELECT g.stage, g.run_date, g.run_id FROM {core}.v_good_runs g ORDER BY g.stage, g.run_date")
    assert rows == [
        {"stage": "detect", "run_date": day(1), "run_id": "det-y"},
        {"stage": "detect", "run_date": D, "run_id": "det-new"},
    ]


def test_good_runs_newest_ok_run_survives_shuffled_inserts_and_ties():
    # three ok detect runs for D share a start time; other stages and dates tie on finish time
    rows = [
        run("detect", D, "det-08", hour=8), run("detect", D, "det-10", hour=10), run("detect", D, "det-12", hour=12),
        run("detect", D, "det-failed", status="failed", hour=13),
        run("collect", D, "col-12", hour=12), run("detect", day(1), "det-y-12", hour=12),
        run("aggregate", D, "agg-12", hour=12), run("aggregate", D, "agg-11", hour=11),
    ]
    for r in rows:
        r["started_at"] = at(day(1), 6)
    for seed in range(50):
        random.Random(seed).shuffle(rows)
        c = duck.connect()
        duck.load(c, "agent.runs", rows)
        got = duck.query(c, "SELECT g.stage, g.run_date, g.run_id FROM {core}.v_good_runs g "
                            "ORDER BY g.stage, g.run_date")
        c.close()
        assert got == [
            {"stage": "aggregate", "run_date": D, "run_id": "agg-12"},
            {"stage": "collect", "run_date": D, "run_id": "col-12"},
            {"stage": "detect", "run_date": day(1), "run_id": "det-y-12"},
            {"stage": "detect", "run_date": D, "run_id": "det-12"},
        ], f"seed {seed}"


def test_array_agg_order_by_limit_keeps_every_order_key(con):
    # ties on p are broken by x, ascending for top and descending for top2
    rows = [{"g": "a", "x": 5, "p": 3}, {"g": "a", "x": 2, "p": 3}, {"g": "a", "x": 9, "p": 1},
            {"g": "b", "x": 7, "p": 2}, {"g": "b", "x": 4, "p": 2}, {"g": "b", "x": 1, "p": 2}]
    sql = ("SELECT t.g, ARRAY_AGG(t.x ORDER BY t.p DESC, t.x LIMIT 1)[OFFSET(0)] top, "
           "ARRAY_AGG(t.x ORDER BY t.p DESC, t.x DESC LIMIT 2) top2 "
           "FROM UNNEST(@rows) t GROUP BY t.g ORDER BY t.g")
    for seed in range(50):
        random.Random(seed).shuffle(rows)
        assert duck.query(con, sql, {"rows": rows}) == [
            {"g": "a", "top": 2, "top2": [5, 2]}, {"g": "b", "top": 1, "top2": [7, 4]}], f"seed {seed}"


CURRENT_VIEWS = [   # view, table, stage, date column, required columns
    ("core.v_collection_health_current", "core.collection_health", "collect", "day",
     {"series": "s", "protocol": "p"}),
    ("core.v_item_daily_current", "core.item_daily", "aggregate", "metric_date",
     {"platform": "_all", "item_id": "i", "lane_class": "_any"}),
    ("core.v_series_test_current", "core.series_test", "stats", "metric_date", {"series_id": "s"}),
    ("core.v_coord_signals_current", "core.coord_signals", "coaction", "metric_date", {}),
    ("core.v_item_state_current", "core.item_state", "detect", "metric_date", {}),
    ("agent.v_briefs_current", "agent.briefs", "brief", "brief_date", {}),
]


@pytest.mark.parametrize("view,table,stage,date_col,required", CURRENT_VIEWS)
def test_current_views_read_only_the_latest_good_run(con, view, table, stage, date_col, required):
    duck.load(con, "agent.runs", [
        run(stage, D, "old", hour=8), run(stage, D, "new", hour=10),
        run(stage, D, "bad", status="failed", hour=11),
        run("other", D, "other", hour=12),
    ])
    duck.load(con, table, [{date_col: D, "market": "ZA", "run_id": r, **required}
                           for r in ("old", "new", "bad", "other")])
    schema, name = view.split(".")
    rows = duck.query(con, f"SELECT v.run_id FROM {{{schema}}}.{name} v")
    assert rows == [{"run_id": "new"}]


def test_counter_current_takes_newest_read_from_good_collect_runs(con):
    duck.load(con, "agent.runs", [
        run("collect", day(1)), run("collect", D), run("collect", D, "c-failed", status="failed", hour=20),
    ])
    duck.load(con, "core.item_counter_daily", [
        counter("it1", "curve_tiktok_hashtag", day(1), 10.0, lane_class="unbiased_counter", unit="delta",
                read_day=day(1)),
        counter("it1", "curve_tiktok_hashtag", day(1), 12.0, lane_class="unbiased_counter", unit="delta",
                read_day=D, source="vendor_history"),
        counter("it1", "curve_tiktok_hashtag", day(1), 99.0, lane_class="unbiased_counter", unit="delta",
                read_day=D, hour=20, run_id="c-failed"),
    ])
    rows = duck.query(con, "SELECT c.obs_date, c.value, c.run_id FROM {core}.v_item_counter_daily_current c")
    assert rows == [{"obs_date": day(1), "value": 12.0, "run_id": rid("collect", D)}]


# 3.4 Series and their features


def series_daily(con, series_id):
    return duck.query(con, "SELECT sd.day, sd.value, sd.trials, sd.lane_class FROM {core}.v_series_daily sd "
                           "WHERE sd.series_id = @sid ORDER BY sd.day", {"sid": series_id})


def test_rank_list_zero_before_first_sighting_on_valid_days_null_on_invalid(con):
    days = [day(i) for i in range(4, -1, -1)]
    duck.load(con, "agent.runs", [run("collect", d) for d in days])
    duck.load(con, "core.collection_health",
              [health("feed_tiktok", d, valid=(d != day(2))) for d in days]
              + [health("x_trends", d, platform="x") for d in days])
    duck.load(con, "core.item_counter_daily", [
        counter("itA", "feed_tiktok", day(2), 1.0),       # read on an invalid day: still NULL
        counter("itA", "feed_tiktok", day(1), 2.0),
        counter("itA", "feed_tiktok", D, 3.0),
        counter("itX", "x_trends", D, 2.0, platform="x"),
    ])
    rows = series_daily(con, "itA|ZA|feed_tiktok|p1")
    assert [(r["day"], r["value"], r["trials"]) for r in rows] == [
        (day(4), 0.0, 3), (day(3), 0.0, 3), (day(2), None, 3), (day(1), 2.0, 3), (D, 3.0, 3)]
    assert {r["lane_class"] for r in rows} == {"unbiased_rank"}
    assert duck.query(con, "SELECT sd.series_id FROM {core}.v_series_daily sd WHERE sd.series = 'x_trends'") == []


def test_counter_item_has_no_rows_before_first_read_and_reads_warmup(con):
    days = [day(i) for i in range(30, -1, -1)]
    duck.load(con, "agent.runs", [run("collect", d) for d in days])
    duck.load(con, "core.collection_health", [
        health("counter_tiktok_hashtag", d, market="GLOBAL", lane_class="unbiased_counter", units_ok=40)
        for d in days])
    duck.load(con, "core.cultural_map", [cmap("itC")])
    kw = dict(market="GLOBAL", lane_class="unbiased_counter", unit="delta")
    duck.load(con, "core.item_counter_daily", [
        counter("itC", "counter_tiktok_hashtag", day(2), None, **kw),   # first read: no previous total
        counter("itC", "counter_tiktok_hashtag", day(1), 40.0, **kw),
        counter("itC", "counter_tiktok_hashtag", D, 5000.0, **kw),
    ])
    sid = "itC|GLOBAL|counter_tiktok_hashtag|p1"
    rows = series_daily(con, sid)
    assert [(r["day"], r["value"]) for r in rows] == [(day(2), None), (day(1), 40.0), (D, 5000.0)]
    sig = duck.query(con, "SELECT * FROM {core}.tvf_series_signal(@d) s WHERE s.series_id = @sid",
                     {"d": D, "sid": sid})
    assert len(sig) == 1
    s = sig[0]
    assert s["baseline_state"] == "warmup"
    assert s["obs_prior"] == 1 and s["obs28"] == 1
    assert s["y"] == 5000.0 and s["med"] == 40.0
    assert s["first_measured"] == day(1)


def test_vendor_history_rows_count_as_observed_days(con):
    duck.load(con, "agent.runs", [run("collect", D), run("collect", day(5))])
    duck.load(con, "core.collection_health", [
        health("curve_tiktok_hashtag", D, lane_class="unbiased_counter", units_ok=40),
        health("curve_tiktok_hashtag", day(5), lane_class="unbiased_counter", units_ok=40, valid=False),
    ])
    duck.load(con, "core.cultural_map", [cmap("itV")])
    kw = dict(lane_class="unbiased_counter", unit="delta", read_day=D)
    duck.load(con, "core.item_counter_daily",
              [counter("itV", "curve_tiktok_hashtag", day(i), 10.0, source="vendor_history", **kw)
               for i in range(20, 0, -1)]
              + [counter("itV", "curve_tiktok_hashtag", D, 11.0, **kw)])
    sid = "itV|ZA|curve_tiktok_hashtag|p1"
    rows = series_daily(con, sid)
    assert len(rows) == 21 and all(r["value"] is not None for r in rows)
    s = duck.query(con, "SELECT * FROM {core}.tvf_series_signal(@d) s WHERE s.series_id = @sid",
                   {"d": D, "sid": sid})[0]
    assert s["obs_prior"] == 20 and s["obs28"] == 20
    assert s["baseline_state"] == "short"


def test_panel_values_are_scaled_by_k(con):
    duck.load(con, "agent.runs", [run("collect", day(i)) for i in range(4)]
              + [run("aggregate", day(i)) for i in range(4)])
    duck.load(con, "core.collection_health", [
        health("panel_fb_hub", day(3), platform="facebook", lane_class="panel", valid=False, k=3.0),
        health("panel_fb_hub", day(2), platform="facebook", lane_class="panel", k=1.0),
        health("panel_fb_hub", day(1), platform="facebook", lane_class="panel", k=1.5),
        health("panel_fb_hub", D, platform="facebook", lane_class="panel", k=2.0),
    ])
    duck.load(con, "core.item_daily", [
        item_daily("itP", day(3), 5), item_daily("itP", day(1), 4), item_daily("itP", D, 3),
        item_daily("itP", D, 7, lane_class="_any", platform="_all", series=None, protocol=None),
    ])
    rows = series_daily(con, "itP|ZA|panel_fb_hub|p1")
    assert [(r["day"], r["value"], r["trials"], r["lane_class"]) for r in rows] == [
        (day(3), None, None, "panel"), (day(2), 0.0, None, "panel"),
        (day(1), 6.0, None, "panel"), (D, 6.0, None, "panel")]


def test_multi_platform_panel_series_counts_each_post_once(con):
    # The culture desk's route names no platform: its health rows carry NULL and item_daily has a row per
    # platform its posts came from. The series is one row a day, its posts summed across platforms, with
    # the platform it first saw the item on.
    duck.load(con, "agent.runs", [run("collect", day(i)) for i in range(3)]
              + [run("aggregate", day(i)) for i in range(3)])
    duck.load(con, "core.collection_health", [
        health("panel_culture_desk", day(i), platform=None, lane_class="panel", k=1.5) for i in range(3)])
    duck.load(con, "core.cultural_map", [cmap("itC")])
    duck.load(con, "core.item_daily", [
        item_daily("itC", day(1), 2, platform="tiktok", series="panel_culture_desk"),
        item_daily("itC", D, 3, platform="tiktok", series="panel_culture_desk"),
        item_daily("itC", D, 5, platform="instagram", series="panel_culture_desk"),
    ])
    rows = duck.query(con, "SELECT sd.day, sd.platform, sd.value FROM {core}.v_series_daily sd "
                           "WHERE sd.series_id = 'itC|ZA|panel_culture_desk|p1' ORDER BY sd.day")
    assert [(r["day"], r["platform"], r["value"]) for r in rows] == [
        (day(2), "tiktok", 0.0), (day(1), "tiktok", 3.0), (D, "tiktok", 12.0)]
    sig = duck.query(con, "SELECT s.platform, s.y, s.obs_prior FROM {core}.tvf_series_signal(@d) s", {"d": D})
    assert sig == [{"platform": "tiktok", "y": 12.0, "obs_prior": 2}]


def test_series_signal_known_answer(con):
    # feed_tiktok item, protocol started at day(30); invalid days 10 to 12; first sighting day(20).
    # day(30..21) 0, day(20..13) 1, day(12..10) NULL, day(9..1) 2, D 3.
    def value(i):
        if i > 20:
            return 0
        if 13 <= i <= 20:
            return 1
        if 10 <= i <= 12:
            return None
        return 3 if i == 0 else 2

    days = {i: day(i) for i in range(30, -1, -1)}
    duck.load(con, "agent.runs", [run("collect", d) for d in days.values()])
    duck.load(con, "core.collection_health",
              [health("feed_tiktok", d, valid=not 10 <= i <= 12) for i, d in days.items()])
    duck.load(con, "core.cultural_map", [cmap("itS")])
    duck.load(con, "core.item_counter_daily",
              [counter("itS", "feed_tiktok", d, float(value(i))) for i, d in days.items() if value(i)]
              + [counter("itS", "feed_tiktok", day(11), 5.0)])      # read on an invalid day
    s = duck.query(con, "SELECT * FROM {core}.tvf_series_signal(@d) s WHERE s.item_id = 'itS'", {"d": D})
    assert len(s) == 1
    s = s[0]
    # hist is day(28..1) minus 3 invalid days: 8 zeros, 8 ones, 9 twos
    assert s["series_id"] == "itS|ZA|feed_tiktok|p1" and s["kind"] == "hashtag"
    assert s["y"] == 3.0 and s["trials"] == 3
    assert s["obs_prior"] == 27
    assert s["obs28"] == 25
    assert s["baseline_state"] == "short"
    assert s["med"] == 1.0
    assert s["hist_mean"] == pytest.approx(26 / 25)
    assert s["v3"] == pytest.approx(7 / 3)
    assert s["v7"] == pytest.approx(15.0)
    assert s["peak28"] == 3.0
    assert s["first_measured"] == day(20)
    assert s["vel"] == pytest.approx(math.log(1 + 7 / 3) - math.log(3))
    assert s["accel"] == pytest.approx(math.log(1 + 7 / 3) - math.log(3))
    assert s["z_display"] == pytest.approx(2 / 1.4826)
    assert len(s["hist"]) == 25
    assert {h["y"] for h in s["hist"]} == {0.0, 1.0, 2.0}
    assert all(h["n"] == 3 for h in s["hist"])


# 3.6 The item's window


def test_item_window_floors_boards_and_top10(con):
    duck.load(con, "agent.runs", [run("collect", day(i)) for i in range(6)])
    ob, po = [], []

    def add(pid, creator_id, d, lane_class, lane):
        ob.append(obs(pid, d, lane_class, lane))
        po.append(post(pid, creator_id, d))

    # measured lanes in the 3-day window: c1 three posts, c2 to c4 one each, flagged c5 two
    add("m1", "c1", D, "unbiased_rank", "sweep")
    add("m2", "c1", day(1), "panel", "panel")
    add("m3", "c1", day(2), "unbiased_rank", "sweep")
    add("m4", "c2", D, "panel", "panel")
    add("m5", "c3", day(1), "unbiased_rank", "sweep")
    add("m6", "c4", D, "unbiased_rank", "sweep")
    add("m7", "c5", D, "unbiased_rank", "sweep")
    add("m8", "c5", day(1), "panel", "panel")
    # the window before: one post; an older post seen again today stays dated by its first sighting
    add("m9", "c1", day(4), "unbiased_rank", "sweep")
    add("m10", "c2", day(10), "unbiased_rank", "sweep")
    ob.append(obs("m10", D, "unbiased_rank", "sweep"))
    # unmeasured evidence lanes: count in posts7, never in the floors
    add("s1", "c6", D, "search_presence", "expansion")
    add("w1", "c7", D, "watchlist", "watchlist")
    # excluded everywhere
    add("l1", "c8", D, "legacy", "legacy")
    add("pl1", "c9", D, "search_presence", "placebo")
    add("ag1", "c10", D, "search_presence", "agent_live")
    duck.load(con, "core.post_observations", ob)
    duck.load(con, "core.posts", po)
    duck.load(con, "core.post_items", [{"post_id": p["post_id"], "item_id": "itW", "via": "hashtag"} for p in po])
    duck.load(con, "core.creators", [creator(f"c{i}") for i in range(1, 11) if i != 5] + [creator("c5", 2)])
    duck.load(con, "core.item_counter_daily", [
        counter("itW", "x_trends", D, 1.0, platform="x", is_board=True),
        counter("itX", "x_trends", D, 1.0, platform="x", is_board=True),
        counter("itX", "x_trends", day(1), 2.0, platform="x", unit="rank", pull_seq=9),
        counter("itX", "x_trends", D, 1.0, platform="x", unit="rank", pull_seq=10),
        counter("itB", "board_youtube", D, 1.0, platform="youtube", is_board=True),
        counter("itT", "feed_tiktok", day(1), 7.0, unit="rank", pull_seq=41),
        counter("itT", "feed_tiktok", D, 4.0, unit="rank", pull_seq=42),
        counter("itU", "feed_tiktok", day(1), 5.0, unit="rank", pull_seq=40),
        counter("itU", "feed_tiktok", D, 3.0, unit="rank", pull_seq=42),
        counter("itR", "feed_tiktok", day(1), 3.0, unit="rank", pull_seq=41),
        counter("itR", "feed_tiktok", D, 12.0, unit="rank", pull_seq=42),
    ])
    rows = by(duck.query(con, "SELECT * FROM {core}.tvf_item_window(@d)", {"d": D}), "item_id")

    w = rows["itW"]
    assert w["market"] == "ZA"
    assert w["posts3"] == 6
    assert w["creators3"] == 4
    assert w["top_creator_share3"] == pytest.approx(3 / 8)
    assert w["posts3_prev"] == 1
    assert w["posts7"] == 11          # nine measured, one search, one watchlist
    assert w["seen7_all"] == 11
    # 7-day posts per creator: c1 4, c5 2, five others 1 each; six of the eleven posted at 09:00 today
    assert w["top3_share"] == pytest.approx(7 / 11)
    assert w["burst_share"] == pytest.approx(6 / 11)
    assert w["young_share"] == 0 and w["sponsored_share"] == 0 and w["near_dup_share"] == 0
    assert w["board_entry"] is False
    assert w["top10_twice"] is False

    assert rows["itB"]["board_entry"] is True
    assert rows["itT"]["top10_twice"] is True
    assert not rows.get("itU", {}).get("top10_twice", False)
    assert not rows.get("itR", {}).get("top10_twice", False)
    assert not rows.get("itX", {}).get("board_entry", False)
    assert not rows.get("itX", {}).get("top10_twice", False)


def test_top10_twice_ignores_a_pull_before_a_restart(con):
    # collect numbers a list's pulls over a 35-day window, so after a 40-day gap the list restarts at pull 1
    duck.load(con, "agent.runs", [run("collect", day(i)) for i in (0, 1, 40, 41)])
    duck.load(con, "core.item_counter_daily", [
        counter("itOld", "feed_tiktok", day(41), 3.0, unit="rank", pull_seq=1),    # old run, in top 10
        counter("itOld", "feed_tiktok", day(1), 15.0, unit="rank", pull_seq=1),    # restarted, outside top 10
        counter("itOld", "feed_tiktok", D, 2.0, unit="rank", pull_seq=2),
        counter("itNew", "feed_tiktok", day(1), 6.0, unit="rank", pull_seq=1),
        counter("itNew", "feed_tiktok", D, 5.0, unit="rank", pull_seq=2),
    ])
    rows = by(duck.query(con, "SELECT * FROM {core}.tvf_item_window(@d)", {"d": D}), "item_id")
    assert rows["itNew"]["top10_twice"] is True
    assert not rows.get("itOld", {}).get("top10_twice", False)


def test_top3_share_with_tied_creators(con):
    # 7-day posts per creator 3, 2, 2, 2, 1: whichever tied creator ranks third, the top 3 hold 7 of 10
    counts = {"k1": 3, "k2": 2, "k3": 2, "k4": 2, "k5": 1}
    ob, po = [], []
    for c, n in counts.items():
        for j in range(n):
            pid = f"{c}-{j}"
            ob.append(obs(pid, day(j), "unbiased_rank", "sweep"))
            po.append(post(pid, c, day(j)))
    duck.load(con, "core.post_observations", ob)
    duck.load(con, "core.posts", po)
    duck.load(con, "core.post_items", [{"post_id": p["post_id"], "item_id": "itTie", "via": "hashtag"} for p in po])
    w = duck.query(con, "SELECT w.item_id, w.top3_share FROM {core}.tvf_item_window(@d) w", {"d": D})
    assert w == [{"item_id": "itTie", "top3_share": pytest.approx(7 / 10)}]


@pytest.mark.parametrize("known,anonymous,anonymous_age,flagged", [
    (4, 1, 0, False), (4, 3, 0, False), (5, 1, 0, False),
    (0, 2, 0, False), (4, 0, 0, False), (5, 0, 0, False),
    (5, 1, 0, True), (4, 1, 3, False),
])
def test_unknown_creator_ids_never_add_to_distinct_creator_floors(
        con, known, anonymous, anonymous_age, flagged):
    posts, observations = [], []
    for index in range(known):
        for n in range(2):
            pid = f"known-{index}-{n}"
            posts.append(post(pid, f"creator-{index}", D))
            observations.append(obs(pid, D, "unbiased_rank", "sweep"))
    for index in range(anonymous):
        pid = f"anonymous-{index}"
        posts.append(post(pid, None, day(anonymous_age)))
        observations.append(obs(pid, day(anonymous_age), "unbiased_rank", "sweep"))
    duck.load(con, "core.posts", posts)
    duck.load(con, "core.post_observations", observations)
    duck.load(con, "core.post_items", [{"post_id": p["post_id"], "item_id": "itCreators", "via": "hashtag"}
                                         for p in posts])
    if flagged:
        duck.load(con, "core.creators", [creator("creator-0", coord_score=1)])

    [window] = duck.query(con, "SELECT * FROM {core}.tvf_item_window(@d)", {"d": D})

    recent_anonymous = anonymous if anonymous_age < 3 else 0
    recent_posts = 2 * known + recent_anonymous
    expected_creators = known - int(flagged)
    assert window["creators3"] == expected_creators
    assert (window["creators3"] >= 5) is (expected_creators >= 5)
    assert window["posts3"] == recent_posts - 2 * int(flagged)
    assert window["posts3_prev"] == (anonymous if anonymous_age == 3 else 0)
    assert window["posts7"] == window["seen7_all"] == len(posts)
    assert window["top_creator_share3"] == pytest.approx(max(2 if known else 0, recent_anonymous) / recent_posts)
    assert con.execute("SELECT COUNT(*) FROM core.posts").fetchone()[0] == len(posts)


def test_repeated_enrichment_rows_count_each_post_once(con):
    # e1 has two identical rows (an embed retry); e3 has two that disagree, and each field takes the
    # cautious value: the largest near_dup_size, and sponsored if any row says so
    duck.load(con, "core.post_observations", [obs(p, D, "unbiased_rank", "sweep") for p in ("e1", "e2", "e3")])
    duck.load(con, "core.posts", [post("e1", "c1", D), post("e2", "c2", D), post("e3", "c3", D)])
    duck.load(con, "core.post_items", [{"post_id": p, "item_id": "itE", "via": "hashtag"} for p in ("e1", "e2", "e3")])
    duck.load(con, "core.post_enrichment", [
        {"post_id": "e1", "near_dup_size": 3, "sponsored": True},
        {"post_id": "e1", "near_dup_size": 3, "sponsored": True},
        {"post_id": "e3", "near_dup_size": 1, "sponsored": True},
        {"post_id": "e3", "near_dup_size": 4, "sponsored": False},
    ])
    w = duck.query(con, "SELECT * FROM {core}.tvf_item_window(@d)", {"d": D})
    assert len(w) == 1
    w = w[0]
    assert w["posts3"] == 3 and w["creators3"] == 3 and w["posts7"] == 3
    assert w["top_creator_share3"] == pytest.approx(1 / 3)
    assert w["near_dup_share"] == pytest.approx(2 / 3)
    assert w["sponsored_share"] == pytest.approx(2 / 3)


def test_no_ignore_nulls_in_window_functions():
    # BigQuery rejects it: "Analytic function array_agg does not support IGNORE NULLS or RESPECT NULLS"
    for s in sqlrun.statements():
        body = sqlglot.parse_one(duck.split_create(s)[3], read="bigquery")
        for win in body.find_all(sqlglot.exp.Window):
            assert not isinstance(win.this, (sqlglot.exp.IgnoreNulls, sqlglot.exp.RespectNulls)), sqlrun.object_name(s)


def test_z_display_survives_a_negative_median_delta(con):
    # hist medians -5 and -1: the robust scale floors at SQRT(0 + 1) = 1, so z = (3 - med) / 1
    days = [day(i) for i in range(3, -1, -1)]
    duck.load(con, "agent.runs", [run("collect", d) for d in days])
    duck.load(con, "core.collection_health", [
        health("counter_tiktok_hashtag", d, market="GLOBAL", lane_class="unbiased_counter") for d in days])
    duck.load(con, "core.cultural_map", [cmap("itM5"), cmap("itM1")])
    kw = dict(market="GLOBAL", lane_class="unbiased_counter", unit="delta")
    rows = []
    for item, med in (("itM5", -5.0), ("itM1", -1.0)):
        rows += [counter(item, "counter_tiktok_hashtag", d, med, **kw) for d in days[:3]]
        rows.append(counter(item, "counter_tiktok_hashtag", D, 3.0, **kw))
    duck.load(con, "core.item_counter_daily", rows)
    s = by(duck.query(con, "SELECT s.item_id, s.med, s.z_display FROM {core}.tvf_series_signal(@d) s", {"d": D}),
           "item_id")
    assert s["itM5"]["med"] == -5.0 and s["itM5"]["z_display"] == pytest.approx(8.0)
    assert s["itM1"]["med"] == -1.0 and s["itM1"]["z_display"] == pytest.approx(4.0)


def test_no_aggregate_inside_unnest():
    # BigQuery rejects an aggregate function inside UNNEST ("Aggregate function ARRAY_AGG not allowed in UNNEST")
    for s in sqlrun.statements():
        body = sqlglot.parse_one(duck.split_create(s)[3], read="bigquery")
        for un in body.find_all(sqlglot.exp.Unnest):
            assert not list(un.find_all(sqlglot.exp.AggFunc)), sqlrun.object_name(s)


def test_placebo_base_counts_placebo_items(con):
    duck.load(con, "agent.runs", [run("aggregate", D), run("stats", D)])
    duck.load(con, "core.seed_queue", [
        {"seed_date": day(3), "market": "ZA", "item_id": f"pb{i}", "kind": "hashtag", "lane": "placebo"}
        for i in range(3)] + [{"seed_date": day(3), "market": "ZA", "item_id": "ex", "kind": "hashtag",
                               "lane": "expansion"}])
    # every placebo item is found on x and reddit; the '_any' row and the expansion item never count,
    # so the p95 is 2 whatever quantile rule the engine uses
    sp = dict(lane_class="search_presence", series=None, protocol=None)
    duck.load(con, "core.item_daily",
              [item_daily(f"pb{i}", day(i), 1, platform=pf, **sp) for i in range(3) for pf in ("x", "reddit")]
              + [item_daily("pb0", D, 2, platform="_all", lane_class="_any", series=None, protocol=None)]
              + [item_daily("ex", D, 1, platform=pf, **sp) for pf in ("x", "reddit", "youtube")])
    duck.load(con, "agent.runs", [run("aggregate", day(i)) for i in (1, 2)])
    rows = duck.query(con, "SELECT * FROM {core}.tvf_placebo_base(@d)", {"d": D})
    assert rows == [{"market": "ZA", "placebo_items": 3, "found_p95": 2, "rising_p95": 0}]



def test_market_scope_leaves_suppressed_creators_out_before_ranking(con):
    # As evidence.sql and the brief's market_scope.sql: a creator on the suppression list leaves before the pack
    # is ranked, so six located posts of suppressed creators cannot make a mostly unlocated pack market-local.
    duck.load(con, "agent.runs", [run("detect", D)])
    duck.load(con, "core.item_state", [{"metric_date": D, "market": "ZA", "item_id": "i1",
                                        "run_id": rid("detect", D)}])
    located = {"geo_market": "ZA", "geo_confidence": 0.9, "geo_source": "ext_region"}
    posts = ([(f"hidden_{n}", 1000 + n, located) for n in range(6)]
             + [(f"local_{n}", 500 + n, located) for n in range(2)]
             + [(f"unlocated_{n}", 100 + n, {}) for n in range(10)])
    duck.load(con, "core.posts", [post(p, p, day(1), engagement=e, **geo) for p, e, geo in posts])
    duck.load(con, "core.post_items", [{"post_id": p, "item_id": "i1", "via": "hashtag"} for p, _, _ in posts])
    duck.load(con, "core.post_observations", [obs(p, day(1), "unbiased_rank", "sweep") for p, _, _ in posts])
    duck.load(con, "core.suppressed_fixture", [{"creator_id": f"hidden_{n}"} for n in range(6)])
    rows = duck.query(con, "SELECT s.market_scope, s.market_posts7, s.total_posts7 "
                           "FROM {core}.v_item_market_scope s WHERE s.item_id = 'i1'")
    assert rows == [{"market_scope": "global", "market_posts7": 2, "total_posts7": 12}]


def test_market_scope_counts_the_news_posts_among_the_market_posts_only(con):
    # W8-DEC-12 at the view: seven located news posts, and five posts located in another market (one of them news),
    # make a market scope of seven posts that are all news, whatever case or padding the platform carries.
    duck.load(con, "agent.runs", [run("detect", D)])
    duck.load(con, "core.item_state", [{"metric_date": D, "market": "ZA", "item_id": "i1",
                                        "run_id": rid("detect", D)}])
    za = {"geo_market": "ZA", "geo_confidence": 0.9, "geo_source": "ext_region"}
    ng = {"geo_market": "NG", "geo_confidence": 0.9, "geo_source": "ext_region"}
    platforms = ["news", "News", " NEWS ", "news", "news", "news", "news"]
    posts = ([(f"news_{n}", 1000 + n, platforms[n], za) for n in range(7)]
             + [("ngnews", 500, "news", ng)]
             + [(f"ng_{n}", 100 + n, "tiktok", ng) for n in range(4)])
    duck.load(con, "core.posts", [post(p, p, day(1), platform=pf, engagement=e, **geo) for p, e, pf, geo in posts])
    duck.load(con, "core.post_items", [{"post_id": p, "item_id": "i1", "via": "hashtag"} for p, _, _, _ in posts])
    duck.load(con, "core.post_observations", [obs(p, day(1), "unbiased_rank", "sweep") for p, _, _, _ in posts])
    query = ("SELECT s.market_scope, s.market_posts7, s.total_posts7, s.market_news_posts7 "
             "FROM {core}.v_item_market_scope s WHERE s.item_id = 'i1'")
    assert duck.query(con, query) == [{"market_scope": "market", "market_posts7": 7, "total_posts7": 12,
                                       "market_news_posts7": 7}]

    # one market post that is not news, and the same count is six
    con.execute("UPDATE core.posts SET platform = 'tiktok' WHERE post_id = 'news_0'")
    assert duck.query(con, query)[0]["market_news_posts7"] == 6
