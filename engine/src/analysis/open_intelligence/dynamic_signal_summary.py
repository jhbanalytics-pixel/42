"""Evidence-bound dynamic signal summary caller."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from src.analysis.open_intelligence.canary_manifests import stage_manifest
from src.analysis.open_intelligence.canary_policy import CanaryPolicy
from src.analysis.open_intelligence.canary_receipts import CanaryReceiptSink
from src.analysis.open_intelligence.canary_runtime import CanaryStageResult, execute_canary_stage
from src.analysis.open_intelligence.canary_semantics import (
    validate_claim_semantics,
    validate_generated_prose,
)
from src.analysis.prompts.dynamic_signal_summary import RESPONSE_SCHEMA
from src.contracts.open_intelligence_budget import OpenIntelligenceRunBudget
from src.utils.gemini_usage import GeminiUsageEvent

DynamicSignalSummaryResult = CanaryStageResult


def _stage(input_envelope: Mapping[str, object]) -> dict[str, object]:
    task_id = input_envelope.get("task_id")
    stage = stage_manifest(f"dynamic_signal_summary:summary:{task_id}")
    if stage["envelope"] != dict(input_envelope):
        raise ValueError("summary input does not match the canonical manifest")
    return stage


def _validate(output: Mapping[str, object], envelope: Mapping[str, object]) -> None:
    claims = output["claims"]
    claim_ids = [claim["claim_id"] for claim in claims]
    if len(claim_ids) != len(set(claim_ids)) or not set(claim_ids).issubset(
        envelope["claim_slots"]
    ):
        raise ValueError("summary claim identity is invalid")
    evidence_ids = {item["evidence_id"] for item in envelope["candidate_input"]["evidence"]}
    if any(not set(claim["evidence_ids"]).issubset(evidence_ids) for claim in claims):
        raise ValueError("summary evidence identity is invalid")
    audience_lens_ids = set(envelope["candidate_input"].get("audience_lens_ids", ()))
    if any(not set(claim["audience_lens_ids"]).issubset(audience_lens_ids) for claim in claims):
        raise ValueError("summary audience lens identity is invalid")
    if output["signal_id"] != envelope["candidate_input"]["signal"]["signal_id"]:
        raise ValueError("summary signal identity is invalid")
    if output["evidence_state"] != envelope["candidate_input"]["signal"]["evidence_state"]:
        raise ValueError("summary evidence state changed")
    validate_claim_semantics(claims, text_field="claim_text", audience_lenses=())
    validate_generated_prose(output, RESPONSE_SCHEMA, audience_lenses=(), election=False)
    if not set(output["why_now_claim_ids"]).issubset(claim_ids):
        raise ValueError("summary why-now claims are invalid")
    if not set(output["possible_response"]["supporting_claim_ids"]).issubset(claim_ids):
        raise ValueError("summary response claims are invalid")
    if not set(output["possible_response"]["evidence_ids"]).issubset(evidence_ids):
        raise ValueError("summary response evidence is invalid")
    for output_field, input_field in (
        ("contradiction_ids", "contradictions"),
        ("limitation_ids", "limitations"),
        ("missing_work_ids", "missing_work"),
    ):
        if not set(output[output_field]).issubset({item["id"] for item in envelope[input_field]}):
            raise ValueError(f"summary {output_field} is invalid")


def summarize_dynamic_signal(
    input_envelope: Mapping[str, object],
    *,
    lane: str,
    model: str,
    policy: CanaryPolicy,
    sdk_client: object,
    budget: OpenIntelligenceRunBudget,
    persist_usage: Callable[[GeminiUsageEvent], GeminiUsageEvent],
    receipt_sink: CanaryReceiptSink,
) -> DynamicSignalSummaryResult:
    frozen = _stage(input_envelope)
    return execute_canary_stage(
        stage_manifest=frozen,
        lane=lane,
        model=model,
        policy=policy,
        sdk_client=sdk_client,
        budget=budget,
        persist_usage=persist_usage,
        receipt_sink=receipt_sink,
        response_schema=RESPONSE_SCHEMA,
        validate_output=lambda output: _validate(output, input_envelope),
    )


__all__ = ["DynamicSignalSummaryResult", "summarize_dynamic_signal"]
