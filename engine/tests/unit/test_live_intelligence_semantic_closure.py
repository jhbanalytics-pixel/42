from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "open_intelligence" / "v2"
JOURNEY_IDS = (
    "journey_crisis_affluent_za_v1",
    "journey_brand_south_africa_narratives_v1",
    "journey_election_brand_role_v1",
)
RESULT_FIELDS = (
    "observations",
    "interpretations",
    "inferences",
    "contradictions",
    "uncertainties",
    "historical_analogues",
    "recommendations",
    "clauses",
    "abstentions",
    "model_receipt_ids",
    "usage_receipt_ids",
    "human_review_required",
    "export_allowed",
)


def _load(journey_id: str) -> dict[str, object]:
    path = FIXTURE_ROOT / f"{journey_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_fixed_semantic_method_and_selector_are_code_only() -> None:
    from src.analysis.open_intelligence.brain_semantics import (
        LIVE_SEMANTIC_METHOD,
        live_semantic_selector_enabled,
    )

    assert LIVE_SEMANTIC_METHOD == {
        "method_id": "vertex_gemini_3_5_flash_evidence_synthesis_v1",
        "provider": "Google Vertex AI",
        "model": "gemini-3.5-flash",
        "location": "global",
        "maximum_model_calls": 1,
        "temperature": 0,
        "candidate_count": 1,
        "response_mime_type": "application/json",
    }
    assert live_semantic_selector_enabled() is False


def test_semantic_journey_fixture_contract_is_digest_bound() -> None:
    from src.contracts.open_intelligence_fixtures import load_live_intelligence_journeys

    package = load_live_intelligence_journeys()
    assert package.contract_version == "42_live_intelligence_semantic_closure_v1"
    assert package.fixture_count == 3
    assert tuple(item.fixture_id for item in package.fixtures) == JOURNEY_IDS
    assert all(item.fixture_kind == "live_intelligence_journey" for item in package.fixtures)
    assert len(package.manifest_sha256) == 64


@pytest.mark.parametrize("journey_id", JOURNEY_IDS)
def test_named_journeys_cross_the_public_live_brain_validator(journey_id: str) -> None:
    from src.analysis.open_intelligence.brain import evaluate_live_intelligence_journey

    fixture = _load(journey_id)
    result = evaluate_live_intelligence_journey(fixture)

    assert tuple(result) == RESULT_FIELDS
    assert fixture["semantic_selector_enabled"] is False
    assert fixture["model_calls"] == 0
    assert result["model_receipt_ids"] == []
    assert result["usage_receipt_ids"] == []
    assert result["export_allowed"] is False
    assert (
        result["recommendations"][0]["text"]
        == fixture["recommendation_authorities"][0]["semantic_preimage"]
    )


def test_crisis_journey_refuses_affluence_and_demographic_invention() -> None:
    from src.analysis.open_intelligence.brain import evaluate_live_intelligence_journey

    result = evaluate_live_intelligence_journey(_load(JOURNEY_IDS[0]))
    text = " ".join(item["statement"] for item in result["abstentions"])
    assert "affluence" in text.lower()
    assert "age" in text.lower()
    assert "gender" in text.lower()
    assert result["inferences"][0]["audience_lens_ids"] == ["lens_crisis_participation"]


def test_brand_south_africa_keeps_contradiction_and_transfer_limits_visible() -> None:
    from src.analysis.open_intelligence.brain import evaluate_live_intelligence_journey

    result = evaluate_live_intelligence_journey(_load(JOURNEY_IDS[1]))
    analogue = result["historical_analogues"][0]
    recommendation = result["recommendations"][0]
    assert analogue["transfer_limits"] == [
        "market_context_not_transferable",
        "source_mix_not_transferable",
    ]
    assert recommendation["contradiction_ids"] == ["con_bsa_counter_narrative"]
    assert recommendation["analogue_ids"] == [analogue["analogue_id"]]
    assert result["clauses"][0]["citation_state"] == "exact"


def test_election_journey_is_review_gated_and_nonpolling() -> None:
    from src.analysis.open_intelligence.brain import evaluate_live_intelligence_journey

    result = evaluate_live_intelligence_journey(_load(JOURNEY_IDS[2]))
    reasons = {reason for item in result["abstentions"] for reason in item["reason_codes"]}
    assert result["human_review_required"] is True
    assert result["export_allowed"] is False
    assert {
        "polling_not_measured",
        "voting_intention_not_measured",
        "majority_not_measured",
    } <= reasons


