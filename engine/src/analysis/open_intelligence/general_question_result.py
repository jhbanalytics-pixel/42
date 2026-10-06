"""Immutable terminal responses selected by the existing shared control record."""

import copy
import hashlib
import re
from datetime import date, timedelta

from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.general_question_control import (
    QuestionStoreError,
    control_time,
    require_digest,
    utc_time,
)
from src.analysis.open_intelligence.general_question_planning import validate_stored_question_plan
from src.analysis.open_intelligence.general_question_store import _pack
from src.analysis.open_intelligence.general_question_usage import read_question_usage

_BINDINGS = ("request_digest", "intake_digest", "policy_digest", "deployment_digest")
_STATES = {"complete", "partial", "needs_clarification", "unavailable", "refused", "held"}
_FIELDS = {
    "contract_version",
    "request_id",
    *_BINDINGS,
    "plan_digest",
    "snapshot_digest",
    "state",
    "recorded_at",
    "response",
}
_INTELLIGENCE = {
    "contract_version",
    "request_id",
    "request_digest",
    "status",
    "resolved_scope",
    "window",
    "as_of",
    "snapshot_id",
    "sections",
    "claims",
    "receipts",
    "readings",
    "limitations",
    "missing_work",
    "clarification",
    "review_required",
    "ready_for_downstream",
    "usage",
}


def observed_result_usage(store, *, request_id, scope):
    usage = read_question_usage(store, request_id=request_id, scope=scope)
    context = store.read_request(request_id, scope=scope)
    if any(call["metering_failed"] for call in context["admission"]["execution"]["calls"].values()):
        usage["status"] = "unresolved"
        usage["reason"] = "metering_persistence_failed"
    return usage


def unavailable_response(request, usage, *, reason, window=None, markets=None):
    day = control_time(request["as_of"]).date()
    if window is None:
        fallback = request["requested_window"] or {
            "start": (day - timedelta(days=14)).isoformat(),
            "end": (day - timedelta(days=1)).isoformat(),
        }
        window = {**fallback, "closed": date.fromisoformat(fallback["end"]) < day}
    else:
        window = copy.deepcopy(window)
    return {
        "answer": "This request could not be completed within its execution window.",
        "sources": [],
        "error": True,
        "reason": reason,
        "intelligence": {
            "contract_version": "general_cultural_question_v1",
            "request_id": request["request_id"],
            "request_digest": request["request_digest"],
            "status": "unavailable",
            "resolved_scope": {
                **{
                    key: copy.deepcopy(request[key])
                    for key in (
                        "client_scope_id",
                        "brand_config_id",
                        "audience_lens_ids",
                        "theme_id",
                    )
                },
                "market_scope": copy.deepcopy(
                    request["market_scope"] if markets is None else markets
                ),
            },
            "window": window,
            "as_of": request["as_of"],
            "snapshot_id": None,
            "sections": [],
            "claims": [],
            "receipts": [],
            "readings": [],
            "limitations": ["No completed answer is available."],
            "missing_work": [reason],
            "clarification": None,
            "review_required": False,
            "ready_for_downstream": False,
            "usage": copy.deepcopy(usage),
        },
    }


