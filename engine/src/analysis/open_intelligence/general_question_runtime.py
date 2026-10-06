"""Durable planning worker for an admitted general cultural question."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import sys
from contextlib import suppress
from datetime import datetime, timedelta
from time import monotonic

from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.general_question_answer import (
    PlanAdapterMismatch,
    _answer_adapter,
    _expand_answer_aliases,
    _hydrate_typed_answer,
    build_question_answering_request,
    project_question_answer,
)
from src.analysis.open_intelligence.general_question_calls import (
    CallSubmissionPermit,
    GeneralQuestionCalls,
)
from src.analysis.open_intelligence.general_question_control import control_time, utc_time
from src.analysis.open_intelligence.general_question_planning import (
    CHALLENGE_PLAN_VERSIONS,
    build_question_planning_request,
    planned_draft_validator,
    planning_adapter_id,
    planning_request_for_call,
    validate_stored_question_plan,
)
from src.analysis.open_intelligence.general_question_store import QuestionStoreError
from src.analysis.open_intelligence.general_question_transport import (
    create_question_model_client,
    question_call_options,
)

_SERVICE_ACCOUNT = "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
# The one place that selects adapters. New requests plan and answer under this policy;
# a stored request replays under the adapters recorded on its calls and is never
# re-planned or re-answered under a newer one.
# v1 is retained for rollback: under it discovery questions planned no challenge search.
RUNTIME_ADAPTER_POLICY_V1 = {
    "contract_version": "general_question_runtime_adapter_policy_v1",
    "planning_adapter": "challenge_v5",
    "answer_adapter": "typed_v5",
    "retained_answer_adapter": "typed_v4",
}
# v2 is live: discovery questions also plan a challenge search.
RUNTIME_ADAPTER_POLICY_V2 = {
    "contract_version": "general_question_runtime_adapter_policy_v2",
    "planning_adapter": "discovery_challenge_v6",
    "answer_adapter": "typed_v5",
    "retained_answer_adapter": "typed_v4",
}
RUNTIME_ADAPTER_POLICY_V2_DIGEST = canonical_digest(RUNTIME_ADAPTER_POLICY_V2)
RUNTIME_ADAPTER_POLICY = RUNTIME_ADAPTER_POLICY_V2
RUNTIME_ADAPTER_POLICY_DIGEST = canonical_digest(RUNTIME_ADAPTER_POLICY)
_CURRENT_INTAKE_VERSION = "general_question_intake_context_v3"


def _planning_adapter(intake):
    """Policy planning adapter for a current intake; older intakes keep their retained adapter."""
    if intake.get("contract_version") == _CURRENT_INTAKE_VERSION:
        return RUNTIME_ADAPTER_POLICY["planning_adapter"]
    return None


def _new_answer_adapter(plan):
    """Policy answer adapter for a challenge plan; an older plan keeps the retained adapter."""
    if plan["contract_version"] in CHALLENGE_PLAN_VERSIONS:
        return RUNTIME_ADAPTER_POLICY["answer_adapter"]
    return RUNTIME_ADAPTER_POLICY["retained_answer_adapter"]


def _clock(started_at, started_monotonic):
    return started_at + timedelta(seconds=monotonic() - started_monotonic)


class StageDeadlineReached(ValueError):
    """The admitted deadline has passed; no new stage may start."""


def next_stage_deadline(admitted_deadline: datetime, now: datetime, stage_seconds: int) -> datetime:
    """Bound one new stage by its own limit and the admitted deadline, whichever is earlier."""
    if type(stage_seconds) is not int or stage_seconds <= 0:
        raise ValueError("stage limit must be a positive whole number of seconds")
    for value in (admitted_deadline, now):
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("stage clocks must carry a timezone")
    if now >= admitted_deadline:
        raise StageDeadlineReached("admitted deadline already reached")
    return min(admitted_deadline, now + timedelta(seconds=stage_seconds))


def _stage_deadline(store, context, started_at, started_monotonic):
    # The original admission deadline bounds every stage; a queue wait never resets it.
    current_monotonic = monotonic()
    current = started_at + timedelta(seconds=current_monotonic - started_monotonic)
    limit = store._stored_policy(context["request"]["policy_digest"])["limits"][
        "model_timeout_seconds"
    ]
    try:
        stage_deadline_at = next_stage_deadline(
            control_time(context["admission"]["deadline_at"]), current, limit
        )
    except StageDeadlineReached as exc:
        raise QuestionStoreError("request_expired") from exc
    except ValueError as exc:
        raise QuestionStoreError("control_invalid") from exc
    return current_monotonic + (stage_deadline_at - current).total_seconds()


def _remaining(context, started_at, started_monotonic, stage_deadline=None):
    current_monotonic = monotonic()
    current = _clock(started_at, started_monotonic)
    admission_deadline = control_time(context["admission"]["deadline_at"])
    seconds = (admission_deadline - current).total_seconds()
    if seconds <= 0:
        raise QuestionStoreError("request_expired")
    if stage_deadline is not None:
        stage_seconds = stage_deadline - current_monotonic
        if stage_seconds <= 0:
            raise QuestionStoreError("model_timeout")
        seconds = min(seconds, stage_seconds)
    return min(60.0, seconds)


def _validate_credentials(credentials, runtime_identity):
    expected = (
        runtime_identity.get("service_account_email")
        if isinstance(runtime_identity, dict)
        else None
    )
    if (
        expected != _SERVICE_ACCOUNT
        or getattr(credentials, "service_account_email", None) != expected
    ):
        raise QuestionStoreError("identity_invalid")


def _log_model_failure(error, *, request_id, stage):
    from google.genai.errors import APIError

    event = {
        "contract_version": "general_question_model_failure_v1",
        "request_id": request_id,
        "stage": stage,
        "exception_class": type(error).__name__[:100],
        "error_code": None,
        "provider_status": None,
        "provider_status_label": None,
        "provider_message": None,
    }
    if isinstance(error, QuestionStoreError) and error.code in {
        "call_invalid",
        "response_invalid",
        "response_unknown",
        "response_model_invalid",
        "usage_unknown",
        "usage_unresolved",
        "metering_failed",
        "count_invalid",
        "budget_exhausted",
        "call_already_started",
        "request_expired",
        "request_terminal",
        "control_conflict",
        "control_invalid",
        "protected_context_registry_invalid",
        "snapshot_cap_version_unknown",
        "storage_unavailable",
        "storage_conflict",
        "approval_required",
        "model_timeout",
        "model_unavailable",
    }:
        event["error_code"] = error.code
    if isinstance(error, APIError):
        if type(error.code) is int and 100 <= error.code <= 599:
            event["provider_status"] = error.code
        if type(error.status) is str and re.fullmatch(r"[A-Z_]{1,64}", error.status):
            event["provider_status_label"] = error.status
        if type(error.message) is str:
            message = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [redacted]", error.message)
            message = re.sub(
                r"(?i)\b(token|access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|authorization|password|credential|contents|system_instruction|headers)\b[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
                r"\1=[redacted]",
                message,
            )
            message = re.sub(
                r"(?is)\b(?:contents|prompt|system_?instruction|headers|request(?:_body)?|response_schema)\b[\"']?\s*[:=].*",
                "[request material redacted]",
                message,
            )
            message = re.sub(
                r"\b(?:AIza[\w-]{20,}|ya29\.[\w.-]+|eyJ[\w-]+\.[\w-]+\.[\w-]+)",
                "[redacted]",
                message,
            )
            event["provider_message"] = (
                " ".join(message.split()).encode("utf-8")[:1000].decode("utf-8", errors="ignore")
            )
    with suppress(Exception):
        line = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        while len(line.encode("utf-8")) > 2048 and event["provider_message"]:
            event["provider_message"] = event["provider_message"][:-1]
            line = (
                json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
            )
        sys.stderr.buffer.write(line.encode("utf-8"))
        sys.stderr.buffer.flush()


def _permit_matches(permit, sdk_request, *, stage="planning"):
    thinking = sdk_request.generation_config.thinking_config.thinking_level.value
    if (
        type(permit) is not CallSubmissionPermit
        or permit.stage != stage
        or permit.model != sdk_request.model
        or permit.max_output_tokens != sdk_request.generation_config.max_output_tokens
        or permit.thinking_level != thinking
    ):
        raise QuestionStoreError("call_invalid")


def _draft_from_snapshot(snapshot):
    from src.analysis.open_intelligence.general_question_response import question_response_text

    try:
        draft = json.loads(question_response_text(snapshot["raw_sdk_response"]))
        if type(draft) is not dict:
            raise ValueError()
        return draft
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise QuestionStoreError("plan_invalid") from exc


def _validate_planned_draft(snapshot, *, request, intake, parent_context=None):
    """Validate a planner draft under the adapter recorded on its call, so the plan version follows it."""
    adapter = planning_adapter_id(snapshot["binding"]["system_instruction_digest"])
    validator = planned_draft_validator(adapter)
    return validator(
        _draft_from_snapshot(snapshot),
        request=request,
        intake=intake,
        parent_context=parent_context,
    )


def _persist_plan(store, context, snapshot):
    request = context["request"]
    intake = context["intake"]
    from src.analysis.open_intelligence.general_question_parent_context import read_parent_context

    parent_context = read_parent_context(store, request, intake)
    try:
        planning_request_for_call(
            snapshot["binding"],
            request=request,
            intake=intake,
            policy=store._stored_policy(request["policy_digest"]),
            parent_context=parent_context,
        )
        plan = _validate_planned_draft(
            snapshot, request=request, intake=intake, parent_context=parent_context
        )
    except QuestionStoreError:
        raise
    except ValueError as exc:
        raise QuestionStoreError("plan_invalid") from exc
    key = f"requests/{request['request_id']}/plan.json"
    existing = store._objects.read(key)
    if existing is not None:
        try:
            stored_plan = validate_stored_question_plan(
                existing.value, request=request, intake=intake
            )
        except ValueError as exc:
            raise QuestionStoreError("plan_invalid") from exc
        if stored_plan != plan:
            raise QuestionStoreError("plan_invalid")
        return stored_plan
    stored = store._objects.create(key, plan)
    try:
        return validate_stored_question_plan(stored.value, request=request, intake=intake)
    except ValueError as exc:
        raise QuestionStoreError("plan_invalid") from exc


def _recover_existing(calls, store, context, scope, persist_usage):
    request_id = context["request"]["request_id"]
    call = context["admission"]["execution"]["calls"].get("planning")
    if call is None:
        return None
    if call["response"] is None:
        snapshot = calls.recover_response(request_id, stage="planning", scope=scope)
    else:
        snapshot = calls.read_response(request_id, stage="planning", scope=scope)
    calls.persist_usage(request_id, stage="planning", scope=scope, persist=persist_usage)
    return _persist_plan(store, context, snapshot)


async def execute_question_planning(
    invocation,
    *,
    store,
    scope,
    runtime_identity,
    credentials,
    persist_usage,
    now,
) -> dict:
    from src.analysis.open_intelligence.general_question_deployment import (
        validate_question_invocation,
    )

    started_at = utc_time(now)
    started_monotonic = monotonic()
    context = validate_question_invocation(
        invocation,
        store=store,
        scope=scope,
        runtime_identity=runtime_identity,
    )
    calls = GeneralQuestionCalls(store)
    recovered = _recover_existing(calls, store, context, scope, persist_usage)
    if recovered is not None:
        return recovered

    if store._objects.read(f"requests/{context['request']['request_id']}/plan.json") is not None:
        raise QuestionStoreError("plan_invalid")
    _validate_credentials(credentials, runtime_identity)
    stage_deadline = _stage_deadline(store, context, started_at, started_monotonic)
    from src.analysis.open_intelligence.general_question_parent_context import read_parent_context

    adapter = _planning_adapter(context["intake"])
    sdk_request = build_question_planning_request(
        context["request"],
        context["intake"],
        policy=store.policy,
        remaining_seconds=_remaining(context, started_at, started_monotonic, stage_deadline),
        parent_context=read_parent_context(store, context["request"], context["intake"]),
        **({"_adapter": adapter} if adapter is not None else {}),
    )
    timeout = _remaining(context, started_at, started_monotonic, stage_deadline)
    try:
        async with asyncio.timeout(timeout):
            async with create_question_model_client(credentials=credentials) as client:
                sdk_request.count_tokens_config.http_options = question_call_options(
                    remaining_seconds=_remaining(
                        context, started_at, started_monotonic, stage_deadline
                    )
                )
                counted = await client.aio.models.count_tokens(
                    model=sdk_request.model,
                    contents=sdk_request.contents,
                    config=sdk_request.count_tokens_config,
                )
                counted_input_tokens = counted.total_tokens
                if type(counted_input_tokens) is not int or counted_input_tokens <= 0:
                    raise QuestionStoreError("count_invalid")
                _remaining(context, started_at, started_monotonic, stage_deadline)
                permit = calls.claim_call(
                    context["request"]["request_id"],
                    stage="planning",
                    scope=scope,
                    input_digest=sdk_request.input_digest,
                    system_instruction_digest=sdk_request.system_instruction_digest,
                    response_schema_digest=sdk_request.response_schema_digest,
                    counted_input_tokens=counted_input_tokens,
                    now=_clock(started_at, started_monotonic),
                )
                _permit_matches(permit, sdk_request)
                sdk_request.generation_config.http_options = question_call_options(
                    remaining_seconds=_remaining(
                        context, started_at, started_monotonic, stage_deadline
                    )
                )
                sdk_request.generation_config.should_return_http_response = True
                response = await client.aio.models.generate_content(
                    model=sdk_request.model,
                    contents=sdk_request.contents,
                    config=sdk_request.generation_config,
                )
                raw_response = response.model_dump(mode="json")
                received_at = _clock(started_at, started_monotonic)
                snapshot = calls.record_response(
                    permit,
                    scope=scope,
                    response=raw_response,
                    received_at=received_at,
                )
    except TimeoutError as exc:
        _log_model_failure(exc, request_id=invocation["request_id"], stage="planning")
        raise QuestionStoreError("model_timeout") from exc
    except QuestionStoreError as exc:
        _log_model_failure(exc, request_id=invocation["request_id"], stage="planning")
        raise
    except Exception as exc:
        _log_model_failure(exc, request_id=invocation["request_id"], stage="planning")
        raise QuestionStoreError("model_unavailable") from exc

    calls.persist_usage(
        context["request"]["request_id"],
        stage="planning",
        scope=scope,
        persist=persist_usage,
    )
    return _persist_plan(store, context, snapshot)


def _answer_material(store, context, validate_snapshot):
    request, intake = context["request"], context["intake"]
    if not callable(validate_snapshot):
        raise QuestionStoreError("snapshot_unavailable")
    plan_object = store._objects.read(f"requests/{request['request_id']}/plan.json")
    snapshot_object = store._objects.read(f"requests/{request['request_id']}/snapshot.json")
    if plan_object is None or snapshot_object is None:
        raise QuestionStoreError("snapshot_unavailable")
    try:
        plan = validate_stored_question_plan(plan_object.value, request=request, intake=intake)
        planning_raw = GeneralQuestionCalls(store).read_response(
            request["request_id"],
            stage="planning",
            scope={
                key: request[key]
                for key in (
                    "client_scope_id",
                    "market_scope",
                    "brand_config_id",
                    "audience_lens_ids",
                    "theme_id",
                )
            },
        )
        if _validate_planned_draft(planning_raw, request=request, intake=intake) != plan:
            raise ValueError()
        snapshot = snapshot_object.value
        if snapshot.get("deployment_digest") != context["admission"]["deployment_digest"]:
            raise ValueError()
        checked = validate_snapshot(
            copy.deepcopy(snapshot),
            request=copy.deepcopy(request),
            intake=copy.deepcopy(intake),
            plan=copy.deepcopy(plan),
        )
        if type(checked) is not dict or canonical_bytes(checked) != snapshot_object.raw:
            raise ValueError()
        return plan, snapshot
    except QuestionStoreError:
        raise
    except ValueError as error:
        if error.args in (
            ("snapshot_cap_version_unknown",),
            ("protected_context_registry_invalid",),
        ):
            raise QuestionStoreError(error.args[0]) from error
        raise QuestionStoreError("snapshot_invalid") from error
    except Exception as error:
        raise QuestionStoreError("snapshot_invalid") from error


def _answer_request(calls, store, context, plan, snapshot, seconds):
    policy = store._stored_policy(context["request"]["policy_digest"])
    budget = calls._budget(context, "answering").snapshot
    remaining_input = min(
        policy["stages"]["answering"]["input_tokens"],
        budget.input_ceiling - budget.input_used,
    )
    remaining_output = min(
        policy["stages"]["answering"]["output_tokens"], budget.remaining_max_output_tokens
    )
    if budget.exhausted or remaining_input <= 0 or remaining_output <= 0:
        raise QuestionStoreError("budget_exhausted")
    existing = context["admission"]["execution"]["calls"].get("answering")
    adapter = _new_answer_adapter(plan)
    if existing is not None:
        try:
            adapter = _answer_adapter(
                existing["response_schema_digest"], existing["system_instruction_digest"]
            )
        except ValueError as error:
            raise QuestionStoreError("answer_binding_invalid") from error
    try:
        return build_question_answering_request(
            context["request"],
            context["intake"],
            plan,
            snapshot,
            policy=policy,
            remaining_input_tokens=remaining_input,
            remaining_output_tokens=remaining_output,
            remaining_seconds=seconds,
            _adapter=adapter,
        )
    except PlanAdapterMismatch as error:
        raise QuestionStoreError(str(error)) from error
    except ValueError as error:
        raise QuestionStoreError("answer_invalid") from error


def _answer_binding(call, sdk_request):
    if (
        any(
            call[field] != getattr(sdk_request, field)
            for field in (
                "input_digest",
                "system_instruction_digest",
                "response_schema_digest",
                "model",
            )
        )
        or call["thinking_level"]
        != sdk_request.generation_config.thinking_config.thinking_level.value
        or call["max_output_tokens"] != sdk_request.generation_config.max_output_tokens
    ):
        raise QuestionStoreError("answer_binding_invalid")


def _project_stored_answer(store, scope, context, plan, snapshot, raw, sdk_request):
    from src.analysis.open_intelligence.general_question_result import observed_result_usage

    calls = GeneralQuestionCalls(store)
    current, _, _ = calls._context(context["request"]["request_id"], scope)
    call = current["admission"]["execution"]["calls"].get("answering")
    if call is None:
        raise QuestionStoreError("response_unknown")
    _answer_binding(call, sdk_request)
    _answer_binding(call, _answer_request(calls, store, current, plan, snapshot, 60.0))
    usage = observed_result_usage(store, request_id=context["request"]["request_id"], scope=scope)
    if usage["status"] != "resolved":
        raise QuestionStoreError("usage_unresolved")
    budget = calls._budget(current, "answering")
    permit = budget.prepare_call("answering", "", lambda _: call["counted_input_tokens"])
    budget.record_response(
        permit,
        calls._response(raw),
        lambda event: event,
        recorded_at=control_time(raw["received_at"]),
    )
    if budget.snapshot.exhausted:
        raise QuestionStoreError("budget_exhausted")
    try:
        adapter = _answer_adapter(call["response_schema_digest"], call["system_instruction_digest"])
        context_args = {
            "request": context["request"],
            "intake": context["intake"],
            "plan": plan,
            "snapshot": snapshot,
        }
        raw_draft = _draft_from_snapshot(raw)
        if adapter in ("typed_v5", "typed_v4", "typed_v3", "typed", "typed_v1"):
            draft = _hydrate_typed_answer(raw_draft, **context_args, _adapter=adapter)
        else:
            draft = _expand_answer_aliases(
                raw_draft,
                **context_args,
                derived_references=adapter in ("span_v2", "span_v3"),
                span_mode=adapter != "compact",
            )
        response = project_question_answer(
            draft,
            request=context["request"],
            intake=context["intake"],
            plan=plan,
            snapshot=snapshot,
            usage=usage,
            _structural_uncertainty=adapter
            in ("span_v3", "typed_v5", "typed_v4", "typed_v3", "typed", "typed_v1"),
            _span_mode=adapter != "compact",
            **({"_answer_call": raw} if adapter in ("typed_v4", "typed_v5") else {}),
        )
    except (ValueError, QuestionStoreError) as error:
        raise QuestionStoreError("answer_invalid") from error
    return {
        "response": response,
        "state": response["intelligence"]["status"],
        "plan_digest": plan["plan_digest"],
        "snapshot_digest": snapshot["snapshot_digest"],
    }


async def execute_question_answering(
    invocation,
    *,
    store,
    scope,
    runtime_identity,
    credentials,
    persist_usage,
    now,
    validate_snapshot,
):
    from src.analysis.open_intelligence.general_question_deployment import (
        validate_question_invocation,
    )

    started_at, started_monotonic = utc_time(now), monotonic()
    context = validate_question_invocation(
        invocation, store=store, scope=scope, runtime_identity=runtime_identity
    )
    calls = GeneralQuestionCalls(store)
    context, _, control = calls._context(invocation["request_id"], scope)
    plan, snapshot = _answer_material(store, context, validate_snapshot)
    sdk_request = _answer_request(calls, store, context, plan, snapshot, 60.0)
    existing = context["admission"]["execution"]["calls"].get("answering")
    if existing is not None:
        _answer_binding(existing, sdk_request)
        raw = calls.recover_response(invocation["request_id"], stage="answering", scope=scope)
        calls.persist_usage(
            invocation["request_id"], stage="answering", scope=scope, persist=persist_usage
        )
        plan, snapshot = _answer_material(store, context, validate_snapshot)
        return _project_stored_answer(store, scope, context, plan, snapshot, raw, sdk_request)
    calls._live(context, control, _clock(started_at, started_monotonic))
    _validate_credentials(credentials, runtime_identity)
    stage_deadline = _stage_deadline(store, context, started_at, started_monotonic)
    try:
        async with asyncio.timeout(
            _remaining(context, started_at, started_monotonic, stage_deadline)
        ):
            async with create_question_model_client(credentials=credentials) as client:
                sdk_request.count_tokens_config.http_options = question_call_options(
                    remaining_seconds=_remaining(
                        context, started_at, started_monotonic, stage_deadline
                    )
                )
                counted = await client.aio.models.count_tokens(
                    model=sdk_request.model,
                    contents=sdk_request.contents,
                    config=sdk_request.count_tokens_config,
                )
                if type(counted.total_tokens) is not int or counted.total_tokens <= 0:
                    raise QuestionStoreError("count_invalid")
                _remaining(context, started_at, started_monotonic, stage_deadline)
                permit = calls.claim_call(
                    invocation["request_id"],
                    stage="answering",
                    scope=scope,
                    input_digest=sdk_request.input_digest,
                    system_instruction_digest=sdk_request.system_instruction_digest,
                    response_schema_digest=sdk_request.response_schema_digest,
                    counted_input_tokens=counted.total_tokens,
                    now=_clock(started_at, started_monotonic),
                )
                _permit_matches(permit, sdk_request, stage="answering")
                sdk_request.generation_config.http_options = question_call_options(
                    remaining_seconds=_remaining(
                        context, started_at, started_monotonic, stage_deadline
                    )
                )
                sdk_request.generation_config.should_return_http_response = True
                response = await client.aio.models.generate_content(
                    model=sdk_request.model,
                    contents=sdk_request.contents,
                    config=sdk_request.generation_config,
                )
                raw = calls.record_response(
                    permit,
                    scope=scope,
                    response=response.model_dump(mode="json"),
                    received_at=_clock(started_at, started_monotonic),
                )
    except TimeoutError as error:
        _log_model_failure(error, request_id=invocation["request_id"], stage="answering")
        raise QuestionStoreError("model_timeout") from error
    except QuestionStoreError as error:
        _log_model_failure(error, request_id=invocation["request_id"], stage="answering")
        raise
    except Exception as error:
        _log_model_failure(error, request_id=invocation["request_id"], stage="answering")
        raise QuestionStoreError("model_unavailable") from error
    calls.persist_usage(
        invocation["request_id"], stage="answering", scope=scope, persist=persist_usage
    )
    plan, snapshot = _answer_material(store, context, validate_snapshot)
    return _project_stored_answer(store, scope, context, plan, snapshot, raw, sdk_request)


def validate_persisted_question_answer(
    response, *, store, scope, request, plan, snapshot, validate_snapshot
):
    calls = GeneralQuestionCalls(store)
    context, _, _ = calls._context(request["request_id"], scope)
    actual_plan, actual_snapshot = _answer_material(store, context, validate_snapshot)
    if context["request"] != request or actual_plan != plan or actual_snapshot != snapshot:
        raise QuestionStoreError("answer_binding_invalid")
    sdk_request = _answer_request(calls, store, context, actual_plan, actual_snapshot, 60.0)
    raw = calls.read_response(request["request_id"], stage="answering", scope=scope)
    projected = _project_stored_answer(
        store, scope, context, actual_plan, actual_snapshot, raw, sdk_request
    )
    if canonical_bytes(projected["response"]) != canonical_bytes(response):
        raise QuestionStoreError("answer_binding_invalid")
    return copy.deepcopy(response)


__all__ = [
    "RUNTIME_ADAPTER_POLICY",
    "RUNTIME_ADAPTER_POLICY_DIGEST",
    "RUNTIME_ADAPTER_POLICY_V1",
    "RUNTIME_ADAPTER_POLICY_V2",
    "RUNTIME_ADAPTER_POLICY_V2_DIGEST",
    "StageDeadlineReached",
    "execute_question_answering",
    "execute_question_planning",
    "next_stage_deadline",
    "validate_persisted_question_answer",
]
