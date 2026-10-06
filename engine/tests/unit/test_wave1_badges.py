"""Unit tests for the Wave 1 continuity + lifecycle producer bridge.

``src/analysis/wave1_badges.py`` reads the continuity_state / continuity_day /
lifecycle_phase columns that compute_trend_scores stores on trend_scores and
tags them onto the brief dicts, so the PULSE card render lights the badges.
Tests pin the BQ read shape (matched / NULL-row / allow-list), the tagger
key-shape contract, and the non-fatal failure path.
"""

import datetime
from unittest import mock

from src.analysis.wave1_badges import (
    fetch_continuity_lifecycle,
    fetch_seed_scores,
    tag_briefs_with_continuity_lifecycle,
    tag_briefs_with_seed_score,
)


def _row(market, query_group, state, day, phase):
    r = mock.Mock()
    r.market = market
    r.query_group = query_group
    r.continuity_state = state
    r.continuity_day = day
    r.lifecycle_phase = phase
    return r


def _fake_client(rows):
    fake_job = mock.Mock()
    fake_job.result.return_value = rows
    fake_client = mock.Mock()
    fake_client.project = "p"
    fake_client.query.return_value = fake_job
    return fake_client


# --- fetch_continuity_lifecycle -----------------------------------------


def test_fetch_non_fatal_on_bq_error():
    """A client/BQ failure yields {} (never raises), so the cron is never blocked."""
    with mock.patch("src.analysis.wave1_badges.get_client", side_effect=RuntimeError("bq down")):
        out = fetch_continuity_lifecycle(datetime.date(2026, 6, 21))
    assert out == {}


def test_fetch_happy_path_keys_and_values():
    rows = [
        _row("za", "music_amapiano", "day3plus", 5, "growth"),
        _row("ng", "music_afrobeats", "new", 1, "birth"),
        _row("ke", "genz_sheng", "rebounding", 1, "maturity"),
    ]
    client = _fake_client(rows)
    with (
        mock.patch("src.analysis.wave1_badges.get_client", return_value=client),
        mock.patch("src.analysis.wave1_badges.get_dataset", return_value="ds"),
    ):
        out = fetch_continuity_lifecycle(datetime.date(2026, 6, 21))
    assert out[("za", "music_amapiano")] == {
        "continuity_state": "day3plus",
        "continuity_day": 5,
        "lifecycle_phase": "growth",
    }
    assert out[("ng", "music_afrobeats")]["continuity_state"] == "new"
    assert out[("ke", "genz_sheng")]["lifecycle_phase"] == "maturity"


def test_fetch_drops_unknown_values_to_empty():
    """Out-of-contract state/phase are blanked so a producer rename cannot ship
    an unknown badge to stakeholders."""
    rows = [_row("za", "x", "bogus", 2, "zombie")]
    client = _fake_client(rows)
    with (
        mock.patch("src.analysis.wave1_badges.get_client", return_value=client),
        mock.patch("src.analysis.wave1_badges.get_dataset", return_value="ds"),
    ):
        out = fetch_continuity_lifecycle(datetime.date(2026, 6, 21))
    assert out[("za", "x")]["continuity_state"] == ""
    assert out[("za", "x")]["lifecycle_phase"] == ""
    assert out[("za", "x")]["continuity_day"] == 2


def test_fetch_handles_null_continuity_day():
    rows = [_row("za", "x", "new", None, "birth")]
    client = _fake_client(rows)
    with (
        mock.patch("src.analysis.wave1_badges.get_client", return_value=client),
        mock.patch("src.analysis.wave1_badges.get_dataset", return_value="ds"),
    ):
        out = fetch_continuity_lifecycle(datetime.date(2026, 6, 21))
    assert out[("za", "x")]["continuity_day"] is None


# --- tag_briefs_with_continuity_lifecycle -------------------------------


def test_tag_matches_and_misses():
    """A matched key is tagged on all present fields; a non-matched brief is
    left untouched, so key-shape drift degrades to no-badge."""
    briefs = {("za", "music_amapiano"): {}, ("ng", "music_afrobeats"): {}}
    lookup = {
        ("za", "music_amapiano"): {
            "continuity_state": "day3plus",
            "continuity_day": 5,
            "lifecycle_phase": "growth",
        },
        ("ke", "genz_sheng"): {  # no matching brief
            "continuity_state": "new",
            "continuity_day": 1,
            "lifecycle_phase": "birth",
        },
    }
    tagged = tag_briefs_with_continuity_lifecycle(briefs, lookup)
    assert tagged == 1
    za = briefs[("za", "music_amapiano")]
    assert za["continuity_state"] == "day3plus"
    assert za["continuity_day"] == 5
    assert za["lifecycle_phase"] == "growth"
    assert briefs[("ng", "music_afrobeats")] == {}


