"""Known-answer tests for the news to social bridge and its detect views.

News candidate rows share a seed_queue shape across GDELT and local headline sources. Without a producer marker
and source date, these views cannot identify which source produced a row or infer a news date. Tests use the real
items.canonical_key and items.item_id to build distinct seed and social identities. Collect yield rows carry the
run day as seed_date. First sightings count measured lanes only, never a news outlet's post.
"""

import os
import re
from datetime import datetime

import pytest
import sqlglot

from .. import job, sqlrun
from ..items import canonical_key, item_id
from . import duck
from . import test_detect_job as jobtests
from .fixtures import D, day, health, obs, post, run

SEED_COLUMNS = [("seed_queue", "priority", "DOUBLE"), ("seed_queue", "template", "VARCHAR"),
                ("seed_queue", "ttl_days", "BIGINT"), ("seed_queue", "credits_estimate", "DOUBLE"),
                ("seed_queue", "yield_posts", "BIGINT"), ("seed_queue", "yield_new_creators", "BIGINT"),
                ("clusters", "label", "VARCHAR"), ("clusters", "keywords", "VARCHAR[]")]
NEWS_OBJECTS = ["intelligence_42_agent.v_news_bridge", "intelligence_42_agent.v_item_first_sighting",
                "intelligence_42_agent.v_news_followthrough", "intelligence_42_agent.v_item_origin"]
SERIES = "tiktok_local_feed"


def add_columns(con):
    for table, col, typ in SEED_COLUMNS:
        con.execute(f"ALTER TABLE core.{table} ADD COLUMN IF NOT EXISTS {col} {typ}")


@pytest.fixture
def con():
    c = duck.connect()
    add_columns(c)
    for stmt in sqlrun.news_statements("core", "agent"):
        c.execute(duck.create_statement(stmt))
    yield c
    c.close()


def iid(kind, raw):
    return item_id(kind, canonical_key(kind, raw))


def gdelt_seed(name, seed_date, market="ZA", kind="topic", lane="expansion", priority=3.0):
    """The row gdelt._seed writes: query is the canonical key, item_id the topic or brand id."""
    key = canonical_key(kind, name)
    return {"seed_date": seed_date, "market": market, "item_id": item_id(kind, key), "query": key, "kind": kind,
            "lane": lane, "priority": priority, "template": "search/multi", "ttl_days": 3, "credits_estimate": 5.0}


def yield_row(seed, run_day, posts, creators, lane=None):
    """Collect's yield row (core/collect/seeds.py yield_rows): the run day as seed_date, ttl 0."""
    return {**seed, "seed_date": run_day, "lane": lane or seed["lane"], "ttl_days": 0,
            "yield_posts": posts, "yield_new_creators": creators}


def dated(row):
    """sqlglot turns BigQuery's DATE_SUB on a DATE into a DuckDB TIMESTAMP; BigQuery returns a DATE."""
    return {k: v.date() if isinstance(v, datetime) else v for k, v in row.items()}


