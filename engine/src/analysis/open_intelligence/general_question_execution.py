"""Join an admitted question to source validation, metered synthesis and durable results."""

import asyncio
import copy
import re
from contextlib import suppress
from datetime import timedelta
from time import monotonic
from types import MappingProxyType
from uuid import UUID, uuid4

from src.analysis.open_intelligence.general_question_calls import GeneralQuestionCalls
from src.analysis.open_intelligence.general_question_control import (
    QuestionStoreError,
    control_time,
    require_digest,
    require_request_id,
    utc_time,
)
from src.analysis.open_intelligence.general_question_deployment import validate_question_invocation
from src.analysis.open_intelligence.general_question_result import (
    observed_result_usage,
    unavailable_response,
)
from src.analysis.open_intelligence.general_question_runtime import (
    _answer_material,
    execute_question_answering,
    execute_question_planning,
    validate_persisted_question_answer,
)
from src.analysis.open_intelligence.general_question_snapshot import (
    build_general_question_snapshot,
    validate_stored_general_question_snapshot,
)
from src.analysis.open_intelligence.general_question_usage import persist_question_usage_event
from src.analysis.open_intelligence.general_question_window_comparison import (
    uncovered_window_missing_work,
)
from src.analysis.open_intelligence.historical_evidence_bridge import (
    UnresolvedHistory,
    build_question_history_resolver,
    planned_history_requirement_ids,
)

_SCOPE = ("client_scope_id", "market_scope", "brand_config_id", "audience_lens_ids", "theme_id")
_ACK = (
    "request_id",
    "request_digest",
    "intake_digest",
    "result_digest",
    "result_generation",
    "state",
)
_PUBLIC_CODES = {
    "request_invalid",
    "scope_invalid",
    "approval_required",
    "request_expired",
    "retrieval_incomplete",
    "evidence_insufficient",
    "claim_support_failed",
    "answer_invalid",
    "semantic_output_invalid",
    "model_timeout",
    "metering_persistence_failed",
    "response_usage_unavailable",
    "storage_unavailable",
    "worker_failed",
    "budget_exhausted_no_grounded_result",
}
_RETRIEVAL_CHECK_CODES = frozenset(
    {
        "coverage_incomplete",
        "corroborated_foreign_local_only",
        "parent_source_unavailable",
        "protected_context_registry_invalid",
        "copy_schema_invalid",
        "current_source_copy_invalid",
        "physical_schema_invalid",
        "release_admission_invalid",
        "release_material_invalid",
        "snapshot_source_copy_binding_invalid",
        "source_validation_incomplete",
    }
)
# The reasons the retrieval builder itself returns on a refusal. The retrieval cause line
# carries these and nothing else. They are internal: the published missing_work keeps the
# retrieval check codes its contract version names, and every other builder reason stays
# behind the generic public code there.
_PUBLIC_BUILDER_REASONS = _RETRIEVAL_CHECK_CODES | frozenset(
    {
        "plan_invalid",
        "plan_unavailable",
        "plan_conflict",
        "query_budget_invalid",
        "history_resolution_invalid",
        "snapshot_readback_invalid",
        "snapshot_validation_failed",
    }
)
STAGE_EVENT_VERSION = "question_stage_event_v1"
STAGES = ("admission", "queue", "planning", "retrieval", "answering", "validation", "publication")
STAGE_EVENTS = ("entered", "completed", "failed", "held")
USAGE_STATES = ("none", "reserved", "pending", "settled", "unknown")
# One closed vocabulary for every stage; the app keeps a frozen copy checked against this set.
STAGE_REASON_CODES = frozenset(
    _PUBLIC_CODES
    | {"request_cancelled", "request_terminal", "request_conflict", "queue_dispatch_unresolved"}
)
_HELD_STAGES = frozenset({"planning", "answering", "publication"})
_STAGE_EVENT_FIELDS = frozenset(
    {
        "event_version",
        "request_id",
        "invocation_id",
        "policy_digest",
        "deployment_digest",
        "stage",
        "event",
        "occurred_at",
        "elapsed_ms",
        "reason_code",
        "usage_state",
    }
)
_STAGE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _stage_transitions():
    # Frozen lifecycle: each stage is entered once and leaves by exactly one outcome.
    outcomes = {
        "admission": ("completed", "failed"),
        "queue": ("completed", "failed"),
        "planning": ("completed", "failed", "held"),
        "retrieval": ("completed", "failed"),
        "answering": ("completed", "failed", "held"),
        "validation": ("completed", "failed"),
        "publication": ("completed", "failed", "held"),
    }
    table = {
        (stage, "entered"): tuple((stage, event) for event in events)
        for stage, events in outcomes.items()
    }
    to_publication = (("publication", "entered"),)
    table[("admission", "completed")] = (("queue", "entered"),)
    table[("admission", "failed")] = ()
    table[("queue", "completed")] = (("planning", "entered"),)
    table[("queue", "failed")] = to_publication
    table[("planning", "completed")] = (("retrieval", "entered"), ("publication", "entered"))
    table[("planning", "failed")] = to_publication
    table[("planning", "held")] = to_publication
    table[("retrieval", "completed")] = (("answering", "entered"),)
    table[("retrieval", "failed")] = to_publication
    table[("answering", "completed")] = (("validation", "entered"),)
    table[("answering", "failed")] = to_publication
    table[("answering", "held")] = to_publication
    table[("validation", "completed")] = to_publication
    table[("validation", "failed")] = to_publication
    table[("publication", "completed")] = ()
    table[("publication", "failed")] = ()
    table[("publication", "held")] = ()
    return MappingProxyType(table)


