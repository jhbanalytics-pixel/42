"""The hourly Breaking rule (ENGINE.md section 3, "Later"): known answers on DuckDB, one runs row per run.

The fixture hour is 14:00 SAST on Tuesday 29 September 2026, so the six-hour window is 09:00 to 15:00 SAST, and
the baseline is the 28 SAST days 1 to 28 September. feed_tiktok is valid on every baseline day in every market,
and base() gives the item 40 posts a day, so the expected six-hour share is 40 * 6 / 24 = 10 posts.
hourly() writes sightings as the pulse does: posts, post_items and post_observations, plus the item_hourly rows
L1's pulse_job.hourly_rows would write for them (distinct posts and creators per pull and hour), which the rule
does not read. BreakingClient adds insert_rows_json for the runs row and a get_table that looks the table up in
DuckDB's catalog and raises NotFound as BigQuery does.
"""

import itertools
import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest
from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from .. import breaking, sqlrun
from . import duck

SAST = timezone(timedelta(hours=2))
HOUR = datetime(2026, 9, 29, 14, tzinfo=SAST)
DAY = date(2026, 9, 29)
BASE_DAYS = [DAY - timedelta(days=n) for n in range(1, 29)]
MARKETS = ("ZA", "NG", "KE", "GLOBAL")


class BreakingClient(duck.Client):
    def __init__(self, con, fail_on=None):
        super().__init__(con)
        self.fail_on = fail_on

    def query(self, sql, job_config=None):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("refused")
        return super().query(sql, job_config)

    def insert_rows_json(self, table, rows):
        duck.load(self.con, table, rows)
        return []

    def get_table(self, ref):
        schema, table = ref.split(".")[-2:]
        found = self.con.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
                                 [schema, table]).fetchall()
        if not found:
            raise NotFound(f"Not found: Table {ref}")
        return bigquery.Table("p." + ref, schema=[])


@pytest.fixture
def con():
    c = duck.connect()
    runs = []
    for d in BASE_DAYS:
        runs += [{"run_id": f"collect-{d}", "stage": "collect", "run_date": d, "status": "ok",
                  "finished_at": datetime(d.year, d.month, d.day, 3, tzinfo=timezone.utc)},
                 {"run_id": f"aggregate-{d}", "stage": "aggregate", "run_date": d, "status": "ok",
                  "finished_at": datetime(d.year, d.month, d.day, 4, tzinfo=timezone.utc)}]
    duck.load(c, "agent.runs", runs)
    valid_days(c, BASE_DAYS)
    yield c
    c.close()


def valid_days(c, days, valid=True):
    duck.load(c, "core.collection_health", [
        {"day": d, "market": m, "platform": "tiktok", "series": "feed_tiktok", "protocol": "p1",
         "lane_class": "unbiased_rank", "valid": valid, "run_id": f"collect-{d}"}
        for d in days for m in MARKETS])


def health(c, platform, lane, days=BASE_DAYS, market="ZA", series=None, valid=True):
    """collection_health rows for one market, platform and lane class; a series has rows only once it runs."""
    duck.load(c, "core.collection_health", [
        {"day": d, "market": market, "platform": platform, "series": series or f"{lane}_{platform}",
         "protocol": "p1", "lane_class": lane, "valid": valid, "run_id": f"collect-{d}"} for d in days])


def base(c, item="i1", market="ZA", platform="tiktok", lane="unbiased_rank", per_day=40, days=BASE_DAYS,
         series=None):
    """item_daily rows; panel rows carry their series, as aggregate.sql writes them."""
    series = (series or f"{lane}_{platform}") if lane == "panel" else None
    duck.load(c, "core.item_daily", [
        {"metric_date": d, "market": market, "platform": platform, "item_id": item, "lane_class": lane,
         "series": series, "protocol": "p1" if series else None,
         "posts": per_day, "creators": per_day, "run_id": f"aggregate-{d}"} for d in days])


_IDS = itertools.count()
_CREATOR = {}


