"""Tests for core/collect/parse.py and core/collect/ids.py on small unprobed-shape fixtures (parse_*.json)."""

import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.collect import ids
from core.collect.parse import parse, parse_time

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FETCHED = "2026-09-28T00:30:00Z"          # 02:30 SAST, 01:30 WAT, 03:30 EAT on 28 September
FETCHED_ISO = "2026-09-28T00:30:00+00:00"


def fixture(name, key):
    return json.loads((FIXTURES / f"parse_{name}.json").read_text(encoding="utf-8"))[key]


def fake_item_id(kind, raw, platform=None):
    """Stands in for core.detect.items.item_id(kind, canonical_key(kind, raw, platform))."""
    text = str(raw).strip()
    if kind == "hashtag":
        key = text.lstrip("#").casefold()
    elif kind in ("sound", "creator"):
        if not platform:
            raise ValueError("needs a platform")
        key = f"{platform}:" + (text if kind == "sound" else text.lstrip("@").casefold())
    else:
        key = text.casefold()
    if not key:
        raise ValueError("empty")
    return f"{kind}|{key}"


class FakeGeo:
    """Stands in for core.detect.geo.geo_for_post; like it, refuses any market but ZA, NG and KE."""

    def __init__(self):
        self.calls = []

    def __call__(self, platform, market, *, ext_region=None, home_market=None, profile_location=None,
                 text=None, language=None):
        assert market in ("ZA", "NG", "KE")
        self.calls.append(dict(platform=platform, market=market, ext_region=ext_region, home_market=home_market,
                               profile_location=profile_location, text=text, language=language))
        if ext_region in ("ZA", "NG", "KE"):
            return ext_region, 0.9, "ext_region"
        if profile_location == "Johannesburg":
            return "ZA", 0.8, "home_market"
        return None, None, None


def run(route, params, market, body, *, fetched_at=FETCHED, run_id="run1", geo=None, **kw):
    return parse(route, params, market, body, fetched_at, run_id,
                 item_id_fn=fake_item_id, geo_fn=geo or FakeGeo(), **kw)


def ddl_columns(table):
    text = (ROOT / "docs/full-42/DATA.md").read_text(encoding="utf-8")
    body = re.search(r"intelligence_42_core\." + table + r" \((.*?)\)\nPARTITION BY", text, re.S).group(1)
    body = re.sub(r"-{2}[^\n]*", "", body)
    return [chunk.split()[0] for chunk in body.split(",") if chunk.strip()]


def by_id(rows):
    return {row["post_id"]: row for row in rows}


def counters_by_item(rows):
    return {row["item_id"]: row for row in rows}


# Post identity

def test_post_id_is_the_old_observation_id_with_an_empty_market():
    spec = importlib.util.spec_from_file_location("old_obs", ROOT / "engine/src/ingestion/observation_id.py")
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    url = "https://www.tiktok.com/@dj.x/video/1?utm_source=x&b=2&a=1"
    for native, link in (("7400000000000000001", url), (7400000000000000001, url), (None, url)):
        row = {"source": "socialcrawl", "platform": "tiktok", "market": "", "native_id": native, "url": link}
        assert ids.post_id("tiktok", native, link) == old.observation_id(row)
    assert ids.post_id("TikTok", "7400000000000000001") == ids.post_id("tiktok", "7400000000000000001", "x")


def test_post_id_needs_a_native_id_or_an_http_url():
    assert ids.post_id("tiktok", None, None) is None
    assert ids.post_id("tiktok", "", "not a url") is None
    assert ids.post_id("tiktok", 1.5, None) is None
    assert ids.post_id("tiktok", None, "https://x.com/a/status/1").startswith("obs1_")


def test_post_id_is_stable_across_markets_and_runs():
    body = fixture("rank", "tiktok_trending")
    za = run("tiktok/trending", {"region": "ZA", "feed": "local"}, "ZA", body, pull_seq=1, run_id="r1")
    ke = run("tiktok/trending", {"region": "KE", "feed": "local"}, "KE", body, pull_seq=9, run_id="r2",
             fetched_at="2026-09-29T05:00:00Z")
    assert [p["post_id"] for p in za["posts"]] == [p["post_id"] for p in ke["posts"]]
    assert za["posts"][0]["post_id"] == ids.post_id(
        "tiktok", "7400000000000000001", "https://www.tiktok.com/@dj.x/video/7400000000000000001")
    search = run("tiktok/search/top", {"query": "soweto"}, "ZA", fixture("search", "search_top"), lane="expansion")
    assert search["posts"][0]["post_id"] == ids.post_id("tiktok", "7400000000000000002")


# Row shapes

def test_rows_carry_exactly_the_ddl_columns_and_are_json_ready():
    cases = [
        run("tiktok/trending", {"region": "ZA", "feed": "local"}, "ZA", fixture("rank", "tiktok_trending"), pull_seq=1),
        run("tiktok/hashtags/popular", {"countryCode": "ZA", "period": "7"}, "ZA",
            fixture("boards", "hashtags_popular"), pull_seq=1),
        run("prism/post-stats", {}, "ZA", fixture("restat", "post_stats")),
    ]
    want = {"posts": ddl_columns("posts"), "observations": ddl_columns("post_observations"),
            "counters": ddl_columns("item_counter_daily")}
    assert "creator_tier_at_post" in want["posts"] and "is_board" in want["counters"]
    seen = set()
    for out in cases:
        assert set(out) == set(want)
        for name, rows in out.items():
            for row in rows:
                assert list(row) == want[name], name
                seen.add(name)
        json.dumps(out)
    assert seen == set(want)