STAGE_TRANSITIONS = _stage_transitions()


def validate_stage_event(value):
    """Admit only the exact closed record; every field has a grammar no raw text can pass."""
    if type(value) is not dict or set(value) != _STAGE_EVENT_FIELDS:
        raise QuestionStoreError("stage_event_invalid")
    try:
        if value["event_version"] != STAGE_EVENT_VERSION:
            raise ValueError()
        for field in ("request_id", "invocation_id"):
            if type(value[field]) is not str or str(UUID(value[field])) != value[field]:
                raise ValueError()
        for field in ("policy_digest", "deployment_digest"):
            require_digest(value[field], "stage_event_invalid")
        if (value["stage"], value["event"]) not in STAGE_TRANSITIONS:
            raise ValueError()
        if (
            type(value["occurred_at"]) is not str
            or _STAGE_TIME.fullmatch(value["occurred_at"]) is None
        ):
            raise ValueError()
        control_time(value["occurred_at"])
        if type(value["elapsed_ms"]) is not int or not 0 <= value["elapsed_ms"] <= 600000:
            raise ValueError()
        reason = value["reason_code"]
        if value["event"] in ("failed", "held"):
            if type(reason) is not str or reason not in STAGE_REASON_CODES:
                raise ValueError()
        elif reason is not None:
            raise ValueError()
        if type(value["usage_state"]) is not str or value["usage_state"] not in USAGE_STATES:
            raise ValueError()
    except (ValueError, TypeError, AttributeError, QuestionStoreError) as error:
        raise QuestionStoreError("stage_event_invalid") from error
    return copy.deepcopy(value)


def _usage_state(admission):
    calls = admission.get("execution", {}).get("calls", {})
    if not calls:
        return "reserved"
    if any(call["metering_failed"] or call["usage_status"] == "failed" for call in calls.values()):
        return "unknown"
    if all(call["usage_status"] == "acknowledged" for call in calls.values()):
        return "settled"
    return "pending"


