from __future__ import annotations

import copy
import json
import pickle
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

import pytest


def _intent():
    from src.analysis.open_intelligence.brain_contract import IntelligenceBrainIntent

    return IntelligenceBrainIntent(
        "intelligence_brain_v1",
        "inv_fixture_01",
        "sig_" + "a" * 64,
        "investigation",
        "What changed?",
        None,
    )


def _snapshot_context():
    from src.analysis.open_intelligence.brain_authority import (
        _issue_brain_evidence_snapshot,
        _issue_brain_runtime_request,
    )

    runtime = _issue_brain_runtime_request(_intent())
    return runtime, _issue_brain_evidence_snapshot(runtime)


def _coherently_forge_snapshot(snapshot) -> None:
    from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest

    authority = snapshot._authority
    private = list(authority.private)
    payload = json.loads(private[2].decode("utf-8"))
    payload["measurements"][0]["value"] = 99.0
    forged_payload = canonical_bytes(payload)
    forged_projection_digest = canonical_digest(payload)
    public = dict(private[1])
    public["evidence_state"] = "forged"
    public["source_projection_digest"] = forged_projection_digest
    public_without_ids = {
        key: value for key, value in public.items() if key not in {"snapshot_id", "snapshot_digest"}
    }
    public["snapshot_id"] = "bsp_" + canonical_digest(public_without_ids)
    public_without_digest = {
        key: value for key, value in public.items() if key != "snapshot_digest"
    }
    public["snapshot_digest"] = canonical_digest(public_without_digest)
    private[1] = public
    private[2] = forged_payload
    private[3] = forged_projection_digest
    authority.private = tuple(private)
    for key, value in public.items():
        object.__setattr__(snapshot, key, value)


def test_snapshot_payload_returns_independent_nested_copies():
    from src.analysis.open_intelligence.brain_authority import _snapshot_payload

    _runtime, snapshot = _snapshot_context()
    first = _snapshot_payload(snapshot)
    second = _snapshot_payload(snapshot)
    assert first is not second
    assert first["historical"] is not second["historical"]
    assert first["measurements"] is not second["measurements"]
    assert first["measurements"][0] is not second["measurements"][0]


def test_post_issuance_nested_mutation_cannot_reach_consuming_roles():
    from src.analysis.open_intelligence.brain_analyst import _run_analyst
    from src.analysis.open_intelligence.brain_authority import (
        _snapshot_measurements,
        _snapshot_payload,
    )
    from src.analysis.open_intelligence.brain_editor import _run_editor
    from src.analysis.open_intelligence.brain_historian import _run_historian
    from src.analysis.open_intelligence.brain_observer import _run_observer
    from src.analysis.open_intelligence.brain_skeptic import _run_skeptic
    from src.analysis.open_intelligence.brain_strategist import _run_strategist

    runtime, snapshot = _snapshot_context()
    payload = _snapshot_payload(snapshot)
    payload["historical"]["historical_object_id"] = "hist_foreign"
    payload["historical"]["source_first_observed_at"] = "2026-08-03T00:00:00.000000Z"
    payload["measurements"][0]["measurement_id"] = "bm_forged"
    payload["measurements"][0]["value"] = 99.0
    payload["evidence_ids"][0] = "ev_" + "c" * 64

    measurements = _snapshot_measurements(snapshot)
    observer = _run_observer(runtime, snapshot)
    historian = _run_historian(runtime, snapshot, observer)
    analyst = _run_analyst(runtime, snapshot, observer, historian)
    skeptic = _run_skeptic(runtime, snapshot, observer, historian, analyst)
    strategist = _run_strategist(runtime, snapshot, observer, historian, analyst, skeptic)
    editor = _run_editor(
        runtime,
        snapshot,
        observer,
        historian,
        analyst,
        skeptic,
        strategist,
    )

    source = _snapshot_payload(snapshot)
    assert source["historical"]["historical_object_id"] == "hist_fixture_1"
    assert source["historical"]["source_first_observed_at"] == "2026-08-02T00:00:00.000000Z"
    assert source["measurements"][0]["value"] == 0.74
    assert source["evidence_ids"][0] == "ev_" + "a" * 64
    assert measurements[0].value == 0.74
    assert observer.observations[0].evidence_ids == snapshot.evidence_ids
    assert historian.selected_analogue.historical_object_id == "hist_fixture_1"
    assert historian.selected_analogue.source_first_observed_at == datetime(2026, 8, 2, tzinfo=UTC)
    assert analyst.why_now.novelty_measurement_id == "bm_novelty"
    assert skeptic.result_digest
    assert strategist.result_digest
    assert editor.result_digest


