"""Creator-breakout detector (BUILD.md 2.13): fixture tests with known answers, run through breakout.sql on DuckDB."""

import os
import re
from datetime import timedelta

import pytest

from core.detect import breakout, sqlrun
from core.detect.items import item_id

from . import duck
from .fixtures import D, at, day, post

SOUND = item_id("sound", "tiktok:s1")
OTHER_SOUND = item_id("sound", "tiktok:s2")
FORMAT = item_id("format", "duet reply")
FYP = item_id("hashtag", "fyp")
USUAL = [400, 500, 600, 500, 500]          # median 500
SAME = object()


def cmap(iid, kind, key, status="active"):
    return {"item_id": iid, "kind": kind, "canonical_key": key, "label": key, "status": status,
            "valid_from": at(day(400)), "valid_to": None}


class World:
    def __init__(self):
        self.posts, self.obs, self.items, self.creators = [], [], [], []
        self.published = {}
        self.cmap = [cmap(SOUND, "sound", "tiktok:s1"), cmap(OTHER_SOUND, "sound", "tiktok:s2"),
                     cmap(FORMAT, "format", "duet reply"), cmap(FYP, "hashtag", "fyp")]

    def add(self, pid, creator_id, d, views, *, platform="tiktok", tier="micro", market="ZA", items=(),
            lag_h=3, geo=SAME, geo_confidence=0.9, **sighting):
        """A post published at 09:00 on d, first read lag_h hours later in market; geo defaults to market."""
        published = at(d, 9)
        self.published[pid] = published
        self.posts.append(post(pid, creator_id, d, platform=platform, tier=tier, published_at=published,
                               geo_market=market if geo is SAME else geo, geo_confidence=geo_confidence))
        self.read(pid, views, lag_h, market=market, platform=platform, **sighting)
        self.items += [{"post_id": pid, "item_id": i, "via": "sound"} for i in items]

    def read(self, pid, views, lag_h, *, market="ZA", platform="tiktok", lane_class="unbiased_rank", lane="sweep",
             route="tiktok/trending", series="feed_tiktok"):
        when = self.published[pid] + timedelta(hours=lag_h)
        self.obs.append({"post_id": pid, "observed_at": when, "observed_date": when.date(), "market": market,
                         "platform": platform, "route": route, "series": series, "lane": lane,
                         "lane_class": lane_class, "views": views, "run_id": f"collect-{when:%Y%m%d}"})

    def reread(self, pid, views, lag_h, market="ZA"):
        self.read(pid, views, lag_h, market=market, lane_class="unbiased_counter", lane="watchlist",
                  route="prism/post-stats", series="counter_post_views")

    def history(self, creator_id, views=USUAL, *, platform="tiktok", start=40, **kw):
        for i, v in enumerate(views):
            self.add(f"{platform}-{creator_id}-h{i}", creator_id, day(start - i), v, platform=platform, **kw)

    def breaker(self, creator_id, views=2000, *, d=None, items=(SOUND,), lag_h=3, prior_lag_h=None, **kw):
        """A creator with the usual history and one window post; returns the window post id."""
        platform = kw.get("platform", "tiktok")
        self.history(creator_id, platform=platform, lag_h=lag_h if prior_lag_h is None else prior_lag_h)
        pid = f"{platform}-{creator_id}-w"
        self.add(pid, creator_id, d or day(1), views, items=items, lag_h=lag_h, **kw)
        return pid

    def load(self, con):
        duck.load(con, "core.posts", self.posts)
        duck.load(con, "core.post_observations", self.obs)
        duck.load(con, "core.post_items", self.items)
        duck.load(con, "core.cultural_map", self.cmap)
        duck.load(con, "core.creators", self.creators)
        return con

    def run(self, d=D):
        client = duck.Client(self.load(duck.connect(views=False)))
        out = breakout.run_breakout(client, d, "detect-20260920", "r1", core="core", agent="agent")
        out["sql"] = client.sql
        return out


def breakouts(out):
    return {b["post_id"]: b for b in out["breakouts"]}


def creator_row(creator_id, platform="tiktok", handle=None, home_market=None, coord_score=0):
    return {"creator_id": creator_id, "platform": platform, "handle": handle or creator_id,
            "home_market": home_market, "coord_score": coord_score}


# (1) Breakout posts

def test_usual_views_is_the_median_of_five_or_more_prior_posts():
    assert breakout.usual_views([400, 500, 600, 500, 500]) == 500
    assert breakout.usual_views([100, 200, 300, 400, 500, 600]) == 350
    assert breakout.usual_views([100, 200, 300, 400]) is None


