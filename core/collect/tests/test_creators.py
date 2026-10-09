"""Creators and post geo from collect: parse.parse_with_creators, the creators MERGE, the posts MERGE's geo
fill, and the job's --backfill-creators mode.

Bodies are the live-shaped parse_live.json and parse_panels.json fixtures. Geo is L2's real
core.detect.geo.geo_for_post. The MERGE statements run on DuckDB, with only MERGE `table` (DuckDB wants
MERGE INTO), UNNEST(@rows) and SAFE.PARSE_JSON swapped for their DuckDB forms.
"""

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from core.collect import gdelt, job, parse, writers
from core.collect.tests.test_job import FakeBQ, FakeClient, collect, fake_item_id
from core.detect import geo
from core.detect.geo import geo_for_post

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FETCHED = "2026-09-28T00:30:00Z"
FETCHED_ISO = "2026-09-28T00:30:00+00:00"
LIVE = json.loads((FIXTURES / "parse_live.json").read_text(encoding="utf-8"))
PANELS = json.loads((FIXTURES / "parse_panels.json").read_text(encoding="utf-8"))

FEED = ("tiktok/trending", {"region": "ZA", "feed": "local"}, "tiktok_trending", {"pull_seq": 1})
BOARD = ("youtube/videos/trending", {"region": "ZA"}, "youtube_trending", {"pull_seq": 1})
SEARCH = ("tiktok/search/top", {"query": "matatu", "country": "KE"}, "search_top", {"lane": "expansion"})
MULTI = ("search/multi", {"query": "invented"}, "search_multi", {"lane": "expansion"})
AGE_WORDS = re.compile(r"\b(age|ages|aged|birth\w*|dob|born|years?_old)\b", re.I)


def run(case, market="ZA", body=None, fetched_at=FETCHED):
    route, params, key, kw = case
    return parse.parse_with_creators(route, params, market, LIVE[key] if body is None else body, fetched_at,
                                     "run1", item_id_fn=fake_item_id, geo_fn=geo_for_post, **kw)


def geo_of(post):
    return post["geo_market"], post["geo_confidence"], post["geo_source"]


# Creators from parsed bodies

def test_feed_posts_give_one_creator_row_each_with_the_author_fields():
    out = run(FEED)
    assert [c["creator_id"] for c in out["creators"]] == [p["creator_id"] for p in out["posts"]]
    first, second = out["creators"]
    assert first == {
        "creator_id": "kasi.keys", "platform": "tiktok", "handle": "kasi.keys", "display_name": "Kasi Keys",
        "followers": None, "verified": False, "profile_location": None, "home_market": None,
        "first_seen": FETCHED_ISO, "last_seen": FETCHED_ISO}
    assert second["display_name"] is None and second["handle"] == "pap.and.wors"
    assert list(first) == list(parse.CREATOR_COLUMNS)


def test_search_posts_take_followers_from_ext_and_each_platform_its_own_creator():
    [creator] = run(SEARCH)["creators"]
    assert (creator["creator_id"], creator["platform"], creator["followers"]) == ("nai.steps", "tiktok", 45000)
    multi = run(MULTI)["creators"]
    assert [(c["platform"], c["creator_id"], c["display_name"]) for c in multi] == [
        ("instagram", "synth.kitchen", None), ("twitter", "synthdesk", "Synth Desk"),
        ("reddit", "synth_redditor", None)]
    assert multi[2]["verified"] is None  # null in the body stays unknown


def test_board_posts_key_the_creator_like_the_post_even_without_a_handle():
    out = run(BOARD)
    [creator] = out["creators"]
    assert creator["creator_id"] == out["posts"][0]["creator_id"] == "UCsyntheticPitch01"
    assert creator["handle"] is None and creator["display_name"] == "Pitch Side"


