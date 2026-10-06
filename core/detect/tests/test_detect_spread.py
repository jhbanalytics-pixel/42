"""Known-answer tests for spread (BUILD.md 2.3): v_item_spread (sql/spread.sql), the creator-tier spread and each
platform's first measured sighting per item, market and day, and v_item_waves.above_half_days (sql/waves.sql).

Spread reads measured lanes only (unbiased_rank and panel, tvf_item_window's measured set): DATA.md section 3.2
says search_presence and watchlist sightings are never spread, because their dates record when 42 searched.
"""

from datetime import date

import pytest
import sqlglot

from .. import sqlrun
from . import duck
from .fixtures import D, creator, day, item_daily, obs, post, run
from .test_detect_states import sql_statements


@pytest.fixture
def con():
    c = duck.connect()
    for stmt in sql_statements("waves.sql") + sqlrun.spread_statements("core", "agent"):
        c.execute(duck.create_statement(stmt))
    yield c
    c.close()


def tiers(nano=0, micro=0, mid=0, macro=0, mega=0):
    return {"nano": nano, "micro": micro, "mid": mid, "macro": macro, "mega": mega}


class Sightings:
    def __init__(self):
        self.n = 0
        self.rows = {"core.post_observations": [], "core.posts": [], "core.post_items": [], "core.creators": []}

    def add(self, item, d, platform="tiktok", lane_class="unbiased_rank", lane="sweep", market="ZA", tier="micro"):
        self.n += 1
        pid = f"{item}-s{self.n}"
        self.rows["core.post_observations"].append(obs(pid, d, lane_class, lane, market=market, platform=platform))
        self.rows["core.posts"].append(post(pid, f"{item}-c{self.n}", d, platform=platform, tier=tier))
        self.rows["core.post_items"].append({"post_id": pid, "item_id": item, "via": "hashtag"})
        self.rows["core.creators"].append(creator(f"{item}-c{self.n}"))
        return pid

    def again(self, pid, d, lane_class="unbiased_rank", lane="sweep", market="ZA"):
        """A later sighting of a post already seen."""
        platform = next(p["platform"] for p in self.rows["core.posts"] if p["post_id"] == pid)
        self.rows["core.post_observations"].append(obs(pid, d, lane_class, lane, market=market, platform=platform))

    def load(self, con):
        for table, rows in self.rows.items():
            duck.load(con, table, rows)


def spread(con):
    rows = duck.query(con, "SELECT s.item_id, s.market, s.metric_date, s.tiers, s.platform_first_seen, s.spread_line "
                           "FROM {core}.v_item_spread s ORDER BY s.item_id, s.market, s.metric_date")
    return {(r["item_id"], r["market"], r["metric_date"]): r for r in rows}


def seen(*pairs):
    return [{"platform": p, "first_seen": d} for p, d in pairs]


# v_item_spread


def test_spread_sql_is_one_bigquery_view_applied_on_its_own():
    stmts = sqlrun.spread_statements()
    assert [sqlrun.object_name(s) for s in stmts] == ["intelligence_42_core.v_item_spread"]
    assert sqlglot.parse_one(duck.split_create(stmts[0])[3], read="bigquery") is not None
    assert "intelligence_42_core.v_item_spread" not in [sqlrun.object_name(s) for s in sqlrun.statements()]