def test_snapshot_validation_rejects_frozen_source_digest_drift():
    from src.analysis.open_intelligence.brain_authority import _validate_brain_evidence_snapshot
    from src.analysis.open_intelligence.brain_contract import canonical_bytes

    runtime, snapshot = _snapshot_context()
    authority = snapshot._authority
    private = list(authority.private)
    frozen = private[2]
    if isinstance(frozen, bytes):
        payload = json.loads(frozen.decode("utf-8"))
        payload["historical"]["historical_object_id"] = "hist_foreign"
        private[2] = canonical_bytes(payload)
    else:
        payload = deepcopy(frozen)
        payload["historical"]["historical_object_id"] = "hist_foreign"
        private[2] = payload
    authority.private = tuple(private)
    with pytest.raises(ValueError, match="snapshot authority"):
        _validate_brain_evidence_snapshot(runtime, snapshot)


def test_snapshot_validation_rejects_coherent_private_and_public_forgery():
    from src.analysis.open_intelligence.brain_authority import _validate_brain_evidence_snapshot

    runtime, snapshot = _snapshot_context()
    _coherently_forge_snapshot(snapshot)
    with pytest.raises(ValueError, match="snapshot authority"):
        _validate_brain_evidence_snapshot(runtime, snapshot)


def test_snapshot_measurements_reject_coherent_private_and_public_forgery():
    from src.analysis.open_intelligence.brain_authority import _snapshot_measurements

    _runtime, snapshot = _snapshot_context()
    _coherently_forge_snapshot(snapshot)
    with pytest.raises(ValueError, match="snapshot authority"):
        _snapshot_measurements(snapshot)


@pytest.mark.parametrize("operation", [copy.copy, copy.deepcopy, pickle.dumps])
def test_snapshot_cannot_be_copied_or_serialized(operation):
    _runtime, snapshot = _snapshot_context()
    with pytest.raises(TypeError, match="authority cannot be"):
        operation(snapshot)


def _issued_root_functions(brain_authority):
    issue = getattr(brain_authority, "_issue_snapshot_root", None)
    lookup = getattr(brain_authority, "_lookup_snapshot_root", None)
    assert callable(issue), "issued snapshot root issue function is missing"
    assert callable(lookup), "issued snapshot root lookup function is missing"
    return issue, lookup


def _coherent_same_owner_root(snapshot, root):
    from src.analysis.open_intelligence.brain_contract import canonical_digest

    private = snapshot._authority.private
    public = private[1]
    return replace(
        root,
        canonical_payload=private[2],
        projection_digest=private[3],
        public_field_digest=canonical_digest(public),
        snapshot_id=public["snapshot_id"],
        snapshot_digest=public["snapshot_digest"],
    )


def test_in_place_same_owner_root_replacement_cannot_authorize_validation():
    from src.analysis.open_intelligence import brain_authority

    runtime, snapshot = _snapshot_context()
    _coherently_forge_snapshot(snapshot)
    registry = getattr(brain_authority, "_ISSUED_SNAPSHOT_ROOTS", None)
    original_root = registry.get(id(snapshot)) if isinstance(registry, dict) else None
    if original_root is not None:
        registry[id(snapshot)] = _coherent_same_owner_root(snapshot, original_root)
    try:
        with pytest.raises(ValueError, match="snapshot authority"):
            brain_authority._validate_brain_evidence_snapshot(runtime, snapshot)
    finally:
        if original_root is not None:
            registry[id(snapshot)] = original_root