def test_panel_creators_take_the_handle_from_the_call_and_the_profile_location():
    tweets = parse.parse_with_creators(
        "twitter/user/tweets", {"handle": "SundayTimesZA", "since": "2026-09-27"}, "ZA", PANELS["twitter_tweets"],
        FETCHED, "run1", item_id_fn=fake_item_id, geo_fn=geo_for_post)
    assert {(c["creator_id"], c["handle"], c["display_name"]) for c in tweets["creators"]} >= {
        ("SundayTimesZA", "SundayTimesZA", "Sunday Times")}
    fb = parse.parse_with_creators(
        "facebook/profile/posts", {"pageId": "100064"}, "ZA", PANELS["facebook_posts"], FETCHED, "run1",
        item_id_fn=fake_item_id, geo_fn=geo_for_post)
    assert {(c["creator_id"], c["handle"], c["display_name"]) for c in fb["creators"]} == {("100064", None, "eNCA")}
    prism = parse.parse_with_creators(
        "prism/profiles", {"include": "posts"}, "ZA", PANELS["prism_profiles"], FETCHED, "run1",
        item_id_fn=fake_item_id, geo_fn=geo_for_post)
    [creator] = prism["creators"]
    assert (creator["handle"], creator["followers"], creator["profile_location"]) == (
        "culture.desk.za", 750000, None)  # the stored profile block carries no location


def test_boards_and_charts_are_not_posts_and_give_no_creators():
    out = parse.parse_with_creators("tiktok/hashtags/popular", {"countryCode": "ZA", "period": "7"}, "ZA",
                                    LIVE["hashtags_popular"], FETCHED, "run1", item_id_fn=fake_item_id,
                                    geo_fn=geo_for_post, pull_seq=1)
    assert out["creators"] == [] and out["posts"] == []


def test_parse_keeps_its_three_row_kinds_and_parse_with_creators_adds_the_fourth():
    plain = parse.parse(*FEED[:2], "ZA", LIVE[FEED[2]], FETCHED, "run1", item_id_fn=fake_item_id,
                        geo_fn=geo_for_post, **FEED[3])
    both = run(FEED)
    assert set(plain) == {"posts", "observations", "counters"}
    assert {k: v for k, v in both.items() if k != "creators"} == plain


def test_no_creator_field_holds_or_infers_age():
    for column in parse.CREATOR_COLUMNS:
        assert not AGE_WORDS.search(column.replace("_", " ")), column
    body = copy.deepcopy(LIVE["tiktok_trending"])
    body["data"]["items"][0]["post"]["author"].update({"age": 23, "birthday": "2003-01-01"})
    creator = run(FEED, body=body)["creators"][0]
    assert list(creator) == list(parse.CREATOR_COLUMNS)


# Geo on the live-shaped bodies

def located(out):
    return sum(p["geo_market"] is not None for p in out["posts"]), len(out["posts"])


def test_fixture_geo_counts():
    # Before this change 3 of 7 posts had a market (the TikTok ext.region ones); the live fixtures carry no
    # other evidence, so they stay NULL and nothing is invented.
    counts = [located(run(case)) for case in (FEED, BOARD, SEARCH, MULTI)]
    assert counts == [(2, 2), (0, 1), (1, 1), (0, 3)]


def test_youtube_audio_language_places_a_post_at_language_confidence():
    body = copy.deepcopy(LIVE["youtube_trending"])
    body["data"]["items"][0]["post"]["ext"]["defaultAudioLanguage"] = "zu"
    [post] = run(BOARD, body=body)["posts"]
    assert geo_of(post) == ("ZA", 0.3, "language")


def test_tiktok_content_language_is_read_when_computed_language_is_empty():
    body = copy.deepcopy(LIVE["tiktok_trending"])
    post = body["data"]["items"][1]["post"]
    del post["ext"]["region"]
    post["ext"]["content_language"] = "zu"
    second = run(FEED, body=body)["posts"][1]
    assert geo_of(second) == ("ZA", 0.3, "language")


def test_youtube_description_counts_as_post_text_for_place_mentions():
    body = copy.deepcopy(LIVE["youtube_trending"])
    body["data"]["items"][0]["post"]["ext"]["description"] = "Full recap from Soweto"
    [post] = run(BOARD, body=body)["posts"]
    assert geo_of(post) == ("ZA", 0.7, "place_mention")
    assert post["text"] == "Derby recap"  # the stored text is unchanged
    # the post text still counts next to the description: two countries named is unknown
    body["data"]["items"][0]["post"]["content"]["text"] = "Derby recap, Lagos"
    [both] = run(BOARD, body=body)["posts"]
    assert geo_of(both) == (None, None, None)


