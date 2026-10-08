"""Parser tests on stored SocialCrawl response shapes (fixtures/parse_stored_*.json).

Each fixture is a stored raw_responses body from the 28 September to 7 October 2026 window, stripped to structure:
keys, types, nesting, counts, timestamps and metric numbers stay; every text, handle, name, id and url is a
placeholder. Expected counts below are pinned numbers counted by hand from the fixture, not read from it.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from core.collect.parse import parse
from core.collect.tests.test_parse import FakeGeo, fake_item_id

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STORED = {}
for _name in ("prism", "song"):
    STORED.update(json.loads((FIXTURES / f"parse_stored_{_name}.json").read_text(encoding="utf-8")))


def stored(name):
    return STORED[name]


def run_stored(route, name, params=None, **kw):
    entry = stored(name)
    return parse(route, params or {}, entry["market"], entry["body"], entry["fetched_at"], "run1",
                 item_id_fn=fake_item_id, geo_fn=FakeGeo(), **kw)


# prism/profiles: the culture desk and gossip panels

PRISM_POSTS = {
    "prism_profiles_ZA": {"instagram": 4, "tiktok": 11},
    "prism_profiles_NG": {"instagram": 32, "tiktok": 6},
    "prism_profiles_KE": {"instagram": 3},
}


@pytest.mark.parametrize("name", sorted(PRISM_POSTS))
def test_stored_prism_profiles_give_the_posts_held_in_each_rows_posts_object(name):
    out = run_stored("prism/profiles", name, {"include": "posts", "since": "2026-10-03"})
    expected = PRISM_POSTS[name]
    got = {}
    for post in out["posts"]:
        got[post["platform"]] = got.get(post["platform"], 0) + 1
    assert got == expected
    assert len({p["post_id"] for p in out["posts"]}) == sum(expected.values())
    assert len(out["observations"]) == sum(expected.values())
    assert {o["series"] for o in out["observations"]} == {"panel_culture_desk"}
    assert {(o["lane"], o["lane_class"]) for o in out["observations"]} == {("panel", "panel")}
    assert {o["protocol"] for o in out["observations"]} == {"prism/profiles?include=posts"}
    assert out["counters"] == []
    assert all(p["endpoint"] == "prism/profiles" and p["published_at"] for p in out["posts"])


def test_stored_prism_profiles_since_still_drops_older_posts():
    # 37 of the 38 NG posts are dated 3 October in Lagos, one 4 October.
    out = run_stored("prism/profiles", "prism_profiles_NG", {"include": "posts", "since": "2026-10-04"})
    assert len(out["posts"]) == 1
    assert out["posts"][0]["post_date"] == "2026-10-04"


def test_stored_prism_profiles_empty_and_not_found_rows_give_no_posts():
    entry = stored("prism_profiles_KE")
    rows = entry["body"]["data"]["results"]
    assert sorted({(r["status"], (r.get("posts") or {}).get("status")) for r in rows}) == [
        ("not_found", None), ("ok", "empty"), ("ok", "ok")]
    out = run_stored("prism/profiles", "prism_profiles_KE", {"include": "posts", "since": "2026-10-03"})
    assert len(out["posts"]) == 3  # only the three posts in the one row that holds them


def test_stored_prism_profiles_creators_carry_the_profile_followers():
    from core.collect.parse import parse_with_creators

    entry = stored("prism_profiles_KE")
    out = parse_with_creators("prism/profiles", {"include": "posts", "since": "2026-10-03"}, entry["market"],
                              entry["body"], entry["fetched_at"], "run1", item_id_fn=fake_item_id, geo_fn=FakeGeo())
    followers = {r["data"]["author"]["username"]: r["data"]["author"]["followers"]
                 for r in entry["body"]["data"]["results"] if r["status"] == "ok"}
    assert len(out["creators"]) == 3
    assert all(p["creator_tier_at_post"] is not None for p in out["posts"])
    for creator in out["creators"]:
        assert creator["followers"] == followers[creator["handle"]]
        assert isinstance(creator["followers"], int)


def test_the_flat_list_panel_shapes_still_parse():
    # The shape the earlier fixtures use (posts as a list on the row) must keep working.
    body = {"success": True, "data": {"results": [{"status": "ok", "platform": "instagram", "posts": [
        {"post": {"id": "p1", "url": "https://example.invalid/p1", "published_at": "2026-09-28T00:10:00Z",
                  "author": {"username": "h1"}}}]}]}}
    out = parse("prism/profiles", {"include": "posts"}, "ZA", body, "2026-09-28T00:30:00Z", "run1",
                item_id_fn=fake_item_id, geo_fn=FakeGeo())
    assert [p["native_id"] for p in out["posts"]] == ["p1"]
    assert datetime.fromisoformat(out["posts"][0]["published_at"]).year == 2026


# tiktok/song: the running count of a sound

SONG_COUNTS = {  # fixture: (use_count, SAST day of the fetch, which is the GLOBAL row's day)
    "tiktok_song_ZA": (84, "2026-09-28"),
    "tiktok_song_NG": (299820, "2026-10-07"),
    "tiktok_song_KE": (101247, "2026-10-02"),
}


@pytest.mark.parametrize("name", sorted(SONG_COUNTS))
def test_stored_tiktok_song_count_is_the_use_count_in_post_ext(name):
    entry = stored(name)
    ext = entry["body"]["data"]["post"]["ext"]
    assert sorted(ext) == ["music_id", "published_at_epoch", "use_count", "use_count_unit"]
    out = run_stored("tiktok/song", name)
    (counter,) = out["counters"]
    value, day = SONG_COUNTS[name]
    sound = ext["music_id"]
    assert (counter["item_id"], counter["value"], counter["obs_date"]) == (f"sound|tiktok:{sound}", float(value), day)
    assert (counter["market"], counter["platform"], counter["series"], counter["unit"], counter["lane_class"],
            counter["is_board"], counter["source"], counter["route"]) == (
        "GLOBAL", "tiktok", "counter_tiktok_sound", "total", "unbiased_counter", False, "live", "tiktok/song")
    assert out["posts"] == [] and out["observations"] == []


def test_stored_tiktok_song_count_follows_the_calls_clip_id():
    out = run_stored("tiktok/song", "tiktok_song_ZA", {"clipId": "m100"})
    assert [(c["item_id"], c["value"]) for c in out["counters"]] == [("sound|tiktok:m100", 84.0)]


def test_stored_tiktok_song_count_is_dropped_when_the_unit_is_not_videos():
    entry = stored("tiktok_song_ZA")
    body = json.loads(json.dumps(entry["body"]))
    body["data"]["post"]["ext"]["use_count_unit"] = "plays"
    out = parse("tiktok/song", {}, entry["market"], body, entry["fetched_at"], "run1",
                item_id_fn=fake_item_id, geo_fn=FakeGeo())
    assert out["counters"] == []


def test_stored_tiktok_song_without_a_use_count_writes_no_counter():
    entry = stored("tiktok_song_ZA")
    body = json.loads(json.dumps(entry["body"]))
    del body["data"]["post"]["ext"]["use_count"]
    out = parse("tiktok/song", {}, entry["market"], body, entry["fetched_at"], "run1",
                item_id_fn=fake_item_id, geo_fn=FakeGeo())
    assert out["counters"] == []