def hourly(c, *rows):
    """rows: (hours before HOUR, posts, creators) with optional keys: item, market, platform, lane (lane class),
    lane_name (post_observations.lane), series, run, pool (creator pool name) and again (post ids to see again
    instead of new posts). New posts take their creators in turn from a pool of that many accounts, shared by the
    item and platform unless pool names another, so a second row with fewer creators adds none. Each sighting is
    10 minutes past its hour. Returns the post ids of each row."""
    out = []
    for r in rows:
        back, n, creators, extra = r[0], r[1], r[2], (r[3] if len(r) > 3 else {})
        market, item = extra.get("market", "ZA"), extra.get("item", "i1")
        platform, lane = extra.get("platform", "tiktok"), extra.get("lane", "unbiased_rank")
        series = extra.get("series") or ("feed_tiktok" if (platform, lane) == ("tiktok", "unbiased_rank")
                                          else f"{lane}_{platform}")
        at = HOUR - timedelta(hours=back) + timedelta(minutes=10)
        ids = list(extra.get("again") or [])
        if not ids:
            pool = extra.get("pool", "c")
            ids = [f"{item}-{platform}-{next(_IDS)}" for _ in range(n)]
            for j, pid in enumerate(ids):
                _CREATOR[pid] = f"{platform}-{item}-{pool}{j % creators}"
            duck.load(c, "core.posts", [{"post_id": pid, "platform": platform, "creator_id": _CREATOR[pid],
                                         "post_date": at.astimezone(SAST).date()} for pid in ids])
            duck.load(c, "core.post_items", [{"post_id": pid, "item_id": item, "via": "hashtag"} for pid in ids])
        duck.load(c, "core.post_observations", [
            {"post_id": pid, "observed_at": at, "observed_date": at.astimezone(SAST).date(), "market": market,
             "platform": platform, "series": series, "protocol": "p1", "lane": extra.get("lane_name", "sweep"),
             "lane_class": lane, "run_id": extra.get("run", "pulse-1")} for pid in ids])
        duck.load(c, "core.item_hourly", [
            {"market": market, "item_id": item, "platform": platform,
             "hour": HOUR - timedelta(hours=back), "posts": len(ids),
             "creators": len({_CREATOR[pid] for pid in ids}), "lane_class": lane,
             "run_id": extra.get("run", "pulse-1")}])
        out.append(ids)
    return out


def compute(c):
    return {(r["market"], r["item_id"]): r for r in breaking.compute(BreakingClient(c), HOUR, core="core",
                                                                     agent="agent")}


def flagged(c):
    return {k for k, r in compute(c).items() if r["breaking"]}


def signals(c):
    return duck.query(c, "SELECT * FROM {core}.breaking_signals s ORDER BY s.run_id, s.market, s.item_id")


def breaking_runs(c):
    return duck.query(c, "SELECT * FROM {agent}.runs r WHERE r.stage = 'breaking' ORDER BY r.finished_at")


def main(c, argv=("--hour", "2026-09-29T14:00"), **kw):
    return breaking.main(list(argv), client=BreakingClient(c, **kw), core="core", agent="agent")


# The known answers


def test_an_item_at_3_2_times_its_share_with_9_creators_on_an_unbiased_feed_flags(con):
    base(con)
    hourly(con, (2, 20, 9), (0, 12, 5))
    row = compute(con)[("ZA", "i1")]
    assert (row["posts6"], row["creators6"], row["platforms"], row["unbiased"]) == (32, 9, 1, True)
    assert row["expected6"] == pytest.approx(10.0)
    assert row["ratio"] == pytest.approx(3.2)
    assert row["baseline_days"] == 28
    assert row["breaking"] is True


def test_at_2_9_times_it_does_not_flag(con):
    base(con)
    hourly(con, (2, 20, 9), (0, 9, 5))
    row = compute(con)[("ZA", "i1")]
    assert row["ratio"] == pytest.approx(2.9)
    assert row["breaking"] is False


def test_exactly_3_times_flags(con):
    base(con)
    hourly(con, (1, 30, 8))
    assert flagged(con) == {("ZA", "i1")}


def test_with_7_creators_it_does_not_flag(con):
    base(con)
    hourly(con, (2, 20, 7), (0, 12, 5))
    row = compute(con)[("ZA", "i1")]
    assert (row["posts6"], row["creators6"]) == (32, 7)
    assert row["breaking"] is False


def test_the_same_creators_in_several_hours_count_once(con):
    base(con)
    hourly(con, (0, 8, 4), (1, 8, 4), (2, 8, 4), (3, 8, 4))
    row = compute(con)[("ZA", "i1")]
    assert (row["posts6"], row["creators6"]) == (32, 4)
    assert row["breaking"] is False


