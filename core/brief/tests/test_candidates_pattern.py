"""The pattern bucket of the candidate statement (core/brief/sql/brief.sql, held-topics root cause, 16 Oct 2026), run
on the DuckDB harness. Since 04bc5cc the critic passes only an evidenced adopted format, practice or behaviour, so the
pool orders candidates whose own posts show a sound or an enriched format used by 3 or more creators on 2 or more days
before plain rows, and plain rows before ones the code sees as one creator, a calendar moment or mostly news. It only
orders: every row stays in the pool, and worth_raw still orders rows within a bucket."""

from datetime import datetime, timedelta, timezone

import pytest

from core.brief import job
from core.brief.tests.test_candidates_locality import D, RUN, STUBS, T, put, state, stubbed
from core.detect.tests import duck

UTC = timezone.utc


def posts(con, item, rows):
    """rows: (post_id, creator_id, day offset from D, sound_id, formats or None for no enrichment row)."""
    for post_id, creator, back, sound, formats in rows:
        day = D - timedelta(days=back)
        duck.load(con, "core.posts", [{"post_id": post_id, "platform": "tiktok", "creator_id": creator,
                                       "sound_id": sound, "published_at": datetime(day.year, day.month, day.day, 9,
                                                                                   tzinfo=UTC),
                                       "post_date": day}])
        duck.load(con, "core.post_items", [{"post_id": post_id, "item_id": item, "via": "hashtag", "linked_on": day}])
        duck.load(con, "core.post_observations", [{"post_id": post_id, "observed_at": T, "observed_date": day,
                                                   "market": "NG", "platform": "tiktok", "lane": "seed",
                                                   "lane_class": "seeded"}])
        if formats is not None:
            duck.load(con, "core.post_enrichment", [{"post_id": post_id, "formats": formats}])


def item(con, name, worth, kind="hashtag", **over):
    state(con, name, True, worth)
    con.execute("UPDATE core.item_state SET kind = ? WHERE item_id = ?", [kind, name])
    for column, value in over.items():
        con.execute(f"UPDATE core.item_state SET {column} = ? WHERE item_id = ?", [value, name])
    put(con, name, 8, 6, "local")


@pytest.fixture
def con():
    c = duck.connect()
    c.execute(STUBS)
    duck.load(c, "agent.runs", [{"run_id": RUN, "stage": "detect", "run_date": D, "status": "ok", "started_at": T,
                                 "finished_at": T + timedelta(minutes=20)}])
    item(c, "spike", .90)                                   # a plain spike with the highest worth
    item(c, "plain", .50)
    item(c, "sound", .10)                                   # one sound, three creators, two days
    posts(c, "sound", [("s1", "a", 0, "snd1", None), ("s2", "b", 0, "snd1", None), ("s3", "c", 1, "snd1", None)])
    item(c, "dance", .05)                                   # one enriched adoption format, three creators, two days
    posts(c, "dance", [("f1", "a", 0, None, ["dance"]), ("f2", "b", 1, None, ["dance"]),
                       ("f3", "c", 2, None, ["dance", "talking_head"])])
    item(c, "oneday", .40)                                  # one sound, three creators, one day: not lifted
    posts(c, "oneday", [("o1", "a", 0, "snd2", None), ("o2", "b", 0, "snd2", None), ("o3", "c", 0, "snd2", None)])
    item(c, "talk", .45)                                    # talking heads on two days: not an adoption format
    posts(c, "talk", [("t1", "a", 0, None, ["talking_head"]), ("t2", "b", 1, None, ["talking_head"]),
                      ("t3", "c", 1, None, ["other"])])
    item(c, "person", .95, kind="creator")                  # a creator item is one creator by definition
    item(c, "solo", .93, top_creator_share3=.7)             # one creator carries 70% of its 3 day posts
    item(c, "promo", .925, sponsored_share=.6)              # most of its posts carry a paid label
    item(c, "fixture", .92, moment="Champions League")      # sits on a calendar moment
    item(c, "news", .91)                                    # most of its market posts are news posts
    duck.load(c, "core.ms_stub", [{"metric_date": D, "item_id": "news", "market": "NG", "market_scope": "market",
                                   "total_posts7": 10, "market_posts7": 10, "market_news_posts7": 6}])
    item(c, "clips", .89)                                   # most of its enriched posts are news clips
    posts(c, "clips", [("n1", "a", 0, None, ["news_clip"]), ("n2", "b", 1, None, ["news_clip"]),
                       ("n3", "c", 1, None, ["skit"]), ("n4", "d", 2, None, ["news_clip"])])
    return c


