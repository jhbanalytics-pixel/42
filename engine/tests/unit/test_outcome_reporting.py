"""Full due cohort reporting: unknowns stay in the denominator, no rate is invented."""

from __future__ import annotations

import inspect
import json
from datetime import date

import pytest
from src.analysis.open_intelligence.outcome_reporting import (
    COHORT_METRIC_FIELDS,
    cohort_metrics,
    cohort_metrics_by_source,
    passes_effectiveness_gate,
)


def test_unknown_outcomes_remain_in_the_cohort():
    result = cohort_metrics(
        [
            {"prediction_id": "p1", "is_false_discovery": False},
            {"prediction_id": "p2", "is_false_discovery": None},
        ]
    )
    assert result["unresolved"] == 1
    assert result["fdr_lower"] == 0
    assert result["fdr_upper"] == 0.5


def test_cohort_metrics_reports_every_field_with_exact_denominators() -> None:
    result = cohort_metrics(
        [
            {"prediction_id": "p1", "is_false_discovery": True},
            {"prediction_id": "p2", "is_false_discovery": False},
            {"prediction_id": "p3", "is_false_discovery": None},
            {"prediction_id": "p4", "is_false_discovery": None},
        ]
    )
    assert tuple(result) == COHORT_METRIC_FIELDS
    assert result == {
        "due": 4,
        "resolved": 2,
        "unresolved": 2,
        "conditional_fdr": 0.5,
        "fdr_lower": 0.25,
        "fdr_upper": 0.75,
    }


def test_empty_due_cohort_has_no_rates_and_cannot_pass_the_gate() -> None:
    result = cohort_metrics([])
    assert result == {
        "due": 0,
        "resolved": 0,
        "unresolved": 0,
        "conditional_fdr": None,
        "fdr_lower": None,
        "fdr_upper": None,
    }
    assert passes_effectiveness_gate(result, minimum_due=1) is False
    assert passes_effectiveness_gate(result, minimum_due=10) is False


def test_duplicate_prediction_is_refused_before_any_rate() -> None:
    with pytest.raises(ValueError, match="duplicate_prediction"):
        cohort_metrics(
            [
                {"prediction_id": "p1", "is_false_discovery": False},
                {"prediction_id": "p1", "is_false_discovery": True},
            ]
        )


@pytest.mark.parametrize("flag", [0, 1, "false", "", 0.0, [], {}])
def test_invalid_outcome_flag_is_refused(flag: object) -> None:
    with pytest.raises(ValueError, match="invalid_outcome_flag"):
        cohort_metrics([{"prediction_id": "p1", "is_false_discovery": flag}])


def test_cohort_metrics_by_source_preserves_unknowns_and_unavailable_fdr() -> None:
    result = cohort_metrics_by_source(
        [
            {"prediction_id": "p1", "is_false_discovery": False, "source_attribution": "youtube"},
            {"prediction_id": "p2", "is_false_discovery": None, "source_attribution": "youtube"},
            {"prediction_id": "p3", "is_false_discovery": None, "source_attribution": "reddit"},
        ]
    )
    assert tuple(result) == ("reddit", "youtube")
    assert result["youtube"] == {
        "due": 2,
        "resolved": 1,
        "unresolved": 1,
        "conditional_fdr": 0.0,
        "fdr_lower": 0.0,
        "fdr_upper": 0.5,
    }
    assert result["reddit"]["due"] == 1
    assert result["reddit"]["unresolved"] == 1
    assert result["reddit"]["conditional_fdr"] is None
    assert result["reddit"]["fdr_lower"] == 0.0
    assert result["reddit"]["fdr_upper"] == 1.0
    assert passes_effectiveness_gate(result["reddit"], minimum_due=1) is False


@pytest.mark.parametrize(
    "row",
    [
        {"prediction_id": "p1", "is_false_discovery": False},
        {"prediction_id": "p1", "is_false_discovery": False, "source_attribution": None},
        {"prediction_id": "p1", "is_false_discovery": False, "source_attribution": ""},
        {"prediction_id": "p1", "is_false_discovery": False, "source_attribution": 3},
    ],
)
def test_cohort_metrics_by_source_requires_attribution(row: dict) -> None:
    with pytest.raises(ValueError, match="source_attribution_required"):
        cohort_metrics_by_source([row])


def test_cohort_metrics_by_source_refuses_a_prediction_attributed_twice() -> None:
    with pytest.raises(ValueError, match="duplicate_prediction"):
        cohort_metrics_by_source(
            [
                {
                    "prediction_id": "p1",
                    "is_false_discovery": False,
                    "source_attribution": "youtube",
                },
                {
                    "prediction_id": "p1",
                    "is_false_discovery": False,
                    "source_attribution": "reddit",
                },
            ]
        )


def test_gate_needs_the_minimum_due_and_a_measured_rate() -> None:
    measured = cohort_metrics(
        [
            {"prediction_id": "p1", "is_false_discovery": False},
            {"prediction_id": "p2", "is_false_discovery": None},
        ]
    )
    assert passes_effectiveness_gate(measured, minimum_due=2) is True
    assert passes_effectiveness_gate(measured, minimum_due=3) is False
    unmeasured = cohort_metrics([{"prediction_id": "p1", "is_false_discovery": None}])
    assert unmeasured["conditional_fdr"] is None
    assert passes_effectiveness_gate(unmeasured, minimum_due=1) is False


def test_gate_refuses_foreign_metrics_and_invalid_minimum() -> None:
    assert inspect.signature(passes_effectiveness_gate).parameters["minimum_due"].default is (
        inspect.Parameter.empty
    )
    good = cohort_metrics([{"prediction_id": "p1", "is_false_discovery": False}])
    for minimum in (0, -1, True, 1.5, "1"):
        with pytest.raises(ValueError, match="minimum_due_invalid"):
            passes_effectiveness_gate(good, minimum_due=minimum)
    with pytest.raises(ValueError, match="cohort_metrics_invalid"):
        passes_effectiveness_gate({"due": 5}, minimum_due=1)


def outcome_rows():
    base = {
        "client_scope_id": "fixture_scope",
        "market": "za",
        "evaluated_at": "2026-09-08T05:00:00Z",
        "evaluation_date": "2026-09-01",
    }
    return [
        {
            **base,
            "prediction_id": "pred_1",
            "outcome": "sustained",
            "source_families": ["news", "reddit"],
        },
        {
            **base,
            "prediction_id": "pred_2",
            "outcome": "fizzled",
            "source_families": ["reddit", "youtube"],
        },
        {
            **base,
            "prediction_id": "pred_3",
            "outcome": "noise",
            "source_families": ["news", "youtube"],
        },
        {
            **base,
            "prediction_id": "pred_4",
            "outcome": "unresolved",
            "source_families": ["news", "reddit"],
        },
        {
            **base,
            "prediction_id": "pred_5",
            "outcome": "peaked",
            "source_families": ["reddit", "youtube"],
        },
    ]


