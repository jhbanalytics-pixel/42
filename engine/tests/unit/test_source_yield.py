from __future__ import annotations

import importlib
import importlib.util

import pytest


def source_module():
    name = "src.analysis.open_intelligence.source_yield"
    assert importlib.util.find_spec(name) is not None, "source yield module is missing"
    return importlib.import_module(name)


def observation(module, endpoint_id="endpoint_a", **overrides):
    values = {
        "status": "active",
        "calls": 10,
        "confirmed_credits": 20,
        "usable_receipts": 40,
        "unique_receipts": 20,
        "integrity": 0.9,
        "geo_precision": 0.8,
        "median_lead_hours": 6.0,
        "false_discovery_rate": 0.1,
        "outcome_contribution": 0.7,
        "kill_test_passed": True,
    }
    values.update(overrides)
    return module.SourcePerformanceObservation(endpoint_id=endpoint_id, **values)


def rules(module, **overrides):
    values = {
        "efficiency_weight": 0.2,
        "unique_lift_weight": 0.1,
        "integrity_weight": 0.2,
        "geo_precision_weight": 0.15,
        "lead_time_weight": 0.1,
        "precision_weight": 0.15,
        "outcome_weight": 0.1,
        "receipts_per_credit_cap": 2.0,
        "maximum_lead_hours": 24.0,
        "minimum_integrity": 0.7,
        "minimum_geo_precision": 0.6,
        "maximum_false_discovery_rate": 0.2,
    }
    values.update(overrides)
    return module.SourceYieldRules(**values)


def envelope(module, **overrides):
    values = {
        "funding_math_status": "complete",
        "funded_increase_observed": True,
        "funded_increase_amount": 3000,
        "optional_funded_credits_used": 1000,
        "balance": 5000,
        "baseline_credits_per_day": 100.0,
        "runway_floor_days": 14,
        "monthly_optional_credit_cap": 2500,
        "monthly_optional_credits_used": 500,
    }
    values.update(overrides)
    return module.CreditEnvelope(**values)


def test_inventory_route_is_unmeasured_not_zero_value():
    module = source_module()
    item = observation(
        module,
        status="inventory_only",
        calls=None,
        confirmed_credits=None,
        usable_receipts=None,
        unique_receipts=None,
        integrity=None,
        geo_precision=None,
        median_lead_hours=None,
        false_discovery_rate=None,
        outcome_contribution=None,
        kill_test_passed=None,
    )

    result = module.evaluate_source_yield(item, rules(module))

    assert result.state == "unmeasured"
    assert result.score is None
    assert result.usable_receipts_per_credit is None
    assert result.eligible_for_optional is False
    assert result.reasons == ("inventory_only",)


def test_measured_route_score_uses_auditable_components():
    module = source_module()

    result = module.evaluate_source_yield(observation(module), rules(module))

    assert result.state == "eligible"
    assert result.usable_receipts_per_credit == pytest.approx(2.0)
    assert result.unique_receipts_per_credit == pytest.approx(1.0)
    assert result.unique_lift == pytest.approx(0.5)
    assert result.lead_time_score == pytest.approx(0.75)
    assert result.precision == pytest.approx(0.9)
    assert result.score == pytest.approx(0.73)
    assert result.eligible_for_optional is True
    assert result.reasons == ()


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"integrity": 0.6}, "integrity_below_floor"),
        ({"geo_precision": 0.5}, "geo_precision_below_floor"),
        ({"false_discovery_rate": 0.3}, "false_discovery_above_ceiling"),
        ({"kill_test_passed": False}, "kill_test_failed"),
    ],
)
def test_quality_and_kill_gates_block_optional_allocation(overrides, reason):
    module = source_module()

    result = module.evaluate_source_yield(observation(module, **overrides), rules(module))

    assert result.state == "ineligible"
    assert result.eligible_for_optional is False
    assert reason in result.reasons