class World:
    def __init__(self):
        self.n = 0
        self.rows = {"core.seed_queue": [], "core.post_observations": [], "core.post_items": [], "core.posts": [],
                     "core.item_state": [], "core.cultural_map": [], "core.collection_health": [],
                     "core.clusters": [], "agent.runs": []}
        self.detect_days = set()

    def seed(self, *rows):
        self.rows["core.seed_queue"] += rows
        return rows[0]

    def gdelt_every_day(self, first, last, market="ZA"):
        """GDELT seeding the market on every seed day from day(first) to day(last), for an unrelated entity."""
        for i in range(first, last - 1, -1):
            self.seed(gdelt_seed(f"filler {i}", day(i), market=market))

    def hashtag(self, raw):
        key = canonical_key("hashtag", raw)
        self.rows["core.cultural_map"].append({"item_id": item_id("hashtag", key), "kind": "hashtag",
                                               "canonical_key": key, "label": "#" + raw.lstrip("#"), "valid_to": None})
        return item_id("hashtag", key)

    def mapped(self, kind, raw, label):
        key = canonical_key(kind, raw, "tiktok" if kind in ("sound", "creator") else None)
        self.rows["core.cultural_map"].append({"item_id": item_id(kind, key), "kind": kind, "canonical_key": key,
                                               "label": label, "valid_to": None})
        return item_id(kind, key)

    def collecting(self, since, market="ZA", series=SERIES):
        self.rows["core.collection_health"].append(health(series, since, market=market))

    def sight(self, item, d, lane_class="unbiased_rank", lane="sweep", market="ZA", series=SERIES,
              creator="someone", platform="tiktok"):
        self.n += 1
        pid = f"p{self.n}"
        self.rows["core.post_observations"].append(obs(pid, d, lane_class, lane, market=market, series=series,
                                                       platform=platform))
        self.rows["core.post_items"].append({"post_id": pid, "item_id": item, "via": "hashtag"})
        self.rows["core.posts"].append(post(pid, creator, d, platform=platform))

    def state(self, item, d, state, market="ZA"):
        self.detect_days.add(d)
        self.rows["core.item_state"].append({"metric_date": d, "market": market, "item_id": item,
                                             "state": state, "run_id": f"detect-{d:%Y%m%d}"})

    def load(self, con):
        self.rows["agent.runs"] = [run("detect", d) for d in sorted(self.detect_days)]
        for table, rows in self.rows.items():
            duck.load(con, table, rows)


def bridge(con):
    rows = duck.query(con, "SELECT b.seed_date, b.market, b.seed_item_id, b.item_id, b.matched_by "
                           "FROM {agent}.v_news_bridge b")
    return {(r["seed_item_id"], r["item_id"]): r["matched_by"] for r in rows}


def bridge_by_market(con):
    rows = duck.query(con, "SELECT b.market, b.seed_item_id, b.item_id, b.matched_by "
                           "FROM {agent}.v_news_bridge b")
    return {(r["market"], r["seed_item_id"], r["item_id"]): r["matched_by"] for r in rows}


def followthrough(con):
    rows = duck.query(con, "SELECT * FROM {agent}.v_news_followthrough f")
    return {(r["market"], r["item_id"], dated(r)["seed_date"]): dated(r) for r in rows}


def origins(con):
    rows = duck.query(con, "SELECT * FROM {agent}.v_item_origin o")
    return {(r["market"], r["item_id"]): dated(r) for r in rows}


LOAD = gdelt_seed("Load Shedding", day(10))


# The SQL file


def test_news_sql_is_four_bigquery_views_in_the_agent_dataset_that_only_read():
    stmts = sqlrun.news_statements()
    assert [sqlrun.object_name(s) for s in stmts] == NEWS_OBJECTS
    for stmt in stmts:
        assert sqlglot.parse_one(duck.split_create(stmt)[3], read="bigquery") is not None
        upper = stmt.upper()
        for word in ("INSERT", "MERGE", "DELETE", "DROP", "TRUNCATE", "UPDATE", "ALTER"):
            assert word not in upper
        assert not re.search(r"\{[a-z_]+\}", stmt)                      # every placeholder filled
    assert not set(NEWS_OBJECTS) & {sqlrun.object_name(s) for s in sqlrun.statements() + sqlrun.agent_statements()}


def test_the_news_outlets_are_the_hub_accounts_of_kind_news():
    handles = sqlrun.news_outlet_handles()
    assert {"sundaytimesza", "iol", "standardkenya"} <= set(handles)
    assert "tyla" not in handles                                       # culture desk, kind music
    assert handles == sorted(set(handles)) and all(h == h.lower() for h in handles)


def test_apply_news_creates_the_views_in_order_and_returns_their_names():
    con = duck.connect()
    add_columns(con)
    names = sqlrun.apply_news(jobtests.JobClient(con), "core", "agent")
    assert names == [n.replace("intelligence_42_agent", "agent") for n in NEWS_OBJECTS]
    for name in names:
        assert duck.query(con, f"SELECT COUNT(*) n FROM {name} v") == [{"n": 0}]
    con.close()


