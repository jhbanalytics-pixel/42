from __future__ import annotations

from dataclasses import replace

import pytest


def context():
    from src.analysis.open_intelligence.brain_skeptic import _run_skeptic

    from tests.unit.test_intelligence_brain_skeptic import context as skeptic_context

    values = skeptic_context()
    return (*values, _run_skeptic(*values))


def test_strategist_requires_competing_behaviors_and_specific_opportunity():
    from src.analysis.open_intelligence.brain_strategist import (
        _run_strategist,
        validate_brain_specificity,
    )

    result = _run_strategist(*context())
    assert set(result.cultural_tension.first_behavior_observation_ids).isdisjoint(
        result.cultural_tension.second_behavior_observation_ids
    )
    assert result.opportunity.admission.ready_for_downstream is True
    assert validate_brain_specificity(result.opportunity).passed is True


@pytest.mark.parametrize(
    "field",
    [
        "brand_role",
        "why_role_fits",
        "what_to_avoid",
        "smallest_useful_experiment",
        "scale_evidence",
        "risk_too_early",
        "risk_too_late",
        "risk_without_permission",
    ],
)
def test_each_specificity_field_is_load_bearing(field):
    from src.analysis.open_intelligence.brain_strategist import (
        _run_strategist,
        validate_brain_specificity,
    )

    result = _run_strategist(*context())
    mutated = replace(result.opportunity, **{field: ""})
    assert validate_brain_specificity(mutated).passed is False


def test_phrase_match_alone_does_not_reject_complete_opportunity():
    from src.analysis.open_intelligence.brain_strategist import (
        _run_strategist,
        validate_brain_specificity,
    )

    result = _run_strategist(*context())
    generic = replace(result.opportunity, statement="Join the conversation.")
    check = validate_brain_specificity(generic)
    assert check.passed is True
    assert "generic_phrase_family" in check.reason_codes


def test_tension_rejects_overlapping_behavior_sets():
    from src.analysis.open_intelligence.brain_strategist import _validate_tension

    with pytest.raises(ValueError, match="tension"):
        _validate_tension(("obs_1",), ("obs_1",))