@pytest.mark.parametrize("journey_id", JOURNEY_IDS)
@pytest.mark.parametrize(
    "mutation",
    [
        "receipt",
        "evidence_row",
        "contradiction",
        "transfer_limit",
        "audience_method",
        "recommendation_authority",
        "citation",
    ],
)
def test_each_acceptance_journey_fails_when_authority_is_removed_or_changed(
    journey_id: str, mutation: str
) -> None:
    from src.analysis.open_intelligence.brain import evaluate_live_intelligence_journey

    fixture = copy.deepcopy(_load(journey_id))
    if mutation == "receipt":
        fixture["evidence"][0]["source_receipt_id"] += "_changed"
    elif mutation == "evidence_row":
        fixture["evidence"].pop()
    elif mutation == "contradiction":
        fixture["result"]["contradictions"].pop()
    elif mutation == "transfer_limit":
        fixture["result"]["historical_analogues"][0]["transfer_limits"].pop()
    elif mutation == "audience_method":
        fixture["audience_lenses"][0]["method_id"] = ""
    elif mutation == "recommendation_authority":
        fixture["recommendation_authorities"].pop()
    else:
        fixture["result"]["clauses"][0]["cited_evidence_ids"].pop()

    with pytest.raises(ValueError):
        evaluate_live_intelligence_journey(fixture)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("wrong_demographic_dimension", "audience"),
        ("inferred_gender", "audience"),
        ("missing_lens_method", "audience"),
        ("missing_lens_evidence", "audience"),
        ("changed_recommendation_text", "recommendation"),
        ("unsupported_recommendation_support", "recommendation"),
        ("irrelevant_clause_evidence", "citation"),
        ("missing_transfer_limits", "transfer"),
        ("future_analogue_evidence", "future"),
        ("missing_model_receipt", "model receipt"),
        ("unmetered_model_output", "usage receipt"),
        ("model_call_ceiling", "maximum model calls"),
        ("enabled_selector", "semantic selector"),
        ("schema_drift", "result fields"),
        ("enabled_election_export", "election export"),
    ],
)
def test_semantic_mutations_fail_at_the_public_boundary(mutation: str, error: str) -> None:
    from src.analysis.open_intelligence.brain import evaluate_live_intelligence_journey

    fixture = copy.deepcopy(_load(JOURNEY_IDS[1]))
    if mutation in {"wrong_demographic_dimension", "inferred_gender"}:
        fixture = copy.deepcopy(_load(JOURNEY_IDS[0]))
        lens = fixture["audience_lenses"][0]
        lens["dimension"] = "age" if mutation == "wrong_demographic_dimension" else "gender"
    elif mutation == "missing_lens_method":
        fixture = copy.deepcopy(_load(JOURNEY_IDS[0]))
        fixture["audience_lenses"][0]["method_id"] = ""
    elif mutation == "missing_lens_evidence":
        fixture = copy.deepcopy(_load(JOURNEY_IDS[0]))
        fixture["audience_lenses"][0]["evidence_ids"] = []
    elif mutation == "changed_recommendation_text":
        fixture["result"]["recommendations"][0]["text"] += " Changed."
    elif mutation == "unsupported_recommendation_support":
        fixture["result"]["recommendations"][0]["supporting_claim_ids"] = ["clm_unknown"]
    elif mutation == "irrelevant_clause_evidence":
        fixture["result"]["clauses"][0]["cited_evidence_ids"].append(
            fixture["evidence"][-1]["evidence_id"]
        )
    elif mutation == "missing_transfer_limits":
        fixture["result"]["historical_analogues"][0]["transfer_limits"] = []
    elif mutation == "future_analogue_evidence":
        fixture["result"]["historical_analogues"][0]["as_of"] = "2026-09-01T00:00:00Z"
    elif mutation == "missing_model_receipt":
        fixture["model_calls"] = 1
    elif mutation == "unmetered_model_output":
        fixture["model_calls"] = 1
        fixture["result"]["model_receipt_ids"] = ["model_receipt_01"]
    elif mutation == "model_call_ceiling":
        fixture["model_calls"] = 2
        fixture["result"]["model_receipt_ids"] = ["model_receipt_01"]
        fixture["result"]["usage_receipt_ids"] = ["usage_receipt_01"]
    elif mutation == "enabled_selector":
        fixture["semantic_selector_enabled"] = True
    elif mutation == "schema_drift":
        fixture["result"]["summary"] = "not in the contract"
    else:
        fixture = copy.deepcopy(_load(JOURNEY_IDS[2]))
        fixture["result"]["export_allowed"] = True

    with pytest.raises(ValueError, match=error):
        evaluate_live_intelligence_journey(fixture)


def test_measured_age_requires_matching_observation_authority() -> None:
    from src.analysis.open_intelligence.brain import evaluate_live_intelligence_journey

    fixture = copy.deepcopy(_load(JOURNEY_IDS[0]))
    lens = fixture["audience_lenses"][0]
    lens.update(
        {
            "basis": "measured",
            "dimension": "age",
            "segment": "people aged 25 to 34",
            "method_id": "measured_age_observation_v1",
        }
    )
    fixture["evidence"][0]["measurement_authorities"] = [
        {"dimension": "age", "method_id": "measured_age_observation_v1"}
    ]

    evaluate_live_intelligence_journey(fixture)
    fixture["evidence"][0]["measurement_authorities"][0]["dimension"] = "gender"
    with pytest.raises(ValueError, match="audience measurement authority"):
        evaluate_live_intelligence_journey(fixture)


def test_canary_recommendation_text_is_bound_to_the_approved_plan_preimage() -> None:
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.open_question_answer import _validate_answer

    from tests.unit.test_gemini_canary_callers import answering_output, approved_record

    stage = stage_manifest("open_question_answer:answering:golden_01_emerging_without_keyword")
    output = answering_output(stage["envelope"])
    output["recommendations"][0]["text"] = stage["envelope"]["approved_plan"]["decision"]
    _validate_answer(output, stage["envelope"], approved_record(stage))
    output["recommendations"][0]["text"] = "Invented recommendation prose."
    with pytest.raises(ValueError, match="recommendation semantic preimage"):
        _validate_answer(output, stage["envelope"], approved_record(stage))


def test_journey_cannot_supply_and_rehash_its_own_recommendation_authority() -> None:
    from src.analysis.open_intelligence.brain import evaluate_live_intelligence_journey
    from src.analysis.open_intelligence.brain_semantics import (
        recommendation_authority_digest,
    )

    fixture = copy.deepcopy(_load(JOURNEY_IDS[1]))
    authority = fixture["recommendation_authorities"][0]
    authority["semantic_preimage"] = "Invented recommendation with valid local identities."
    authority["semantic_digest"] = recommendation_authority_digest(authority)
    fixture["result"]["recommendations"][0]["text"] = authority["semantic_preimage"]

    with pytest.raises(ValueError, match="approved recommendation authority"):
        evaluate_live_intelligence_journey(fixture)