def test_fizzled_is_not_a_false_discovery_and_unresolved_stays_unknown() -> None:
    from src.analysis.open_intelligence.outcome_reporting import attributed_cohort_rows

    rows = attributed_cohort_rows(outcome_rows())

    flags = {row["prediction_id"]: row["is_false_discovery"] for row in rows}
    assert flags == {
        "pred_1": False,
        "pred_2": False,
        "pred_3": True,
        "pred_4": None,
        "pred_5": False,
    }
    assert {row["source_attribution"] for row in rows} == {
        "news+reddit",
        "reddit+youtube",
        "news+youtube",
    }
    assert rows[0]["source_families"] == ("news", "reddit")
    assert cohort_metrics(rows) == {
        "due": 5,
        "resolved": 4,
        "unresolved": 1,
        "conditional_fdr": 0.25,
        "fdr_lower": 0.2,
        "fdr_upper": 0.4,
    }
    with pytest.raises(ValueError, match="outcome state is invalid"):
        attributed_cohort_rows([{**outcome_rows()[0], "outcome": "won"}])


def test_source_family_cohorts_overlap_and_keep_unavailable_fdr_as_none() -> None:
    from src.analysis.open_intelligence.outcome_reporting import (
        attributed_cohort_rows,
        source_family_metrics,
    )

    rows = attributed_cohort_rows(outcome_rows())
    by_set = cohort_metrics_by_source(rows)
    by_family = source_family_metrics(rows)

    assert set(by_set) == {"news+reddit", "reddit+youtube", "news+youtube"}
    assert by_set["news+reddit"]["due"] == 2
    assert by_set["news+reddit"]["conditional_fdr"] == 0.0
    assert set(by_family) == {"news", "reddit", "youtube"}
    assert by_family["news"] == {
        "due": 3,
        "resolved": 2,
        "unresolved": 1,
        "conditional_fdr": 0.5,
        "fdr_lower": 1 / 3,
        "fdr_upper": 2 / 3,
    }
    assert sum(metrics["due"] for metrics in by_family.values()) > cohort_metrics(rows)["due"]
    unresolved_only = attributed_cohort_rows([outcome_rows()[3]])
    assert source_family_metrics(unresolved_only)["news"]["conditional_fdr"] is None


def test_source_observations_carry_measured_fdr_and_never_zero_for_unavailable() -> None:
    from src.analysis.open_intelligence.outcome_reporting import (
        attributed_cohort_rows,
        source_performance_observations,
    )
    from src.analysis.open_intelligence.source_yield import SourcePerformanceObservation

    measurements = {
        family: {
            "status": "active",
            "calls": 10,
            "confirmed_credits": 20,
            "usable_receipts": 40,
            "unique_receipts": 20,
            "integrity": 0.9,
            "geo_precision": 0.8,
            "median_lead_hours": 6.0,
            "kill_test_passed": True,
        }
        for family in ("news", "reddit", "youtube")
    }
    observations = source_performance_observations(
        attributed_cohort_rows(outcome_rows()), measurements=measurements
    )

    by_id = {item.endpoint_id: item for item in observations}
    assert all(isinstance(item, SourcePerformanceObservation) for item in observations)
    assert by_id["news"].false_discovery_rate == 0.5
    assert by_id["reddit"].false_discovery_rate == 0.0
    assert by_id["news"].outcome_contribution == 1 / 3
    assert by_id["reddit"].outcome_contribution == 1.0
    unresolved = source_performance_observations(
        attributed_cohort_rows([outcome_rows()[3]]), measurements=measurements
    )
    assert {item.endpoint_id for item in unresolved} == {"news", "reddit"}
    assert all(item.false_discovery_rate is None for item in unresolved)
    assert all(item.outcome_contribution is None for item in unresolved)
    with pytest.raises(ValueError, match="source_measurement_missing"):
        source_performance_observations(
            attributed_cohort_rows(outcome_rows()), measurements={"news": measurements["news"]}
        )


def test_allocation_is_a_bounded_proposal_with_spending_disabled_by_construction() -> None:
    from src.analysis.open_intelligence import outcome_reporting
    from src.analysis.open_intelligence.outcome_reporting import (
        AllocationProposal,
        attributed_cohort_rows,
        propose_allocation,
        source_performance_observations,
    )
    from src.analysis.open_intelligence.source_yield import (
        CreditEnvelope,
        SourceYieldRules,
        available_optional_credits,
    )

    measurements = {
        family: {
            "status": "active",
            "calls": 10,
            "confirmed_credits": 20,
            "usable_receipts": 40,
            "unique_receipts": 20,
            "integrity": 0.9,
            "geo_precision": 0.8,
            "median_lead_hours": 6.0,
            "kill_test_passed": True,
        }
        for family in ("news", "reddit", "youtube")
    }
    rules = SourceYieldRules(
        efficiency_weight=0.2,
        unique_lift_weight=0.1,
        integrity_weight=0.2,
        geo_precision_weight=0.15,
        lead_time_weight=0.1,
        precision_weight=0.15,
        outcome_weight=0.1,
        receipts_per_credit_cap=2.0,
        maximum_lead_hours=24.0,
        minimum_integrity=0.7,
        minimum_geo_precision=0.6,
        maximum_false_discovery_rate=0.2,
    )
    envelope = CreditEnvelope(
        funding_math_status="complete",
        funded_increase_observed=True,
        funded_increase_amount=3000,
        optional_funded_credits_used=1000,
        balance=5000,
        baseline_credits_per_day=100.0,
        runway_floor_days=14,
        monthly_optional_credit_cap=2500,
        monthly_optional_credits_used=500,
    )
    observations = source_performance_observations(
        attributed_cohort_rows(outcome_rows()), measurements=measurements
    )

    proposal = propose_allocation(
        observations, rules=rules, envelope=envelope, route_caps={"reddit": 900, "youtube": 900}
    )

    assert isinstance(proposal, AllocationProposal)
    assert proposal.automatic_spend_enabled is False
    assert proposal.available_credits == available_optional_credits(envelope)
    assert sum(item.credits for item in proposal.allocations) <= proposal.available_credits
    assert {item.endpoint_id for item in proposal.allocations} <= {"reddit", "youtube"}
    states = {item.endpoint_id: item.state for item in proposal.evaluations}
    assert states["news"] == "ineligible"
    assert "news" not in {item.endpoint_id for item in proposal.allocations}
    with pytest.raises(ValueError, match="automatic spending is disabled"):
        AllocationProposal(
            evaluations=proposal.evaluations,
            allocations=proposal.allocations,
            available_credits=proposal.available_credits,
            envelope=proposal.envelope,
            budget_basis=proposal.budget_basis,
            unmeasured_endpoint_ids=proposal.unmeasured_endpoint_ids,
            automatic_spend_enabled=True,
        )
    source = inspect.getsource(outcome_reporting)
    for forbidden in ("funded_control", "funded_lane", "persist", "bigquery", "apply_allocation"):
        assert forbidden not in source


