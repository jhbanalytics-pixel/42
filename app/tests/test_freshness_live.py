"""Freshness is a function of the clock, so it must never survive a cache.

The desk payload is cached until UTC midnight and the health freshness read for
three hours. Both used to cache the derived age and status alongside the stamp,
so a payload built at 01:17 still reported "3h" and green at 22:00. These tests
pin the two properties that were broken: the age advances as the clock advances,
and it crosses the amber threshold on time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.api import bq, desk, main


def _stamp(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


class TestRefreshFreshness:
    def test_recomputes_age_from_the_stamp(self):
        out = bq.refresh_freshness(
            {"stamp_utc": _stamp(6.5), "age_hours": 2.73, "status": "green"}
        )
        assert 6.4 < out["age_hours"] < 6.6
        assert out["status"] == "amber"

    def test_a_stale_cached_age_is_discarded_not_trusted(self):
        stale = {"stamp_utc": _stamp(9.0), "age_hours": 2.73, "status": "green"}
        assert bq.refresh_freshness(stale)["age_hours"] > 8.9

    def test_fresh_data_reads_green(self):
        assert bq.refresh_freshness({"stamp_utc": _stamp(0.5)})["status"] == "green"

    def test_threshold_is_three_hours(self):
        assert bq.refresh_freshness({"stamp_utc": _stamp(2.9)})["status"] == "green"
        assert bq.refresh_freshness({"stamp_utc": _stamp(3.1)})["status"] == "amber"

    def test_future_stamp_clamps_to_zero(self):
        ahead = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        assert bq.refresh_freshness({"stamp_utc": ahead})["age_hours"] == 0.0

    def test_missing_stamp_reports_amber_not_green(self):
        for value in ({}, {"stamp_utc": None}, None):
            out = bq.refresh_freshness(value)
            assert out["status"] == "amber"
            assert out["age_hours"] is None

    def test_naive_stamp_is_treated_as_utc(self):
        naive = (
            (datetime.now(UTC) - timedelta(hours=4)).replace(tzinfo=None).isoformat()
        )
        assert 3.9 < bq.refresh_freshness({"stamp_utc": naive})["age_hours"] < 4.1


class TestAgeLabel:
    def test_rounds_to_whole_hours(self):
        assert desk.age_label(2.73) == "3h"
        assert desk.age_label(6.69) == "7h"
        assert desk.age_label(0.2) == "0h"

    def test_unknown_age_has_no_chip(self):
        assert desk.age_label(None) == ""


class TestWithLiveFreshness:
    def test_rewrites_the_payload_freshness(self):
        payload = {
            "freshness": {
                "stamp_utc": _stamp(6.69),
                "age_hours": 2.73,
                "status": "green",
            },
            "topics": [{"id": "music_amapiano", "age": "3h"}],
        }
        out = main._with_live_freshness(payload)
        assert out["freshness"]["status"] == "amber"
        assert 6.5 < out["freshness"]["age_hours"] < 6.9

    def test_restamps_every_topic_age_chip(self):
        payload = {
            "freshness": {
                "stamp_utc": _stamp(7.0),
                "age_hours": 3.0,
                "status": "green",
            },
            "topics": [{"id": "a", "age": "3h"}, {"id": "b", "age": "3h"}],
        }
        out = main._with_live_freshness(payload)
        assert [t["age"] for t in out["topics"]] == ["7h", "7h"]

    def test_does_not_mutate_the_cached_payload(self):
        cached = {
            "freshness": {
                "stamp_utc": _stamp(8.0),
                "age_hours": 2.0,
                "status": "green",
            },
            "topics": [{"id": "a", "age": "2h"}],
        }
        main._with_live_freshness(cached)
        # The cache entry must survive untouched, or the next serve re-derives
        # from a value this call already rewrote.
        assert cached["freshness"]["age_hours"] == 2.0
        assert cached["topics"][0]["age"] == "2h"

    def test_topics_without_an_age_key_are_left_alone(self):
        payload = {"freshness": {"stamp_utc": _stamp(5.0)}, "topics": [{"id": "a"}]}
        assert main._with_live_freshness(payload)["topics"][0] == {"id": "a"}

    def test_passes_through_a_payload_with_no_freshness(self):
        assert main._with_live_freshness({"topics": []}) == {"topics": []}
        assert main._with_live_freshness(None) is None

    def test_empty_desk_payload_still_gets_live_freshness(self):
        payload = {
            "updated": None,
            "freshness": {
                "stamp_utc": _stamp(6.0),
                "age_hours": 1.0,
                "status": "green",
            },
            "topics": [],
        }
        assert main._with_live_freshness(payload)["freshness"]["status"] == "amber"