def world(con):
    """Detect ran well on D and day(3) and failed on D + 1."""
    duck.load(con, "agent.runs", [run("detect", D), run("detect", day(3)), run("detect", day(-1), status="failed")])
    s = Sightings()
    first = s.add("sp", day(20), "tiktok")
    s.again(first, day(2))                                     # seen again: still first seen day(20), in no window
    s.add("sp", day(7), "tiktok", tier="nano")
    s.add("sp", day(6), "tiktok")
    s.add("sp", day(6), "tiktok")
    for _ in range(3):
        s.add("sp", D, "tiktok", tier="nano")
    s.add("sp", D, "news", lane_class="panel", lane="panel", tier="macro")
    mega = s.add("sp", D, "tiktok", tier="mega")
    s.rows["core.post_items"].append({"post_id": mega, "item_id": "sp", "via": "sound"})   # one post, two links
    s.add("sp", day(2), None, tier=None)                       # no platform and no tier: neither
    # evidence lanes set neither tiers nor first seen
    s.add("sp", day(4), "youtube", lane_class="search_presence", lane="expansion", tier="mid")
    s.add("sp", day(1), "instagram", lane_class="watchlist", lane="watchlist", tier="mid")
    s.add("sp", day(10), "instagram", lane_class="search_presence", lane="placebo", tier="mid")
    s.add("sp", day(30), "twitter", lane_class="legacy", lane="legacy", tier="mid")
    s.add("sp", day(5), "twitter", lane_class="search_presence", lane="agent_live", tier="mid")
    s.add("sp", day(1), "facebook", lane_class="panel", lane="placebo", tier="mid")          # a guard: never
    s.add("sp", day(1), "reddit", lane_class="unbiased_rank", lane="agent_live", tier="mid")  # a guard: never
    s.add("sp", day(-1), "facebook", tier="mega")               # after the latest good detect day
    s.add("sp", day(3), "tiktok", market="NG", tier="mid")
    found = s.add("sm", day(5), "youtube", lane_class="search_presence", lane="expansion", tier="mid")
    s.again(found, day(1))                                      # found by search, measured from day(1)
    s.add("old", day(8), "tiktok")                              # in the windows of day(6) to day(2)
    s.add("np", day(1), None)                                   # a measured post with no platform
    s.add("gone", day(13), "tiktok")                            # before day(6)'s window
    s.add("ev", day(1), "tiktok", lane_class="search_presence", lane="expansion")   # search only: no spread
    s.load(con)


def test_spread_rows_cover_the_latest_good_detect_day_and_the_6_days_before(con):
    world(con)
    got = spread(con)
    assert sorted({k[2] for k in got}) == [day(i) for i in range(6, -1, -1)]
    assert sorted(k for k in got if k[0] == "sp" and k[1] == "ZA") == [("sp", "ZA", day(i)) for i in range(6, -1, -1)]
    assert sorted(k[2] for k in got if k[0] == "old") == [day(i) for i in range(6, 1, -1)]
    assert sorted(k[2] for k in got if k[0] == "sm") == [day(1), D]
    assert sorted(k[2] for k in got if k[1] == "NG") == [day(i) for i in range(3, -1, -1)]
    assert not [k for k in got if k[0] in ("gone", "ev")]


def test_tiers_count_measured_posts_first_seen_in_the_7_days_ending_metric_date(con):
    world(con)
    got = spread(con)
    assert got[("sp", "ZA", D)]["tiers"] == tiers(nano=3, micro=2, macro=1, mega=1)          # day(6) to D
    assert got[("sp", "ZA", day(1))]["tiers"] == tiers(nano=1, micro=2)                      # day(7) to day(1)
    assert got[("sp", "ZA", day(6))]["tiers"] == tiers(nano=1, micro=2)
    assert got[("sp", "NG", D)]["tiers"] == tiers(mid=1)
    assert got[("sm", "ZA", D)]["tiers"] == tiers(mid=1)
    assert got[("old", "ZA", day(2))]["tiers"] == tiers(micro=1)


def test_platform_first_seen_uses_measured_lanes_only(con):
    world(con)
    got = spread(con)
    assert got[("sp", "ZA", D)]["platform_first_seen"] == seen(("tiktok", day(20)), ("news", D))
    assert got[("sp", "ZA", day(2))]["platform_first_seen"] == seen(("tiktok", day(20)))
    assert got[("sp", "NG", D)]["platform_first_seen"] == seen(("tiktok", day(3)))
    assert got[("sm", "ZA", D)]["platform_first_seen"] == seen(("youtube", day(1)))       # not the search day
    assert got[("np", "ZA", D)]["platform_first_seen"] == []