def test_a_trending_call_region_is_never_a_market():
    for region, market in (("ZA", "ZA"), ("NG", "NG"), ("KE", "KE")):
        [post] = run(("youtube/videos/trending", {"region": region}, "youtube_trending", {"pull_seq": 1}),
                     market=market)["posts"]
        assert geo_of(post) == (None, None, None)


def test_the_panel_profile_location_places_a_post_that_carries_its_own_author():
    # Older row shape: the profile block at row level, with a location. The stored data.author has none, so this
    # path is kept alive by an inline body.
    body = {"success": True, "data": {"results": [{"platform": "instagram", "status": "ok", "handle": "culture.desk.za",
        "author": {"username": "culture.desk.za", "followers": 750000, "location": "Johannesburg"},
        "posts": [{"post": {"id": "p1", "url": "https://example.invalid/p1", "published_at": "2026-09-28T00:10:00Z",
                            "author": {"username": "culture.desk.za"}, "content": {"text": "Fit check"}}}]}]}}
    out = parse.parse_with_creators("prism/profiles", {"include": "posts"}, "ZA", body, FETCHED, "run1",
                                    item_id_fn=fake_item_id, geo_fn=geo_for_post)
    assert geo_of(out["posts"][0]) == ("ZA", 0.8, "home_market")
    assert out["creators"][0]["profile_location"] == "Johannesburg"


# The creators MERGE, run on DuckDB

CREATORS_DDL = ("creator_id VARCHAR NOT NULL, platform VARCHAR, handle VARCHAR, followers BIGINT, tier VARCHAR, "
                "account_created_at TIMESTAMPTZ, home_market VARCHAR, verified_region VARCHAR, coord_score BIGINT, "
                "display_name VARCHAR, verified BOOLEAN, profile_location VARCHAR, first_seen TIMESTAMPTZ, "
                "last_seen TIMESTAMPTZ")
DUCK = {"STRING": "VARCHAR", "INT64": "BIGINT", "FLOAT64": "DOUBLE", "BOOL": "BOOLEAN", "TIMESTAMP": "TIMESTAMPTZ",
        "DATE": "DATE", "ARRAY": "VARCHAR[]", "JSON": "VARCHAR"}


def duck_sql(sql, name, source):
    return (sql.replace(f"MERGE `{writers.table(name)}`", f"MERGE INTO {name}").replace("UNNEST(@rows)", source)
            .replace("SAFE.PARSE_JSON(S.vendor_labels)", "S.vendor_labels"))


def duck_merge(con, sql, name, types, rows):
    """One MERGE with rows as its source, a fresh in-memory DuckDB table standing in for @rows."""
    source = f"src{con.execute('SELECT COUNT(*) FROM duckdb_tables()').fetchone()[0]}"
    con.execute(f"CREATE TABLE {source} (" + ", ".join(f"{c} {DUCK[t]}" for c, t in types.items()) + ")")
    for row in rows:
        con.execute(f"INSERT INTO {source} VALUES ({', '.join('?' for _ in types)})", [row.get(c) for c in types])
    con.execute(duck_sql(sql, name, source))


def creator(cid, seen, platform="tiktok", **kw):
    row = dict.fromkeys(parse.CREATOR_COLUMNS)
    row.update(creator_id=cid, platform=platform, first_seen=seen, last_seen=seen, **kw)
    return row


def creators_table(con):
    rows = con.execute("SELECT platform, creator_id, handle, display_name, followers, verified, profile_location, "
                       "first_seen, last_seen, coord_score FROM creators ORDER BY platform, creator_id").fetchall()
    return {(r[0], r[1]): r[2:] for r in rows}