def test_different_creators_in_several_hours_add_up_to_their_distinct_count(con):
    # 4 new accounts an hour for 4 hours are 16 distinct creators; no single hour reaches 8.
    base(con)
    hourly(con, *[(h, 8, 4, {"pool": f"hour{h}-"}) for h in range(4)])
    row = compute(con)[("ZA", "i1")]
    assert (row["posts6"], row["creators6"]) == (32, 16)
    assert row["breaking"] is True


def test_on_one_platform_outside_an_unbiased_feed_it_does_not_flag(con):
    health(con, "facebook", "panel")
    base(con, platform="facebook", lane="panel")
    hourly(con, (2, 20, 9, {"platform": "facebook", "lane": "panel"}), (0, 12, 5, {"platform": "facebook",
                                                                                   "lane": "panel"}))
    row = compute(con)[("ZA", "i1")]
    assert (row["posts6"], row["creators6"], row["platforms"], row["unbiased"]) == (32, 9, 1, False)
    assert row["ratio"] == pytest.approx(3.2)
    assert row["breaking"] is False


def test_on_two_platforms_it_flags_without_an_unbiased_feed(con):
    health(con, "facebook", "panel")
    health(con, "x", "panel")
    base(con, platform="facebook", lane="panel")
    hourly(con, (2, 16, 5, {"platform": "facebook", "lane": "panel"}), (1, 16, 4, {"platform": "x",
                                                                                   "lane": "panel"}))
    row = compute(con)[("ZA", "i1")]
    assert (row["posts6"], row["creators6"], row["platforms"], row["unbiased"]) == (32, 9, 2, False)
    assert row["breaking"] is True


def test_placebo_search_and_other_unmeasured_lanes_are_excluded(con):
    base(con)
    hourly(con, (2, 20, 9), (0, 9, 5),
           (1, 30, 30, {"lane": "search_presence"}), (1, 30, 30, {"lane": "placebo", "platform": "instagram"}),
           (1, 30, 30, {"lane": "agent_live", "platform": "x"}), (1, 30, 30, {"lane": "watchlist"}),
           (1, 30, 30, {"lane": "legacy"}), (1, 30, 30, {"lane": "unbiased_counter"}),
           (1, 30, 30, {"lane_name": "placebo"}), (1, 30, 30, {"lane_name": "agent_live", "platform": "youtube"}),
           (0, 50, 50, {"item": "found_by_search", "lane": "search_presence"}),
           (0, 50, 50, {"item": "placebo_only", "lane": "search_presence", "lane_name": "placebo"}))
    rows = compute(con)
    assert set(rows) == {("ZA", "i1")}
    row = rows[("ZA", "i1")]
    assert (row["posts6"], row["creators6"], row["platforms"]) == (29, 9, 1)
    assert row["breaking"] is False


def test_placebo_and_search_rows_do_not_count_as_a_second_platform(con):
    health(con, "facebook", "panel")
    base(con, platform="facebook", lane="panel")
    hourly(con, (2, 32, 9, {"platform": "facebook", "lane": "panel"}),
           (1, 5, 5, {"platform": "x", "lane": "search_presence"}), (1, 5, 5, {"platform": "x", "lane": "placebo"}))
    row = compute(con)[("ZA", "i1")]
    assert (row["platforms"], row["breaking"]) == (1, False)


def test_global_is_excluded(con):
    base(con, market="GLOBAL")
    hourly(con, (2, 200, 90, {"market": "GLOBAL"}))
    assert compute(con) == {}


def test_each_market_is_judged_on_its_own_baseline(con):
    base(con, market="ZA")
    base(con, market="KE", per_day=200)
    hourly(con, (2, 32, 9), (2, 32, 9, {"market": "KE"}))
    rows = compute(con)
    assert rows[("ZA", "i1")]["breaking"] is True
    assert rows[("KE", "i1")]["expected6"] == pytest.approx(50.0)
    assert rows[("KE", "i1")]["breaking"] is False


def test_only_the_six_hours_ending_with_the_hour_count(con):
    base(con)
    hourly(con, (5, 16, 9), (0, 16, 4), (6, 100, 50), (-1, 100, 50))
    row = compute(con)[("ZA", "i1")]
    assert (row["posts6"], row["creators6"]) == (32, 9)
    assert row["breaking"] is True


