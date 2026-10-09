"""Known-answer tests for the warm-up states (BUILD.md 1.6): v_item_waves, the stats pass-through and the
item_state INSERT (DATA.md sections 3.5, 3.6 and 3.7).

Each test builds a small world of fixture rows, copies tvf_series_signal to series_test through
stats.passthrough_rows inside DuckDB, runs sql/state.sql and reads item_state back.
"""

from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import pytest
import sqlglot
from google.cloud import bigquery

from .. import sqlrun, stats
from . import duck
from .duck import run_duck, temp_macro
from .duck import strip_leading_comments as _strip_leading_comments
from .fixtures import D, at, cmap, counter, creator, day, health, item_daily, obs, post, rid, run

SQL = Path(__file__).resolve().parents[1] / "sql"
DATA_MD = Path(__file__).resolve().parents[3] / "docs" / "full-42" / "DATA.md"
RUN, RULE, STATS_RUN = "detect-test", "r1", "stats-test"

RANK = dict(lane_class="unbiased_rank", platform="tiktok", series="feed_tiktok", protocol="p1")
LEGACY = dict(lane_class="legacy", platform="tiktok", series=None, protocol=None)
SEARCH = dict(lane_class="search_presence", platform="instagram", series=None, protocol=None)
ANY = dict(lane_class="_any", platform="_all", series=None, protocol=None)


def sql_statements(name, core="core", agent="agent"):
    text = (SQL / name).read_text(encoding="utf-8")
    return [_strip_leading_comments(s) for s in sqlrun.split(sqlrun.render(text, core, agent))]


@pytest.fixture
def con():
    c = duck.connect()
    for stmt in sql_statements("waves.sql"):
        c.execute(duck.create_statement(stmt))
    yield c
    c.close()


def by(rows, key):
    return {r[key]: r for r in rows}


class World:
    """Fixture rows for one test; build() loads them, runs rows for every stage and date, and series_test."""

    def __init__(self):
        self.t = defaultdict(list)
        self.days = defaultdict(set)
        self.items = set()
        self.health = {}
        self.n = 0

    def protocol(self, series, first, platform="tiktok", lane_class="unbiased_rank", market="ZA",
                 protocol="p1", k=None):
        """A series protocol with valid days from day(first) to D."""
        for i in range(first, -1, -1):
            self.health[(series, market, protocol, day(i))] = health(
                series, day(i), market=market, platform=platform, protocol=protocol, lane_class=lane_class, k=k)
            self.days["collect"].add(day(i))

    def counter(self, item, series, i, value, **kw):
        self.items.add(item)
        self.t["core.item_counter_daily"].append(counter(item, series, day(i), value, **kw))
        self.days["collect"].add(kw.get("read_day") or day(i))

    def daily(self, item, i, posts, **kw):
        self.items.add(item)
        self.t["core.item_daily"].append(item_daily(item, day(i), posts, **kw))
        self.days["aggregate"].add(day(i))

    def posts(self, item, i, creators, lane_class="unbiased_rank", lane="sweep", platform="tiktok", flagged=()):
        """One post per creator on day(i), each in its own 10-minute bucket."""
        self.items.add(item)
        for c in creators:
            self.n += 1
            pid = f"{item}-p{self.n}"
            p = post(pid, c, day(i), platform=platform)
            p["published_at"] = at(day(i), 9) + timedelta(minutes=10 * self.n)
            self.t["core.post_observations"].append(obs(pid, day(i), lane_class, lane, platform=platform))
            self.t["core.posts"].append(p)
            self.t["core.post_items"].append({"post_id": pid, "item_id": item, "via": "hashtag"})
            if c not in {r["creator_id"] for r in self.t["core.creators"]}:
                self.t["core.creators"].append(creator(c, 1 if c in flagged else 0))

    def load(self, con):
        duck.load(con, "core.cultural_map", [cmap(i) for i in sorted(self.items)])
        duck.load(con, "core.collection_health", list(self.health.values()))
        duck.load(con, "agent.runs", [run(stage, d) for stage, ds in self.days.items() for d in ds])
        for table, rows in self.t.items():
            duck.load(con, table, rows)

    def build(self, con, d=D):
        self.load(con)
        signal = duck.query(con, "SELECT * FROM {core}.tvf_series_signal(@d)", {"d": d})
        duck.load(con, "core.series_test", stats.passthrough_rows(signal, d, STATS_RUN, RULE))
        duck.load(con, "agent.runs", [run("stats", d, STATS_RUN)])