class _StageTimeline:
    """One invocation's lifecycle audit trail, checked against the frozen table as it grows."""

    def __init__(self, sink, *, store, request_id, admission, started, started_at):
        self.sink = sink
        self.store = store
        self.request_id = request_id
        self.policy_digest = admission["policy_digest"]
        self.deployment_digest = admission["deployment_digest"]
        self.started = started
        self.started_at = started_at
        self.invocation_id = str(uuid4())
        self.previous = None

    def emit(self, stage, event, reason_code=None):
        if self.sink is None:
            return
        if (self.previous is None and event != "entered") or (
            self.previous is not None and (stage, event) not in STAGE_TRANSITIONS[self.previous]
        ):
            raise QuestionStoreError("stage_event_invalid")
        _, control = self.store._control()
        admission = control["requests"].get(self.request_id)
        if admission is None:
            raise QuestionStoreError("request_unknown")
        elapsed = monotonic() - self.started
        occurred = self.started_at + timedelta(seconds=elapsed)
        record = validate_stage_event(
            {
                "event_version": STAGE_EVENT_VERSION,
                "request_id": self.request_id,
                "invocation_id": self.invocation_id,
                "policy_digest": self.policy_digest,
                "deployment_digest": self.deployment_digest,
                "stage": stage,
                "event": event,
                "occurred_at": occurred.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "elapsed_ms": min(600000, max(0, int(elapsed * 1000))),
                "reason_code": reason_code,
                "usage_state": "unknown" if event == "held" else _usage_state(admission),
            }
        )
        self.previous = (stage, event)
        with suppress(OSError):
            self.sink(record)


class _RetrievalIncomplete(QuestionStoreError):
    def __init__(self, checks, refusal=None):
        super().__init__("retrieval_incomplete")
        self.checks = checks
        self.refusal = refusal


class _EvidenceInsufficient(QuestionStoreError):
    def __init__(self):
        super().__init__("evidence_insufficient")


class _RequestTerminal(QuestionStoreError):
    def __init__(self, status):
        super().__init__("request_terminal")
        self.status = status


def _public_builder_reason(value):
    return type(value) is str and value in _PUBLIC_BUILDER_REASONS


def _code_shaped(value):
    # A builder reason is always a code; raw provider or file text never fits this grammar.
    return type(value) is str and _REASON_CODE.fullmatch(value) is not None


def _retrieval_checks(value):
    # Only the user facing retrieval check codes reach the published missing_work. An
    # internal builder reason such as plan_invalid maps to the generic public code that
    # already leads the field, and the cause line names it for the operator.
    if type(value) is not dict:
        return ()
    reason = value.get("reason")
    supplied = [reason] if type(reason) is str and reason in _RETRIEVAL_CHECK_CODES else []
    missing = value.get("missing_work")
    if type(missing) is list:
        supplied.extend(
            item for item in missing[:16] if type(item) is str and item in _RETRIEVAL_CHECK_CODES
        )
    return tuple(dict.fromkeys(supplied))


def _retrieval_cause(error, refusal):
    # Everything here is a type name or a code; the exception message, requirement ids and
    # any free text in the builder's missing_work stay out of the log. Eight codes keep the
    # whole line inside the 2047 byte line the app's stderr drain accepts.
    code = getattr(error, "code", None)
    cause = {
        "exception_type": type(error).__name__,
        "exception_code": code if _code_shaped(code) else None,
        "builder_status": None,
        "builder_reason": None,
        "builder_missing_work": None,
        "builder_missing_work_omitted": 0,
    }
    if type(refusal) is dict:
        status, reason = refusal.get("status"), refusal.get("reason")
        missing = refusal.get("missing_work")
        missing = missing if type(missing) is list else []
        kept = [item for item in missing if _public_builder_reason(item)]
        cause.update(
            builder_status=status if _code_shaped(status) else None,
            builder_reason=reason if _code_shaped(reason) else None,
            builder_missing_work=kept[:8],
            builder_missing_work_omitted=len(missing) - len(kept[:8]),
        )
    return cause


def _context(invocation, store, runtime_identity):
    if type(invocation) is not dict:
        raise QuestionStoreError("request_invalid")
    request_id = invocation.get("request_id")
    require_request_id(request_id)
    _, control = store._control()
    admission = control["requests"].get(request_id)
    if admission is None:
        raise QuestionStoreError("request_unknown")
    stored = store._objects.read(
        f"requests/{request_id}/request.json", generation=int(admission["request_generation"])
    )
    if stored is None:
        raise QuestionStoreError("storage_unavailable")
    try:
        scope = {field: copy.deepcopy(stored.value[field]) for field in _SCOPE}
    except (TypeError, KeyError) as error:
        raise QuestionStoreError("request_invalid") from error
    context = validate_question_invocation(
        invocation, store=store, scope=scope, runtime_identity=runtime_identity
    )
    return context, scope


