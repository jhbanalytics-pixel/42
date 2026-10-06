from __future__ import annotations

import copy
import pickle

import pytest


def intent():
    from src.analysis.open_intelligence.brain_contract import IntelligenceBrainIntent

    return IntelligenceBrainIntent(
        "intelligence_brain_v1",
        "inv_fixture_01",
        "sig_" + "a" * 64,
        "investigation",
        "What changed?",
        None,
    )


def test_runtime_authority_rejects_construction_and_copy():
    from src.analysis.open_intelligence.brain_authority import (
        _IntelligenceBrainRuntimeRequest,
        _issue_brain_runtime_request,
        _validate_brain_runtime_request,
    )

    runtime = _issue_brain_runtime_request(intent())
    assert _validate_brain_runtime_request(runtime) is runtime
    with pytest.raises(ValueError, match="runtime authority"):
        _IntelligenceBrainRuntimeRequest()
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(runtime)


def test_snapshot_authority_rejects_rehashed_public_drift():
    from src.analysis.open_intelligence.brain_authority import (
        _issue_brain_evidence_snapshot,
        _issue_brain_runtime_request,
        _validate_brain_evidence_snapshot,
    )

    runtime = _issue_brain_runtime_request(intent())
    snapshot = _issue_brain_evidence_snapshot(runtime)
    assert _validate_brain_evidence_snapshot(runtime, snapshot) is snapshot
    object.__setattr__(snapshot, "market_ids", ("ke",))
    object.__setattr__(snapshot, "snapshot_digest", "0" * 64)
    with pytest.raises(ValueError, match="snapshot authority"):
        _validate_brain_evidence_snapshot(runtime, snapshot)


def test_snapshot_copy_and_foreign_runtime_fail():
    from src.analysis.open_intelligence.brain_authority import (
        _issue_brain_evidence_snapshot,
        _issue_brain_runtime_request,
        _validate_brain_evidence_snapshot,
    )

    first = _issue_brain_runtime_request(intent())
    second = _issue_brain_runtime_request(intent())
    snapshot = _issue_brain_evidence_snapshot(first)
    with pytest.raises(ValueError, match="snapshot authority"):
        _validate_brain_evidence_snapshot(second, snapshot)
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(snapshot)
