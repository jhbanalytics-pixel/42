"""Known-answer tests for clusters as items (BUILD.md 2.2: trends become clusters, not just hashtags).

The understand job (core/understand/cluster.py on L3) writes clusters, cluster_members and the cluster's topic row
in cultural_map before detect starts. Detect links each post first sighted on @d to the items of that day's
clusters it is a member of (post_items, via 'cluster'), so a cluster item runs through item_daily, series_test and
item_state exactly as a hashtag does. A cluster whose item has no open cultural_map row links nothing.
"""

import hashlib
from datetime import timedelta

import pytest

from .. import aggregate, stats
from ..items import canonical_key, item_id
from . import duck
from .fixtures import D, at, cmap, counter, creator, day, health, obs, post, run
from .test_detect_states import World, detect, sql_statements
from .test_detect_worth import st_row

AMAPIANO = item_id("hashtag", "amapiano")


def topic_id(cluster_id):
    """The item id L3's cluster.new_item_id gives a new cluster: sha256 of "topic|topic:{cluster_id}"."""
    return hashlib.sha256(f"topic|topic:{cluster_id}".encode("utf-8")).hexdigest()


TOPIC = topic_id("20260915-za-000")
GONE = topic_id("20260915-za-001")


@pytest.fixture
def con():
    c = duck.connect()
    for stmt in sql_statements("waves.sql"):
        c.execute(duck.create_statement(stmt))
    yield c
    c.close()


def topic_rows(iid, closed_versions=0, open_version=True):
    """cultural_map rows as L3's cluster_write.sql leaves them: closed versions first, then the open one."""
    rows = []
    for v in range(closed_versions):
        rows.append({**cmap(iid, kind="topic"), "canonical_key": "topic:x", "label": f"v{v}",
                     "valid_from": at(day(30 - v)), "valid_to": at(day(29 - v))})
    if open_version:
        rows.append({**cmap(iid, kind="topic"), "canonical_key": "topic:x", "label": "open",
                     "valid_from": at(day(29 - closed_versions)), "valid_to": None})
    return rows


def cluster(cid, d, iid, market="za", match_kind="new"):
    return {"cluster_date": d, "cluster_id": cid, "market": market, "item_id": iid, "match_kind": match_kind}


def members(cid, *post_ids):
    return [{"cluster_id": cid, "post_id": p, "probability": 0.9} for p in post_ids]


def test_l3_topic_ids_are_the_detect_item_id_of_their_key():
    cid = "20260915-za-000"
    assert item_id("topic", canonical_key("topic", f"topic:{cid}")) == topic_id(cid)


def test_items_rows_links_each_cluster_item_once_and_leaves_the_map_to_understand():
    posts = [
        {"post_id": "p1", "platform": "tiktok", "hashtags": ["#Amapiano"], "sound_id": None, "creator_id": None,
         "market": "ZA", "first_day": D, "cluster_items": [TOPIC, TOPIC]},
        {"post_id": "p1", "platform": "tiktok", "hashtags": ["#Amapiano"], "sound_id": None, "creator_id": None,
         "market": "NG", "first_day": D, "cluster_items": [TOPIC]},
        {"post_id": "p2", "platform": "tiktok", "hashtags": [], "sound_id": None, "creator_id": None,
         "market": "ZA", "first_day": D, "cluster_items": None},
    ]
    post_items, items = aggregate.items_rows(posts)
    assert sorted((r["post_id"], r["item_id"], r["via"]) for r in post_items) == sorted([
        ("p1", AMAPIANO, "hashtag"), ("p1", TOPIC, "cluster")])
    assert [r["item_id"] for r in items] == [AMAPIANO]