def test_a_re_read_of_the_same_hour_counts_once(con):
    base(con)
    [ids] = hourly(con, (2, 20, 9))
    hourly(con, (2, 0, 0, {"again": ids, "run": "pulse-2"}), (2, 0, 0, {"again": ids[:18], "run": "pulse-3"}))
    row = compute(con)[("ZA", "i1")]
    assert (row["posts6"], row["creators6"]) == (20, 9)
    assert row["breaking"] is False


def test_the_baseline_leaves_out_invalid_days_and_unmeasured_lanes(con):
    # 10 of the 28 days turn invalid; their 40 posts leave the baseline and the mean stays 40 a day.
    con.execute("UPDATE core.collection_health SET valid = FALSE WHERE day < DATE '2026-09-11'")
    base(con)
    base(con, lane="search_presence", per_day=400)
    base(con, lane="watchlist", per_day=400)
    base(con, lane="_any", platform="_all", per_day=400)
    hourly(con, (2, 32, 9))
    row = compute(con)[("ZA", "i1")]
    assert row["baseline_days"] == 18
    assert row["expected6"] == pytest.approx(10.0)
    assert row["breaking"] is True


def test_a_market_with_fewer_than_14_valid_baseline_days_is_not_judged(con):
    con.execute("UPDATE core.collection_health SET valid = FALSE WHERE day < DATE '2026-09-16'")
    base(con)
    hourly(con, (2, 100, 20))
    row = compute(con)[("ZA", "i1")]
    assert row["baseline_days"] == 13
    assert row["expected6"] is None and row["ratio"] is None
    assert row["breaking"] is False


def test_an_item_with_no_baseline_posts_flags_on_creators_and_sightings(con):
    hourly(con, (2, 12, 9, {"item": "new"}))
    row = compute(con)[("ZA", "new")]
    assert row["expected6"] == pytest.approx(0.0)
    assert row["ratio"] is None
    assert row["breaking"] is True


def test_invalid_days_of_the_items_own_platform_are_not_zeros(con):
    # feed_tiktok is invalid on 10 of the 28 days while list_reddit stays valid on all 28. The item ran at 40
    # posts a day on every day its own feed was valid, so expected6 is 10 and today's 20 posts are 2 times it.
    con.execute("UPDATE core.collection_health SET valid = FALSE WHERE day < DATE '2026-09-11'")
    health(con, "reddit", "unbiased_rank")
    base(con, days=[d for d in BASE_DAYS if d >= date(2026, 9, 11)])
    hourly(con, (2, 20, 9))
    row = compute(con)[("ZA", "i1")]
    assert row["baseline_days"] == 18
    assert row["expected6"] == pytest.approx(10.0)
    assert row["ratio"] == pytest.approx(2.0)
    assert row["breaking"] is False


def test_a_series_under_14_valid_days_old_has_no_baseline(con):
    # panel_fb_hub started 7 days ago and x has no collection at all; the market's feed ran all 28 days.
    recent = [d for d in BASE_DAYS if d >= date(2026, 9, 22)]
    health(con, "facebook", "panel", days=recent)
    base(con, platform="facebook", lane="panel", days=recent, per_day=40)
    base(con, platform="x", lane="panel", days=recent, per_day=0)
    hourly(con, (2, 6, 5, {"platform": "facebook", "lane": "panel"}), (1, 4, 4, {"platform": "x", "lane": "panel"}))
    row = compute(con)[("ZA", "i1")]
    assert row["baseline_days"] == 0
    assert row["expected6"] is None and row["ratio"] is None
    assert row["breaking"] is False


def test_a_young_series_alone_reports_its_own_day_count(con):
    recent = [d for d in BASE_DAYS if d >= date(2026, 9, 22)]
    health(con, "facebook", "panel", days=recent)
    base(con, platform="facebook", lane="panel", days=recent, per_day=40)
    hourly(con, (2, 60, 9, {"platform": "facebook", "lane": "panel"}), (1, 60, 9, {"platform": "x", "lane": "panel"}))
    health(con, "x", "panel")
    row = compute(con)[("ZA", "i1")]
    assert row["baseline_days"] == 7
    assert row["expected6"] is None and row["breaking"] is False


