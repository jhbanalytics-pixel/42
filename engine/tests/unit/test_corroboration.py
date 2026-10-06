"""Unit tests for the deterministic corroboration scorer.

Pure: no BigQuery, no Gemini. Covers the four quadrants (factual x social), the
recency decay, the None published_at neutral case, and the hard invariant that
social families alone never yield the "corroborated" tier.
"""

from datetime import UTC, datetime, timedelta

from src.scoring.corroboration import (
    RECENCY_UNKNOWN_HOURS,
    compute_corroboration,
)

NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=UTC)
FRESH = NOW - timedelta(hours=1)  # essentially full recency weight


def _call(weights, **kw):
    params = {
        "channel_family_weights": weights,
        "freshest_published_at": FRESH,
        "now": NOW,
        "search_velocity": 0.0,
        "tone_rows": 0,
        "config": None,
    }
    params.update(kw)
    return compute_corroboration(**params)


# --- quadrant 1: high factual + high social --------------------------------


def test_high_factual_high_social_both_strong():
    out = _call(
        {"news": 5.0, "search": 2.0, "youtube": 1.0, "ensemble": 3.0, "reddit": 1.0},
        search_velocity=0.5,
        tone_rows=4,
    )
    assert out["factual_corroboration"] > 0.6
    assert out["social_corroboration"] > 0.4
    assert out["confidence_tier"] == "corroborated"


# --- quadrant 2: high factual + low social must NOT be under-confident ------


def test_high_factual_low_social_not_underconfident():
    out = _call(
        {"news": 5.0, "search": 2.0, "youtube": 1.0},
        search_velocity=0.5,
        tone_rows=4,
    )
    # No social channels at all, but factual corroboration must stay strong and
    # the tier must be corroborated. Factual is not penalised for social absence.
    assert out["factual_corroboration"] > 0.6
    assert out["social_corroboration"] == 0.0
    assert out["confidence_tier"] == "corroborated"


# --- quadrant 3: low factual + high social = social-only, NEVER corroborated -


def test_low_factual_high_social_is_social_only():
    out = _call(
        {"ensemble": 8.0, "reddit": 3.0, "brand24": 2.0, "threads": 1.0},
        search_velocity=0.9,
        tone_rows=10,
    )
    assert out["factual_corroboration"] == 0.0
    assert out["social_corroboration"] > 0.5
    assert out["confidence_tier"] == "social-only"


def test_social_families_alone_never_corroborated():
    # Even with an absurd number of social families and every factual bonus
    # input maxed, no factual family means the tier can never be corroborated.
    out = _call(
        {
            "ensemble": 99.0,
            "reddit": 99.0,
            "brand24": 99.0,
            "ig": 99.0,
            "threads": 99.0,
            "some_new_social_family": 99.0,
        },
        search_velocity=1.0,
        tone_rows=999,
    )
    assert out["confidence_tier"] != "corroborated"
    assert out["confidence_tier"] == "social-only"
    assert out["factual_corroboration"] == 0.0


# --- quadrant 4: low + low = thin ------------------------------------------


def test_low_low_is_thin():
    out = _call({})
    assert out["factual_corroboration"] == 0.0
    assert out["social_corroboration"] == 0.0
    assert out["confidence_tier"] == "thin"


# --- single factual family early -> single-source-factual ------------------


def test_single_factual_family_is_single_source():
    # One factual family, no entity/search bonus, so it does not clear the
    # corroborated floor: the tier is the early single-source-factual.
    out = _call({"news": 3.0})
    assert out["confidence_tier"] == "single-source-factual"
    assert out["factual_corroboration"] > 0.0
    assert out["factual_corroboration"] < 0.40


def test_single_factual_family_with_bonuses_reaches_corroborated():
    # One factual family but with both factual bonuses present clears the floor.
    out = _call({"news": 3.0}, search_velocity=0.5, tone_rows=4)
    assert out["confidence_tier"] == "corroborated"
    assert out["factual_corroboration"] >= 0.40


# --- recency decay ----------------------------------------------------------


def test_recency_decay_reduces_corroboration():
    weights = {"news": 5.0, "search": 2.0, "ensemble": 3.0}
    fresh = _call(weights, freshest_published_at=NOW - timedelta(hours=1))
    stale = _call(weights, freshest_published_at=NOW - timedelta(hours=96))
    # 96h at a 48h half-life is two half-lives -> ~0.25 of the fresh weight.
    assert stale["factual_corroboration"] < fresh["factual_corroboration"]
    assert stale["social_corroboration"] < fresh["social_corroboration"]
    assert stale["corroboration_recency_hours"] == 96
    assert fresh["corroboration_recency_hours"] == 1


def test_recency_half_life_is_roughly_half():
    weights = {"news": 5.0, "search": 2.0}
    fresh = _call(weights, freshest_published_at=NOW)
    one_hl = _call(weights, freshest_published_at=NOW - timedelta(hours=48))
    ratio = one_hl["factual_corroboration"] / fresh["factual_corroboration"]
    assert 0.45 < ratio < 0.55


# --- None published_at neutral case ----------------------------------------


def test_none_published_at_is_neutral():
    out = _call({"news": 5.0, "search": 2.0}, freshest_published_at=None)
    # Neutral 0.5 factor, and the recency hours is the unknown sentinel.
    assert out["corroboration_recency_hours"] == RECENCY_UNKNOWN_HOURS
    assert out["factual_corroboration"] > 0.0
    # With a 0.5 neutral factor the score is exactly half of the full-recency
    # equivalent at the same breadth.
    full = _call({"news": 5.0, "search": 2.0}, freshest_published_at=NOW)
    assert abs(out["factual_corroboration"] - full["factual_corroboration"] * 0.5) < 0.02


# --- bounded 0..1 -----------------------------------------------------------


def test_scores_are_bounded():
    out = _call(
        {"news": 9.0, "search": 9.0, "youtube": 9.0, "music": 9.0},
        search_velocity=1.0,
        tone_rows=50,
        freshest_published_at=NOW,
    )
    assert 0.0 <= out["factual_corroboration"] <= 1.0
    assert 0.0 <= out["social_corroboration"] <= 1.0


# --- config override --------------------------------------------------------


def test_config_block_overrides_defaults():
    cfg = {"corroboration": {"corroborated_floor": 0.99}}
    # A floor of 0.99 means a normally-corroborated topic drops below it.
    out = compute_corroboration(
        channel_family_weights={"news": 5.0, "search": 2.0},
        freshest_published_at=NOW,
        now=NOW,
        search_velocity=0.0,
        tone_rows=0,
        config=cfg,
    )
    assert out["confidence_tier"] != "corroborated"


# --- unknown family defaults to social -------------------------------------


def test_unknown_family_treated_as_social():
    out = _call({"some_future_platform": 4.0})
    assert out["factual_corroboration"] == 0.0
    assert out["social_corroboration"] > 0.0
    assert out["confidence_tier"] == "social-only"