def at(text):
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def test_creators_merge_inserts_new_and_moves_only_the_six_columns():
    con = duckdb.connect()
    con.execute(f"CREATE TABLE creators ({CREATORS_DDL})")
    sql = writers.CREATORS_MERGE_SQL.format(table=writers.table("creators"))
    first = "2026-09-28T00:30:00+00:00"
    duck_merge(con, sql, "creators", writers.CREATOR_TYPES, [
        creator("kasi.keys", first, handle="kasi.keys", display_name="Kasi Keys", followers=900, verified=False)])
    con.execute("UPDATE creators SET coord_score = 1")  # set by the co-action job; the MERGE leaves it
    later = "2026-09-29T00:30:00+00:00"
    duck_merge(con, sql, "creators", writers.CREATOR_TYPES, [
        creator("kasi.keys", later, handle="kasi.keys2", display_name=None, followers=1500, verified=True,
                profile_location="Durban"),
        creator("kasi.keys", later, platform="instagram", handle="kasi.keys")])
    table = creators_table(con)
    assert table[("tiktok", "kasi.keys")] == (
        "kasi.keys2", "Kasi Keys", 1500, True, "Durban", at(first), at(later), 1)
    assert table[("instagram", "kasi.keys")][0] == "kasi.keys" and len(table) == 2


def test_an_older_sighting_never_rolls_a_creator_back_and_first_seen_never_moves():
    con = duckdb.connect()
    con.execute(f"CREATE TABLE creators ({CREATORS_DDL})")
    sql = writers.CREATORS_MERGE_SQL.format(table=writers.table("creators"))
    new, old = "2026-09-30T00:30:00+00:00", "2026-09-28T00:30:00+00:00"
    duck_merge(con, sql, "creators", writers.CREATOR_TYPES, [creator("a", new, handle="now", followers=10)])
    duck_merge(con, sql, "creators", writers.CREATOR_TYPES, [
        creator("a", old, handle="then", followers=5, display_name="A", verified=True)])
    handle, name, followers, verified, _, first_seen, last_seen, _ = creators_table(con)[("tiktok", "a")]
    assert (handle, followers, first_seen, last_seen) == ("now", 10, at(new), at(new))
    assert (name, verified) == ("A", True)  # an older reading only fills what was unknown


def test_creators_merge_has_no_delete_and_never_sets_first_seen():
    sql = writers.CREATORS_MERGE_SQL
    assert "DELETE" not in sql.upper() and "WHEN NOT MATCHED THEN INSERT" in sql
    assert re.search(r"ON\s+T\.platform\s*=\s*S\.platform\s+AND\s+T\.creator_id\s*=\s*S\.creator_id", sql)
    update = sql[sql.index("UPDATE SET"):sql.index("WHEN NOT MATCHED")]
    assert sorted(re.findall(r"(\w+) = ", update)) == sorted(writers.CREATOR_UPDATES + ("last_seen",))
    assert "first_seen =" not in update


def test_dedupe_creators_keeps_the_earliest_first_seen_and_the_latest_fields():
    rows = [creator("a", "2026-09-28T02:00:00+00:00", handle="mid", followers=5),
            creator("a", "2026-09-28T01:00:00+00:00", handle="early", display_name="A"),
            creator("a", "2026-09-28T03:00:00+00:00", handle=None, followers=9),
            creator("b", "2026-09-28T01:00:00+00:00")]
    a, b = writers.dedupe_creators(rows)
    assert (a["first_seen"], a["last_seen"]) == ("2026-09-28T01:00:00+00:00", "2026-09-28T03:00:00+00:00")
    assert (a["handle"], a["display_name"], a["followers"]) == ("mid", "A", 9)
    assert b["creator_id"] == "b"


def test_merge_creators_sends_typed_batches():
    bq = FakeBQ()
    rows = [creator(str(i), FETCHED_ISO, verified=True, followers=i) for i in range(5)]
    assert writers.merge_creators(bq, rows, batch=2) == 3
    sql, params = bq.queries[0]
    assert "UNNEST(@rows)" in sql and params["rows"].array_type == "STRUCT"
    struct = params["rows"].values[0]
    assert list(struct.struct_types) == list(writers.CREATOR_TYPES)
    assert struct.struct_types["verified"] == "BOOL" and struct.struct_types["first_seen"] == "TIMESTAMP"
    assert len(bq.creators) == 5 and writers.merge_creators(bq, []) == 0