def detect(con, d=D):
    temp, insert = sql_statements("state.sql")
    con.execute(temp_macro(temp))
    run_duck(con, insert, {"d": d, "run_id": RUN, "rule_version": RULE})
    rows = duck.query(con, "SELECT * FROM {core}.item_state s WHERE s.run_id = @r AND s.metric_date = @d",
                      {"r": RUN, "d": d})
    return by(rows, "item_id")


def fresh(w, item, first=2, creators=3, flagged=0):
    """An item on the local feed from day(first), three posts by distinct creators in the last 3 days."""
    w.protocol("feed_tiktok", max(9, first))
    for i in range(first, -1, -1):
        w.counter(item, "feed_tiktok", i, 1.0)
    cs = [f"{item}-c{j}" for j in range(creators)]
    for j, c in enumerate(cs):
        w.posts(item, j % 3, [c], flagged=cs[:flagged])


# Files and the stats pass-through


def test_waves_sql_is_one_bigquery_view():
    stmts = sql_statements("waves.sql", core=sqlrun.CORE, agent=sqlrun.AGENT)
    assert [sqlrun.object_name(s) for s in stmts] == ["intelligence_42_core.v_item_waves"]
    assert sqlglot.parse_one(duck.split_create(stmts[0])[3], read="bigquery") is not None


def test_state_sql_is_the_data_md_query_verbatim():
    section = DATA_MD.read_text(encoding="utf-8").split("### 3.7 States", 1)[1]
    block = section.split("```sql\n", 1)[1].split("```", 1)[0]
    ours = sqlrun.render((SQL / "state.sql").read_text(encoding="utf-8"))
    assert _strip_leading_comments(ours).strip() == block.strip()


SIGNAL = {
    "series_id": "it|ZA|feed_tiktok|p1", "item_id": "it", "market": "ZA", "platform": "tiktok",
    "series": "feed_tiktok", "protocol": "p1", "lane_class": "unbiased_rank", "kind": "hashtag",
    "y": 3.0, "trials": 3, "hist": [{"day": day(1), "y": 2.0, "n": 3}], "obs_prior": 20, "obs28": 20,
    "first_measured": day(20), "hist_mean": 1.5, "med": 2.0, "v3": 2.5, "v7": 14.0, "peak28": 3.0,
    "vel": 0.2, "accel": -0.1, "z_display": 0.7, "baseline_state": "short",
}

SERIES_TEST_COLUMNS = [
    "metric_date", "series_id", "item_id", "market", "platform", "series", "protocol", "lane_class", "kind",
    "y", "trials", "obs_prior", "obs28", "first_measured", "baseline_state", "hist_mean", "med", "v3", "v7",
    "peak28", "vel", "accel", "z_display", "test", "mu", "alpha", "weekday_factor", "mu_prior", "ratio",
    "p_mid", "q", "significant", "run_id", "rule_version",
]


def test_passthrough_copies_every_signal_row_untested():
    rows = stats.passthrough_rows([SIGNAL, {**SIGNAL, "series_id": "b", "y": None}], D, "st-1", "r9")
    assert len(rows) == 2
    r = rows[0]
    assert sorted(r) == sorted(SERIES_TEST_COLUMNS)
    for k, v in SIGNAL.items():
        if k != "hist":
            assert r[k] == v
    assert r["metric_date"] == D and r["run_id"] == "st-1" and r["rule_version"] == "r9"
    assert r["test"] == "none" and r["significant"] is False
    assert all(r[k] is None for k in ("mu", "alpha", "weekday_factor", "mu_prior", "ratio", "p_mid", "q"))
    assert rows[1]["series_id"] == "b" and rows[1]["y"] is None


