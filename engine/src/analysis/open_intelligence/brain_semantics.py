"""Pure semantic authority and live Intelligence result validation."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from types import MappingProxyType

from src.analysis.open_intelligence.brain_contract import canonical_digest

LIVE_SEMANTIC_METHOD = {
    "method_id": "vertex_gemini_3_5_flash_evidence_synthesis_v1",
    "provider": "Google Vertex AI",
    "model": "gemini-3.5-flash",
    "location": "global",
    "maximum_model_calls": 1,
    "temperature": 0,
    "candidate_count": 1,
    "response_mime_type": "application/json",
}

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
AUDIENCE_LENS_FIELDS = (
    "lens_contract_version",
    "lens_id",
    "basis",
    "dimension",
    "segment",
    "source_receipt_ids",
    "window_start",
    "window_end",
    "method_id",
    "confidence",
    "evidence_ids",
    "limitations",
)
RECOMMENDATION_AUTHORITY_FIELDS = (
    "recommendation_authority_version",
    "recommendation_id",
    "semantic_preimage",
    "semantic_digest",
    "supporting_claim_ids",
    "evidence_ids",
    "contradiction_ids",
    "limitation_ids",
    "invalidation_condition",
    "human_review_required",
)
_JOURNEY_FIELDS = (
    "journey_id",
    "sanitized",
    "semantic_selector_enabled",
    "model_calls",
    "evidence",
    "audience_lenses",
    "recommendation_authorities",
    "result",
)
_EVIDENCE_FIELDS = (
    "evidence_id",
    "source_receipt_id",
    "observed_at",
    "claim_ids",
    "measurement_authorities",
)
_MEASUREMENT_AUTHORITY_FIELDS = ("dimension", "method_id")
_OBSERVATION_FIELDS = ("statement_id", "text", "evidence_ids", "source_receipt_ids")
_INTERPRETATION_FIELDS = (
    "statement_id",
    "text",
    "parent_observation_ids",
    "evidence_ids",
)
_INFERENCE_FIELDS = (
    "statement_id",
    "text",
    "method_id",
    "audience_lens_ids",
    "historical_analogue_ids",
    "evidence_ids",
)
_CONTRADICTION_FIELDS = ("contradiction_id", "statement", "claim_ids", "evidence_ids")
_UNCERTAINTY_FIELDS = ("uncertainty_id", "statement", "evidence_ids")
_ANALOGUE_FIELDS = (
    "analogue_id",
    "signal_id",
    "as_of",
    "cutoff_at",
    "market",
    "similarity_basis",
    "difference_codes",
    "evidence_ids",
    "transfer_limits",
)
_RECOMMENDATION_FIELDS = (
    "recommendation_id",
    "text",
    "supporting_claim_ids",
    "evidence_ids",
    "contradiction_ids",
    "limitation_ids",
    "invalidation_condition",
    "human_review_required",
    "analogue_ids",
)
_CLAUSE_FIELDS = (
    "clause_id",
    "clause_text",
    "claim_ids",
    "required_evidence_ids",
    "cited_evidence_ids",
    "citation_state",
)
_ABSTENTION_FIELDS = ("abstention_id", "statement", "reason_codes")
_DIMENSIONS = frozenset(
    {"age", "gender", "location", "behavior", "interest", "attitude", "participation"}
)
_INFERRED_DIMENSIONS = frozenset({"behavior", "interest", "attitude", "participation"})
_DEMOGRAPHIC_SEGMENT = re.compile(
    r"\b(?:women|woman|men|man|female|male|gender|gen\s*z|millennials?|"
    r"aged?\s+\d+|\d+\s*(?:to|-)\s*\d+)\b",
    re.IGNORECASE,
)
_TRANSFER_LIMITS = {
    "market_changed": "market_context_not_transferable",
    "source_mix_changed": "source_mix_not_transferable",
    "evidence_state_changed": "evidence_state_not_transferable",
    "time_regime_changed": "time_regime_not_transferable",
    "trajectory_changed": "time_regime_not_transferable",
    "diffusion_path_changed": "diffusion_path_not_transferable",
    "audience_basis_changed": "audience_basis_not_transferable",
}
APPROVED_JOURNEY_AUTHORITY_DIGESTS = MappingProxyType(
    {
        "journey_crisis_affluent_za_v1": (
            "d872307c680cd3a1124b03c8bf120f1185a593b974065d14c64cf53015ae876f"
        ),
        "journey_brand_south_africa_narratives_v1": (
            "5dc6e2496907be516d51fb0241a4088b7d4dc67a71e0d99c5bfc662297d03daf"
        ),
        "journey_election_brand_role_v1": (
            "1bca9430a090b67eda8222314ad96c1d941818ffb8198417e1d29334fdf62adf"
        ),
    }
)


def live_semantic_selector_enabled() -> bool:
    return False


def _exact(value: object, fields: Sequence[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or tuple(value) != tuple(fields):
        raise ValueError(f"{label} fields are invalid")
    return value


def _items(value: object, label: str, *, nonempty: bool = False) -> list[object]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{label} is invalid")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is invalid")
    return value


def _strings(value: object, label: str, *, nonempty: bool = False) -> list[str]:
    items = _items(value, label, nonempty=nonempty)
    if any(not isinstance(item, str) or not item for item in items) or len(items) != len(
        set(items)
    ):
        raise ValueError(f"{label} is invalid")
    return items


def _utc(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{label} must be closed UTC")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} must be closed UTC") from error
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{label} must be closed UTC")
    return parsed


def _authority_digest(value: Mapping[str, object]) -> str:
    return canonical_digest(
        {
            "semantic_preimage": value["semantic_preimage"],
            "supporting_claim_ids": value["supporting_claim_ids"],
            "evidence_ids": value["evidence_ids"],
            "contradiction_ids": value["contradiction_ids"],
            "limitation_ids": value["limitation_ids"],
        }
    )


def recommendation_authority_digest(value: Mapping[str, object]) -> str:
    _exact(value, RECOMMENDATION_AUTHORITY_FIELDS, "recommendation authority")
    return _authority_digest(value)


def _validate_evidence(value: object) -> tuple[dict[str, Mapping[str, object]], set[str]]:
    evidence = {}
    receipts = set()
    for raw in _items(value, "evidence", nonempty=True):
        item = _exact(raw, _EVIDENCE_FIELDS, "evidence")
        evidence_id = _text(item["evidence_id"], "evidence identity")
        receipt_id = _text(item["source_receipt_id"], "source receipt identity")
        if evidence_id in evidence or receipt_id in receipts:
            raise ValueError("evidence identity is invalid")
        _utc(item["observed_at"], "evidence observation")
        _strings(item["claim_ids"], "evidence claim identities", nonempty=True)
        for authority in _items(item["measurement_authorities"], "measurement authorities"):
            measured = _exact(authority, _MEASUREMENT_AUTHORITY_FIELDS, "measurement authority")
            if measured["dimension"] not in _DIMENSIONS or not _text(
                measured["method_id"], "measurement method"
            ):
                raise ValueError("audience measurement authority is invalid")
        evidence[evidence_id] = item
        receipts.add(receipt_id)
    return evidence, receipts


def _validate_lenses(
    value: object,
    evidence: Mapping[str, Mapping[str, object]],
    receipts: set[str],
) -> set[str]:
    lens_ids = set()
    for raw in _items(value, "audience lenses"):
        lens = _exact(raw, AUDIENCE_LENS_FIELDS, "audience lens")
        lens_id = _text(lens["lens_id"], "audience lens identity")
        if lens["lens_contract_version"] != "audience_lens_v2" or lens_id in lens_ids:
            raise ValueError("audience lens contract is invalid")
        basis = lens["basis"]
        dimension = lens["dimension"]
        method_id = _text(lens["method_id"], "audience method")
        segment = _text(lens["segment"], "audience segment")
        source_receipt_ids = _strings(
            lens["source_receipt_ids"], "audience source receipts", nonempty=True
        )
        evidence_ids = _strings(lens["evidence_ids"], "audience evidence", nonempty=True)
        start = _utc(lens["window_start"], "audience window start")
        end = _utc(lens["window_end"], "audience window end")
        confidence = lens["confidence"]
        if (
            basis not in {"measured", "inferred"}
            or dimension not in _DIMENSIONS
            or start > end
            or isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
            or not set(source_receipt_ids).issubset(receipts)
            or not set(evidence_ids).issubset(evidence)
            or not isinstance(lens["limitations"], list)
            or any(not isinstance(item, str) or not item for item in lens["limitations"])
        ):
            raise ValueError("audience lens is invalid")
        if any(
            not start <= _utc(evidence[item]["observed_at"], "audience observation") <= end
            for item in evidence_ids
        ) or not set(source_receipt_ids).issubset(
            {evidence[item]["source_receipt_id"] for item in evidence_ids}
        ):
            raise ValueError("audience evidence window is invalid")
        if basis == "inferred" and (
            dimension not in _INFERRED_DIMENSIONS or _DEMOGRAPHIC_SEGMENT.search(segment)
        ):
            raise ValueError("audience inference is invalid")
        if dimension in {"age", "gender"} and (
            basis != "measured"
            or not any(
                {"dimension": dimension, "method_id": method_id}
                in evidence[item]["measurement_authorities"]
                for item in evidence_ids
            )
        ):
            raise ValueError("audience measurement authority is invalid")
        lens_ids.add(lens_id)
    return lens_ids


def _validate_authorities(value: object, evidence_ids: set[str]) -> dict[str, Mapping[str, object]]:
    authorities = {}
    for raw in _items(value, "recommendation authorities"):
        authority = _exact(raw, RECOMMENDATION_AUTHORITY_FIELDS, "recommendation authority")
        recommendation_id = _text(authority["recommendation_id"], "recommendation identity")
        if (
            authority["recommendation_authority_version"] != "recommendation_authority_v1"
            or recommendation_id in authorities
            or authority["semantic_digest"] != _authority_digest(authority)
            or not set(
                _strings(
                    authority["evidence_ids"],
                    "recommendation authority evidence",
                    nonempty=True,
                )
            ).issubset(evidence_ids)
            or not _strings(
                authority["supporting_claim_ids"],
                "recommendation authority claims",
                nonempty=True,
            )
            or not isinstance(authority["human_review_required"], bool)
        ):
            raise ValueError("recommendation authority is invalid")
        _text(authority["semantic_preimage"], "recommendation semantic preimage")
        _strings(authority["contradiction_ids"], "recommendation contradiction identities")
        _strings(authority["limitation_ids"], "recommendation limitation identities")
        _text(authority["invalidation_condition"], "recommendation invalidation condition")
        authorities[recommendation_id] = authority
    return authorities


def _validate_statement_evidence(
    items: object,
    fields: Sequence[str],
    identity_field: str,
    evidence_ids: set[str],
    label: str,
) -> tuple[Mapping[str, object], ...]:
    output = []
    identities = set()
    for raw in _items(items, label):
        item = _exact(raw, fields, label)
        identity = _text(item[identity_field], f"{label} identity")
        if identity in identities or not set(
            _strings(item["evidence_ids"], f"{label} evidence", nonempty=True)
        ).issubset(evidence_ids):
            raise ValueError(f"{label} is invalid")
        identities.add(identity)
        output.append(item)
    return tuple(output)


def validate_live_intelligence_result_v2(
    *,
    journey_id: str,
    evidence: Mapping[str, Mapping[str, object]],
    lens_ids: set[str],
    authorities: Mapping[str, Mapping[str, object]],
    result: object,
    semantic_selector_enabled: bool,
    model_calls: int,
) -> None:
    result = _exact(result, RESULT_FIELDS, "result")
    evidence_ids = set(evidence)
    observations = _validate_statement_evidence(
        result["observations"], _OBSERVATION_FIELDS, "statement_id", evidence_ids, "observation"
    )
    observation_ids = {item["statement_id"] for item in observations}
    for item in observations:
        if set(item["source_receipt_ids"]) != {
            evidence[evidence_id]["source_receipt_id"] for evidence_id in item["evidence_ids"]
        }:
            raise ValueError("observation receipt citation is invalid")
    interpretations = _validate_statement_evidence(
        result["interpretations"],
        _INTERPRETATION_FIELDS,
        "statement_id",
        evidence_ids,
        "interpretation",
    )
    if any(
        not set(
            _strings(item["parent_observation_ids"], "interpretation parents", nonempty=True)
        ).issubset(observation_ids)
        for item in interpretations
    ):
        raise ValueError("interpretation parent is invalid")
    inferences = _validate_statement_evidence(
        result["inferences"], _INFERENCE_FIELDS, "statement_id", evidence_ids, "inference"
    )
    contradictions = _validate_statement_evidence(
        result["contradictions"],
        _CONTRADICTION_FIELDS,
        "contradiction_id",
        evidence_ids,
        "contradiction",
    )
    uncertainties = _validate_statement_evidence(
        result["uncertainties"],
        _UNCERTAINTY_FIELDS,
        "uncertainty_id",
        evidence_ids,
        "uncertainty",
    )
    del uncertainties
    contradiction_ids = {item["contradiction_id"] for item in contradictions}
    analogues = {}
    for raw in _items(result["historical_analogues"], "historical analogues"):
        analogue = _exact(raw, _ANALOGUE_FIELDS, "historical analogue")
        analogue_id = _text(analogue["analogue_id"], "historical analogue identity")
        as_of = _utc(analogue["as_of"], "historical analogue as of")
        cutoff = _utc(analogue["cutoff_at"], "historical analogue cutoff")
        analogue_evidence = _strings(
            analogue["evidence_ids"], "historical analogue evidence", nonempty=True
        )
        difference_codes = _strings(analogue["difference_codes"], "historical analogue differences")
        required_limits = []
        for code in difference_codes:
            limit = _TRANSFER_LIMITS.get(code)
            if limit is not None and limit not in required_limits:
                required_limits.append(limit)
        transfer_limits = _strings(
            analogue["transfer_limits"], "historical analogue transfer limits"
        )
        if (
            analogue_id in analogues
            or not set(analogue_evidence).issubset(evidence_ids)
            or as_of >= cutoff
            or any(
                _utc(evidence[item]["observed_at"], "historical evidence") > as_of
                for item in analogue_evidence
            )
            or transfer_limits != required_limits
            or (difference_codes and not transfer_limits)
        ):
            if as_of >= cutoff or any(
                _utc(evidence[item]["observed_at"], "historical evidence") > as_of
                for item in analogue_evidence
                if item in evidence
            ):
                raise ValueError("future historical analogue evidence is invalid")
            raise ValueError("historical analogue transfer limits are invalid")
        analogues[analogue_id] = analogue
    for inference in inferences:
        if (
            not _text(inference["method_id"], "inference method")
            or not set(inference["audience_lens_ids"]).issubset(lens_ids)
            or not set(inference["historical_analogue_ids"]).issubset(analogues)
        ):
            raise ValueError("inference authority is invalid")
    recommendations = {}
    for raw in _items(result["recommendations"], "recommendations"):
        recommendation = _exact(raw, _RECOMMENDATION_FIELDS, "recommendation")
        recommendation_id = recommendation["recommendation_id"]
        authority = authorities.get(recommendation_id)
        if authority is None:
            raise ValueError("recommendation authority is missing")
        if (
            recommendation_id in recommendations
            or recommendation["text"] != authority["semantic_preimage"]
            or not set(recommendation["supporting_claim_ids"])
            or not set(recommendation["supporting_claim_ids"]).issubset(
                authority["supporting_claim_ids"]
            )
            or not set(recommendation["evidence_ids"])
            or not set(recommendation["evidence_ids"]).issubset(authority["evidence_ids"])
            or recommendation["contradiction_ids"] != authority["contradiction_ids"]
            or recommendation["limitation_ids"] != authority["limitation_ids"]
            or recommendation["invalidation_condition"] != authority["invalidation_condition"]
            or recommendation["human_review_required"] is not authority["human_review_required"]
            or not set(recommendation["contradiction_ids"]).issubset(contradiction_ids)
            or not set(recommendation["analogue_ids"]).issubset(analogues)
        ):
            raise ValueError("recommendation semantic authority is invalid")
        recommendations[recommendation_id] = recommendation
    contradiction_evidence = {
        evidence_id for item in contradictions for evidence_id in item["evidence_ids"]
    }
    for raw in _items(result["clauses"], "clauses", nonempty=True):
        clause = _exact(raw, _CLAUSE_FIELDS, "clause citation")
        required = _strings(
            clause["required_evidence_ids"], "clause required evidence", nonempty=True
        )
        cited = _strings(clause["cited_evidence_ids"], "clause cited evidence")
        claim_ids = _strings(clause["claim_ids"], "clause claim identities", nonempty=True)
        if not set(required).issubset(evidence_ids) or any(
            not any(claim_id in evidence[item]["claim_ids"] for item in required)
            for claim_id in claim_ids
        ):
            raise ValueError("clause citation evidence is invalid")
        expected_state = (
            "unavailable"
            if not cited
            else "contradictory"
            if set(cited).intersection(contradiction_evidence) and set(cited) != set(required)
            else "exact"
            if set(cited) == set(required)
            else "insufficient"
        )
        if clause["citation_state"] != expected_state:
            raise ValueError("clause citation state is invalid")
    for raw in _items(result["abstentions"], "abstentions"):
        abstention = _exact(raw, _ABSTENTION_FIELDS, "abstention")
        _text(abstention["abstention_id"], "abstention identity")
        _text(abstention["statement"], "abstention statement")
        _strings(abstention["reason_codes"], "abstention reasons", nonempty=True)
    model_receipts = _strings(result["model_receipt_ids"], "model receipt identities")
    usage_receipts = _strings(result["usage_receipt_ids"], "usage receipt identities")
    if (
        isinstance(model_calls, bool)
        or not isinstance(model_calls, int)
        or model_calls < 0
        or model_calls > LIVE_SEMANTIC_METHOD["maximum_model_calls"]
    ):
        if isinstance(model_calls, int) and model_calls > 1:
            raise ValueError("maximum model calls exceeded")
        raise ValueError("model call count is invalid")
    if model_calls and not model_receipts:
        raise ValueError("model receipt is required")
    if model_calls and not usage_receipts:
        raise ValueError("usage receipt is required")
    if model_calls == 0 and (model_receipts or usage_receipts):
        raise ValueError("zero-call evaluator cannot carry model receipts")
    if semantic_selector_enabled or live_semantic_selector_enabled():
        raise ValueError("live semantic selector is disabled")
    if not isinstance(result["human_review_required"], bool) or not isinstance(
        result["export_allowed"], bool
    ):
        raise ValueError("review and export state is invalid")
    if journey_id == "journey_election_brand_role_v1" and (
        not result["human_review_required"] or result["export_allowed"]
    ):
        raise ValueError("election export is prohibited")


def evaluate_live_intelligence_journey(value: object) -> dict[str, object]:
    fixture = _exact(value, _JOURNEY_FIELDS, "journey")
    journey_id = _text(fixture["journey_id"], "journey identity")
    if journey_id not in APPROVED_JOURNEY_AUTHORITY_DIGESTS or fixture["sanitized"] is not True:
        raise ValueError("journey identity is invalid")
    if (
        canonical_digest(fixture["recommendation_authorities"])
        != APPROVED_JOURNEY_AUTHORITY_DIGESTS[journey_id]
    ):
        raise ValueError("approved recommendation authority differs")
    evidence, receipts = _validate_evidence(fixture["evidence"])
    lens_ids = _validate_lenses(fixture["audience_lenses"], evidence, receipts)
    authorities = _validate_authorities(fixture["recommendation_authorities"], set(evidence))
    validate_live_intelligence_result_v2(
        journey_id=journey_id,
        evidence=evidence,
        lens_ids=lens_ids,
        authorities=authorities,
        result=fixture["result"],
        semantic_selector_enabled=fixture["semantic_selector_enabled"],
        model_calls=fixture["model_calls"],
    )
    return deepcopy(dict(fixture["result"]))


__all__ = (
    "APPROVED_JOURNEY_AUTHORITY_DIGESTS",
    "AUDIENCE_LENS_FIELDS",
    "LIVE_SEMANTIC_METHOD",
    "RECOMMENDATION_AUTHORITY_FIELDS",
    "RESULT_FIELDS",
    "evaluate_live_intelligence_journey",
    "live_semantic_selector_enabled",
    "recommendation_authority_digest",
    "validate_live_intelligence_result_v2",
)