def test_is_breakout_needs_three_times_usual_and_a_thousand_views():
    assert breakout.is_breakout(1500, 500)
    assert not breakout.is_breakout(1499, 500)
    assert not breakout.is_breakout(900, 200)
    assert breakout.is_breakout(1000, 0)
    assert not breakout.is_breakout(5000, None)
    assert not breakout.is_breakout(None, 500)


def test_a_post_at_three_times_its_creators_median_is_a_breakout():
    w = World()
    exact = w.breaker("a", 1500)
    under = w.breaker("b", 1499)
    w.history("c", [100, 200, 200, 300, 200])
    small = "tiktok-c-w"
    w.add(small, "c", day(1), 900, items=(SOUND,))
    got = breakouts(w.run())
    assert set(got) == {exact}
    assert (got[exact]["views"], got[exact]["usual_views"], got[exact]["ratio"]) == (1500, 500, 3.0)
    assert under not in got and small not in got


def test_null_lanes_keep_breakout_baselines_and_first_readings():
    w = World()
    pids = three_breakers(w, lane=None)

    out = w.run()

    assert set(breakouts(out)) == set(pids)
    assert len(out["signals"]) == 1


def test_null_lane_sightings_place_breakouts_in_the_other_market():
    w = World()
    pids = [w.breaker(c, geo="KE") for c in ("a", "b", "c")]
    for pid in pids:
        w.read(pid, None, 4, market="KE", lane=None)

    out = w.run()

    assert set(breakouts(out)) == set(pids)
    assert [(row["market"], row["creators"]) for row in out["signals"]] == [("KE", 3)]


def test_creators_with_fewer_than_five_prior_posts_are_not_judged():
    w = World()
    w.history("a", [100, 100, 100, 100])
    w.add("tiktok-a-w", "a", day(1), 50000, items=(SOUND,))
    assert w.run()["breakouts"] == []


def test_prior_posts_are_published_before_the_post():
    w = World()
    w.history("a", [500, 500, 500, 500])
    w.add("tiktok-a-later", "a", day(1), 500)
    w.add("tiktok-a-w", "a", day(2), 2000, items=(SOUND,))
    assert "tiktok-a-w" not in breakouts(w.run())


def test_prior_posts_come_from_measured_lanes_and_the_creator_profile_route():
    w = World()
    w.history("a", [500, 500, 500, 500])
    w.add("tiktok-a-profile", "a", day(20), 500, lane_class="watchlist", lane="watchlist",
          route="tiktok/profile/videos", series="watch")
    w.add("tiktok-a-w", "a", day(1), 2000, items=(SOUND,))
    got = breakouts(w.run())
    assert "tiktok-a-w" in got and got["tiktok-a-w"]["usual_views"] == 500

    for extra in (dict(lane_class="search_presence", lane="expansion", route="tiktok/search/top", series="search"),
                  dict(lane_class="watchlist", lane="watchlist", route="tiktok/song/videos", series="watch"),
                  dict(lane_class="legacy", lane="legacy", route=None, series="legacy")):
        v = World()
        v.history("a", [500, 500, 500])
        v.add("tiktok-a-panel", "a", day(21), 500, lane_class="panel", lane="panel", route="prism/profiles",
              series="panel_culture_desk")
        v.add("tiktok-a-x", "a", day(20), 500, **extra)
        v.add("tiktok-a-w", "a", day(1), 2000, items=(SOUND,))
        assert "tiktok-a-w" not in breakouts(v.run()), extra


def test_a_prior_reading_from_a_search_lane_is_not_its_baseline_reading():
    w = World()
    w.history("a", [500, 500, 5000, 5000])
    w.add("tiktok-a-p", "a", day(20), 5000, lane_class="search_presence", lane="expansion",
          route="tiktok/search/top", series="search", lag_h=1)
    w.read("tiktok-a-p", 500, 3)                                # its first measured reading
    w.add("tiktok-a-w", "a", day(1), 2000, items=(SOUND,))
    assert breakouts(w.run())["tiktok-a-w"]["usual_views"] == 500


def test_prior_posts_count_only_on_the_same_platform():
    w = World()
    w.history("a", [500, 500, 500, 500])
    w.add("instagram-a-1", "a", day(30), 500, platform="instagram")
    w.add("tiktok-a-w", "a", day(1), 2000, items=(SOUND,))
    assert w.run()["breakouts"] == []