@pytest.mark.parametrize(("route", "params", "market", "family", "body_key", "source_market"), [
    ("tiktok/trending", {"region": " za ", "feed": "local"}, "ZA", "rank", "tiktok_trending", "ZA"),
    ("youtube/videos/trending", {"region": "ke"}, "KE", "rank", "youtube_trending", "KE"),
    ("tiktok/search/top", {"query": "lagos", "country": "ng"}, "NG", "search", "search_top", "NG"),
    ("tiktok/search/hashtag", {"hashtag": "naija", "region": "NG"}, "NG", "search", "search_top", "NG"),
])
def test_supported_explicit_market_routes_carry_normalized_source_provenance(
        route, params, market, family, body_key, source_market):
    kwargs = {"pull_seq": 1} if family == "rank" else {"lane": "expansion"}
    out = run(route, params, market, fixture(family, body_key), **kwargs)
    assert out["observations"]
    assert {(o["source_market"], o["source_region"]) for o in out["observations"]} == {
        (source_market, source_market)}


@pytest.mark.parametrize("params", [
    {"query": "Kenya matatu"},
    {"query": "matatu", "country": "Nairobi"},
    {"query": "matatu", "country": "ZA"},
    {"query": "matatu", "country": "KE", "region": "ZA"},
])
def test_search_market_allocation_query_text_and_invalid_scope_never_supply_source_market(params):
    out = run("tiktok/search/top", params, "KE", fixture("search", "search_top"), lane="expansion")
    assert out["observations"]
    assert {(o["source_market"], o["source_region"]) for o in out["observations"]} == {(None, None)}


# Rank lists

def test_tiktok_local_feed_gives_posts_ranked_observations_and_rank_counters():
    geo = FakeGeo()
    out = run("tiktok/trending", {"region": "ZA", "feed": "local", "trim": "true"}, "ZA",
              fixture("rank", "tiktok_trending"), pull_seq=2, geo=geo)
    posts = out["posts"]
    assert [p["native_id"] for p in posts] == ["7400000000000000001", "7400000000000000002", "7400000000000000003"]
    first, second, third = posts
    assert first["platform"] == "tiktok" and first["creator_id"] == "dj.x"
    assert first["text"] == "Amapiano Sunday #amapiano #fyp" and first["transcript"] is None
    assert first["hashtags"] == ["amapiano", "fyp"] and second["hashtags"] == ["Amapiano", "dance"]
    assert first["sound_id"] == "m100" and second["sound_id"] is None
    assert first["thumbnail_url"] == "https://p16.example/cover1.jpg" and first["duration_s"] == 15.5
    assert first["published_at"] == "2026-09-27T22:30:00+00:00"
    assert first["post_date"] == "2026-09-28"                 # 00:30 SAST
    assert second["published_at"] == "2026-09-27T12:00:00+00:00"   # epoch seconds
    assert third["published_at"] is None and third["post_date"] == "2026-09-28"
    assert [p["creator_tier_at_post"] for p in posts] == ["nano", "mid", "mega"]
    assert (first["views"], first["likes"], first["comments"], first["shares"], first["engagement"]) == (
        12000, 900, 41, 12, 953)
    assert third["engagement"] is None
    assert (first["geo_market"], first["geo_confidence"], first["geo_source"]) == ("ZA", 0.9, "ext_region")
    assert second["geo_market"] == "NG" and third["geo_market"] is None
    assert geo.calls[0] == dict(platform="tiktok", market="ZA", ext_region="ZA", home_market=None,
                                profile_location=None, text="Amapiano Sunday #amapiano #fyp", language="en")
    assert first["vendor"] == "socialcrawl" and first["endpoint"] == "tiktok/trending"
    assert first["vendor_labels"] == {"relevance": 0.9, "niche": "music"} and second["vendor_labels"] is None
    assert first["run_id"] == "run1"

    obs = out["observations"]
    assert [o["rank"] for o in obs] == [1, 2, 3]
    assert [o["post_id"] for o in obs] == [p["post_id"] for p in posts]
    for o in obs:
        assert (o["market"], o["platform"], o["route"], o["series"], o["lane"], o["lane_class"]) == (
            "ZA", "tiktok", "tiktok/trending", "feed_tiktok", "sweep", "unbiased_rank")
        assert o["protocol"] == "tiktok/trending?feed=local&region=ZA"
        assert o["pull_seq"] == 2 and o["observed_at"] == FETCHED_ISO and o["observed_date"] == "2026-09-28"
    assert obs[0]["views"] == 12000

    counters = counters_by_item(out["counters"])
    assert {k: v["value"] for k, v in counters.items()} == {
        "hashtag|amapiano": 1.0, "hashtag|fyp": 1.0, "sound|tiktok:m100": 1.0, "creator|tiktok:dj.x": 1.0,
        "hashtag|dance": 2.0, "creator|tiktok:lagos.moves": 2.0, "creator|tiktok:nairobi.eats": 3.0}
    for c in out["counters"]:
        assert (c["unit"], c["is_board"], c["lane_class"], c["series"], c["market"], c["source"]) == (
            "rank", False, "unbiased_rank", "feed_tiktok", "ZA", "live")
        assert c["pull_seq"] == 2 and c["obs_date"] == "2026-09-28" and c["available_at"] == FETCHED_ISO


def test_post_date_is_the_first_sighting_markets_local_day():
    body = fixture("rank", "tiktok_trending")
    za = run("tiktok/trending", {"region": "ZA", "feed": "local"}, "ZA", body, pull_seq=1)
    ng = run("tiktok/trending", {"region": "NG", "feed": "local"}, "NG", body, pull_seq=1)
    assert za["posts"][0]["post_date"] == "2026-09-28"       # 22:30 UTC is 00:30 SAST
    assert ng["posts"][0]["post_date"] == "2026-09-27"       # and 23:30 WAT


def test_observed_date_is_market_local_across_midnight_utc():
    body = fixture("rank", "tiktok_trending")
    late = "2026-09-27T22:30:00Z"
    dates = {m: run("tiktok/trending", {"region": m, "feed": "local"}, m, body, pull_seq=1, fetched_at=late)
             for m in ("ZA", "NG", "KE")}
    assert dates["ZA"]["observations"][0]["observed_date"] == "2026-09-28"
    assert dates["NG"]["observations"][0]["observed_date"] == "2026-09-27"
    assert dates["KE"]["observations"][0]["observed_date"] == "2026-09-28"
    assert dates["NG"]["counters"][0]["obs_date"] == "2026-09-27"
    assert dates["NG"]["observations"][0]["observed_at"] == "2026-09-27T22:30:00+00:00"
    aware = datetime(2026, 9, 27, 23, 30, tzinfo=timezone.utc)
    ng = run("tiktok/trending", {"region": "NG", "feed": "local"}, "NG", body, pull_seq=1, fetched_at=aware)
    assert ng["observations"][0]["observed_date"] == "2026-09-28"


