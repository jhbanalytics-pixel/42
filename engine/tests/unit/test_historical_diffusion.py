"""Boundary tests for future-bounded cross-market diffusion forecasts."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
from dataclasses import asdict
from datetime import date, datetime

import pytest

SIGNAL_ID = "sig_" + "a" * 64


def diffusion_module():
    name = "src.analysis.open_intelligence.historical_diffusion"
    assert importlib.util.find_spec(name) is not None, "historical diffusion module is missing"
    return importlib.import_module(name)


def current(module, **overrides):
    values = {
        "signal_id": SIGNAL_ID,
        "origin_market": "za",
        "origin_date": date(2026, 8, 25),
        "as_of": date(2026, 8, 27),
        "carrier_markets": ("za",),
        "source_families": ("reddit", "youtube"),
    }
    values.update(overrides)
    return module.CurrentDiffusionSignal(**values)


def sequence(module, index, lag_days, **overrides):
    origin_date = date(2026, 8, 1 + index * 3)
    values = {
        "sequence_id": f"sequence_{index:03d}",
        "origin_market": "za",
        "origin_date": origin_date,
        "target_market": "ng",
        "target_date": origin_date.replace(day=origin_date.day + lag_days),
        "receipt_ids": (f"ev_{index:064x}",),
    }
    values.update(overrides)
    return module.HistoricalDiffusionSequence(**values)


def rules(module, **overrides):
    values = {
        "minimum_comparable_sequences": 3,
        "maximum_absolute_deviation_days": 2.0,
        "rule_version": "diffusion_rules_v1",
    }
    values.update(overrides)
    return module.DiffusionRules(**values)


def forecast(module, sequences, **overrides):
    values = {
        "current": current(module),
        "historical_sequences": sequences,
        "target_market": "ng",
        "rules": rules(module),
    }
    values.update(overrides)
    return module.forecast_cross_market_diffusion(**values)


def test_three_bounded_sequences_produce_dated_empirical_forecast():
    module = diffusion_module()
    sequences = tuple(sequence(module, index, lag) for index, lag in enumerate((4, 5, 6), 1))

    result = forecast(module, sequences)

    assert result == module.DiffusionForecast(
        state="forecast",
        reason="bounded_history",
        signal_id=SIGNAL_ID,
        as_of=date(2026, 8, 27),
        origin_market="za",
        origin_date=date(2026, 8, 25),
        current_carrier_markets=("za",),
        current_source_families=("reddit", "youtube"),
        historical_analogue_count=3,
        median_lag_days=5.0,
        lag_interval_days=(4, 6),
        maximum_absolute_deviation_days=1.0,
        target_market="ng",
        forecast_date=date(2026, 8, 30),
        invalidation_date=date(2026, 8, 31),
        confidence="bounded",
        receipt_ids=(f"ev_{1:064x}", f"ev_{2:064x}", f"ev_{3:064x}"),
        rule_version="diffusion_rules_v1",
        display_eligible=False,
    )


def test_fewer_than_three_exact_route_sequences_is_insufficient():
    module = diffusion_module()
    sequences = (
        sequence(module, 1, 4),
        sequence(module, 2, 5),
        sequence(module, 3, 6, origin_market="ng", target_market="ke"),
    )

    result = forecast(module, sequences)

    assert result.state == "insufficient_history"
    assert result.reason == "minimum_comparable_sequences_not_met"
    assert result.historical_analogue_count == 2
    assert result.median_lag_days is None
    assert result.forecast_date is None
    assert result.confidence == "none"


def test_unbounded_lag_dispersion_blocks_forecast():
    module = diffusion_module()
    sequences = tuple(sequence(module, index, lag) for index, lag in enumerate((5, 5, 15), 1))

    result = forecast(
        module,
        sequences,
        current=current(
            module,
            origin_date=date(2026, 9, 1),
            as_of=date(2026, 9, 2),
        ),
    )

    assert result.state == "unbounded_dispersion"
    assert result.reason == "maximum_absolute_deviation_above_ceiling"
    assert result.historical_analogue_count == 3
    assert result.median_lag_days == 5.0
    assert result.lag_interval_days == (5, 15)
    assert result.maximum_absolute_deviation_days == 10.0
    assert result.forecast_date is None
    assert result.confidence == "none"


def test_target_already_carried_returns_observed_without_history():
    module = diffusion_module()

    result = forecast(
        module,
        (),
        current=current(module, carrier_markets=("ng", "za")),
    )

    assert result.state == "already_observed"
    assert result.reason == "target_market_is_current_carrier"
    assert result.current_carrier_markets == ("ng", "za")
    assert result.historical_analogue_count == 0
    assert result.forecast_date is None


def test_elapsed_upper_lag_bound_cannot_remain_a_forecast():
    module = diffusion_module()
    sequences = tuple(sequence(module, index, lag) for index, lag in enumerate((4, 5, 6), 1))

    result = forecast(
        module,
        sequences,
        current=current(module, as_of=date(2026, 9, 2)),
    )

    assert result.state == "invalidation_elapsed"
    assert result.reason == "upper_lag_bound_elapsed"
    assert result.forecast_date == date(2026, 8, 30)
    assert result.invalidation_date == date(2026, 8, 31)
    assert result.confidence == "none"


def test_sequences_must_be_complete_before_current_origin_to_prevent_future_leakage():
    module = diffusion_module()
    leaking = sequence(
        module,
        1,
        4,
        origin_date=date(2026, 8, 22),
        target_date=date(2026, 8, 26),
    )

    with pytest.raises(ValueError, match="complete before current origin"):
        forecast(module, (leaking,))


def test_unrelated_future_sequence_does_not_poison_the_exact_route():
    module = diffusion_module()
    exact = tuple(sequence(module, index, lag) for index, lag in enumerate((4, 5, 6), 1))
    unrelated_future = sequence(
        module,
        9,
        2,
        origin_market="ng",
        origin_date=date(2026, 8, 25),
        target_market="ke",
        target_date=date(2026, 8, 27),
    )

    result = forecast(module, (*exact, unrelated_future))

    assert result.state == "forecast"
    assert result.historical_analogue_count == 3


def test_unrelated_malformed_mapping_is_ignored_after_route_identity_is_read():
    module = diffusion_module()
    exact = tuple(sequence(module, index, lag) for index, lag in enumerate((4, 5, 6), 1))
    unrelated = {"origin_market": "ke", "target_market": "za", "broken": True}

    result = forecast(module, (*exact, unrelated))

    assert result.state == "forecast"
    assert result.historical_analogue_count == 3


def test_unrelated_malformed_object_is_ignored_but_comparable_object_fails_closed():
    module = diffusion_module()
    exact = tuple(sequence(module, index, lag) for index, lag in enumerate((4, 5, 6), 1))

    class Malformed:
        def __init__(self, origin_market, target_market):
            self.origin_market = origin_market
            self.target_market = target_market

    result = forecast(module, (*exact, Malformed("ke", "za")))

    assert result.state == "forecast"
    with pytest.raises(ValueError, match="comparable historical sequence"):
        forecast(module, (*exact, Malformed("za", "ng")))


def test_duplicate_sequence_and_receipt_identity_fail_closed():
    module = diffusion_module()
    first = sequence(module, 1, 4)
    duplicate_sequence = sequence(module, 1, 5)
    duplicate_receipt = sequence(module, 2, 5, receipt_ids=first.receipt_ids)

    with pytest.raises(ValueError, match="duplicate historical sequence"):
        forecast(module, (first, duplicate_sequence))
    with pytest.raises(ValueError, match="duplicate historical receipt"):
        forecast(module, (first, duplicate_receipt))


def test_input_order_is_deterministic_and_callers_cannot_enable_display():
    module = diffusion_module()
    sequences = tuple(sequence(module, index, lag) for index, lag in enumerate((4, 5, 6), 1))

    assert forecast(module, sequences) == forecast(module, tuple(reversed(sequences)))
    assert (
        "display_eligible"
        not in inspect.signature(module.forecast_cross_market_diffusion).parameters
    )


def test_invalid_market_dates_rules_and_receipts_are_rejected():
    module = diffusion_module()

    with pytest.raises(ValueError, match="as of"):
        current(module, as_of=date(2026, 8, 24))
    with pytest.raises(ValueError, match="origin date"):
        current(module, origin_date=datetime(2026, 8, 25, 0, 0))
    with pytest.raises(ValueError, match="carrier markets"):
        current(module, carrier_markets=("za", 1))
    with pytest.raises(ValueError, match="different markets"):
        sequence(module, 1, 4, target_market="za")
    with pytest.raises(ValueError, match="target date"):
        sequence(module, 1, 4, target_date=date(2026, 8, 1))
    with pytest.raises(ValueError, match="receipt"):
        sequence(module, 1, 4, receipt_ids=())
    with pytest.raises(ValueError, match="minimum comparable"):
        rules(module, minimum_comparable_sequences=2)
    with pytest.raises(ValueError, match="maximum absolute deviation"):
        rules(module, maximum_absolute_deviation_days=-1)


def test_forecast_value_rejects_internally_inconsistent_display_state():
    module = diffusion_module()

    with pytest.raises(ValueError, match="forecast state requires"):
        module.DiffusionForecast(
            state="forecast",
            reason="bounded_history",
            signal_id=SIGNAL_ID,
            as_of=date(2026, 8, 27),
            origin_market="za",
            origin_date=date(2026, 8, 25),
            current_carrier_markets=("za",),
            current_source_families=("reddit", "youtube"),
            historical_analogue_count=3,
            median_lag_days=None,
            lag_interval_days=None,
            maximum_absolute_deviation_days=None,
            target_market="ng",
            forecast_date=None,
            invalidation_date=None,
            confidence="bounded",
            receipt_ids=(),
            rule_version="diffusion_rules_v1",
            display_eligible=False,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"historical_analogue_count": 0},
        {"median_lag_days": -4.0},
        {"lag_interval_days": (7, 4)},
        {"maximum_absolute_deviation_days": -1.0},
        {"forecast_date": date(2026, 8, 24)},
        {"invalidation_date": date(2026, 8, 29)},
        {"maximum_absolute_deviation_days": 0.5},
        {"forecast_date": date(2026, 8, 29)},
        {"invalidation_date": date(2026, 9, 1)},
    ],
)
def test_forecast_value_rejects_contradictory_bounded_state(overrides):
    module = diffusion_module()
    values = {
        "state": "forecast",
        "reason": "bounded_history",
        "signal_id": SIGNAL_ID,
        "as_of": date(2026, 8, 27),
        "origin_market": "za",
        "origin_date": date(2026, 8, 25),
        "current_carrier_markets": ("za",),
        "current_source_families": ("reddit", "youtube"),
        "historical_analogue_count": 3,
        "median_lag_days": 5.0,
        "lag_interval_days": (4, 6),
        "maximum_absolute_deviation_days": 1.0,
        "target_market": "ng",
        "forecast_date": date(2026, 8, 30),
        "invalidation_date": date(2026, 8, 31),
        "confidence": "bounded",
        "receipt_ids": (f"ev_{1:064x}",),
        "rule_version": "diffusion_rules_v1",
        "display_eligible": False,
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        module.DiffusionForecast(**values)


def test_blocked_state_cannot_claim_bounded_confidence():
    module = diffusion_module()
    result = forecast(module, (sequence(module, 1, 4),))

    with pytest.raises(ValueError, match="confidence"):
        module.DiffusionForecast(**{**asdict(result), "confidence": "bounded"})


def test_even_median_rounds_up_and_upper_bound_expires_only_after_its_date():
    module = diffusion_module()
    sequences = tuple(sequence(module, index, lag) for index, lag in enumerate((4, 5, 6, 7), 1))

    on_upper_bound = forecast(
        module,
        sequences,
        current=current(module, as_of=date(2026, 9, 1)),
    )
    after_upper_bound = forecast(
        module,
        sequences,
        current=current(module, as_of=date(2026, 9, 2)),
    )

    assert on_upper_bound.median_lag_days == 5.5
    assert on_upper_bound.forecast_date == date(2026, 8, 31)
    assert on_upper_bound.invalidation_date == date(2026, 9, 1)
    assert on_upper_bound.state == "forecast"
    assert after_upper_bound.state == "invalidation_elapsed"
