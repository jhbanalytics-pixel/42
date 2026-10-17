"""A sparser later reading keeps the metrics it did not read, and vendor "unknown" times are not 1970."""
from datetime import datetime, timezone

import pytest

from core.collect import writers
from core.collect.parse import parse_time


def test_a_views_only_reading_keeps_the_stored_likes_comments_shares_and_engagement():
    first = {"post_id": "p1", "views": 100, "likes": 10, "comments": 2, "shares": 1, "engagement": 13}
    later = {"post_id": "p1", "views": 150, "likes": None, "comments": None, "shares": None, "engagement": None}
    [row] = writers.dedupe_posts([first, later])
    assert (row["views"], row["likes"], row["comments"], row["shares"], row["engagement"]) == (150, 10, 2, 1, 13)


def test_a_full_later_reading_still_replaces_every_metric():
    first = {"post_id": "p1", "views": 100, "likes": 10, "comments": 2, "shares": 1, "engagement": 13}
    later = {"post_id": "p1", "views": 150, "likes": 12, "comments": 3, "shares": 1, "engagement": 16}
    [row] = writers.dedupe_posts([first, later])
    assert (row["views"], row["likes"], row["engagement"]) == (150, 12, 16)


def test_the_stored_merge_keeps_a_metric_the_new_reading_lacks():
    for metric in writers.METRICS:
        assert f"{metric} = IF({writers.HAS_METRIC}, IFNULL(S.{metric}, T.{metric}), T.{metric})" in writers.MERGE_SQL


@pytest.mark.parametrize("raw", [0, "0", 0.0, 12345])
def test_an_epoch_before_2001_is_unknown(raw):
    assert parse_time(raw) is None


def test_a_compact_date_is_that_day_not_1970():
    assert parse_time("20261015") == datetime(2026, 10, 15, tzinfo=timezone.utc)


@pytest.mark.parametrize("raw", [1760600000, "1760600000", 1760600000000])
def test_real_epochs_still_parse(raw):
    assert parse_time(raw) == datetime(2025, 10, 16, 7, 33, 20, tzinfo=timezone.utc)