class FakeJob:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.done = False

    def result(self):
        self.done = True
        return self.rows


class FakeRow(dict):
    pass


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.queries, self.loads = [], []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        return FakeJob([FakeRow(r) for r in self.rows])

    def get_table(self, ref):
        return bigquery.Table("p." + ref, schema=[bigquery.SchemaField(c, "STRING") for c in SERIES_TEST_COLUMNS])

    def load_table_from_json(self, rows, table, job_config=None):
        job = FakeJob()
        self.loads.append((rows, table, job_config, job))
        return job


def test_run_stats_reads_the_tvf_and_appends():
    client = FakeClient([SIGNAL])
    assert stats.run_stats(client, D, "st-1", "r9") == 1
    sql, cfg = client.queries[0]
    assert "intelligence_42_core.tvf_series_signal(@d)" in sql
    assert [(p.name, p.type_, p.value) for p in cfg.query_parameters] == [("d", "DATE", D)]
    rows, table, cfg, job = client.loads[0]
    assert table.table_id == "series_test" and table.dataset_id == "intelligence_42_core"
    assert cfg.write_disposition == bigquery.WriteDisposition.WRITE_APPEND
    assert job.done
    assert rows[0]["metric_date"] == D.isoformat() and rows[0]["first_measured"] == day(20).isoformat()
    assert rows[0]["test"] == "none" and "hist" not in rows[0]


def test_run_stats_with_no_series_loads_nothing():
    client = FakeClient([])
    assert stats.run_stats(client, D, "st-1", "r9") == 0
    assert client.loads == []


# v_item_waves


def waves(con, item):
    return duck.query(con, "SELECT w.market, w.wave_start, w.wave_end, w.peak_date, w.peak_posts "
                           "FROM {core}.v_item_waves w WHERE w.item_id = @i ORDER BY w.market, w.wave_start",
                      {"i": item})


def test_waves_split_on_28_quiet_days_and_peak_on_the_busiest_day(con):
    w = World()
    w.daily("wv", 101, 3, **LEGACY)
    w.daily("wv", 100, 9, **LEGACY)
    w.daily("wv", 71, 2, **RANK)                     # 28 quiet days after day(100): a new wave
    w.daily("wv", 43, 4, **RANK)                     # 27 quiet days after day(71): the same wave
    w.daily("wv", 43, 3, **SEARCH)
    w.daily("wv", 43, 2, **{**SEARCH, "platform": "x"})   # search lane on day(43): 5 posts over two platforms
    w.daily("wv", 43, 50, **ANY)                     # '_any' never counts
    w.daily("wv", 40, 5, **RANK)                     # ties day(43): the earlier day is the peak
    w.daily("wv", 43, 1, market="NG", **RANK)
    w.load(con)
    assert waves(con, "wv") == [
        {"market": "NG", "wave_start": day(43), "wave_end": day(43), "peak_date": day(43), "peak_posts": 1},
        {"market": "ZA", "wave_start": day(101), "wave_end": day(100), "peak_date": day(100), "peak_posts": 9},
        {"market": "ZA", "wave_start": day(71), "wave_end": day(40), "peak_date": day(43), "peak_posts": 5},
    ]


def test_waves_leave_out_search_sightings_on_the_items_placebo_days(con):
    w = World()
    w.daily("pw", 10, 9, **SEARCH)                   # the item was a placebo search that day
    w.daily("pw", 10, 1, **RANK)                     # a measured sighting the same day stays
    w.daily("pw", 5, 2, **SEARCH)
    w.daily("pw", 60, 9, **SEARCH)                   # placebo seed in another market: stays
    w.t["core.seed_queue"] += [
        {"seed_date": day(10), "market": "ZA", "item_id": "pw", "kind": "hashtag", "lane": "placebo"},
        {"seed_date": day(60), "market": "NG", "item_id": "pw", "kind": "hashtag", "lane": "placebo"},
        {"seed_date": day(5), "market": "ZA", "item_id": "pw", "kind": "hashtag", "lane": "expansion"},
    ]
    w.load(con)
    assert waves(con, "pw") == [
        {"market": "ZA", "wave_start": day(60), "wave_end": day(60), "peak_date": day(60), "peak_posts": 9},
        {"market": "ZA", "wave_start": day(10), "wave_end": day(5), "peak_date": day(5), "peak_posts": 2},
    ]


