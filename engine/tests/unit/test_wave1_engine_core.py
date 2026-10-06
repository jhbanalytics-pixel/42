"""Unit tests for Wave 1 EngineCore compute (run_rss_now.py).

Covers the four dark features wired into the scoring pipeline:
  - momentum_label thresholds (rising / building / steady / cooling)
  - lifecycle_phase classifier branches (birth / growth / maturity / decline)
  - continuity counter (new / day2 / day3plus / rebounding)
  - classification instrumentation columns + labelling_rate math
  - the 10-signal composite still sums to 1.0 with momentum NOT in the composite

These are pure-function tests; no BigQuery or network access. compute_trend_scores
is driven with explicit stats dicts and load_scoring patched, matching the
existing tests in test_run_rss_now.py.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest


@pytest.fixture
def run_module():
    import importlib
    import sys

    sys.path.insert(0, "scripts")
    import run_rss_now  # type: ignore

    importlib.reload(run_rss_now)
    return run_rss_now


# --- momentum_label --------------------------------------------------------


def test_momentum_label_rising(run_module):
    """7d well above 30d (past the band) reads rising."""
    assert run_module._momentum_label(0.80, 0.40) == "rising"


def test_momentum_label_cooling(run_module):
    """7d well below 30d reads cooling."""
    assert run_module._momentum_label(0.20, 0.70) == "cooling"


def test_momentum_label_building_inside_band(run_module):
    """Both windows warm and close together reads building, not rising."""
    # 0.52 vs 0.50: gap 0.02, band = max(0.15*0.50, 0.05) = 0.075, inside band.
    assert run_module._momentum_label(0.52, 0.50) == "building"


def test_momentum_label_steady_when_both_dormant(run_module):
    """Neither window carries real signal -> steady (flat / dormant)."""
    assert run_module._momentum_label(0.0, 0.0) == "steady"
    assert run_module._momentum_label(0.02, 0.01) == "steady"


def test_momentum_label_absolute_floor_guards_near_zero(run_module):
    """A positive 7d over a near-zero 30d does not read rising on noise alone.

    7d=0.06, 30d=0.0: both are above/below the 0.05 min-signal differently;
    the absolute floor (0.05) is the threshold, gap 0.06 > 0.05 -> rising is
    correct here, but 7d=0.055 over 30d=0.0 (gap 0.055) is still > floor. Use a
    case strictly inside the floor to prove the guard: 7d=0.05, 30d=0.02.
    """
    # both >= min-signal? 0.05 and 0.02: 0.02 < 0.05 so not both-dormant.
    # gap = 0.03, threshold = max(0.15*0.02, 0.05) = 0.05, inside -> building.
    assert run_module._momentum_label(0.05, 0.02) == "building"


# --- lifecycle_phase -------------------------------------------------------


def test_lifecycle_birth_no_history_real_volume(run_module):
    """No prior 7d median (None) + real volume today -> birth."""
    phase = run_module._lifecycle_phase(
        velocity_14d=0.9, velocity_7d=0.9, item_count=40, median_item_count_7d=None
    )
    assert phase == "birth"


def test_lifecycle_decline_no_history_thin_volume(run_module):
    """No prior history and a thin count today -> decline, not birth."""
    phase = run_module._lifecycle_phase(
        velocity_14d=0.0, velocity_7d=0.0, item_count=1, median_item_count_7d=None
    )
    assert phase == "decline"


def test_lifecycle_growth_rising_past_median(run_module):
    """Count well past the 7d median while 7d velocity is warm -> growth."""
    phase = run_module._lifecycle_phase(
        velocity_14d=0.3, velocity_7d=0.4, item_count=100, median_item_count_7d=50
    )
    assert phase == "growth"


def test_lifecycle_maturity_stable_positive_low_velocity(run_module):
    """Positive composite, count near median, low velocity -> maturity."""
    phase = run_module._lifecycle_phase(
        velocity_14d=0.05, velocity_7d=0.05, item_count=52, median_item_count_7d=50
    )
    assert phase == "maturity"


def test_lifecycle_decline_count_below_median(run_module):
    """Count fallen well below the prior median -> decline."""
    phase = run_module._lifecycle_phase(
        velocity_14d=0.1, velocity_7d=0.1, item_count=20, median_item_count_7d=50
    )
    assert phase == "decline"


def test_lifecycle_decline_when_velocity_cold(run_module):
    """Count near median but 14d velocity at zero -> decline."""
    phase = run_module._lifecycle_phase(
        velocity_14d=0.0, velocity_7d=0.0, item_count=50, median_item_count_7d=50
    )
    assert phase == "decline"


# --- continuity counter ----------------------------------------------------


def test_continuity_new_no_history(run_module):
    """No prior appearance at all -> day 1, new."""
    day, state = run_module._continuity(
        prior_continuity_day=None, appeared_yesterday=False, had_recent_gap=False
    )
    assert (day, state) == (1, "new")


def test_continuity_day2_consecutive(run_module):
    """Appeared yesterday at day 1 -> day 2."""
    day, state = run_module._continuity(
        prior_continuity_day=1, appeared_yesterday=True, had_recent_gap=False
    )
    assert (day, state) == (2, "day2")


def test_continuity_day3plus_consecutive(run_module):
    """Appeared yesterday at day 2 -> day 3, day3plus."""
    day, state = run_module._continuity(
        prior_continuity_day=2, appeared_yesterday=True, had_recent_gap=False
    )
    assert (day, state) == (3, "day3plus")


def test_continuity_rebounding_after_gap(run_module):
    """Had history in the window but not yesterday -> day 1, rebounding."""
    day, state = run_module._continuity(
        prior_continuity_day=None, appeared_yesterday=False, had_recent_gap=True
    )
    assert (day, state) == (1, "rebounding")


def test_fetch_continuity_lookup_handles_null_prior_day(run_module):
    """A NULL prior_continuity_day comes back from BigQuery as pandas pd.NA,
    not Python None. The parse must treat it as None and not crash on int(pd.NA).

    Regression: the live cron aborted on 2026-06-28 with
    `TypeError: int() argument ... not 'NAType'` because the guard used
    `prior_day is None`, which pd.NA fails. A topic seen in the lookback window
    but NOT yesterday yields a NULL prior day, so this row shape is routine.
    """
    import pandas as pd

    df = pd.DataFrame(
        {
            "market": ["za", "za"],
            "query_group": ["had_gap_topic", "consecutive_topic"],
            "prior_continuity_day": [pd.NA, 2],
            "appeared_yesterday": [0, 1],
            "appeared_before_yesterday": [1, 1],
        }
    )
    with patch("src.utils.bigquery.run_query", return_value=df):
        out = run_module.fetch_continuity_lookup(date(2026, 6, 28))

    assert out[("za", "had_gap_topic")]["prior_continuity_day"] is None
    assert out[("za", "had_gap_topic")]["had_recent_gap"] is True
    assert out[("za", "consecutive_topic")]["prior_continuity_day"] == 2


# --- classification instrumentation columns + labelling rate ---------------


def test_classification_columns_empty_when_off(run_module):
    """No layer counts (flag off) -> every column None, byte-identical OFF path."""
    cols = run_module._classification_run_columns({})
    assert cols["classification_regex_rows"] is None
    assert cols["classification_brand24_rows"] is None
    assert cols["labelling_rate_percent"] is None


def test_classification_columns_map_per_layer(run_module):
    """Each layer count lands in its column."""
    counts = {
        "brand24": 5,
        "regex": 40,
        "gdelt": 3,
        "slang": 2,
        "embedding": 10,
        "unclassified": 40,
    }
    cols = run_module._classification_run_columns(counts)
    assert cols["classification_brand24_rows"] == 5
    assert cols["classification_regex_rows"] == 40
    assert cols["classification_gdelt_rows"] == 3
    assert cols["classification_slang_rows"] == 2
    assert cols["classification_embedding_rows"] == 10


def test_labelling_rate_math_excludes_drop(run_module):
    """labelling_rate = 100 * classified / (classified + unclassified).

    classified = 5+40+3+2+10 = 60; unclassified = 40; drop is excluded.
    60 / (60+40) = 60.0%. The drop bucket does not move the denominator.
    """
    counts = {
        "brand24": 5,
        "regex": 40,
        "gdelt": 3,
        "slang": 2,
        "embedding": 10,
        "unclassified": 40,
        "drop": 25,
    }
    cols = run_module._classification_run_columns(counts)
    assert cols["labelling_rate_percent"] == 60.0


def test_labelling_rate_full_when_no_unclassified(run_module):
    """All rows classified -> 100%."""
    counts = {"regex": 80, "unclassified": 0}
    cols = run_module._classification_run_columns(counts)
    assert cols["labelling_rate_percent"] == 100.0


# --- composite still sums to 1.0 with momentum NOT in the composite --------


def _all_signals_one_stats() -> dict:
    """Stats where every normalised signal saturates to 1.0 and tone is present.

    With every signal at 1.0 and a real tone, the composite (before the
    cross-source multiplier, which is 1.0 at channel_diversity<=1) must equal
    the sum of all 10 weights = 1.0 exactly.
    """
    return {
        "item_count": 100.0,
        "source_diversity": 100,  # /10 capped to 1.0
        "channel_diversity": 1,  # multiplier stays 1.0
        "engagement_item_count": 1.0,
        "engagement_sum": 1_000_000.0,  # per-row >> 5000 cap -> 1.0
        "regional_avg": 1.0,
        "genz_avg": 1.0,
        "slang_avg": 1.0,
        "watchlist_avg": 1.0,
        "search_velocity_avg": 1.0,
        "creator_spread": 100,  # /25 capped to 1.0
        "tone_rows": 1,
        "tone_avg_mean": 1.0,
    }


def _live_weights() -> dict:
    return {
        "weights": {
            "velocity": 0.20,
            "genz_score": 0.00,
            "watchlist_score": 0.00,
            "engagement": 0.10,
            "slang_score": 0.10,
            "diversity": 0.16,
            "regional_score": 0.12,
            "creator_spread": 0.12,
            "search_velocity_score": 0.15,
            "tone_score": 0.05,
        },
        "cross_source_bonus_per_channel": 0.05,
        "cross_source_max_bonus": 0.15,
    }


def test_bundle_sums_to_one_momentum_not_in_composite(run_module, monkeypatch):
    """With all 10 signals at 1.0 and momentum NOT in the composite, the score
    is exactly 1.0. Momentum windows are present but display-only, so they do
    not change the composite."""
    monkeypatch.delenv("MOMENTUM_IN_COMPOSITE", raising=False)
    velocity_scores = {("za", "news"): 1.0}
    velocity_windows = {
        ("za", "news"): {
            "velocity_score_7d": 1.0,
            "velocity_score_30d": 1.0,
            "median_item_count_7d": 100.0,
        }
    }
    with patch.object(run_module, "load_scoring", return_value=_live_weights()):
        rows = run_module.compute_trend_scores(
            {("za", "news"): _all_signals_one_stats()},
            date(2026, 6, 19),
            datetime(2026, 6, 19, tzinfo=UTC),
            velocity_scores,
            velocity_windows=velocity_windows,
        )
    assert rows[0]["trend_score"] == pytest.approx(1.0, abs=1e-9)


def test_momentum_windows_do_not_change_live_score(run_module, monkeypatch):
    """The trend_score with momentum windows present equals the trend_score
    without them. Momentum is display-only in Wave 1."""
    monkeypatch.delenv("MOMENTUM_IN_COMPOSITE", raising=False)
    stats = {
        "item_count": 30.0,
        "source_diversity": 4,
        "channel_diversity": 2,
        "engagement_item_count": 30.0,
        "engagement_sum": 60_000.0,
        "regional_avg": 0.3,
        "genz_avg": 0.4,
        "slang_avg": 0.2,
        "watchlist_avg": 0.1,
        "search_velocity_avg": 0.0,
        "creator_spread": 3,
        "tone_rows": 0,
        "tone_avg_mean": None,
    }
    velocity_scores = {("ng", "afrobeats"): 0.5}
    windows = {
        ("ng", "afrobeats"): {
            "velocity_score_7d": 0.9,
            "velocity_score_30d": 0.1,
            "median_item_count_7d": 20.0,
        }
    }
    with patch.object(run_module, "load_scoring", return_value=_live_weights()):
        without = run_module.compute_trend_scores(
            {("ng", "afrobeats"): dict(stats)},
            date(2026, 6, 19),
            datetime(2026, 6, 19, tzinfo=UTC),
            velocity_scores,
        )
        with_windows = run_module.compute_trend_scores(
            {("ng", "afrobeats"): dict(stats)},
            date(2026, 6, 19),
            datetime(2026, 6, 19, tzinfo=UTC),
            velocity_scores,
            velocity_windows=windows,
        )
    assert without[0]["trend_score"] == with_windows[0]["trend_score"]
    # And the momentum columns ARE populated on the windows run.
    assert with_windows[0]["velocity_score_7d"] == 0.9
    assert with_windows[0]["momentum_label"] == "rising"


def test_momentum_carve_demotes_flash_keeps_sustained(run_module, monkeypatch):
    """MOMENTUM_IN_COMPOSITE on, momentum (0.07) carved FROM velocity (0.13).

    The momentum signal is min(7d, 30d). A sustained topic (both windows at 1.0)
    keeps the full velocity-family weight so its score is unchanged at 1.0. A
    one-day flash (both windows at 0.0, but the same 14d velocity) loses the 0.07
    momentum weight with no compensating bonus and is demoted to 0.93. Proves the
    carve makes the score harder to fool while keeping the bundle at 1.0."""
    monkeypatch.setenv("MOMENTUM_IN_COMPOSITE", "true")
    weights = _live_weights()
    weights["weights"]["momentum"] = 0.07
    sustained = {
        ("za", "news"): {
            "velocity_score_7d": 1.0,
            "velocity_score_30d": 1.0,
            "median_item_count_7d": 50.0,
        }
    }
    flash = {
        ("za", "news"): {
            "velocity_score_7d": 0.0,
            "velocity_score_30d": 0.0,
            "median_item_count_7d": 50.0,
        }
    }
    with patch.object(run_module, "load_scoring", return_value=weights):
        sus = run_module.compute_trend_scores(
            {("za", "news"): _all_signals_one_stats()},
            date(2026, 6, 19),
            datetime(2026, 6, 19, tzinfo=UTC),
            {("za", "news"): 1.0},
            velocity_windows=sustained,
        )
        fla = run_module.compute_trend_scores(
            {("za", "news"): _all_signals_one_stats()},
            date(2026, 6, 19),
            datetime(2026, 6, 19, tzinfo=UTC),
            {("za", "news"): 1.0},
            velocity_windows=flash,
        )
    assert sus[0]["trend_score"] == 1.0
    assert round(fla[0]["trend_score"], 4) == 0.93
    assert sus[0]["trend_score"] > fla[0]["trend_score"]


def test_lifecycle_empty_when_flag_off(run_module, monkeypatch):
    """LIFECYCLE_ENABLED off -> lifecycle_phase stays None (column NULL)."""
    monkeypatch.delenv("LIFECYCLE_ENABLED", raising=False)
    windows = {
        ("za", "news"): {
            "velocity_score_7d": 0.9,
            "velocity_score_30d": 0.1,
            "median_item_count_7d": 10.0,
        }
    }
    with patch.object(run_module, "load_scoring", return_value=_live_weights()):
        rows = run_module.compute_trend_scores(
            {("za", "news"): _all_signals_one_stats()},
            date(2026, 6, 19),
            datetime(2026, 6, 19, tzinfo=UTC),
            {("za", "news"): 1.0},
            velocity_windows=windows,
        )
    assert rows[0]["lifecycle_phase"] is None


def test_lifecycle_populated_when_flag_on(run_module, monkeypatch):
    """LIFECYCLE_ENABLED on -> lifecycle_phase is classified and stored."""
    monkeypatch.setenv("LIFECYCLE_ENABLED", "true")
    windows = {
        ("za", "news"): {
            "velocity_score_7d": 0.9,
            "velocity_score_30d": 0.1,
            "median_item_count_7d": 10.0,
        }
    }
    with patch.object(run_module, "load_scoring", return_value=_live_weights()):
        rows = run_module.compute_trend_scores(
            {("za", "news"): _all_signals_one_stats()},
            date(2026, 6, 19),
            datetime(2026, 6, 19, tzinfo=UTC),
            {("za", "news"): 1.0},
            velocity_windows=windows,
        )
    # item_count 100 vs median 10 = 10x, 7d velocity warm -> growth.
    assert rows[0]["lifecycle_phase"] == "growth"


def test_continuity_empty_when_flag_off(run_module, monkeypatch):
    """CONTINUITY_BADGES_ENABLED off -> continuity columns stay None."""
    monkeypatch.delenv("CONTINUITY_BADGES_ENABLED", raising=False)
    with patch.object(run_module, "load_scoring", return_value=_live_weights()):
        rows = run_module.compute_trend_scores(
            {("za", "news"): _all_signals_one_stats()},
            date(2026, 6, 19),
            datetime(2026, 6, 19, tzinfo=UTC),
            {("za", "news"): 1.0},
        )
    assert rows[0]["continuity_day"] is None
    assert rows[0]["continuity_state"] is None


def test_continuity_populated_when_flag_on(run_module, monkeypatch):
    """CONTINUITY_BADGES_ENABLED on -> continuity computed from the lookup."""
    monkeypatch.setenv("CONTINUITY_BADGES_ENABLED", "true")
    continuity_lookup = {
        ("za", "news"): {
            "prior_continuity_day": 2,
            "appeared_yesterday": True,
            "had_recent_gap": False,
        }
    }
    with patch.object(run_module, "load_scoring", return_value=_live_weights()):
        rows = run_module.compute_trend_scores(
            {("za", "news"): _all_signals_one_stats()},
            date(2026, 6, 19),
            datetime(2026, 6, 19, tzinfo=UTC),
            {("za", "news"): 1.0},
            continuity_lookup=continuity_lookup,
        )
    assert rows[0]["continuity_day"] == 3
    assert rows[0]["continuity_state"] == "day3plus"