def test_prior_posts_count_only_within_180_days():
    w = World()
    w.history("a", start=180)                                   # published 180 to 176 days before D
    w.add("tiktok-a-w", "a", day(1), 2000, items=(SOUND,))
    assert "tiktok-a-w" in breakouts(w.run())
    w = World()
    w.history("a", start=181)                                   # the oldest, 181 days before, is out
    w.add("tiktok-a-w", "a", day(1), 2000, items=(SOUND,))
    assert w.run()["breakouts"] == []


# (5) Views readings: each post's first reading, counter_post_views never, compared at a matched delay after publishing

def test_posts_with_null_views_never_count():
    w = World()
    w.history("a", [500, 500, 500, 500, None])                  # only four prior posts with views
    w.add("tiktok-a-w", "a", day(1), 2000, items=(SOUND,))
    w.history("b")
    w.add("tiktok-b-w", "b", day(1), None, items=(SOUND,))
    assert w.run()["breakouts"] == []


def test_counter_post_views_rereads_never_make_a_breakout():
    w = World()
    for c in ("a", "b", "c"):
        pid = w.breaker(c, 300, lag_h=24, d=day(6))
        w.reread(pid, 1600, 24 * 6)
    out = w.run()
    assert out["breakouts"] == [] and out["signals"] == []


def test_counter_post_views_rereads_are_not_prior_readings():
    w = World()
    w.history("a", [500, 500, 500, 500])
    w.add("tiktok-a-p", "a", day(20), None)
    w.reread("tiktok-a-p", 500, 48)
    w.add("tiktok-a-w", "a", day(1), 2000, items=(SOUND,))
    assert w.run()["breakouts"] == []


def test_the_first_reading_is_used_not_a_later_one():
    w = World()
    pid = w.breaker("a", 300, lag_h=24, prior_lag_h=72, d=day(4))
    w.read(pid, 1600, 48)
    assert w.run()["breakouts"] == []


@pytest.mark.parametrize("lane", ["placebo", "agent_live"])
def test_placebo_and_agent_live_sightings_are_never_a_first_reading(lane):
    w = World()
    w.history("a")
    w.add("tiktok-a-w", "a", day(1), 2000, items=(SOUND,), lag_h=1, lane_class="search_presence", lane=lane,
          route="search/multi", series=lane)
    w.read("tiktok-a-w", 300, 3)
    assert w.run()["breakouts"] == []


def test_a_counter_post_views_reread_is_never_a_first_reading():
    w = World()
    for c in ("a", "b", "c"):
        pid = w.breaker(c, None, lag_h=3)
        w.reread(pid, 1600, 3)
    out = w.run()
    assert out["breakouts"] == [] and out["signals"] == []


def test_posts_read_at_the_same_age_make_a_signal():
    w = World()
    pids = [w.breaker(c, 1600, lag_h=24) for c in ("a", "b", "c")]
    (row,) = w.run()["signals"]
    assert (row["creators"], row["top_ratio"], row["evidence_post_ids"]) == (3, 3.2, sorted(pids))


def test_prior_posts_read_younger_than_the_target_are_not_its_baseline():
    w = World()
    pid = w.breaker("a", 1600, lag_h=48, prior_lag_h=24, d=day(3))
    assert pid not in breakouts(w.run())
    w = World()
    pid = w.breaker("a", 1600, lag_h=24, prior_lag_h=72, d=day(3))
    assert pid in breakouts(w.run())


def test_readings_after_the_run_date_are_not_seen():
    w = World()
    pid = w.breaker("a", None, lag_h=3)
    w.read(pid, 5000, 24 * 2)                                   # published day(1), read D + 1
    assert w.run()["breakouts"] == []


def surges(w, d=D):
    client = duck.Client(w.load(duck.connect(views=False)))
    return breakout.run_creator_surges(client, d, core="core", agent="agent")


def test_creator_surges_count_the_days_breakout_posts_per_creator_and_reuse_the_breakout_rule():
    w = World()
    w.breaker("a", 1500, d=D)                                   # three times usual, published on d
    w.history("b")
    w.add("tiktok-b-w1", "b", D, 2000)
    w.add("tiktok-b-w2", "b", D, 4000)
    w.breaker("c", 1499, d=D)                                   # just under three times usual
    w.breaker("e", 2000, d=day(1))                              # a breakout, but the day before
    w.history("f", [100, 100, 100, 100, 100])
    w.add("tiktok-f-w", "f", D, 900)                            # nine times usual, under 1,000 views
    assert surges(w) == {("tiktok", "a"): 1, ("tiktok", "b"): 2, ("tiktok", "c"): 0, ("tiktok", "f"): 0}