# item_state: warm-up states


def test_new_to_42(con):
    w = World()
    fresh(w, "new")
    fresh(w, "old", first=20)                        # first measured 20 days ago
    fresh(w, "flag", flagged=1)                      # only 2 unflagged creators
    w.build(con)
    rows = detect(con)
    r = rows["new"]
    assert (r["state"], r["state_raw"], r["untested"]) == ("new_to_42", "new_to_42", True)
    assert (r["creators3"], r["posts3"], r["novelty"]) == (3, 3, "new")
    assert r["main_series_id"] == "new|ZA|feed_tiktok|p1" and r["main_mu"] is None
    assert r["run_id"] == RUN and r["rule_version"] == RULE
    assert "old" not in rows and "flag" not in rows


def test_on_the_boards_never_from_x_trends(con):
    w = World()
    w.protocol("board_youtube", 9, platform="youtube")
    w.counter("board", "board_youtube", 0, 1.0, platform="youtube", is_board=True)
    fresh(w, "xtr")
    w.counter("xtr", "x_trends", 0, 1.0, platform="x", is_board=True)
    w.build(con)
    rows = detect(con)
    assert rows["board"]["state"] == "on_the_boards" and rows["board"]["untested"] is True
    assert rows["xtr"]["state"] == "new_to_42"


def test_spike_untested_by_top10_twice(con):
    w = World()
    w.protocol("feed_tiktok", 9)
    for item, seqs in (("top", (41, 42)), ("gap", (40, 42))):
        w.counter(item, "feed_tiktok", 1, 1.0)
        w.counter(item, "feed_tiktok", 0, 2.0)
        w.counter(item, "feed_tiktok", 1, 4.0, unit="rank", pull_seq=seqs[0])
        w.counter(item, "feed_tiktok", 0, 6.0, unit="rank", pull_seq=seqs[1])
    fresh(w, "gap")                                  # pulls 40 and 42 are not consecutive
    w.build(con)
    rows = detect(con)
    assert rows["top"]["state"] == "spike" and rows["top"]["untested"] is True
    assert rows["gap"]["state"] == "new_to_42"


def panel_item(w, item, history, today, protocol, creators=3):
    """A hub panel protocol started len(history) days ago; history is posts per day, oldest first."""
    n = len(history)
    w.protocol("panel_fb_hub", n, platform="facebook", lane_class="panel", protocol=protocol, k=1.0)
    for i, v in zip(range(n, 0, -1), history):
        w.daily(item, i, v, protocol=protocol)
    w.daily(item, 0, today, protocol=protocol)
    w.posts(item, 0, [f"{item}-c{j}" for j in range(creators)], lane_class="panel", lane="panel",
            platform="facebook")


def test_spike_untested_by_a_panel_jump(con):
    w = World()
    panel_item(w, "jump", [2] * 5, 8, "p1")
    panel_item(w, "low", [2] * 5, 7, "p2")          # under 8
    panel_item(w, "short", [2] * 4, 8, "p3")        # 4 earlier observed days
    panel_item(w, "flat", [3] * 5, 8, "p4")         # under 3 times the median
    panel_item(w, "thin", [2] * 5, 8, "p5", creators=2)
    w.build(con)
    rows = detect(con)
    r = rows["jump"]
    assert (r["state"], r["untested"], r["main_y"]) == ("spike", True, 8.0)
    for item in ("low", "short", "flat"):
        assert rows[item]["state"] == "new_to_42", item
    assert "thin" not in rows


