"""locality_v2 on the DuckDB harness: truth table, validity edges, lanes, window, feeds, digest, and the
three v1 against v2 disagreement worlds. Lane 5 places this at core/detect/tests/test_locality_v2.py with the
two SQL files at core/detect/sql/locality_members.sql and locality_summary.sql."""

import hashlib
import itertools
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.brief.market_scope import read_market_scope
from core.detect import aggregate, sqlrun
from core.detect.tests import duck

SQL = Path(__file__).resolve().parents[1] / "sql"          # in the repo; the scratch run points this at its copy
UTC = timezone.utc
D = date(2026, 10, 7)
CUTOFF = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)          # the detect run's recorded started_at
SERIAL = itertools.count(1)
DETECT, VERSION, STATS = "detect-20261007-aaaa", "locality_v2.1", "stats-20261007-aaaa"

@pytest.fixture
def con():
    c = duck.connect()
    duck.load(c, "agent.runs", [
        {"run_id": STATS, "stage": "stats", "run_date": D, "status": "ok",
         "started_at": CUTOFF - timedelta(minutes=30), "finished_at": CUTOFF - timedelta(minutes=10)},
        {"run_id": DETECT, "stage": "detect", "run_date": D, "status": "ok",
         "started_at": CUTOFF, "finished_at": CUTOFF + timedelta(minutes=20)}])
    yield c
    c.close()


def key(con, item, market="NG"):
    duck.load(con, "core.series_test", [{"metric_date": D, "series_id": f"{item}|{market}|s|p", "item_id": item,
                                          "market": market, "run_id": STATS}])


def add(con, item, *, creator=None, platform="tiktok", geo=None, conf=None, src=None, days_ago=1, engagement=10,
        lane_class="unbiased_rank", lane="sweep", route="tiktok/trending", source_market=None, market="NG",
        observed_at=None, extra_obs=()):
    n = next(SERIAL)
    pid = f"{item}-p{n}"
    published = datetime(2026, 10, 7, 9, tzinfo=UTC) - timedelta(days=days_ago)
    duck.load(con, "core.posts", [{"post_id": pid, "platform": platform, "creator_id": creator or f"c{n}",
                                    "published_at": published, "post_date": published.date(),
                                    "engagement": engagement, "geo_market": geo, "geo_confidence": conf,
                                    "geo_source": src}])
    duck.load(con, "core.post_items", [{"post_id": pid, "item_id": item, "via": "hashtag"}])
    seen = observed_at or published + timedelta(hours=1)
    for lc, ln, rt, sm in [(lane_class, lane, route, source_market), *extra_obs]:
        duck.load(con, "core.post_observations", [{
            "post_id": pid, "observed_at": seen, "observed_date": seen.date(), "market": market,
            "platform": platform, "route": rt, "lane": ln, "lane_class": lc, "source_market": sm}])
        if sm:
            duck.load(con, "core.source_market_fixture", [{
                "post_id": pid, "source_markets": [sm],
                "source_sightings": [{"source_market": sm, "source_region": None, "route": rt, "protocol": "p",
                                      "observed_at": seen, "obs_date": seen.date()}]}])
    return pid


def known(con, item, local, foreign, unknown):
    for _ in range(local):
        add(con, item, geo="NG", conf=0.9, src="ext_region")
    for _ in range(foreign):
        add(con, item, geo="AE", conf=0.9, src="ext_region")
    for _ in range(unknown):
        add(con, item)


def compute(con):
    params = {"d": D, "cutoff": CUTOFF, "detect_run_id": DETECT, "metric_version": VERSION}
    for name in ("locality_members.sql", "locality_summary.sql"):
        duck.run_duck(con, sqlrun.render((SQL / name).read_text(encoding="utf-8"), "core", "agent"), params)
    return {r["item_id"]: r for r in duck.query(con, "SELECT * FROM {core}.item_locality", {})}


def v1_detect(con):
    days = [D - timedelta(days=i) for i in range(6, -1, -1)]
    duck.load(con, "agent.runs", [{"run_id": f"aggregate-{d:%Y%m%d}", "stage": "aggregate", "run_date": d,
                                    "status": "ok", "started_at": datetime.combine(d, datetime.min.time(), UTC),
                                    "finished_at": datetime.combine(d, datetime.min.time(), UTC)} for d in days])
    for d in days:
        duck.run_duck(con, sqlrun.render(aggregate.aggregate_sql(), "core", "agent"),
                      {"d": d, "run_id": f"aggregate-{d:%Y%m%d}", "rule_version": "r1"})
    out = {}
    for r in duck.query(con, "SELECT * FROM {core}.tvf_item_window(@d)", {"d": D}):
        k, loc = r["geo_known_posts7"] or 0, r["local_posts7"] or 0
        out[r["item_id"]] = (k, loc, "market_unconfirmed" if k < 8 else "not_local" if loc / k < .6 else "local")
    return out


