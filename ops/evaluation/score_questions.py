"""Case scoring for the frozen question evaluation (E01).

The kernel separates transport execution from claim support. The wrappers
validate each record before the kernel runs, and the completion function
measures whole-service supported-task success over every issued case that
was labelled answerable before execution. The blind assessment record binds
every claim verdict to the answerability label the corpus assessor digested
before any model output existed, so a supported question that fails cannot
be recategorized afterwards.
"""

import hashlib
import json
import re
from datetime import datetime

TRANSPORT_STATES = frozenset(
    {
        "planned",
        "submitted",
        "pending",
        "complete",
        "partial",
        "refused",
        "failed",
        "unknown",
    }
)
CLAIM_VERDICTS = frozenset({"supported", "contradicted", "unverified"})
RETURNED_STATES = frozenset({"complete", "partial"})
COMPLETED_CLASSIFICATION = "requires_usefulness_review"
CONDITIONAL_LABEL = "conditional_on_returned_answers_not_a_completion_gate"


def classify_case(*, answerable: bool, state: str, material_claims: list[dict]) -> str:
    if any(
        claim["verdict"] in {"contradicted", "unverified"} for claim in material_claims
    ):
        return "fail_claim_support"
    if answerable and state not in {"complete", "partial"}:
        return "fail_supported_request"
    if not answerable and state in {"refused", "partial"}:
        return "pass_honest_boundary"
    if state == "complete" and not material_claims:
        return "fail_empty_success"
    return "requires_usefulness_review"


def classify_validated_case(*, answerable, state, material_claims) -> str:
    """Validate one case record, then classify it with the kernel.

    The answerability label must be a real boolean, the state must be one of
    the transport states, and every material claim must carry a known verdict.
    """
    if type(answerable) is not bool:
        raise ValueError("answerable_bool_required")
    if not isinstance(state, str) or state not in TRANSPORT_STATES:
        raise ValueError("state_invalid")
    if not isinstance(material_claims, list):
        raise ValueError("material_claims_list_required")
    for claim in material_claims:
        if not isinstance(claim, dict) or "verdict" not in claim:
            raise ValueError("claim_verdict_required")
        verdict = claim["verdict"]
        if not isinstance(verdict, str) or verdict not in CLAIM_VERDICTS:
            raise ValueError("claim_verdict_invalid")
    return classify_case(
        answerable=answerable, state=state, material_claims=material_claims
    )


def supported_completion(cases: list[dict]) -> dict:
    """Score whole-service completion over every issued supported case.

    The denominator is every issued case labelled answerable before execution.
    A supported case counts as completed only when it returned an answer whose
    material claims all survived assessment, so held, unavailable, timed out,
    transport-failed, refused, empty and contradicted supported cases are all
    failures. The whole-service gate passes only when the denominator is not
    empty and no supported case failed. Conditional answer quality among the
    supported cases that returned an answer is reported under its own label and
    is never the completion gate.
    """
    if not isinstance(cases, list):
        raise ValueError("cases_list_required")
    classifications = {}
    for record in cases:
        if not isinstance(record, dict):
            raise ValueError("case_record_invalid")
        case_id = record.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("case_id_required")
        if case_id in classifications:
            raise ValueError("duplicate_case_id")
        classifications[case_id] = classify_validated_case(
            answerable=record.get("answerable"),
            state=record.get("state"),
            material_claims=record.get("material_claims"),
        )
    supported = [record for record in cases if record["answerable"]]
    unsupported = [record for record in cases if not record["answerable"]]
    returned = [record for record in supported if record["state"] in RETURNED_STATES]
    denominator = len(supported)
    completed = sum(
        classifications[record["case_id"]] == COMPLETED_CLASSIFICATION
        for record in supported
    )
    quality = sum(
        classifications[record["case_id"]] == COMPLETED_CLASSIFICATION
        for record in returned
    )
    return {
        "issued": len(cases),
        "supported_denominator": denominator,
        "supported_completed": completed,
        "supported_failed": denominator - completed,
        "supported_completion_rate": completed / denominator if denominator else None,
        "whole_service_pass": bool(denominator) and completed == denominator,
        "conditional_answer_quality": {
            "label": CONDITIONAL_LABEL,
            "returned": len(returned),
            "supported": quality,
            "rate": quality / len(returned) if returned else None,
            "pass": bool(returned) and quality == len(returned),
        },
        "unsupported_issued": len(unsupported),
        "honest_boundaries": sum(
            classifications[record["case_id"]] == "pass_honest_boundary"
            for record in unsupported
        ),
        "classifications": classifications,
    }


# Blind claim assessment