def test_multi_platform_panel_is_one_series_on_one_platform(con):
    # A culture desk item with posts on two platforms today is one significant series, so it is a Spike,
    # never Rising on two platforms. The test result is stood in for: the copied rows are marked significant.
    w = World()
    w.protocol("panel_culture_desk", 30, platform=None, lane_class="panel", k=1.0)
    for i in range(30, 0, -1):
        w.daily("cd", i, 1, platform="tiktok", series="panel_culture_desk")
    w.daily("cd", 0, 6, platform="tiktok", series="panel_culture_desk")
    w.daily("cd", 0, 4, platform="instagram", series="panel_culture_desk")
    for i in range(3):
        w.posts("cd", i, [f"cd-c{i}{j}" for j in range(3)], lane_class="panel", lane="panel")
    w.load(con)
    signal = duck.query(con, "SELECT * FROM {core}.tvf_series_signal(@d)", {"d": D})
    rows = stats.passthrough_rows(signal, D, STATS_RUN, RULE)
    for r in rows:
        r.update(test="nb", significant=True, ratio=3.0, p_mid=1e-6, q=1e-5, mu=2.0)
    duck.load(con, "core.series_test", rows)
    duck.load(con, "agent.runs", [run("stats", D, STATS_RUN)])
    r = detect(con)["cd"]
    assert (r["state"], r["main_series_id"], r["main_y"]) == ("spike", "cd|ZA|panel_culture_desk|p1", 10.0)
    assert [(r["series_id"], r["platform"], r["y"], r["obs_prior"]) for r in rows] == [
        ("cd|ZA|panel_culture_desk|p1", "tiktok", 10.0, 30)]


def test_main_series_ties_break_on_series_id(con):
    # Two rank lists with the same 3-day level: the main series is the first by series_id, whatever order
    # series_test holds the rows in.
    w = World()
    for protocol in ("p1", "p2"):
        w.protocol("feed_tiktok", 9, protocol=protocol)
        for i in (2, 1, 0):
            w.counter("tie", "feed_tiktok", i, 1.0, protocol=protocol)
    for j in range(3):
        w.posts("tie", j, [f"tie-c{j}"])
    w.load(con)
    signal = duck.query(con, "SELECT * FROM {core}.tvf_series_signal(@d)", {"d": D})
    rows = sorted(stats.passthrough_rows(signal, D, STATS_RUN, RULE), key=lambda r: r["series_id"], reverse=True)
    for r in rows:
        duck.load(con, "core.series_test", [r])
    duck.load(con, "agent.runs", [run("stats", D, STATS_RUN)])
    assert [r["series_id"] for r in rows if r["item_id"] == "tie"][:2] == [
        "tie|ZA|feed_tiktok|p2", "tie|ZA|feed_tiktok|p1"]
    assert detect(con)["tie"]["main_series_id"] == "tie|ZA|feed_tiktok|p1"


def emerging_item(w, item, observed_days, prev_posts, protocol):
    """Five creators with 8 posts in 3 days (top creator 25%) on a feed protocol of observed_days days."""
    w.protocol("feed_tiktok", observed_days - 1, protocol=protocol)
    for i in (2, 1, 0):
        w.counter(item, "feed_tiktok", i, 1.0, protocol=protocol)
    for j, n in enumerate((2, 2, 2, 1, 1)):
        for k in range(n):
            w.posts(item, k, [f"{item}-c{j}"])
    w.posts(item, 4, [f"{item}-prev{j}" for j in range(prev_posts)])


def test_emerging_untested_after_5_observed_days_with_floors(con):
    w = World()
    emerging_item(w, "emerge", 5, 8, "p1")          # posts3 8, posts3_prev 8
    emerging_item(w, "four", 4, 0, "p2")            # only 4 observed days
    emerging_item(w, "falling", 5, 9, "p3")         # posts3 below the previous 3 days
    w.build(con)
    rows = detect(con)
    r = rows["emerge"]
    assert (r["state"], r["untested"], r["novelty"]) == ("emerging", True, "new")
    assert (r["creators3"], r["posts3"], r["top_creator_share3"]) == (5, 8, 0.25)
    assert rows["four"]["state"] == "new_to_42"
    assert rows["falling"]["state"] == "new_to_42"


def moment(offset, market="ZA", item="cal"):
    return {"moment_date": D + timedelta(days=offset), "market": market, "name": "Heritage Day",
            "kind": "holiday", "source": "fixture", "item_ids": [item]}


