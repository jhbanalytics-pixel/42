"""The G6 scope gate judges the pack the writer sees (C4 v2 section 18: market_posts7 and total_posts7 are the pack
counts). evidence.sql is the one source of the selection; market_scope.sql (the brief) and v_item_market_scope
(detect) must pick the same posts. The reviewer's two probes are the first two tests; the rest check the three
statements against each other on worlds with members."""

import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.brief import evidence, pack_order
from core.brief.market_scope import QUERIES as SCOPE_QUERIES
from core.brief.market_scope import read_market_scope
from core.brief.tests.test_brief_market_scope import D, add_post, scope, sighting, view_scope
from core.detect.tests import duck

UTC = timezone.utc
LOCAL = {"geo_market": "ZA", "geo_confidence": 0.9, "geo_source": "ext_region"}
LOCAL_NG = {"geo_market": "NG", "geo_confidence": 0.9, "geo_source": "ext_region"}
MARKETS = ("ZA", "NG")
CLUSTER = "20260930-za-001"


def clusters(con):
    duck.load(con, "core.clusters", [{"cluster_date": D, "cluster_id": CLUSTER, "market": "za", "item_id": "i1",
                                       "match_kind": "match"}])


def member(con, post_id):
    duck.load(con, "core.cluster_members", [{"cluster_id": CLUSTER, "post_id": post_id, "probability": 0.9}])


def pack_counts(con, market="ZA", d=D):
    """(total, local) of the pack evidence.sql builds for the same world: the reference both scopes must match."""
    start = datetime.combine(d - timedelta(days=6), datetime.min.time(), timezone(timedelta(hours=2)))
    rows = evidence._pack_rows(duck.Client(con), {
        "item_id": "i1", "market": market, "d": d, "start": start, "end": start + timedelta(days=7),
        "outlet_cap": pack_order.OUTLET_CAP}, "core", "agent")
    rows = [r for r in rows if r["post_id"] is not None]
    return len(rows), sum(bool(r["local_flag"]) for r in rows)


def context_vs_members(*, members_local):
    """12 high engagement context posts and 10 low engagement members, one creator each. The pack is the 10
    members and the 2 best context posts; the old order would have picked the 12 context posts."""
    def build(con):
        clusters(con)
        for n in range(12):
            add_post(con, f"ctx_{n}", creator_id=f"ctx_{n}", engagement=10_000 + n, **({} if members_local else LOCAL))
        for n in range(10):
            add_post(con, f"mem_{n}", creator_id=f"mem_{n}", engagement=10 + n, **(LOCAL if members_local else {}))
            member(con, f"mem_{n}")
    return build


@pytest.mark.parametrize("detect_view", [False, True], ids=["brief query", "detect view"])
def test_ten_of_the_twelve_pack_posts_local_is_not_scoped_global(detect_view):
    con = duck.connect(views=detect_view)
    context_vs_members(members_local=True)(con)

    result = view_scope(con) if detect_view else scope(con)[0]

    assert pack_counts(con) == (12, 10)
    assert result == {"market_scope": "market", "market_posts7": 10, "total_posts7": 12, "market_share7": 10 / 12}


@pytest.mark.parametrize("detect_view", [False, True], ids=["brief query", "detect view"])
def test_two_of_the_twelve_pack_posts_local_is_not_scoped_market(detect_view):
    con = duck.connect(views=detect_view)
    context_vs_members(members_local=False)(con)

    result = view_scope(con) if detect_view else scope(con)[0]

    assert pack_counts(con) == (12, 2)
    assert result == {"market_scope": "global", "market_posts7": 2, "total_posts7": 12, "market_share7": 2 / 12}


DATES = (D, D - timedelta(days=1))