def test_in_place_same_owner_root_replacement_cannot_authorize_measurements():
    from src.analysis.open_intelligence import brain_authority

    _runtime, snapshot = _snapshot_context()
    _coherently_forge_snapshot(snapshot)
    registry = getattr(brain_authority, "_ISSUED_SNAPSHOT_ROOTS", None)
    original_root = registry.get(id(snapshot)) if isinstance(registry, dict) else None
    if original_root is not None:
        registry[id(snapshot)] = _coherent_same_owner_root(snapshot, original_root)
    try:
        with pytest.raises(ValueError, match="snapshot authority"):
            brain_authority._snapshot_measurements(snapshot)
    finally:
        if original_root is not None:
            registry[id(snapshot)] = original_root


def test_issued_root_has_no_writable_registry_alias():
    from src.analysis.open_intelligence import brain_authority

    assert not isinstance(getattr(brain_authority, "_ISSUED_SNAPSHOT_ROOTS", None), dict)
    assert not isinstance(getattr(brain_authority, "_ISSUED_SNAPSHOT_ROOT_REGISTRY", None), dict)


def test_issued_root_rejects_duplicate_and_same_owner_replacement():
    from src.analysis.open_intelligence import brain_authority

    _runtime, snapshot = _snapshot_context()
    issue, lookup = _issued_root_functions(brain_authority)
    root = lookup(snapshot)
    with pytest.raises(ValueError, match="already issued"):
        issue(snapshot, root)
    with pytest.raises(ValueError, match="already issued"):
        issue(snapshot, replace(root, projection_digest="f" * 64))


def test_issued_root_rejects_foreign_owner():
    from src.analysis.open_intelligence import brain_authority

    _runtime, snapshot = _snapshot_context()
    issue, lookup = _issued_root_functions(brain_authority)
    with pytest.raises(ValueError, match="foreign owner"):
        issue(object(), lookup(snapshot))


def test_snapshot_validation_rejects_caller_supplied_root_lookup():
    from src.analysis.open_intelligence import brain_authority

    runtime, snapshot = _snapshot_context()
    _coherently_forge_snapshot(snapshot)
    _issue, lookup = _issued_root_functions(brain_authority)
    forged_root = _coherent_same_owner_root(snapshot, lookup(snapshot))
    with pytest.raises(TypeError):
        brain_authority._validate_brain_evidence_snapshot(
            runtime, snapshot, lambda _owner: forged_root
        )


def test_snapshot_payload_rejects_caller_supplied_root_lookup():
    from src.analysis.open_intelligence import brain_authority

    _runtime, snapshot = _snapshot_context()
    _coherently_forge_snapshot(snapshot)
    _issue, lookup = _issued_root_functions(brain_authority)
    forged_root = _coherent_same_owner_root(snapshot, lookup(snapshot))
    with pytest.raises(TypeError):
        brain_authority._snapshot_payload(snapshot, lambda _owner: forged_root)


def test_snapshot_validation_rejects_missing_issued_root():
    from src.analysis.open_intelligence import brain_authority

    runtime, snapshot = _snapshot_context()
    unissued = object.__new__(type(snapshot))
    for field in type(snapshot).__slots__:
        if field != "_authority":
            object.__setattr__(unissued, field, getattr(snapshot, field))
    object.__setattr__(
        unissued,
        "_authority",
        brain_authority._Authority(
            brain_authority._SNAPSHOT_CAPABILITY,
            unissued,
            snapshot._authority.private,
        ),
    )
    with pytest.raises(ValueError, match="snapshot authority"):
        brain_authority._validate_brain_evidence_snapshot(runtime, unissued)


def test_historian_rejects_analogue_outside_snapshot_ids(tmp_path, monkeypatch):
    from src.analysis.open_intelligence import brain_authority
    from src.analysis.open_intelligence.brain_historian import _run_historian
    from src.analysis.open_intelligence.brain_observer import _run_observer

    payload = json.loads(brain_authority._FIXTURE_PATH.read_text(encoding="utf-8"))
    payload["historical"]["historical_object_id"] = "hist_foreign"
    fixture = tmp_path / "evidence_snapshot.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(brain_authority, "_FIXTURE_PATH", fixture)
    runtime = brain_authority._issue_brain_runtime_request(_intent())
    snapshot = brain_authority._issue_brain_evidence_snapshot(runtime)
    observer = _run_observer(runtime, snapshot)
    with pytest.raises(ValueError, match="Historical analogue unavailable"):
        _run_historian(runtime, snapshot, observer)