def test_a_creator_who_cannot_be_judged_on_the_day_has_no_surge_count_rather_than_zero():
    w = World()
    w.history("a", [400, 500, 600, 500])                        # four earlier posts: no usual views
    w.add("tiktok-a-w", "a", D, 9000)
    w.history("b")
    w.add("tiktok-b-w", "b", D, None)                           # no reading with views
    w.history("c")                                              # nothing published on d
    assert surges(w) == {}


# (2) Item signals

def three_breakers(w, items=(SOUND,), **kw):
    return [w.breaker(c, items=items, **kw) for c in ("a", "b", "c")]


def test_three_unrelated_small_creators_breaking_out_on_one_sound_make_a_signal():
    w = World()
    pids = three_breakers(w)
    w.add("tiktok-a-x", "a", day(3), 100, items=(SOUND,))       # not a breakout, not evidence
    (row,) = w.run()["signals"]
    assert row == {"metric_date": D, "market": "ZA", "item_id": SOUND, "run_id": "detect-20260920",
                   "creators": 3, "posts": 3, "evidence_post_ids": sorted(pids), "top_ratio": 4.0,
                   "held_flagged": 0, "rule_version": "r1"}


def test_two_creators_are_not_enough():
    w = World()
    w.breaker("a")
    w.breaker("b")
    w.breaker("c", items=(OTHER_SOUND,))
    assert w.run()["signals"] == []


def test_a_format_item_makes_a_signal_across_platforms():
    w = World()
    w.breaker("a", items=(FORMAT,))
    w.breaker("b", items=(FORMAT,), platform="instagram")
    w.breaker("c", items=(FORMAT,), platform="youtube")
    (row,) = w.run()["signals"]
    assert (row["item_id"], row["creators"]) == (FORMAT, 3)


def test_the_same_handle_across_platforms_is_one_creator():
    w = World()
    w.breaker("ama", items=(FORMAT,))
    w.breaker("Ama", items=(FORMAT,), platform="instagram")
    w.breaker("b", items=(FORMAT,))
    assert w.run()["signals"] == []


def test_the_creators_table_handle_joins_accounts_across_platforms():
    w = World()
    w.breaker("ama_za", items=(FORMAT,))
    w.breaker("ama.official", items=(FORMAT,), platform="instagram")
    w.breaker("b", items=(FORMAT,))
    w.creators += [creator_row("ama_za", handle="ama"), creator_row("ama.official", "instagram", handle="@AMA")]
    assert w.run()["signals"] == []


def test_one_creator_breaking_out_twice_counts_once():
    w = World()
    w.breaker("a")
    w.add("tiktok-a-w2", "a", day(2), 3000, items=(SOUND,))
    w.breaker("b")
    assert w.run()["signals"] == []


def test_flagged_co_action_accounts_do_not_count():
    w = World()
    three_breakers(w)
    w.creators.append(creator_row("c", coord_score=1))
    assert w.run()["signals"] == []


def test_flagged_breakouts_are_reported_as_held():
    w = World()
    three_breakers(w)
    w.breaker("d")
    w.add("tiktok-d-w2", "d", day(2), 3000, items=(SOUND,))
    w.creators.append(creator_row("d", coord_score=2))
    (row,) = w.run()["signals"]
    assert (row["creators"], row["posts"], row["held_flagged"]) == (3, 3, 2)
    assert "tiktok-d-w" not in row["evidence_post_ids"]


@pytest.mark.parametrize("tier", ["mid", "macro", "mega", None])
def test_only_nano_and_micro_creators_count(tier):
    w = World()
    w.breaker("a", tier="nano")
    w.breaker("b")
    w.breaker("c", tier=tier)
    assert w.run()["signals"] == []


def test_breakouts_count_only_within_the_seven_days_to_the_run_date():
    w = World()
    w.breaker("a", d=day(6))
    w.breaker("b", d=day(0), lag_h=1)
    w.breaker("c", d=day(7))
    assert w.run()["signals"] == []
    w = World()
    w.breaker("a", d=day(6))
    w.breaker("b", d=day(0), lag_h=1)
    w.breaker("c", d=day(3))
    assert len(w.run()["signals"]) == 1