def digest(members):
    lines = sorted((m["post_id"], m["locality_class"], m["creator_key"] or "", "1" if m["feed_sighted"] else "0")
                   for m in members)
    return hashlib.sha256("\n".join("|".join(x) for x in lines).encode()).hexdigest()


# (item, population, known, local, foreign, unknown, status, builder)
TRUTH = [
    ("t01", 0, 0, 0, 0, 0, "market_unconfirmed", lambda c, i: None),                       # a valid zero
    ("t02", 12, 0, 0, 0, 12, "market_unconfirmed", lambda c, i: known(c, i, 0, 0, 12)),
    ("t03", 7, 7, 7, 0, 0, "market_unconfirmed", lambda c, i: known(c, i, 7, 0, 0)),       # 7 known, all local
    ("t04", 7, 7, 0, 7, 0, "market_unconfirmed", lambda c, i: known(c, i, 0, 7, 0)),       # 7 known, none local
    ("t05", 11, 8, 4, 4, 3, "not_local", lambda c, i: known(c, i, 4, 4, 3)),               # 8 known, 4 local
    ("t06", 8, 8, 5, 3, 0, "local", lambda c, i: known(c, i, 5, 3, 0)),                    # 8 known, 5 local
    ("t07", 10, 10, 6, 4, 0, "local", lambda c, i: known(c, i, 6, 4, 0)),                  # exactly 0.6
    ("t08", 9, 9, 5, 4, 0, "not_local", lambda c, i: known(c, i, 5, 4, 0)),                # 0.5556
    ("t09", 8, 8, 0, 8, 0, "not_local", lambda c, i: known(c, i, 0, 8, 0)),                # a real zero local count
]


@pytest.mark.parametrize(("item", "pop", "kn", "loc", "fo", "unk", "status", "build"), TRUTH)
def test_truth_table(con, item, pop, kn, loc, fo, unk, status, build):
    key(con, item)
    build(con, item)
    row = compute(con)[item]
    assert (row["population_posts"], row["known_posts"], row["local_posts"], row["foreign_posts"],
            row["unknown_posts"], row["status"]) == (pop, kn, loc, fo, unk, status)
    assert row["local_share"] == (loc / kn if kn else None)


def test_validity_edges_known_means_confidence_source_and_a_nonempty_location(con):
    key(con, "t10")
    add(con, "t10", geo="NG", conf=0.69, src="ext_region")      # below 0.7
    add(con, "t10", geo="NG", conf=1.0, src="language")         # language never counts, whatever the confidence
    add(con, "t10", geo="", conf=0.9, src="home_market")        # empty location
    add(con, "t10", geo="NG", conf=0.9, src=None)               # no source
    add(con, "t10", geo="NG", conf=0.7, src="place_mention")    # exactly 0.7 is valid
    add(con, "t10", geo="ng ", conf=0.8, src="home_market")     # trimmed and upper-cased
    row = compute(con)["t10"]
    assert (row["population_posts"], row["known_posts"], row["local_posts"], row["unknown_posts"]) == (6, 2, 2, 4)
    assert v1_detect(con)["t10"][:2] == (5, 3)                  # v1 detect counts five as known, three as local


def test_only_measured_feed_sightings_make_a_post_part_of_the_population(con):
    key(con, "t11")
    for lc, ln in (("search_presence", "confirm"), ("search_presence", "placebo"), ("search_presence", "agent_live"),
                   ("search_presence", "expansion"), ("legacy", "legacy"), ("watchlist", "watchlist"),
                   ("unbiased_counter", "watchlist")):
        add(con, "t11", geo="NG", conf=0.9, src="ext_region", lane_class=lc, lane=ln)
    add(con, "t11", geo="NG", conf=0.9, src="ext_region", lane_class="search_presence", lane="expansion",
        extra_obs=[("unbiased_rank", "sweep", "tiktok/trending", None)])   # an own-search find also seen on a feed
    assert compute(con)["t11"]["population_posts"] == 1
    assert v1_detect(con)["t11"][:2] == (6, 6)                  # v1 detect counts every lane but legacy and agent_live