def scope_clusters(con):
    """Every kind of cluster a post can be a member of: the market's own current run in ZA and in NG, and the ones
    that must not make a post a member: a stale run, a future run, the pooled run and another item's run."""
    day = timedelta(days=1)
    rows = [("cur", D, "za", "i1"), ("cur", D, "ng", "i1"), ("old", D - 10 * day, "za", "i1"),
            ("old", D - 10 * day, "ng", "i1"), ("new", D + day, "za", "i1"), ("new", D + day, "ng", "i1"),
            ("pan", D, "pan", "i1"), ("two", D, "za", "i2"), ("two", D, "ng", "i2")]
    duck.load(con, "core.clusters", [{"cluster_date": when, "cluster_id": f"{when:%Y%m%d}-{market}-{kind}",
                                       "market": market, "item_id": item, "match_kind": "match"}
                                      for kind, when, market, item in rows])
    return {(kind, market, item): f"{when:%Y%m%d}-{market}-{kind}" for kind, when, market, item in rows}


def join(con, cluster_id, post_id):
    duck.load(con, "core.cluster_members", [{"cluster_id": cluster_id, "post_id": post_id, "probability": 0.9}])


def random_world(seed):
    """A world with members of every kind of cluster, repeated creators, confirm finds, feed sightings, engagement
    ties, posts seen in one market or both and on one day or two."""
    rnd = random.Random(seed)

    def build(con):
        ids = list(scope_clusters(con).values())
        for n in range(rnd.randint(8, 30)):
            fields = rnd.choice([LOCAL, LOCAL_NG, {}, {"geo_market": "KE", "geo_confidence": 0.9, "geo_source": "ext_region"},
                                 {"source_sightings": [sighting("ZA", D)]}, {"source_sightings": [sighting("NG", D)]}])
            lane = rnd.choice(["sweep", "sweep", "confirm"])
            add_post(con, f"p{n:02d}", creator_id=f"c{rnd.randint(0, 9)}", engagement=rnd.choice([5, 5, 50, 500, 5000]),
                     lane=lane, lane_class="search_presence" if lane == "confirm" else rnd.choice(
                         ["unbiased_rank", "panel", "search_presence"]),
                     observation_markets=rnd.choice([("ZA",), ("NG",), ("ZA", "NG")]),
                     observed_date=rnd.choice(DATES), **fields)
            for cluster_id in rnd.sample(ids, rnd.choice([0, 0, 1, 1, 2])):
                join(con, cluster_id, f"p{n:02d}")
    return build


def detect_scopes(con):
    """The detect view's (total, local) for each market and date, with a good detect run and item state for each."""
    duck.load(con, "agent.runs", [{"run_id": f"detect-{d}", "stage": "detect", "run_date": d, "status": "ok",
                                   "finished_at": datetime.combine(d, datetime.min.time(), UTC) + timedelta(hours=6)}
                                  for d in DATES])
    duck.load(con, "core.item_state", [{"metric_date": d, "market": m, "item_id": "i1", "run_id": f"detect-{d}"}
                                       for d in DATES for m in MARKETS])
    rows = duck.query(con, "SELECT metric_date, market, market_posts7, total_posts7 FROM {core}.v_item_market_scope "
                           "WHERE item_id = 'i1'")
    return {(r["market"], r["metric_date"]): (r["total_posts7"], r["market_posts7"]) for r in rows}


def brief_scope(con, market, d):
    got = read_market_scope(duck.Client(con), {"item_id": "i1"}, d, market, core="core", agent="agent")
    return got["total_posts7"], got["market_posts7"]


def assert_the_scopes_count_the_pack(brief_con, view_con):
    seen = detect_scopes(view_con)
    for market in MARKETS:
        for d in DATES:
            expected = pack_counts(brief_con, market, d)
            assert brief_scope(brief_con, market, d) == expected, ("brief", market, d)
            assert seen.get((market, d)) == expected, ("detect", market, d)


@pytest.mark.parametrize("seed", range(14))
def test_the_brief_scope_and_the_detect_view_count_exactly_the_posts_the_pack_holds(seed):
    brief_con, view_con = duck.connect(views=False), duck.connect()
    random_world(seed)(brief_con)
    random_world(seed)(view_con)
    assert_the_scopes_count_the_pack(brief_con, view_con)


