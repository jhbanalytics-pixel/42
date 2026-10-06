from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest


def _intent(investigation_id: str = "inv_fixture_01"):
    from src.analysis.open_intelligence.brain_contract import IntelligenceBrainIntent

    return IntelligenceBrainIntent(
        "intelligence_brain_v1",
        investigation_id,
        "sig_" + "a" * 64,
        "investigation",
        "What changed?",
        None,
    )


def _task_payload() -> dict[str, object]:
    path = Path("tests/fixtures/open_intelligence/brain_v1/task_01_emerging_without_keyword.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _review_record(review_id: str):
    from src.analysis.open_intelligence.brain_evaluation import (
        BrainScoreVector,
        EvaluationReviewRecord,
        pairing_key,
    )

    payload = _task_payload()
    key = pairing_key(
        payload["task_id"],
        "task_01_emerging_without_keyword.json",
        payload["baseline_result_digest"],
        payload["candidate_result_digest"],
    )
    return EvaluationReviewRecord(
        payload["task_id"],
        "task_01_emerging_without_keyword.json",
        key,
        payload["baseline_result_digest"],
        payload["candidate_result_digest"],
        BrainScoreVector(*(3 for _ in range(10))),
        BrainScoreVector(*(4 for _ in range(10))),
        100,
        90,
        0,
        0,
        0,
        0,
        0,
        review_id,
        (),
        "pending",
    )


@pytest.mark.parametrize(
    "count_field",
    [
        "unsupported_claim_count",
        "unresolved_citation_count",
        "demographic_violation_count",
        "future_leak_count",
        "pre_gate_model_call_count",
    ],
)
@pytest.mark.parametrize("invalid_value", [True, -1, 1.5, "0", 1])
def test_review_pair_rejects_invalid_or_nonzero_automated_counts(count_field, invalid_value):
    from src.analysis.open_intelligence.brain_evaluation import evaluate_review_pair

    with pytest.raises(ValueError, match="automated failure count"):
        replace(_review_record("review_1"), **{count_field: invalid_value})
    first = _review_record("review_1")
    object.__setattr__(first, count_field, invalid_value)
    second = _review_record("review_2")
    with pytest.raises(ValueError, match="automated failure count"):
        evaluate_review_pair(first, second)


@pytest.mark.parametrize("digest_value", [None, 7, True, ("a",)])
def test_review_pair_bounds_non_string_digest_errors(digest_value):
    from src.analysis.open_intelligence.brain_evaluation import evaluate_review_pair

    first = replace(_review_record("review_1"), baseline_result_digest=digest_value)
    second = _review_record("review_2")
    with pytest.raises(ValueError, match="output digest"):
        evaluate_review_pair(first, second)


def test_task_output_rejects_foreign_canonical_evidence():
    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.brain_evaluation import _validate_task_fixture

    payload = _task_payload()
    output = payload["candidate_output"]
    foreign = "ev_" + "f" * 64
    output["claims"][0]["evidence_ids"] = [foreign]
    output["citations"][0]["required_evidence_ids"] = [foreign]
    output["citations"][0]["cited_evidence_ids"] = [foreign]
    payload["candidate_result_digest"] = canonical_digest(output)
    with pytest.raises(ValueError, match="evidence universe"):
        _validate_task_fixture(payload["task_id"], payload)


def test_task_output_rejects_rehashed_foreign_evidence_universe():
    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.brain_evaluation import _validate_task_fixture

    payload = _task_payload()
    foreign = "ev_" + "f" * 64
    payload["evidence_universe_ids"] = [foreign]
    for lane in ("baseline_output", "candidate_output"):
        output = payload[lane]
        output["claims"][0]["evidence_ids"] = [foreign]
        output["citations"][0]["required_evidence_ids"] = [foreign]
        output["citations"][0]["cited_evidence_ids"] = [foreign]
        payload[lane.replace("output", "result_digest")] = canonical_digest(output)
    with pytest.raises(ValueError, match="evidence universe"):
        _validate_task_fixture(payload["task_id"], payload)


@pytest.mark.parametrize("field", ["required_evidence_ids", "cited_evidence_ids"])
def test_task_output_rejects_duplicate_citation_evidence(field):
    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.brain_evaluation import _validate_task_fixture

    payload = _task_payload()
    output = payload["candidate_output"]
    output["citations"][0][field] *= 2
    payload["candidate_result_digest"] = canonical_digest(output)
    with pytest.raises(ValueError, match="citation"):
        _validate_task_fixture(payload["task_id"], payload)


@pytest.mark.parametrize("claim_id", [" claim_01", "claim_01 ", "claim 01", "claim/01"])
def test_task_output_rejects_whitespace_or_malformed_claim_ids(claim_id):
    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.brain_evaluation import _validate_task_fixture

    payload = _task_payload()
    output = payload["candidate_output"]
    output["claims"][0]["claim_id"] = claim_id
    output["citations"][0]["claim_id"] = claim_id
    payload["candidate_result_digest"] = canonical_digest(output)
    with pytest.raises(ValueError, match="claim"):
        _validate_task_fixture(payload["task_id"], payload)


def _mutable_registry(module):
    return {key: dict(value) for key, value in module._STORED_INVESTIGATIONS.items()}


def test_runtime_rejects_injected_investigation_registry(monkeypatch):
    from src.analysis.open_intelligence import brain_authority

    registry = _mutable_registry(brain_authority)
    registry["inv_foreign_not_stored"] = deepcopy(registry["inv_fixture_01"])
    monkeypatch.setattr(brain_authority, "_STORED_INVESTIGATIONS", registry)
    with pytest.raises(ValueError, match="investigation unavailable"):
        brain_authority._issue_brain_runtime_request(_intent("inv_foreign_not_stored"))


def test_runtime_rejects_nested_stored_frame_drift(monkeypatch):
    from src.analysis.open_intelligence import brain_authority

    registry = _mutable_registry(brain_authority)
    monkeypatch.setattr(brain_authority, "_STORED_INVESTIGATIONS", registry)
    runtime = brain_authority._issue_brain_runtime_request(_intent())
    registry["inv_fixture_01"]["market_scope"] = ("ke",)
    with pytest.raises(ValueError, match="runtime authority"):
        brain_authority._validate_brain_runtime_request(runtime)


def _graph_inputs():
    from src.analysis.open_intelligence import brain

    result = brain.run_intelligence_brain(_intent())
    authority = brain._RESULT_AUTHORITIES[id(result)]
    return result, authority[4], authority[5]


def test_graph_contains_every_observer_and_why_now_support_membership():
    result, _snapshot, _roles = _graph_inputs()
    triples = {
        (edge.from_node_id, edge.to_node_id, edge.edge_type) for edge in result.evidence_graph.edges
    }
    expected = {
        (evidence_id, result.observer.what_changed_claim_id, "supports")
        for observation in result.observer.observations
        for evidence_id in observation.evidence_ids
    }
    expected.update(
        (evidence_id, result.analyst.why_now.claim_id, "supports")
        for evidence_id in result.analyst.why_now.evidence_ids
    )
    assert expected.issubset(triples)


def test_graph_validator_is_independent_from_builder_helper(monkeypatch):
    from src.analysis.open_intelligence import brain_graph

    _result, snapshot, roles = _graph_inputs()
    original = brain_graph._expected

    def incomplete(value_snapshot, value_roles):
        nodes, edges = original(value_snapshot, value_roles)
        return nodes, edges[:-1]

    monkeypatch.setattr(brain_graph, "_expected", incomplete)
    graph = brain_graph.build_intelligence_evidence_graph(snapshot, roles)
    with pytest.raises(ValueError, match="graph incomplete"):
        brain_graph.validate_intelligence_evidence_graph(graph, snapshot, roles)
