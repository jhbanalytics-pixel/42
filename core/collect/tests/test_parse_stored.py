"""Parser tests on stored SocialCrawl response shapes (fixtures/parse_stored_*.json).

Each fixture is a stored raw_responses body from the 28 September to 7 October 2026 window, stripped to structure:
keys, types, nesting, counts, timestamps and metric numbers stay; every text, handle, name, id and url is a
placeholder. Expected counts below are pinned numbers counted by hand from the fixture, not read from it.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from core.collect.parse import parse, parse_with_creators
from core.collect.tests.test_parse import FakeGeo, fake_item_id

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STORED = {}
for _name in ("prism", "song", "song_videos"):
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


def _post_authors(entry):
    """Posts per author username, counted from the items; an author can differ from the profile a row targets."""
    got = {}
    for row in entry["body"]["data"]["results"]:
        for item in (row.get("posts") or {}).get("items", []):
            name = item["post"]["author"]["username"]
            got[name] = got.get(name, 0) + 1
    return got


@pytest.mark.parametrize("name", sorted(PRISM_POSTS))
def test_stored_prism_profiles_creator_is_the_posts_own_author(name):
    entry = stored(name)
    out = run_stored("prism/profiles", name, {"include": "posts", "since": "2026-10-03"})
    got = {}
    for post in out["posts"]:
        got[post["creator_id"]] = got.get(post["creator_id"], 0) + 1
    assert None not in got and got == _post_authors(entry)


def test_stored_prism_profiles_creator_falls_back_to_the_rows_target_handle():
    entry = stored("prism_profiles_KE")
    body = json.loads(json.dumps(entry["body"]))
    for row in body["data"]["results"]:
        for item in (row.get("posts") or {}).get("items", []):
            del item["post"]["author"]
    out = parse("prism/profiles", {"include": "posts", "since": "2026-10-03"}, entry["market"], body,
                entry["fetched_at"], "run1", item_id_fn=fake_item_id, geo_fn=FakeGeo())
    wanted = {r["target"]["handle"]: len(r["posts"]["items"]) for r in entry["body"]["data"]["results"]
              if r["status"] == "ok" and r["posts"]["items"]}
    got = {}
    for post in out["posts"]:
        got[post["creator_id"]] = got.get(post["creator_id"], 0) + 1
    assert got == wanted


STORED_AUTHOR_KEYS = {"avatar_url", "bio", "display_name", "ext", "followers", "following", "id", "joined_at",
                      "last_post_at", "likes_count", "posts_count", "private", "url", "username", "verified"}


def _stored_row_shape_problems(body):
    """What a prism/profiles body must look like to be the vendor's: rows in data.results, each naming its profile
    under target, an ok row holding its profile under data and its posts as an object with an items list."""
    problems = []
    rows = body.get("data", {}).get("results")
    if not isinstance(rows, list) or not rows:
        return ["data.results is not a non-empty list"]
    for i, row in enumerate(rows):
        if not isinstance(row.get("target"), dict) or not row["target"].get("handle"):
            problems.append(f"row {i} has no target.handle")
        for old in ("handle", "author", "profile", "items"):
            if old in row:
                problems.append(f"row {i} carries {old} at row level")
        if row.get("status") == "ok":
            if not isinstance(row.get("data"), dict):
                problems.append(f"row {i} has no data object")
            elif set(row["data"].get("author") or {}) - STORED_AUTHOR_KEYS:
                problems.append(f"row {i} data.author has a key the vendor does not send")
            posts = row.get("posts")
            if posts is not None and not (isinstance(posts, dict) and isinstance(posts.get("items"), list)):
                problems.append(f"row {i} posts is not an object with an items list")
    return problems


def test_every_prism_profiles_fixture_in_the_suite_has_the_stored_row_shape():
    tests = Path(__file__).resolve().parent / "fixtures"
    bodies = {name: STORED[name]["body"] for name in PRISM_POSTS}
    bodies["parse_panels"] = json.loads((tests / "parse_panels.json").read_text(encoding="utf-8"))["prism_profiles"]
    bodies["local_profiles"] = json.loads((tests / "local_profiles.json").read_text(encoding="utf-8"))
    bodies["job_responses"] = json.loads((tests / "job_responses.json").read_text(encoding="utf-8"))["prism/profiles"]
    assert {name: _stored_row_shape_problems(body) for name, body in bodies.items()} == {name: [] for name in bodies}


def test_the_shape_check_rejects_the_flat_list_shape_the_suite_used_to_pin():
    old = {"success": True, "data": {"items": [{"platform": "instagram", "handle": "h", "status": "ok",
                                                 "author": {"username": "h"}, "posts": [{"post": {"id": "p"}}]}]}}
    assert _stored_row_shape_problems(old) == ["data.results is not a non-empty list"]
    old["data"] = {"results": old["data"]["items"]}
    assert len(_stored_row_shape_problems(old)) == 5


# Older and fallback row shapes. No stored row has them, so each is pinned by an inline body.

LEGACY_POST = {"post": {"id": "p1", "url": "https://example.invalid/p1", "published_at": "2026-09-28T00:10:00Z",
                        "content": {"text": "caption"}}}


def _legacy_run(row, **kw):
    body = {"success": True, "data": {"results": [{"platform": "instagram", "status": "ok", **row}]}}
    return parse_with_creators("prism/profiles", {"include": "posts"}, "ZA", body, "2026-09-28T00:30:00Z", "run1",
                               item_id_fn=fake_item_id, geo_fn=FakeGeo(), **kw)


@pytest.mark.parametrize("key", ["author", "profile"])
def test_a_row_level_profile_block_fills_what_the_post_lacks(key):
    out = _legacy_run({"handle": "h1", key: {"username": "h1", "followers": 750000, "location": "Johannesburg"},
                       "posts": [LEGACY_POST]})
    [post] = out["posts"]
    assert (post["creator_id"], post["creator_tier_at_post"], post["geo_market"]) == ("h1", "macro", "ZA")
    assert [(c["followers"], c["profile_location"]) for c in out["creators"]] == [(750000, "Johannesburg")]


def test_a_row_level_handle_is_the_creator_when_nothing_else_names_one():
    [post] = _legacy_run({"handle": "h1", "posts": [LEGACY_POST]})["posts"]
    assert post["creator_id"] == "h1"


def test_the_target_handle_is_the_creator_when_the_row_has_no_profile_block():
    row = {"target": {"platform": "instagram", "handle": "t1"}, "posts": {"status": "ok", "items": [LEGACY_POST]}}
    assert "data" not in row
    [post] = _legacy_run(row)["posts"]
    assert post["creator_id"] == "t1"


def test_a_row_level_items_list_is_read_when_the_row_has_no_posts():
    out = _legacy_run({"handle": "h1", "items": [LEGACY_POST]})
    assert [p["native_id"] for p in out["posts"]] == ["p1"]


# tiktok/song edges

def _song_body(**ext):
    return {"success": True, "data": {"post": {"id": "7300000000000000009", "ext": ext}}}


def _song(route, body, params):
    return parse(route, params, "ZA", body, "2026-09-28T00:30:00Z", "run1", item_id_fn=fake_item_id, geo_fn=FakeGeo())


def test_a_song_count_with_no_unit_is_still_read():
    out = _song("tiktok/song", _song_body(use_count=412), {"clipId": "m100"})
    assert [(c["item_id"], c["value"]) for c in out["counters"]] == [("sound|tiktok:m100", 412.0)]


def test_use_count_is_read_for_the_song_route_only():
    body = {"success": True, "data": {"post": {"ext": {"use_count": 412, "use_count_unit": "videos"}}}}
    assert _song("tiktok/hashtag", body, {"hashtag": "amapiano"})["counters"] == []


def test_the_curve_route_writes_no_counter_even_when_the_body_carries_a_count():
    body = {"success": True, "data": {"video_count": 7, "adoption": {"by_day": [{"date": "2026-09-27", "videos": 1}]},
                                      "post": {"ext": {"use_count": 412, "video_count": 7}}, "items": []}}
    assert _song("tiktok/song/videos", body, {"clipId": "m100"})["counters"] == []


def test_adoption_sample_points_counts_the_list_and_the_object_forms():
    from core.collect.parse import adoption_sample_points

    assert adoption_sample_points({"data": {"adoption": [{"date": "2026-09-27", "videos": 1}] * 3}}) == 3
    assert adoption_sample_points({"data": {"adoption": {"by_day": [{"date": "2026-09-27", "videos": 1}] * 2}}}) == 2
    assert adoption_sample_points({"data": {}}) == 0 and adoption_sample_points(None) == 0


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


# tiktok/song/videos: the adoption object is a page sample and is not written as a series

ADOPTION_DAYS = {"song_videos_1": 14, "song_videos_2": 16, "song_videos_3": 0}  # by_day points, counted by hand
SONG_VIDEO_POSTS = {"song_videos_1": 14, "song_videos_2": 18, "song_videos_3": 0}


@pytest.mark.parametrize("name", sorted(ADOPTION_DAYS))
def test_stored_song_videos_write_no_counter_row_to_any_series(name):
    out = run_stored("tiktok/song/videos", name, {"clipId": "m100"})
    assert out["counters"] == []  # nothing for item_counter_daily, so nothing for detect to read from this route


@pytest.mark.parametrize("name", sorted(SONG_VIDEO_POSTS))
def test_stored_song_videos_still_give_the_watchlist_posts(name):
    out = run_stored("tiktok/song/videos", name, {"clipId": "m100"})
    assert len(out["posts"]) == SONG_VIDEO_POSTS[name] == len(out["observations"])
    assert {(o["lane"], o["lane_class"], o["series"]) for o in out["observations"]} <= {
        ("watchlist", "watchlist", "watch")}


@pytest.mark.parametrize("name", sorted(ADOPTION_DAYS))
def test_stored_song_videos_sample_points_are_counted(name):
    from core.collect.parse import adoption_sample_points

    assert adoption_sample_points(stored(name)["body"]) == ADOPTION_DAYS[name]


def test_stored_song_videos_sample_is_the_page_not_the_sound():
    # Why nothing is written: by_day totals the videos on the page it came with, in every stored body.
    for name in ("song_videos_1", "song_videos_2"):
        data = stored(name)["body"]["data"]
        assert sum(p["videos"] for p in data["adoption"]["by_day"]) == data["adoption"]["dated"] == len(data["items"])


def test_the_list_shaped_adoption_curve_is_not_written_either():
    entry = stored("song_videos_1")
    body = json.loads(json.dumps(entry["body"]))
    body["data"]["adoption"] = [{"date": "2026-10-06", "videos": 4}, {"date": "2026-10-07", "videos": 9}]
    out = parse("tiktok/song/videos", {"clipId": "m100"}, entry["market"], body, entry["fetched_at"], "run1",
                item_id_fn=fake_item_id, geo_fn=FakeGeo())
    assert out["counters"] == []