def test_zero_confirmed_credits_and_partial_metrics_fail_closed():
    module = source_module()

    zero = module.evaluate_source_yield(
        observation(module, confirmed_credits=0),
        rules(module),
    )
    partial = module.evaluate_source_yield(
        observation(module, outcome_contribution=None),
        rules(module),
    )

    assert zero.state == "unmeasured"
    assert zero.reasons == ("no_confirmed_credit_spend",)
    assert partial.state == "unmeasured"
    assert partial.reasons == ("incomplete_measurement",)


def test_unknown_funding_or_unobserved_increase_disables_optional_budget():
    module = source_module()

    assert (
        module.available_optional_credits(
            envelope(module, funding_math_status="unknown", funded_increase_observed=False)
        )
        == 0
    )
    assert module.available_optional_credits(envelope(module, funded_increase_observed=False)) == 0
    assert (
        module.available_optional_credits(
            envelope(
                module,
                funded_increase_amount=1,
                optional_funded_credits_used=0,
            )
        )
        == 1
    )


def test_optional_budget_respects_monthly_cap_and_runway_floor():
    module = source_module()

    assert module.available_optional_credits(envelope(module)) == 2000
    assert module.available_optional_credits(envelope(module, balance=2000)) == 600
    assert (
        module.available_optional_credits(envelope(module, monthly_optional_credits_used=2500)) == 0
    )


def evaluation(module, endpoint_id, score, *, eligible=True):
    return module.SourceYieldEvaluation(
        endpoint_id=endpoint_id,
        state="eligible" if eligible else "ineligible",
        score=score,
        usable_receipts_per_credit=1.0,
        unique_receipts_per_credit=0.5,
        unique_lift=0.5,
        lead_time_score=0.5,
        precision=0.9,
        eligible_for_optional=eligible,
        kill_test_passed=eligible,
        reasons=() if eligible else ("blocked",),
    )


def test_allocator_is_weighted_capped_deterministic_and_budget_conserving():
    module = source_module()
    items = (
        evaluation(module, "endpoint_a", 0.8),
        evaluation(module, "endpoint_b", 0.2),
    )
    budget = envelope(
        module,
        balance=10_000,
        monthly_optional_credit_cap=100,
        monthly_optional_credits_used=0,
    )

    uncapped = module.allocate_optional_credits(
        items,
        budget,
        route_caps={"endpoint_a": 100, "endpoint_b": 100},
    )
    capped = module.allocate_optional_credits(
        items,
        budget,
        route_caps={"endpoint_a": 50, "endpoint_b": 100},
    )

    assert [(item.endpoint_id, item.credits) for item in uncapped] == [
        ("endpoint_a", 80),
        ("endpoint_b", 20),
    ]
    assert [(item.endpoint_id, item.credits) for item in capped] == [
        ("endpoint_a", 50),
        ("endpoint_b", 50),
    ]
    assert sum(item.credits for item in capped) == 100


def test_allocator_excludes_ineligible_uncapped_and_duplicate_routes():
    module = source_module()
    budget = envelope(
        module,
        balance=10_000,
        monthly_optional_credit_cap=10,
        monthly_optional_credits_used=0,
    )
    eligible = evaluation(module, "endpoint_a", 1.0)
    blocked = evaluation(module, "endpoint_b", 1.0, eligible=False)

    result = module.allocate_optional_credits(
        (eligible, blocked),
        budget,
        route_caps={"endpoint_a": 10},
    )
    assert [(item.endpoint_id, item.credits) for item in result] == [("endpoint_a", 10)]

    assert (
        module.allocate_optional_credits(
            (eligible,),
            budget,
            route_caps={},
        )
        == ()
    )
    with pytest.raises(ValueError, match="duplicate source yield endpoint"):
        module.allocate_optional_credits(
            (eligible, eligible),
            budget,
            route_caps={"endpoint_a": 10},
        )