def _response(value, request, state):
    if type(value) is not dict or set(value) not in (
        {"answer", "sources", "intelligence"},
        {"answer", "sources", "intelligence", "error", "reason"},
    ):
        raise QuestionStoreError("result_invalid")
    if (
        type(value["answer"]) is not str
        or not value["answer"].strip()
        or type(value["sources"]) is not list
    ):
        raise QuestionStoreError("result_invalid")
    if "error" in value and (
        value["error"] is not True
        or type(value["reason"]) is not str
        or re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value["reason"]) is None
    ):
        raise QuestionStoreError("result_invalid")
    item = value["intelligence"]
    if type(item) is not dict or set(item) != _INTELLIGENCE:
        raise QuestionStoreError("result_invalid")
    usage = item["usage"]
    if (
        type(usage) is not dict
        or set(usage)
        != {
            "status",
            "model_calls",
            "input_tokens",
            "output_tokens",
            "usage_receipt_ids",
            "call_receipt_ids",
            "reservation_ids",
            "reserved_cost_usd",
            "reason",
        }
        or usage["status"] not in {"resolved", "unresolved"}
    ):
        raise QuestionStoreError("result_usage_invalid")
    for field in ("model_calls", "input_tokens", "output_tokens"):
        count = usage[field]
        if not (type(count) is int and count >= 0) and not (
            count is None and usage["status"] == "unresolved"
        ):
            raise QuestionStoreError("result_usage_invalid")
    for field in ("usage_receipt_ids", "call_receipt_ids", "reservation_ids"):
        values = usage[field]
        if (
            type(values) is not list
            or any(type(value) is not str or not value for value in values)
            or len(set(values)) != len(values)
        ):
            raise QuestionStoreError("result_usage_invalid")
    if (
        usage["reservation_ids"] != [request["request_id"]]
        or type(usage["reserved_cost_usd"]) is not str
        or re.fullmatch(r"[0-9]+\.[0-9]{6}", usage["reserved_cost_usd"]) is None
    ):
        raise QuestionStoreError("result_usage_invalid")
    if (usage["status"] == "resolved" and usage["reason"] is not None) or (
        usage["status"] == "unresolved"
        and (type(usage["reason"]) is not str or not usage["reason"])
    ):
        raise QuestionStoreError("result_usage_invalid")
    if item["contract_version"] != "general_cultural_question_v1" or any(
        item[key] != request[key] for key in ("request_id", "request_digest", "as_of")
    ):
        raise QuestionStoreError("result_invalid")
    if (
        item["status"] not in _STATES - {"held"}
        or (state != "held" and state != item["status"])
        or (state == "held" and item["status"] not in {"partial", "unavailable"})
    ):
        raise QuestionStoreError("result_invalid")
    scope = item["resolved_scope"]
    keys = {"client_scope_id", "market_scope", "brand_config_id", "audience_lens_ids", "theme_id"}
    if (
        type(scope) is not dict
        or set(scope) != keys
        or any(scope[k] != request[k] for k in keys - {"market_scope"})
    ):
        raise QuestionStoreError("scope_invalid")
    if (
        type(scope["market_scope"]) is not list
        or not scope["market_scope"]
        or len(set(scope["market_scope"])) != len(scope["market_scope"])
        or not set(scope["market_scope"]) <= set(request["market_scope"])
    ):
        raise QuestionStoreError("scope_invalid")
    for key in ("sections", "claims", "receipts", "readings", "limitations", "missing_work"):
        if type(item[key]) is not list:
            raise QuestionStoreError("result_invalid")
    for key in ("review_required", "ready_for_downstream"):
        if type(item[key]) is not bool:
            raise QuestionStoreError("result_invalid")
    if state == "held" and item["ready_for_downstream"]:
        raise QuestionStoreError("result_invalid")
    window = item["window"]
    null_clarification = window is None and state == "needs_clarification"
    if not null_clarification and (
        type(window) is not dict
        or set(window) != {"start", "end", "closed"}
        or type(window["closed"]) is not bool
    ):
        raise QuestionStoreError("result_invalid")
    try:
        if not null_clarification and date.fromisoformat(window["start"]) > date.fromisoformat(
            window["end"]
        ):
            raise ValueError()
    except (ValueError, TypeError) as error:
        raise QuestionStoreError("result_invalid") from error
    if item["status"] == "needs_clarification":
        if type(item["clarification"]) is not str or not item["clarification"].strip():
            raise QuestionStoreError("result_invalid")
    elif item["clarification"] is not None:
        raise QuestionStoreError("result_invalid")
    if item["status"] not in {"complete", "partial"}:
        if (
            any(item[key] for key in ("sections", "claims", "receipts", "readings"))
            or value["sources"]
            or item["snapshot_id"] is not None
            or item["ready_for_downstream"]
        ):
            raise QuestionStoreError("result_invalid")
    elif not item["claims"] or not item["receipts"] or not item["sections"]:
        raise QuestionStoreError("result_invalid")