def test_tag_partial_fields_only_sets_present():
    """Only continuity present -> lifecycle_phase is not stamped, and vice versa."""
    briefs = {("za", "a"): {}, ("za", "b"): {}}
    lookup = {
        ("za", "a"): {"continuity_state": "day2", "continuity_day": 2, "lifecycle_phase": ""},
        ("za", "b"): {"continuity_state": "", "continuity_day": None, "lifecycle_phase": "decline"},
    }
    tagged = tag_briefs_with_continuity_lifecycle(briefs, lookup)
    assert tagged == 2
    assert briefs[("za", "a")]["continuity_state"] == "day2"
    assert "lifecycle_phase" not in briefs[("za", "a")]
    assert briefs[("za", "b")]["lifecycle_phase"] == "decline"
    assert "continuity_state" not in briefs[("za", "b")]


def test_tag_empty_entry_leaves_brief_untouched():
    briefs = {("za", "a"): {}}
    lookup = {("za", "a"): {"continuity_state": "", "continuity_day": None, "lifecycle_phase": ""}}
    tagged = tag_briefs_with_continuity_lifecycle(briefs, lookup)
    assert tagged == 0
    assert briefs[("za", "a")] == {}


def test_tag_skips_continuity_day_when_not_int():
    """A non-int continuity_day is not stamped (day3plus label falls back)."""
    briefs = {("za", "a"): {}}
    lookup = {
        ("za", "a"): {"continuity_state": "day3plus", "continuity_day": None, "lifecycle_phase": ""}
    }
    tag_briefs_with_continuity_lifecycle(briefs, lookup)
    assert briefs[("za", "a")]["continuity_state"] == "day3plus"
    assert "continuity_day" not in briefs[("za", "a")]


# --- fetch_seed_scores --------------------------------------------------


def _seed_row(market, query_group, seed_score):
    r = mock.Mock()
    r.market = market
    r.query_group = query_group
    r.seed_score = seed_score
    return r


def test_fetch_seed_scores_non_fatal_on_bq_error():
    """A client/BQ failure yields {} (never raises) so the cron is not blocked."""
    with mock.patch("src.analysis.wave1_badges.get_client", side_effect=RuntimeError("bq down")):
        out = fetch_seed_scores(datetime.date(2026, 6, 22))
    assert out == {}


def test_fetch_seed_scores_happy_path_and_range_filter():
    rows = [
        _seed_row("za", "music_amapiano", 0.82),
        _seed_row("ng", "fashion_asoebi", 0.41),
        _seed_row("ke", "bad_row", 1.7),  # out of range, dropped
    ]
    client = _fake_client(rows)
    with (
        mock.patch("src.analysis.wave1_badges.get_client", return_value=client),
        mock.patch("src.analysis.wave1_badges.get_dataset", return_value="ds"),
    ):
        out = fetch_seed_scores(datetime.date(2026, 6, 22))
    assert out[("za", "music_amapiano")] == 0.82
    assert out[("ng", "fashion_asoebi")] == 0.41
    assert ("ke", "bad_row") not in out


# --- tag_briefs_with_seed_score -----------------------------------------


def test_tag_seed_matches_and_misses():
    briefs = {("za", "music_amapiano"): {}, ("ng", "music_afrobeats"): {}}
    lookup = {("za", "music_amapiano"): 0.77, ("ke", "genz_sheng"): 0.9}
    tagged = tag_briefs_with_seed_score(briefs, lookup)
    assert tagged == 1
    assert briefs[("za", "music_amapiano")]["seed_score"] == 0.77
    assert "seed_score" not in briefs[("ng", "music_afrobeats")]


def test_tag_seed_ignores_out_of_range():
    briefs = {("za", "a"): {}}
    tag_briefs_with_seed_score(briefs, {("za", "a"): 2.5})
    assert "seed_score" not in briefs[("za", "a")]
