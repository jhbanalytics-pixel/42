from __future__ import annotations

import pytest


def context():
    from src.analysis.open_intelligence.brain_authority import (
        _issue_brain_evidence_snapshot,
        _issue_brain_runtime_request,
    )
    from src.analysis.open_intelligence.brain_contract import IntelligenceBrainIntent

    runtime = _issue_brain_runtime_request(
        IntelligenceBrainIntent(
            "intelligence_brain_v1",
            "inv_fixture_01",
            "sig_" + "a" * 64,
            "investigation",
            "What changed?",
            None,
        )
    )
    return runtime, _issue_brain_evidence_snapshot(runtime)


def test_observer_emits_fourteen_deterministic_measurements():
    from src.analysis.open_intelligence.brain_observer import _run_observer

    runtime, snapshot = context()
    result = _run_observer(runtime, snapshot)
    assert len(result.observations) == 14
    assert {item.observation_type for item in result.observations} == {
        "novelty",
        "velocity",
        "breadth",
        "source_independence",
        "persistence",
        "creator_spread",
        "engagement_quality",
        "search_movement",
        "geographic_movement",
        "historical_rarity",
        "entity_emergence",
        "language_emergence",
        "cross_platform_spread",
        "source_movement",
    }
    assert all(
        item.evidence_ids or item.measurement_id.startswith("bm_unavailable_")
        for item in result.observations
    )
    assert result.audience_availability.state == "unavailable"


def test_observer_rejects_foreign_snapshot_authority():
    from src.analysis.open_intelligence.brain_observer import _run_observer

    first_runtime, _ = context()
    _, second_snapshot = context()
    with pytest.raises(ValueError, match="snapshot authority"):
        _run_observer(first_runtime, second_snapshot)