CLAIM_KINDS = frozenset({"observation", "interpretation", "recommendation", "proposal"})
DETERMINISTIC_CHECKS = frozenset(
    {"identity", "quote_span", "scope_time", "numeric", "units_denominator"}
)
HUMAN_CHECKS = frozenset({"support", "relevance", "cultural_specificity", "usefulness"})
CHECK_RESULTS = frozenset({"pass", "fail", "not_applicable"})
LABEL_FIELDS = (
    "case_id",
    "answerable",
    "corpus_sha256",
    "scope",
    "window",
    "assessor",
    "labelled_at",
)
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _utc_stamp(value):
    if not isinstance(value, str) or _STAMP.fullmatch(value) is None:
        return None
    try:
        whole_second = datetime.fromisoformat(value[:19] + "+00:00")
    except ValueError:
        return None
    fraction = value[20:-1] if len(value) > 20 else ""
    return whole_second, fraction.rstrip("0")


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def answerability_label(
    *, case_id, answerable, corpus_sha256, scope, window, assessor, labelled_at
):
    """The corpus assessor's label, digested before any model output is read."""
    if type(answerable) is not bool:
        raise ValueError("answerable_bool_required")
    if not isinstance(corpus_sha256, str) or _DIGEST.fullmatch(corpus_sha256) is None:
        raise ValueError("corpus_sha256_invalid")
    for name, value in (("case_id", case_id), ("assessor", assessor)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"label_field_invalid: {name}")
    if _utc_stamp(labelled_at) is None:
        raise ValueError("label_field_invalid: labelled_at")
    if not isinstance(scope, dict) or not isinstance(window, dict):
        raise ValueError("label_field_invalid: scope_or_window")
    body = {
        "case_id": case_id,
        "answerable": answerable,
        "corpus_sha256": corpus_sha256,
        "scope": scope,
        "window": window,
        "assessor": assessor,
        "labelled_at": labelled_at,
    }
    return {
        "contract_version": "answerability_label_v1",
        **body,
        "label_digest": _digest(body),
    }


def _validate_label(label):
    if not isinstance(label, dict) or label.get("contract_version") != (
        "answerability_label_v1"
    ):
        raise ValueError("label_record_required")
    body = {field: label.get(field) for field in LABEL_FIELDS}
    if label.get("label_digest") != _digest(body):
        raise ValueError("label_digest_mismatch")


def _validate_claim(claim):
    if not isinstance(claim, dict):
        raise ValueError("claim_record_invalid")
    for name in ("claim_id", "text"):
        if not isinstance(claim.get(name), str) or not claim[name].strip():
            raise ValueError(f"claim_field_invalid: {name}")
    if type(claim.get("verbatim")) is not bool:
        raise ValueError("claim_field_invalid: verbatim")
    if claim.get("kind") not in CLAIM_KINDS:
        raise ValueError("claim_kind_invalid")
    passage = claim.get("cited_passage")
    if passage is not None and (not isinstance(passage, str) or not passage.strip()):
        raise ValueError("claim_field_invalid: cited_passage")
    checks = claim.get("checks")
    if not isinstance(checks, dict):
        raise ValueError("claim_check_invalid")
    known = DETERMINISTIC_CHECKS | HUMAN_CHECKS
    for name, outcome in checks.items():
        if name not in known or outcome not in CHECK_RESULTS:
            raise ValueError("claim_check_invalid")
    verdict = claim.get("verdict")
    if verdict not in CLAIM_VERDICTS:
        raise ValueError("claim_verdict_invalid")
    if "fail" in checks.values() and verdict != "contradicted":
        raise ValueError("verdict_inconsistent: failed_check_requires_contradicted")
    if passage is None and verdict != "unverified":
        raise ValueError("verdict_inconsistent: uncited_claim_is_unverified")
    if claim["kind"] == "proposal" and any(
        not isinstance(claim.get(name), str) or not claim[name].strip()
        for name in ("basis", "falsifier")
    ):
        raise ValueError("proposal_basis_required")


def blind_claim_assessment(
    label, *, state, material_claims, output_recorded_at, reviewer
):
    """Score claims against the frozen label; the label is the only source of
    answerability, so nothing here can recategorize a supported case."""
    _validate_label(label)
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("reviewer_required")
    output_stamp = _utc_stamp(output_recorded_at)
    if output_stamp is None:
        raise ValueError("output_recorded_at_invalid")
    if output_stamp <= _utc_stamp(label["labelled_at"]):
        raise ValueError("label_after_output")
    if not isinstance(material_claims, list):
        raise ValueError("material_claims_list_required")
    seen = set()
    for claim in material_claims:
        _validate_claim(claim)
        if claim["claim_id"] in seen:
            raise ValueError("duplicate_claim_id")
        seen.add(claim["claim_id"])
    classification = classify_validated_case(
        answerable=label["answerable"], state=state, material_claims=material_claims
    )
    return {
        "contract_version": "blind_claim_assessment_v1",
        "case_id": label["case_id"],
        "answerable": label["answerable"],
        "label_digest": label["label_digest"],
        "state": state,
        "material_claims": [dict(claim) for claim in material_claims],
        "classification": classification,
        "reviewer": reviewer,
        "output_recorded_at": output_recorded_at,
    }
