"""Known-answer tests for the aggregate step (DATA.md section 3.4, first paragraph) and two spec fixes in views.sql.

The BigQuery path of aggregate.py runs here against DuckDB through duck.Client, which takes the
google-cloud-bigquery query parameters run_aggregate builds and runs each statement with duck.query.
"""

import pytest

from .. import aggregate
from ..items import canonical_key, item_id
from . import duck
from .fixtures import D, at, cmap, counter, creator, day, health, obs, post, run

AMAPIANO = item_id("hashtag", "amapiano")


@pytest.fixture
def con():
    c = duck.connect()
    yield c
    c.close()


# Spec fixes in views.sql (they live here because this task may only add this test file)


def test_negative_counter_v3_gives_null_vel_instead_of_an_error(con):
    days = [day(i) for i in range(5, -1, -1)]
    duck.load(con, "agent.runs", [run("collect", d) for d in days])
    duck.load(con, "core.collection_health", [
        health("counter_tiktok_hashtag", d, market="GLOBAL", lane_class="unbiased_counter") for d in days])
    duck.load(con, "core.cultural_map", [cmap("itN")])
    kw = dict(market="GLOBAL", lane_class="unbiased_counter", unit="delta")
    duck.load(con, "core.item_counter_daily",
              [counter("itN", "counter_tiktok_hashtag", d, 4.0, **kw) for d in days[:3]]
              + [counter("itN", "counter_tiktok_hashtag", d, -9.0, **kw) for d in days[3:]])
    s = duck.query(con, "SELECT * FROM {core}.tvf_series_signal(@d) s", {"d": D})
    assert len(s) == 1
    assert s[0]["v3"] == pytest.approx(-9.0)
    assert s[0]["vel"] is None and s[0]["accel"] is None


def test_sighting_with_null_lane_counts_in_the_item_window(con):
    duck.load(con, "core.post_observations", [obs("n1", D, "unbiased_rank", None), obs("n2", D, "panel", None)])
    duck.load(con, "core.posts", [post("n1", "c1", D), post("n2", "c2", D)])
    duck.load(con, "core.post_items", [{"post_id": p, "item_id": "itL", "via": "hashtag"} for p in ("n1", "n2")])
    w = duck.query(con, "SELECT * FROM {core}.tvf_item_window(@d)", {"d": D})
    assert len(w) == 1
    assert w[0]["posts3"] == 2 and w[0]["creators3"] == 2 and w[0]["posts7"] == 2


# items_rows and the cultural_map MERGE


def test_items_rows_itemises_raw_fields_once_per_post_and_item():
    posts = [
        {"post_id": "p1", "platform": "tiktok", "hashtags": ["#Amapiano", "amapiano", "#Gqom"],
         "sound_id": "777", "creator_id": "c1", "market": "ZA", "first_day": D},
        {"post_id": "p2", "platform": "tiktok", "hashtags": ["#AMAPIANO"], "sound_id": None,
         "creator_id": "c2", "market": "NG", "first_day": day(1)},
    ]
    post_items, items = aggregate.items_rows(posts)
    assert sorted((r["post_id"], r["item_id"], r["via"]) for r in post_items) == sorted([
        ("p1", AMAPIANO, "hashtag"), ("p1", item_id("hashtag", "gqom"), "hashtag"),
        ("p1", item_id("sound", canonical_key("sound", "777", "tiktok")), "sound"),
        ("p1", item_id("creator", canonical_key("creator", "c1", "tiktok")), "creator"),
        ("p2", AMAPIANO, "hashtag"),
        ("p2", item_id("creator", canonical_key("creator", "c2", "tiktok")), "creator"),
    ])
    amap = {r["item_id"]: r for r in items}[AMAPIANO]
    assert amap == {"item_id": AMAPIANO, "kind": "hashtag", "canonical_key": "amapiano", "label": "#AMAPIANO",
                    "first_seen": day(1), "first_seen_market": "NG", "first_seen_platform": "tiktok",
                    "last_seen": D, "status": "active"}
    assert len(items) == 5