@pytest.mark.parametrize("offset,state", [(-4, "new_to_42"), (-3, "seasonal"), (14, "seasonal"),
                                          (15, "new_to_42")])
def test_seasonal_from_a_calendar_moment(con, offset, state):
    w = World()
    fresh(w, "cal")
    w.t["core.calendar"] += [moment(offset), moment(0, market="NG")]
    w.build(con)
    r = detect(con)["cal"]
    assert r["state"] == state
    assert r["moment"] == ("Heritage Day" if state == "seasonal" else None)


def test_seasonal_from_a_wave_this_date_last_year(con):
    w = World()
    assert day(365) == date(D.year - 1, D.month, D.day)
    for item, peak_day in (("annual", 360), ("offyear", 355)):
        fresh(w, item)
        w.daily(item, peak_day, 9, **LEGACY)
        for i in (2, 1, 0):
            w.daily(item, i, 1, **RANK)
    w.build(con)
    rows = detect(con)
    assert rows["annual"]["state"] == "seasonal" and rows["annual"]["moment"] is None
    assert rows["offyear"]["state"] == "recurring"


def test_recurring_from_a_legacy_wave_and_a_new_wave(con):
    w = World()
    for item, peak_day, peak in (("rec", 200, 9), ("small", 200, 7), ("stale", 400, 9), ("long", 200, 9)):
        fresh(w, item)
        w.daily(item, peak_day + 1, 3, **LEGACY)
        w.daily(item, peak_day, peak, **LEGACY)
        for i in range(30 if item == "long" else 2, -1, -1):   # 'long': the current wave began 30 days ago
            w.daily(item, i, 1, **RANK)
    w.build(con)
    rows = detect(con)
    r = rows["rec"]
    assert (r["state"], r["novelty"]) == ("recurring", "recurrence")
    assert r["last_wave"] == {"peak_date": day(200), "peak_posts": 9}
    assert rows["small"]["state"] == "new_to_42"     # the legacy peak of 7 is not a wave that counts
    assert "stale" not in rows                        # earlier wave over 365 days ago: neither state applies
    assert "long" not in rows                         # current wave not under 28 days old


def test_base_state_keeps_what_the_seasonal_or_recurring_override_replaced(con):
    """base_state is the state before the Seasonal or Recurring override when that is Rising, Emerging, Spike or
    New to 42, else NULL; it is the last column, after rule_version, where L1's ADD COLUMN puts it."""
    w = World()
    for item in ("cal", "plain", "rec"):
        fresh(w, item)
    w.t["core.calendar"] += [moment(0)]
    w.daily("rec", 201, 3, **LEGACY)                  # an earlier wave peaking 200 days ago, a new one now
    w.daily("rec", 200, 9, **LEGACY)
    for i in (2, 1, 0):
        w.daily("rec", i, 1, **RANK)
    w.build(con)
    rows = detect(con)
    got = {i: (rows[i]["state"], rows[i]["base_state"], rows[i]["rule_version"]) for i in ("cal", "plain", "rec")}
    assert got == {"cal": ("seasonal", "new_to_42", RULE), "plain": ("new_to_42", "new_to_42", RULE),
                   "rec": ("recurring", None, RULE)}   # New to 42 needs no earlier wave, so nothing underneath
    cols = [r[0] for r in con.execute("DESCRIBE core.item_state").fetchall()]
    assert cols[-2:] == ["rule_version", "base_state"]


def test_newly_watched_counter_with_one_big_first_read_is_not_a_spike(con):
    w = World()
    w.protocol("curve_tiktok_hashtag", 9, lane_class="unbiased_counter")
    kw = dict(lane_class="unbiased_counter", unit="delta")
    w.counter("big", "curve_tiktok_hashtag", 1, None, **kw)          # first read: no previous total
    w.counter("big", "curve_tiktok_hashtag", 0, 5000.0, **kw)
    for i in range(5, 0, -1):                                        # a vendor curve carries 5 earlier days
        w.counter("curve", "curve_tiktok_hashtag", i, 10.0, source="vendor_history", read_day=D, **kw)
    w.counter("curve", "curve_tiktok_hashtag", 0, 5000.0, **kw)
    for item in ("big", "curve"):
        w.posts(item, 0, [f"{item}-c{j}" for j in range(3)])
    w.build(con)
    rows = detect(con)
    assert (rows["big"]["state"], rows["big"]["main_y"]) == ("new_to_42", 5000.0)
    assert rows["curve"]["state"] == "spike"


