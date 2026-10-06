"""Evidence-plan and approved-plan answer callers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from src.analysis.open_intelligence.canary_manifests import stage_manifest
from src.analysis.open_intelligence.canary_plan_approvals import (
    ReadbackApprovedPlan,
)
from src.analysis.open_intelligence.canary_policy import CanaryPolicy
from src.analysis.open_intelligence.canary_receipts import CanaryReceiptSink
from src.analysis.open_intelligence.canary_runtime import CanaryStageResult, execute_canary_stage
from src.analysis.open_intelligence.canary_semantics import (
    validate_claim_semantics,
    validate_generated_prose,
)
from src.analysis.prompts.open_question_answering import RESPONSE_SCHEMA as ANSWER_SCHEMA
from src.analysis.prompts.open_question_planning import RESPONSE_SCHEMA as PLAN_SCHEMA
from src.contracts.open_intelligence_budget import OpenIntelligenceRunBudget
from src.utils.gemini_usage import GeminiUsageEvent

OpenQuestionPlanResult = CanaryStageResult
OpenQuestionAnswerResult = CanaryStageResult


class ApprovalRecordUnavailable(ValueError):
    code = "approval_record_unavailable"


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ApprovedPlanRecord:
    plan_id: str
    plan_digest: str
    approved_by: str
    approved_at: datetime
    approval_id: str
    approved_claim_ids: tuple[str, ...]

    @classmethod
    def from_manifest(cls, stage: Mapping[str, object]) -> ApprovedPlanRecord:
        raise ApprovalRecordUnavailable("approval_record_unavailable")


def _stage(input_envelope: Mapping[str, object], stage: str) -> dict[str, object]:
    task_id = input_envelope.get("task_id")
    frozen = stage_manifest(f"open_question_answer:{stage}:{task_id}")
    if frozen["envelope"] != dict(input_envelope):
        raise ValueError(f"{stage} input does not match the canonical manifest")
    return frozen


def _unique_members(values: list[str], allowed: list[str], field: str) -> None:
    if len(values) != len(set(values)) or not set(values).issubset(allowed):
        raise ValueError(f"{field} identity is invalid")


def _validate_plan(output: Mapping[str, object], envelope: Mapping[str, object]) -> None:
    if output["plan_id"] != envelope["plan_id"]:
        raise ValueError("plan identity changed")
    _unique_members(
        [item["requirement_id"] for item in output["evidence_requirements"]],
        envelope["requirement_slots"],
        "requirement",
    )
    _unique_members(
        [item["claim_id"] for item in output["claim_requirements"]],
        envelope["claim_slots"],
        "claim",
    )
    if output["audience_lens_ids"] != envelope["request"]["audience_lens_ids"]:
        raise ValueError("planning audience lens identity changed")
    from src.analysis.open_intelligence.canary_manifests import load_canonical_manifest

    task = next(
        item
        for item in load_canonical_manifest()["tasks"]
        if item["task_id"] == envelope["task_id"]
    )
    expected = {
        "intent": task["expected_intent"],
        "decision": envelope["request"]["decision"],
        "markets": envelope["request"]["market_scope"],
        "window": envelope["request"]["window"],
        "historical_comparison": envelope["context_index"]["historical_comparison"],
        "source_families": envelope["context_index"]["source_families"],
        "output_form": envelope["request"]["output_form"],
        "audience_lens_ids": envelope["request"]["audience_lens_ids"],
        "human_review_required": envelope["context_index"]["human_review_required"],
    }
    for field, value in expected.items():
        if output[field] != value:
            raise ValueError(f"planning {field} changed")
    validate_claim_semantics(
        output["claim_requirements"],
        text_field="question",
        audience_lenses=envelope["context_index"]["audience_lenses"],
    )
    validate_generated_prose(
        output,
        PLAN_SCHEMA,
        audience_lenses=envelope["context_index"]["audience_lenses"],
        election=(
            envelope["context_index"]["human_review_required"]
            and not envelope["context_index"]["export_allowed"]
        ),
    )


def _validate_answer(
    output: Mapping[str, object],
    envelope: Mapping[str, object],
    approved_plan: ApprovedPlanRecord,
) -> None:
    if output["plan_id"] != envelope["plan_id"] or approved_plan.plan_id != envelope["plan_id"]:
        raise ValueError("answer plan identity changed")
    source_plan = (
        approved_plan.plan
        if isinstance(approved_plan, ReadbackApprovedPlan)
        else envelope["approved_plan"]
    )
    expected_digest = hashlib.sha256(_canonical(source_plan).encode("utf-8")).hexdigest()
    if approved_plan.plan_digest != expected_digest:
        raise ValueError("answer plan approval digest is invalid")
    claim_ids = [item["claim_id"] for item in output["claims"]]
    _unique_members(claim_ids, list(approved_plan.approved_claim_ids), "claim")
    _unique_members(
        [item["recommendation_id"] for item in output["recommendations"]],
        envelope["recommendation_slots"],
        "recommendation",
    )
    _unique_members(
        [item["section_id"] for item in output["sections"]],
        envelope["section_slots"],
        "section",
    )
    evidence_ids = {item["evidence_id"] for item in envelope["resolved_evidence"]}
    if any(not set(item["evidence_ids"]).issubset(evidence_ids) for item in output["claims"]):
        raise ValueError("answer evidence identity is invalid")
    if any(
        not set(item["evidence_ids"]).issubset(evidence_ids) for item in output["recommendations"]
    ):
        raise ValueError("recommendation evidence identity is invalid")
    if any(item["text"] != source_plan["decision"] for item in output["recommendations"]):
        raise ValueError("recommendation semantic preimage changed")
    audience_lens_ids = {item["lens_id"] for item in envelope["audience_lenses"]}
    if any(
        not set(item["audience_lens_ids"]).issubset(audience_lens_ids) for item in output["claims"]
    ):
        raise ValueError("answer audience lens identity is invalid")
    validate_claim_semantics(
        output["claims"],
        text_field="claim_text",
        audience_lenses=envelope["audience_lenses"],
    )
    validate_generated_prose(
        output,
        ANSWER_SCHEMA,
        audience_lenses=envelope["audience_lenses"],
        election=envelope["human_review_required"] and not envelope["export_allowed"],
    )
    if any(not set(item["claim_ids"]).issubset(claim_ids) for item in output["sections"]):
        raise ValueError("section claim identity is invalid")
    if any(
        not set(item["supporting_claim_ids"]).issubset(claim_ids)
        for item in output["recommendations"]
    ):
        raise ValueError("recommendation claim identity is invalid")
    if output["evidence_state"] != envelope["approved_plan"]["evidence_state"]:
        raise ValueError("answer evidence state changed")
    for output_field, input_field in (
        ("contradiction_ids", "contradictions"),
        ("limitation_ids", "limitations"),
        ("missing_work_ids", "missing_work"),
    ):
        if not set(output[output_field]).issubset({item["id"] for item in envelope[input_field]}):
            raise ValueError(f"answer {output_field} is invalid")
    if envelope["human_review_required"] and not output["human_review_required"]:
        raise ValueError("answer removed human review")
    if not envelope["export_allowed"] and output["export_allowed"]:
        raise ValueError("answer enabled prohibited export")


def plan_open_question(
    input_envelope: Mapping[str, object],
    *,
    lane: str,
    model: str,
    policy: CanaryPolicy,
    sdk_client: object,
    budget: OpenIntelligenceRunBudget,
    persist_usage: Callable[[GeminiUsageEvent], GeminiUsageEvent],
    receipt_sink: CanaryReceiptSink,
) -> OpenQuestionPlanResult:
    frozen = _stage(input_envelope, "planning")
    return execute_canary_stage(
        stage_manifest=frozen,
        lane=lane,
        model=model,
        policy=policy,
        sdk_client=sdk_client,
        budget=budget,
        persist_usage=persist_usage,
        receipt_sink=receipt_sink,
        response_schema=PLAN_SCHEMA,
        validate_output=lambda output: _validate_plan(output, input_envelope),
    )


def answer_open_question(
    input_envelope: Mapping[str, object],
    *,
    approved_plan: ApprovedPlanRecord | ReadbackApprovedPlan,
    lane: str,
    model: str,
    policy: CanaryPolicy,
    sdk_client: object,
    budget: OpenIntelligenceRunBudget,
    persist_usage: Callable[[GeminiUsageEvent], GeminiUsageEvent],
    receipt_sink: CanaryReceiptSink,
) -> OpenQuestionAnswerResult:
    frozen = _stage(input_envelope, "answering")
    if not isinstance(approved_plan, ReadbackApprovedPlan):
        raise ApprovalRecordUnavailable("approval_record_unavailable")
    record = approved_plan.record
    if (
        record.manifest_sha256 != "0f90586306fae9932b88424a16699182e4fc3ecbda47015a63bb3491dbb833c7"
        or record.task_id != frozen["task_id"]
        or record.stage != "answering"
        or record.plan_id != frozen["envelope"]["plan_id"]
    ):
        raise ApprovalRecordUnavailable("approval_record_unavailable")
    return execute_canary_stage(
        stage_manifest=frozen,
        lane=lane,
        model=model,
        policy=policy,
        sdk_client=sdk_client,
        budget=budget,
        persist_usage=persist_usage,
        receipt_sink=receipt_sink,
        response_schema=ANSWER_SCHEMA,
        validate_output=lambda output: _validate_answer(output, input_envelope, approved_plan),
    )


__all__ = [
    "ApprovalRecordUnavailable",
    "ApprovedPlanRecord",
    "OpenQuestionAnswerResult",
    "OpenQuestionPlanResult",
    "answer_open_question",
    "plan_open_question",
]