def test_cultural_map_merge_inserts_new_items_and_only_moves_last_seen(con):
    old = cmap(AMAPIANO)
    old.update(first_seen=day(30), first_seen_market="KE", first_seen_platform="x", last_seen=day(9),
               label="#amapiano", status="rejected")
    closed = cmap("gone")
    closed.update(last_seen=day(40), valid_to=at(day(20)))
    duck.load(con, "core.cultural_map", [old, closed])
    items = [
        {"item_id": AMAPIANO, "kind": "hashtag", "canonical_key": "amapiano", "label": "#Amapiano",
         "first_seen": D, "first_seen_market": "ZA", "first_seen_platform": "tiktok", "last_seen": D,
         "status": "active"},
        {"item_id": "new", "kind": "hashtag", "canonical_key": "new", "label": "#new",
         "first_seen": day(1), "first_seen_market": "ZA", "first_seen_platform": "tiktok", "last_seen": D,
         "status": "active"},
        {"item_id": "gone", "kind": "hashtag", "canonical_key": "gone", "label": "#gone",
         "first_seen": D, "first_seen_market": "ZA", "first_seen_platform": "tiktok", "last_seen": D,
         "status": "active"},
    ]
    duck.query(con, aggregate.cultural_map_merge_sql(), {"items": items})
    rows = {r["item_id"]: r for r in duck.query(con, "SELECT * FROM {core}.cultural_map c")}
    assert len(rows) == 3
    a = rows[AMAPIANO]
    assert (a["first_seen"], a["first_seen_market"], a["first_seen_platform"], a["label"], a["status"]) == (
        day(30), "KE", "x", "#amapiano", "rejected")
    assert a["last_seen"] == D
    n = rows["new"]
    assert (n["kind"], n["canonical_key"], n["label"], n["first_seen"], n["first_seen_market"],
            n["first_seen_platform"], n["last_seen"], n["status"]) == (
        "hashtag", "new", "#new", day(1), "ZA", "tiktok", D, "active")
    assert n["valid_from"] is not None and n["valid_to"] is None
    assert rows["gone"]["last_seen"] == day(40)      # a closed version is never reopened or moved


FYP = item_id("hashtag", "fyp")


def test_items_rows_marks_stoplisted_hashtags_generic_and_everything_else_active():
    posts = [{"post_id": "p1", "platform": "tiktok", "hashtags": ["#FYP", "＃ｆｏｒｙｏｕ", "#Amapiano"],
              "sound_id": "777", "creator_id": "fyp", "market": "ZA", "first_day": D}]
    _, items = aggregate.items_rows(posts)
    status = {(r["kind"], r["canonical_key"]): r["status"] for r in items}
    assert status == {("hashtag", "fyp"): "generic", ("hashtag", "foryou"): "generic",
                      ("hashtag", "amapiano"): "active", ("sound", "tiktok:777"): "active",
                      ("creator", "tiktok:fyp"): "active"}


def test_items_rows_gives_an_age_hashtag_no_cultural_map_row_as_collect_does():
    """Rule 1: the label collect's cultural_map_rows refuses (gdelt.blocked) is not restored here as an active item."""
    from core.collect.gdelt import blocked

    posts = [{"post_id": "p1", "platform": "tiktok", "hashtags": ["#GenZ", "#Amapiano", "#BurnaBoy"], "sound_id": None,
              "creator_id": None, "market": "ZA", "first_day": D}]
    assert blocked("#GenZ") and not blocked("#Amapiano") and not blocked("#BurnaBoy")  # a named artist passes
    post_items, items = aggregate.items_rows(posts)
    assert [r["label"] for r in items] == ["#Amapiano", "#BurnaBoy"]
    assert {r["item_id"] for r in post_items} == {item_id("hashtag", "genz"), AMAPIANO,
                                                  item_id("hashtag", "burnaboy")}


def test_items_rows_refuses_a_blocked_creator_or_sound_label_for_every_kind_as_collect_does():
    """N16: collect refuses a blocked label for every item kind, so a creator handle or a sound id that gdelt.blocked
    flags gets no cultural_map row here either. The post_items link is kept, as it is for a refused hashtag."""
    from core.collect.gdelt import blocked

    posts = [{"post_id": "p1", "platform": "tiktok", "hashtags": ["#Amapiano"], "sound_id": "teen_vibes",
              "creator_id": "genzcomedy_ke", "market": "KE", "first_day": D},
             {"post_id": "p2", "platform": "tiktok", "hashtags": [], "sound_id": None,
              "creator_id": "studentlife_za", "market": "ZA", "first_day": D}]
    assert blocked("genzcomedy_ke") and blocked("teen_vibes") and blocked("studentlife_za")
    post_items, items = aggregate.items_rows(posts)
    assert [(r["kind"], r["label"]) for r in items] == [("hashtag", "#Amapiano")]
    assert {r["item_id"] for r in post_items} == {
        AMAPIANO, item_id("creator", "tiktok:genzcomedy_ke"), item_id("sound", "tiktok:teen_vibes"),
        item_id("creator", "tiktok:studentlife_za")}


