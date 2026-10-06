"""Contract tests for audience-neutral Open Intelligence scoring."""

from __future__ import annotations

import inspect
import re
from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta

import pytest
from src.analysis.open_intelligence import scoring as scoring_module
from src.analysis.open_intelligence.scoring import (
    COHORT_PERCENTILE_VERSION,
    DECISION_STRENGTH_VERSION,
    EVIDENCE_STRENGTH_VERSION,
    DecisionStrengthRules,
    SignalScoreInput,
    calibrate_cohort_percentiles,
    score_signal,
)


def rules(**overrides: object) -> DecisionStrengthRules:
    values = {
        "approved_receipt_cap": 8,
        "geo_floor_by_market": {"za": 0.8, "ng": 0.8, "ke": 0.8},
        "approved_cluster_build_versions": ("hybrid_graph_v1",),
        "rule_version": "decision_rules_fixture_v1",
    }
    values.update(overrides)
    return DecisionStrengthRules(**values)


def signal(**overrides: object) -> SignalScoreInput:
    values = {
        "signal_id": "sig_" + "a" * 64,
        "market": "za",
        "signal_date": date(2026, 8, 26),
        "discovery_mode": "dynamic",
        "evidence_state": "ready",
        "qualifying_source_families": 4,
        "qualifying_current_receipts": 8,
        "source_integrity": 0.8,
        "velocity": 0.8,
        "breadth": 0.8,
        "source_independence": 0.8,
        "geo_confidence": 0.8,
        "foreign_market_sample_reviewed": True,
        "foreign_market_leakage": 0,
        "duplicate_identity": False,
        "factual_conflict": False,
        "directional_conflict": False,
        "membership_receipts_complete": True,
        "cluster_build_version": "hybrid_graph_v1",
    }
    values.update(overrides)
    return SignalScoreInput(**values)


def test_equal_geometric_contract_and_versions_are_exact() -> None:
    result = score_signal(signal(source_integrity=0.512), rules())

    assert result.promotion_eligible is True
    assert result.reasons == ()
    assert result.evidence_strength == pytest.approx(0.8)
    assert result.decision_strength == pytest.approx(0.8)
    assert result.cohort_percentile is None
    assert result.evidence_strength_version == EVIDENCE_STRENGTH_VERSION
    assert result.decision_strength_version == DECISION_STRENGTH_VERSION
    assert result.cohort_percentile_version == COHORT_PERCENTILE_VERSION
    assert result.rule_version == "decision_rules_fixture_v1"


def test_evidence_strength_uses_family_receipt_and_integrity_geometric_mean() -> None:
    result = score_signal(
        signal(
            qualifying_source_families=2,
            qualifying_current_receipts=4,
            source_integrity=0.8,
        ),
        rules(),
    )

    assert result.evidence_strength == pytest.approx((0.5 * 0.5 * 0.8) ** (1 / 3))
    assert result.decision_strength == pytest.approx(
        (0.8 * 0.8 * 0.8 * result.evidence_strength * 0.8) ** (1 / 5)
    )