def world(con):
    """Posts carrying #amapiano on the ZA feed; clusters of D and day(1) written as L3 writes them.

    p1 sits in the ZA cluster and the pooled cluster, both matched to TOPIC. p2 sits in the ZA cluster only.
    p3 sits in a cluster whose item GONE has only a closed map row. p4 was first sighted day(1), in that day's
    cluster, and is sighted again today, when it sits in today's cluster too. p5 is first sighted today and sits in
    no cluster of today, only in tomorrow's, as a backfill rerun of today would find it."""
    sights = {"p1": [D], "p2": [D], "p3": [D], "p4": [day(1), D], "p5": [D]}
    duck.load(con, "core.posts", [post(p, f"c{p}", min(ds), hashtags=["#Amapiano"], engagement=1)
                                  for p, ds in sights.items()])
    duck.load(con, "core.post_observations", [obs(p, d, "unbiased_rank", "sweep", series="feed_tiktok",
                                                  protocol="p1") for p, ds in sights.items() for d in ds])
    duck.load(con, "core.creators", [creator(f"c{p}") for p in sights])
    duck.load(con, "core.cultural_map", topic_rows(TOPIC, closed_versions=2)
              + topic_rows(GONE, closed_versions=1, open_version=False))
    duck.load(con, "core.clusters", [
        cluster("20260920-za-000", D, TOPIC), cluster("20260920-pan-000", D, TOPIC, market="pan"),
        cluster("20260920-za-001", D, GONE), cluster("20260919-za-000", day(1), TOPIC),
        cluster("20260921-za-000", D + timedelta(days=1), TOPIC)])
    duck.load(con, "core.cluster_members", members("20260920-za-000", "p1", "p2", "p4")
              + members("20260920-pan-000", "p1") + members("20260920-za-001", "p3")
              + members("20260919-za-000", "p4") + members("20260921-za-000", "p5"))


def daily(con, iid):
    rows = duck.query(con, "SELECT * FROM {core}.item_daily i WHERE i.item_id = @it", {"it": iid})
    return {(r["metric_date"], r["market"], r["lane_class"], r["platform"]): r["posts"] for r in rows}


def test_cluster_item_gets_item_daily_once_per_post_beside_its_hashtag(con):
    world(con)
    client = duck.Client(con)
    for d in (day(1), D):
        aggregate.run_aggregate(client, d, f"agg-{d:%d}", "r1", core="core", agent="agent")
    assert daily(con, TOPIC) == {
        (day(1), "ZA", "unbiased_rank", "tiktok"): 1, (day(1), "ZA", "_any", "_all"): 1,   # p4
        (D, "ZA", "unbiased_rank", "tiktok"): 2, (D, "ZA", "_any", "_all"): 2}             # p1 and p2
    assert daily(con, AMAPIANO) == {
        (day(1), "ZA", "unbiased_rank", "tiktok"): 1, (day(1), "ZA", "_any", "_all"): 1,
        (D, "ZA", "unbiased_rank", "tiktok"): 4, (D, "ZA", "_any", "_all"): 4}             # p1, p2, p3, p5
    assert daily(con, GONE) == {}
    links = duck.query(con, "SELECT p.post_id, p.via FROM {core}.post_items p WHERE p.item_id = @it "
                            "ORDER BY p.post_id", {"it": TOPIC})
    assert links == [{"post_id": "p1", "via": "cluster"}, {"post_id": "p2", "via": "cluster"},
                     {"post_id": "p4", "via": "cluster"}]
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.post_items p WHERE p.item_id = @it", {"it": GONE}) == [
        {"n": 0}]
    # the topic rows are understand's: detect adds none, closes none, reopens none
    cm = duck.query(con, "SELECT c.item_id, c.label, c.valid_to IS NULL is_open FROM {core}.cultural_map c "
                         "WHERE c.kind = 'topic' ORDER BY c.item_id, c.label", {})
    assert sorted((r["item_id"], r["label"], r["is_open"]) for r in cm) == sorted([
        (TOPIC, "v0", False), (TOPIC, "v1", False), (TOPIC, "open", True), (GONE, "v0", False)])


def test_rerun_adds_no_cluster_link_twice(con):
    world(con)
    client = duck.Client(con)
    for run_id in ("a", "b"):
        aggregate.run_aggregate(client, D, run_id, "r1", core="core", agent="agent")
    assert duck.query(con, "SELECT p.post_id, p.item_id FROM {core}.post_items p "
                           "GROUP BY p.post_id, p.item_id HAVING COUNT(*) > 1") == []
    rows = duck.query(con, "SELECT i.run_id, i.posts FROM {core}.item_daily i WHERE i.item_id = @it "
                           "AND i.lane_class = '_any' ORDER BY i.run_id", {"it": TOPIC})
    assert rows == [{"run_id": "a", "posts": 2}, {"run_id": "b", "posts": 2}]