def test_fetched_at_must_carry_a_timezone():
    with pytest.raises(ValueError):
        run("tiktok/trending", {"region": "ZA", "feed": "local"}, "ZA", fixture("rank", "tiktok_trending"),
            pull_seq=1, fetched_at=datetime(2026, 9, 28, 0, 30))


def test_rank_lists_need_a_pull_seq_and_the_local_feed():
    body = fixture("rank", "tiktok_trending")
    with pytest.raises(ValueError):
        run("tiktok/trending", {"region": "ZA", "feed": "local"}, "ZA", body)
    with pytest.raises(ValueError):
        run("tiktok/trending", {"region": "ZA", "feed": "global"}, "ZA", body, pull_seq=1)
    with pytest.raises(ValueError):
        run("tiktok/trending", {"region": "ZA"}, "ZA", body, pull_seq=1)


def test_youtube_trending_is_a_board_per_category():
    out = run("youtube/videos/trending", {"region": "NG", "category": "music", "max_results": "25"}, "NG",
              fixture("rank", "youtube_trending"), pull_seq=4)
    first, second = out["posts"]
    assert first["creator_id"] == "UC_one" and first["creator_tier_at_post"] == "micro"
    assert first["published_at"] == "2026-09-27T10:06:18+00:00" and first["text"] == "Match highlights"
    assert second["creator_tier_at_post"] is None
    assert [o["rank"] for o in out["observations"]] == [1, 2]
    assert out["observations"][0]["protocol"] == "youtube/videos/trending?category=music&max_results=25&region=NG"
    assert {c["item_id"]: c["value"] for c in out["counters"]} == {
        "creator|youtube:uc_one": 1.0, "creator|youtube:uc_two": 2.0}
    assert all(c["is_board"] and c["series"] == "board_youtube" for c in out["counters"])


def test_global_shorts_board_writes_global_rows_and_never_asks_geo():
    geo = FakeGeo()
    out = run("youtube/shorts/trending", {}, "GLOBAL", fixture("rank", "shorts_trending"), pull_seq=1, geo=geo)
    assert geo.calls == []
    post = out["posts"][0]
    assert post["geo_market"] is None and post["post_date"] == "2026-09-27"
    assert out["observations"][0]["market"] == "GLOBAL" and out["observations"][0]["observed_date"] == "2026-09-28"
    assert (out["observations"][0]["source_market"], out["observations"][0]["source_region"]) == (None, None)
    assert {c["item_id"] for c in out["counters"]} == {"hashtag|shorts", "creator|youtube:uc_three"}
    assert all(c["market"] == "GLOBAL" and c["is_board"] and c["series"] == "board_global_music"
               for c in out["counters"])
    with pytest.raises(ValueError):
        run("youtube/shorts/trending", {}, "ZA", fixture("rank", "shorts_trending"), pull_seq=1)


def test_reddit_lists_are_ranked_but_not_boards():
    out = run("reddit/subreddit", {"subreddit": "southafrica", "sort": "hot", "cursor": "x"}, "ZA",
              fixture("rank", "reddit_subreddit"), pull_seq=1)
    assert [o["rank"] for o in out["observations"]] == [1, 2]
    assert out["posts"][0]["text"] == "Load shedding schedule\nStage 2 tonight"
    assert out["observations"][0]["protocol"] == "reddit/subreddit?sort=hot&subreddit=southafrica"
    assert all(c["series"] == "list_reddit" and c["is_board"] is False for c in out["counters"])


def test_hashtag_board_keeps_the_vendor_rank_and_carries_the_daily_curve():
    out = run("tiktok/hashtags/popular", {"countryCode": "ZA", "period": "7", "industry": "all"}, "ZA",
              fixture("boards", "hashtags_popular"), pull_seq=3)
    assert out["posts"] == [] and out["observations"] == []
    ranks = [c for c in out["counters"] if c["unit"] == "rank"]
    assert [(c["item_id"], c["value"]) for c in ranks] == [("hashtag|braai", 2.0), ("hashtag|amapiano", 1.0)]
    for c in ranks:
        assert (c["series"], c["is_board"], c["lane_class"], c["market"], c["pull_seq"]) == (
            "board_tiktok_hashtag", True, "unbiased_rank", "ZA", 3)
        assert c["protocol"] == "tiktok/hashtags/popular?countryCode=ZA&industry=all&period=7"
    curve = [c for c in out["counters"] if c["series"] == "curve_tiktok_hashtag"]
    got = {(c["item_id"], c["obs_date"]): (c["value"], c["source"]) for c in curve}
    assert got == {
        ("hashtag|braai", "2026-09-26"): (30.0, "vendor_history"),
        ("hashtag|braai", "2026-09-27"): (45.0, "vendor_history"),
        ("hashtag|amapiano", "2026-09-26"): (120.0, "vendor_history"),
        ("hashtag|amapiano", "2026-09-27"): (200.0, "vendor_history"),
        ("hashtag|amapiano", "2026-09-28"): (60.0, "live")}
    for c in curve:
        assert (c["unit"], c["lane_class"], c["is_board"], c["market"], c["pull_seq"]) == (
            "delta", "unbiased_counter", False, "ZA", None)
        assert c["available_at"] == FETCHED_ISO


def test_instagram_music_board_names_sounds_on_instagram():
    out = run("instagram/music/trending", {}, "GLOBAL", fixture("boards", "ig_music_trending"), pull_seq=1)
    assert [(c["item_id"], c["value"]) for c in out["counters"]] == [
        ("sound|instagram:ig_a1", 1.0), ("sound|instagram:ig_a2", 2.0)]
    assert all(c["market"] == "GLOBAL" and c["series"] == "board_global_music" and c["platform"] == "instagram"
               for c in out["counters"])


