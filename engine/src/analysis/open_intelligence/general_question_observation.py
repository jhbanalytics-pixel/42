"""Bounded immutable observations of admitted questions and selected results."""

import copy
from datetime import UTC, datetime
from time import monotonic

from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.general_question_control import (
    QuestionStoreError,
    require_request_id,
    utc_time,
)
from src.analysis.open_intelligence.general_question_execution import question_answer_validators
from src.analysis.open_intelligence.general_question_parent_context import (
    parent_planning_aliases,
    parent_read_attempt,
    recorded_response_versions,
)
from src.analysis.open_intelligence.general_question_planning import validate_stored_question_plan
from src.analysis.open_intelligence.general_question_request import (
    _scope,
    validate_question_request,
)
from src.analysis.open_intelligence.general_question_result import _ack, _selected
from src.analysis.open_intelligence.general_question_store import _LEDGER

_STATUSES = (
    "planned",
    "running",
    "unconfirmed",
    "complete",
    "partial",
    "needs_clarification",
    "refused",
    "held",
    "killed",
    "unavailable",
)
_READ_SECONDS = 15
_OUTPUT_BYTES = 64 * 1024


class _ScopedInvalid(ValueError):
    pass


def validate_observe_request(value):
    if (
        type(value) is not dict
        or set(value) != {"contract_version", "scope", "request_id"}
        or value["contract_version"] != "general_question_observe_request_v1"
    ):
        raise QuestionStoreError("request_invalid")
    try:
        scope = _scope(value["scope"])
        if value["request_id"] is not None:
            require_request_id(value["request_id"])
    except (ValueError, QuestionStoreError) as error:
        raise ValueError("request_invalid") from error
    return {**value, "scope": scope}


def _reason(error):
    code = getattr(error, "code", None)
    if code == "parent_read_budget_exhausted":
        return "read_limit_reached"
    if code == "parent_context_deadline":
        return "read_deadline_reached"
    if code in {"storage_unavailable", "object_version_unavailable", "result_unavailable"}:
        return "record_unavailable"
    return "record_invalid"


def _request_context(store, identifier, row, scope):
    if row["client_scope_id"] != scope["client_scope_id"]:
        return None
    stored = store._objects.read(
        f"requests/{identifier}/request.json", generation=int(row["request_generation"])
    )
    if stored is None:
        raise QuestionStoreError("storage_unavailable")
    own_scope = {key: stored.value[key] for key in scope}
    request = validate_question_request(
        stored.value, scope=own_scope, policy_digest=row["policy_digest"]
    )
    if request["request_id"] != identifier or request["request_digest"] != row["request_digest"]:
        raise QuestionStoreError("control_invalid")
    if any(own_scope[key] != scope[key] for key in scope if key != "market_scope") or not set(
        own_scope["market_scope"]
    ) <= set(scope["market_scope"]):
        return None
    try:
        return store.read_request(identifier, scope=own_scope)
    except (ValueError, QuestionStoreError) as error:
        if _reason(error) in {"read_limit_reached", "read_deadline_reached"}:
            raise
        raise _ScopedInvalid() from error