def test_evaluation_rejects_nonfinite_or_inconsistent_state():
    module = source_module()

    with pytest.raises(ValueError, match="source yield score is invalid"):
        evaluation(module, "endpoint_a", float("nan"))
    with pytest.raises(ValueError, match="eligible state and flag disagree"):
        module.SourceYieldEvaluation(
            endpoint_id="endpoint_a",
            state="ineligible",
            score=0.5,
            usable_receipts_per_credit=1.0,
            unique_receipts_per_credit=0.5,
            unique_lift=0.5,
            lead_time_score=0.5,
            precision=0.9,
            eligible_for_optional=True,
            kill_test_passed=False,
            reasons=(),
        )
    with pytest.raises(ValueError, match="eligible evaluations require a passed kill test"):
        module.SourceYieldEvaluation(
            endpoint_id="endpoint_a",
            state="eligible",
            score=0.5,
            usable_receipts_per_credit=1.0,
            unique_receipts_per_credit=0.5,
            unique_lift=0.5,
            lead_time_score=0.5,
            precision=0.9,
            eligible_for_optional=True,
            kill_test_passed=False,
            reasons=(),
        )
    with pytest.raises(ValueError, match="eligible evaluations cannot carry rejection reasons"):
        module.SourceYieldEvaluation(
            endpoint_id="endpoint_a",
            state="eligible",
            score=0.5,
            usable_receipts_per_credit=1.0,
            unique_receipts_per_credit=0.5,
            unique_lift=0.5,
            lead_time_score=0.5,
            precision=0.9,
            eligible_for_optional=True,
            kill_test_passed=True,
            reasons=("kill_test_failed",),
        )


def test_one_credit_tie_breaks_by_endpoint_id():
    module = source_module()
    budget = envelope(
        module,
        balance=10_000,
        monthly_optional_credit_cap=1,
        monthly_optional_credits_used=0,
    )

    result = module.allocate_optional_credits(
        (
            evaluation(module, "endpoint_b", 1.0),
            evaluation(module, "endpoint_a", 1.0),
        ),
        budget,
        route_caps={"endpoint_a": 1, "endpoint_b": 1},
    )

    assert [(item.endpoint_id, item.credits) for item in result] == [("endpoint_a", 1)]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "ineligible"),
        ("eligible_for_optional", 1),
        ("kill_test_passed", False),
        ("reasons", None),
        ("score", float("nan")),
        ("score", "invalid"),
        ("unique_lift", None),
        ("unique_lift", "invalid"),
    ],
)
def test_allocator_fails_closed_on_corrupted_eligible_evaluation(field, value):
    module = source_module()
    item = evaluation(module, "endpoint_a", 1.0)
    object.__setattr__(item, field, value)

    result = module.allocate_optional_credits(
        (item,),
        envelope(module),
        route_caps={"endpoint_a": 10},
    )

    assert result == ()


def test_two_credit_equal_score_remainders_go_to_two_routes():
    module = source_module()
    budget = envelope(
        module,
        balance=10_000,
        monthly_optional_credit_cap=2,
        monthly_optional_credits_used=0,
        funded_increase_amount=2,
        optional_funded_credits_used=0,
    )
    result = module.allocate_optional_credits(
        tuple(
            evaluation(module, endpoint, 1.0)
            for endpoint in ("endpoint_c", "endpoint_a", "endpoint_b")
        ),
        budget,
        route_caps={"endpoint_a": 2, "endpoint_b": 2, "endpoint_c": 2},
    )

    assert [(item.endpoint_id, item.credits) for item in result] == [
        ("endpoint_a", 1),
        ("endpoint_b", 1),
    ]


def test_near_one_weights_normalize_to_bounded_score():
    module = source_module()
    tuned = rules(module, efficiency_weight=0.20000000005)
    perfect = observation(
        module,
        confirmed_credits=10,
        usable_receipts=20,
        unique_receipts=20,
        integrity=1.0,
        geo_precision=1.0,
        median_lead_hours=0.0,
        false_discovery_rate=0.0,
        outcome_contribution=1.0,
    )

    result = module.evaluate_source_yield(perfect, tuned)

    assert result.score == pytest.approx(1.0)


