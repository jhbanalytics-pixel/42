"""Unit tests for Wave 1 momentum windows (7d/30d) in velocity.py.

Covers compute_velocity_windows_for_today: the 7-day and 30-day baselines plus
the 7-day median item_count, all in one batch query, returned per
(market, query_group). The 14-day live score path is unchanged and is tested
in test_velocity.py.
"""

from typing import Any
from unittest.mock import MagicMock

import pandas as pd
from src.scoring import velocity as velocity_module
from src.scoring.velocity import compute_velocity_windows_for_today


def _windows_df(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Shape run_query returns for the windows query."""
    if not rows:
        return pd.DataFrame(
            {
                "market": [],
                "query_group": [],
                "baseline_avg_short": [],
                "baseline_avg_long": [],
                "median_item_count_short": [],
            }
        )
    return pd.DataFrame(rows)


def test_windows_single_query_for_all_pairs(monkeypatch):
    """Both window baselines + median come from ONE query, not three."""
    mock_run_query = MagicMock(
        return_value=_windows_df(
            [
                {
                    "market": "za",
                    "query_group": "news",
                    "baseline_avg_short": 100.0,
                    "baseline_avg_long": 50.0,
                    "median_item_count_short": 90.0,
                },
            ]
        )
    )
    monkeypatch.setattr(velocity_module, "run_query", mock_run_query)

    out = compute_velocity_windows_for_today({("za", "news"): {"item_count": 200}})

    assert mock_run_query.call_count == 1
    assert set(out[("za", "news")].keys()) == {
        "velocity_score_7d",
        "velocity_score_30d",
        "median_item_count_7d",
    }


def test_windows_7d_and_30d_score_independently(monkeypatch):
    """today=200 over a 7d baseline of 100 is 2x (0.25); over a 30d baseline of
    50 it is 4x ((4-1)/4=0.75). The two windows score against their own
    baselines."""
    monkeypatch.setattr(
        velocity_module,
        "run_query",
        lambda *a, **k: _windows_df(
            [
                {
                    "market": "za",
                    "query_group": "news",
                    "baseline_avg_short": 100.0,
                    "baseline_avg_long": 50.0,
                    "median_item_count_short": 100.0,
                },
            ]
        ),
    )

    out = compute_velocity_windows_for_today({("za", "news"): {"item_count": 200}})

    assert out[("za", "news")]["velocity_score_7d"] == 0.25
    assert out[("za", "news")]["velocity_score_30d"] == 0.75


def test_windows_no_history_is_volume_scaled_both(monkeypatch):
    """A pair with no baseline row gets new-topic volume-scaled velocity on both
    windows and a None median."""
    monkeypatch.setattr(velocity_module, "run_query", lambda *a, **k: _windows_df([]))

    out = compute_velocity_windows_for_today({("ng", "afrobeats"): {"item_count": 15}})

    assert out[("ng", "afrobeats")]["velocity_score_7d"] == round(15 / 75, 4)
    assert out[("ng", "afrobeats")]["velocity_score_30d"] == round(15 / 75, 4)
    assert out[("ng", "afrobeats")]["median_item_count_7d"] is None


def test_windows_null_median_passes_through_as_none(monkeypatch):
    """A NULL median in the row (no short-window history) comes back as None,
    not 0.0, so the lifecycle classifier can tell 'no history' from 'zero'."""
    monkeypatch.setattr(
        velocity_module,
        "run_query",
        lambda *a, **k: _windows_df(
            [
                {
                    "market": "ke",
                    "query_group": "politics",
                    "baseline_avg_short": None,
                    "baseline_avg_long": 30.0,
                    "median_item_count_short": None,
                },
            ]
        ),
    )

    out = compute_velocity_windows_for_today({("ke", "politics"): {"item_count": 60}})

    assert out[("ke", "politics")]["median_item_count_7d"] is None
    # short baseline NULL -> new-topic volume score; 60/75
    assert out[("ke", "politics")]["velocity_score_7d"] == round(60 / 75, 4)


def test_windows_median_returned_as_float(monkeypatch):
    """A real short-window median comes back as a float for lifecycle to use."""
    monkeypatch.setattr(
        velocity_module,
        "run_query",
        lambda *a, **k: _windows_df(
            [
                {
                    "market": "za",
                    "query_group": "news",
                    "baseline_avg_short": 80.0,
                    "baseline_avg_long": 80.0,
                    "median_item_count_short": 74.0,
                },
            ]
        ),
    )

    out = compute_velocity_windows_for_today({("za", "news"): {"item_count": 80}})

    assert out[("za", "news")]["median_item_count_7d"] == 74.0


def test_windows_empty_input_returns_empty(monkeypatch):
    """No input pairs means no query and an empty dict back."""
    mock_run_query = MagicMock()
    monkeypatch.setattr(velocity_module, "run_query", mock_run_query)

    assert compute_velocity_windows_for_today({}) == {}
    assert mock_run_query.call_count == 0


def test_windows_short_long_days_are_parameters(monkeypatch):
    """short_days and long_days reach run_query as bound parameters."""
    captured: dict[str, Any] = {}

    def fake_run_query(sql: str, params: dict | None = None) -> pd.DataFrame:
        captured["params"] = params
        captured["sql"] = sql
        return _windows_df([])

    monkeypatch.setattr(velocity_module, "run_query", fake_run_query)
    compute_velocity_windows_for_today(
        {("za", "news"): {"item_count": 10}}, short_days=7, long_days=30
    )

    assert captured["params"] == {"short_days": 7, "long_days": 30}
    assert "@short_days" in captured["sql"]
    assert "@long_days" in captured["sql"]