def test_spread_line_names_each_platform_and_its_first_day(con):
    world(con)
    got = spread(con)
    assert D == date(2026, 9, 20)
    assert got[("sp", "ZA", D)]["spread_line"] == "First seen on TikTok 31 August, news 20 September"
    assert got[("sp", "ZA", day(2))]["spread_line"] == "First seen on TikTok 31 August"
    assert got[("sp", "NG", day(3))]["spread_line"] == "First seen on TikTok 17 September"
    assert got[("sm", "ZA", D)]["spread_line"] == "First seen on YouTube 19 September"
    assert got[("np", "ZA", D)]["spread_line"] is None


def test_spread_line_names_x_once_and_gives_the_year_when_it_differs(con):
    duck.load(con, "agent.runs", [run("detect", D)])
    s = Sightings()
    s.add("yr", date(2025, 12, 30), "tiktok")
    s.add("yr", day(9), "x", lane_class="search_presence", lane="expansion")    # a search: not first seen
    s.add("yr", day(2), "twitter")
    s.add("yr", day(2), "apple_music")
    s.add("yr", day(1), "x", lane_class="panel", lane="panel")                   # X again, later than twitter
    s.load(con)
    r = spread(con)[("yr", "ZA", D)]
    assert r["platform_first_seen"] == seen(("tiktok", date(2025, 12, 30)), ("apple_music", day(2)),
                                            ("twitter", day(2)), ("x", day(1)))
    assert r["spread_line"] == "First seen on TikTok 30 December 2025, Apple Music 18 September, X 18 September"


def test_no_spread_rows_before_any_good_detect_run(con):
    duck.load(con, "agent.runs", [run("detect", D, status="failed")])
    s = Sightings()
    s.add("sp", D)
    s.load(con)
    assert spread(con) == {}


# v_item_waves.above_half_days


RANK = dict(lane_class="unbiased_rank", platform="tiktok", series="feed_tiktok", protocol="p1")
SEARCH = dict(lane_class="search_presence", platform="instagram", series=None, protocol=None)


def test_above_half_days_counts_the_waves_days_at_or_above_half_its_peak(con):
    daily = [
        (100, 9, RANK), (99, 4, RANK), (98, 5, RANK),      # peak 9: 5 is at or above 4.5, 4 is not
        (60, 6, RANK), (59, 3, RANK), (58, 2, RANK),       # half of this wave's peak of 6, not the item's 10
        (10, 4, RANK), (9, 10, RANK), (8, 5, RANK),        # peak 10: 5 is exactly half and counts
        (8, 20, SEARCH),                                   # a placebo search that day: left out, 5 stays
        (7, 4, RANK), (7, 4, SEARCH),                      # a day's posts is its largest lane class, 4 not 8
        (6, 5, RANK),
    ]
    duck.load(con, "core.item_daily", [item_daily("ah", day(i), n, **kw) for i, n, kw in daily])
    duck.load(con, "agent.runs", [run("aggregate", day(i)) for i in {i for i, _, _ in daily}])
    duck.load(con, "core.seed_queue", [{"seed_date": day(8), "market": "ZA", "item_id": "ah", "kind": "hashtag",
                                        "lane": "placebo"}])
    rows = duck.query(con, "SELECT w.wave_start, w.peak_posts, w.above_half_days FROM {core}.v_item_waves w "
                           "WHERE w.item_id = 'ah' ORDER BY w.wave_start")
    assert rows == [{"wave_start": day(100), "peak_posts": 9, "above_half_days": 2},
                    {"wave_start": day(60), "peak_posts": 6, "above_half_days": 2},
                    {"wave_start": day(10), "peak_posts": 10, "above_half_days": 3}]