def test_cluster_item_reaches_item_state_like_its_hashtag(con):
    """A hub panel: 2 posts a day for five days, then 8 today, every post #amapiano and in that day's cluster."""
    posts, sightings, creators, clusters_, members_ = [], [], [], [], []
    days = [day(i) for i in range(5, -1, -1)]
    n = 0
    for d in days:
        cid = f"{d:%Y%m%d}-za-000"
        clusters_.append(cluster(cid, d, TOPIC))
        for _ in range(8 if d == D else 2):
            n += 1
            pid, cr = f"q{n}", f"qc{n}"
            posts.append(post(pid, cr, d, platform="facebook", hashtags=["#Amapiano"]))
            sightings.append(obs(pid, d, "panel", "panel", platform="facebook", series="panel_fb_hub",
                                 protocol="p1"))
            creators.append(creator(cr))
            members_ += members(cid, pid)
    duck.load(con, "core.posts", posts)
    duck.load(con, "core.post_observations", sightings)
    duck.load(con, "core.creators", creators)
    duck.load(con, "core.clusters", clusters_)
    duck.load(con, "core.cluster_members", members_)
    duck.load(con, "core.cultural_map", topic_rows(TOPIC, closed_versions=1))
    duck.load(con, "core.collection_health", [health("panel_fb_hub", d, platform="facebook", lane_class="panel",
                                                     k=1.0) for d in days])
    duck.load(con, "agent.runs", [run(stage, d) for stage in ("collect", "aggregate") for d in days])
    client = duck.Client(con)
    for d in days:
        aggregate.run_aggregate(client, d, f"aggregate-{d:%Y%m%d}", "r1", core="core", agent="agent")
    signal = duck.query(con, "SELECT * FROM {core}.tvf_series_signal(@d)", {"d": D})
    duck.load(con, "core.series_test", stats.passthrough_rows(signal, D, "stats-test", "r1"))
    duck.load(con, "agent.runs", [run("stats", D, "stats-test")])
    rows = detect(con)
    t, h = rows[TOPIC], rows[AMAPIANO]
    # six observed days and 12 posts in the last 3 against 6 before: emerging, which outranks the panel spike
    assert (t["kind"], t["state"], t["untested"], t["main_y"]) == ("topic", "emerging", True, 8.0)
    assert t["main_series_id"] == f"{TOPIC}|ZA|panel_fb_hub|p1"
    assert (t["posts3"], t["creators3"]) == (12, 12)
    assert (h["kind"], h["state"], h["main_y"], h["posts3"], h["creators3"]) == ("hashtag", "emerging", 8.0, 12, 12)
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.item_state s WHERE s.item_id = @it",
                      {"it": TOPIC}) == [{"n": 1}]


def test_lowercase_market_cluster_labels_its_market_row_and_a_pooled_cluster_labels_none(con):
    """L3 writes clusters.market in lowercase ('za', 'ng', 'ke') or 'pan'. Two On the boards items in ZA, each
    matched by its own variant cluster: zv's cluster is the ZA run's ('za'), so the ZA row is a variant; pv's is
    the pooled run's ('pan'), which labels no market row, so pv stays ongoing."""
    w = World()
    w.days["stats"].add(D)
    w.days["collect"].add(D)
    for item in ("zv", "pv"):
        w.items.add(item)
        w.t["core.series_test"].append(st_row(item, D, tested=True, y=1.0, p=.5, mu=1.0))
        w.t["core.item_counter_daily"].append(counter(item, "board_tiktok_hashtag", D, 1.0, is_board=True))
    w.t["core.clusters"] += [cluster("20260920-za-000", D, "zv", match_kind="variant"),
                             cluster("20260920-pan-000", D, "pv", market="pan", match_kind="variant")]
    w.load(con)
    rows = detect(con)
    assert (rows["zv"]["market"], rows["zv"]["state"], rows["zv"]["novelty"]) == ("ZA", "on_the_boards", "variant")
    assert (rows["pv"]["market"], rows["pv"]["state"], rows["pv"]["novelty"]) == ("ZA", "on_the_boards", "ongoing")