def _observation(store, context, scope):
    scope = {key: context["request"][key] for key in scope}
    identifier = context["request"]["request_id"]
    record = plan = None
    reasons = []
    missing = []
    if context["admission"].get("result") is not None:
        try:
            _, validator = question_answer_validators(store, scope)
            record = _selected(store, context, validator)
            if record is not None and record["state"] in {"complete", "partial"}:
                recorded_response_versions(
                    context,
                    parent_aliases=parent_planning_aliases(
                        store, context["request"], context["intake"]
                    ),
                )
        except (ValueError, QuestionStoreError, KeyError, TypeError) as error:
            record = None
            reasons.append(_reason(error))
            missing.append("terminal_record_unavailable")
    if record is None and not reasons:
        stored = store._objects.read(f"requests/{identifier}/plan.json")
        if stored is not None:
            try:
                plan = validate_stored_question_plan(
                    stored.value, request=context["request"], intake=context["intake"]
                )
            except (ValueError, KeyError, TypeError):
                reasons.append("record_invalid")
                missing.append("plan_record_unavailable")
    state = record["state"] if record is not None else "unconfirmed"
    markets = (
        record["response"]["intelligence"]["resolved_scope"]["market_scope"]
        if record is not None
        else plan["markets"]
        if plan is not None
        else [context["intake"]["selected_market"]]
        if context["intake"]["selected_market"] is not None
        else context["request"]["market_scope"]
    )
    if record is None:
        missing.append("execution_unconfirmed")
    missing.extend(
        reason
        for reason in reasons
        if reason in {"record_invalid", "read_limit_reached", "read_deadline_reached"}
    )
    messages = {
        "execution_unconfirmed": "Execution is unconfirmed; no validated terminal result is selected.",
        "terminal_record_unavailable": "The selected terminal record could not be validated.",
        "plan_record_unavailable": "The stored plan could not be validated.",
        "record_invalid": "A required record failed validation.",
        "read_limit_reached": "The bounded observation read limit was reached.",
        "read_deadline_reached": "The bounded observation deadline was reached.",
    }
    row = {
        "operation_id": "gq_" + identifier.replace("-", ""),
        "kind": "research",
        "operation_type": "general_question",
        "entity_id": identifier,
        "title": context["request"]["question"][:300],
        "status": state,
        "market_scope": copy.deepcopy(markets),
        "updated_at": record["recorded_at"]
        if record is not None
        else context["admission"]["admitted_at"],
        "evidence_readiness": "unchecked",
        "gaps": [messages[code] for code in missing],
        "next_operation": {
            "action": "open_question_request",
            "entity_id": identifier,
            "label": "Open question",
            "available": True,
            "reason": None,
        },
    }
    return row, record, missing, reasons


def _inventory_reply(rows, *, generation, observed_at, boundary, reasons, invalid_count):
    statuses = dict.fromkeys(_STATUSES, 0)
    for row in rows:
        statuses[row["status"]] += 1
    complete = not reasons
    ordered = sorted(rows, key=lambda row: row["operation_id"])
    ordered.sort(
        key=lambda row: (
            datetime.fromisoformat(row["updated_at"]).astimezone(UTC)
            if row["updated_at"] is not None
            else datetime.min.replace(tzinfo=UTC)
        ),
        reverse=True,
    )
    returned = ordered[:50]
    state = "complete" if complete else "partial" if generation is not None else "unavailable"
    coverage = {
        "state": state,
        "observed_at": observed_at,
        "window": {
            "basis": "retained_question_ledger",
            "start": min(
                (row["updated_at"] for row in rows if row["updated_at"]),
                key=lambda value: datetime.fromisoformat(value).astimezone(UTC),
                default=None,
            ),
            "end": boundary,
        }
        if state != "unavailable"
        else None,
        "known_count": len(rows),
        "known_by_status": statuses,
        "total_count": len(rows) if complete else None,
        "returned_count": len(returned),
        "unread_count": 0 if complete else None,
        "invalid_count": invalid_count,
        "reasons": sorted(set(reasons)),
    }
    result = {
        "contract_version": "general_question_observe_reply_v1",
        "mode": "inventory",
        "ledger_generation": generation,
        "coverage": coverage,
        "rows": returned,
    }
    while len(canonical_bytes(result)) + 1 > _OUTPUT_BYTES and returned:
        returned.pop()
        coverage["returned_count"] = len(returned)
    return result