def test_a_day_counts_only_when_every_series_the_items_posts_came_through_was_valid(con):
    # the item's new posts came through board_youtube and a second youtube list; the second fails on 10 days.
    health(con, "youtube", "unbiased_rank", series="board_youtube")
    health(con, "youtube", "unbiased_rank", series="list_youtube_2", days=BASE_DAYS[:10], valid=False)
    health(con, "youtube", "unbiased_rank", series="list_youtube_2", days=BASE_DAYS[10:])
    base(con, platform="youtube", days=BASE_DAYS[10:])
    hourly(con, (2, 15, 9, {"platform": "youtube", "series": "board_youtube"}),
           (2, 15, 9, {"platform": "youtube", "series": "list_youtube_2"}))
    row = compute(con)[("ZA", "i1")]
    assert row["baseline_days"] == 18
    assert row["expected6"] == pytest.approx(10.0)
    assert row["breaking"] is True


def test_the_expected_share_adds_up_the_items_platforms_in_the_window(con):
    # tiktok feed 40 a day and facebook panel 20 a day give expected6 (40 + 20) * 6 / 24 = 15. x panel carries
    # 400 a day in the baseline but has no posts in the window, so it is not part of the comparison.
    for platform in ("facebook", "x"):
        health(con, platform, "panel")
    base(con)
    base(con, platform="facebook", lane="panel", per_day=20)
    base(con, platform="x", lane="panel", per_day=400)
    hourly(con, (2, 30, 5), (1, 16, 4, {"platform": "facebook", "lane": "panel"}))
    row = compute(con)[("ZA", "i1")]
    assert (row["posts6"], row["creators6"], row["platforms"]) == (46, 9, 2)
    assert row["expected6"] == pytest.approx(15.0)
    assert row["ratio"] == pytest.approx(46 / 15)
    assert row["breaking"] is True


# Each post once, at its first sighting (reviewer round 3: item_hourly counts a post at every pull that shows it)


def test_on_the_pulse_launch_day_a_flat_item_does_not_flag(con):
    # 30 new posts a day, so expected6 is 7.5. The morning collect saw 10 posts at 02:00; the 09:00 pull shows
    # those 10 and 2 new ones, and the 12:00 pull the same 12. item_hourly reads 24 posts, 3.2 times; 2 are new.
    base(con, per_day=30)
    [morning] = hourly(con, (12, 10, 10, {"run": "collect-1"}))
    [nine] = hourly(con, (5, 2, 2, {"pool": "new"}))
    hourly(con, (5, 0, 0, {"again": morning}), (2, 0, 0, {"again": morning + nine}))
    row = compute(con)[("ZA", "i1")]
    assert (row["posts6"], row["creators6"]) == (2, 2)
    assert row["expected6"] == pytest.approx(7.5)
    assert row["breaking"] is False


def test_the_same_posts_seen_again_are_not_new_posts(con):
    # 3 new posts a day; the same 10 posts from yesterday are on the feed at 09:00 and 12:00 (item_hourly 26.7x).
    base(con, per_day=3)
    [old] = hourly(con, (24, 10, 10, {"run": "collect-0"}))
    hourly(con, (5, 0, 0, {"again": old}), (2, 0, 0, {"again": old}))
    assert ("ZA", "i1") not in compute(con)


def test_a_flat_flow_seen_at_two_pulls_counts_once(con):
    # 10 new posts a day, expected6 2.5. 8 were seen at 02:00; 09:00 shows them and 2 new, 12:00 the same 10
    # (item_hourly 8.0x). The window has 2 new posts.
    base(con, per_day=10)
    [morning] = hourly(con, (12, 8, 8, {"run": "collect-1"}))
    [nine] = hourly(con, (5, 2, 2, {"pool": "new"}))
    hourly(con, (5, 0, 0, {"again": morning}), (2, 0, 0, {"again": morning + nine}))
    row = compute(con)[("ZA", "i1")]
    assert row["posts6"] == 2 and row["ratio"] == pytest.approx(0.8)
    assert row["breaking"] is False


def test_item_hourly_is_not_read(con):
    base(con)
    duck.load(con, "core.item_hourly", [{"market": "ZA", "item_id": "i1", "platform": "tiktok", "hour": HOUR,
                                         "posts": 500, "creators": 90, "lane_class": "unbiased_rank",
                                         "run_id": "pulse-1"}])
    assert compute(con) == {}