def test_apple_music_chart_keeps_rank_order():
    out = run("apple_music/charts", {"country": "za", "type": "songs", "limit": "100"}, "ZA",
              fixture("boards", "apple_charts"), pull_seq=1)
    assert [(c["item_id"], c["value"]) for c in out["counters"]] == [
        ("sound|apple_music:am_1", 1.0), ("sound|apple_music:am_2", 2.0), ("sound|apple_music:am_3", 3.0)]
    assert all(c["series"] == "board_apple_music" and c["is_board"] for c in out["counters"])
    assert out["counters"][0]["protocol"] == "apple_music/charts?country=za&limit=100&type=songs"


# Counters

def test_song_and_hashtag_totals_are_global_counters():
    song = run("tiktok/song", {"clipId": "m100"}, "ZA", fixture("counters", "tiktok_song"))
    tag = run("tiktok/hashtag", {"hashtag": "Amapiano"}, "ZA", fixture("counters", "tiktok_hashtag"))
    assert song["posts"] == [] and tag["posts"] == []
    (s,), (t,) = song["counters"], tag["counters"]
    assert (s["item_id"], s["value"], s["series"]) == ("sound|tiktok:m100", 15000.0, "counter_tiktok_sound")
    assert (t["item_id"], t["value"], t["series"]) == ("hashtag|amapiano", 900000.0, "counter_tiktok_hashtag")
    for c in (s, t):
        assert (c["market"], c["unit"], c["lane_class"], c["is_board"], c["source"]) == (
            "GLOBAL", "total", "unbiased_counter", False, "live")
        assert c["obs_date"] == "2026-09-28" and c["pull_seq"] is None


def test_song_videos_give_watchlist_posts_and_no_adoption_series():
    out = run("tiktok/song/videos", {"clipId": "m100"}, "ZA", fixture("counters", "song_videos"))
    assert out["counters"] == []  # the adoption points are a page sample, not a daily total (see test_parse_stored)
    (obs,) = out["observations"]
    assert (obs["lane"], obs["lane_class"], obs["series"], obs["market"], obs["rank"]) == (
        "watchlist", "watchlist", "watch", "ZA", None)
    assert out["posts"][0]["sound_id"] == "m100"


# Panels

def test_facebook_hub_panel_uses_the_page_id_and_drops_posts_before_since():
    out = run("facebook/profile/posts", {"pageId": "100068189748310", "since": "2026-09-27"}, "ZA",
              fixture("panels", "facebook_posts"))
    (post,) = out["posts"]
    assert post["native_id"] == "fb_1" and post["creator_id"] == "100068189748310"
    assert post["engagement"] == 2550
    (obs,) = out["observations"]
    assert (obs["lane"], obs["lane_class"], obs["series"], obs["rank"], obs["pull_seq"]) == (
        "panel", "panel", "panel_fb_hub", None, None)
    assert (obs["source_market"], obs["source_region"]) == (None, None)
    assert obs["protocol"] == "facebook/profile/posts"
    assert out["counters"] == []


def test_panel_protocol_can_name_its_membership():
    out = run("facebook/profile/posts", {"pageId": "1"}, "ZA", fixture("panels", "facebook_posts"),
              protocol="panel_fb_hub:v1")
    assert {o["protocol"] for o in out["observations"]} == {"panel_fb_hub:v1"}


def test_culture_desk_profiles_take_platform_and_author_from_each_row():
    geo = FakeGeo()
    out = run("prism/profiles", {"include": "posts", "since": "2026-09-27"}, "ZA",
              fixture("panels", "prism_profiles"), geo=geo)
    (post,) = out["posts"]
    assert (post["platform"], post["creator_id"], post["creator_tier_at_post"]) == (
        "instagram", "culture.desk.za", "macro")
    assert post["hashtags"] == ["durbanjuly"]
    assert post["geo_market"] == "ZA" and geo.calls[0]["profile_location"] == "Johannesburg"
    assert out["observations"][0]["series"] == "panel_culture_desk"
    assert (out["observations"][0]["source_market"], out["observations"][0]["source_region"]) == (None, None)
    assert out["observations"][0]["protocol"] == "prism/profiles?include=posts"


def test_x_hub_panel_parses_twitter_dates_keeps_vendor_order_and_drops_old_tweets():
    out = run("twitter/user/tweets", {"handle": "SundayTimesZA", "since": "2026-09-27"}, "ZA",
              fixture("panels", "twitter_tweets"))
    assert [p["native_id"] for p in out["posts"]] == ["1971000000000000002", "1971000000000000001"]
    first, second = out["posts"]
    assert first["published_at"] == "2026-09-27T20:15:00+00:00" and first["post_date"] == "2026-09-27"
    assert second["published_at"] == "2026-09-27T22:30:00+00:00" and second["post_date"] == "2026-09-28"
    assert first["creator_id"] == "SundayTimesZA"
    assert all(o["series"] == "panel_x_hub" and o["lane"] == "panel" for o in out["observations"])
    assert {(o["source_market"], o["source_region"]) for o in out["observations"]} == {(None, None)}


def test_parse_time_reads_every_vendor_form():
    assert parse_time("Wed Oct 30 05:45:03 +0000 2019") == datetime(2019, 10, 30, 5, 45, 3, tzinfo=timezone.utc)
    assert parse_time("2026-07-23T12:55:16.000Z") == datetime(2026, 7, 23, 12, 55, 16, tzinfo=timezone.utc)
    assert parse_time("2026-07-23 12:06:18 +02:00") == datetime(2026, 7, 23, 10, 6, 18, tzinfo=timezone.utc)
    assert parse_time(1790510400) == parse_time("1790510400") == parse_time(1790510400000)
    assert parse_time(None) is None and parse_time("") is None and parse_time("soon") is None


# Searches

