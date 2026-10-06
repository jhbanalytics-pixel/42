"""Shared no-retry execution boundary for evidence-consumer canaries."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from jsonschema import validate

from src.analysis.gemini_client import BriefResponse
from src.analysis.open_intelligence.canary_policy import CanaryPolicy
from src.analysis.open_intelligence.canary_pricing import estimate_cost_usd, price_for
from src.analysis.open_intelligence.canary_receipts import (
    CanaryReceiptSink,
    build_reservation,
    build_terminal,
    join_receipt,
    next_call_index,
)
from src.analysis.open_intelligence.canary_request import (
    build_canary_request,
    validate_canary_client,
)
from src.contracts.open_intelligence_budget import OpenIntelligenceRunBudget
from src.utils.gemini_usage import GeminiUsageEvent

CANARY_CONTRACT_VERSION = "gemini_3_7_canary_v1"
SDK_VERSION = "2.20.0"
API_VERSION = "v1"


class UsageMetadataIncomplete(ValueError):
    """The SDK response omitted required canary usage counters."""


@dataclass(frozen=True, slots=True)
class CanaryStageResult:
    parsed: Mapping[str, object]
    raw_text: str
    input_digest: str
    system_instruction_digest: str
    response_schema_digest: str
    prompt_tokens: int
    candidates_tokens: int
    thoughts_tokens: int
    total_tokens: int
    usage_event_id: str
    receipt_id: str
    model: str
    thinking_level: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _schema_for_jsonschema(value: object) -> object:
    if isinstance(value, list):
        return [_schema_for_jsonschema(item) for item in value]
    if not isinstance(value, dict):
        return value
    converted = {
        key: _schema_for_jsonschema(item) for key, item in value.items() if key != "nullable"
    }
    if "type" in converted and isinstance(converted["type"], str):
        converted["type"] = converted["type"].lower()
    if value.get("nullable") is True:
        return {"anyOf": [converted, {"type": "null"}]}
    return converted


def validate_response_schema(output: Mapping[str, object], schema: Mapping[str, object]) -> None:
    validate(instance=dict(output), schema=_schema_for_jsonschema(dict(schema)))


def _run_context(envelope: Mapping[str, object], stage: str) -> tuple[str, date, str | None]:
    if stage == "summary":
        source = envelope["candidate_input"]
        run_id = source["run_id"]
        trend_date = date.fromisoformat(source["signal"]["signal_date"])
        markets = source["market_scope"]
    elif stage == "planning":
        source = envelope["request"]
        run_id = source["run_id"]
        trend_date = date.fromisoformat(source["window"]["end_date"])
        markets = source["market_scope"]
    else:
        source = envelope["approved_plan"]
        run_id = source["run_id"]
        trend_date = date.fromisoformat(source["window"]["end_date"])
        markets = source["market_scope"]
    market = markets[0] if len(markets) == 1 else None
    return run_id, trend_date, market


def execute_canary_stage(
    *,
    stage_manifest: dict[str, object],
    lane: str,
    model: str,
    policy: CanaryPolicy,
    sdk_client: object,
    budget: OpenIntelligenceRunBudget,
    persist_usage: Callable[[GeminiUsageEvent], GeminiUsageEvent],
    receipt_sink: CanaryReceiptSink,
    response_schema: Mapping[str, object],
    validate_output: Callable[[Mapping[str, object]], None],
) -> CanaryStageResult:
    if policy.lane != lane or policy.model != model:
        raise ValueError("caller model and lane must match the resolved policy")
    validate_canary_client(sdk_client)
    envelope = stage_manifest["envelope"]
    run_id, _trend_date, _market = _run_context(envelope, policy.stage)
    call_index = next_call_index(
        sink=receipt_sink,
        run_id=run_id,
        contract_version=CANARY_CONTRACT_VERSION,
        lane=lane,
        consumer=policy.consumer,
        stage=policy.stage,
    )
    max_output_tokens = min(
        policy.output_ceiling,
        budget.snapshot.remaining_max_output_tokens,
    )
    request = build_canary_request(
        stage_manifest,
        policy=policy,
        max_output_tokens=max_output_tokens,
    )
    now = _utc_now()
    price = price_for(model, now)
    reservation = build_reservation(
        lane=lane,
        consumer=policy.consumer,
        stage=policy.stage,
        model=model,
        thinking_level=policy.thinking_level,
        run_id=run_id,
        call_index=call_index,
        input_digest=request.input_digest,
        system_instruction_digest=request.system_instruction_digest,
        response_schema_digest=request.response_schema_digest,
        sdk_version=SDK_VERSION,
        api_version=API_VERSION,
        pricing_version=price.pricing_version,
        reserved_at=now,
        contract_version=CANARY_CONTRACT_VERSION,
    )
    receipt_sink.reserve(reservation)
    if receipt_sink.read(reservation.receipt_id) != (reservation,):
        raise RuntimeError("canary reservation readback failed")
    started_at = _utc_now()
    try:
        counted = sdk_client.models.count_tokens(
            model=request.model,
            contents=request.contents,
            config=request.count_tokens_config,
        )
        total_input = getattr(counted, "total_tokens", None)
        if isinstance(total_input, bool) or not isinstance(total_input, int) or total_input < 0:
            raise ValueError("token preflight metadata is invalid")
        permit = budget.prepare_call(policy.stage, request.contents, lambda _: total_input)
        if (
            permit.call_index != reservation.call_index
            or permit.max_output_tokens != max_output_tokens
        ):
            raise ValueError("budget permit differs from the reserved request")
        response = sdk_client.models.generate_content(
            model=request.model,
            contents=request.contents,
            config=request.generation_config,
        )
        parsed = getattr(response, "parsed", None)
        if not isinstance(parsed, Mapping):
            raise ValueError("model response is not a JSON object")
        validate_response_schema(parsed, response_schema)
        validate_output(parsed)
        usage = getattr(response, "usage_metadata", None)
        prompt_tokens = getattr(usage, "prompt_token_count", None)
        candidates_tokens = getattr(usage, "candidates_token_count", None)
        thoughts_tokens = getattr(usage, "thoughts_token_count", None)
        total_tokens = getattr(usage, "total_token_count", None)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (prompt_tokens, candidates_tokens, thoughts_tokens, total_tokens)
        ):
            raise UsageMetadataIncomplete("model usage metadata is incomplete")
        if total_tokens != prompt_tokens + candidates_tokens + thoughts_tokens:
            raise ValueError("model usage metadata does not reconcile")
        brief = BriefResponse(
            parsed=dict(parsed),
            raw_text=getattr(response, "text", "") or "",
            prompt_tokens=prompt_tokens,
            completion_tokens=candidates_tokens + thoughts_tokens,
            model=model,
            usage_metadata_complete=True,
        )
        usage_event = budget.record_response(permit, brief, persist_usage)
    except Exception as error:
        completed_at = _utc_now()
        terminal_state = "partial" if isinstance(error, UsageMetadataIncomplete) else "error"
        terminal = build_terminal(
            reservation=reservation,
            started_at=started_at,
            completed_at=completed_at,
            terminal_state=terminal_state,
            prompt_token_count=None,
            candidates_token_count=None,
            thoughts_token_count=None,
            total_token_count=None,
            estimated_cost_usd=None,
            usage_event_id=None,
            error_code=type(error).__name__,
            completed_contract_version=CANARY_CONTRACT_VERSION,
        )
        _complete_terminal(receipt_sink, reservation, terminal)
        raise
    completed_at = _utc_now()
    terminal = build_terminal(
        reservation=reservation,
        started_at=started_at,
        completed_at=completed_at,
        terminal_state="complete",
        prompt_token_count=prompt_tokens,
        candidates_token_count=candidates_tokens,
        thoughts_token_count=thoughts_tokens,
        total_token_count=total_tokens,
        estimated_cost_usd=estimate_cost_usd(
            price, prompt_tokens, candidates_tokens + thoughts_tokens
        ),
        usage_event_id=usage_event.usage_id,
        error_code=None,
        completed_contract_version=CANARY_CONTRACT_VERSION,
    )
    _complete_terminal(receipt_sink, reservation, terminal)
    return CanaryStageResult(
        parsed=dict(parsed),
        raw_text=brief.raw_text,
        input_digest=request.input_digest,
        system_instruction_digest=request.system_instruction_digest,
        response_schema_digest=request.response_schema_digest,
        prompt_tokens=prompt_tokens,
        candidates_tokens=candidates_tokens,
        thoughts_tokens=thoughts_tokens,
        total_tokens=total_tokens,
        usage_event_id=usage_event.usage_id,
        receipt_id=reservation.receipt_id,
        model=model,
        thinking_level=policy.thinking_level.value,
    )


def _complete_terminal(
    receipt_sink: CanaryReceiptSink,
    reservation: object,
    terminal: object,
) -> None:
    receipt_sink.complete(terminal)
    try:
        joined = join_receipt(receipt_sink.read(reservation.receipt_id))
    except Exception as error:
        raise RuntimeError("canary terminal readback failed") from error
    if joined.reservation != reservation or joined.terminal != terminal:
        raise RuntimeError("canary terminal readback failed")


def canonical_digest(value: Mapping[str, object]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "API_VERSION",
    "CANARY_CONTRACT_VERSION",
    "SDK_VERSION",
    "CanaryStageResult",
    "canonical_digest",
    "execute_canary_stage",
    "validate_response_schema",
]
