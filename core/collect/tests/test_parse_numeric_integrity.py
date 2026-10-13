import json

import pytest

from core.collect.tests.test_parse import fixture, run


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_post_metrics_are_unknown_and_other_posts_survive(value):
    body = fixture("rank", "tiktok_trending")
    body["data"]["items"][0]["post"]["engagement"]["views"] = value
    body["data"]["items"][0]["post"]["media"]["duration"] = value
    rows = run("tiktok/trending", {"region": "ZA", "feed": "local"}, "ZA", body, pull_seq=1)
    assert len(rows["posts"]) == 3
    assert rows["posts"][0]["views"] is None
    assert rows["observations"][0]["views"] is None
    assert rows["posts"][0]["duration_s"] is None
    assert rows["posts"][1]["views"] == 50000
    json.dumps(rows, allow_nan=False)


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_daily_curve_values_do_not_enter_persistence_rows(value):
    body = fixture("boards", "hashtags_popular")
    body["data"]["items"][0]["trend"][0]["value"] = value
    rows = run("tiktok/hashtags/popular", {"countryCode": "ZA"}, "ZA", body, pull_seq=1)
    assert rows["counters"]
    assert len([row for row in rows["counters"] if row["unit"] == "delta"]) == 4
    json.dumps(rows, allow_nan=False)


def test_finite_duration_and_zero_metrics_remain_readable():
    body = fixture("rank", "tiktok_trending")
    body["data"]["items"][0]["post"]["engagement"]["views"] = 0
    rows = run("tiktok/trending", {"region": "ZA", "feed": "local"}, "ZA", body, pull_seq=1)
    assert rows["posts"][0]["views"] == 0
    assert rows["posts"][0]["duration_s"] == 15.5
    json.dumps(rows, allow_nan=False)