def question_answer_validators(store, scope):
    def snapshot_validator(snapshot, *, request, intake, plan):
        _, control = store._control()
        admission = control["requests"].get(request["request_id"])
        if admission is None:
            raise QuestionStoreError("request_unknown")
        records = {
            int(ordinal): {
                "reservation": {
                    key: copy.deepcopy(value) for key, value in row.items() if key != "receipt"
                },
                "receipt": copy.deepcopy(row["receipt"]),
            }
            for ordinal, row in admission["execution"]["queries"].items()
        }
        return validate_stored_general_question_snapshot(
            snapshot, request=request, intake=intake, plan=plan, query_records=records
        )

    def answer_validator(response, *, request, plan, snapshot):
        return validate_persisted_question_answer(
            response,
            store=store,
            scope=scope,
            request=request,
            plan=plan,
            snapshot=snapshot,
            validate_snapshot=snapshot_validator,
        )

    return snapshot_validator, answer_validator


def read_general_question_status(request_id, *, store, scope, now):
    _, validator = question_answer_validators(store, scope)
    return store.status(request_id, scope=scope, now=now, answer_validator=validator)


def _reason(error, phase):
    code = getattr(error, "code", None)
    if code == "plan_invalid":
        return "semantic_output_invalid"
    if code in _PUBLIC_CODES:
        return code
    if code in {"budget_exhausted", "query_budget_exhausted"}:
        return "budget_exhausted_no_grounded_result"
    if code in {"usage_unknown", "usage_unresolved", "response_unknown"}:
        return "response_usage_unavailable"
    if code == "metering_failed":
        return "metering_persistence_failed"
    if phase == "retrieval":
        return "retrieval_incomplete"
    if phase == "validation":
        return "claim_support_failed"
    if isinstance(error, ValueError):
        return "claim_support_failed" if phase == "answering" else "semantic_output_invalid"
    return "worker_failed"


def _failure_event(stage, state):
    # Only a stage that can leave cost unknown may hold; every other stage fails.
    return "held" if state == "held" and stage in _HELD_STAGES else "failed"


def _durable_marker(store, request_id, scope, now, validate_answer):
    # Reread the durable record before any new stage; an acknowledged marker stops new work.
    status = store.status(request_id, scope=scope, now=now, answer_validator=validate_answer)
    if status["result_digest"] is not None:
        raise _RequestTerminal(status)


def _validation_material(store, request_id, scope, validate_snapshot):
    current, _, _ = GeneralQuestionCalls(store)._context(request_id, scope)
    return _answer_material(store, current, validate_snapshot)


HISTORY_ANALOGUE_LIMIT = 1

# The retained history seam, off. Normal runtime retains no historical signals
# against a question scope: the authorized historical cutoffs that would
# produce them are a native gate and no local run can close it, so there is
# nothing to read and no provider to name. This is an explicit switch rather
# than a stub a test replaces, so the seam below is the code that actually
# runs in production and is exercised as written, on both sides. When a
# provider lands it is set here, takes (context, scope, plan) and returns the
# frame, the retained sources and the analogue rules for that question.
RETAINED_HISTORY_SOURCE_PROVIDER = None


def retained_history_material(context, scope, plan):
    """Return the admitted retained historical sources for one question, or None.

    The bridge answers a history requirement by reading an admitted retained
    evidence projection for named historical signals, then binding the result
    to a frozen history record. With the switch above unset there is no such
    projection to read, so this returns None.

    The resolver then records an explicit unresolved state on every planned
    history requirement, which the snapshot carries and the answer reports as
    missing work, instead of the requirement quietly resolving to nothing.
    That unresolved state is the only thing this seam produces in production
    today, and it is what every question with a history requirement gets.
    """
    provider = RETAINED_HISTORY_SOURCE_PROVIDER
    if provider is None:
        return None
    return provider(context, scope, plan)