def test_cohort_report_labels_conditional_fdr_and_never_presents_it_as_the_cohort_rate() -> None:
    from src.analysis.open_intelligence.outcome_reporting import cohort_report

    report = cohort_report(outcome_rows(), minimum_due=3)

    assert report["cohort"]["conditional_fdr"] == 0.25
    assert report["cohort"]["resolved"] == 4
    assert report["cohort"]["fdr_upper"] == 0.4
    assert report["state_counts"] == {
        "peaked": 1,
        "sustained": 1,
        "fizzled": 1,
        "noise": 1,
        "unresolved": 1,
    }
    assert report["fizzled_are_false_discoveries"] is False
    assert report["effectiveness_gate"] is True
    assert set(report["by_source_family"]) == {"news", "reddit", "youtube"}
    assert set(report["by_source_set"]) == {"news+reddit", "reddit+youtube", "news+youtube"}
    rendered = json.dumps(report)
    assert '"fdr"' not in rendered
    assert '"false_discovery_rate"' not in rendered
    empty = cohort_report([], minimum_due=1)
    assert empty["cohort"]["conditional_fdr"] is None
    assert empty["effectiveness_gate"] is False


def measurement(**overrides: object) -> dict:
    """One complete source measurement. Every field is declared, null included."""
    base = {
        "status": "active",
        "calls": 10,
        "confirmed_credits": 20,
        "usable_receipts": 40,
        "unique_receipts": 20,
        "integrity": 0.9,
        "geo_precision": 0.8,
        "median_lead_hours": 6.0,
        "kill_test_passed": True,
    }
    base.update(overrides)
    return base


def complete_measurements() -> dict:
    return {family: measurement() for family in ("news", "reddit", "youtube")}


def yield_rules():
    from src.analysis.open_intelligence.source_yield import SourceYieldRules

    return SourceYieldRules(
        efficiency_weight=0.2,
        unique_lift_weight=0.1,
        integrity_weight=0.2,
        geo_precision_weight=0.15,
        lead_time_weight=0.1,
        precision_weight=0.15,
        outcome_weight=0.1,
        receipts_per_credit_cap=2.0,
        maximum_lead_hours=24.0,
        minimum_integrity=0.7,
        minimum_geo_precision=0.6,
        maximum_false_discovery_rate=0.6,
    )