def test_normalized_convex_score_clamps_float_rounding_at_one():
    module = source_module()
    tuned = rules(
        module,
        efficiency_weight=0.21748918003308632,
        unique_lift_weight=0.2677583365323565,
        integrity_weight=0.24652070012261024,
        geo_precision_weight=0.07276430090025861,
        lead_time_weight=0.01019764884452355,
        precision_weight=0.10021355042216643,
        outcome_weight=0.0850562831449985,
    )
    perfect = observation(
        module,
        confirmed_credits=10,
        usable_receipts=20,
        unique_receipts=20,
        integrity=1.0,
        geo_precision=1.0,
        median_lead_hours=0.0,
        false_discovery_rate=0.0,
        outcome_contribution=1.0,
    )

    result = module.evaluate_source_yield(perfect, tuned)

    assert result.score == 1.0


def test_invalid_weight_type_reports_contract_error_before_normalizing():
    module = source_module()

    with pytest.raises(ValueError, match="source yield weights must be finite and sum to one"):
        rules(module, efficiency_weight="invalid")


def test_normalized_duplicate_route_caps_are_rejected():
    module = source_module()
    budget = envelope(module)
    item = evaluation(module, "endpoint_a", 1.0)

    with pytest.raises(ValueError, match="duplicate normalized route cap endpoint"):
        module.allocate_optional_credits(
            (item,),
            budget,
            route_caps={"endpoint_a": 10, " endpoint_a ": 5},
        )


def test_the_score_does_not_depend_on_how_sum_accumulates(monkeypatch):
    """CPython 3.12 gave sum() compensated summation for floats.

    Before that it accumulated left to right, so the same weights normalised by
    a slightly different divisor put a perfect score just under one. The clamp
    only caps the upper end, so it could not lift it back. A score that changes
    with the interpreter's summation strategy is not a score, and this asserts
    the property on every version rather than waiting for the old one to fail.
    """
    module = source_module()

    def left_to_right(values, start=0.0):
        total = start
        for value in values:
            total += value
        return total

    monkeypatch.setattr(module, "sum", left_to_right, raising=False)
    tuned = rules(
        module,
        efficiency_weight=0.21748918003308632,
        unique_lift_weight=0.2677583365323565,
        integrity_weight=0.24652070012261024,
        geo_precision_weight=0.07276430090025861,
        lead_time_weight=0.01019764884452355,
        precision_weight=0.10021355042216643,
        outcome_weight=0.0850562831449985,
    )
    perfect = observation(
        module,
        confirmed_credits=10,
        usable_receipts=20,
        unique_receipts=20,
        integrity=1.0,
        geo_precision=1.0,
        median_lead_hours=0.0,
        false_discovery_rate=0.0,
        outcome_contribution=1.0,
    )

    assert module.evaluate_source_yield(perfect, tuned).score == 1.0


def test_the_score_is_bounded_at_both_ends(monkeypatch):
    """The upper clamp is load bearing. The lower bound is a contract.

    Normalised weights sum to one only to within a rounding step, so a perfect
    observation can land a fraction above one and the upper clamp is what keeps
    it in range. The lower bound holds by construction instead: every component
    is bounded at or above zero before it is weighted, and every weight is
    non-negative, so no input reaches the lower clamp. It is asserted here as
    the range a score is read on, not as a claim that the clamp fires.
    """
    module = source_module()
    tuned = rules(
        module,
        efficiency_weight=0.21748918003308632,
        unique_lift_weight=0.2677583365323565,
        integrity_weight=0.24652070012261024,
        geo_precision_weight=0.07276430090025861,
        lead_time_weight=0.01019764884452355,
        precision_weight=0.10021355042216643,
        outcome_weight=0.0850562831449985,
    )
    perfect = observation(
        module,
        confirmed_credits=10,
        usable_receipts=20,
        unique_receipts=20,
        integrity=1.0,
        geo_precision=1.0,
        median_lead_hours=0.0,
        false_discovery_rate=0.0,
        outcome_contribution=1.0,
    )
    assert module.evaluate_source_yield(perfect, tuned).score <= 1.0

    worst = observation(
        module,
        confirmed_credits=10,
        usable_receipts=20,
        unique_receipts=0,
        integrity=0.0,
        geo_precision=0.0,
        median_lead_hours=10_000.0,
        false_discovery_rate=1.0,
        outcome_contribution=0.0,
    )
    assert module.evaluate_source_yield(worst, tuned).score >= 0.0
