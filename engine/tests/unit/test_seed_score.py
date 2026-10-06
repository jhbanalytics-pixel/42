"""Unit tests for the seed score (Jo, 22 Jun 2026).

seed_score is a 0..1 worth-seeding-for-Nanobanana/Lyria signal built from
audience-fit + creative-format-fit atoms, deliberately independent of the
velocity/diversity signals that drive trend_score. These tests pin the pure
helper (range, clamping, tone gate) and the hidden-gem invariant: a topic that
aligns with the audience and suits image/audio creation scores high on seed even
when its trend_score is low.
"""

import datetime as dt

from scripts.run_rss_now import _seed_breakdown, compute_seed_score, compute_trend_scores


def test_seed_breakdown_components_exact():
    """The breakdown the tool shows must be the real decomposition:
    audience_fit 0.7725, format_fit 1.0, gate 1.0, seed 0.7725, and the float
    wrapper equals the breakdown's seed_score."""
    b = _seed_breakdown(
        genz=0.9,
        slang=0.85,
        engagement_score=0.7,
        creator_spread=20,
        visual_audio_share=1.0,
        tone_score=0.6,
        tone_rows=3,
    )
    assert b["seed_score"] == 0.7725
    assert b["audience_fit"] == 0.7725
    assert b["format_fit"] == 1.0
    assert b["safety_gate"] == 1.0
    assert b["visual_audio_share"] == 1.0
    assert (
        compute_seed_score(
            genz=0.9,
            slang=0.85,
            engagement_score=0.7,
            creator_spread=20,
            visual_audio_share=1.0,
            tone_score=0.6,
            tone_rows=3,
        )
        == b["seed_score"]
    )


def test_seed_breakdown_unsafe_tone_gate():
    b = _seed_breakdown(
        genz=0.8,
        slang=0.8,
        engagement_score=0.6,
        creator_spread=10,
        visual_audio_share=0.8,
        tone_score=0.1,
        tone_rows=4,
    )
    assert b["safety_gate"] == 0.6
    assert b["seed_score"] == round(b["audience_fit"] * b["format_fit"] * 0.6, 4)


def test_compute_trend_scores_row_carries_seed_components():
    market_counts = {
        ("za", "genz_softlife"): {
            "item_count": 40.0,
            "source_diversity": 1,
            "platform_diversity": 1,
            "channel_diversity": 1,
            "engagement_item_count": 40.0,
            "regional_avg": 0.0,
            "genz_avg": 0.9,
            "slang_avg": 0.9,
            "watchlist_avg": 0.0,
            "search_velocity_avg": 0.0,
            "engagement_sum": 0.0,
            "creator_spread": 2,
            "tone_avg_mean": None,
            "tone_rows": 0,
            "channel_family_weights": {"ensemble": 40.0},
        }
    }
    rows = compute_trend_scores(
        market_counts, dt.date(2026, 6, 22), dt.datetime(2026, 6, 22, 0, 30), velocity_scores={}
    )
    row = rows[0]
    for k in ("seed_audience_fit", "seed_format_fit", "seed_safety_gate", "visual_audio_share"):
        assert k in row
        assert 0.0 <= row[k] <= 1.0
    # format_fit on an all-ensemble topic (share 1.0) is floor + (1-floor)*1 = 1.0
    assert row["seed_format_fit"] == 1.0

    variants = (
        ({"genz_avg": 0.0, "watchlist_avg": 0.0}, "genz_score", None),
        ({"genz_avg": 1.0, "watchlist_avg": 0.0}, "genz_score", "watchlist_score"),
        ({"genz_avg": 0.0, "watchlist_avg": 1.0}, "watchlist_score", "genz_score"),
    )
    scored = []
    for overrides, changed_field, unchanged_field in variants:
        counts = {key: {**stats, **overrides} for key, stats in market_counts.items()}
        variant = compute_trend_scores(
            counts,
            dt.date(2026, 6, 22),
            dt.datetime(2026, 6, 22, 0, 30),
            velocity_scores={},
        )[0]
        scored.append(variant)
        if unchanged_field:
            assert variant[changed_field] != scored[0][changed_field]
            assert variant[unchanged_field] == scored[0][unchanged_field]

    for variant in scored[1:]:
        for key in (
            "trend_score",
            "seed_score",
            "seed_audience_fit",
            "seed_format_fit",
            "seed_safety_gate",
            "visual_audio_share",
        ):
            assert variant[key] == scored[0][key]


def test_high_audience_visual_topic_scores_high():
    s = compute_seed_score(
        genz=0.9,
        slang=0.85,
        engagement_score=0.7,
        creator_spread=20,
        visual_audio_share=1.0,
        tone_score=0.6,
        tone_rows=3,
    )
    assert s > 0.6
    assert 0.0 <= s <= 1.0