def funded_envelope(**overrides: object):
    from src.analysis.open_intelligence.source_yield import CreditEnvelope

    base = {
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
    base.update(overrides)
    return CreditEnvelope(**base)


def closed_rows() -> list[dict]:
    """A closed weekly cohort: every row carries its declared date and immutable run."""
    return [
        {
            **row,
            "run_id": "outcome_eval_v2_2026-09-07",
            "evaluation_date": "2026-09-07",
        }
        for row in outcome_rows()
    ]


def test_a_source_measurement_field_that_was_never_recorded_is_refused() -> None:
    from src.analysis.open_intelligence.outcome_reporting import (
        attributed_cohort_rows,
        source_performance_observations,
    )

    rows = attributed_cohort_rows(outcome_rows())
    absent = complete_measurements()
    del absent["news"]["integrity"]
    with pytest.raises(ValueError, match="source_measurement_field_missing"):
        source_performance_observations(rows, measurements=absent)

    unknown = complete_measurements()
    unknown["news"]["integrity_estimate"] = 0.9
    with pytest.raises(ValueError, match="source_measurement_field_unknown"):
        source_performance_observations(rows, measurements=unknown)

    declared = complete_measurements()
    declared["news"] = measurement(integrity=None)
    observations = {
        item.endpoint_id: item
        for item in source_performance_observations(rows, measurements=declared)
    }
    assert observations["news"].integrity is None


@pytest.mark.parametrize(
    ("zeroed", "reason"),
    [
        ({"usable_receipts": 0, "unique_receipts": 0}, None),
        ({"unique_receipts": 0}, None),
        ({"integrity": 0.0}, None),
        ({"geo_precision": 0.0}, None),
        ({"median_lead_hours": 0.0}, None),
        ({"calls": 0}, "no_completed_calls"),
        ({"confirmed_credits": 0}, "no_confirmed_credit_spend"),
    ],
)
def test_a_measured_zero_is_never_read_as_a_value_nobody_measured(
    zeroed: dict, reason: str | None
) -> None:
    """Every field that can hold a recorded zero, not only the two that score.

    A zero the evaluator can divide by is scored as a zero. A zero it cannot
    divide by stops the scoring, and it says so under its own name, never under
    the name an absent field gets.
    """
    from src.analysis.open_intelligence.outcome_reporting import (
        attributed_cohort_rows,
        propose_allocation,
        source_performance_observations,
    )

    rows = attributed_cohort_rows(outcome_rows())
    measurements = complete_measurements()
    measurements["news"] = measurement(**zeroed)
    measurements["reddit"] = measurement(**dict.fromkeys(zeroed, None))
    observations = source_performance_observations(rows, measurements=measurements)
    proposal = propose_allocation(
        observations,
        rules=yield_rules(),
        envelope=funded_envelope(),
        route_caps={"news": 900, "reddit": 900, "youtube": 900},
    )

    states = {item.endpoint_id: item for item in proposal.evaluations}
    assert states["reddit"].state == "unmeasured"
    assert states["reddit"].score is None
    assert states["reddit"].reasons == ("incomplete_measurement",)
    if reason is None:
        assert states["news"].state != "unmeasured"
        assert states["news"].score is not None
        assert proposal.unmeasured_endpoint_ids == ("reddit",)
    else:
        assert states["news"].state == "unmeasured"
        assert states["news"].reasons == (reason,)
        assert states["news"].reasons != states["reddit"].reasons
        assert proposal.unmeasured_endpoint_ids == ("news", "reddit")


def test_a_measured_zero_receipt_count_still_carries_a_zero_unique_lift() -> None:
    from src.analysis.open_intelligence.outcome_reporting import (
        attributed_cohort_rows,
        propose_allocation,
        source_performance_observations,
    )

    measurements = complete_measurements()
    measurements["news"] = measurement(usable_receipts=0, unique_receipts=0)
    proposal = propose_allocation(
        source_performance_observations(
            attributed_cohort_rows(outcome_rows()), measurements=measurements
        ),
        rules=yield_rules(),
        envelope=funded_envelope(),
        route_caps={"news": 900, "reddit": 900, "youtube": 900},
    )

    states = {item.endpoint_id: item for item in proposal.evaluations}
    assert states["news"].unique_lift == 0.0
    assert states["news"].usable_receipts_per_credit == 0.0
    assert states["news"].score is not None
    assert "news" not in proposal.unmeasured_endpoint_ids


def test_an_allocation_proposal_names_its_unmeasured_sources_and_refuses_an_empty_set() -> None:
    from src.analysis.open_intelligence.outcome_reporting import (
        AllocationProposal,
        attributed_cohort_rows,
        propose_allocation,
        source_performance_observations,
    )

    with pytest.raises(ValueError, match="allocation proposal has no evaluated source"):
        propose_allocation(
            (), rules=yield_rules(), envelope=funded_envelope(), route_caps={"news": 10}
        )

    measurements = complete_measurements()
    measurements["youtube"] = measurement(kill_test_passed=None)
    proposal = propose_allocation(
        source_performance_observations(
            attributed_cohort_rows(outcome_rows()), measurements=measurements
        ),
        rules=yield_rules(),
        envelope=funded_envelope(),
        route_caps={"news": 900, "reddit": 900, "youtube": 900},
    )
    assert proposal.unmeasured_endpoint_ids == ("youtube",)
    with pytest.raises(ValueError, match="allocation proposal unmeasured sources are invalid"):
        AllocationProposal(
            evaluations=proposal.evaluations,
            allocations=proposal.allocations,
            available_credits=proposal.available_credits,
            envelope=proposal.envelope,
            budget_basis=proposal.budget_basis,
            unmeasured_endpoint_ids=(),
        )


def test_a_zero_budget_from_unknown_funding_is_not_a_measured_zero_budget() -> None:
    from src.analysis.open_intelligence.outcome_reporting import (
        attributed_cohort_rows,
        propose_allocation,
        source_performance_observations,
    )

    observations = source_performance_observations(
        attributed_cohort_rows(outcome_rows()), measurements=complete_measurements()
    )
    caps = {"news": 900, "reddit": 900, "youtube": 900}

    unknown = propose_allocation(
        observations,
        rules=yield_rules(),
        envelope=funded_envelope(funding_math_status="unknown"),
        route_caps=caps,
    )
    assert unknown.budget_basis == "unmeasured_funding"
    assert unknown.available_credits == 0
    assert unknown.allocations == ()

    spent = propose_allocation(
        observations,
        rules=yield_rules(),
        envelope=funded_envelope(monthly_optional_credits_used=2500),
        route_caps=caps,
    )
    assert spent.budget_basis == "measured"
    assert spent.available_credits == 0
    assert spent.allocations == ()
    assert unknown.budget_basis != spent.budget_basis


def test_the_closed_cohort_record_is_fixed_by_the_declared_close_and_never_by_a_clock() -> None:
    from src.analysis.open_intelligence.outcome_reporting import (
        CLOSED_COHORT_RECORD_FIELDS,
        closed_cohort_record,
    )

    arguments = record_arguments()
    first = closed_cohort_record(closed_rows(), **arguments)
    second = closed_cohort_record(closed_rows(), **arguments)

    assert tuple(first) == (*CLOSED_COHORT_RECORD_FIELDS, "record_digest")
    assert not any(
        marker in field
        for field in CLOSED_COHORT_RECORD_FIELDS
        for marker in ("computed", "generated", "reported_at", "now")
    )
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["cohort_close"] == "2026-09-07"
    # Not a scan of one module's text for a few spellings: a profile hook names
    # every clock primitive called anywhere below closed_cohort_record, so a
    # clock reached through a callee is caught as plainly as one written here.
    assert (
        clock_primitives_called_by(lambda: closed_cohort_record(closed_rows(), **arguments)) == ()
    )
    assert clock_primitives_called_by(_reads_a_clock) == ("datetime.now",)

    # The stamp the record actually reads is the evaluation date. Moving every
    # row's date back a month moves the record: the dates change, a gap appears
    # and the digest moves. A key the record never reads proves nothing either
    # way, so it is no longer offered as proof.
    moved = [{**row, "evaluation_date": "2026-08-07"} for row in closed_rows()]
    earlier = closed_cohort_record(moved, **arguments)
    assert first["evaluation_dates"] == ["2026-09-07"]
    assert earlier["evaluation_dates"] == ["2026-08-07"]
    assert "cohort_close_not_reached" not in first["gaps"]
    assert "cohort_close_not_reached" in earlier["gaps"]
    assert earlier["record_digest"] != first["record_digest"]


def test_the_closed_cohort_record_names_a_close_it_has_not_reached_and_refuses_rows_past_it() -> (
    None
):
    """A close the cohort stops short of is a named gap, not a refusal.

    The refusals here are the ones that really are refusals: an empty cohort, a
    row dated after the close, a close that is not a plain day, and a row with
    no run to attribute it to.
    """
    from src.analysis.open_intelligence.outcome_reporting import closed_cohort_record

    arguments = {key: value for key, value in record_arguments().items() if key != "cohort_close"}
    with pytest.raises(ValueError, match="closed_cohort_empty"):
        closed_cohort_record([], cohort_close=date(2026, 9, 7), **arguments)
    short = closed_cohort_record(closed_rows(), cohort_close=date(2026, 9, 14), **arguments)
    assert short["cohort_close"] == "2026-09-14"
    assert "cohort_close_not_reached" in short["gaps"]
    assert short["state"] == "incomplete"
    with pytest.raises(ValueError, match="outcome_after_cohort_close"):
        closed_cohort_record(
            [
                *closed_rows(),
                {**closed_rows()[0], "prediction_id": "pred_9", "evaluation_date": "2026-09-08"},
            ],
            cohort_close=date(2026, 9, 7),
            **arguments,
        )
    with pytest.raises(ValueError, match="cohort_close_invalid"):
        closed_cohort_record(closed_rows(), cohort_close="2026-09-07", **arguments)
    with pytest.raises(ValueError, match="evaluation_run_id_required"):
        closed_cohort_record(
            [
                {key: value for key, value in row.items() if key != "run_id"}
                for row in closed_rows()
            ],
            cohort_close=date(2026, 9, 7),
            **arguments,
        )
    claimed = [
        {**row, "cohort_close": "2026-10-31", "as_of": "2026-10-31"} for row in closed_rows()
    ]
    assert (
        closed_cohort_record(claimed, cohort_close=date(2026, 9, 7), **arguments)["cohort_close"]
        == "2026-09-07"
    )


def test_the_closed_cohort_record_names_what_was_never_measured() -> None:
    from src.analysis.open_intelligence.outcome_reporting import closed_cohort_record

    measurements = complete_measurements()
    measurements["youtube"] = measurement(geo_precision=None)
    # Every one of these was measured and came back at its floor. A floor is a
    # result; only the declared null above is an absence of one.
    measurements["reddit"] = measurement(
        integrity=0.0, median_lead_hours=0.0, kill_test_passed=False
    )
    record = closed_cohort_record(
        closed_rows(),
        **record_arguments(
            measurements=measurements,
            envelope=funded_envelope(funding_math_status="unknown"),
        ),
    )

    assert record["measurement_state"]["youtube"] == {
        "state": "unmeasured",
        "unmeasured_fields": ["geo_precision"],
        "scoring_state": "unmeasured",
        "scoring_reasons": ["incomplete_measurement"],
    }
    assert record["measurement_state"]["news"] == {
        "state": "measured",
        "unmeasured_fields": [],
        "scoring_state": "eligible",
        "scoring_reasons": [],
    }
    assert record["measurement_state"]["reddit"] == {
        "state": "measured",
        "unmeasured_fields": [],
        "scoring_state": "ineligible",
        "scoring_reasons": ["integrity_below_floor", "kill_test_failed"],
    }
    assert record["allocation"]["unmeasured_endpoint_ids"] == ["youtube"]
    evaluations = {item["endpoint_id"]: item for item in record["allocation"]["evaluations"]}
    # A family the record publishes as ineligible publishes why, and one it
    # cannot score publishes nothing it does not have.
    assert evaluations["reddit"] == {
        "endpoint_id": "reddit",
        "state": "ineligible",
        "score": pytest.approx(0.1 + 0.05 + 0.12 + 0.1 + 0.15 + 0.1),
        "unique_lift": 0.5,
        "lead_time_score": 1.0,
        "precision": 1.0,
        "eligible_for_optional": False,
        "kill_test_passed": False,
        "reasons": ["integrity_below_floor", "kill_test_failed"],
    }
    assert evaluations["youtube"] == {
        "endpoint_id": "youtube",
        "state": "unmeasured",
        "score": None,
        "unique_lift": None,
        "lead_time_score": None,
        "precision": None,
        "eligible_for_optional": False,
        "kill_test_passed": None,
        "reasons": ["incomplete_measurement"],
    }
    assert evaluations["news"]["reasons"] == []
    assert evaluations["news"]["eligible_for_optional"] is True
    assert record["allocation"]["budget_basis"] == "unmeasured_funding"
    assert record["state"] == "incomplete"
    assert set(record["gaps"]) == {
        "unrecorded_measurement_fields",
        "unscoreable_source_families",
        "unmeasured_funding_math",
    }
    assert record["evaluation_run_ids"] == ["outcome_eval_v2_2026-09-07"]
    assert record["evaluation_dates"] == ["2026-09-07"]

    unresolved_only = [
        {
            **closed_rows()[3],
            "prediction_id": f"pred_only_{index}",
        }
        for index in range(3)
    ]
    thin = closed_cohort_record(
        unresolved_only,
        **record_arguments(minimum_due=5, route_caps={"news": 900, "reddit": 900}),
    )
    assert thin["cohort"]["conditional_fdr"] is None
    assert thin["effectiveness_gate"] is False
    assert set(thin["gaps"]) >= {"no_resolved_outcome", "cohort_below_minimum_due"}


DOCUMENT_DIGEST = "d" * 64
COHORT_RUN_IDS = ("outcome_eval_v2_2026-09-07",)
MEASUREMENT_RULE_VERSION = "outcome_source_measurement_rules_v1"
_CLOCK_PRIMITIVES = frozenset(
    {
        "datetime.now",
        "datetime.utcnow",
        "datetime.today",
        "date.today",
        "time",
        "time_ns",
        "monotonic",
        "monotonic_ns",
        "perf_counter",
        "perf_counter_ns",
        "process_time",
        "gmtime",
        "localtime",
    }
)


def _reads_a_clock() -> None:
    """A deliberate clock read, so the proof below is known to detect one."""
    from datetime import UTC, datetime

    datetime.now(UTC)


def clock_primitives_called_by(call) -> tuple[str, ...]:
    """Name every clock primitive reached while running call, callees included.

    A profile hook sees each C level call made anywhere below the call, so a
    clock read through a helper in another module is named here as plainly as
    one written in the module under test.
    """
    import sys

    seen: list[str] = []

    def profile(frame, event, arg):
        if event != "c_call":
            return
        qualname = getattr(arg, "__qualname__", "")
        module = getattr(arg, "__module__", None)
        if qualname in _CLOCK_PRIMITIVES and module in (None, "", "time", "datetime"):
            seen.append(qualname)

    sys.setprofile(profile)
    try:
        call()
    finally:
        sys.setprofile(None)
    return tuple(seen)


def record_arguments(**overrides: object) -> dict:
    values = {
        "cohort_close": date(2026, 9, 7),
        "client_scope_id": "fixture_scope",
        "evaluation_version": "outcome_evaluation_v2",
        "rule_version": MEASUREMENT_RULE_VERSION,
        "measurement_document_digest": DOCUMENT_DIGEST,
        "evaluation_run_ids": COHORT_RUN_IDS,
        "minimum_due": 3,
        "measurements": complete_measurements(),
        "rules": yield_rules(),
        "envelope": funded_envelope(),
        "route_caps": {"news": 900, "reddit": 900, "youtube": 900},
    }
    values.update(overrides)
    return values


def body_digest(record: dict) -> str:
    """Recompute the digest here, from the record's own body, without the module."""
    import hashlib

    body = {key: value for key, value in record.items() if key != "record_digest"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def test_the_record_digest_tells_two_client_cohorts_apart() -> None:
    from src.analysis.open_intelligence.outcome_reporting import closed_cohort_record

    def scoped(scope: str) -> dict:
        rows = [{**row, "client_scope_id": scope} for row in closed_rows()]
        return closed_cohort_record(rows, **record_arguments(client_scope_id=scope))

    alpha = scoped("client_alpha")
    beta = scoped("client_beta")

    assert alpha["client_scope_id"] == "client_alpha"
    assert beta["client_scope_id"] == "client_beta"
    assert alpha["cohort"] == beta["cohort"]
    assert alpha["by_source_family"] == beta["by_source_family"]
    assert alpha["record_digest"] != beta["record_digest"]

    base = closed_cohort_record(closed_rows(), **record_arguments())
    assert base["rule_version"] == MEASUREMENT_RULE_VERSION
    assert base["evaluation_version"] == "outcome_evaluation_v2"
    assert base["measurement_document_digest"] == DOCUMENT_DIGEST
    for field, value in (
        ("rule_version", "outcome_source_measurement_rules_v2"),
        ("evaluation_version", "outcome_evaluation_v1"),
        ("measurement_document_digest", "e" * 64),
    ):
        moved = closed_cohort_record(closed_rows(), **record_arguments(**{field: value}))
        assert moved[field] == value
        assert moved["record_digest"] != base["record_digest"]


def test_the_record_digest_is_the_digest_of_the_body_it_publishes() -> None:
    from src.analysis.open_intelligence.outcome_reporting import (
        closed_cohort_record,
        verify_closed_cohort_record,
    )

    record = closed_cohort_record(closed_rows(), **record_arguments())

    assert record["record_digest"] == body_digest(record)
    assert verify_closed_cohort_record(record) is None

    tampered = {**record, "cohort": {**record["cohort"], "due": 99}}
    with pytest.raises(ValueError, match="closed_cohort_record_digest_mismatch"):
        verify_closed_cohort_record(tampered)
    scoped = {**record, "client_scope_id": "someone_else"}
    with pytest.raises(ValueError, match="closed_cohort_record_digest_mismatch"):
        verify_closed_cohort_record(scoped)
    with pytest.raises(ValueError, match="closed_cohort_record_fields_invalid"):
        verify_closed_cohort_record(
            {key: value for key, value in record.items() if key != "record_digest"}
        )


def test_a_record_verifies_whatever_order_its_keys_arrive_in() -> None:
    import json

    from src.analysis.open_intelligence.outcome_reporting import (
        closed_cohort_record,
        verify_closed_cohort_record,
    )

    record = closed_cohort_record(closed_rows(), **record_arguments())
    canonical = json.loads(json.dumps(record, sort_keys=True))
    assert tuple(canonical) != tuple(record)
    assert verify_closed_cohort_record(canonical) is None
    reversed_record = dict(reversed(list(record.items())))
    assert verify_closed_cohort_record(reversed_record) is None

    with pytest.raises(ValueError, match="closed_cohort_record_fields_invalid"):
        verify_closed_cohort_record({**canonical, "extra_field": 1})
    missing = {key: value for key, value in canonical.items() if key != "gaps"}
    with pytest.raises(ValueError, match="closed_cohort_record_fields_invalid"):
        verify_closed_cohort_record(missing)
    moved = {**canonical, "state": "complete" if canonical["state"] != "complete" else "incomplete"}
    with pytest.raises(ValueError, match="closed_cohort_record_digest_mismatch"):
        verify_closed_cohort_record(moved)


def test_a_row_carrying_another_run_or_another_client_is_refused() -> None:
    from src.analysis.open_intelligence.outcome_reporting import closed_cohort_record

    with pytest.raises(ValueError, match="evaluation_run_id_unexpected"):
        closed_cohort_record(
            [{**row, "run_id": "outcome_eval_v2_2026-08-01"} for row in closed_rows()],
            **record_arguments(),
        )
    with pytest.raises(ValueError, match="evaluation_run_id_required"):
        closed_cohort_record(
            [{**row, "run_id": "   "} for row in closed_rows()], **record_arguments()
        )
    with pytest.raises(ValueError, match="client_scope_mismatch"):
        closed_cohort_record(
            [{**row, "client_scope_id": "another_client"} for row in closed_rows()],
            **record_arguments(),
        )
    with pytest.raises(ValueError, match="client_scope_required"):
        closed_cohort_record(
            [
                {key: value for key, value in row.items() if key != "client_scope_id"}
                for row in closed_rows()
            ],
            **record_arguments(),
        )
    with pytest.raises(ValueError, match="evaluation_run_ids_required"):
        closed_cohort_record(closed_rows(), **record_arguments(evaluation_run_ids=()))

    expected = ("outcome_eval_v2_2026-09-06", *COHORT_RUN_IDS)
    record = closed_cohort_record(closed_rows(), **record_arguments(evaluation_run_ids=expected))
    assert record["evaluation_run_ids"] == sorted(expected)
    assert record["evaluation_dates"] == ["2026-09-07"]


def test_a_field_never_recorded_and_a_cohort_that_cannot_score_are_separate_gaps() -> None:
    from src.analysis.open_intelligence.outcome_reporting import (
        GAP_UNRECORDED_MEASUREMENT_FIELDS,
        GAP_UNSCOREABLE_SOURCE_FAMILIES,
        closed_cohort_record,
    )

    unrecorded = complete_measurements()
    unrecorded["youtube"] = measurement(geo_precision=None)
    first = closed_cohort_record(closed_rows(), **record_arguments(measurements=unrecorded))
    assert first["measurement_state"]["youtube"] == {
        "state": "unmeasured",
        "unmeasured_fields": ["geo_precision"],
        "scoring_state": "unmeasured",
        "scoring_reasons": ["incomplete_measurement"],
    }
    assert GAP_UNRECORDED_MEASUREMENT_FIELDS in first["gaps"]
    assert GAP_UNSCOREABLE_SOURCE_FAMILIES in first["gaps"]

    # Every field recorded, yet the cohort itself supplies no rate for youtube:
    # its only row is unresolved. The record must say measured and unscoreable
    # in the same breath, never measured and silent.
    rows = [
        {
            "client_scope_id": "fixture_scope",
            "run_id": COHORT_RUN_IDS[0],
            "evaluation_date": "2026-09-07",
            "prediction_id": prediction_id,
            "outcome": outcome,
            "source_families": families,
        }
        for prediction_id, outcome, families in (
            ("pred_a", "sustained", ["news", "reddit"]),
            ("pred_b", "noise", ["news", "reddit"]),
            ("pred_c", "unresolved", ["youtube"]),
        )
    ]
    second = closed_cohort_record(rows, **record_arguments())
    assert second["by_source_family"]["youtube"]["conditional_fdr"] is None
    assert second["measurement_state"]["youtube"] == {
        "state": "measured",
        "unmeasured_fields": [],
        "scoring_state": "unmeasured",
        "scoring_reasons": ["incomplete_measurement"],
    }
    assert GAP_UNSCOREABLE_SOURCE_FAMILIES in second["gaps"]
    assert GAP_UNRECORDED_MEASUREMENT_FIELDS not in second["gaps"]

    # A recorded zero on a field the evaluator divides by is a measurement and
    # is named as its own reason, never as an unrecorded field.
    zeroed = complete_measurements()
    zeroed["youtube"] = measurement(calls=0)
    third = closed_cohort_record(closed_rows(), **record_arguments(measurements=zeroed))
    assert third["measurement_state"]["youtube"] == {
        "state": "measured",
        "unmeasured_fields": [],
        "scoring_state": "unmeasured",
        "scoring_reasons": ["no_completed_calls"],
    }
    assert GAP_UNSCOREABLE_SOURCE_FAMILIES in third["gaps"]
    assert GAP_UNRECORDED_MEASUREMENT_FIELDS not in third["gaps"]


def test_the_minimum_due_floor_is_pinned_in_code_and_the_value_used_is_recorded() -> None:
    from src.analysis.open_intelligence.outcome_reporting import (
        CLOSED_COHORT_MINIMUM_DUE_FLOOR,
        closed_cohort_record,
    )

    assert CLOSED_COHORT_MINIMUM_DUE_FLOOR > 1

    single = closed_cohort_record(closed_rows()[:1], **record_arguments(minimum_due=1))
    assert single["minimum_due"] == CLOSED_COHORT_MINIMUM_DUE_FLOOR
    assert single["cohort"]["due"] == 1
    assert single["effectiveness_gate"] is False
    assert "cohort_below_minimum_due" in single["gaps"]
    assert single["state"] == "incomplete"

    # Exactly at the value used there is no gap, one below it there is: the
    # comparison is strict and the document may raise the floor, never lower it.
    at_floor = closed_cohort_record(
        closed_rows(), **record_arguments(minimum_due=CLOSED_COHORT_MINIMUM_DUE_FLOOR)
    )
    assert at_floor["cohort"]["due"] == CLOSED_COHORT_MINIMUM_DUE_FLOOR
    assert at_floor["minimum_due"] == CLOSED_COHORT_MINIMUM_DUE_FLOOR
    assert "cohort_below_minimum_due" not in at_floor["gaps"]

    above = closed_cohort_record(
        closed_rows(), **record_arguments(minimum_due=CLOSED_COHORT_MINIMUM_DUE_FLOOR + 1)
    )
    assert above["minimum_due"] == CLOSED_COHORT_MINIMUM_DUE_FLOOR + 1
    assert above["effectiveness_gate"] is False
    assert "cohort_below_minimum_due" in above["gaps"]

    for refused in (0, -1, True, 2.0, None):
        with pytest.raises(ValueError, match="minimum_due_invalid"):
            closed_cohort_record(closed_rows(), **record_arguments(minimum_due=refused))


def test_the_record_publishes_every_scored_value_it_claims() -> None:
    from src.analysis.open_intelligence.outcome_reporting import closed_cohort_record

    record = closed_cohort_record(closed_rows(), **record_arguments(minimum_due=5))

    assert record["state_counts"] == {
        "peaked": 1,
        "sustained": 1,
        "fizzled": 1,
        "noise": 1,
        "unresolved": 1,
    }
    assert tuple(record["state_counts"]) == (
        "peaked",
        "sustained",
        "fizzled",
        "noise",
        "unresolved",
    )
    assert record["fizzled_are_false_discoveries"] is False
    assert record["cohort"] == {
        "due": 5,
        "resolved": 4,
        "unresolved": 1,
        "conditional_fdr": 0.25,
        "fdr_lower": 0.2,
        "fdr_upper": 0.4,
    }
    assert record["by_source_family"]["news"] == {
        "due": 3,
        "resolved": 2,
        "unresolved": 1,
        "conditional_fdr": 0.5,
        "fdr_lower": pytest.approx(1 / 3),
        "fdr_upper": pytest.approx(2 / 3),
    }
    assert set(record["by_source_family"]) == {"news", "reddit", "youtube"}
    assert set(record["by_source_set"]) == {"news+reddit", "news+youtube", "reddit+youtube"}
    assert record["effectiveness_gate"] is True
    assert record["gaps"] == []
    assert record["state"] == "complete"

    evaluations = {item["endpoint_id"]: item for item in record["allocation"]["evaluations"]}
    assert set(evaluations) == {"news", "reddit", "youtube"}
    assert evaluations["reddit"] == {
        "endpoint_id": "reddit",
        "state": "eligible",
        "score": pytest.approx(0.775),
        "unique_lift": 0.5,
        "lead_time_score": 0.75,
        "precision": 1.0,
        "eligible_for_optional": True,
        "kill_test_passed": True,
        "reasons": [],
    }
    assert evaluations["news"]["score"] == pytest.approx(0.525 + 0.075 + 1 / 30)
    assert evaluations["news"]["precision"] == 0.5
    assert evaluations["youtube"]["precision"] == pytest.approx(2 / 3)
    assert evaluations["youtube"]["score"] == pytest.approx(0.525 + 0.1 + 1 / 15)

    # The caps here total more than the envelope, so the envelope binds and the
    # published total is the budget itself, not whatever the caps happen to be.
    allocation = record["allocation"]
    assert allocation["available_credits"] == 2000
    assert {item["endpoint_id"]: item["credits"] for item in allocation["allocations"]} == {
        "news": 603,
        "reddit": 738,
        "youtube": 659,
    }
    assert sum(item["credits"] for item in allocation["allocations"]) == 2000
    assert allocation["unmeasured_endpoint_ids"] == []
    assert allocation["budget_basis"] == "measured"
    assert allocation["automatic_spend_enabled"] is False


def test_the_published_uniqueness_lift_is_the_one_that_was_measured() -> None:
    from src.analysis.open_intelligence.outcome_reporting import closed_cohort_record

    measurements = complete_measurements()
    measurements["youtube"] = measurement(unique_receipts=10)
    measurements["news"] = measurement(unique_receipts=40)

    record = closed_cohort_record(closed_rows(), **record_arguments(measurements=measurements))

    lifts = {
        item["endpoint_id"]: item["unique_lift"] for item in record["allocation"]["evaluations"]
    }
    assert lifts == {"news": 1.0, "reddit": 0.5, "youtube": 0.25}


def test_route_caps_bind_the_allocation_below_the_envelope() -> None:
    from src.analysis.open_intelligence.outcome_reporting import closed_cohort_record

    record = closed_cohort_record(
        closed_rows(),
        **record_arguments(route_caps={"news": 10, "reddit": 10, "youtube": 0}),
    )

    allocation = record["allocation"]
    assert allocation["available_credits"] == 2000
    assert {item["endpoint_id"]: item["credits"] for item in allocation["allocations"]} == {
        "news": 10,
        "reddit": 10,
    }


def test_the_proposal_sorts_its_unmeasured_sources_whatever_order_they_arrive_in() -> None:
    from src.analysis.open_intelligence.outcome_reporting import (
        AllocationProposal,
        propose_allocation,
    )
    from src.analysis.open_intelligence.source_yield import SourcePerformanceObservation

    def observation(endpoint_id: str, *, measured: bool) -> SourcePerformanceObservation:
        return SourcePerformanceObservation(
            endpoint_id=endpoint_id,
            status="active",
            calls=10,
            confirmed_credits=20,
            usable_receipts=40,
            unique_receipts=20,
            integrity=0.9,
            geo_precision=0.8,
            median_lead_hours=6.0,
            false_discovery_rate=0.1 if measured else None,
            outcome_contribution=0.5,
            kill_test_passed=True,
        )

    proposal = propose_allocation(
        (
            observation("youtube", measured=False),
            observation("reddit", measured=True),
            observation("news", measured=False),
        ),
        rules=yield_rules(),
        envelope=funded_envelope(),
        route_caps={"news": 900, "reddit": 900, "youtube": 900},
    )

    assert tuple(item.endpoint_id for item in proposal.evaluations) == (
        "youtube",
        "reddit",
        "news",
    )
    assert proposal.unmeasured_endpoint_ids == ("news", "youtube")
    with pytest.raises(ValueError, match="allocation proposal unmeasured sources are invalid"):
        AllocationProposal(
            evaluations=proposal.evaluations,
            allocations=proposal.allocations,
            available_credits=proposal.available_credits,
            envelope=proposal.envelope,
            budget_basis=proposal.budget_basis,
            unmeasured_endpoint_ids=("youtube", "news"),
        )


def test_the_proposal_re_derives_its_budget_from_the_envelope_it_carries() -> None:
    from src.analysis.open_intelligence.outcome_reporting import (
        AllocationProposal,
        attributed_cohort_rows,
        propose_allocation,
        source_performance_observations,
    )

    proposal = propose_allocation(
        source_performance_observations(
            attributed_cohort_rows(outcome_rows()), measurements=complete_measurements()
        ),
        rules=yield_rules(),
        envelope=funded_envelope(),
        route_caps={"news": 900, "reddit": 900, "youtube": 900},
    )
    assert proposal.envelope == funded_envelope()
    assert proposal.budget_basis == "measured"

    def rebuilt(**overrides: object) -> AllocationProposal:
        values = {
            "evaluations": proposal.evaluations,
            "allocations": proposal.allocations,
            "available_credits": proposal.available_credits,
            "envelope": proposal.envelope,
            "budget_basis": proposal.budget_basis,
            "unmeasured_endpoint_ids": proposal.unmeasured_endpoint_ids,
        }
        values.update(overrides)
        return AllocationProposal(**values)

    assert rebuilt().budget_basis == "measured"
    with pytest.raises(ValueError, match="budget basis does not match the envelope"):
        rebuilt(budget_basis="unmeasured_funding")
    with pytest.raises(ValueError, match="budget basis does not match the envelope"):
        rebuilt(envelope=funded_envelope(funding_math_status="unknown"))
    with pytest.raises(ValueError, match="available credits do not match the envelope"):
        rebuilt(available_credits=proposal.available_credits - 1)
    with pytest.raises(ValueError, match="allocation proposal envelope is invalid"):
        rebuilt(envelope=None)


def test_a_datetime_is_never_a_cohort_day() -> None:
    from datetime import UTC, datetime

    from src.analysis.open_intelligence.outcome_reporting import closed_cohort_record

    with pytest.raises(ValueError, match="cohort_close_invalid"):
        closed_cohort_record(
            closed_rows(),
            **record_arguments(cohort_close=datetime(2026, 9, 7, 12, 0, tzinfo=UTC)),
        )
    with pytest.raises(ValueError, match="evaluation_date_invalid"):
        closed_cohort_record(
            [
                {**row, "evaluation_date": datetime(2026, 9, 7, 12, 0, tzinfo=UTC)}
                for row in closed_rows()
            ],
            **record_arguments(),
        )


def test_the_close_gap_reads_the_latest_evaluation_day_not_the_earliest() -> None:
    from src.analysis.open_intelligence.outcome_reporting import closed_cohort_record

    spread = [
        {**row, "evaluation_date": "2026-09-05" if index < 2 else "2026-09-07"}
        for index, row in enumerate(closed_rows())
    ]

    record = closed_cohort_record(spread, **record_arguments())

    assert record["evaluation_dates"] == ["2026-09-05", "2026-09-07"]
    assert "cohort_close_not_reached" not in record["gaps"]

    early = closed_cohort_record(
        [{**row, "evaluation_date": "2026-09-05"} for row in closed_rows()],
        **record_arguments(),
    )
    assert "cohort_close_not_reached" in early["gaps"]


def test_record_field_drift_is_a_named_refusal_a_caller_can_catch(monkeypatch) -> None:
    from src.analysis.open_intelligence import outcome_reporting

    monkeypatch.setattr(
        outcome_reporting,
        "CLOSED_COHORT_RECORD_FIELDS",
        outcome_reporting.CLOSED_COHORT_RECORD_FIELDS[:-1],
    )

    with pytest.raises(ValueError, match="closed_cohort_record_field_drift"):
        outcome_reporting.closed_cohort_record(closed_rows(), **record_arguments())