@pytest.mark.parametrize("lane", ["expansion", "exploration"])
def test_search_top_takes_the_callers_lane_and_carries_vendor_labels(lane):
    out = run("tiktok/search/top", {"query": "soweto", "publish_time": "this-week", "country": "ZA", "seen": "s1"},
              "ZA", fixture("search", "search_top"), lane=lane, seed_key="soweto dance")
    (post,) = out["posts"]
    assert post["vendor_labels"] == {"relevance": 0.91, "labels": {"sponsored": False, "intent": "entertain"}}
    (obs,) = out["observations"]
    assert (obs["lane"], obs["lane_class"], obs["series"], obs["seed_key"], obs["rank"]) == (
        lane, "search_presence", "search", "soweto dance", None)
    assert obs["protocol"] == "tiktok/search/top?country=ZA&publish_time=this-week"
    assert out["counters"] == []


def test_search_needs_a_search_lane():
    body = fixture("search", "search_top")
    with pytest.raises(ValueError):
        run("tiktok/search/top", {"query": "x"}, "ZA", body)
    with pytest.raises(ValueError):
        run("tiktok/search/top", {"query": "x"}, "ZA", body, lane="panel")
    placebo = run("tiktok/search/top", {"query": "x"}, "ZA", body, lane="placebo")
    assert placebo["observations"][0]["series"] == "placebo"


def test_fixed_lane_routes_refuse_a_different_lane():
    panel = run("facebook/profile/posts", {"pageId": "1"}, "ZA", fixture("panels", "facebook_posts"))
    try:
        seeded = run("facebook/profile/posts", {"pageId": "1", "since": "2026-09-26"}, "ZA",
                     fixture("panels", "facebook_posts"), lane="expansion", seed_key="brand|page")
    except ValueError as exc:
        pytest.fail(f"panel seed lane was rejected: {exc}")
    assert panel["observations"][0]["lane_class"] == "panel"
    assert {(o["lane"], o["lane_class"], o["series"], o["seed_key"]) for o in seeded["observations"]} == {
        ("expansion", "search_presence", "search", "brand|page")}
    with pytest.raises(ValueError):
        run("prism/profiles", {"include": "posts"}, "ZA", fixture("panels", "prism_profiles"), lane="expansion")


def test_search_multi_takes_the_platform_from_each_source():
    try:
        out = run("search/multi", {"query": "stokvel", "platforms": "instagram,youtube,reddit", "since": "2026-09-21"},
                  "ZA", fixture("search", "search_multi"), lane="anchor", seed_key="topic|stokvel")
    except ValueError as exc:
        pytest.fail(f"anchor seed lane was rejected: {exc}")
    assert [(p["platform"], p["native_id"]) for p in out["posts"]] == [("instagram", "ig_s1"), ("youtube", "yt_s1")]
    assert out["posts"][0]["vendor_labels"] == {"relevance": 0.7}
    assert [o["platform"] for o in out["observations"]] == ["instagram", "youtube"]
    assert {(o["lane"], o["seed_key"]) for o in out["observations"]} == {("anchor", "topic|stokvel")}
    assert out["observations"][0]["protocol"] == "search/multi?platforms=instagram,youtube,reddit"
    assert {(o["source_market"], o["source_region"]) for o in out["observations"]} == {(None, None)}


def test_instagram_location_posts_are_search_presence():
    out = run("instagram/location/posts", {"location_id": "123"}, "ZA", fixture("search", "ig_location"))
    (obs,) = out["observations"]
    assert (obs["lane"], obs["lane_class"], obs["series"]) == ("sweep", "search_presence", "ig_location")
    assert obs["protocol"] == "instagram/location/posts?location_id=123"
    assert (obs["source_market"], obs["source_region"]) == (None, None)
    assert out["posts"][0]["creator_tier_at_post"] == "micro"


def test_a_mismatched_source_country_never_replaces_post_location():
    out = run("tiktok/search/top", {"query": "matatu", "country": "ZA"}, "KE",
              fixture("search", "search_top"), lane="confirm")
    assert out["posts"][0]["geo_market"] == "ZA"
    assert {(o["source_market"], o["source_region"]) for o in out["observations"]} == {(None, None)}


def test_scoped_source_market_does_not_replace_vendor_location():
    out = run("tiktok/search/top", {"query": "matatu", "country": "KE"}, "KE",
              fixture("search", "search_top"), lane="confirm")
    assert out["posts"][0]["geo_market"] == "ZA"
    assert {(o["source_market"], o["source_region"]) for o in out["observations"]} == {("KE", "KE")}


# Re-reads

def test_post_stats_rereads_are_observations_with_metrics():
    out = run("prism/post-stats", {}, "ZA", fixture("restat", "post_stats"))
    assert out["posts"] == [] and out["counters"] == []
    first, second = out["observations"]
    assert first["post_id"] == ids.post_id("tiktok", "7400000000000000001")
    assert (first["views"], first["likes"], first["comments"], first["shares"]) == (20000, 1500, 60, 20)
    assert second["platform"] == "youtube" and second["post_id"] == ids.post_id("youtube", "yt_aaa")
    for o in (first, second):
        assert (o["lane"], o["lane_class"], o["series"], o["route"]) == (
            "watchlist", "unbiased_counter", "counter_post_views", "prism/post-stats")


def test_post_stats_keeps_zero_readings_and_falls_back_only_when_none_are_given():
    nested = {"id": "7400000000000000001", "engagement": {"views": 99, "likes": 9, "comments": 9, "shares": 9}}
    body = {"success": True, "data": {"items": [
        {"url": "https://www.tiktok.com/@a/video/7400000000000000001", "platform": "tiktok", "status": "ok",
         "engagement": {"views": 0, "likes": 0, "comments": 0, "shares": 0}, "post": nested},
        {"url": "https://www.tiktok.com/@a/video/7400000000000000001", "platform": "tiktok", "status": "ok",
         "post": nested}]}}
    zeros, fallback = run("prism/post-stats", {}, "ZA", body)["observations"]
    assert (zeros["views"], zeros["likes"], zeros["comments"], zeros["shares"]) == (0, 0, 0, 0)
    assert (fallback["views"], fallback["likes"], fallback["comments"], fallback["shares"]) == (99, 9, 9, 9)