def test_formula_exact_worked_example():
    """Lock the exact coefficients, not just the shape. audience_fit =
    0.25*0.85 + 0.40*0.7 + 0.35*0.8 = 0.7725; format_fit = 1.0;
    gate = 1.0. A weight change slips past the monotonic tests but not this."""
    s = compute_seed_score(
        genz=0.9,
        slang=0.85,
        engagement_score=0.7,
        creator_spread=20,
        visual_audio_share=1.0,
        tone_score=0.6,
        tone_rows=3,
    )
    assert s == 0.7725


def test_seed_weights_match_neutral_contract():
    from src.utils.config_loader import load_scoring

    assert load_scoring()["seed_score"]["audience_weights"] == {
        "genz": 0.0,
        "slang": 0.25,
        "engagement": 0.40,
        "creator_spread": 0.35,
    }


def test_clamps_out_of_range_and_nan_inputs():
    s = compute_seed_score(
        genz=5.0,
        slang=-2.0,
        engagement_score=float("nan"),
        creator_spread=999,
        visual_audio_share=9.0,
        tone_score=0.6,
        tone_rows=1,
    )
    assert 0.0 <= s <= 1.0


def test_negative_tone_downweights_when_gdelt_present():
    kwargs = {
        "genz": 0.8,
        "slang": 0.8,
        "engagement_score": 0.6,
        "creator_spread": 10,
        "visual_audio_share": 0.8,
        "tone_rows": 4,
    }
    safe = compute_seed_score(tone_score=0.6, **kwargs)
    risky = compute_seed_score(tone_score=0.1, **kwargs)
    assert risky < safe
    # The gate multiplies by 0.6, within rounding tolerance.
    assert abs(risky - safe * 0.6) < 0.01


def test_no_gdelt_tone_is_not_penalised():
    kwargs = {
        "genz": 0.8,
        "slang": 0.8,
        "engagement_score": 0.6,
        "creator_spread": 10,
        "visual_audio_share": 0.8,
    }
    no_tone = compute_seed_score(tone_score=0.0, tone_rows=0, **kwargs)
    safe_tone = compute_seed_score(tone_score=0.6, tone_rows=4, **kwargs)
    assert no_tone == safe_tone


def test_seed_score_ignores_genz_even_when_legacy_config_weights_it():
    kwargs = {
        "slang": 0.5,
        "engagement_score": 0.5,
        "creator_spread": 10,
        "visual_audio_share": 0.5,
        "tone_score": 0.6,
        "tone_rows": 2,
    }
    legacy_cfg = {
        "audience_weights": {
            "genz": 0.35,
            "slang": 0.25,
            "engagement": 0.25,
            "creator_spread": 0.15,
        }
    }
    lo = compute_seed_score(genz=0.0, cfg=legacy_cfg, **kwargs)
    hi = compute_seed_score(genz=1.0, cfg=legacy_cfg, **kwargs)
    assert hi == lo


def test_more_visual_audio_share_raises_seed():
    kwargs = {
        "genz": 0.7,
        "slang": 0.6,
        "engagement_score": 0.5,
        "creator_spread": 10,
        "tone_score": 0.6,
        "tone_rows": 2,
    }
    news = compute_seed_score(visual_audio_share=0.0, **kwargs)
    social = compute_seed_score(visual_audio_share=1.0, **kwargs)
    assert social > news


def _stats(**over):
    base = {
        "item_count": 40.0,
        "source_diversity": 1,
        "platform_diversity": 1,
        "channel_diversity": 1,
        "engagement_item_count": 40.0,
        "regional_avg": 0.0,
        "genz_avg": 0.9,
        "slang_avg": 0.9,
        "watchlist_avg": 0.0,
        "search_velocity_avg": 0.0,
        "engagement_sum": 0.0,
        "creator_spread": 2,
        "tone_avg_mean": None,
        "tone_rows": 0,
        "channel_family_weights": {"ensemble": 40.0},
    }
    base.update(over)
    return base


def test_high_participation_visual_topic_seed_beats_trend():
    """Real engagement and creator spread can surface a visual hidden gem."""
    market_counts = {
        ("za", "culture_softlife"): _stats(
            engagement_sum=200_000.0,
            creator_spread=20,
        )
    }
    rows = compute_trend_scores(
        market_counts,
        dt.date(2026, 6, 22),
        dt.datetime(2026, 6, 22, 0, 30),
        velocity_scores={},
    )
    assert len(rows) == 1
    row = rows[0]
    assert "seed_score" in row
    assert 0.0 <= row["seed_score"] <= 1.0
    assert row["seed_score"] > 0.4
    # The point of the metric: this topic does not rank on trend_score but does
    # on seed_score.
    assert row["seed_score"] > row["trend_score"]


def test_news_only_topic_scores_low_seed_in_compute_trend_scores():
    market_counts = {
        ("za", "politics_budget"): _stats(
            genz_avg=0.1,
            slang_avg=0.1,
            channel_family_weights={"news": 40.0},
        )
    }
    rows = compute_trend_scores(
        market_counts,
        dt.date(2026, 6, 22),
        dt.datetime(2026, 6, 22, 0, 30),
        velocity_scores={},
    )
    assert rows[0]["seed_score"] < 0.4