# v_news_bridge


def test_topic_seed_id_bridges_to_distinct_hashtag_and_topic_ids(con):
    w = World()
    seed = w.seed(LOAD)
    tag = w.hashtag("#LoadShedding")
    underscored = w.hashtag("#load_shedding")
    longer = w.hashtag("#LoadSheddingZA")
    topic = w.mapped("topic", "load-shedding crisis", "Load-shedding")          # an L3 cluster topic
    clustered = w.mapped("topic", "power cuts", "Power cuts")
    w.rows["core.clusters"].append({"cluster_date": day(12), "cluster_id": "c1", "market": "pan",
                                    "item_id": clustered, "label": "Power cuts", "keywords": ["Eskom", "load shedding"]})
    w.rows["core.clusters"].append({"cluster_date": day(12), "cluster_id": "c2", "market": "NG",
                                    "item_id": iid("topic", "ng only"), "label": "NG", "keywords": ["loadshedding"]})
    w.load(con)
    assert seed["item_id"] != tag                                     # the reviewer's probe: the ids never meet
    got = bridge(con)
    assert got == {(seed["item_id"], seed["item_id"]): "seed", (seed["item_id"], tag): "hashtag_key",
                   (seed["item_id"], underscored): "hashtag_key", (seed["item_id"], topic): "label",
                   (seed["item_id"], clustered): "cluster_keyword"}
    assert (seed["item_id"], longer) not in got


def test_brand_seed_bridges_to_distinct_social_ids_and_respects_cluster_market(con):
    w = World()
    za = gdelt_seed("Cape Town Film Festival", day(10), market="ZA", kind="brand")
    ng = gdelt_seed("Cape Town Film Festival", day(10), market="NG", kind="brand")
    w.seed(za, ng)
    tag = w.hashtag("#Cape_Town_Film_Festival")
    topic = w.mapped("topic", "CapeTownFilmFestival", "CAPE TOWN-FILM FESTIVAL")
    ng_only = iid("topic", "ng only")
    w.rows["core.clusters"].append({"cluster_date": day(12), "cluster_id": "ng-c1", "market": "NG",
                                    "item_id": ng_only, "label": "NG only", "keywords": ["Cape Town Film Festival"]})
    w.load(con)
    got = bridge_by_market(con)
    assert za["item_id"] == ng["item_id"]
    assert za["item_id"] != tag and za["item_id"] != topic
    assert got[("ZA", za["item_id"], tag)] == "hashtag_key"
    assert got[("ZA", za["item_id"], topic)] == "label"
    assert got[("NG", ng["item_id"], tag)] == "hashtag_key"
    assert got[("NG", ng["item_id"], topic)] == "label"
    assert ("ZA", za["item_id"], ng_only) not in got
    assert got[("NG", ng["item_id"], ng_only)] == "cluster_keyword"


# v_news_followthrough


def test_followthrough_keeps_source_dates_and_lag_unknown(con):
    w = World()
    seed = w.seed(LOAD)
    w.seed(yield_row(seed, day(10), 12, 7), yield_row(seed, day(9), 3, 1))
    tag = w.hashtag("#LoadShedding")
    w.collecting(day(30))
    w.sight(tag, day(9))
    w.load(con)
    r = followthrough(con)[("ZA", seed["item_id"], day(10))]
    assert (r["news_day"], r["news_day_from"]) == (None, None)
    assert (r["seed_date"], r["label"], r["kind"], r["lane"], r["rise_score"]) == (
        day(10), "load shedding", "topic", "expansion", 3.0)
    assert sorted(m["item_id"] for m in r["matched_items"]) == sorted([seed["item_id"], tag])
    assert (r["collect_ran"], r["collect_runs"], r["yield_posts"], r["yield_new_creators"]) == (True, 2, 12, 7)
    assert (r["reached_state"], list(r["states_7d"]), r["first_state_day"]) == (False, [], None)
    assert (r["first_measured"], r["lag_days"]) == (day(9), None)