def test_items_rows_keeps_an_unblocked_creator_and_sound():
    posts = [{"post_id": "p1", "platform": "tiktok", "hashtags": [], "sound_id": "777", "creator_id": "mzansi_dancer",
              "market": "ZA", "first_day": D}]
    _, items = aggregate.items_rows(posts)
    assert {(r["kind"], r["label"]) for r in items} == {("sound", "777"), ("creator", "mzansi_dancer")}


def test_cultural_map_merge_status_follows_the_stoplist_on_open_active_or_generic_rows_only(con):
    def row(iid, status, valid_to=None):
        r = cmap(iid)
        r.update(status=status, last_seen=day(9), valid_to=valid_to)
        return r

    duck.load(con, "core.cultural_map", [
        row(FYP, "active"), row("rej", "rejected"), row("gen", "generic"), row("closed", "active", at(day(20))),
        row("keep", "active"), row("nullst", None)])
    src = {FYP: "generic", "rej": "generic", "gen": "active", "closed": "generic", "keep": "active",
           "nullst": "generic", "newgen": "generic", "newact": "active"}
    items = [{"item_id": i, "kind": "hashtag", "canonical_key": i, "label": "#" + i, "first_seen": D,
              "first_seen_market": "ZA", "first_seen_platform": "tiktok", "last_seen": D, "status": st}
             for i, st in src.items()]
    duck.query(con, aggregate.cultural_map_merge_sql(), {"items": items})
    rows = duck.query(con, "SELECT * FROM {core}.cultural_map c")
    assert len(rows) == 8
    by_id = {r["item_id"]: r for r in rows}
    assert {i: r["status"] for i, r in by_id.items()} == {
        FYP: "generic", "rej": "rejected", "gen": "active", "closed": "active", "keep": "active",
        "nullst": None, "newgen": "generic", "newact": "active"}
    assert by_id[FYP]["last_seen"] == D and by_id[FYP]["valid_to"] is None
    assert by_id["closed"]["last_seen"] == day(9)


def test_merge_sql_never_deletes():
    sql = aggregate.cultural_map_merge_sql().upper()
    assert "MERGE" in sql and "DELETE" not in sql and "REPLACE" not in sql


# run_aggregate: item_daily known answers


def scenario(con):
    """Posts carrying #amapiano in ZA (and one in KE), sighted across lanes up to D."""
    posts, sightings = [], []

    def add(pid, creator_id, sights, tier="micro", platform="tiktok", geo=(None, None, None), engagement=1):
        g_market, g_conf, g_source = geo
        first = min(s[0] for s in sights)
        posts.append(post(pid, creator_id, first, platform=platform, tier=tier, hashtags=["#Amapiano"],
                          engagement=engagement, geo_market=g_market, geo_confidence=g_conf, geo_source=g_source))
        for d, lane_class, lane, *rest in sights:
            market = rest[0] if rest else "ZA"
            series, protocol = ("panel_fb_hub", "p1") if lane_class == "panel" else ("feed_tiktok", "p1")
            sightings.append(obs(pid, d, lane_class, lane, market=market, platform=platform,
                                 series=series, protocol=protocol))

    add("pA", "cA", [(D, "unbiased_rank", "sweep"), (D, "unbiased_rank", "sweep")],
        tier="micro", geo=("ZA", 0.9, "ext_region"), engagement=10)
    add("pB", "cB", [(D, "unbiased_rank", "sweep")], tier="mega", geo=("NG", 0.8, "home_market"), engagement=5)
    add("pC", "cC", [(day(1), "search_presence", "expansion"), (D, "unbiased_rank", "sweep")],
        tier="nano", geo=("ZA", 0.3, "language"), engagement=2)
    add("pD", "cD", [(D, "panel", "panel")], platform="facebook", geo=("ZA", 0.7, "place_mention"), engagement=4)
    add("pE", "cE", [(D, "search_presence", "agent_live")])
    add("pF", "cF", [(D, "legacy", "legacy")])
    add("pG", "cG", [(day(3), "unbiased_rank", "sweep"), (D, "unbiased_rank", "sweep")])
    add("pH", "cH", [(D, "unbiased_rank", "sweep", "KE")])
    add("pP", "cP", [(day(1), "search_presence", "placebo")])
    duck.load(con, "core.posts", posts)
    duck.load(con, "core.post_observations", sightings)
    duck.load(con, "core.creators", [creator(c) for c in ("cA", "cC", "cD", "cE", "cF", "cG", "cH", "cP")]
              + [creator("cB", 2)])


