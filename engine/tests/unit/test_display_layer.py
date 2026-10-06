"""Unit tests for the PULSE v2 display layer pure functions."""

from src.analysis.display_layer import (
    channel_weights,
    confidence_label,
    in_market_pct,
    search_read,
    trend_phase,
    trend_state,
)


def test_trend_state_new_when_no_prior():
    assert trend_state(score=0.4, prior_score=None)["badge"] == "New on the board"


def test_trend_state_accelerating_on_rising_score():
    s = trend_state(score=0.5, prior_score=0.4)
    assert s["badge"] == "Building"
    assert s["direction"] == "up"


def test_trend_state_cooled_on_falling_score():
    assert trend_state(score=0.3, prior_score=0.45)["direction"] == "down"


def test_trend_phase_peaking_high_velocity_high_score():
    assert trend_phase(score=0.55, velocity=0.4) == "Peaking"


def test_trend_phase_emerging_low_score_high_velocity():
    assert trend_phase(score=0.2, velocity=0.5) == "Emerging"


def test_in_market_pct_drops_on_foreign_rows():
    rows = [
        {"title": "sapa hustle jozi side gig", "text": "", "hashtags": ""},
        {"title": "sapa vietnam fansipan tour package", "text": "", "hashtags": ""},
    ]
    pct = in_market_pct(rows, market="ng", topic="economy_sapa_hustle")
    assert 40 <= pct <= 60


def test_channel_weights_scales_to_max():
    w = channel_weights({"tiktok": 40, "threads": 20, "reddit": 5})
    assert w[0] == ("tiktok", 4)
    assert ("reddit", 1) in w


def test_confidence_single_source_not_confirmed():
    assert "single-source" in confidence_label(1).lower()
    # The label must never say "confirmed" (Nick: unattributed claim, client risk).
    assert "confirmed" not in confidence_label(4).lower()
    assert "4 channels" in confidence_label(4)


def test_search_read_labels():
    assert search_read(velocity_7d=0.5) == "rising"
    assert search_read(velocity_7d=0.0) == "flat"
    assert search_read(velocity_7d=None) == "no lift"
