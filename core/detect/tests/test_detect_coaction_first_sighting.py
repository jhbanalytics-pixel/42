"""coaction.sql dates a post by its true first sighting, as tvf_item_window does. Before 16 October 2026 it took the
earliest sighting inside the 30-day window, so a watchlist re-read of a 40-day-old post entered the 7-day set as new."""
from .. import coaction
from . import duck
from .fixtures import D, cmap, day, obs, post


def load(con, observations):
    duck.load(con, "core.post_observations", observations)
    duck.load(con, "core.posts", [post("p", "c1", day(40))])
    duck.load(con, "core.post_items", [{"post_id": "p", "item_id": "it", "via": "hashtag"}])
    duck.load(con, "core.cultural_map", [cmap("it")])


def test_a_reread_of_an_old_post_is_not_a_new_sighting():
    con = duck.connect()
    load(con, [obs("p", day(40), "unbiased_rank", "sweep"), obs("p", day(1), "unbiased_counter", "watchlist")])
    assert duck.query(con, coaction.posts_sql(), {"d": D}) == []


def test_a_post_first_sighted_in_the_window_keeps_its_first_day():
    con = duck.connect()
    load(con, [obs("p", day(5), "unbiased_rank", "sweep"), obs("p", day(1), "unbiased_counter", "watchlist")])
    [row] = duck.query(con, coaction.posts_sql(), {"d": D})
    assert row["first_day"] == day(5)


def test_a_sighting_with_no_lane_is_read():
    con = duck.connect()
    load(con, [obs("p", day(2), "unbiased_rank", None)])
    [row] = duck.query(con, coaction.posts_sql(), {"d": D})
    assert row["first_day"] == day(2)