def member_kind_world(kind):
    """12 unlocated posts that outrank two low engagement posts, one local to ZA and one local to NG, all seen in both
    markets on the day before D. Both are members of the clusters of one kind (scope_clusters); in "swapped" each is
    a member only of the other market's current run. Such a post is in the pack, and so counted local, only when it is
    a member of its own market's run and that run is dated inside the 7 days to the date asked for."""
    def build(con):
        ids = scope_clusters(con)
        for n in range(12):
            add_post(con, f"ctx_{n}", creator_id=f"ctx_{n}", engagement=10_000 + n, observation_markets=MARKETS,
                     observed_date=D - timedelta(days=1))
        for market, fields in (("za", LOCAL), ("ng", LOCAL_NG)):
            add_post(con, f"tgt_{market}", creator_id=f"tgt_{market}", engagement=1, observation_markets=MARKETS,
                     observed_date=D - timedelta(days=1), **fields)
        for market, other in (("za", "ng"), ("ng", "za")):
            if kind == "swapped":
                join(con, ids[("cur", other, "i1")], f"tgt_{market}")
            elif kind == "pan":
                join(con, ids[("pan", "pan", "i1")], f"tgt_{market}")
            else:
                for post in ("tgt_za", "tgt_ng"):
                    join(con, ids[(kind, market, "i2" if kind == "two" else "i1")], post)
    return build


@pytest.mark.parametrize("kind,local_on", [("cur", {D}), ("old", set()), ("new", set()), ("two", set()),
                                           ("pan", set()), ("swapped", set())])
def test_only_a_cluster_of_the_item_in_the_markets_own_run_inside_the_window_makes_a_post_a_member(kind, local_on):
    brief_con, view_con = duck.connect(views=False), duck.connect()
    member_kind_world(kind)(brief_con)
    member_kind_world(kind)(view_con)

    assert_the_scopes_count_the_pack(brief_con, view_con)

    # The world discriminates: the market's own target is in the pack exactly when it is a member on that date.
    for market in MARKETS:
        for d in DATES:
            assert pack_counts(brief_con, market, d)[1] == (1 if d in local_on else 0), (kind, market, d)


def test_the_outlet_cap_is_not_applied_by_either_scope_so_it_must_stay_at_or_above_the_pack_size():
    """market_scope.sql and v_item_market_scope do not apply the outlet cap (the view has no way to be given the
    registry). At 12 the cap changes no pack, so they still match. Setting it lower is W8-DEC-05c-ii: copy the outlet
    filter into both first, then change this test."""
    assert pack_order.OUTLET_CAP >= pack_order.PACK_LIMIT
    assert "outlet" not in SCOPE_QUERIES["market_scope"].lower()


CREATOR_ORDER = re.compile(r"ORDER BY mm\.post_id IS NULL, \w+\.measured DESC, IFNULL\(ps\.engagement, 0\) DESC, ps\.post_id")
PACK_ORDER = re.compile(r"ORDER BY (\w+\.)?market_member DESC, (\w+\.)?measured DESC, (\w+\.)?eng DESC, (\w+\.)?post_id")
MEMBERS = re.compile(r"UPPER\(k\.market\) = (@market|s\.market)")


def test_the_three_statements_rank_members_first_wherever_they_order_the_pack():
    root = Path(__file__).resolve().parents[2]
    view = (root / "detect" / "sql" / "views.sql").read_text(encoding="utf-8")
    view = view[view.index("CREATE OR REPLACE VIEW {core}.v_item_market_scope"):]
    for name, sql in (("evidence.sql", evidence.QUERIES["evidence"]), ("market_scope.sql", SCOPE_QUERIES["market_scope"]),
                      ("v_item_market_scope", view)):
        sql = re.sub(r"\s+", " ", sql)
        assert CREATOR_ORDER.search(sql), name
        assert PACK_ORDER.search(sql), name
        assert MEMBERS.search(sql), name