def test_the_signal_is_in_the_market_the_posts_were_sighted_in():
    w = World()
    w.breaker("a")
    w.breaker("b")
    w.breaker("c", market="NG")
    assert w.run()["signals"] == []
    w = World()
    three_breakers(w, market="KE")
    assert [r["market"] for r in w.run()["signals"]] == ["KE"]


def test_placebo_and_agent_live_sightings_do_not_place_a_post_in_a_market():
    w = World()
    w.breaker("a")
    w.breaker("b")
    w.breaker("c", lane_class="search_presence", lane="agent_live", route="search/multi", series="agent_live")
    assert w.run()["signals"] == []


def test_counter_post_views_rereads_do_not_place_a_post_in_a_market():
    w = World()
    pids = three_breakers(w, market="KE", geo="ZA")
    for pid in pids:
        w.reread(pid, None, 4, market="ZA")

    out = w.run()

    assert set(breakouts(out)) == set(pids)
    assert out["signals"] == []


# (3a) Locality

@pytest.mark.parametrize("geo, confidence", [("NG", 0.9), ("ZA", 0.5), (None, None)])
def test_a_creator_not_located_in_the_market_does_not_count(geo, confidence):
    w = World()
    w.breaker("a")
    w.breaker("b")
    w.breaker("c", geo=geo, geo_confidence=confidence)
    assert w.run()["signals"] == []


def test_a_creators_home_market_locates_them():
    w = World()
    w.breaker("a")
    w.breaker("b")
    w.breaker("c", geo=None, geo_confidence=None)
    w.creators.append(creator_row("c", home_market="ZA"))
    assert len(w.run()["signals"]) == 1


def test_a_post_located_at_exactly_0_7_counts():
    w = World()
    w.breaker("a")
    w.breaker("b")
    w.breaker("c", geo_confidence=0.7)
    assert len(w.run()["signals"]) == 1


# (4) Generic items

def test_generic_items_never_make_a_signal():
    w = World()
    w.cmap[0]["status"] = "generic"
    three_breakers(w)
    assert w.run()["signals"] == []


def test_stoplisted_tags_never_make_a_signal():
    w = World()
    three_breakers(w, items=(FYP,))
    assert w.run()["signals"] == []


def test_the_stoplist_is_asked_for_every_item(monkeypatch):
    monkeypatch.setattr(breakout, "is_generic", lambda kind, key: key == "tiktok:s1")
    w = World()
    three_breakers(w)
    assert w.run()["signals"] == []


# (3) run_breakout only reads; append_signals is the one write

def test_the_step_only_reads():
    w = World()
    three_breakers(w)
    out = w.run()
    assert out["signals"] and out["sql"]
    for sql in out["sql"]:
        assert not any(word in sql.upper() for word in ("INSERT", "MERGE", "UPDATE", "DELETE", "CREATE"))


def test_both_queries_are_date_bounded():
    posts, sightings = breakout.queries()
    for column in ("ps.post_date", "po.observed_date"):
        assert f"{column} BETWEEN DATE_SUB(@d, INTERVAL 180 DAY) AND @d" in posts
        assert f"{column} BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d" in sightings


# (6) The detect step appends the day's signals to breakout_signals

SIGNAL_COLUMNS = ["metric_date", "market", "item_id", "run_id", "creators", "posts", "evidence_post_ids",
                  "top_ratio", "held_flagged", "rule_version"]


def append(con, run_id="breakout-20260920-a", d=D, client=None):
    client = client or duck.Client(con)
    return breakout.append_signals(client, d, run_id, "r1", core="core", agent="agent")


def written(con):
    return duck.query(con, "SELECT * FROM {core}.breakout_signals s ORDER BY s.metric_date, s.market, s.item_id")


def test_append_signals_writes_the_days_signals_with_the_table_columns():
    w = World()
    pids = three_breakers(w)
    top = w.breaker("d", 3000)                                  # ratio 6, so it leads the evidence
    con = w.load(duck.connect(views=False))
    counts = append(con)
    assert (counts["breakouts"], counts["signals"], counts["appended"]) == (4, 1, 1)
    assert counts["eligibility"]["empty_reason"] is None
    rows = written(con)
    assert [list(r) for r in rows] == [SIGNAL_COLUMNS]
    assert rows == [{"metric_date": D, "market": "ZA", "item_id": SOUND, "run_id": "breakout-20260920-a",
                     "creators": 4, "posts": 4, "evidence_post_ids": [top] + sorted(pids), "top_ratio": 6.0,
                     "held_flagged": 0, "rule_version": "r1"}]