def observe_question_requests(value, *, store, now=None):
    value = validate_observe_request(value)
    boundary = (
        utc_time(datetime.now(UTC) if now is None else now).isoformat().replace("+00:00", "Z")
    )
    scope, identifier = value["scope"], value["request_id"]
    rows, reasons = [], []
    generation = observed_at = None
    invalid_count = 0
    with parent_read_attempt(store, deadline=monotonic() + _READ_SECONDS) as attempt:
        try:
            ledger, control = store._control()
            generation = str(ledger.generation)
            with attempt.freeze_ledger(ledger):
                if identifier is None:
                    try:
                        blob = store._objects.bucket.get_blob(
                            store._objects._name(_LEDGER),
                            generation=ledger.generation,
                            timeout=attempt.before_read(),
                            retry=None,
                        )
                        if (
                            blob is None
                            or type(blob.generation) is not int
                            or blob.generation != ledger.generation
                            or not isinstance(blob.updated, datetime)
                            or blob.updated.utcoffset() is None
                        ):
                            raise QuestionStoreError("storage_unavailable")
                    except QuestionStoreError:
                        raise
                    except Exception as error:
                        raise QuestionStoreError("storage_unavailable") from error
                    observed_at = utc_time(blob.updated).isoformat().replace("+00:00", "Z")
                identifiers = (
                    [identifier] if identifier is not None else sorted(control["requests"])
                )
                for current_id in identifiers:
                    row = control["requests"].get(current_id)
                    if row is None:
                        if identifier is not None:
                            raise QuestionStoreError("observation_request_unavailable")
                        continue
                    try:
                        context = _request_context(store, current_id, row, scope)
                        if context is None:
                            if identifier is not None:
                                raise QuestionStoreError("observation_request_unavailable")
                            continue
                        projected, record, missing, errors = _observation(store, context, scope)
                        if identifier is not None:
                            if row.get("result") is not None and record is None:
                                raise QuestionStoreError("observation_invalid")
                            return {
                                "contract_version": "general_question_observe_reply_v1",
                                "mode": "detail",
                                "ledger_generation": generation,
                                "invocation": {
                                    "contract_version": "general_cultural_question_v1",
                                    "request_id": identifier,
                                    **{
                                        key: row[key]
                                        for key in (
                                            "request_digest",
                                            "intake_digest",
                                            "policy_digest",
                                            "deployment_digest",
                                        )
                                    },
                                },
                                "request_generation": row["request_generation"],
                                "intake_generation": row["intake_generation"],
                                "observed_state": projected["status"],
                                "result_pointer": _ack(context, record)
                                if record is not None
                                else None,
                                "reserved_microusd": row["reserved_microusd"],
                                "missing_work": missing,
                            }
                        rows.append(projected)
                        reasons.extend(errors)
                        if attempt.failed or any(
                            reason in {"read_limit_reached", "read_deadline_reached"}
                            for reason in errors
                        ):
                            break
                    except (ValueError, QuestionStoreError, KeyError, TypeError) as error:
                        if identifier is not None:
                            if (
                                isinstance(error, QuestionStoreError)
                                and error.code == "observation_request_unavailable"
                            ):
                                raise
                            raise QuestionStoreError("observation_invalid") from error
                        reason = _reason(error)
                        reasons.append(reason)
                        invalid_count = (
                            invalid_count + 1
                            if isinstance(error, _ScopedInvalid) and invalid_count is not None
                            else None
                        )
                        if attempt.failed or reason in {
                            "read_limit_reached",
                            "read_deadline_reached",
                        }:
                            break
        except (ValueError, QuestionStoreError, KeyError, TypeError, AttributeError) as error:
            if identifier is not None:
                if (
                    isinstance(error, QuestionStoreError)
                    and error.code == "observation_request_unavailable"
                ):
                    raise
                raise QuestionStoreError("observation_invalid") from error
            reasons.append(_reason(error))
            invalid_count = None
    return _inventory_reply(
        rows,
        generation=generation,
        observed_at=observed_at,
        boundary=boundary,
        reasons=reasons,
        invalid_count=invalid_count,
    )