def order(con, market="NG"):
    rows = duck.query(con, stubbed(job.QUERIES["candidates"]), {"d": D, "market": market})
    return [r["item_id"] for r in rows], rows


def test_adopted_sound_and_format_rank_first_then_plain_rows_then_single_creator_moment_and_news(con):
    ids, _ = order(con)
    assert ids == ["sound", "dance", "spike", "plain", "talk", "oneday", "person", "solo", "promo", "fixture", "news", "clips"]


def test_the_bucket_inputs_come_back_for_the_receipt_and_are_never_handed_on(con):
    _, rows = order(con)
    by = {r["item_id"]: r for r in rows}
    assert (by["sound"]["_selection_sound_creators"], by["sound"]["_selection_sound_days"]) == (3, 2)
    assert (by["dance"]["_selection_format_creators"], by["dance"]["_selection_format_days"]) == (3, 3)
    assert (by["clips"]["_selection_enriched_posts"], by["clips"]["_selection_news_clip_posts"]) == (4, 3)
    assert {r["item_id"]: r["_selection_pattern"] for r in rows} == {
        "sound": 0, "dance": 0, "spike": 1, "plain": 1, "talk": 1, "oneday": 1,
        "person": 2, "solo": 2, "promo": 2, "fixture": 2, "news": 2, "clips": 2}


def test_nothing_is_dropped_and_another_markets_posts_do_not_count(con):
    ids, _ = order(con)
    assert len(ids) == 12
    con.execute("UPDATE core.post_observations SET market = 'ZA' WHERE post_id IN ('s1', 's2', 's3')")
    ids, rows = order(con)
    assert len(ids) == 12 and {r["item_id"]: r["_selection_pattern"] for r in rows}["sound"] == 1


def test_a_generic_sound_riding_on_unrelated_items_does_not_lift_any_of_them(con):
    # snd1 is also on four posts of three other items: the sound item's own posts are under half of its posts.
    for n, other in enumerate(("x1", "x2", "x3", "x3")):
        if n < 3:
            item(con, other, .01)
        posts(con, other, [(f"g{n}", f"z{n}", n % 2, "snd1", None)])
    _, rows = order(con)
    assert {r["item_id"]: r["_selection_pattern"] for r in rows}["sound"] == 1


def test_any_sound_that_qualifies_lifts_the_item_not_only_the_one_with_most_creators(con):
    # snd3 has four creators on one day, snd4 three creators on two days: snd4 qualifies.
    item(con, "two", .02)
    posts(con, "two", [("w1", "a", 0, "snd3", None), ("w2", "b", 0, "snd3", None), ("w3", "c", 0, "snd3", None),
                       ("w4", "d", 0, "snd3", None), ("w5", "e", 0, "snd4", None), ("w6", "f", 1, "snd4", None),
                       ("w7", "g", 1, "snd4", None)])
    _, rows = order(con)
    by = {r["item_id"]: r for r in rows}
    assert by["two"]["_selection_pattern"] == 0 and by["two"]["_selection_sound_creators"] == 4


def test_a_post_published_before_the_window_does_not_count_as_a_day_of_adoption(con):
    con.execute("UPDATE core.posts SET post_date = DATE '2026-09-01' WHERE post_id = 's3'")
    _, rows = order(con)
    assert {r["item_id"]: r["_selection_pattern"] for r in rows}["sound"] == 1


def test_the_statement_without_the_news_column_still_runs_and_orders_the_same_buckets(con):
    sql = job.QUERIES["candidates_without_news_column"]
    rows = duck.query(con, stubbed(sql, ordered=sql[sql.index("ordered AS ("):]), {"d": D, "market": "NG"})
    pattern = {r["item_id"]: r["_selection_pattern"] for r in rows}
    assert len(rows) == 12 and pattern["news"] == 1 and pattern["sound"] == 0 and pattern["clips"] == 2


def test_a_suppressed_creator_does_not_count_towards_adoption(con):
    duck.load(con, "core.suppressed_fixture", [{"creator_id": "c"}])
    _, rows = order(con)
    by = {r["item_id"]: r for r in rows}
    assert by["sound"]["_selection_pattern"] == 1 and by["dance"]["_selection_pattern"] == 1


def test_the_bucket_sits_below_locality_and_breadth(con):
    # A plain row in the first locality bucket still ranks before an adopted sound whose locality is unreadable.
    con.execute("DELETE FROM core.item_locality WHERE item_id = 'sound'")
    con.execute("DELETE FROM core.item_locality_verified WHERE item_id = 'sound'")
    ids, _ = order(con)
    assert ids.index("sound") > ids.index("clips")
