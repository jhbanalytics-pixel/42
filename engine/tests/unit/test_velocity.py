"""Unit tests for velocity scoring."""

import math
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest
from src.scoring import velocity as velocity_module
from src.scoring.velocity import (
    NEW_TOPIC_SCORE,
    NO_SIGNAL_SCORE,
    compute_velocity_score,
    compute_velocity_scores_for_today,
)


def _baseline_df(avg: float | None) -> pd.DataFrame:
    """Build the shape run_query returns for the single-key velocity query."""
    return pd.DataFrame({"baseline_avg": [avg]})


def _empty_baseline_df() -> pd.DataFrame:
    """Empty DataFrame with the expected column, simulates no-history."""
    return pd.DataFrame({"baseline_avg": []})


def _batch_df(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Shape run_query returns for the batch velocity query."""
    if not rows:
        return pd.DataFrame({"market": [], "query_group": [], "baseline_avg": []})
    return pd.DataFrame(rows)


def test_velocity_spike_5x_saturates_to_one(monkeypatch):
    """today=500, baseline_avg=100 gives raw=5.0, score=(5-1)/4=1.0."""
    monkeypatch.setattr(velocity_module, "run_query", lambda *a, **k: _baseline_df(100.0))
    score = compute_velocity_score("za", "news", today_count=500)
    assert score == 1.0


def test_velocity_flat_returns_zero(monkeypatch):
    """today=100, baseline_avg=100 gives raw=1.0, score=0.0 (not growing)."""
    monkeypatch.setattr(velocity_module, "run_query", lambda *a, **k: _baseline_df(100.0))
    score = compute_velocity_score("za", "news", today_count=100)
    assert score == 0.0


def test_velocity_decline_returns_zero(monkeypatch):
    """today=50, baseline_avg=100 gives raw=0.5, score=0.0 (declining)."""
    monkeypatch.setattr(velocity_module, "run_query", lambda *a, **k: _baseline_df(100.0))
    score = compute_velocity_score("za", "news", today_count=50)
    assert score == 0.0


def test_velocity_new_topic_is_volume_scaled(monkeypatch):
    """No history rows: a thin new topic (today=10) scores 10/75, not a flat 1.0."""
    monkeypatch.setattr(velocity_module, "run_query", lambda *a, **k: _empty_baseline_df())
    score = compute_velocity_score("ng", "afrobeats", today_count=10)
    assert score == round(10 / 75, 4)


def test_velocity_new_topic_at_full_volume(monkeypatch):
    """A new topic at or above the typical volume earns the full ceiling."""
    monkeypatch.setattr(velocity_module, "run_query", lambda *a, **k: _empty_baseline_df())
    assert compute_velocity_score("ng", "afrobeats", today_count=75) == NEW_TOPIC_SCORE
    assert compute_velocity_score("ng", "afrobeats", today_count=300) == NEW_TOPIC_SCORE


def test_velocity_new_topic_with_zero_today_zero_baseline(monkeypatch):
    """No history and no today signal returns 0.0, nothing to score."""
    monkeypatch.setattr(velocity_module, "run_query", lambda *a, **k: _empty_baseline_df())
    score = compute_velocity_score("ke", "politics", today_count=0)
    assert score == NO_SIGNAL_SCORE


def test_velocity_baseline_avg_is_null(monkeypatch):
    """AVG returns a single NULL row when nothing matches, treat as no-history."""
    monkeypatch.setattr(velocity_module, "run_query", lambda *a, **k: _baseline_df(None))
    score = compute_velocity_score("za", "news", today_count=25)
    assert score == round(25 / 75, 4)


def test_velocity_2x_lift_is_quarter(monkeypatch):
    """today=200, baseline_avg=100 gives raw=2.0, score=(2-1)/4=0.25."""
    monkeypatch.setattr(velocity_module, "run_query", lambda *a, **k: _baseline_df(100.0))
    score = compute_velocity_score("za", "news", today_count=200)
    assert score == 0.25


def test_velocity_passes_params_to_run_query(monkeypatch):
    """The scalar helper binds market, query_group, and baseline_days as params."""
    captured: dict[str, Any] = {}

    def fake_run_query(sql: str, params: dict | None = None) -> pd.DataFrame:
        captured["sql"] = sql
        captured["params"] = params
        return _baseline_df(50.0)

    monkeypatch.setattr(velocity_module, "run_query", fake_run_query)
    compute_velocity_score("za", "news", today_count=100, baseline_days=14)

    assert captured["params"] == {
        "market": "za",
        "query_group": "news",
        "baseline_days": 14,
    }
    assert "@market" in captured["sql"]
    assert "@query_group" in captured["sql"]
    assert "@baseline_days" in captured["sql"]


def test_batch_helper_single_query(monkeypatch):
    """Batch helper issues ONE query for N input pairs, not N."""
    mock_run_query = MagicMock(
        return_value=_batch_df(
            [
                {"market": "za", "query_group": "news", "baseline_avg": 100.0},
                {"market": "ng", "query_group": "news", "baseline_avg": 50.0},
                {"market": "ke", "query_group": "politics", "baseline_avg": 20.0},
            ]
        )
    )
    monkeypatch.setattr(velocity_module, "run_query", mock_run_query)

    market_counts = {
        ("za", "news"): {"item_count": 200},
        ("ng", "news"): {"item_count": 75},
        ("ke", "politics"): {"item_count": 40},
    }
    scores = compute_velocity_scores_for_today(market_counts)

    assert mock_run_query.call_count == 1
    assert len(scores) == 3


def test_batch_helper_returns_score_per_input(monkeypatch):
    """Every input key gets a score in the output dict, even with no history."""
    monkeypatch.setattr(
        velocity_module,
        "run_query",
        lambda *a, **k: _batch_df(
            [
                {"market": "za", "query_group": "news", "baseline_avg": 100.0},
            ]
        ),
    )

    market_counts = {
        ("za", "news"): {"item_count": 500},
        ("ng", "news"): {"item_count": 10},
        ("ke", "politics"): {"item_count": 0},
    }
    scores = compute_velocity_scores_for_today(market_counts)

    assert set(scores.keys()) == set(market_counts.keys())
    # za has 5x spike over 100 baseline, saturates to 1.0
    assert scores[("za", "news")] == 1.0
    # ng has no baseline row, today=10, new-topic score is volume-scaled (10/75)
    assert scores[("ng", "news")] == round(10 / 75, 4)
    # ke has no baseline row, today=0, returns no-signal 0.0
    assert scores[("ke", "politics")] == NO_SIGNAL_SCORE


def test_batch_helper_nan_baseline_treated_as_zero(monkeypatch):
    """A numpy NaN baseline_avg is neutralised to 0.0, not propagated.

    `float(nan or 0.0)` keeps the NaN because NaN is truthy; the lookup
    must guard with pd.isna so the score falls through to the new-topic
    path instead of going NaN.
    """
    monkeypatch.setattr(
        velocity_module,
        "run_query",
        lambda *a, **k: _batch_df(
            [
                {"market": "za", "query_group": "news", "baseline_avg": float("nan")},
            ]
        ),
    )

    scores = compute_velocity_scores_for_today({("za", "news"): {"item_count": 30}})

    score = scores[("za", "news")]
    assert not math.isnan(score)
    # baseline 0.0 means new-topic volume-scaled score (30/75)
    assert score == round(30 / 75, 4)


def test_batch_helper_empty_input_returns_empty(monkeypatch):
    """No input pairs means no query and an empty dict back."""
    mock_run_query = MagicMock()
    monkeypatch.setattr(velocity_module, "run_query", mock_run_query)

    scores = compute_velocity_scores_for_today({})

    assert scores == {}
    assert mock_run_query.call_count == 0


def test_batch_helper_baseline_days_is_parameter(monkeypatch):
    """baseline_days reaches run_query as a bound parameter."""
    captured: dict[str, Any] = {}

    def fake_run_query(sql: str, params: dict | None = None) -> pd.DataFrame:
        captured["params"] = params
        return _batch_df([])

    monkeypatch.setattr(velocity_module, "run_query", fake_run_query)
    compute_velocity_scores_for_today({("za", "news"): {"item_count": 10}}, baseline_days=7)

    assert captured["params"] == {"baseline_days": 7}


@pytest.mark.parametrize(
    "today_count,baseline_avg,expected",
    [
        (100, 100.0, 0.0),  # flat
        (50, 100.0, 0.0),  # decline
        (500, 100.0, 1.0),  # 5x saturates
        (1000, 100.0, 1.0),  # 10x still saturates
        (200, 100.0, 0.25),  # 2x lift
        (300, 100.0, 0.5),  # 3x lift
    ],
)
def test_normalise_formula(today_count, baseline_avg, expected):
    """The internal _normalise applies the documented formula."""
    assert velocity_module._normalise(today_count, baseline_avg) == expected