def test_followthrough_state_window_starts_on_seed_day_and_includes_day_six(con):
    w = World()
    seed = w.seed(LOAD)
    tag = w.hashtag("#LoadShedding")
    w.state(tag, day(12), "before_window")
    w.state(tag, day(11), "before_seed_day")
    w.state(tag, day(10), "on_seed_day")
    w.state(tag, day(4), "on_day_six")
    w.state(tag, day(3), "after_day_six")
    w.load(con)
    r = followthrough(con)[("ZA", seed["item_id"], day(10))]
    assert (list(r["states_7d"]), r["first_state_day"]) == (["on_seed_day", "on_day_six"], day(10))


def test_collect_ran_needs_a_yield_row_in_the_seeds_lane_market_and_live_days(con):
    w = World()
    seed = w.seed(gdelt_seed("Acme", day(5), kind="brand"))
    w.seed(yield_row(seed, day(5), 9, 4, lane="exploration"))          # another lane
    w.seed(yield_row({**seed, "market": "NG"}, day(5), 9, 4))           # another market
    w.seed(yield_row(seed, day(2), 9, 4))                              # after the seed's 3 live days
    w.seed(yield_row(gdelt_seed("Other", day(5)), day(5), 9, 4))       # another item
    w.load(con)
    r = followthrough(con)[("ZA", seed["item_id"], day(5))]
    assert (r["label"], r["kind"], r["collect_ran"], r["collect_runs"]) == ("acme", "brand", False, 0)
    assert (r["yield_posts"], r["yield_new_creators"]) == (None, None)
    assert (r["reached_state"], list(r["states_7d"]), r["first_state_day"]) == (False, [], None)
    assert (r["first_measured"], r["lag_days"]) == (None, None)


def test_placebo_search_only_agent_live_and_news_outlet_sightings_never_count(con):
    w = World()
    seed = w.seed(LOAD)
    tag = w.hashtag("#LoadShedding")
    w.collecting(day(30))
    w.collecting(day(30), series="panel_culture_desk")
    w.sight(tag, day(10), lane_class="search_presence", lane="expansion")     # the seed's own search
    w.sight(tag, day(9), lane="placebo")
    w.sight(tag, day(9), lane="agent_live")
    w.sight(tag, day(9), lane_class="watchlist", lane="watchlist")
    w.sight(tag, day(9), lane_class="panel", lane="panel", series="panel_culture_desk", creator="SundayTimesZA",
            platform="twitter")                                              # a hub news outlet
    w.sight(tag, day(9), platform="news", creator="somepaper")                # a news page, not social
    w.load(con)
    r = followthrough(con)[("ZA", seed["item_id"], day(10))]
    assert (r["news_day"], r["news_day_from"], r["first_measured"], r["lag_days"]) == (None, None, None, None)
    assert ("ZA", tag) not in origins(con)
    w2 = World()
    w2.n = 100                                                        # post ids apart from w
    w2.sight(tag, day(7), lane_class="panel", lane="panel", series="panel_culture_desk", creator="SundayTimesZA",
             platform="twitter")
    w2.sight(tag, day(6), lane_class="panel", lane="panel", series="panel_culture_desk", creator="casspernyovest",
             platform="twitter")
    w2.state(tag, day(6), "spike")
    w2.load(con)
    r = followthrough(con)[("ZA", seed["item_id"], day(10))]
    assert (r["news_day"], r["news_day_from"], r["first_measured"], r["lag_days"]) == (None, None, day(6), None)
    assert list(r["states_7d"]) == ["spike"]
    assert origins(con)[("ZA", tag)]["origin"] is None