# Engagement

def test_engagement_is_null_unless_likes_comments_and_shares_are_all_read():
    trending = run("youtube/videos/trending", {"region": "NG", "category": "music"}, "NG",
                   fixture("rank", "youtube_trending"), pull_seq=1)
    assert trending["posts"][0]["likes"] == 9000 and trending["posts"][0]["shares"] is None
    assert trending["posts"][0]["engagement"] is None
    reddit = run("reddit/subreddit", {"subreddit": "southafrica"}, "ZA", fixture("rank", "reddit_subreddit"), pull_seq=1)
    assert all(p["engagement"] is None for p in reddit["posts"])
    body = {"success": True, "data": {"items": [{"post": {
        "id": "z1", "url": "https://www.tiktok.com/@a/video/z1",
        "engagement": {"likes": 0, "comments": 0, "shares": 0}}}]}}
    quiet = run("tiktok/search/top", {"query": "x"}, "ZA", body, lane="expansion")
    assert quiet["posts"][0]["engagement"] == 0


# Live probe shapes (parse_live.json: synthetic values in the 28 September 2026 probe's key shapes)

def live(key):
    return fixture("live", key)


def test_live_feed_posts_read_content_ext_and_the_epoch_fallback():
    out = run("tiktok/trending", {"region": "ZA", "feed": "local"}, "ZA", live("tiktok_trending"), pull_seq=1)
    first, second = out["posts"]
    assert (first["creator_id"], first["sound_id"], first["hashtags"]) == (
        "kasi.keys", "7300000000000000001", ["kasivibes"])
    assert first["duration_s"] == 21.4 and first["thumbnail_url"] == "https://p16.example/t1.jpg"
    assert first["published_at"] == "2026-09-27T22:00:00+00:00" and first["post_date"] == "2026-09-28"
    assert first["engagement"] == 739 and first["geo_market"] == "ZA"
    assert second["published_at"] == "2026-09-27T11:00:00+00:00"        # post.published_at null, ext epoch
    assert second["likes"] is None and second["engagement"] is None     # no invented zeros
    assert second["duration_s"] is None and second["thumbnail_url"] is None
    assert first["vendor_labels"] == {}
    assert [o["rank"] for o in out["observations"]] == [1, 2]
    got = {c["item_id"]: c["value"] for c in out["counters"]}
    assert got == {"hashtag|kasivibes": 1.0, "sound|tiktok:7300000000000000001": 1.0, "creator|tiktok:kasi.keys": 1.0,
                   "sound|tiktok:7300000000000000002": 2.0, "creator|tiktok:pap.and.wors": 2.0}


def test_live_youtube_creator_is_the_ext_channel_id_when_the_username_is_null():
    out = run("youtube/videos/trending", {"region": "NG", "category": "sports"}, "NG", live("youtube_trending"),
              pull_seq=1)
    (post,) = out["posts"]
    assert post["creator_id"] == "UCsyntheticPitch01" and post["duration_s"] == 612.0
    assert post["text"] == "Derby recap" and post["shares"] is None and post["engagement"] is None
    assert {c["item_id"]: c["value"] for c in out["counters"]} == {"creator|youtube:ucsyntheticpitch01": 1.0}


def test_live_search_top_tier_comes_from_ext_author_followers():
    out = run("tiktok/search/top", {"query": "matatu", "country": "KE"}, "KE", live("search_top"), lane="exploration")
    (post,) = out["posts"]
    assert post["creator_tier_at_post"] == "micro" and post["geo_market"] == "KE"


def test_live_hashtag_board_reads_name_and_rank_from_the_post_envelope():
    out = run("tiktok/hashtags/popular", {"countryCode": "ZA", "period": "7"}, "ZA", live("hashtags_popular"),
              pull_seq=5)
    assert out["posts"] == [] and out["observations"] == []
    ranks = [(c["item_id"], c["value"]) for c in out["counters"] if c["unit"] == "rank"]
    assert ranks == [("hashtag|shebeenfriday", 2.0), ("hashtag|kotarun", 1.0), ("hashtag|taxirankdance", 3.0)]
    for c in out["counters"]:
        assert (c["series"], c["is_board"], c["lane_class"], c["market"], c["pull_seq"], c["platform"]) == (
            "board_tiktok_hashtag", True, "unbiased_rank", "ZA", 5, "tiktok")


def test_live_hashtag_popularity_curve_is_an_index_so_it_is_not_written_as_daily_counts():
    """popularity_curve runs 0 to 100 against each tag's own peak; it is not a count of posts per day."""
    out = run("tiktok/hashtags/popular", {"countryCode": "ZA", "period": "7"}, "ZA", live("hashtags_popular"),
              pull_seq=1)
    assert [c for c in out["counters"] if c["unit"] != "rank"] == []


def test_live_apple_chart_takes_the_song_id_and_ext_trend_rank_else_position():
    out = run("apple_music/charts", {"country": "ng", "type": "songs"}, "NG", live("apple_charts"), pull_seq=2)
    assert [(c["item_id"], c["value"]) for c in out["counters"]] == [
        ("sound|apple_music:1800000001", 1.0), ("sound|apple_music:1800000002", 2.0),
        ("sound|apple_music:1800000003", 3.0)]
    assert out["posts"] == [] and all(c["series"] == "board_apple_music" for c in out["counters"])


def test_live_tiktok_hashtag_total_is_the_post_ext_video_count():
    tag = run("tiktok/hashtag", {"hashtag": "KotaRun"}, "ZA", live("tiktok_hashtag"))
    (c,) = tag["counters"]
    assert (c["item_id"], c["value"], c["market"], c["unit"], c["series"]) == (
        "hashtag|kotarun", 43210.0, "GLOBAL", "total", "counter_tiktok_hashtag")
    assert tag["posts"] == [] and tag["observations"] == []
    named = run("tiktok/hashtag", {}, "ZA", live("tiktok_hashtag"))
    assert [c["item_id"] for c in named["counters"]] == ["hashtag|kotarun"]
    assert run("tiktok/hashtag", {"hashtag": "kotarun"}, "ZA", live("tiktok_hashtag_no_count"))["counters"] == []