def test_evidence_factors_saturate_above_family_and_receipt_caps() -> None:
    at_cap = score_signal(signal(), rules())
    above_cap = score_signal(
        signal(qualifying_source_families=8, qualifying_current_receipts=16), rules()
    )

    assert above_cap.evidence_strength == at_cap.evidence_strength
    assert above_cap.decision_strength == at_cap.decision_strength


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"evidence_state": "thin"}, "evidence_not_ready"),
        ({"qualifying_source_families": 1}, "insufficient_source_families"),
        ({"geo_confidence": 0.79}, "geo_below_market_floor"),
        ({"foreign_market_sample_reviewed": False}, "foreign_market_sample_unreviewed"),
        ({"foreign_market_leakage": 1}, "foreign_market_leakage"),
        ({"duplicate_identity": True}, "duplicate_identity"),
        ({"factual_conflict": True}, "factual_conflict"),
        ({"directional_conflict": True}, "directional_conflict"),
        ({"membership_receipts_complete": False}, "membership_receipts_incomplete"),
        ({"cluster_build_version": "unapproved_v2"}, "cluster_build_unapproved"),
    ],
)
def test_every_hard_gate_blocks_promotion_without_coercing_score_to_zero(
    overrides: dict[str, object], reason: str
) -> None:
    result = score_signal(signal(**overrides), rules())

    assert result.promotion_eligible is False
    assert reason in result.reasons
    assert result.decision_strength is None
    assert result.evidence_strength is not None


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("velocity", "velocity_unmeasured"),
        ("breadth", "breadth_unmeasured"),
        ("source_independence", "source_independence_unmeasured"),
        ("geo_confidence", "geo_confidence_unmeasured"),
        ("qualifying_source_families", "source_families_unmeasured"),
        ("qualifying_current_receipts", "current_receipts_unmeasured"),
        ("source_integrity", "source_integrity_unmeasured"),
        ("foreign_market_sample_reviewed", "foreign_market_sample_unreviewed"),
        ("foreign_market_leakage", "foreign_market_leakage_unmeasured"),
        ("duplicate_identity", "duplicate_identity_unmeasured"),
        ("factual_conflict", "factual_conflict_unmeasured"),
        ("directional_conflict", "directional_conflict_unmeasured"),
        ("membership_receipts_complete", "membership_receipts_unmeasured"),
        ("cluster_build_version", "cluster_build_unmeasured"),
    ],
)
def test_missing_measurement_is_null_and_fails_closed(field: str, reason: str) -> None:
    result = score_signal(signal(**{field: None}), rules())

    assert result.promotion_eligible is False
    assert reason in result.reasons
    assert result.decision_strength is None
    if field in {
        "qualifying_source_families",
        "qualifying_current_receipts",
        "source_integrity",
    }:
        assert result.evidence_strength is None


@pytest.mark.parametrize(
    "field",
    ["velocity", "breadth", "source_independence", "source_integrity", "geo_confidence"],
)
def test_genuine_zero_in_each_required_component_produces_zero(field: str) -> None:
    result = score_signal(
        signal(**{field: 0.0}), rules(geo_floor_by_market={"za": 0.0, "ng": 0.8, "ke": 0.8})
    )

    assert result.promotion_eligible is True
    assert result.decision_strength == 0.0