def test_publication_window_and_cutoff(con):
    key(con, "t12")
    add(con, "t12", geo="NG", conf=0.9, src="ext_region", days_ago=7)    # published 7 local days back: out
    add(con, "t12", geo="NG", conf=0.9, src="ext_region", days_ago=6)    # first day of the window: in
    add(con, "t12", geo="NG", conf=0.9, src="ext_region", days_ago=0)    # the run date: in
    add(con, "t12", geo="NG", conf=0.9, src="ext_region", days_ago=0,
        observed_at=CUTOFF + timedelta(minutes=5))                        # sighted after the cutoff: out
    assert compute(con)["t12"]["population_posts"] == 2


def test_market_by_source_is_its_own_quantity_and_the_foreign_veto_is_per_post(con):
    key(con, "t13")
    add(con, "t13", source_market="NG")                                               # feed only
    add(con, "t13", route="youtube/videos/trending", source_market="NG")             # the board is not a feed
    add(con, "t13", geo="AE", conf=0.9, src="ext_region", source_market="NG")        # vetoed, not whole-topic
    add(con, "t13", geo="NG", conf=0.9, src="ext_region", source_market="NG")        # located, also a feed post
    add(con, "t13", source_market="ZA")                                               # another market's feed
    row = compute(con)["t13"]
    assert (row["population_posts"], row["known_posts"], row["local_posts"], row["feed_only_posts"],
            row["vetoed_feed_posts"], row["feed_only_creators"]) == (5, 2, 1, 1, 1, 1)


def test_a_suppressed_creator_is_not_in_the_population(con):
    con.execute("INSERT INTO core.suppressed_fixture VALUES ('hidden')")
    key(con, "t14")
    add(con, "t14", creator="hidden", geo="NG", conf=0.9, src="ext_region")
    add(con, "t14", creator="open", geo="NG", conf=0.9, src="ext_region")
    assert compute(con)["t14"]["population_posts"] == 1


def test_breadth_is_one_distinct_count_over_located_and_feed_only_creators(con):
    key(con, "t18")
    add(con, "t18", creator="solo", geo="NG", conf=0.9, src="ext_region")        # one creator, one located post
    add(con, "t18", creator="solo", source_market="NG")                           # and one feed-only post
    add(con, "t18", creator="other", source_market="NG")                          # a second creator, feed only
    row = compute(con)["t18"]
    assert (row["local_creators"], row["feed_only_creators"], row["breadth_creators"]) == (1, 2, 2)
    # summing the two counts would give 3 and let a single creator pass the "2 or more creators" key


def test_creators_are_counted_as_creators_not_posts(con):
    key(con, "t15")
    for who in ("a", "a", "b"):
        add(con, "t15", creator=who, geo="NG", conf=0.9, src="ext_region")
    row = compute(con)["t15"]
    assert (row["local_posts"], row["local_creators"]) == (3, 2)


def test_the_digest_is_recomputed_from_the_retained_members_and_changes_when_one_member_does(con):
    key(con, "t16")
    known(con, "t16", 3, 2, 4)
    row = compute(con)["t16"]
    members = duck.query(con, "SELECT * FROM {core}.item_locality_post WHERE item_id = 't16'", {})
    assert len(members) == row["population_posts"] and digest(members) == row["population_digest"]
    members[0]["locality_class"] = "local" if members[0]["locality_class"] != "local" else "unknown"
    assert digest(members) != row["population_digest"]


def test_a_second_run_for_the_same_detect_run_adds_nothing(con):
    key(con, "t17")
    known(con, "t17", 2, 0, 1)
    first = compute(con)
    members = duck.query(con, "SELECT COUNT(*) n FROM {core}.item_locality_post", {})[0]["n"]
    second = compute(con)
    assert first == second
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.item_locality_post", {})[0]["n"] == members


# Three worlds on which v1 and v2 disagree, read through the real v1 SQL.

def world_v1_rejects_v2_admits(con, i):
    """Own-search finds located abroad push v1 detect past 8 known posts and under 0.6 local."""
    for n in range(3):
        add(con, i, creator=f"{i}-l{n}", geo="NG", conf=0.9, src="ext_region", source_market="NG")
    for n in range(5):
        add(con, i, creator=f"{i}-u{n}", source_market="NG")
    for n in range(9):
        add(con, i, creator=f"{i}-s{n}", geo="AE", conf=0.9, src="ext_region", lane_class="search_presence",
            lane="expansion", route="tiktok/search/top", source_market="NG")


def world_pack_rejects_v2_admits(con, i):
    """Six outlets with two high engagement unlocated posts each fill the 12 post sample; twenty creators located
    in NG sit below them. This is the Tinubu shape of the 7 Oct retained ranking."""
    for n in range(6):
        for _ in range(2):
            add(con, i, creator=f"{i}-outlet{n}", platform="twitter", lane_class="panel", lane="panel",
                route="twitter/user/tweets", engagement=5000)
    for n in range(20):
        add(con, i, creator=f"{i}-l{n}", geo="NG", conf=0.9, src="ext_region", source_market="NG")