def _history_resolver(context, scope, plan):
    """Build the history resolver the snapshot builder calls for this question.

    A resolver is always supplied, because the builder treats a missing one as
    a state to record rather than a reason to skip the requirement. With no
    admitted retained source the resolver names that gap on every planned
    history requirement; with one it reads the admitted retained projection and
    bridges it into a history record.
    """
    material = retained_history_material(context, scope, plan)
    if material is None:

        def unresolved(value):
            return {
                requirement_id: UnresolvedHistory(requirement_id, "historical_sources_unavailable")
                for requirement_id in planned_history_requirement_ids(value)
            }

        return unresolved
    return build_question_history_resolver(
        frame=material["frame"],
        sources=material["sources"],
        rules=material["rules"],
        limit=material.get("limit", HISTORY_ANALOGUE_LIMIT),
    )


async def execute_general_question(
    invocation,
    *,
    store,
    runtime_identity,
    credentials,
    now,
    diagnostics=None,
    stage_events=None,
):
    started, started_at = monotonic(), utc_time(now)

    def current():
        return started_at + timedelta(seconds=monotonic() - started)

    context, scope = _context(invocation, store, runtime_identity)
    request_id = context["request"]["request_id"]
    timeline = _StageTimeline(
        stage_events,
        store=store,
        request_id=request_id,
        admission=context["admission"],
        started=started,
        started_at=started_at,
    )

    def emit(phase, state, code=None):
        if diagnostics is not None:
            event = {
                "contract_version": "general_question_diagnostic_v1",
                "request_id": request_id,
                "phase": "result" if phase == "validation" else phase,
                "state": state,
                "code": code,
                "elapsed_ms": min(210000, max(0, int((monotonic() - started) * 1000))),
            }
            with suppress(OSError):
                diagnostics(event)

    def reuse(existing):
        timeline.emit("publication", "entered")
        if existing["state"] == "held":
            usage = observed_result_usage(store, request_id=request_id, scope=scope)
            timeline.emit("publication", "held", usage["reason"] or "response_usage_unavailable")
        else:
            timeline.emit("publication", "completed")
        emit("execution", "reused")
        return {field: existing[field] for field in _ACK}

    validate_snapshot, validate_answer = question_answer_validators(store, scope)
    emit("execution", "started")
    existing = store.status(
        request_id, scope=scope, now=current(), answer_validator=validate_answer
    )
    if existing["result_digest"] is not None:
        return reuse(existing)

    def persist(event):
        return persist_question_usage_event(event, store=store, scope=scope)

    phase, stage, plan = "planning", "planning", None
    try:
        emit(phase, "started")
        timeline.emit(stage, "entered")
        plan = await execute_question_planning(
            invocation,
            store=store,
            scope=scope,
            runtime_identity=runtime_identity,
            credentials=credentials,
            persist_usage=persist,
            now=current(),
        )
        emit(phase, "succeeded")
        timeline.emit(stage, "completed")
        if plan["status"] == "needs_clarification":
            usage = observed_result_usage(store, request_id=request_id, scope=scope)
            response = unavailable_response(
                context["request"],
                usage,
                reason="evidence_insufficient",
                markets=plan["markets"]
                if context["intake"]["contract_version"] == "general_question_intake_context_v3"
                else None,
            )
            response.pop("error")
            response.pop("reason")
            response["answer"] = plan["clarification"]
            response["intelligence"].update(
                status="needs_clarification",
                window=plan["window"],
                clarification=plan["clarification"],
                limitations=plan["limitations"],
                missing_work=[],
            )
            phase, stage = "result", "publication"
            emit(phase, "started")
            timeline.emit(stage, "entered")
            result = store.publish_result(
                request_id,
                scope=scope,
                response=response,
                state="needs_clarification",
                now=current(),
                plan_digest=plan["plan_digest"],
            )
        else:
            phase = stage = "retrieval"
            emit(phase, "started")
            timeline.emit(stage, "entered")
            _durable_marker(store, request_id, scope, current(), validate_answer)
            snapshot_result = await asyncio.to_thread(
                build_general_question_snapshot,
                invocation,
                store=store,
                scope=scope,
                runtime_identity=runtime_identity,
                credentials=credentials,
                plan=plan,
                candidate_limit=100,
                evidence_limit=200,
                discovery_bytes=100_000_000,
                source_bytes=300_000_000,
                release_bytes=50_000_000,
                now=current(),
                history_resolver=_history_resolver(context, scope, plan),
            )
            if snapshot_result.get("status") != "admitted":
                if snapshot_result == {
                    "contract_version": "general_question_snapshot_build_v1",
                    "status": "coverage_gap",
                    "snapshot": None,
                    "reason": "evidence_insufficient",
                    "missing_work": ["no_matching_evidence"],
                }:
                    raise _EvidenceInsufficient()
                raise _RetrievalIncomplete(_retrieval_checks(snapshot_result), snapshot_result)
            emit(phase, "succeeded")
            timeline.emit(stage, "completed")
            phase = stage = "answering"
            emit(phase, "started")
            timeline.emit(stage, "entered")
            _durable_marker(store, request_id, scope, current(), validate_answer)
            answer = await execute_question_answering(
                invocation,
                store=store,
                scope=scope,
                runtime_identity=runtime_identity,
                credentials=credentials,
                persist_usage=persist,
                now=current(),
                validate_snapshot=validate_snapshot,
            )
            emit(phase, "succeeded")
            timeline.emit(stage, "completed")
            phase = stage = "validation"
            emit(phase, "started")
            timeline.emit(stage, "entered")
            plan_record, snapshot_record = _validation_material(
                store, request_id, scope, validate_snapshot
            )
            validate_answer(
                copy.deepcopy(answer["response"]),
                request=copy.deepcopy(context["request"]),
                plan=plan_record,
                snapshot=snapshot_record,
            )
            timeline.emit(stage, "completed")
            phase, stage = "result", "publication"
            timeline.emit(stage, "entered")
            result = store.publish_result(
                request_id, scope=scope, now=current(), answer_validator=validate_answer, **answer
            )
        emit("result", "succeeded")
        timeline.emit("publication", "completed")
        emit("execution", "succeeded")
        return result
    except _RequestTerminal as terminal:
        timeline.emit(stage, "failed", "request_terminal")
        return reuse(terminal.status)
    except Exception as error:
        reason = _reason(error, phase)
        empty = isinstance(error, _EvidenceInsufficient)
        if phase == "retrieval" and not empty and diagnostics is not None:
            # The public code is generic by design; the log keeps the underlying cause.
            cause = {
                "contract_version": "general_question_retrieval_cause_v1",
                "request_id": request_id,
                "phase": phase,
                "public_code": reason,
                **_retrieval_cause(error, getattr(error, "refusal", None)),
                "elapsed_ms": min(210000, max(0, int((monotonic() - started) * 1000))),
            }
            with suppress(OSError):
                diagnostics(cause)
        emit(phase, "succeeded" if empty else "failed", reason)
        usage = observed_result_usage(store, request_id=request_id, scope=scope)
        response = unavailable_response(
            context["request"],
            usage,
            reason=reason,
            window=plan["window"] if plan is not None else None,
            markets=plan["markets"] if plan is not None else None,
        )
        if isinstance(error, _RetrievalIncomplete):
            response["intelligence"]["missing_work"] = [
                reason,
                *error.checks,
                *uncovered_window_missing_work(error.refusal),
            ]
        response["answer"] = "I could not produce a supported answer for this request."
        if empty:
            response["answer"] = (
                "No admissible evidence matched this request. The requested answer cannot be established from this read."
            )
            response["intelligence"]["missing_work"] = [reason, "no_matching_evidence"]
        if plan is not None:
            response["intelligence"]["window"] = plan["window"]
        state = "held" if usage["status"] == "unresolved" else "unavailable"
        if stage != "publication":
            timeline.emit(stage, _failure_event(stage, state), reason)
            timeline.emit("publication", "entered")
        try:
            result = store.publish_result(
                request_id,
                scope=scope,
                response=response,
                state=state,
                now=current(),
                plan_digest=plan["plan_digest"] if plan is not None else None,
            )
        except Exception as failure:
            timeline.emit("publication", "failed", _reason(failure, "result"))
            raise
        timeline.emit(
            "publication",
            "held" if state == "held" else "completed",
            reason if state == "held" else None,
        )
        emit("execution", "held" if state == "held" else "succeeded" if empty else "failed", reason)
        return result