def _artifacts(store, context, record, answer_validator):
    request = context["request"]
    plan = snapshot = None
    for field in ("plan_digest", "snapshot_digest"):
        if record[field] is not None:
            require_digest(record[field], "result_invalid")
    if record["plan_digest"] is not None:
        stored = store._objects.read(f"requests/{request['request_id']}/plan.json")
        if stored is None:
            raise QuestionStoreError("result_binding_invalid")
        plan = validate_stored_question_plan(
            stored.value, request=request, intake=context["intake"]
        )
        if plan["plan_digest"] != record["plan_digest"]:
            raise QuestionStoreError("result_binding_invalid")
    response = record["response"]
    if response["intelligence"]["window"] is None and (
        context["intake"]["contract_version"] != "general_question_intake_context_v3"
        or plan is None
        or plan["status"] != "needs_clarification"
        or plan["window"] is not None
        or response["answer"] != plan["clarification"]
        or response["intelligence"]["clarification"] != plan["clarification"]
        or response["intelligence"]["resolved_scope"]["market_scope"] != plan["markets"]
    ):
        raise QuestionStoreError("result_binding_invalid")
    if record["snapshot_digest"] is not None:
        stored = store._objects.read(f"requests/{request['request_id']}/snapshot.json")
        if stored is None or plan is None:
            raise QuestionStoreError("result_binding_invalid")
        snapshot = stored.value
        if (
            snapshot.get("snapshot_digest") != record["snapshot_digest"]
            or canonical_digest({k: v for k, v in snapshot.items() if k != "snapshot_digest"})
            != record["snapshot_digest"]
        ):
            raise QuestionStoreError("result_binding_invalid")
        expected = {
            "request_id": request["request_id"],
            **{key: record[key] for key in _BINDINGS},
            "plan_digest": record["plan_digest"],
        }
        if snapshot.get("contract_version") != "general_question_snapshot_v1" or any(
            snapshot.get(k) != v for k, v in expected.items()
        ):
            raise QuestionStoreError("result_binding_invalid")
    if record["response"]["intelligence"]["status"] in {"complete", "partial"}:
        if (
            plan is None
            or snapshot is None
            or snapshot.get("snapshot_id") != record["response"]["intelligence"]["snapshot_id"]
        ):
            raise QuestionStoreError("result_binding_invalid")
        if not callable(answer_validator):
            raise QuestionStoreError("result_support_unverified")
        checked = answer_validator(
            copy.deepcopy(record["response"]),
            request=copy.deepcopy(request),
            plan=copy.deepcopy(plan),
            snapshot=copy.deepcopy(snapshot),
        )
        if canonical_bytes(checked) != canonical_bytes(record["response"]):
            raise QuestionStoreError("result_support_unverified")


def _validate_record(store, context, record, answer_validator):
    if (
        type(record) is not dict
        or set(record) != _FIELDS
        or record["contract_version"] != "general_question_result_record_v1"
        or record["state"] not in _STATES
    ):
        raise QuestionStoreError("result_invalid")
    if (
        record["request_id"] != context["request"]["request_id"]
        or any(record[key] != context["admission"][key] for key in _BINDINGS)
        or control_time(record["recorded_at"]) < control_time(context["admission"]["admitted_at"])
    ):
        raise QuestionStoreError("result_binding_invalid")
    _response(record["response"], context["request"], record["state"])
    _artifacts(store, context, record, answer_validator)


def _context(store, request_id, scope):
    """Read the request and the control as one consistent snapshot.

    The two reads are separate round trips, so a control write landing between them
    (another publication of the same request selecting its result) tears the snapshot.
    That is a stale read to repeat, not a conflict; the callers' own loops decide what
    the fresh control means.
    """
    for _ in range(6):
        context = store.read_request(request_id, scope=scope)
        if context is None:
            raise QuestionStoreError("request_unknown")
        stored, control = store._control()
        if control["requests"].get(request_id) == context["admission"]:
            return context, stored, control
    raise QuestionStoreError("control_conflict")


def _selected(store, context, answer_validator):
    pointer = context["admission"].get("result")
    if pointer is None:
        return None
    stored = store._objects.read(
        f"requests/{context['request']['request_id']}/results/{pointer['digest']}.json",
        generation=int(pointer["generation"]),
    )
    if stored is None or hashlib.sha256(stored.raw).hexdigest() != pointer["digest"]:
        raise QuestionStoreError("result_unavailable")
    _validate_record(store, context, stored.value, answer_validator)
    return stored.value


def _ack(context, record):
    return {
        "request_id": record["request_id"],
        "request_digest": record["request_digest"],
        "intake_digest": record["intake_digest"],
        "result_digest": context["admission"]["result"]["digest"],
        "result_generation": context["admission"]["result"]["generation"],
        "state": record["state"],
    }


