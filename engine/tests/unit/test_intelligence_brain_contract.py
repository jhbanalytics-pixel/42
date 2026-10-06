from __future__ import annotations

from dataclasses import asdict, fields
from datetime import UTC, datetime

import pytest


def test_contract_values_and_dark_policy_are_exact():
    from src.analysis.open_intelligence.brain_contract import (
        BRAIN_CONTRACT_VERSION,
        DARK_DEPTH_POLICY,
        DARK_DEPTH_POLICY_DIGEST,
        IntelligenceBrainIntent,
        canonical_digest,
    )

    assert BRAIN_CONTRACT_VERSION == "intelligence_brain_v1"
    assert DARK_DEPTH_POLICY_DIGEST == (
        "a638cb1d4633fa8b336a248ea4a256e6f3528abd53e246b568e683b6afb69d31"
    )
    assert canonical_digest(DARK_DEPTH_POLICY) == DARK_DEPTH_POLICY_DIGEST
    assert tuple(item.name for item in fields(IntelligenceBrainIntent)) == (
        "contract_version",
        "investigation_id",
        "signal_id",
        "research_depth",
        "decision_question",
        "brand_context_id",
    )
    assert all(
        value["required_roles"]
        == ["observer", "historian", "analyst", "skeptic", "strategist", "editor"]
        for value in DARK_DEPTH_POLICY["depths"].values()
    )
    assert all(value["model_stage_limit"] == 0 for value in DARK_DEPTH_POLICY["depths"].values())


def test_missing_measurement_never_becomes_zero():
    from src.analysis.open_intelligence.brain_contract import BrainMeasurement

    missing = BrainMeasurement(
        measurement_id="bm_missing",
        metric_name="velocity",
        availability="unavailable",
        value=None,
        unit=None,
        method_id=None,
        window_start=None,
        window_end=None,
        market=None,
        evidence_ids=(),
        limitations=("not measured",),
    )
    assert missing.value is None
    with pytest.raises(ValueError, match="unavailable measurement"):
        BrainMeasurement(**{**asdict(missing), "value": 0.0})


def test_confidence_and_audience_invariants():
    from src.analysis.open_intelligence.brain_contract import (
        AudienceAvailabilityRecord,
        BrainConfidence,
    )

    evidence_id = "ev_" + "1" * 64
    confidence = BrainConfidence("available", 0.7, "confidence_v1", (evidence_id,), ())
    audience = AudienceAvailabilityRecord(
        state="measured",
        statement="Observed in the approved sample.",
        source_id="source_1",
        window_start=datetime(2026, 8, 1, tzinfo=UTC),
        window_end=datetime(2026, 8, 20, tzinfo=UTC),
        method_id="audience_v1",
        scope_digest="scp_1",
        confidence=confidence,
        evidence_ids=(evidence_id,),
        limitations=(),
    )
    assert audience.confidence.value == 0.7
    with pytest.raises(ValueError, match="confidence"):
        BrainConfidence("available", 2.0, "confidence_v1", (evidence_id,), ())
    with pytest.raises(ValueError, match="unavailable audience"):
        AudienceAvailabilityRecord(
            "unavailable", "invented", None, None, None, None, None, None, (), ()
        )


@pytest.mark.parametrize(
    ("availability", "validation", "approval", "ready"),
    [
        ("unavailable", "validated", "approved", False),
        ("available", "proposed", "not_required", False),
        ("available", "rejected", "not_required", False),
        ("available", "validated", "not_required", True),
        ("available", "validated", "pending", False),
        ("available", "validated", "approved", True),
        ("available", "validated", "rejected", False),
    ],
)
def test_admission_decision_table(availability, validation, approval, ready):
    from src.analysis.open_intelligence.brain_contract import BrainAdmission

    decision = "decision_1" if approval in {"approved", "rejected"} else None
    value = BrainAdmission(availability, validation, approval, ready, ("fixture",), decision)
    assert value.ready_for_downstream is ready