def test_live_search_multi_takes_the_platform_from_each_item():
    out = run("search/multi", {"query": "invented", "platforms": "instagram,twitter,reddit"}, "ZA",
              live("search_multi"), lane="expansion")
    assert [(p["platform"], p["native_id"]) for p in out["posts"]] == [
        ("instagram", "3700000000000000001"), ("twitter", "1972000000000000001"), ("reddit", "t3_synth1")]
    assert out["posts"][0]["vendor_labels"] == {
        "labels": None, "relevance": {"depth": 0.6, "p": 0.8, "sense": "on_topic", "spam": 0.05}}
    assert [o["lane_class"] for o in out["observations"]] == ["search_presence"] * 3
    assert out["counters"] == []
    estimate = run("search/multi", {"query": "invented"}, "ZA", live("search_multi_estimate"), lane="expansion")
    assert estimate == {"posts": [], "observations": [], "counters": []}


# Spec-shaped, not live (parse_spec.json): tiktok/search/hashtag, tiktok/profile/videos and instagram/audio/reels
# were not probed, so these bodies take sc_routes.json's parameters and the probed {computed, post} envelope.
# Each fixture is replaced by a stored live body once the route has been called.

def spec(key):
    return fixture("spec", key)


def test_spec_routes_rows_carry_exactly_the_ddl_columns():
    cases = [
        run("tiktok/search/hashtag", {"hashtag": "kotarun"}, "ZA", spec("search_hashtag"), lane="expansion"),
        run("tiktok/profile/videos", {"handle": "kasi.keys"}, "ZA", spec("profile_videos")),
        run("instagram/audio/reels", {"audio_id": "ig_audio_77"}, "GLOBAL", spec("ig_audio_reels")),
    ]
    want = {"posts": ddl_columns("posts"), "observations": ddl_columns("post_observations"),
            "counters": ddl_columns("item_counter_daily")}
    for out in cases:
        assert out["posts"] and out["observations"]
        for name, rows in out.items():
            for row in rows:
                assert list(row) == want[name], name
        json.dumps(out)


@pytest.mark.parametrize("lane", ["expansion", "exploration", "anchor", "confirm"])
def test_spec_search_hashtag_is_search_presence_in_the_callers_lane(lane):
    geo = FakeGeo()
    params = {"hashtag": "kotarun", "region": "ZA", "max_age_days": "7", "max_pages": "2", "seen": "s9", "trim": "true"}
    try:
        out = run("tiktok/search/hashtag", params, "ZA", spec("search_hashtag"), lane=lane,
                  seed_key="hashtag|kotarun", geo=geo)
    except ValueError as exc:
        pytest.fail(f"search lane was rejected: {exc}")
    first, second = out["posts"]
    assert (first["platform"], first["native_id"], first["creator_id"], first["creator_tier_at_post"]) == (
        "tiktok", "7400000000000000101", "kota.queen", "micro")
    assert first["hashtags"] == ["kotarun", "food"] and first["sound_id"] == "7300000000000000009"
    assert first["published_at"] == "2026-09-27T09:00:00+00:00" and first["engagement"] == 850
    assert first["geo_market"] == "ZA" and second["geo_market"] == "NG"
    assert first["vendor_labels"] == {"labels": {"sponsored": False, "intent": "entertain", "niche": "food"}}
    assert second["published_at"] is None and second["engagement"] is None
    for o in out["observations"]:
        assert (o["lane"], o["lane_class"], o["series"], o["seed_key"], o["rank"], o["pull_seq"]) == (
            lane, "search_presence", "search", "hashtag|kotarun", None, None)
        assert o["protocol"] == "tiktok/search/hashtag?max_age_days=7&region=ZA"
        assert o["route"] == "tiktok/search/hashtag" and o["market"] == "ZA"
    assert out["counters"] == []


@pytest.mark.parametrize("lane", ["anchor", "placebo"])
@pytest.mark.parametrize("route,params,body", [
    ("tiktok/song/videos", {"clipId": "7300000000000000009", "use": 1}, ("counters", "song_videos")),
    ("twitter/user/tweets", {"handle": "SundayTimesZA", "since": "2026-09-26"}, ("panels", "twitter_tweets")),
    ("facebook/profile/posts", {"pageId": "12345", "since": "2026-09-26"}, ("panels", "facebook_posts")),
])
def test_seed_templates_on_curve_and_panel_routes_use_search_presence(route, params, body, lane):
    try:
        out = run(route, params, "ZA", fixture(*body), lane=lane, seed_key="creator|x")
    except ValueError as exc:
        pytest.fail(f"seed template lane was rejected: {exc}")
    assert out["observations"] and out["counters"] == []
    series = "placebo" if lane == "placebo" else "search"
    assert {(o["lane"], o["lane_class"], o["series"], o["market"], o["seed_key"]) for o in out["observations"]} == {
        (lane, "search_presence", series, "ZA", "creator|x")}
    assert {(o["source_market"], o["source_region"]) for o in out["observations"]} == {(None, None)}


def test_spec_search_hashtag_needs_a_search_lane_and_placebo_is_its_own_series():
    body = spec("search_hashtag")
    with pytest.raises(ValueError):
        run("tiktok/search/hashtag", {"hashtag": "kotarun"}, "ZA", body)
    with pytest.raises(ValueError):
        run("tiktok/search/hashtag", {"hashtag": "kotarun"}, "ZA", body, lane="watchlist")
    with pytest.raises(ValueError):
        run("tiktok/search/hashtag", {"hashtag": "kotarun"}, "GLOBAL", body, lane="expansion")
    placebo = run("tiktok/search/hashtag", {"hashtag": "kotarun"}, "ZA", body, lane="placebo")
    assert {o["series"] for o in placebo["observations"]} == {"placebo"}