def amapiano_rows(con, run_id=None):
    rows = duck.query(con, "SELECT * FROM {core}.item_daily i WHERE i.item_id = @it "
                           "AND (@run IS NULL OR i.run_id = @run)", {"it": AMAPIANO, "run": run_id})
    return {(r["metric_date"], r["market"], r["lane_class"], r["platform"]): r for r in rows}


def test_run_aggregate_item_daily_known_answers(con):
    scenario(con)
    client = duck.Client(con)
    for d in (day(3), day(1), D):
        aggregate.run_aggregate(client, d, f"agg-{d:%d}", "r1", core="core", agent="agent")
    rows = amapiano_rows(con)
    assert set(rows) == {
        (day(3), "ZA", "unbiased_rank", "tiktok"), (day(3), "ZA", "_any", "_all"),
        (day(1), "ZA", "search_presence", "tiktok"), (day(1), "ZA", "_any", "_all"),
        (D, "ZA", "unbiased_rank", "tiktok"), (D, "ZA", "panel", "facebook"), (D, "ZA", "legacy", "tiktok"),
        (D, "ZA", "_any", "_all"), (D, "KE", "unbiased_rank", "tiktok"), (D, "KE", "_any", "_all"),
    }
    # pA (seen twice today), pB, and pC, whose first unbiased_rank sighting is today; pG was first seen day(3)
    r = rows[(D, "ZA", "unbiased_rank", "tiktok")]
    assert r["posts"] == 3 and r["creators"] == 3 and r["unflagged_creators"] == 2
    assert r["engagement"] == 17
    assert r["tier_posts"] == {"nano": 1, "micro": 1, "mid": 0, "macro": 0, "mega": 1}
    assert r["geo_known_posts"] == 2 and r["local_posts"] == 1      # pC's language guess never counts
    assert r["series"] is None and r["protocol"] is None
    assert r["first_post_at"] == at(day(1), 9)
    assert r["run_id"] == "agg-20" and r["rule_version"] == "r1" and r["available_at"] is not None

    p = rows[(D, "ZA", "panel", "facebook")]
    assert (p["posts"], p["series"], p["protocol"], p["geo_known_posts"], p["local_posts"]) == (
        1, "panel_fb_hub", "p1", 1, 1)
    assert rows[(D, "ZA", "legacy", "tiktok")]["posts"] == 1
    # '_any' today: pA, pB, pD; pC's first sighting anywhere was yesterday; agent_live and legacy never count
    a = rows[(D, "ZA", "_any", "_all")]
    assert a["posts"] == 3 and a["creators"] == 3 and a["unflagged_creators"] == 2
    assert a["series"] is None and a["protocol"] is None
    # yesterday: pC in search_presence and on '_any', plus the placebo post
    assert rows[(day(1), "ZA", "search_presence", "tiktok")]["posts"] == 2
    assert rows[(day(1), "ZA", "_any", "_all")]["posts"] == 2
    assert rows[(day(3), "ZA", "unbiased_rank", "tiktok")]["posts"] == 1
    assert rows[(D, "KE", "unbiased_rank", "tiktok")]["posts"] == 1
    assert rows[(D, "KE", "_any", "_all")]["posts"] == 1

    # the agent_live post is itemised (hashtag and creator) but counts in no item_daily row
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.post_items p WHERE p.post_id = 'pE'") == [{"n": 2}]
    cm = duck.query(con, "SELECT c.first_seen, c.first_seen_market, c.last_seen, c.status FROM {core}.cultural_map c "
                         "WHERE c.item_id = @it", {"it": AMAPIANO})
    assert cm == [{"first_seen": day(3), "first_seen_market": "ZA", "last_seen": D, "status": "active"}]