# The posts MERGE's geo fill, run on DuckDB

def post(pid, geo=(None, None, None), views=None):
    row = dict.fromkeys(writers.POST_TYPES)
    row.update(post_id=pid, platform="tiktok", post_date="2026-09-28", views=views, hashtags=[],
               geo_market=geo[0], geo_confidence=geo[1], geo_source=geo[2])
    return row


def posts_table(con):
    rows = con.execute("SELECT post_id, geo_market, geo_confidence, geo_source, views FROM posts ORDER BY post_id")
    return {r[0]: r[1:] for r in rows.fetchall()}


def posts_con():
    con = duckdb.connect()
    con.execute("CREATE TABLE posts (" + ", ".join(f"{c} {DUCK[t]}" for c, t in writers.POST_TYPES.items()) + ")")
    return con


def test_geo_is_written_only_at_the_known_threshold():
    assert writers.GEO_KNOWN == geo.KNOWN == 0.7
    assert f"S.geo_confidence >= {geo.KNOWN}" in writers.FILLS_GEO


def test_posts_merge_fills_geo_only_where_the_stored_market_is_null_and_the_new_one_is_known():
    con = posts_con()
    sql = writers.MERGE_SQL.format(table=writers.table("posts"))
    duck_merge(con, sql, "posts", writers.POST_TYPES, [
        post("known", ("ZA", 0.9, "ext_region"), views=10), post("empty", views=10), post("stays", views=10),
        post("guess", views=10)])
    duck_merge(con, sql, "posts", writers.POST_TYPES, [
        post("known", ("NG", 0.7, "place_mention"), views=11), post("empty", ("KE", 0.7, "place_mention")),
        post("stays", views=20), post("guess", ("KE", 0.3, "language"))])
    assert posts_table(con) == {"known": ("ZA", 0.9, "ext_region", 11), "empty": ("KE", 0.7, "place_mention", 10),
                                "stays": (None, None, None, 20), "guess": (None, None, None, 10)}


def test_a_language_guess_is_never_written_so_it_cannot_lock_a_post():
    [row] = writers.dedupe_posts([post("p", ("ZA", 0.3, "language"), views=1)])
    assert geo_of(row) == (None, None, None)
    bq = FakeBQ()
    writers.merge_posts(bq, [post("p", ("ZA", 0.3, "language"), views=1)])
    writers.merge_posts(bq, [post("p", ("ZA", 0.9, "ext_region"))])
    assert geo_of(bq.posts["p"]) == ("ZA", 0.9, "ext_region")


def test_geo_fill_merge_updates_existing_posts_and_never_inserts():
    con = posts_con()
    duck_merge(con, writers.MERGE_SQL.format(table=writers.table("posts")), "posts", writers.POST_TYPES,
               [post("empty", views=3), post("known", ("ZA", 0.9, "ext_region"))])
    sql = writers.GEO_FILL_SQL.format(table=writers.table("posts"))
    assert "INSERT" not in sql.upper() and "DELETE" not in sql.upper()
    duck_merge(con, sql, "posts", writers.GEO_TYPES, [
        {"post_id": "empty", "geo_market": "NG", "geo_confidence": 0.7, "geo_source": "place_mention"},
        {"post_id": "known", "geo_market": "KE", "geo_confidence": 0.8, "geo_source": "home_market"},
        {"post_id": "absent", "geo_market": "ZA", "geo_confidence": 0.9, "geo_source": "ext_region"}])
    assert posts_table(con) == {"empty": ("NG", 0.7, "place_mention", 3), "known": ("ZA", 0.9, "ext_region", None)}
    con2 = posts_con()
    duck_merge(con2, writers.MERGE_SQL.format(table=writers.table("posts")), "posts", writers.POST_TYPES,
               [post("empty", views=3)])
    duck_merge(con2, sql, "posts", writers.GEO_TYPES,
               [{"post_id": "empty", "geo_market": "ZA", "geo_confidence": 0.3, "geo_source": "language"}])
    assert posts_table(con2) == {"empty": (None, None, None, 3)}
    bq = FakeBQ()
    assert writers.fill_geo(bq, [post("a", ("ZA", 0.3, "language"))]) == 0 and not bq.queries