def test_a_post_first_seen_in_the_window_on_another_lane_counts_there_once(con):
    # first seen in search this morning, first seen on the feed at 12:00: new to the feed, as in item_daily.
    base(con)
    [found] = hourly(con, (12, 32, 9, {"lane": "search_presence", "lane_name": "confirm"}))
    hourly(con, (2, 0, 0, {"again": found}))
    row = compute(con)[("ZA", "i1")]
    assert (row["posts6"], row["creators6"], row["breaking"]) == (32, 9, True)


def test_an_agent_live_sighting_before_the_window_does_not_take_the_first_sighting(con):
    base(con)
    [ids] = hourly(con, (12, 32, 9, {"lane_name": "agent_live"}))
    hourly(con, (2, 0, 0, {"again": ids}))
    assert compute(con)[("ZA", "i1")]["posts6"] == 32


def test_a_post_without_post_items_is_not_counted(con):
    base(con)
    [ids] = hourly(con, (2, 32, 9))
    con.execute("UPDATE core.post_items SET item_id = 'other' WHERE list_contains(?, post_id)", [ids[:10]])
    assert compute(con)[("ZA", "i1")]["posts6"] == 22


def test_a_sibling_series_that_ran_longer_does_not_add_days(con):
    # reviewer note 1, case 4: board_tiktok_hashtag ran all 28 days, feed_tiktok only the last 14. The item came
    # through the feed at 40 a day, so expected6 is 10 and 20 posts are 2 times it.
    con.execute("UPDATE core.collection_health SET market = 'ZZ'")
    health(con, "tiktok", "unbiased_rank", series="board_tiktok_hashtag")
    health(con, "tiktok", "unbiased_rank", series="feed_tiktok", days=BASE_DAYS[:14])
    base(con, days=BASE_DAYS[:14])
    hourly(con, (2, 20, 9))
    row = compute(con)[("ZA", "i1")]
    assert row["baseline_days"] == 14
    assert row["expected6"] == pytest.approx(10.0)
    assert row["breaking"] is False


def test_a_panel_with_no_platform_in_collection_health_is_judged(con):
    # the culture desk panel (prism/profiles) has health rows with a NULL platform; they cover every platform
    # its posts are on.
    duck.load(con, "core.collection_health", [
        {"day": d, "market": "ZA", "platform": None, "series": "panel_culture_desk", "protocol": "p1",
         "lane_class": "panel", "valid": True, "run_id": f"collect-{d}"} for d in BASE_DAYS])
    base(con, platform="instagram", lane="panel", series="panel_culture_desk")
    base(con, platform="x", lane="panel", per_day=0, series="panel_culture_desk")
    desk = {"lane": "panel", "series": "panel_culture_desk"}
    hourly(con, (2, 16, 5, {**desk, "platform": "instagram"}), (1, 16, 4, {**desk, "platform": "x"}))
    row = compute(con)[("ZA", "i1")]
    assert row["baseline_days"] == 28
    assert row["expected6"] == pytest.approx(10.0)
    assert (row["posts6"], row["creators6"], row["platforms"]) == (32, 9, 2)
    assert row["breaking"] is True


def test_a_panel_baseline_counts_only_the_series_the_new_posts_came_through(con):
    # facebook has two panels. The item's new posts came through panel_fb_hub (40 a day); its 400 a day through
    # the culture desk on facebook are another series and stay out of the baseline.
    health(con, "facebook", "panel", series="panel_fb_hub")
    health(con, "facebook", "panel", series="panel_culture_desk")
    base(con, platform="facebook", lane="panel", series="panel_fb_hub")
    base(con, platform="facebook", lane="panel", series="panel_culture_desk", per_day=400)
    hourly(con, (2, 32, 9, {"platform": "facebook", "lane": "panel", "series": "panel_fb_hub"}))
    row = compute(con)[("ZA", "i1")]
    assert row["expected6"] == pytest.approx(10.0)
    assert row["ratio"] == pytest.approx(3.2)


# The current view


