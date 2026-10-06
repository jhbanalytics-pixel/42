"""Read the approved usage projection without writes or execution authority."""

from __future__ import annotations

import hashlib
from uuid import UUID

from src.analysis.open_intelligence.general_question_calls import (
    GeneralQuestionCalls,
    _binding,
    _digest,
    _event_json,
)
from src.analysis.open_intelligence.general_question_control import QuestionStoreError, control_time
from src.analysis.open_intelligence.general_question_response import decode_question_response
from src.utils.gemini_usage import GeminiUsageEvent, build_usage_event


def _observed_tokens(snapshot):
    try:
        native = decode_question_response(snapshot["raw_sdk_response"])
    except ValueError as error:
        raise QuestionStoreError("usage_invalid") from error
    if native.get("model_version") != snapshot["binding"]["model"]:
        raise QuestionStoreError("response_model_invalid")
    usage = native.get("usage_metadata")
    if usage is None:
        return None, None, False
    if type(usage) is not dict:
        raise QuestionStoreError("usage_invalid")
    tool_tokens = usage.get("tool_use_prompt_token_count")
    if tool_tokens is not None and (type(tool_tokens) is not int or tool_tokens != 0):
        raise QuestionStoreError("usage_invalid")
    if "thoughts_token_count" not in usage:
        try:
            reconciled = GeneralQuestionCalls(None)._response(snapshot)
        except QuestionStoreError as error:
            if error.code != "usage_unknown":
                raise
        else:
            return reconciled.prompt_tokens, reconciled.completion_tokens, True
    values = [
        usage.get(field)
        for field in (
            "prompt_token_count",
            "candidates_token_count",
            "thoughts_token_count",
            "total_token_count",
        )
    ]
    if any(value is not None and (type(value) is not int or value < 0) for value in values):
        raise QuestionStoreError("usage_invalid")
    prompt, candidate, thoughts, total = values
    complete = all(value is not None for value in values)
    known_sum = sum(value for value in values[:3] if value is not None)
    if total is not None and (known_sum > total or (complete and known_sum != total)):
        raise QuestionStoreError("usage_invalid")
    output = None if candidate is None or thoughts is None else candidate + thoughts
    return prompt, output, complete


_PROJECTED_FIELDS = (
    "request_digest",
    "intake_digest",
    "deployment_digest",
    "policy_digest",
    "reserved_microusd",
    "execution",
)


def _projected(admission):
    """Narrow an admission to what this projection reads.

    The admission is read once before the object round trips and once after, so a control
    write landing between them tears the snapshot. A publication selecting a result moves
    only the result pointer, which no projected field is derived from: that is a stale read
    for the caller's own loop to repeat, not a conflict. A change to a projected field is a
    conflict and still refuses the projection.
    """
    return {key: admission.get(key) for key in _PROJECTED_FIELDS}


def read_question_usage(store, *, request_id, scope):
    calls = GeneralQuestionCalls(store)
    context, _, _ = calls._context(request_id, scope)
    request = context["request"]
    admission = context["admission"]
    reserved = admission["reserved_microusd"]
    value = {
        "status": "resolved",
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "usage_receipt_ids": [],
        "call_receipt_ids": [],
        "reservation_ids": [request_id],
        "reserved_cost_usd": f"{reserved // 1000000}.{reserved % 1000000:06d}",
        "reason": None,
    }
    response_unknown = False
    metering_unresolved = False
    for stage in ("planning", "answering"):
        call = admission["execution"]["calls"].get(stage)
        if call is None:
            continue
        value["call_receipt_ids"].append(_digest(_binding(call)))
        if call["response"] is None:
            observed = (None, None, None)
            response_unknown = True
        else:
            snapshot = calls._snapshot(context, stage)
            prompt, output, complete = _observed_tokens(snapshot)
            observed = (1, prompt, output)
            if not complete:
                response_unknown = True
                if call["usage_status"] == "acknowledged":
                    raise QuestionStoreError("usage_unresolved")
            elif call["usage_status"] != "acknowledged":
                metering_unresolved = True
            else:
                event = build_usage_event(
                    trend_date=control_time(request["as_of"]).date(),
                    run_id=request["run_id"],
                    consumer="open_question_answer",
                    stage=stage,
                    call_index=0,
                    market=request["market_scope"][0]
                    if len(request["market_scope"]) == 1
                    else None,
                    gemini_model=call["model"],
                    prompt_tokens=prompt,
                    completion_tokens=output,
                    recorded_at=control_time(snapshot["received_at"]),
                )
                pointer = call["usage"]
                stored = store._objects.read(
                    f"requests/{request_id}/usage/{stage}.json",
                    generation=int(pointer["generation"]),
                )
                if (
                    stored is None
                    or hashlib.sha256(stored.raw).hexdigest() != pointer["digest"]
                    or stored.value != _event_json(event)
                ):
                    raise QuestionStoreError("usage_unresolved")
                value["usage_receipt_ids"].append(event.usage_id)
        for field, counter in zip(
            ("model_calls", "input_tokens", "output_tokens"), observed, strict=True
        ):
            value[field] = (
                None if value[field] is None or counter is None else value[field] + counter
            )
    if response_unknown or metering_unresolved:
        value["status"] = "unresolved"
        value["reason"] = (
            "response_usage_unavailable" if response_unknown else "metering_persistence_failed"
        )
    current, _, _ = calls._context(request_id, scope)
    if _projected(current["admission"]) != _projected(admission):
        raise QuestionStoreError("control_conflict")
    return value


def persist_question_usage_event(event, *, store, scope):
    if not isinstance(event, GeminiUsageEvent) or not event.run_id.startswith("question_"):
        raise ValueError("usage_event_invalid")
    try:
        request_id = str(UUID(event.run_id.removeprefix("question_")))
    except ValueError as error:
        raise ValueError("usage_event_invalid") from error
    calls = GeneralQuestionCalls(store)
    context, _, _ = calls._context(request_id, scope)
    request = context["request"]
    snapshot = calls.read_response(request_id, stage=event.stage, scope=scope)
    prompt, output, complete = _observed_tokens(snapshot)
    if not complete:
        raise QuestionStoreError("usage_unknown")
    expected = build_usage_event(
        trend_date=control_time(request["as_of"]).date(),
        run_id=request["run_id"],
        consumer="open_question_answer",
        stage=event.stage,
        call_index=0,
        market=request["market_scope"][0] if len(request["market_scope"]) == 1 else None,
        gemini_model=snapshot["binding"]["model"],
        prompt_tokens=prompt,
        completion_tokens=output,
        recorded_at=control_time(snapshot["received_at"]),
    )
    if event != expected:
        raise ValueError("usage_event_invalid")
    key = f"requests/{request_id}/usage/{event.stage}.json"
    value = _event_json(expected)
    stored = store._objects.create(key, value)
    readback = store._objects.read(key, generation=stored.generation)
    if readback is None or readback.value != value:
        raise QuestionStoreError("usage_unresolved")
    return expected
