from __future__ import annotations

import json
from pathlib import Path

import pytest


def _task_payload():
    path = Path("tests/fixtures/open_intelligence/brain_v1/task_01_emerging_without_keyword.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _review_record(baseline_digest, candidate_digest, review_id):
    from src.analysis.open_intelligence.brain_evaluation import (
        BrainScoreVector,
        EvaluationReviewRecord,
        pairing_key,
    )

    return EvaluationReviewRecord(
        "emerging_without_keyword",
        "task_01_emerging_without_keyword.json",
        pairing_key(
            "emerging_without_keyword",
            "task_01_emerging_without_keyword.json",
            baseline_digest,
            candidate_digest,
        ),
        baseline_digest,
        candidate_digest,
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


def intent(investigation_id="inv_fixture_01"):
    from src.analysis.open_intelligence.brain_contract import IntelligenceBrainIntent

    return IntelligenceBrainIntent(
        "intelligence_brain_v1",
        investigation_id,
        "sig_" + "a" * 64,
        "investigation",
        "What changed?",
        None,
    )


def test_foreign_investigation_fails_before_snapshot():
    from src.analysis.open_intelligence.brain_authority import _issue_brain_runtime_request

    with pytest.raises(ValueError, match="investigation unavailable"):
        _issue_brain_runtime_request(intent("inv_foreign_not_stored"))


@pytest.mark.parametrize("mutation", ["missing_digest", "equal_digests"])
def test_evaluation_rejects_missing_or_forged_output_digest(mutation):
    from src.analysis.open_intelligence.brain_evaluation import _validate_task_fixture

    path = Path("tests/fixtures/open_intelligence/brain_v1/task_01_emerging_without_keyword.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "missing_digest":
        payload["candidate_result_digest"] = ""
    else:
        payload["candidate_result_digest"] = payload["baseline_result_digest"]
    with pytest.raises(ValueError, match="output digest"):
        _validate_task_fixture(payload["task_id"], payload)


def test_evaluation_derives_facts_from_complete_outputs():
    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.brain_evaluation import _validate_task_fixture

    path = Path("tests/fixtures/open_intelligence/brain_v1/task_01_emerging_without_keyword.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    validated = _validate_task_fixture(payload["task_id"], payload)
    assert validated["baseline_result_digest"] == canonical_digest(validated["baseline_output"])
    assert validated["candidate_result_digest"] == canonical_digest(validated["candidate_output"])
    assert validated["derived_violations"] == ()
    assert validated["derived_citation_failures"] == ()


@pytest.mark.parametrize(
    ("mutation", "baseline_digest", "candidate_digest"),
    [
        ("missing_baseline", "", "1" * 64),
        ("missing_candidate", "1" * 64, ""),
        ("equal", "1" * 64, "1" * 64),
        ("uppercase", "A" * 64, "b" * 64),
        ("short", "a" * 63, "b" * 64),
        ("long", "a" * 65, "b" * 64),
        ("nonhex", "g" * 64, "b" * 64),
        ("foreign_valid", "c" * 64, "d" * 64),
    ],
)
def test_review_pair_rejects_unbound_or_malformed_output_digests(
    mutation, baseline_digest, candidate_digest
):
    from src.analysis.open_intelligence.brain_evaluation import evaluate_review_pair

    first = _review_record(baseline_digest, candidate_digest, "review_1")
    second = _review_record(baseline_digest, candidate_digest, "review_2")
    with pytest.raises(ValueError, match="output digest"):
        evaluate_review_pair(first, second)


@pytest.mark.parametrize(
    "mutation",
    [
        "unexpected_output_field",
        "missing_output_field",
        "empty_statement",
        "empty_claim_evidence",
        "empty_citations",
        "citation_without_evidence",
        "claim_without_citation",
        "citation_without_claim",
    ],
)
def test_automated_evaluation_rejects_incomplete_output_structure(mutation):
    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.brain_evaluation import _validate_task_fixture

    payload = _task_payload()
    output = payload["candidate_output"]
    if mutation == "unexpected_output_field":
        output["invented"] = "value"
    elif mutation == "missing_output_field":
        output.pop("violations")
    elif mutation == "empty_statement":
        output["claims"][0]["statement"] = ""
    elif mutation == "empty_claim_evidence":
        output["claims"][0]["evidence_ids"] = []
        output["citations"][0]["required_evidence_ids"] = []
        output["citations"][0]["cited_evidence_ids"] = []
    elif mutation == "empty_citations":
        output["citations"] = []
    elif mutation == "citation_without_evidence":
        output["citations"][0]["required_evidence_ids"] = []
        output["citations"][0]["cited_evidence_ids"] = []
    elif mutation == "claim_without_citation":
        output["claims"].append(
            {
                "claim_id": "claim_extra",
                "statement": "Extra unsupported claim.",
                "evidence_ids": ["ev_" + "2" * 64],
            }
        )
    else:
        output["citations"][0]["claim_id"] = "claim_foreign"
    payload["candidate_result_digest"] = canonical_digest(output)
    with pytest.raises(ValueError):
        _validate_task_fixture(payload["task_id"], payload)


def test_graph_contains_all_authoritative_membership_edges():
    from src.analysis.open_intelligence.brain import run_intelligence_brain

    result = run_intelligence_brain(intent())
    graph = result.evidence_graph
    triples = {(edge.from_node_id, edge.to_node_id, edge.edge_type) for edge in graph.edges}
    expected = set()
    leading = result.analyst.leading_hypothesis
    expected.update(
        (evidence, leading["hypothesis_id"], "supports") for evidence in leading["evidence_ids"]
    )
    for rival in result.skeptic.rival_hypotheses:
        expected.update(
            (evidence, rival.hypothesis_id, "supports")
            for evidence in rival.supporting_evidence_ids
        )
        expected.update(
            (evidence, rival.hypothesis_id, "challenges")
            for evidence in rival.challenging_evidence_ids
        )
    tension = result.strategist.cultural_tension
    expected.update(
        (evidence, tension.tension_id, "supports") for evidence in tension.supporting_evidence_ids
    )
    expected.update(
        (evidence, tension.tension_id, "challenges")
        for evidence in tension.challenging_evidence_ids
    )
    opportunity = result.strategist.opportunity
    expected.update(
        (evidence, opportunity.opportunity_id, "supports")
        for evidence in opportunity.supporting_evidence_ids
    )
    expected.update(
        (evidence, opportunity.opportunity_id, "challenges")
        for evidence in opportunity.challenging_evidence_ids
    )
    expected.update(
        (citation.clause_id, citation.claim_id, "depends_on")
        for citation in result.editor.citation_map
    )
    assert expected.issubset(triples)
