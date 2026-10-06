"""Unit tests for the lead-time evaluation harness.

Pure-function tests only: no live BigQuery calls. Covers the watchlist
loader schema validation, the SURFACED/INGESTED_NOT_RANKED/NEVER_INGESTED
classification, lead-day arithmetic including the future-event case, and the
match-term SQL builder's parameterization (no raw-string injection).
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
import scripts.leadtime_eval as leadtime_eval
import yaml

VALID_ENTRY = {
    "id": "za-test-entry",
    "market": "za",
    "title": "Test move",
    "event_date": "2026-06-25",
    "match_terms": ["Test Term"],
    "added": "2026-07-03",
    "source": "unit test",
}


def _write_watchlist(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "watchlist.yaml"
    path.write_text(yaml.safe_dump({"entries": entries}), encoding="utf-8")
    return path


class TestLoadWatchlist:
    def test_loads_valid_entry(self, tmp_path):
        path = _write_watchlist(tmp_path, [VALID_ENTRY])
        entries = leadtime_eval.load_watchlist(path)
        assert len(entries) == 1
        assert entries[0]["id"] == "za-test-entry"
        assert entries[0]["event_date"] == datetime.date(2026, 6, 25)
        # match_terms lowercased.
        assert entries[0]["match_terms"] == ["test term"]

    def test_rejects_missing_event_date(self, tmp_path):
        bad = {k: v for k, v in VALID_ENTRY.items() if k != "event_date"}
        path = _write_watchlist(tmp_path, [bad])
        with pytest.raises(leadtime_eval.WatchlistError, match="event_date"):
            leadtime_eval.load_watchlist(path)

    def test_rejects_missing_match_terms(self, tmp_path):
        bad = dict(VALID_ENTRY)
        bad["match_terms"] = []
        path = _write_watchlist(tmp_path, [bad])
        with pytest.raises(leadtime_eval.WatchlistError, match="match_terms"):
            leadtime_eval.load_watchlist(path)

    def test_rejects_empty_entries(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text(yaml.safe_dump({"entries": []}), encoding="utf-8")
        with pytest.raises(leadtime_eval.WatchlistError, match="no entries"):
            leadtime_eval.load_watchlist(path)

    def test_accepts_real_date_object(self, tmp_path):
        entry = dict(VALID_ENTRY)
        entry["event_date"] = datetime.date(2026, 7, 7)
        path = _write_watchlist(tmp_path, [entry])
        entries = leadtime_eval.load_watchlist(path)
        assert entries[0]["event_date"] == datetime.date(2026, 7, 7)


class TestClassify:
    def test_surfaced_when_briefed(self):
        assert (
            leadtime_eval.classify(datetime.date(2026, 6, 24), datetime.date(2026, 6, 25))
            == leadtime_eval.SURFACED
        )

    def test_surfaced_takes_priority_even_if_ingested_missing(self):
        # A briefed-but-not-directly-ingested edge case still counts as surfaced.
        assert leadtime_eval.classify(None, datetime.date(2026, 6, 25)) == leadtime_eval.SURFACED

    def test_ingested_not_ranked_when_no_brief(self):
        assert (
            leadtime_eval.classify(datetime.date(2026, 6, 24), None)
            == leadtime_eval.INGESTED_NOT_RANKED
        )

    def test_never_ingested_when_both_missing(self):
        assert leadtime_eval.classify(None, None) == leadtime_eval.NEVER_INGESTED


class TestLeadDays:
    def test_positive_when_observed_before_event(self):
        event = datetime.date(2026, 6, 25)
        observed = datetime.date(2026, 6, 20)
        assert leadtime_eval.lead_days(event, observed) == 5

    def test_negative_when_observed_after_event(self):
        event = datetime.date(2026, 6, 25)
        observed = datetime.date(2026, 6, 28)
        assert leadtime_eval.lead_days(event, observed) == -3

    def test_none_when_not_observed(self):
        assert leadtime_eval.lead_days(datetime.date(2026, 6, 25), None) is None

    def test_future_event_allows_negative_lead(self):
        # Saba Saba case: event_date is in the future, buildup coverage
        # observed before the event date yields a positive lead; coverage
        # observed after (impossible until the event happens) is not
        # expected but the arithmetic itself must not raise.
        event = datetime.date(2026, 7, 7)
        observed = datetime.date(2026, 7, 1)
        assert leadtime_eval.lead_days(event, observed) == 6


class TestMatchConditionBuilder:
    def test_builds_parameterized_condition(self):
        condition, params = leadtime_eval._build_match_condition(
            ["maandamano", "june 25"], "haystack_expr", "t"
        )
        assert "@t_0" in condition
        assert "@t_1" in condition
        assert params == {"t_0": "maandamano", "t_1": "june 25"}
        # Raw term values never appear inlined in the SQL fragment itself.
        assert "maandamano" not in condition
        assert "june 25" not in condition

    def test_rejects_empty_terms(self):
        with pytest.raises(ValueError):
            leadtime_eval._build_match_condition([], "haystack_expr", "t")

    def test_terms_with_quotes_stay_out_of_sql(self):
        # A term containing a quote or semicolon must still only ever reach
        # the SQL as a bound parameter value, never spliced into the string.
        condition, params = leadtime_eval._build_match_condition(
            ["'; DROP TABLE x; --"], "haystack_expr", "t"
        )
        assert "DROP TABLE" not in condition
        assert params["t_0"] == "'; DROP TABLE x; --"


class TestBuildSummary:
    def test_summary_counts_and_median(self):
        results = [
            {"class": leadtime_eval.SURFACED, "brief_lead_days": 5},
            {"class": leadtime_eval.SURFACED, "brief_lead_days": 3},
            {"class": leadtime_eval.INGESTED_NOT_RANKED, "brief_lead_days": None},
            {"class": leadtime_eval.NEVER_INGESTED, "brief_lead_days": None},
        ]
        summary = leadtime_eval.build_summary(results)
        assert summary["entries_evaluated"] == 4
        assert summary["surfaced_count"] == 2
        assert summary["surfaced_rate"] == 0.5
        assert summary["ingested_not_ranked_count"] == 1
        assert summary["never_ingested_count"] == 1
        assert summary["median_brief_lead_days"] == 4

    def test_summary_handles_no_surfaced_entries(self):
        results = [
            {"class": leadtime_eval.NEVER_INGESTED, "brief_lead_days": None},
        ]
        summary = leadtime_eval.build_summary(results)
        assert summary["median_brief_lead_days"] is None
        assert summary["surfaced_rate"] == 0.0
