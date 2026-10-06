"""Frozen fifteen-task automated evaluation harness."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

from src.analysis.open_intelligence.brain_contract import canonical_digest

TASK_IDS = (
    "emerging_without_keyword",
    "why_moving",
    "cross_market_difference",
    "carrier_creator_analysis",
    "historical_analogue",
    "recurrence",
    "brand_role",
    "audience_availability",
    "source_agreement",
    "source_contradiction",
    "missing_evidence_refusal",
    "election_restraint",
    "causal_claim_rejection",
    "client_citation_accuracy",
    "prediction_review",
)
SCORE_FIELDS = (
    "factual_support",
    "citation_precision",
    "citation_completeness",
    "non_obviousness",
    "strategic_relevance",
    "market_sensitivity",
    "counter_evidence_quality",
    "actionability",
    "calibration",
    "writing_quality",
)
_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST = _ROOT / "tests/fixtures/open_intelligence/brain_v1/evaluation_manifest_v1.json"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EVIDENCE_ID = re.compile(r"ev_[0-9a-f]{64}\Z")
_CLAIM_ID = re.compile(r"claim_[a-z0-9_]+\Z")
_OUTPUT_FIELDS = {"task_id", "lane", "claims", "citations", "violations"}
_CLAIM_FIELDS = {"claim_id", "statement", "evidence_ids"}
_CITATION_FIELDS = {"claim_id", "required_evidence_ids", "cited_evidence_ids"}
_AUTOMATED_FAILURE_FIELDS = (
    "unsupported_claim_count",
    "unresolved_citation_count",
    "demographic_violation_count",
    "future_leak_count",
    "pre_gate_model_call_count",
)


@dataclass(frozen=True, slots=True)
class BrainScoreVector:
    factual_support: int
    citation_precision: int
    citation_completeness: int
    non_obviousness: int
    strategic_relevance: int
    market_sensitivity: int
    counter_evidence_quality: int
    actionability: int
    calibration: int
    writing_quality: int

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5
            for value in asdict(self).values()
        ):
            raise ValueError("evaluation score is invalid")


@dataclass(frozen=True, slots=True)
class EvaluationReviewRecord:
    task_id: str
    fixture_id: str
    pairing_key: str
    baseline_result_digest: str
    candidate_result_digest: str
    baseline_scores: BrainScoreVector
    candidate_scores: BrainScoreVector
    baseline_word_count: int
    candidate_word_count: int
    unsupported_claim_count: int
    unresolved_citation_count: int
    demographic_violation_count: int
    future_leak_count: int
    pre_gate_model_call_count: int
    review_record_id: str
    limitations: tuple[str, ...]
    admission: str

    def __post_init__(self) -> None:
        _validate_automated_failure_counts(self)


def _validate_automated_failure_counts(value: object) -> None:
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item != 0
        for item in (getattr(value, field, None) for field in _AUTOMATED_FAILURE_FIELDS)
    ):
        raise ValueError("automated failure count is invalid")


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _is_evidence_id(value: object) -> bool:
    return isinstance(value, str) and _EVIDENCE_ID.fullmatch(value) is not None


def _is_claim_id(value: object) -> bool:
    return isinstance(value, str) and _CLAIM_ID.fullmatch(value) is not None


def _expected_evidence_universe(task_id: str) -> tuple[str, ...]:
    try:
        task_index = TASK_IDS.index(task_id) + 1
    except ValueError as exc:
        raise ValueError("task evidence universe is invalid") from exc
    return (f"ev_{task_index:064x}",)


def pairing_key(task_id, fixture_id, baseline_digest, candidate_digest):
    return canonical_digest((task_id, fixture_id, baseline_digest, candidate_digest))


def load_evaluation_manifest():
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def validate_evaluation_manifest(value):
    tasks = value.get("tasks") if isinstance(value, dict) else None
    if not isinstance(tasks, list) or tuple(item.get("task_id") for item in tasks) != TASK_IDS:
        raise ValueError("15-task evaluation inventory is invalid")
    for item in tasks:
        if set(item) != {"task_id", "fixture_id"}:
            raise ValueError("evaluation manifest task is invalid")
        fixture = _ROOT / "tests/fixtures/open_intelligence/brain_v1" / item["fixture_id"]
        if not fixture.is_file():
            raise ValueError("15-task evaluation fixture is missing")
        _validate_task_fixture(item["task_id"], json.loads(fixture.read_text(encoding="utf-8")))
    return value


def _score_vector(value: object, field: str) -> BrainScoreVector:
    if not isinstance(value, dict) or tuple(value) != SCORE_FIELDS:
        raise ValueError(f"{field} score vector is invalid")
    return BrainScoreVector(**value)


def _validate_task_fixture(task_id: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("task_id") != task_id:
        raise ValueError("task fixture identity is invalid")
    evidence_universe = value.get("evidence_universe_ids")
    if (
        not isinstance(evidence_universe, list)
        or not evidence_universe
        or any(not _is_evidence_id(item) for item in evidence_universe)
        or len(evidence_universe) != len(set(evidence_universe))
        or tuple(evidence_universe) != _expected_evidence_universe(task_id)
    ):
        raise ValueError("task evidence universe is invalid")
    evidence_universe_set = set(evidence_universe)
    baseline_output = value.get("baseline_output")
    candidate_output = value.get("candidate_output")
    if not isinstance(baseline_output, dict) or not isinstance(candidate_output, dict):
        raise ValueError("task output is incomplete")
    baseline_digest = canonical_digest(baseline_output)
    candidate_digest = canonical_digest(candidate_output)
    stored_baseline_digest = value.get("baseline_result_digest")
    stored_candidate_digest = value.get("candidate_result_digest")
    if (
        not _is_digest(stored_baseline_digest)
        or not _is_digest(stored_candidate_digest)
        or stored_baseline_digest != baseline_digest
        or stored_candidate_digest != candidate_digest
        or baseline_digest == candidate_digest
    ):
        raise ValueError("output digest is invalid")
    _score_vector(value.get("baseline_scores"), "baseline")
    _score_vector(value.get("candidate_scores"), "candidate")
    derived_citation_failures = []
    derived_violations = []
    unsupported_claim_count = 0
    unresolved_citation_count = 0
    for lane, output in (("baseline", baseline_output), ("candidate", candidate_output)):
        if set(output) != _OUTPUT_FIELDS:
            raise ValueError("task output schema is invalid")
        if output.get("task_id") != task_id or output.get("lane") != lane:
            raise ValueError("task output identity is invalid")
        claims = output.get("claims")
        citations = output.get("citations")
        violations = output.get("violations")
        if (
            not isinstance(claims, list)
            or not claims
            or not isinstance(citations, list)
            or not citations
        ):
            raise ValueError("task output is incomplete")
        if any(
            not isinstance(item, dict)
            or set(item) != _CLAIM_FIELDS
            or not _is_claim_id(item["claim_id"])
            or not isinstance(item["statement"], str)
            or not item["statement"].strip()
            or not isinstance(item["evidence_ids"], list)
            or not item["evidence_ids"]
            or any(not _is_evidence_id(evidence) for evidence in item["evidence_ids"])
            or len(item["evidence_ids"]) != len(set(item["evidence_ids"]))
            or not set(item["evidence_ids"]).issubset(evidence_universe_set)
            for item in claims
        ):
            raise ValueError("task output claim or evidence universe is invalid")
        claims_by_id = {item["claim_id"]: item for item in claims}
        if len(claims_by_id) != len(claims):
            raise ValueError("task output claim is invalid")
        unsupported_claim_count += sum(
            not set(item["evidence_ids"]).issubset(evidence_universe_set) for item in claims
        )
        if any(
            not isinstance(citation, dict)
            or set(citation) != _CITATION_FIELDS
            or not _is_claim_id(citation["claim_id"])
            or not isinstance(citation["required_evidence_ids"], list)
            or not citation["required_evidence_ids"]
            or not isinstance(citation["cited_evidence_ids"], list)
            or not citation["cited_evidence_ids"]
            or any(
                not _is_evidence_id(evidence)
                for evidence in (
                    *citation["required_evidence_ids"],
                    *citation["cited_evidence_ids"],
                )
            )
            or len(citation["required_evidence_ids"]) != len(set(citation["required_evidence_ids"]))
            or len(citation["cited_evidence_ids"]) != len(set(citation["cited_evidence_ids"]))
            or not set(citation["required_evidence_ids"]).issubset(evidence_universe_set)
            or not set(citation["cited_evidence_ids"]).issubset(evidence_universe_set)
            for citation in citations
        ):
            raise ValueError("task output citation is invalid")
        citations_by_claim = {item["claim_id"]: item for item in citations}
        if len(citations_by_claim) != len(citations) or set(citations_by_claim) != set(
            claims_by_id
        ):
            raise ValueError("task output claim citation bijection is invalid")
        for citation in citations:
            claim = claims_by_id.get(citation.get("claim_id"))
            if (
                claim is None
                or set(citation.get("required_evidence_ids", ()))
                != set(claim.get("evidence_ids", ()))
                or set(citation.get("required_evidence_ids", ()))
                != set(citation.get("cited_evidence_ids", ()))
            ):
                derived_citation_failures.append(f"{lane}:{citation.get('claim_id')}")
                unresolved_citation_count += 1
        if not isinstance(violations, list):
            raise ValueError("task output violations are invalid")
        derived_violations.extend(f"{lane}:{item}" for item in violations)
    if derived_violations:
        raise ValueError("automated gate failed")
    if derived_citation_failures:
        raise ValueError("task fixture citation is incomplete")
    if value.get("review_records") not in ([], None):
        raise ValueError("review records are not authenticated")
    return {
        **value,
        "derived_violations": tuple(derived_violations),
        "derived_citation_failures": tuple(derived_citation_failures),
        "unsupported_claim_count": unsupported_claim_count,
        "unresolved_citation_count": unresolved_citation_count,
    }


def evaluate_review_pair(first: EvaluationReviewRecord, second: EvaluationReviewRecord):
    _validate_automated_failure_counts(first)
    _validate_automated_failure_counts(second)
    digests = (
        first.baseline_result_digest,
        first.candidate_result_digest,
        second.baseline_result_digest,
        second.candidate_result_digest,
    )
    if any(not _is_digest(value) for value in digests) or (
        first.baseline_result_digest == first.candidate_result_digest
        or second.baseline_result_digest == second.candidate_result_digest
    ):
        raise ValueError("output digest is invalid")
    if (
        first.review_record_id == second.review_record_id
        or first.task_id != second.task_id
        or first.pairing_key != second.pairing_key
    ):
        raise ValueError("review pairing is invalid")
    expected = pairing_key(
        first.task_id, first.fixture_id, first.baseline_result_digest, first.candidate_result_digest
    )
    if first.pairing_key != expected or any(
        (
            first.baseline_result_digest != second.baseline_result_digest,
            first.candidate_result_digest != second.candidate_result_digest,
            first.fixture_id != second.fixture_id,
        )
    ):
        raise ValueError("review pairing is invalid")
    manifest = load_evaluation_manifest()
    matches = [item for item in manifest["tasks"] if item["task_id"] == first.task_id]
    if len(matches) != 1 or matches[0]["fixture_id"] != first.fixture_id:
        raise ValueError("output digest is invalid")
    fixture = _ROOT / "tests/fixtures/open_intelligence/brain_v1" / first.fixture_id
    frozen = _validate_task_fixture(first.task_id, json.loads(fixture.read_text(encoding="utf-8")))
    if (
        first.baseline_result_digest != frozen["baseline_result_digest"]
        or first.candidate_result_digest != frozen["candidate_result_digest"]
        or second.baseline_result_digest != frozen["baseline_result_digest"]
        or second.candidate_result_digest != frozen["candidate_result_digest"]
    ):
        raise ValueError("output digest is invalid")
    output = {"baseline": {}, "candidate": {}}
    for field in SCORE_FIELDS:
        output["baseline"][field] = Fraction(
            getattr(first.baseline_scores, field) + getattr(second.baseline_scores, field), 2
        )
        output["candidate"][field] = Fraction(
            getattr(first.candidate_scores, field) + getattr(second.candidate_scores, field), 2
        )
        if output["candidate"][field] < 3:
            raise ValueError("critical task score is below 3")
    for field in ("factual_support", "strategic_relevance"):
        if output["candidate"][field] < output["baseline"][field]:
            raise ValueError("candidate does not beat baseline")
    return output


def evaluate_frozen_tasks():
    manifest = validate_evaluation_manifest(load_evaluation_manifest())
    baseline_totals = dict.fromkeys(SCORE_FIELDS, 0)
    candidate_totals = dict.fromkeys(SCORE_FIELDS, 0)
    for item in manifest["tasks"]:
        fixture = _ROOT / "tests/fixtures/open_intelligence/brain_v1" / item["fixture_id"]
        payload = _validate_task_fixture(
            item["task_id"], json.loads(fixture.read_text(encoding="utf-8"))
        )
        baseline = _score_vector(payload["baseline_scores"], "baseline")
        candidate = _score_vector(payload["candidate_scores"], "candidate")
        for field in SCORE_FIELDS:
            baseline_totals[field] += getattr(baseline, field)
            candidate_totals[field] += getattr(candidate, field)
    baseline_scores = {field: Fraction(value, 15) for field, value in baseline_totals.items()}
    candidate_scores = {field: Fraction(value, 15) for field, value in candidate_totals.items()}
    if any(candidate_scores[field] < 3 for field in SCORE_FIELDS):
        raise ValueError("critical task score is below 3")
    if any(
        candidate_scores[field] < 4
        for field in ("factual_support", "strategic_relevance", "non_obviousness", "actionability")
    ):
        raise ValueError("human admission threshold is unavailable")
    if any(
        candidate_scores[field] <= baseline_scores[field]
        for field in ("factual_support", "strategic_relevance")
    ):
        raise ValueError("candidate does not beat baseline")
    return {
        "task_count": len(manifest["tasks"]),
        "baseline_scores": baseline_scores,
        "candidate_scores": candidate_scores,
        "automated_admission": "validated",
        "human_admission": "pending",
        "model_call_count": 0,
    }


__all__ = []