def test_the_current_view_keeps_only_rows_of_ok_breaking_runs(con):
    con.execute(duck.create_statement(duck.strip_leading_comments(
        sqlrun.render(breaking.CURRENT_VIEW_SQL, core="core", agent="agent"))))
    duck.load(con, "core.breaking_signals", [
        {"hour": HOUR, "market": "ZA", "item_id": item, "run_id": run, "rule_version": breaking.RULE_VERSION}
        for item, run in (("i1", "breaking-ok"), ("i2", "breaking-failed"), ("i3", "breaking-no-runs-row"),
                          ("i4", "breaking-ok-other-stage"))])
    duck.load(con, "agent.runs", [
        {"run_id": "breaking-ok", "stage": "breaking", "run_date": DAY, "status": "ok"},
        {"run_id": "breaking-failed", "stage": "breaking", "run_date": DAY, "status": "failed"},
        {"run_id": "breaking-ok-other-stage", "stage": "detect", "run_date": DAY, "status": "ok"}])
    rows = duck.query(con, "SELECT * FROM {core}.v_breaking_signals_current v")
    assert [r["item_id"] for r in rows] == ["i1"]
    assert set(rows[0]) == {f for f, _ in breaking.FIELDS}


# The job


def test_the_job_appends_the_flagged_items_and_one_runs_row(con, capsys):
    base(con)
    base(con, item="i2")
    hourly(con, (2, 32, 9), (2, 29, 9, {"item": "i2"}))
    assert main(con) == 0
    [row] = signals(con)
    [run] = breaking_runs(con)
    assert row["hour"] == HOUR
    assert (row["market"], row["item_id"], row["posts6"], row["creators6"], row["platforms"]) == ("ZA", "i1", 32, 9, 1)
    assert row["expected6"] == pytest.approx(10.0) and row["ratio"] == pytest.approx(3.2)
    assert row["run_id"] == run["run_id"] and row["rule_version"] == breaking.RULE_VERSION
    assert (run["status"], run["run_date"], run["error"]) == ("ok", DAY, None)
    assert run["run_id"].startswith("breaking-20260929-")
    counts = json.loads(run["counts"])
    assert counts["model_usd"] == 0.0
    assert counts["hour"] == "2026-09-29T14:00:00+02:00"
    assert counts["candidates"] == 2 and counts["written"] == [["ZA", "i1"]]
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "ok" and printed["breaking"] == "2026-09-29T14:00:00+02:00"


def test_a_rerun_of_the_same_hour_adds_no_duplicates(con):
    base(con)
    hourly(con, (2, 32, 9))
    assert main(con) == 0
    assert main(con) == 0
    assert len(signals(con)) == 1
    first, second = breaking_runs(con)
    assert (first["status"], second["status"]) == ("ok", "ok")
    counts = json.loads(second["counts"])
    assert counts["already_written"] == [["ZA", "i1"]] and counts["written"] == []


def test_a_rerun_adds_an_item_that_crossed_the_line_later(con):
    base(con)
    base(con, item="i2")
    hourly(con, (2, 32, 9), (2, 29, 9, {"item": "i2"}))
    assert main(con) == 0
    hourly(con, (1, 3, 1, {"item": "i2", "run": "pulse-2"}))
    assert main(con) == 0
    assert sorted((r["market"], r["item_id"]) for r in signals(con)) == [("ZA", "i1"), ("ZA", "i2")]
    first, second = breaking_runs(con)
    assert json.loads(second["counts"])["written"] == [["ZA", "i2"]]


def test_rows_of_a_failed_run_do_not_count_as_written(con):
    base(con)
    hourly(con, (2, 32, 9))
    duck.load(con, "core.breaking_signals", [{"hour": HOUR, "market": "ZA", "item_id": "i1", "run_id": "breaking-x",
                                              "rule_version": breaking.RULE_VERSION}])
    duck.load(con, "agent.runs", [{"run_id": "breaking-x", "stage": "breaking", "run_date": DAY,
                                   "status": "failed"}])
    assert main(con) == 0
    assert json.loads(breaking_runs(con)[-1]["counts"])["written"] == [["ZA", "i1"]]


def test_an_hour_with_nothing_breaking_writes_an_ok_runs_row_and_no_signal(con):
    base(con)
    hourly(con, (2, 10, 9))
    assert main(con) == 0
    assert signals(con) == []
    [run] = breaking_runs(con)
    assert run["status"] == "ok" and json.loads(run["counts"])["written"] == []


def hide_table(c):
    c.execute("ALTER TABLE core.breaking_signals RENAME TO breaking_signals_hidden")


