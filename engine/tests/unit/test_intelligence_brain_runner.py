from __future__ import annotations

import copy
import pickle
from dataclasses import replace

import pytest


def intent(signal_id=None):
    from src.analysis.open_intelligence.brain import IntelligenceBrainIntent

    return IntelligenceBrainIntent(
        "intelligence_brain_v1",
        "inv_fixture_01",
        signal_id or "sig_" + "a" * 64,
        "investigation",
        "What changed?",
        None,
    )


def test_public_exports_and_dark_role_order_are_exact():
    from src.analysis.open_intelligence import brain

    assert tuple(brain.__all__) == (
        "IntelligenceBrainIntent",
        "IntelligenceBrainResult",
        "run_intelligence_brain",
        "validate_intelligence_brain_result",
    )
    result = brain.run_intelligence_brain(intent())
    assert result.model_usage_receipt_ids == ()
    assert result.depth_policy_digest == (
        "a638cb1d4633fa8b336a248ea4a256e6f3528abd53e246b568e683b6afb69d31"
    )
    assert [
        getattr(result, name).role_version
        for name in ("observer", "historian", "analyst", "skeptic", "strategist", "editor")
    ] == ["observer_v1", "historian_v1", "analyst_v1", "skeptic_v1", "strategist_v1", "editor_v1"]
    brain.validate_intelligence_brain_result(result)


@pytest.mark.parametrize("mutation", ["replace", "copy", "deepcopy", "pickle", "field"])
def test_aggregate_reconstruction_attacks_fail(mutation):
    from src.analysis.open_intelligence.brain import (
        run_intelligence_brain,
        validate_intelligence_brain_result,
    )

    result = run_intelligence_brain(intent())
    if mutation == "replace":
        forged = replace(result)
    elif mutation == "copy":
        forged = copy.copy(result)
    elif mutation == "deepcopy":
        with pytest.raises(TypeError, match="cannot be copied"):
            copy.deepcopy(result)
        return
    elif mutation == "pickle":
        with pytest.raises(TypeError, match="cannot be serialized"):
            pickle.dumps(result)
        return
    else:
        object.__setattr__(result, "result_digest", "0" * 64)
        forged = result
    with pytest.raises(ValueError, match="brain result authority"):
        validate_intelligence_brain_result(forged)


def test_unapproved_signal_fails_before_roles():
    from src.analysis.open_intelligence.brain import run_intelligence_brain

    with pytest.raises(ValueError, match="signal unavailable"):
        run_intelligence_brain(intent("sig_" + "b" * 64))
