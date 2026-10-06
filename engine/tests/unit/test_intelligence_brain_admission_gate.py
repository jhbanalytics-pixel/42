from __future__ import annotations


def test_mandatory_human_approval_blocks_aggregate_downstream_admission():
    from src.analysis.open_intelligence.brain import (
        IntelligenceBrainIntent,
        run_intelligence_brain,
        validate_intelligence_brain_result,
    )

    result = run_intelligence_brain(
        IntelligenceBrainIntent(
            "intelligence_brain_v1",
            "inv_fixture_01",
            "sig_" + "a" * 64,
            "investigation",
            "What changed?",
            None,
        )
    )

    assert result.overall_admission.availability_state == "available"
    assert result.overall_admission.validation_state == "validated"
    assert result.overall_admission.approval_state == "pending"
    assert result.overall_admission.ready_for_downstream is False
    assert "human approval" in result.missing_work
    validate_intelligence_brain_result(result)