@pytest.mark.parametrize("yesterday,raw_yesterday,state", [
    ("emerging", "emerging", "emerging"),            # shown higher yesterday: kept one more day
    ("emerging", "new_to_42", "new_to_42"),          # already held once yesterday: steps down
    ("on_the_boards", "on_the_boards", "new_to_42"),  # a step up is immediate
])
def test_two_day_hysteresis(con, yesterday, raw_yesterday, state):
    w = World()
    fresh(w, "hy")
    w.t["core.item_state"].append({"metric_date": day(1), "market": "ZA", "item_id": "hy", "kind": "hashtag",
                                   "state": yesterday, "state_raw": raw_yesterday, "run_id": rid("detect", day(1))})
    w.days["detect"].add(day(1))
    w.build(con)
    r = detect(con)["hy"]
    assert (r["state"], r["state_raw"]) == (state, "new_to_42")


@pytest.mark.parametrize("posts,coaction,authenticity", [
    (30, False, "not_assessed"), (30, True, "clear"), (29, True, "not_assessed")])
def test_authenticity_not_assessed_without_a_coaction_run(con, posts, coaction, authenticity):
    w = World()
    w.protocol("feed_tiktok", 9)
    for i in (2, 1, 0):
        w.counter("au", "feed_tiktok", i, 1.0)
    for j in range(posts):                           # 10 creators, 3 posts each, 7 days, no shared bucket
        w.posts("au", j % 7, [f"au-c{j % 10}"])
    if coaction:
        w.days["coaction"].add(D)
    w.build(con)
    r = detect(con)["au"]
    assert r["authenticity"] == authenticity and list(r["share_flags"]) == []


@pytest.mark.parametrize("known,local,status", [(7, 7, "market_unconfirmed"), (10, 5, "not_local"),
                                                (10, 6, "local")])
def test_geo_status(con, known, local, status):
    w = World()
    fresh(w, "geo")
    w.daily("geo", 8, 100, geo_known_posts=100, local_posts=0, **ANY)   # outside the 7 days
    w.daily("geo", 3, 2, geo_known_posts=2, local_posts=2, **ANY)
    w.daily("geo", 0, 20, geo_known_posts=known - 2, local_posts=local - 2, **ANY)
    w.build(con)
    r = detect(con)["geo"]
    assert (r["geo_status"], r["geo_known_posts7"]) == (status, known)
    assert r["local_share"] == pytest.approx(local / known)


def test_item_seen_only_in_search_lanes_gets_no_state(con):
    w = World()
    fresh(w, "seen")
    for i in (2, 1, 0):
        w.daily("srch", i, 5, **SEARCH)
    w.posts("srch", 0, [f"srch-c{j}" for j in range(5)], lane_class="search_presence", lane="expansion",
            platform="instagram")
    w.build(con)
    rows = detect(con)
    assert rows["seen"]["state"] == "new_to_42"
    assert "srch" not in rows
    assert duck.query(con, "SELECT st.series_id FROM {core}.series_test st WHERE st.item_id = 'srch'") == []


def test_with_no_state_row_for_yesterday_nothing_is_held_so_the_day_after_a_partial_day_shows_the_raw_state(con):
    # The day after a partial understand day, topic items have no item_state row for D-1 (they were left out), so
    # state_yesterday is NULL and the two-day hold does not apply: a topic item may step down a day early. state.sql
    # is DATA.md 3.7 verbatim, and this pins that behaviour as it stands (lead ruling: keep it). It heals itself
    # the next day.
    w = World()
    fresh(w, "hy")
    w.build(con)
    r = detect(con)["hy"]
    assert (r["state"], r["state_raw"]) == ("new_to_42", "new_to_42")
