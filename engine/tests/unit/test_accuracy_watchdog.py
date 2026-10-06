"""Accuracy calculations, query availability, report and CLI controls.

Synthetic query stubs exercise the actual check and report functions without
constructing a BigQuery client or calling the network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import scripts.accuracy_watchdog as aw
from scripts.accuracy_watchdog import (
    Check,
    _cross_source_multiplier,
    _direction,
    _verdict,
    check_forecast_beats_persistence,
    composite_match,
    recompute_composite,
    score_prediction_vs_persistence,
)

WEIGHTS = {
    "velocity": 0.20,
    "genz_score": 0.17,
    "watchlist_score": 0.12,
    "engagement": 0.10,
    "slang_score": 0.10,
    "diversity": 0.08,
    "regional_score": 0.08,
    "creator_spread": 0.05,
    "search_velocity_score": 0.05,
    "tone_score": 0.05,
}


def _row(**overrides) -> dict:
    """Default row of zeros; tests set the signals they want to exercise."""
    base = {
        "velocity_score": 0.0,
        "genz_score": 0.0,
        "watchlist_score": 0.0,
        "engagement_score": 0.0,
        "slang_score": 0.0,
        "diversity_score": 0.0,
        "regional_score": 0.0,
        "creator_score": 0.0,
        "search_velocity_score": 0.0,
        "tone_score": 0.0,
        "tone_rows": 0,
    }
    base.update(overrides)
    return base


# --- recompute_composite -----------------------------------------------


def test_recompute_with_tone_uses_nominal_tone_weight():
    row = _row(velocity_score=1.0, tone_score=1.0, tone_rows=5)
    # 1.0*0.20 + 1.0*0.05 = 0.25, no redistribution
    assert recompute_composite(row, WEIGHTS) == 0.25


def test_recompute_without_tone_redistributes_tone_weight():
    # No GDELT tone -> the 0.05 tone slice is redistributed across the 9
    # non-tone signals (multiply by 1/0.95). velocity 1.0 alone =>
    # 0.20 / 0.95 == 0.2105
    row = _row(velocity_score=1.0, tone_rows=0)
    assert recompute_composite(row, WEIGHTS) == pytest.approx(0.2105, abs=1e-4)


def test_recompute_matches_observed_food_nyamachoma_path():
    # Real 7-Jun row that broke a naive audit formula until the redistribution
    # was modelled. The full non-tone weighted sum was 0.1836 with tone_rows=0,
    # so composite (no multiplier) is 0.1836/0.95 = 0.1933; *1.15 = 0.2223 = stored.
    # Drive the whole 0.1836 through velocity alone (0.918 * 0.20 = 0.1836).
    row = _row(velocity_score=0.918, tone_rows=0)
    composite = recompute_composite(row, WEIGHTS)
    assert composite_match(0.2223, composite, 1.15)


def test_recompute_carve_demotes_flash_keeps_sustained():
    """carve=True mirrors the MOMENTUM_IN_COMPOSITE carve: momentum (min of the
    7d and 30d windows) takes 0.07 from velocity. A one-day flash (windows at 0)
    loses 0.07 of velocity with no bonus and drops from 0.25 to 0.18; a sustained
    topic (windows at 1.0) earns it back and stays at 0.25. The no-carve recompute
    is unchanged, so check_composite_integrity (which accepts either) stays correct
    across the flip."""
    weights = dict(WEIGHTS, momentum=0.07)
    flash = _row(
        velocity_score=1.0,
        velocity_score_7d=0.0,
        velocity_score_30d=0.0,
        tone_score=1.0,
        tone_rows=5,
    )
    sustained = _row(
        velocity_score=1.0,
        velocity_score_7d=1.0,
        velocity_score_30d=1.0,
        tone_score=1.0,
        tone_rows=5,
    )
    assert recompute_composite(flash, weights, carve=False) == 0.25
    assert recompute_composite(flash, weights, carve=True) == 0.18
    assert recompute_composite(sustained, weights, carve=True) == 0.25


def test_recompute_all_zero_returns_zero():
    assert recompute_composite(_row(), WEIGHTS) == 0.0


# --- GCAM-reader recompute -------------------------------------------------


def test_recompute_gcam_zero_weight_is_byte_identical():
    """With no gcam weight (WEIGHTS has none, so it defaults 0.0), a row carrying
    gcam signal recomputes exactly like the tone-only path: 1.0*0.20 + 1.0*0.05."""
    row = _row(velocity_score=1.0, tone_score=1.0, tone_rows=5, gcam_score=0.9, gcam_rows=5)
    assert recompute_composite(row, WEIGHTS) == 0.25


def test_recompute_gcam_contributes_when_weighted():
    """Live gcam weight (0.03, carved from tone -> 0.02). A row with gcam present
    scores higher than the same row with gcam absent (its weight redistributed)."""
    weights = dict(WEIGHTS, tone_score=0.02, gcam_score=0.03)
    hot = _row(velocity_score=1.0, tone_score=1.0, tone_rows=5, gcam_score=1.0, gcam_rows=5)
    cold = _row(velocity_score=1.0, tone_score=1.0, tone_rows=5, gcam_score=0.0, gcam_rows=0)
    # hot: 1.0*0.20 + 1.0*0.02 + 1.0*0.03 = 0.25
    assert recompute_composite(hot, weights) == 0.25
    # cold: gcam absent, its 0.03 redistributes: 0.20/0.97 + 1.0*0.02 = 0.2262
    assert recompute_composite(cold, weights) == pytest.approx(0.2262, abs=1e-4)
    assert recompute_composite(hot, weights) > recompute_composite(cold, weights)


def test_recompute_gcam_and_tone_both_absent_redistribute_together():
    """When both GDELT-only signals are absent, both nominal weights redistribute:
    scale = 1/(1 - tone_weight - gcam_weight) = 1/(1 - 0.05 - 0.03) = 1/0.92."""
    weights = dict(WEIGHTS, gcam_score=0.03)  # tone_score stays 0.05
    row = _row(velocity_score=1.0, tone_rows=0, gcam_rows=0)
    assert recompute_composite(row, weights) == pytest.approx(0.20 / 0.92, abs=1e-4)


# --- composite_match ---------------------------------------------------


def test_composite_match_accepts_raw_or_multiplied():
    assert composite_match(0.30, 0.30, 1.15)
    assert composite_match(0.345, 0.30, 1.15)  # 0.30 * 1.15
    assert not composite_match(0.50, 0.30, 1.15)


def test_composite_match_caps_at_one():
    # Composite 0.95 * 1.15 = 1.0925 -> caps at 1.0
    assert composite_match(1.0, 0.95, 1.15)


def test_composite_match_tolerance_absorbs_float_noise():
    # Float wobble inside 1e-4 should not flag drift.
    assert composite_match(0.30005, 0.30, 1.15)


# --- _cross_source_multiplier (exact graded multiplier, 024) -----------

_SCORING_BONUS = {"cross_source_bonus_per_channel": 0.05, "cross_source_max_bonus": 0.15}


def test_cross_source_multiplier_single_channel_no_bonus():
    assert _cross_source_multiplier(1, _SCORING_BONUS) == 1.0
    assert _cross_source_multiplier(0, _SCORING_BONUS) == 1.0


def test_cross_source_multiplier_grades_with_channel_count():
    assert _cross_source_multiplier(2, _SCORING_BONUS) == pytest.approx(1.05)
    assert _cross_source_multiplier(3, _SCORING_BONUS) == pytest.approx(1.10)


def test_cross_source_multiplier_caps_at_max_bonus():
    # 0.05 * (10 - 1) = 0.45, capped to max_bonus 0.15 -> 1.15
    assert _cross_source_multiplier(10, _SCORING_BONUS) == pytest.approx(1.15)


# --- _direction --------------------------------------------------------


def test_direction_band_inclusive_of_steady():
    assert _direction(0.04) == 0  # at band == steady
    assert _direction(-0.04) == 0
    assert _direction(0.041) == 1
    assert _direction(-0.041) == -1
    assert _direction(0.0) == 0


# --- _verdict ----------------------------------------------------------


def test_verdict_all_skipped_returns_skipped():
    checks = [Check("a", "SKIP"), Check("b", "SKIP")]
    assert _verdict(checks) == "SKIPPED"


def test_verdict_composite_drift_trumps_everything():
    checks = [
        Check("composite_integrity", "FAIL"),
        Check("action_pick_in_rising", "PASS"),
        Check("tier_thresholding", "PASS"),
        Check("prediction_degeneracy", "PASS"),
    ]
    assert _verdict(checks) == "DRIFT"


def test_verdict_non_composite_fail_is_degraded():
    checks = [
        Check("composite_integrity", "PASS"),
        Check("action_pick_in_rising", "FAIL"),
        Check("tier_thresholding", "PASS"),
        Check("prediction_degeneracy", "PASS"),
    ]
    assert _verdict(checks) == "DEGRADED"


def test_verdict_all_pass_is_healthy():
    checks = [
        Check(n, "PASS")
        for n in (
            "composite_integrity",
            "action_pick_in_rising",
            "tier_thresholding",
            "prediction_degeneracy",
        )
    ]
    assert _verdict(checks) == "HEALTHY"


# --- score_prediction_vs_persistence -----------------------------------


def _mk(market, qg, day, actual, pred, baseline):
    pred_row = {
        "market": market,
        "query_group": qg,
        "day": day,
        "forecast_value": pred,
        "baseline_value": baseline,
    }
    act_row = {"market": market, "query_group": qg, "day": day, "value": actual}
    return pred_row, act_row


def test_backtest_flags_insufficient_data():
    preds, acts = zip(*[_mk("za", f"t{i}", "d", 0.3, 0.3, 0.3) for i in range(5)], strict=True)
    out = score_prediction_vs_persistence(list(preds), list(acts))
    assert out["status"] == "INSUFFICIENT_DATA"


def test_backtest_predictor_beats_baseline_passes():
    # Build 40 rows where the predictor is closer to actual than persistence,
    # with realistic directional moves to push precision >= 50%.
    preds, acts = [], []
    for i in range(40):
        actual = 0.40 if i % 2 == 0 else 0.20
        baseline = 0.30
        predictor = 0.39 if i % 2 == 0 else 0.21  # close to actual
        p, a = _mk("za", f"t{i}", "d", actual, predictor, baseline)
        preds.append(p)
        acts.append(a)
    out = score_prediction_vs_persistence(preds, acts)
    assert out["status"] == "PASS"
    assert out["mae_predictor"] < out["mae_persistence"]
    assert out["directional_precision"] >= 0.50


def test_backtest_predictor_loses_to_baseline_fails():
    # The actual ARIMA failure shape: predictor is FARTHER from actual than
    # the persistence baseline. Should FAIL.
    preds, acts = [], []
    for i in range(40):
        actual = 0.50 if i % 2 == 0 else 0.10
        baseline = 0.30
        # Predictor points the WRONG way (the ARIMA failure): farther from
        # actual than the do-nothing baseline AND wrong direction.
        predictor = 0.10 if i % 2 == 0 else 0.50
        p, a = _mk("za", f"t{i}", "d", actual, predictor, baseline)
        preds.append(p)
        acts.append(a)
    out = score_prediction_vs_persistence(preds, acts)
    assert out["status"] == "FAIL"
    assert out["mae_predictor"] > out["mae_persistence"]


# --- check_forecast_beats_persistence ----------------------------------
# These mock _bq_client + _query so NO real BigQuery client is constructed.
# That keeps them credential-free: a test that builds a live client passes
# locally with ADC but fails CI with DefaultCredentialsError.


def _archive_row(market, qg, forecast_day, forecast_score, latest_actual, realised):
    # Shape returned by the predictions_archive JOIN trend_scores query.
    return {
        "run_date": "2026-06-01",
        "forecast_day": forecast_day,
        "market": market,
        "query_group": qg,
        "forecast_score": forecast_score,
        "latest_actual": latest_actual,
        "realised_actual": realised,
    }


def _patch_bq(monkeypatch, *, rows=None, raises=None):
    """Stub _bq_client (so no client is built) and _query (so no RPC runs)."""
    monkeypatch.setattr(aw, "_bq_client", lambda: (_FakeBQ(), None))

    def _fake_query(sql, params=None):
        if raises is not None:
            raise raises
        return rows or []

    monkeypatch.setattr(aw, "_query", _fake_query)


class _FakeBQ:
    """Minimal stand-in for the bigquery module: only ScalarQueryParameter."""

    @staticmethod
    def ScalarQueryParameter(name, type_, value):
        return (name, type_, value)


def test_forecast_check_skips_on_empty_archive(monkeypatch):
    # Normal state while the forecast is dropped: no archived rows -> SKIP.
    _patch_bq(monkeypatch, rows=[])
    c = check_forecast_beats_persistence("2026-06-09")
    assert c.status == "SKIP"
    assert "empty" in c.detail.lower()


def test_forecast_check_skips_when_archive_missing(monkeypatch):
    # Table not yet created (DDL unapplied) -> _query raises -> SKIP, not FAIL.
    _patch_bq(monkeypatch, raises=RuntimeError("Not found: Table predictions_archive"))
    c = check_forecast_beats_persistence("2026-06-09")
    assert c.status == "SKIP"
    assert "unavailable" in c.detail.lower()


def test_forecast_check_skips_on_insufficient_rows(monkeypatch):
    # Below the 30-row backtest floor (one week only) -> SKIP, not a verdict.
    rows = [
        _archive_row("za", f"t{i}", f"2026-06-0{1 + (i % 9)}", 0.30, 0.30, 0.30) for i in range(10)
    ]
    _patch_bq(monkeypatch, rows=rows)
    c = check_forecast_beats_persistence("2026-06-09")
    assert c.status == "SKIP"
    assert "matched rows" in c.detail


def test_forecast_check_passes_when_forecast_beats_persistence(monkeypatch):
    # 4 weeks of rows where the forecast is closer to the realised actual than
    # the persistence baseline, with real directional moves -> PASS.
    rows = []
    for i in range(100):
        forecast_day = f"2026-06-{(i % 28) + 1:02d}"
        realised = 0.40 if i % 2 == 0 else 0.20
        baseline = 0.30
        forecast = 0.39 if i % 2 == 0 else 0.21
        rows.append(_archive_row("za", f"t{i}", forecast_day, forecast, baseline, realised))
    _patch_bq(monkeypatch, rows=rows)
    c = check_forecast_beats_persistence("2026-06-09")
    assert c.status == "PASS"
    assert c.metric["mae_predictor"] < c.metric["mae_persistence"]


def test_forecast_check_fails_when_forecast_loses(monkeypatch):
    # The ARIMA failure shape over 4 weeks: forecast points the wrong way and
    # sits farther from the realised actual than persistence -> FAIL -> the
    # report verdict folds this to DEGRADED via _verdict (asserted below).
    rows = []
    for i in range(100):
        forecast_day = f"2026-06-{(i % 28) + 1:02d}"
        realised = 0.50 if i % 2 == 0 else 0.10
        baseline = 0.30
        forecast = 0.10 if i % 2 == 0 else 0.50
        rows.append(_archive_row("za", f"t{i}", forecast_day, forecast, baseline, realised))
    _patch_bq(monkeypatch, rows=rows)
    c = check_forecast_beats_persistence("2026-06-09")
    assert c.status == "FAIL"
    assert c.metric["mae_predictor"] > c.metric["mae_persistence"]


def test_forecast_fail_maps_to_degraded_verdict():
    # A non-composite FAIL (this new check) must surface as DEGRADED, not DRIFT,
    # and not be ignored. Confirms _verdict already handles the 5th check.
    checks = [
        Check("composite_integrity", "PASS"),
        Check("action_pick_in_rising", "PASS"),
        Check("tier_thresholding", "PASS"),
        Check("prediction_degeneracy", "PASS"),
        Check("forecast_beats_persistence", "FAIL"),
    ]
    assert _verdict(checks) == "DEGRADED"


def test_forecast_skip_does_not_raise_verdict():
    # While the forecast is dropped the check SKIPs; the day stays HEALTHY.
    checks = [
        Check("composite_integrity", "PASS"),
        Check("action_pick_in_rising", "PASS"),
        Check("tier_thresholding", "PASS"),
        Check("prediction_degeneracy", "SKIP"),
        Check("forecast_beats_persistence", "SKIP"),
    ]
    assert _verdict(checks) == "HEALTHY"


# ---------------------------------------------------------------------------
# check_dead_signals (15-Jun search_velocity dead-but-weighted regression)
# ---------------------------------------------------------------------------

_FULL_WEIGHTS = {
    "weights": {
        "velocity": 0.20,
        "genz_score": 0.17,
        "watchlist_score": 0.12,
        "engagement": 0.10,
        "slang_score": 0.10,
        "diversity": 0.08,
        "regional_score": 0.08,
        "creator_spread": 0.05,
        "search_velocity_score": 0.05,
        "tone_score": 0.05,
    }
}


def _max_row(n=50, **overrides):
    row = {
        "n": n,
        "velocity_score": 0.3,
        "genz_score": 0.2,
        "watchlist_score": 0.1,
        "engagement_score": 0.4,
        "slang_score": 0.2,
        "diversity_score": 0.3,
        "regional_score": 0.2,
        "creator_score": 0.1,
        "search_velocity_score": 0.5,
    }
    row.update(overrides)
    return row


def test_dead_signals_flags_a_weighted_signal_zero_everywhere(monkeypatch):
    _patch_bq(monkeypatch, rows=[_max_row(search_velocity_score=0.0)])
    check = aw.check_dead_signals("2026-06-12", _FULL_WEIGHTS)
    assert check.status == "FAIL"
    assert "search_velocity_score" in check.detail


def test_dead_signals_passes_when_all_weighted_signals_live(monkeypatch):
    _patch_bq(monkeypatch, rows=[_max_row()])
    assert aw.check_dead_signals("2026-06-12", _FULL_WEIGHTS).status == "PASS"


def test_dead_signals_skips_when_no_scored_rows(monkeypatch):
    _patch_bq(monkeypatch, rows=[_max_row(n=0)])
    assert aw.check_dead_signals("2026-06-12", _FULL_WEIGHTS).status == "SKIP"


def test_dead_signals_ignores_zero_weight_dormant_signal(monkeypatch):
    """A signal intentionally dormant (weight 0) is excluded even at zero."""
    weights = {"weights": dict(_FULL_WEIGHTS["weights"], search_velocity_score=0.0)}
    _patch_bq(monkeypatch, rows=[_max_row(search_velocity_score=0.0)])
    assert aw.check_dead_signals("2026-06-12", weights).status == "PASS"


def test_dead_signals_drives_verdict_to_degraded():
    checks = [
        aw.Check("composite_integrity", "PASS"),
        aw.Check("dead_signals", "FAIL", "search_velocity_score zero"),
    ]
    assert aw._verdict(checks) == "DEGRADED"


# ---------------------------------------------------------------------------
# PULSE Intelligence Core trust checks (shadow tables; SKIP when empty/absent)
# ---------------------------------------------------------------------------

_SCORING_CORROB = {"corroboration": {"corroborated_floor": 0.40}}


# --- check_no_social_only_verified -----------------------------------------


def test_social_only_verified_fails(monkeypatch):
    # A row reached 'corroborated' with factual_corroboration 0 -> FAIL.
    _patch_bq(monkeypatch, rows=[{"tiered": 12, "social_only": 1, "n": 40}])
    c = aw.check_no_social_only_verified("2026-06-26")
    assert c.status == "FAIL"
    assert "social" in c.detail.lower()


def test_social_only_verified_passes_when_none(monkeypatch):
    _patch_bq(monkeypatch, rows=[{"tiered": 12, "social_only": 0, "n": 40}])
    assert aw.check_no_social_only_verified("2026-06-26").status == "PASS"


def test_social_only_verified_skips_when_no_tiers_yet(monkeypatch):
    # Shadow day: no row carries a confidence_tier -> SKIP, not FAIL.
    _patch_bq(monkeypatch, rows=[{"tiered": 0, "social_only": 0, "n": 40}])
    assert aw.check_no_social_only_verified("2026-06-26").status == "SKIP"


def test_social_only_verified_skips_when_column_absent(monkeypatch):
    # Column not yet on the table (DDL unapplied) -> query raises -> SKIP.
    _patch_bq(monkeypatch, raises=RuntimeError("Unrecognized name: confidence_tier"))
    assert aw.check_no_social_only_verified("2026-06-26").status == "SKIP"


# --- check_corroborated_rate_band -------------------------------------------


def test_corroborated_rate_band_fails_on_over_award(monkeypatch):
    # 94.5%-style over-award, the exact 0.40-floor failure mode -> FAIL.
    _patch_bq(monkeypatch, rows=[{"tiered": 200, "corroborated": 189}])
    c = aw.check_corroborated_rate_band("2026-07-03")
    assert c.status == "FAIL"
    assert "exceeds 60%" in c.detail


def test_corroborated_rate_band_fails_when_dead(monkeypatch):
    # Below 5% means the tier is no longer surfacing anything -> FAIL.
    _patch_bq(monkeypatch, rows=[{"tiered": 200, "corroborated": 2}])
    c = aw.check_corroborated_rate_band("2026-07-03")
    assert c.status == "FAIL"
    assert "below 5%" in c.detail


def test_corroborated_rate_band_passes_in_healthy_band(monkeypatch):
    # 27.4%, the observed rate at the recalibrated 0.85 floor -> PASS.
    _patch_bq(monkeypatch, rows=[{"tiered": 201, "corroborated": 55}])
    assert aw.check_corroborated_rate_band("2026-07-03").status == "PASS"


def test_corroborated_rate_band_skips_when_no_tiers_yet(monkeypatch):
    _patch_bq(monkeypatch, rows=[{"tiered": 0, "corroborated": 0}])
    assert aw.check_corroborated_rate_band("2026-07-03").status == "SKIP"


def test_corroborated_rate_band_skips_when_column_absent(monkeypatch):
    _patch_bq(monkeypatch, raises=RuntimeError("Unrecognized name: confidence_tier"))
    assert aw.check_corroborated_rate_band("2026-07-03").status == "SKIP"


# --- check_corroboration_recompute -----------------------------------------


def test_corroboration_recompute_consistent_tiers_pass(monkeypatch):
    rows = [
        {
            "market": "za",
            "query_group": "t1",
            "confidence_tier": "corroborated",
            "factual_corroboration": 0.55,
            "social_corroboration": 0.30,
            "n_factual": 2,
            "n_social": 1,
        },
        {
            "market": "ng",
            "query_group": "t2",
            "confidence_tier": "social_only",
            "factual_corroboration": 0.0,
            "social_corroboration": 0.50,
            "n_factual": 0,
            "n_social": 2,
        },
    ]
    _patch_bq(monkeypatch, rows=rows)
    assert aw.check_corroboration_recompute("2026-06-26", _SCORING_CORROB).status == "PASS"


def test_corroboration_recompute_corroborated_below_floor_fails(monkeypatch):
    # Tier says corroborated but factual is below the 0.40 floor -> contradiction.
    rows = [
        {
            "market": "za",
            "query_group": "t1",
            "confidence_tier": "corroborated",
            "factual_corroboration": 0.20,
            "social_corroboration": 0.40,
        }
    ]
    _patch_bq(monkeypatch, rows=rows)
    c = aw.check_corroboration_recompute("2026-06-26", _SCORING_CORROB)
    assert c.status == "FAIL"
    assert "corroborated" in c.detail


def test_corroboration_recompute_exact_mode_flags_wrong_underconfident_tier(monkeypatch):
    rows = [
        {
            "market": "za",
            "query_group": "t1",
            "confidence_tier": "single-source-factual",
            "factual_corroboration": 0.49,
            "social_corroboration": 0.25,
            "n_factual": 1,
            "n_social": 1,
        }
    ]
    _patch_bq(monkeypatch, rows=rows)
    c = aw.check_corroboration_recompute("2026-06-26", _SCORING_CORROB)
    assert c.status == "FAIL"
    assert "expected=corroborated" in c.detail


def test_corroboration_recompute_partial_mode_ignores_underconfident_tier(monkeypatch):
    rows = [
        {
            "market": "za",
            "query_group": "t1",
            "confidence_tier": "single-source-factual",
            "factual_corroboration": 0.49,
            "social_corroboration": 0.25,
        }
    ]
    calls = {"n": 0}

    def fake_query(_sql, _params=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Unrecognized name: n_factual")
        return rows

    monkeypatch.setattr(aw, "_bq_client", lambda: (_FakeBQ(), None))
    monkeypatch.setattr(aw, "_query", fake_query)
    c = aw.check_corroboration_recompute("2026-06-26", _SCORING_CORROB)
    assert c.status == "PASS"
    assert "conservative floor checks" in c.detail


def test_corroboration_recompute_skips_when_empty(monkeypatch):
    _patch_bq(monkeypatch, rows=[])
    assert aw.check_corroboration_recompute("2026-06-26", _SCORING_CORROB).status == "SKIP"


def test_corroboration_recompute_skips_when_column_absent(monkeypatch):
    _patch_bq(monkeypatch, raises=RuntimeError("Unrecognized name: factual_corroboration"))
    assert aw.check_corroboration_recompute("2026-06-26", _SCORING_CORROB).status == "SKIP"


# --- check_reconcile_receipts_exist ----------------------------------------


def test_reconcile_receipts_present_passes(monkeypatch):
    rows = [
        {
            "market": "za",
            "action": "corroborated",
            "claim_after": "x resolved",
            "matched_entity_key": "x",
            "n_receipts": 2,
        }
    ]
    _patch_bq(monkeypatch, rows=rows)
    assert aw.check_reconcile_receipts_exist("2026-06-26").status == "PASS"


def test_reconcile_receipts_empty_fails(monkeypatch):
    # A correction with a claim_after but zero receipt_ids -> FAIL (never-invent).
    rows = [
        {
            "market": "ng",
            "action": "stale",
            "claim_after": "y is over",
            "matched_entity_key": "y",
            "n_receipts": 0,
        }
    ]
    _patch_bq(monkeypatch, rows=rows)
    c = aw.check_reconcile_receipts_exist("2026-06-26")
    assert c.status == "FAIL"
    assert "receipt" in c.detail.lower()


def test_reconcile_receipts_skips_when_no_corrections(monkeypatch):
    _patch_bq(monkeypatch, rows=[])
    assert aw.check_reconcile_receipts_exist("2026-06-26").status == "SKIP"


def test_reconcile_receipts_skips_when_table_absent(monkeypatch):
    _patch_bq(monkeypatch, raises=RuntimeError("Not found: Table reconcile_actions"))
    assert aw.check_reconcile_receipts_exist("2026-06-26").status == "SKIP"


# --- check_no_unlabeled_stale ----------------------------------------------


def test_no_unlabeled_stale_flags_clear_match(monkeypatch):
    # A 'no_match' claim whose text contains a resolved ledger entity -> FAIL.
    rows = [
        {
            "market": "za",
            "claim_before": "bafana bafana win the cup",
            "entity_key": "bafana",
        }
    ]
    _patch_bq(monkeypatch, rows=rows)
    c = aw.check_no_unlabeled_stale("2026-06-26")
    assert c.status == "FAIL"
    assert "bafana" in c.detail


def test_no_unlabeled_stale_skips_when_no_overlap(monkeypatch):
    # The JOIN returns nothing (no overlap, or either table empty) -> SKIP.
    _patch_bq(monkeypatch, rows=[])
    assert aw.check_no_unlabeled_stale("2026-06-26").status == "SKIP"


def test_no_unlabeled_stale_skips_when_table_absent(monkeypatch):
    _patch_bq(monkeypatch, raises=RuntimeError("Not found: Table event_ledger"))
    assert aw.check_no_unlabeled_stale("2026-06-26").status == "SKIP"


# --- UNVERIFIED verdict tier -----------------------------------------------


def test_trust_check_fail_maps_to_unverified():
    checks = [
        Check("composite_integrity", "PASS"),
        Check("action_pick_in_rising", "PASS"),
        Check("no_social_only_verified", "FAIL"),
    ]
    assert _verdict(checks) == "UNVERIFIED"


def test_composite_drift_trumps_unverified():
    # A wrong stored score is more serious than an unsupported claim.
    checks = [
        Check("composite_integrity", "FAIL"),
        Check("no_social_only_verified", "FAIL"),
    ]
    assert _verdict(checks) == "DRIFT"


def test_unverified_trumps_operational_degraded():
    checks = [
        Check("composite_integrity", "PASS"),
        Check("dead_signals", "FAIL"),
        Check("reconcile_receipts_exist", "FAIL"),
    ]
    assert _verdict(checks) == "UNVERIFIED"


def test_all_trust_checks_skip_stays_healthy():
    # Shadow day: every trust check SKIPs, the rest pass -> HEALTHY.
    checks = [
        Check("composite_integrity", "PASS"),
        Check("no_social_only_verified", "SKIP"),
        Check("corroboration_recompute", "SKIP"),
        Check("reconcile_receipts_exist", "SKIP"),
        Check("no_unlabeled_stale", "SKIP"),
    ]
    assert _verdict(checks) == "HEALTHY"


_OPTIONAL_PROBES = (
    ("check_prediction_degeneracy", ()),
    ("check_forecast_beats_persistence", ()),
    ("check_no_social_only_verified", ()),
    ("check_corroborated_rate_band", ()),
    ("check_corroboration_recompute", (_SCORING_CORROB,)),
    ("check_reconcile_receipts_exist", ()),
    ("check_no_unlabeled_stale", ()),
)


@pytest.mark.parametrize("function,args", _OPTIONAL_PROBES)
@pytest.mark.parametrize("error_type", [PermissionError, TimeoutError, FileNotFoundError])
def test_unavailable_probe_is_not_a_healthy_skip(monkeypatch, function, args, error_type):
    private = "synthetic-private-query-and-token"
    _patch_bq(monkeypatch, raises=error_type(private))
    check = getattr(aw, function)("2026-09-06", *args)
    assert check.status == "SKIP"
    assert check.metric == {
        "availability": "unavailable",
        "reason": "query_failed",
        "error_type": error_type.__name__,
    }
    assert "unavailable" in check.detail
    assert private not in check.detail
    assert "off" not in check.detail
    assert "not live" not in check.detail
    assert _verdict([Check("composite_integrity", "PASS"), check]) == "DEGRADED"
    assert _verdict([check]) == "DEGRADED"


@pytest.mark.parametrize("function,args", _OPTIONAL_PROBES)
def test_measured_empty_probe_remains_a_legitimate_skip(monkeypatch, function, args):
    _patch_bq(monkeypatch, rows=[])
    check = getattr(aw, function)("2026-09-06", *args)
    assert check.status == "SKIP"
    assert check.metric.get("availability") not in ("unavailable", "partial")
    assert "predictor off" not in check.detail
    assert _verdict([Check("composite_integrity", "PASS"), check]) == "HEALTHY"
    assert _verdict([check]) == "SKIPPED"


@pytest.mark.parametrize(
    "rows,expected_status,expected_verdict",
    [
        ([], "SKIP", "DEGRADED"),
        (
            [
                {
                    "market": "za",
                    "query_group": "synthetic",
                    "confidence_tier": "thin",
                    "factual_corroboration": 0.0,
                    "social_corroboration": 0.0,
                }
            ],
            "PASS",
            "DEGRADED",
        ),
        (
            [
                {
                    "market": "za",
                    "query_group": "synthetic",
                    "confidence_tier": "corroborated",
                    "factual_corroboration": 0.0,
                    "social_corroboration": 1.0,
                }
            ],
            "FAIL",
            "UNVERIFIED",
        ),
    ],
)
def test_partial_source_projection_keeps_measurement_and_missing_probe_visible(
    monkeypatch, rows, expected_status, expected_verdict
):
    monkeypatch.setattr(aw, "_bq_client", lambda: (_FakeBQ(), None))
    calls = []

    def query(sql, params=None):
        calls.append(sql)
        if "n_factual" in sql:
            raise PermissionError("synthetic private primary query")
        return rows

    monkeypatch.setattr(aw, "_query", query)
    check = aw.check_corroboration_recompute("2026-09-06", _SCORING_CORROB)
    assert len(calls) == 2
    assert check.status == expected_status
    assert check.metric["availability"] == "partial"
    assert check.metric["reason"] == "primary_query_failed"
    assert check.metric["error_type"] == "PermissionError"
    assert check.metric["rows"] == len(rows)
    assert "n_factual/n_social absent" not in check.detail
    assert _verdict([check]) == expected_verdict


@pytest.mark.parametrize(
    "measured,expected",
    [
        (
            [Check("composite_integrity", "FAIL"), Check("reconcile_receipts_exist", "FAIL")],
            "DRIFT",
        ),
        (
            [Check("composite_integrity", "PASS"), Check("reconcile_receipts_exist", "FAIL")],
            "UNVERIFIED",
        ),
        ([Check("composite_integrity", "PASS"), Check("tier_thresholding", "FAIL")], "DEGRADED"),
    ],
)
def test_measured_failure_precedence_survives_unavailable_probes(measured, expected):
    unavailable = Check(
        "prediction_degeneracy",
        "SKIP",
        metric={"availability": "unavailable", "reason": "query_failed"},
    )
    assert _verdict([unavailable, *measured]) == expected
    assert _verdict([*measured, unavailable]) == expected


def test_probe_availability_uses_existing_public_report_fields(monkeypatch):
    _patch_bq(monkeypatch, raises=PermissionError("synthetic-private-token"))
    check = aw.check_reconcile_receipts_exist("2026-09-06")
    report = aw.AccuracyReport("2026-09-06", _verdict([check]), [check])
    data = report.to_dict()
    assert set(data) == {"trend_date", "verdict", "checks"}
    assert set(data["checks"][0]) == {"name", "status", "detail", "metric"}
    assert data["verdict"] == "DEGRADED"
    assert data["checks"][0]["status"] == "SKIP"
    text = aw.render_text(report)
    assert "VERDICT: DEGRADED" in text
    assert "unavailable" in text
    assert "synthetic-private-token" not in json.dumps(data)
    assert "synthetic-private-token" not in text


def test_actual_report_and_json_cli_expose_failed_optional_probe(monkeypatch, capsys):
    monkeypatch.setattr(aw, "_bq_client", lambda: (_FakeBQ(), None))
    monkeypatch.setattr(aw, "_load_scoring", lambda: {})
    observed = []

    def query(sql, params=None):
        observed.append(sql)
        if "predictions_archive" in sql:
            raise PermissionError("synthetic-private-query")
        return []

    monkeypatch.setattr(aw, "_query", query)
    monkeypatch.setattr(sys, "argv", ["accuracy_watchdog.py", "--date", "2026-09-06", "--json"])
    assert aw.main() == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["verdict"] == "DEGRADED"
    assert len(report["checks"]) == 11
    unavailable = [
        check for check in report["checks"] if check["metric"].get("availability") == "unavailable"
    ]
    assert [check["name"] for check in unavailable] == ["forecast_beats_persistence"]
    assert "synthetic-private-query" not in captured.out
    assert any("predictions_archive" in sql for sql in observed)


@pytest.mark.parametrize("boundary", ["client_setup", "required_query"])
def test_cli_incomplete_report_is_not_healthy_and_keeps_error_text_private(
    monkeypatch, capsys, boundary
):
    error = PermissionError("synthetic-private-token-and-query")
    _patch_bq(monkeypatch, raises=error)
    monkeypatch.setattr(aw, "_load_scoring", lambda: {})
    monkeypatch.setattr(sys, "argv", ["accuracy_watchdog.py", "--date", "2026-09-06", "--json"])
    if boundary == "client_setup":

        def denied_client():
            raise error

        monkeypatch.setattr(aw, "_bq_client", denied_client)
    assert aw.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "synthetic-private-token-and-query" not in captured.err
    assert "report_not_completed" in captured.err
    assert "PermissionError" in captured.err