def test_the_rows_written_are_the_signals_run_breakout_finds():
    w = World()
    three_breakers(w)
    for c in ("d", "e", "f", "g"):
        w.breaker(c, 2500 if c == "e" else 2000, items=(FORMAT,), market="KE")
    w.creators.append(creator_row("g", coord_score=1))
    con = w.load(duck.connect(views=False))
    found = breakout.run_breakout(duck.Client(con), D, "breakout-20260920-a", "r1", core="core", agent="agent")
    assert len(found["signals"]) == 2 and found["signals"][0]["held_flagged"] == 1
    assert append(con)["appended"] == 2
    assert written(con) == sorted(found["signals"], key=lambda r: (r["market"], r["item_id"]))


def test_a_rerun_on_the_same_day_adds_nothing():
    w = World()
    three_breakers(w)
    con = w.load(duck.connect(views=False))
    assert append(con, "breakout-20260920-a")["appended"] == 1
    counts = append(con, "breakout-20260920-b")
    assert (counts["breakouts"], counts["signals"], counts["appended"]) == (3, 1, 0)
    assert counts["eligibility"]["empty_reason"] is None
    assert [r["run_id"] for r in written(con)] == ["breakout-20260920-a"]


def test_a_row_for_another_day_or_item_does_not_block_the_append():
    w = World()
    three_breakers(w)
    con = w.load(duck.connect(views=False))
    old = {"metric_date": day(1), "market": "ZA", "item_id": SOUND, "run_id": "breakout-20260919-a",
           "creators": 3, "posts": 3, "evidence_post_ids": ["p1", "p2", "p3"], "top_ratio": 4.0,
           "held_flagged": 0, "rule_version": "r1"}
    duck.load(con, "core.breakout_signals", [old, {**old, "metric_date": D, "item_id": OTHER_SOUND},
                                             {**old, "metric_date": D, "market": "KE"}])
    assert append(con)["appended"] == 1
    assert [(r["metric_date"], r["market"], r["item_id"]) for r in written(con)] == [
        (day(1), "ZA", SOUND), (D, "KE", SOUND), (D, "ZA", OTHER_SOUND), (D, "ZA", SOUND)]


def test_a_day_without_signals_runs_no_insert():
    w = World()
    w.breaker("a")
    w.breaker("b")
    con = w.load(duck.connect(views=False))
    client = duck.Client(con)
    counts = append(con, client=client)
    assert (counts["breakouts"], counts["signals"], counts["appended"]) == (2, 0, 0)
    assert counts["eligibility"]["empty_reason"] is not None
    assert written(con) == [] and not any("INSERT" in s.upper() for s in client.sql)


def test_the_write_only_appends():
    w = World()
    three_breakers(w)
    con = w.load(duck.connect(views=False))
    client = duck.Client(con)
    append(con, client=client)
    append(con, "breakout-20260920-b", client=client)
    assert any("INSERT INTO core.breakout_signals" in s for s in client.sql)
    for sql in client.sql:
        upper = sql.upper()
        for word in ("DELETE", "DROP", "TRUNCATE", "ALTER", "REPLACE", "CREATE", "MERGE", "UPDATE"):
            assert word not in upper
        assert re.match(r"\s*(SELECT|WITH|INSERT)\b", re.sub(r"--[^\n]*", "", upper))


@pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")
def test_bigquery_dry_run_of_the_append():
    from google.cloud import bigquery

    client = bigquery.Client(project="ogilvy-trends-v2")
    signal = {"metric_date": D, "market": "ZA", "item_id": SOUND, "run_id": "breakout-20260920-a", "creators": 3,
              "posts": 3, "evidence_post_ids": ["p1", "p2", "p3"], "top_ratio": None, "held_flagged": 0,
              "rule_version": "r1"}
    for sql, params in ((breakout.APPEND_SQL, [breakout.rows_param([signal])]),
                        (breakout.APPENDED_SQL, [sqlrun._param("d", D), sqlrun._param("run_id", "r")])):
        result = client.query(sqlrun.render(sql), job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, query_parameters=params))
        assert result.dry_run


@pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")
def test_bigquery_dry_runs():
    from google.cloud import bigquery

    client = bigquery.Client(project="ogilvy-trends-v2")
    for sql in breakout.queries():
        result = client.query(sqlrun.render(sql), job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, query_parameters=[sqlrun._param("d", D)]))
        assert result.dry_run