def test_news_candidate_seed_shape_excludes_detect_and_yield_rows(con):
    w = World()
    g = w.seed(gdelt_seed("g", day(6)))
    x = w.seed(gdelt_seed("x", day(6), lane="exploration", priority=1.2))
    local_shape = w.seed(gdelt_seed("local headline", day(6)))  # seed_queue has no source marker to distinguish it
    w.seed({**gdelt_seed("d1", day(6)), "ttl_days": 1},                        # detect's own expansion
           {**gdelt_seed("d2", day(6)), "template": "tiktok/search/hashtag"},
           {**gdelt_seed("d3", day(6)), "lane": "placebo"},
           {**gdelt_seed("d4", day(6)), "lane": "anchor"},
           {**gdelt_seed("d5", day(6)), "credits_estimate": None},
           yield_row(gdelt_seed("d6", day(6)), day(6), 4, 2))
    w.load(con)
    got = followthrough(con)
    assert sorted(k[1] for k in got) == sorted([g["item_id"], x["item_id"], local_shape["item_id"]])
    assert got[("ZA", x["item_id"], day(6))]["lane"] == "exploration"


def test_only_states_of_the_current_detect_run_count(con):
    w = World()
    seed = w.seed(LOAD)
    tag = w.hashtag("#LoadShedding")
    w.state(tag, day(8), "rising")
    w.load(con)
    duck.load(con, "core.item_state", [{"metric_date": day(7), "market": "ZA", "item_id": tag,
                                        "state": "spike", "run_id": "detect-stale"}])
    assert list(followthrough(con)[("ZA", seed["item_id"], day(10))]["states_7d"]) == ["rising"]


# v_item_origin


def test_an_unrelated_item_stays_null_rather_than_native(con):
    w = World()
    w.gdelt_every_day(16, 3)                                          # GDELT seeded ZA every day
    w.seed(LOAD)
    sound = w.mapped("sound", "7301", "7301")                         # a sound id names no news
    w.collecting(day(30))
    w.sight(sound, day(12))
    w.state(sound, day(10), "rising")
    unmapped = iid("hashtag", "#nomap")                               # no cultural_map row to fold
    w.sight(unmapped, day(12))
    w.state(unmapped, day(10), "rising")
    ke = w.hashtag("#nairobirains")                                   # KE: GDELT seeded only some days
    w.seed(gdelt_seed("filler ke", day(12), market="KE"))
    w.sight(ke, day(12), market="KE")
    w.state(ke, day(10), "rising", market="KE")
    quiet = w.hashtag("#quiet")                                       # sighted, never held a state
    w.sight(quiet, day(12))
    w.load(con)
    got = origins(con)
    assert got[("ZA", sound)]["origin"] is None
    assert got[("ZA", unmapped)]["origin"] is None
    assert got[("KE", ke)]["origin"] is None
    assert got[("ZA", quiet)]["origin"] is None


def test_coverage_and_nameability_do_not_assign_native_origin(con):
    w = World()
    w.gdelt_every_day(16, 3)
    w.seed(LOAD)                                                      # news that names another thing
    tag = w.hashtag("#AmapianoFest")
    w.collecting(day(30))
    w.sight(tag, day(12))
    w.state(tag, day(10), "rising")
    w.seed(gdelt_seed("Amapiano Fest", day(10), market="NG"))         # news in another market
    w.load(con)
    o = origins(con)[("ZA", tag)]
    assert (o["origin"], o["lead_news_day"], o["first_measured"], o["first_state_day"]) == (
        None, None, day(12), day(10))
    assert o["lag_days"] is None


def test_a_pre_collection_sighting_does_not_count(con):
    w = World()
    seed = w.seed(LOAD)
    early = w.hashtag("#LoadShedding")
    w.collecting(day(9))                                              # the series began the day it saw the item
    w.sight(early, day(9))
    w.state(early, day(8), "emerging")
    before = w.hashtag("#load_shedding_now")                          # folds to another key: its own seed below
    w.seed(gdelt_seed("load shedding now", day(10)))
    w.sight(before, day(9), series="legacy_feed")                     # a series with no collection record
    w.state(before, day(8), "emerging")
    w.load(con)
    got = origins(con)
    assert (got[("ZA", early)]["origin"], got[("ZA", early)]["after_collection_began"]) == (None, False)
    assert (got[("ZA", before)]["origin"], got[("ZA", before)]["after_collection_began"]) == (None, False)
    r = followthrough(con)[("ZA", seed["item_id"], day(10))]
    assert (r["news_day"], r["news_day_from"], r["first_measured"], r["lag_days"]) == (None, None, day(9), None)


