"""Unit tests for the standalone pan-African story detection stage.

Pure-logic tests on the detector plus the env-flag short-circuit. No
BigQuery: the IO path (run_pan_african_stage) is exercised flag-off only,
proving it is a no-op that never touches the client.
"""

import datetime
from unittest import mock

from src.analysis.pan_african import (
    detect_pan_african_stories,
    family_label,
    normalise_family,
    run_pan_african_stage,
)

DATE = datetime.date(2026, 6, 19)


def _row(market, query_group, velocity, item_count=10):
    return {
        "market": market,
        "query_group": query_group,
        "velocity_score": velocity,
        "item_count": item_count,
    }


def test_normalise_family_collapses_music_subtopics():
    # The contract's worked example: three market-specific music groups
    # must all collapse to one shared family.
    assert normalise_family("music_amapiano") == "music"
    assert normalise_family("music_afrobeats") == "music"
    assert normalise_family("music_gengetone") == "music"


def test_normalise_family_handles_multi_underscore_and_edges():
    assert normalise_family("economy_sapa_hustle") == "economy"
    assert normalise_family("tech_gemini_ai") == "tech"
    assert normalise_family("nounderscore") == "nounderscore"
    assert normalise_family("") == "other"
    assert normalise_family(None) == "other"


def test_family_label_known_and_fallback():
    assert family_label("music") == "Music"
    # Unknown family falls back to a title-cased name, no crash.
    assert family_label("newthing") == "Newthing"


def test_two_market_family_becomes_a_story():
    rows = [
        _row("za", "music_amapiano", 0.40, item_count=30),
        _row("ng", "music_afrobeats", 0.25, item_count=20),
    ]
    stories = detect_pan_african_stories(rows, DATE)
    assert len(stories) == 1
    story = stories[0]
    assert story.family == "music"
    assert sorted(story.markets) == ["ng", "za"]
    assert sorted(story.topic_keys) == ["music_afrobeats", "music_amapiano"]
    assert story.total_item_count == 50
    # momentum_composite sums each market's strongest rising velocity.
    assert story.momentum_composite == 0.65
    assert story.story_id == "2026-06-19_music"
    assert story.story_label == "Music"


def test_three_market_family_counts_all_markets():
    rows = [
        _row("za", "sports_rugby", 0.20),
        _row("ng", "sports_football", 0.30),
        _row("ke", "sports_football", 0.10),
    ]
    stories = detect_pan_african_stories(rows, DATE)
    assert len(stories) == 1
    assert sorted(stories[0].markets) == ["ke", "ng", "za"]
    assert stories[0].momentum_composite == 0.60


def test_single_market_family_is_excluded():
    # A family rising in only one market is not pan-African; nothing emitted.
    rows = [
        _row("za", "infra_power_eskom", 0.90),
        _row("za", "education_matric_nsfas", 0.50),
    ]
    stories = detect_pan_african_stories(rows, DATE)
    assert stories == []


def test_same_family_same_market_twice_still_single_market():
    # Two query_groups of the same family in ONE market do not make a story;
    # market breadth, not topic count, is the gate.
    rows = [
        _row("za", "music_amapiano", 0.40),
        _row("za", "music_amapiano", 0.30),
    ]
    stories = detect_pan_african_stories(rows, DATE)
    assert stories == []


def test_below_velocity_floor_does_not_count_as_rising():
    # ZA is rising, NG sits under the floor -> only one rising market -> excluded.
    rows = [
        _row("za", "food_rituals_braai", 0.40),
        _row("ng", "food_jollof", 0.01),
    ]
    stories = detect_pan_african_stories(rows, DATE, rising_velocity_min=0.05)
    assert stories == []


def test_velocity_floor_boundary_is_inclusive():
    rows = [
        _row("za", "food_rituals_braai", 0.05),
        _row("ng", "food_jollof", 0.05),
    ]
    stories = detect_pan_african_stories(rows, DATE, rising_velocity_min=0.05)
    assert len(stories) == 1
    assert stories[0].family == "food"