def test_a_missing_table_prints_the_rows_records_skipped_and_exits_0(con, capsys):
    base(con)
    hourly(con, (2, 32, 9))
    hide_table(con)
    client = BreakingClient(con)
    assert breaking.main(["--hour", "2026-09-29T14:00"], client=client, core="core", agent="agent") == 0
    [run] = breaking_runs(con)
    assert run["status"] == "skipped"
    assert "core.breaking_signals does not exist" in run["error"]
    assert json.loads(run["counts"])["written"] == []
    assert not any(sql.lstrip().upper().startswith("INSERT") for sql in client.sql)
    assert duck.query(con, "SELECT * FROM {core}.breaking_signals_hidden") == []
    printed = json.loads(capsys.readouterr().out)
    assert [(r["market"], r["item_id"]) for r in printed["rows"]] == [("ZA", "i1")]


def test_a_failure_appends_a_failed_runs_row_and_exits_1(con):
    base(con)
    hourly(con, (2, 32, 9))
    assert main(con, fail_on="post_observations") == 1
    [run] = breaking_runs(con)
    assert run["status"] == "failed" and "refused" in run["error"]
    assert json.loads(run["counts"])["model_usd"] == 0.0
    assert signals(con) == []


def test_the_job_only_reads_and_appends(con):
    base(con)
    hourly(con, (2, 32, 9))
    client = BreakingClient(con)
    assert breaking.main(["--hour", "2026-09-29T14:00"], client=client, core="core", agent="agent") == 0
    words = ("MERGE", "UPDATE", "DELETE", "CREATE", "DROP", "TRUNCATE", "REPLACE")
    assert not any(w in sql.upper() for sql in client.sql for w in words)
    assert [sql.lstrip().split()[0].upper() for sql in client.sql].count("INSERT") == 1


# The hour


@pytest.mark.parametrize("text, hour", [
    ("2026-09-29T14:00", HOUR),
    ("2026-09-29T14", HOUR),
    ("2026-09-29 14:00", HOUR),
    ("2026-09-29T12:00+00:00", HOUR),
    ("2026-09-29T00:00", datetime(2026, 9, 29, 0, tzinfo=SAST)),
])
def test_the_hour_is_read_in_sast_unless_it_names_its_zone(text, hour):
    assert breaking.parse_hour(text) == hour


@pytest.mark.parametrize("text", ["2026-09-29T14:30", "2026-09-29T14:00:05", "2026-09-29", "14:00"])
def test_an_hour_not_on_the_hour_is_refused(con, text):
    with pytest.raises(SystemExit) as e:
        main(con, ("--hour", text))
    assert e.value.code == 2
    assert breaking_runs(con) == []


@pytest.mark.parametrize("now, hour", [
    (datetime(2026, 9, 29, 13, 5, tzinfo=timezone.utc), HOUR),                          # 15:05 SAST
    (datetime(2026, 9, 29, 12, 59, tzinfo=timezone.utc), HOUR - timedelta(hours=1)),    # 14:59 SAST
    (datetime(2026, 9, 29, 22, 30, tzinfo=timezone.utc), datetime(2026, 9, 29, 23, tzinfo=SAST)),  # 00:30 SAST
])
def test_the_default_hour_is_the_last_complete_sast_hour(now, hour):
    assert breaking.hour_for(now) == hour


def test_without_hour_the_job_judges_the_last_complete_hour(con):
    base(con)
    hourly(con, (2, 32, 9))
    now = datetime(2026, 9, 29, 13, 5, tzinfo=timezone.utc)
    assert breaking.main([], client=BreakingClient(con), now=now, core="core", agent="agent") == 0
    [row] = signals(con)
    assert row["hour"] == HOUR


# BigQuery dry run on staging (F42_BQ=1): parse, resolve names and plan against item_hourly; nothing written.


@pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")
def test_bigquery_dry_run():
    client = bigquery.Client(project="ogilvy-trends-v2")
    config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False,
                                     query_parameters=breaking.params(HOUR))
    result = client.query(sqlrun.render(breaking.SQL.read_text(encoding="utf-8")), job_config=config)
    assert result.dry_run


@pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")
def test_bigquery_dry_run_of_the_current_view_body():
    client = bigquery.Client(project="ogilvy-trends-v2")
    if not breaking.table_exists(client):
        pytest.skip("intelligence_42_core.breaking_signals does not exist on staging yet (lane L1)")
    stmt = duck.strip_leading_comments(sqlrun.render(breaking.CURRENT_VIEW_SQL))
    _, _, _, body = duck.split_create(stmt)
    config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    assert client.query(body, job_config=config).dry_run