def test_dedupe_posts_takes_geo_from_a_later_sighting_when_the_first_had_none():
    first, later = post("p", views=1), post("p", ("ZA", 0.7, "place_mention"))
    [row] = writers.dedupe_posts([first, later])
    assert geo_of(row) == ("ZA", 0.7, "place_mention") and row["views"] == 1
    [kept] = writers.dedupe_posts([post("q", ("NG", 0.9, "ext_region")), post("q", ("ZA", 0.7, "place_mention"))])
    assert kept["geo_market"] == "NG"


# The collect run

def test_while_off_a_collect_run_writes_no_creators_and_everything_else(monkeypatch):
    monkeypatch.setattr(writers, "CREATORS_WRITE", False)
    run = collect(FakeClient())
    assert run.creators  # parsed, just not written
    bq = FakeBQ()
    written = writers.write_run(bq, run, "collect-1")
    assert bq.creators == {} and not [s for s, _ in bq.queries if writers.table("creators") in s]
    assert written["creators_written"] == 0 and written["creators_error"] is None
    assert bq.posts and bq.loaded("post_observations") and bq.loaded("item_counter_daily")


def test_collect_run_writes_a_creator_for_every_post_creator_by_default():
    run = collect(FakeClient())
    posted = {(p["platform"], p["creator_id"]) for p in run.posts if p["creator_id"] is not None}
    assert posted and {(c["platform"], c["creator_id"]) for c in run.creators} == posted
    assert run.counts()["creators"] == len(posted)
    bq = FakeBQ()
    written = writers.write_run(bq, run, "collect-1")
    assert set(bq.creators) == posted and written["creators_written"] == len(posted)
    merges = [sql for sql, _ in bq.queries if writers.table("creators") in sql]
    assert merges and all(sql.lstrip().startswith("MERGE") for sql in merges)


def test_a_failed_creators_merge_never_stops_the_appends():
    bq = FakeBQ()
    bq.fail_creators = True
    written = writers.write_run(bq, collect(FakeClient()), "collect-1")
    assert written["creators_written"] == 0 and "creators MERGE failed" in written["creators_error"]
    assert bq.posts and bq.loaded("post_observations") and bq.loaded("item_counter_daily") \
        and bq.loaded("collection_health") and written["cultural_map"] > 0


# The backfill

def raw_row(key, route, market, lane=None, fetched=FETCHED):
    return {"run_id": "collect-old", "market": market, "route": route, "lane": lane, "seed_key": None,
            "fetched_at": datetime.fromisoformat(fetched.replace("Z", "+00:00")), "body": json.dumps(LIVE[key])}


RAW = [raw_row("tiktok_trending", "tiktok/trending", "ZA", "sweep"),
       raw_row("youtube_trending", "youtube/videos/trending", "ZA", "sweep"),
       raw_row("search_top", "tiktok/search/top", "KE", "expansion"),
       raw_row("search_multi", "search/multi", "ZA", "confirm"),
       raw_row("search_multi", "search/multi", "ZA", "not_a_lane")]


ALL_CREATORS = {"kasi.keys", "pap.and.wors", "UCsyntheticPitch01", "nai.steps", "synth.kitchen", "synthdesk",
                "synth_redditor"}


def test_backfill_plan_reads_nothing_and_says_what_it_would_write(capsys):
    class NoBQ:
        def __getattr__(self, name):
            raise AssertionError("the plan touches no BigQuery")

    code = job.main(["--backfill-creators", "--since", "2026-09-28", "--until", "2026-09-29", "--plan"], env={},
                    bq=NoBQ(), make_client=lambda *a: pytest.fail("no SocialCrawl client"))
    out = capsys.readouterr().out
    assert code == 0
    assert "raw_responses" in out and "2026-09-28 to 2026-09-29" in out and "no network" in out
    assert "write: the creators MERGE" in out and f"{gdelt.MAX_BYTES:,}" in out
    for route in job.BACKFILL_ROUTES:
        assert route in out
    assert "tiktok/hashtags/popular" not in job.BACKFILL_ROUTES and "web/scrape" not in job.BACKFILL_ROUTES