def test_spec_profile_videos_are_watchlist_sightings_of_a_new_feed_author():
    out = run("tiktok/profile/videos", {"handle": "kasi.keys", "sort_by": "latest", "max_cursor": "0"}, "ZA",
              spec("profile_videos"), seed_key="creator|tiktok:kasi.keys")
    first, second = out["posts"]
    assert first["creator_id"] == "kasi.keys" and second["creator_id"] == "kasi.keys"   # handle when username is null
    assert first["creator_tier_at_post"] == "nano" and second["creator_tier_at_post"] is None
    assert first["sound_id"] == "7300000000000000001" and first["hashtags"] == ["amapiano"]
    assert (first["views"], first["engagement"]) == (5200, 320)
    assert second["published_at"] == "2026-09-02T12:00:00+00:00"       # older posts are the creator's usual views
    assert second["post_date"] == "2026-09-02"
    for o in out["observations"]:
        assert (o["lane"], o["lane_class"], o["series"], o["route"], o["rank"], o["pull_seq"]) == (
            "watchlist", "watchlist", "watch", "tiktok/profile/videos", None, None)
        assert o["protocol"] == "tiktok/profile/videos?sort_by=latest"
        assert o["seed_key"] == "creator|tiktok:kasi.keys"
    assert [o["views"] for o in out["observations"]] == [5200, 1400]
    assert out["counters"] == []
    assert run("tiktok/profile/videos", {"handle": "kasi.keys"}, "ZA", spec("profile_videos"),
               lane="watchlist")["observations"][0]["lane_class"] == "watchlist"


def test_spec_profile_videos_from_the_seed_queue_are_search_presence():
    body = spec("profile_videos")
    out = run("tiktok/profile/videos", {"handle": "kasi.keys"}, "NG", body, lane="expansion")
    assert {(o["lane"], o["lane_class"], o["series"], o["market"]) for o in out["observations"]} == {
        ("expansion", "search_presence", "search", "NG")}
    placebo = run("tiktok/profile/videos", {"handle": "kasi.keys"}, "KE", body, lane="placebo")
    assert {o["series"] for o in placebo["observations"]} == {"placebo"}
    with pytest.raises(ValueError):
        run("tiktok/profile/videos", {"handle": "kasi.keys"}, "ZA", body, lane="panel")
    with pytest.raises(ValueError):
        run("tiktok/profile/videos", {"handle": "kasi.keys"}, "GLOBAL", body)


def test_spec_ig_audio_reels_total_is_a_global_counter_and_reels_are_watchlist_sightings():
    geo = FakeGeo()
    out = run("instagram/audio/reels", {"audio_id": "ig_audio_77", "cursor": "c1"}, "GLOBAL", spec("ig_audio_reels"),
              geo=geo)
    (c,) = out["counters"]
    assert (c["item_id"], c["value"], c["market"], c["platform"], c["series"]) == (
        "sound|instagram:ig_audio_77", 5400.0, "GLOBAL", "instagram", "counter_ig_audio")
    assert (c["unit"], c["lane_class"], c["is_board"], c["pull_seq"], c["source"], c["obs_date"]) == (
        "total", "unbiased_counter", False, None, "live", "2026-09-28")
    assert c["protocol"] == "instagram/audio/reels"
    first, second = out["posts"]
    assert (first["platform"], first["native_id"], first["creator_id"], first["creator_tier_at_post"]) == (
        "instagram", "3700000000000000301", "jozi.moves", "mid")
    assert first["sound_id"] == "ig_audio_77" and second["sound_id"] == "ig_audio_77"   # the page's own sound
    assert first["geo_market"] is None and geo.calls == []
    assert second["views"] is None and second["engagement"] is None
    for o in out["observations"]:
        assert (o["lane"], o["lane_class"], o["series"], o["market"], o["platform"]) == (
            "watchlist", "watchlist", "watch", "GLOBAL", "instagram")


def test_spec_ig_audio_reels_without_a_total_writes_no_counter():
    """DATA.md 3.2: counter_ig_audio is unbiased_counter only if a total is returned, else watchlist.
    data.total is the page's row count, not the audio's reel count, so it is never read as one."""
    out = run("instagram/audio/reels", {"audio_id": "ig_audio_77"}, "ZA", spec("ig_audio_reels_no_total"))
    assert out["counters"] == []
    (obs,) = out["observations"]
    assert (obs["lane_class"], obs["series"], obs["market"]) == ("watchlist", "watch", "ZA")
    assert out["posts"][0]["sound_id"] == "ig_audio_77"


def test_spec_ig_audio_reels_from_the_seed_queue_write_no_counter():
    """A seed-queue read (expansion, exploration, placebo) is search_presence and starts no counter series."""
    out = run("instagram/audio/reels", {"audio_id": "ig_audio_77"}, "ZA", spec("ig_audio_reels"), lane="expansion")
    assert out["counters"] == []
    assert {(o["lane"], o["lane_class"], o["series"]) for o in out["observations"]} == {
        ("expansion", "search_presence", "search")}
    with pytest.raises(ValueError):
        run("instagram/audio/reels", {"audio_id": "ig_audio_77"}, "ZA", spec("ig_audio_reels"), lane="sweep")


# Routes

def test_unknown_route_raises():
    with pytest.raises(ValueError):
        run("tiktok/user/audience", {}, "ZA", {"data": {"items": []}})
    with pytest.raises(ValueError):
        run("tiktok/trending", {"feed": "local"}, "GH", fixture("rank", "tiktok_trending"), pull_seq=1)


def test_x_trends_scrape_is_an_explicit_no_rows_route():
    assert run("web/scrape", {"url": "https://example.org"}, "ZA", {"data": {"markdown": "x"}}) == {
        "posts": [], "observations": [], "counters": []}


def test_failed_or_empty_bodies_give_no_rows():
    for body in ({"success": False, "error": {"type": "RESOURCE_NOT_FOUND"}}, {"success": True, "data": {"items": []}},
                 None):
        out = run("facebook/profile/posts", {"pageId": "1"}, "ZA", body)
        assert out == {"posts": [], "observations": [], "counters": []}