def publish_result(
    store,
    request_id,
    *,
    scope,
    response,
    state,
    now,
    plan_digest=None,
    snapshot_digest=None,
    answer_validator=None,
    _previous=None,
):
    now = utc_time(now)
    record = None
    for _ in range(6):
        context, stored, control = _context(store, request_id, scope)
        prior = _selected(store, context, answer_validator)
        if prior is not None:
            if all(
                prior[key] == value
                for key, value in (
                    ("response", response),
                    ("state", state),
                    ("plan_digest", plan_digest),
                    ("snapshot_digest", snapshot_digest),
                )
            ):
                return _ack(context, prior)
            if (
                _previous is None
                or context["admission"]["result"] != _previous
                or prior["state"] != "held"
            ):
                raise QuestionStoreError("result_conflict")
        usage = observed_result_usage(store, request_id=request_id, scope=scope)
        if response.get("intelligence", {}).get("usage") != usage:
            raise QuestionStoreError("result_usage_invalid")
        if usage["status"] == "unresolved" and state != "held":
            raise QuestionStoreError("result_usage_unresolved")
        record = {
            "contract_version": "general_question_result_record_v1",
            "request_id": request_id,
            **{key: context["admission"][key] for key in _BINDINGS},
            "plan_digest": plan_digest,
            "snapshot_digest": snapshot_digest,
            "state": state,
            "recorded_at": now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "response": copy.deepcopy(response),
        }
        _validate_record(store, context, record, answer_validator)
        raw = _pack(record)
        digest = hashlib.sha256(raw).hexdigest()
        result = store._objects.create(f"requests/{request_id}/results/{digest}.json", record)
        latest, _, _ = _context(store, request_id, scope)
        if latest["admission"] != context["admission"]:
            continue
        control["requests"][request_id]["result"] = {
            "digest": digest,
            "generation": str(result.generation),
        }
        store._objects.compare_control(stored.generation, control)
    context, _, _ = _context(store, request_id, scope)
    prior = _selected(store, context, answer_validator)
    if prior == record:
        return _ack(context, prior)
    raise QuestionStoreError("control_conflict")


def question_status(store, request_id, *, scope, now, answer_validator=None):
    from src.analysis.open_intelligence.general_question_calls import GeneralQuestionCalls

    now = utc_time(now)
    for _ in range(6):
        context, _, _ = _context(store, request_id, scope)
        if now < control_time(context["admission"]["admitted_at"]):
            raise QuestionStoreError("request_invalid")
        calls = context["admission"].get("execution", {}).get("calls", {})
        for stage, call in calls.items():
            if call["response"] is None:
                try:
                    GeneralQuestionCalls(store).recover_response(
                        request_id, stage=stage, scope=scope
                    )
                except QuestionStoreError as error:
                    if error.code != "response_unknown":
                        raise
        context, _, _ = _context(store, request_id, scope)
        record = _selected(store, context, answer_validator)
        if record is not None and control_time(record["recorded_at"]) > now:
            raise QuestionStoreError("result_invalid")
        usage = observed_result_usage(store, request_id=request_id, scope=scope)
        if (
            record is not None
            and record["state"] == "held"
            and record["response"]["intelligence"]["usage"] != usage
        ):
            reply = copy.deepcopy(record["response"])
            reply["intelligence"]["usage"] = usage
            state = "held" if usage["status"] == "unresolved" else reply["intelligence"]["status"]
            publish_result(
                store,
                request_id,
                scope=scope,
                response=reply,
                state=state,
                now=now,
                plan_digest=record["plan_digest"],
                snapshot_digest=record["snapshot_digest"],
                answer_validator=answer_validator,
                _previous=context["admission"]["result"],
            )
            continue
        if record is None and now >= control_time(context["admission"]["deadline_at"]):
            plan_digest = None
            window = None
            markets = None
            stored_plan = store._objects.read(f"requests/{request_id}/plan.json")
            if stored_plan is not None:
                try:
                    plan = validate_stored_question_plan(
                        stored_plan.value,
                        request=context["request"],
                        intake=context["intake"],
                    )
                except ValueError as error:
                    raise QuestionStoreError("result_invalid") from error
                plan_digest = plan["plan_digest"]
                window = plan["window"]
                markets = plan["markets"]
            publish_result(
                store,
                request_id,
                scope=scope,
                response=unavailable_response(
                    context["request"],
                    usage,
                    reason="request_expired",
                    window=window,
                    markets=markets,
                ),
                state="held" if usage["status"] == "unresolved" else "unavailable",
                now=now,
                plan_digest=plan_digest,
            )
            continue
        state = (
            record["state"]
            if record
            else (
                "running"
                if calls or context["admission"].get("execution", {}).get("queries")
                else "admitted"
            )
        )
        pointer = context["admission"].get("result")
        reply = {
            "contract_version": "general_question_status_v1",
            "request_id": request_id,
            **{key: context["admission"][key] for key in _BINDINGS},
            "state": state,
            "deadline_at": context["admission"]["deadline_at"],
            "result_digest": pointer["digest"] if pointer else None,
            "result_generation": pointer["generation"] if pointer else None,
        }
        # The lens the stored request was admitted under, so a caller holding only
        # a job id can record the same boundary this request was admitted on. It is
        # present only when the request named one; general 42 keeps the old shape.
        if "client_lens" in context["request"]:
            reply["client_lens"] = copy.deepcopy(context["request"]["client_lens"])
        return reply
    raise QuestionStoreError("control_conflict")