def test_rerun_with_new_run_id_appends_and_keeps_post_items_unique(con):
    scenario(con)
    client = duck.Client(con)
    for d in (day(3), day(1)):
        aggregate.run_aggregate(client, d, f"agg-{d:%d}", "r1", core="core", agent="agent")
    aggregate.run_aggregate(client, D, "first", "r1", core="core", agent="agent")
    aggregate.run_aggregate(client, D, "second", "r2", core="core", agent="agent")
    first, second = amapiano_rows(con, "first"), amapiano_rows(con, "second")
    assert set(first) == set(second) and len(first) == 6
    for key in first:
        assert first[key]["posts"] == second[key]["posts"]
    assert {r["rule_version"] for r in second.values()} == {"r2"}
    total = duck.query(con, "SELECT COUNT(*) n FROM {core}.item_daily i WHERE i.item_id = @it AND i.metric_date = @d",
                       {"it": AMAPIANO, "d": D})
    assert total == [{"n": 12}]
    dupes = duck.query(con, "SELECT p.post_id, p.item_id FROM {core}.post_items p "
                            "GROUP BY p.post_id, p.item_id HAVING COUNT(*) > 1")
    assert dupes == []
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.cultural_map c WHERE c.item_id = @it",
                      {"it": AMAPIANO}) == [{"n": 1}]


def heavy_day(con, n):
    """n posts first sighted on D, each by its own creator and nothing else, so n post_items and n items."""
    con.execute("INSERT INTO core.posts (post_id, platform, creator_id, creator_tier_at_post, published_at, post_date) "
                "SELECT 'h' || i, 'tiktok', 'hc' || i, 'micro', TIMESTAMPTZ '2026-09-20 09:00:00+00', DATE '2026-09-20' "
                "FROM range(?) t(i)", [n])
    con.execute("INSERT INTO core.post_observations (post_id, observed_at, observed_date, market, platform, lane, "
                "lane_class, run_id) SELECT 'h' || i, TIMESTAMPTZ '2026-09-20 12:00:00+00', DATE '2026-09-20', 'ZA', "
                "'tiktok', 'sweep', 'unbiased_rank', 'collect-20260920' FROM range(?) t(i)", [n])


class SizingClient(duck.Client):
    """duck.Client that also records the length of every array parameter it is sent."""

    def __init__(self, con):
        super().__init__(con)
        self.sizes = []

    def query(self, sql, job_config=None):
        params = job_config.query_parameters if job_config else []
        self.sizes.extend(len(p.values) for p in params if hasattr(p, "values"))
        return super().query(sql, job_config)


def test_heavy_day_goes_out_in_chunks_of_at_most_5000_rows_and_every_row_lands_once(con):
    heavy_day(con, 12001)
    client = SizingClient(con)
    counts = aggregate.run_aggregate(client, D, "a", "r1", core="core", agent="agent")
    assert counts == {"posts": 12001, "post_items": 12001, "items": 12001}
    verbs = [sql.lstrip().split(None, 1)[0].upper() for sql in client.sql]
    assert verbs == ["SELECT", "INSERT", "INSERT", "INSERT", "MERGE", "MERGE", "MERGE", "INSERT"]
    assert client.sizes == [5000, 5000, 2001, 5000, 5000, 2001]
    assert duck.query(con, "SELECT COUNT(*) n, COUNT(DISTINCT p.post_id || '|' || p.item_id) k "
                           "FROM {core}.post_items p") == [{"n": 12001, "k": 12001}]
    assert duck.query(con, "SELECT COUNT(*) n, COUNT(DISTINCT c.item_id) k FROM {core}.cultural_map c") == [
        {"n": 12001, "k": 12001}]


def test_heavy_day_rerun_inserts_nothing_twice(con):
    heavy_day(con, 12001)
    client = duck.Client(con)
    aggregate.run_aggregate(client, D, "a", "r1", core="core", agent="agent")
    aggregate.run_aggregate(client, D, "b", "r1", core="core", agent="agent")
    assert duck.query(con, "SELECT COUNT(*) n, COUNT(DISTINCT p.post_id || '|' || p.item_id) k "
                           "FROM {core}.post_items p") == [{"n": 12001, "k": 12001}]
    assert duck.query(con, "SELECT COUNT(*) n, COUNT(DISTINCT c.item_id) k FROM {core}.cultural_map c") == [
        {"n": 12001, "k": 12001}]


def test_run_aggregate_writes_only_by_insert_and_merge(con):
    scenario(con)
    client = duck.Client(con)
    aggregate.run_aggregate(client, D, "a", "r1", core="core", agent="agent")
    verbs = [sql.lstrip().split(None, 1)[0].upper() for sql in client.sql]
    assert verbs == ["SELECT", "INSERT", "MERGE", "INSERT"]
    for sql in client.sql:
        upper = sql.upper()
        assert all(w not in upper for w in ("DELETE", "DROP", "TRUNCATE", "REPLACE", "CREATE"))