def test_empty_day_returns_no_stories():
    assert detect_pan_african_stories([], DATE) == []


def test_rows_missing_fields_are_skipped_safely():
    rows = [
        {"market": "za", "query_group": "music_amapiano", "velocity_score": 0.40},
        {"market": "", "query_group": "music_afrobeats", "velocity_score": 0.40},
        {"market": "ng", "query_group": "", "velocity_score": 0.40},
        {"query_group": "music_gengetone", "velocity_score": 0.40},
    ]
    # Only ZA has a usable rising row, so no cross-market story.
    assert detect_pan_african_stories(rows, DATE) == []


def test_stories_sorted_by_momentum_descending():
    rows = [
        # food: za 0.10 + ng 0.10 = 0.20
        _row("za", "food_rituals_braai", 0.10),
        _row("ng", "food_jollof", 0.10),
        # music: za 0.40 + ng 0.40 = 0.80 (should rank first)
        _row("za", "music_amapiano", 0.40),
        _row("ng", "music_afrobeats", 0.40),
    ]
    stories = detect_pan_african_stories(rows, DATE)
    assert [s.family for s in stories] == ["music", "food"]


def test_to_row_matches_contract_schema():
    rows = [
        _row("za", "music_amapiano", 0.40, item_count=30),
        _row("ng", "music_afrobeats", 0.25, item_count=20),
    ]
    row = detect_pan_african_stories(rows, DATE)[0].to_row()
    assert set(row.keys()) == {
        "trend_date",
        "story_id",
        "story_label",
        "markets",
        "topic_keys",
        "total_item_count",
        "momentum_composite",
    }
    assert row["markets"] == ["ng", "za"]
    assert isinstance(row["total_item_count"], int)
    assert isinstance(row["momentum_composite"], float)


def test_stage_noop_when_flag_off(monkeypatch):
    monkeypatch.delenv("PAN_AFRICAN_ENABLED", raising=False)
    with (
        mock.patch("src.analysis.pan_african.get_client") as gc,
        mock.patch("src.analysis.pan_african.merge_dataframe") as merge,
    ):
        result = run_pan_african_stage(DATE)
    assert result == []
    gc.assert_not_called()
    merge.assert_not_called()


def test_stage_noop_when_flag_explicitly_false(monkeypatch):
    monkeypatch.setenv("PAN_AFRICAN_ENABLED", "false")
    with (
        mock.patch("src.analysis.pan_african.get_client") as gc,
        mock.patch("src.analysis.pan_african.merge_dataframe") as merge,
    ):
        result = run_pan_african_stage(DATE)
    assert result == []
    gc.assert_not_called()
    merge.assert_not_called()


def test_stage_empty_day_writes_nothing(monkeypatch):
    # Flag on, but the fetch returns no rows -> no merge, returns [].
    monkeypatch.setenv("PAN_AFRICAN_ENABLED", "true")
    with (
        mock.patch("src.analysis.pan_african._fetch_trend_scores", return_value=[]),
        mock.patch("src.analysis.pan_african.merge_dataframe") as merge,
    ):
        result = run_pan_african_stage(DATE)
    assert result == []
    merge.assert_not_called()


def test_stage_persists_when_stories_found(monkeypatch):
    monkeypatch.setenv("PAN_AFRICAN_ENABLED", "true")
    fetched = [
        _row("za", "music_amapiano", 0.40, item_count=30),
        _row("ng", "music_afrobeats", 0.25, item_count=20),
    ]
    with (
        mock.patch("src.analysis.pan_african._fetch_trend_scores", return_value=fetched),
        mock.patch("src.analysis.pan_african.merge_dataframe") as merge,
    ):
        result = run_pan_african_stage(DATE)
    assert len(result) == 1
    assert merge.call_count == 1
    args, kwargs = merge.call_args
    # Second positional is the table name, merge_keys is the contract key.
    assert args[1] == "pan_african_stories"
    assert kwargs["merge_keys"] == ["trend_date", "story_id"]