@pytest.mark.parametrize(
    "field",
    ["velocity", "breadth", "source_independence", "source_integrity", "geo_confidence"],
)
def test_each_component_is_monotonic(field: str) -> None:
    low_rules = rules(geo_floor_by_market={"za": 0.0, "ng": 0.8, "ke": 0.8})
    low = score_signal(signal(**{field: 0.25}), low_rules)
    high = score_signal(signal(**{field: 0.75}), low_rules)

    assert low.decision_strength < high.decision_strength


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("velocity", True),
        ("breadth", float("nan")),
        ("source_independence", -0.01),
        ("source_integrity", 1.01),
        ("geo_confidence", float("inf")),
        ("qualifying_source_families", True),
        ("qualifying_current_receipts", -1),
        ("foreign_market_leakage", -1),
        ("duplicate_identity", 0),
        ("market", "ZA"),
    ],
)
def test_malformed_inputs_are_rejected_instead_of_hidden(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        signal(**{field: value})


@pytest.mark.parametrize(
    "overrides",
    [
        {"approved_receipt_cap": 0},
        {"approved_receipt_cap": True},
        {"geo_floor_by_market": {"za": 0.8}},
        {"geo_floor_by_market": {"za": 0.8, "ng": 0.8, "ke": 1.1}},
        {"approved_cluster_build_versions": ()},
        {"rule_version": ""},
    ],
)
def test_rules_fail_closed_on_unapproved_or_malformed_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        rules(**overrides)


def _cohort(size: int, *, market: str = "za", mode: str = "dynamic"):
    output = []
    for index in range(size):
        strength = index / max(1, size - 1)
        item = signal(
            signal_id=f"sig_{index:064x}",
            market=market,
            discovery_mode=mode,
            velocity=strength,
            geo_confidence=max(0.8, strength),
        )
        output.append(score_signal(item, rules()))
    return tuple(output)


def test_cohort_below_twenty_keeps_percentile_null() -> None:
    calibrated = calibrate_cohort_percentiles(_cohort(19))

    assert all(result.cohort_percentile is None for result in calibrated)


def test_twenty_signal_cohort_uses_deterministic_average_rank_from_zero_to_one() -> None:
    calibrated = calibrate_cohort_percentiles(_cohort(20))

    assert calibrated[0].cohort_percentile == 0.0
    assert calibrated[-1].cohort_percentile == 1.0
    assert [item.cohort_percentile for item in calibrated] == sorted(
        item.cohort_percentile for item in calibrated
    )


def test_ties_receive_the_same_average_rank_percentile() -> None:
    cohort = list(_cohort(20))
    cohort[10] = score_signal(
        signal(
            signal_id="sig_" + f"{10:064x}",
            velocity=cohort[9].velocity,
            geo_confidence=cohort[9].geo_confidence,
        ),
        rules(),
    )

    calibrated = calibrate_cohort_percentiles(cohort)

    assert calibrated[9].cohort_percentile == calibrated[10].cohort_percentile == 9.5 / 19


@pytest.mark.parametrize("dimension", ["market", "signal_date", "discovery_mode", "rule_version"])
def test_market_date_mode_and_rule_version_define_separate_cohorts(dimension: str) -> None:
    base = _cohort(20)
    changed = {
        "market": "ng",
        "signal_date": base[0].signal_date + timedelta(days=1),
        "discovery_mode": "replay",
        "rule_version": "decision_rules_fixture_v2",
    }
    split = tuple(
        replace(item, **{dimension: changed[dimension]}) if index < 10 else item
        for index, item in enumerate(base)
    )

    calibrated = calibrate_cohort_percentiles(split)

    assert all(item.cohort_percentile is None for item in calibrated)


def test_ineligible_results_do_not_enter_the_percentile_denominator() -> None:
    cohort = list(_cohort(20))
    cohort.append(score_signal(signal(signal_id="sig_" + "f" * 64, evidence_state="thin"), rules()))

    calibrated = calibrate_cohort_percentiles(cohort)

    assert calibrated[-1].cohort_percentile is None
    assert calibrated[-2].cohort_percentile == 1.0


def test_duplicate_signal_identity_is_rejected_before_calibration() -> None:
    first = _cohort(20)[0]

    with pytest.raises(ValueError, match="duplicate signal score identity"):
        calibrate_cohort_percentiles((first, first))


def test_public_contract_is_frozen_and_contains_no_audience_field() -> None:
    item = signal()
    result = score_signal(item, rules())

    with pytest.raises(FrozenInstanceError):
        item.velocity = 0.2
    with pytest.raises(FrozenInstanceError):
        result.decision_strength = 0.2
    public_fields = " ".join((*item.__dataclass_fields__, *result.__dataclass_fields__))
    formula_source = "\n".join(
        inspect.getsource(function)
        for function in (
            scoring_module._evidence_strength,
            scoring_module._gate_reasons,
            scoring_module.score_signal,
        )
    )
    prohibited = re.compile(
        r"\b(?:audience(?:_fit|_weight)?|gen[_ ]?z|gender|demographic|age(?:_factor|_weight))\b",
        re.IGNORECASE,
    )
    assert prohibited.search(public_fields) is None
    assert prohibited.search(formula_source) is None


def test_public_result_rejects_forged_promotion_states_and_scores() -> None:
    result = score_signal(signal(source_integrity=0.512), rules())

    with pytest.raises(ValueError, match="ineligible result cannot carry decision strength"):
        replace(
            result,
            promotion_eligible=False,
            reasons=("evidence_not_ready",),
            decision_strength=result.decision_strength,
        )
    with pytest.raises(ValueError, match="ineligible result requires reasons"):
        replace(result, promotion_eligible=False, reasons=(), decision_strength=None)
    with pytest.raises(ValueError, match="geometric mean"):
        replace(result, decision_strength=0.123)