def world_v1_admits_v2_rejects(con, i):
    """Twelve unlocated YouTube board videos fill the sample as market posts; eight feed posts are located abroad;
    twelve own-search finds located in NG lift v1 detect to exactly 0.6."""
    for n in range(6):
        for _ in range(2):
            add(con, i, creator=f"{i}-yt{n}", platform="youtube", route="youtube/videos/trending",
                source_market="NG", engagement=1000)
    for n in range(8):
        add(con, i, creator=f"{i}-ae{n}", geo="AE", conf=0.9, src="ext_region", source_market="NG", engagement=100)
    for n in range(12):
        add(con, i, creator=f"{i}-s{n}", geo="NG", conf=0.9, src="ext_region", lane_class="search_presence",
            lane="expansion", route="tiktok/search/top", source_market="NG", engagement=50)


DISAGREE = [
    ("d1", world_v1_rejects_v2_admits, ("not_local", "market"), "market_unconfirmed"),
    ("d2", world_pack_rejects_v2_admits, ("local", "global"), "local"),
    ("d3", world_v1_admits_v2_rejects, ("local", "market"), "not_local"),
]


@pytest.mark.parametrize(("item", "build", "v1", "v2"), DISAGREE, ids=["v1_rejects", "pack_rejects", "v1_admits"])
def test_v1_and_v2_disagree_on_the_same_raw_rows(con, item, build, v1, v2):
    key(con, item)
    build(con, item)
    row = compute(con)[item]
    detect_status = v1_detect(con)[item][2]
    pack = read_market_scope(duck.Client(con), {"item_id": item}, D, "NG", core="core", agent="agent")
    assert (detect_status, pack["market_scope"]) == v1
    assert row["status"] == v2


def test_a_false_match_removal_and_a_split_change_the_population_the_row_counts(con):
    """Review N6: the population reads the dated, end-aware link set, not raw post_items."""
    key(con, "t19")
    key(con, "t19c")
    local = [add(con, "t19", geo="NG", conf=0.9, src="ext_region") for _ in range(5)]
    foreign = [add(con, "t19", geo="AE", conf=0.9, src="ext_region") for _ in range(5)]
    for pid in foreign:                                                   # five wrongly matched posts leave the item
        duck.load(con, "core.post_item_end", [{"post_id": pid, "item_id": "t19", "ended_on": D, "reason": "false_match_removal",
                                                "lineage_id": "fm-1"}])
    for pid in local[:2]:                                                 # two posts move to a split child
        duck.load(con, "core.post_item_end", [{"post_id": pid, "item_id": "t19", "ended_on": D, "reason": "split",
                                                "lineage_id": "sp-1"}])
        duck.load(con, "core.post_item_lineage", [{"post_id": pid, "item_id": "t19c", "linked_on": D, "link_market": "NG",
                                                    "lineage_id": "sp-1"}])
    rows = compute(con)
    assert (rows["t19"]["population_posts"], rows["t19"]["known_posts"], rows["t19"]["local_posts"]) == (3, 3, 3)
    assert (rows["t19c"]["population_posts"], rows["t19c"]["local_posts"]) == (2, 2)
    assert rows["t19"]["status"] == rows["t19c"]["status"] == "market_unconfirmed"


def test_a_topic_link_of_one_market_does_not_put_the_post_in_another_markets_population(con):
    """The link names Kenya; the post was also sighted in Nigeria. Nigeria's population of the item must not hold it."""
    key(con, "t20", "NG")
    key(con, "t20", "KE")
    pid = add(con, "t20", geo="NG", conf=0.9, src="ext_region")
    con.execute("DELETE FROM core.post_items WHERE post_id = ?", [pid])
    duck.load(con, "core.post_items", [{"post_id": pid, "item_id": "t20", "via": "cluster",
                                         "linked_on": D - timedelta(days=2), "link_market": "KE"}])
    seen = datetime(2026, 10, 6, 10, tzinfo=UTC)
    duck.load(con, "core.post_observations", [{
        "post_id": pid, "observed_at": seen, "observed_date": seen.date(), "market": "KE", "platform": "tiktok",
        "route": "tiktok/trending", "lane": "sweep", "lane_class": "unbiased_rank", "source_market": None}])
    compute(con)
    got = {r["market"]: r["population_posts"] for r in duck.query(
        con, "SELECT market, population_posts FROM {core}.item_locality WHERE item_id = 't20'", {})}
    assert got == {"NG": 0, "KE": 1}