def test_backfill_needs_since_and_until_defaults_to_today(capsys):
    assert job.main(["--backfill-creators", "--plan"], env={}) == 2
    assert "--since" in capsys.readouterr().err
    assert job.main(["--backfill-creators", "--since", "2026-09-28", "--plan"], env={}) == 0
    assert f"2026-09-28 to {datetime.now(timezone.utc).date().isoformat()}" in capsys.readouterr().out


def stored_posts(client):
    stored = parse.parse("tiktok/trending", {"region": "ZA", "feed": "local"}, "ZA", LIVE["tiktok_trending"],
                         FETCHED, "collect-old", item_id_fn=fake_item_id, geo_fn=lambda *a, **k: (None, None, None),
                         pull_seq=1)["posts"]
    for row in stored:  # posts stored before the fix, with no market
        client.posts[row["post_id"]] = dict(row)
    return stored


def backfill(client, capsys, *extra):
    code = job.main(["--backfill-creators", "--since", "2026-09-28", "--until", "2026-09-29", *extra], env={},
                    bq=client, make_client=lambda *a: pytest.fail("no SocialCrawl client"),
                    fns=(fake_item_id, geo_for_post))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_backfill_reads_a_bounded_capped_window(capsys):
    client = FakeBQ(raw=RAW)
    code, _ = backfill(client, capsys)
    assert code == 0
    [(dry_sql, _)] = client.dry_runs
    [(i, (sql, params))] = [(i, q) for i, q in enumerate(client.queries) if "raw_responses" in q[0]]
    assert dry_sql == sql and "BETWEEN @since AND @until" in sql
    assert (params["since"].value.isoformat(), params["until"].value.isoformat()) == ("2026-09-28", "2026-09-29")
    assert set(params["jobs"].values) == set(job.BACKFILL_JOBS)
    assert set(params["routes"].values) == set(job.BACKFILL_ROUTES)
    assert client.configs[i].maximum_bytes_billed == gdelt.MAX_BYTES


def test_backfill_refuses_a_read_over_the_cap():
    client = FakeBQ(raw=RAW)
    client.raw_bytes = gdelt.MAX_BYTES + 1
    with pytest.raises(gdelt.OverCap):
        job.main(["--backfill-creators", "--since", "2026-09-28"], env={}, bq=client,
                 fns=(fake_item_id, geo_for_post))
    assert client.dry_runs and not client.queries


def test_backfill_while_off_fills_null_geo_and_writes_no_creators(capsys, monkeypatch):
    monkeypatch.setattr(writers, "CREATORS_WRITE", False)
    client = FakeBQ(raw=RAW)
    stored = stored_posts(client)
    code, report = backfill(client, capsys)
    assert code == 0
    assert sorted(client.posts) == sorted(p["post_id"] for p in stored)  # no post inserted
    assert [geo_of(client.posts[p["post_id"]]) for p in stored] == [("ZA", 0.9, "ext_region")] * 2
    assert client.creators == {} and not [s for s, _ in client.queries if writers.table("creators") in s]
    assert report == {"raw_rows": 5, "skipped": 1, "creators": 7, "creators_written": 0, "geo_rows": 3,
                      "posts_seen": 7}


def test_backfill_writes_creators_by_default(capsys):
    client = FakeBQ(raw=RAW)
    stored = stored_posts(client)
    code, report = backfill(client, capsys)
    assert code == 0
    assert {cid for _, cid in client.creators} == ALL_CREATORS and report["creators_written"] == 7
    assert sorted(client.posts) == sorted(p["post_id"] for p in stored)
    assert not any(q[0].lstrip().startswith("MERGE") and "WHEN NOT MATCHED THEN INSERT (post_id" in q[0]
                   for q in client.queries)
