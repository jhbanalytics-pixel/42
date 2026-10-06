from __future__ import annotations

import pytest


def context():
    from src.analysis.open_intelligence.brain_historian import _run_historian

    from tests.unit.test_intelligence_brain_historian import context as historian_context

    runtime, snapshot, observer = historian_context()
    return runtime, snapshot, observer, _run_historian(runtime, snapshot, observer)


def test_analyst_builds_measured_why_now_and_prediction_learning():
    from src.analysis.open_intelligence.brain_analyst import _run_analyst

    runtime, snapshot, observer, historian = context()
    result = _run_analyst(runtime, snapshot, observer, historian)
    assert result.why_now.velocity_measurement_id == "bm_velocity"
    assert result.why_now.admission.ready_for_downstream is True
    assert result.causal_status == "causal_claim_unavailable"
    assert result.prediction_learning.outcome_state == "resolved"
    assert result.prediction_learning.proposed_changes


def test_why_now_rejects_volume_only_and_missing_movement():
    from src.analysis.open_intelligence.brain_analyst import _movement_ids

    with pytest.raises(ValueError, match="Why Now"):
        _movement_ids(())
    with pytest.raises(ValueError, match="volume"):
        _movement_ids(("engagement_quality",))
    assert _movement_ids(("velocity",)) == ("velocity",)


def test_analyst_requires_historian_authority():
    from src.analysis.open_intelligence.brain_analyst import _run_analyst

    runtime, snapshot, observer, historian = context()
    object.__setattr__(historian, "source_behavior_changes", ("forged",))
    with pytest.raises(ValueError, match="role authority"):
        _run_analyst(runtime, snapshot, observer, historian)
