"""W8-DEC-12 for locality_v2 (C4 v3 ruling finding F3, condition C3): a news public-feed post never counts toward the
population, the feed quantity or the breadth, alone or beside measured posts.

Public-feed news rows carry lane_class 'context' and no geo (core/collect/public_feed_rows.py), so under the measured
lanes rule they never enter the population. The case is pinned here because the fixtures of test_locality_v2 had no
'context' row."""

from core.detect.tests.test_locality_v2 import add, compute, con, key  # noqa: F401 (con is a fixture)


def test_news_feed_posts_alone_leave_the_item_with_an_empty_population(con):  # noqa: F811
    key(con, "n01")
    for _ in range(4):
        add(con, "n01", platform="news", lane_class="context", lane="public_feed", route="public_feed/news",
            source_market="NG")
    row = compute(con)["n01"]
    assert (row["population_posts"], row["feed_only_posts"], row["breadth_creators"], row["status"]) == (
        0, 0, 0, "market_unconfirmed")


def test_news_feed_posts_add_nothing_beside_measured_posts(con):  # noqa: F811
    key(con, "n02")
    add(con, "n02", creator="real", geo="NG", conf=0.9, src="ext_region", source_market="NG")
    add(con, "n02", creator="real2", source_market="NG")
    for _ in range(5):
        add(con, "n02", platform="news", lane_class="context", lane="public_feed", route="public_feed/news",
            source_market="NG")
    row = compute(con)["n02"]
    assert (row["population_posts"], row["known_posts"], row["local_posts"], row["feed_only_posts"],
            row["feed_only_creators"], row["breadth_creators"]) == (2, 1, 1, 1, 1, 2)