def test_candidate_seed_timing_does_not_infer_origin_or_lag(con):
    w = World()
    w.seed(LOAD)                                                      # news day day(11) or day(12)
    same = w.hashtag("#LoadShedding")
    w.seed(gdelt_seed("Eskom", day(10)))
    next_day = w.hashtag("#Eskom")
    w.collecting(day(30))
    w.sight(same, day(11))
    w.state(same, day(10), "emerging")
    w.sight(next_day, day(10))
    w.state(next_day, day(9), "emerging")
    w.load(con)
    got = origins(con)
    assert (got[("ZA", same)]["origin"], got[("ZA", same)]["lead_news_day"], got[("ZA", same)]["lag_days"]) == (
        None, None, None)
    assert (got[("ZA", next_day)]["origin"], got[("ZA", next_day)]["lead_news_day"],
            got[("ZA", next_day)]["lag_days"]) == (None, None, None)
    assert (got[("ZA", same)]["first_measured"], got[("ZA", next_day)]["first_measured"]) == (day(11), day(10))


# The detect step


def job_con():
    con = duck.connect()
    add_columns(con)
    con.execute(jobtests.LEDGER)
    for table, col, typ in jobtests.SEED_COLUMNS:
        con.execute(f"ALTER TABLE core.{table} ADD COLUMN IF NOT EXISTS {col} {typ}")
    jobtests.world(con)
    return con


def test_the_news_step_runs_in_detect_and_its_views_read():
    con = job_con()
    counts = job.run(jobtests.JobClient(con), D, chain=jobtests.FakeChain(con), core="core", agent="agent")
    assert counts["news"] == {"status": "ok"}
    for name in NEWS_OBJECTS:
        con.execute(f"SELECT * FROM {name.replace('intelligence_42_agent', 'agent')}").fetchall()
    con.close()


def test_a_news_step_failure_is_recorded_and_detect_carries_on(monkeypatch, capsys):
    con = job_con()

    def boom(*a, **k):
        raise RuntimeError("news view failed to build")

    monkeypatch.setattr(sqlrun, "apply_news", boom)
    chain = jobtests.FakeChain(con)
    counts = job.run(jobtests.JobClient(con), D, chain=chain, core="core", agent="agent")
    assert counts["news"] == {"status": "failed", "error": "RuntimeError: news view failed to build"}
    assert counts["item_state"] == 2 and set(counts["seeds"]) >= {"appended"}
    _, _, status, finish_counts, error = chain.of("finish")[0]
    assert status == "ok" and error is None and finish_counts["news"] == counts["news"]
    assert "news views failed: RuntimeError: news view failed to build" in capsys.readouterr().err
    con.close()


# BigQuery dry run on staging (F42_BQ=1): parse, resolve names and plan; nothing is created or billed. A view that
# reads another news view gets that view's body inlined, since the news views do not exist on staging yet.


def inlined(stmt):
    body = duck.split_create(stmt)[3]
    for other in sqlrun.news_statements():
        name = sqlrun.object_name(other)
        if name != sqlrun.object_name(stmt):
            body = body.replace(name, f"({duck.split_create(other)[3]})")
    return body


@pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")
@pytest.mark.parametrize("name", [n.split(".")[1] for n in NEWS_OBJECTS])
def test_bigquery_dry_run(name):
    from google.cloud import bigquery

    from .test_detect_bigquery import PROJECT, dry_run

    stmt = next(s for s in sqlrun.news_statements() if sqlrun.object_name(s).endswith("." + name))
    job_ = dry_run(bigquery.Client(project=PROJECT), inlined(stmt))
    print(name, job_.total_bytes_processed)
    assert job_.total_bytes_processed is not None
