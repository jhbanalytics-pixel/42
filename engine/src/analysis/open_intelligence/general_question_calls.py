"""Durable individual call claims and recovery of already-known response bytes."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import asdict, dataclass
from datetime import timedelta
from time import monotonic
from uuid import uuid4

from src.analysis.gemini_client import BriefResponse
from src.analysis.open_intelligence.general_question_control import (
    CALL_BINDING_FIELDS,
    QuestionStoreError,
    control_time,
    require_digest,
    utc_time,
)
from src.analysis.open_intelligence.general_question_policy import require_question_admission_policy
from src.analysis.open_intelligence.general_question_response import decode_question_response
from src.analysis.open_intelligence.general_question_store import _pack
from src.contracts.open_intelligence_budget import (
    BudgetCeilingError,
    MeteringPersistenceError,
    OpenIntelligenceRunBudget,
)


@dataclass(frozen=True, slots=True)
class CallSubmissionPermit:
    request_id: str
    stage: str
    owner_nonce: str
    call_digest: str
    max_output_tokens: int
    model: str
    thinking_level: str


def _binding(call):
    return {key: call[key] for key in CALL_BINDING_FIELDS}


def _digest(value):
    return hashlib.sha256(_pack(value)).hexdigest()


def _time(value):
    return utc_time(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _event_json(event):
    value = asdict(event)
    value["trend_date"] = event.trend_date.isoformat()
    value["recorded_at"] = _time(event.recorded_at)
    return value


class GeneralQuestionCalls:
    def __init__(self, store):
        self.store = store

    def _context(self, request_id, scope):
        context = self.store.read_request(request_id, scope=scope)
        if context is None:
            raise QuestionStoreError("request_unknown")
        stored, control = self.store._control()
        admission = control["requests"].get(request_id)
        if admission is None or any(
            admission[key] != context["admission"][key]
            for key in ("request_digest", "intake_digest", "deployment_digest", "policy_digest")
        ):
            raise QuestionStoreError("control_conflict")
        if "execution" not in admission:
            raise QuestionStoreError("execution_unavailable")
        context["admission"] = admission
        return context, stored, control

    def _call(self, context, stage):
        if stage not in ("planning", "answering"):
            raise QuestionStoreError("stage_invalid")
        call = context["admission"]["execution"]["calls"].get(stage)
        if call is None:
            raise QuestionStoreError("call_unknown")
        return call

    def _live(self, context, control, now):
        admission = context["admission"]
        if admission.get("result") is not None:
            raise QuestionStoreError("request_terminal")
        if admission["deployment_digest"] != self.store.deployment_digest or (
            control["active_deployment_digest"] != self.store.deployment_digest
            or admission["policy_digest"] != self.store.policy["policy_digest"]
        ):
            raise QuestionStoreError("approval_required")
        try:
            require_question_admission_policy(self.store.policy, now=now)
            if (
                not control_time(admission["admitted_at"])
                <= now
                < control_time(admission["deadline_at"])
            ):
                raise ValueError()
        except ValueError as exc:
            raise QuestionStoreError("request_expired") from exc

    def _snapshot(self, context, stage):
        call = self._call(context, stage)
        pointer = call["response"]
        if pointer is None:
            raise QuestionStoreError("response_unknown")
        stored = self.store._objects.read(
            f"requests/{call['request_id']}/calls/{stage}.json",
            generation=int(pointer["generation"]),
        )
        if stored is None or hashlib.sha256(stored.raw).hexdigest() != pointer["digest"]:
            raise QuestionStoreError("response_unavailable")
        value = stored.value
        if set(value) != {
            "contract_version",
            "binding",
            "received_at",
            "raw_sdk_response",
            "response_model",
        } or (
            value["contract_version"] != "general_question_known_response_v1"
            or value["binding"] != _binding(call)
            or value["response_model"] != call["model"]
            or type(value["raw_sdk_response"]) is not dict
            or control_time(value["received_at"]) < control_time(call["started_at"])
        ):
            raise QuestionStoreError("response_invalid")
        return value

    def _response(self, snapshot):
        try:
            native = decode_question_response(snapshot["raw_sdk_response"])
        except ValueError as error:
            raise QuestionStoreError("usage_unknown") from error
        if native.get("model_version") != snapshot["binding"]["model"]:
            raise QuestionStoreError("response_model_invalid")
        usage = native.get("usage_metadata")
        if type(usage) is not dict:
            raise QuestionStoreError("usage_unknown")
        prompt = usage.get("prompt_token_count")
        candidate = usage.get("candidates_token_count")
        thoughts = usage.get("thoughts_token_count")
        total = usage.get("total_token_count")
        if (
            "thoughts_token_count" not in usage
            and all(type(value) is int and value >= 0 for value in (prompt, candidate, total))
            and total == prompt + candidate
        ):
            thoughts = 0
        tool_tokens = usage.get("tool_use_prompt_token_count")
        if tool_tokens is not None and (type(tool_tokens) is not int or tool_tokens != 0):
            raise QuestionStoreError("usage_unknown")
        if any(
            type(value) is not int or value < 0 for value in (prompt, candidate, thoughts, total)
        ):
            raise QuestionStoreError("usage_unknown")
        if prompt + candidate + thoughts != total:
            raise QuestionStoreError("usage_unknown")
        return BriefResponse({}, "", prompt, candidate + thoughts, native["model_version"], True)

    def _budget(self, context, stage):
        request = context["request"]
        budget = OpenIntelligenceRunBudget(
            run_id=request["run_id"],
            consumer="open_question_answer",
            trend_date=control_time(request["as_of"]).date(),
            market=request["market_scope"][0] if len(request["market_scope"]) == 1 else None,
        )
        if stage == "answering":
            prior = context["admission"]["execution"]["calls"].get("planning")
            if prior is None or prior["usage_status"] != "acknowledged":
                raise QuestionStoreError("usage_unresolved")
            if prior["metering_failed"]:
                raise QuestionStoreError("metering_failed")
            snapshot = self._snapshot(context, "planning")
            pointer = prior["usage"]
            stored = self.store._objects.read(
                f"requests/{request['request_id']}/usage/planning.json",
                generation=int(pointer["generation"]),
            )
            if stored is None or hashlib.sha256(stored.raw).hexdigest() != pointer["digest"]:
                raise QuestionStoreError("usage_unresolved")

            def acknowledged(event):
                if stored.value != _event_json(event):
                    raise QuestionStoreError("usage_unresolved")
                return event

            permit = budget.prepare_call("planning", "", lambda _: prior["counted_input_tokens"])
            budget.record_response(
                permit,
                self._response(snapshot),
                acknowledged,
                recorded_at=control_time(snapshot["received_at"]),
            )
        return budget

    def claim_call(
        self,
        request_id,
        *,
        stage,
        scope,
        input_digest,
        system_instruction_digest,
        response_schema_digest,
        counted_input_tokens,
        now,
    ):
        started = monotonic()
        now = utc_time(now)
        nonce = str(uuid4())
        if stage not in ("planning", "answering"):
            raise QuestionStoreError("stage_invalid")
        for digest in (input_digest, system_instruction_digest, response_schema_digest):
            require_digest(digest, "call_invalid")
        proposal = None
        for attempt in range(6):
            context, stored, control = self._context(request_id, scope)
            calls = context["admission"]["execution"]["calls"]
            budget = self._budget(context, stage)
            existing = calls.get(stage)
            if existing is not None:
                if proposal is None or _binding(existing) != _binding(proposal):
                    raise QuestionStoreError("call_already_started")
                self._live(context, control, now + timedelta(seconds=monotonic() - started))
                return CallSubmissionPermit(
                    request_id,
                    stage,
                    nonce,
                    _digest(_binding(existing)),
                    existing["max_output_tokens"],
                    existing["model"],
                    existing["thinking_level"],
                )
            if attempt == 5:
                break
            self._live(context, control, now + timedelta(seconds=monotonic() - started))
            if proposal is None:
                try:
                    permit = budget.prepare_call(stage, "", lambda _: counted_input_tokens)
                except (BudgetCeilingError, ValueError, TypeError) as exc:
                    raise QuestionStoreError("budget_exhausted") from exc
                admission = context["admission"]
                proposal = {
                    "request_id": request_id,
                    "stage": stage,
                    "owner_nonce": nonce,
                    **{
                        key: admission[key]
                        for key in (
                            "request_digest",
                            "intake_digest",
                            "policy_digest",
                            "deployment_digest",
                        )
                    },
                    "input_digest": input_digest,
                    "system_instruction_digest": system_instruction_digest,
                    "response_schema_digest": response_schema_digest,
                    "model": self.store.policy["model"],
                    "thinking_level": self.store.policy["stages"][stage]["thinking_level"],
                    "counted_input_tokens": counted_input_tokens,
                    "max_output_tokens": permit.max_output_tokens,
                    "started_at": _time(now + timedelta(seconds=monotonic() - started)),
                    "response": None,
                    "usage": None,
                    "usage_status": "unknown",
                    "usage_owner_nonce": None,
                    "metering_failed": False,
                }
            calls[stage] = copy.deepcopy(proposal)
            self._live(context, control, now + timedelta(seconds=monotonic() - started))
            self.store._objects.compare_control(stored.generation, control)
        raise QuestionStoreError("control_conflict")

    def _update(self, request_id, stage, scope, mutate):
        for _ in range(5):
            context, stored, control = self._context(request_id, scope)
            call = self._call(context, stage)
            before = copy.deepcopy(call)
            mutate(call)
            if call == before:
                return context
            self.store._objects.compare_control(stored.generation, control)
        context, _, _ = self._context(request_id, scope)
        call = self._call(context, stage)
        before = copy.deepcopy(call)
        mutate(call)
        if before != call:
            raise QuestionStoreError("control_conflict")
        return context

    def record_response(self, permit, *, scope, response, received_at):
        if type(permit) is not CallSubmissionPermit or type(response) is not dict:
            raise QuestionStoreError("response_invalid")
        context, _, _ = self._context(permit.request_id, scope)
        call = self._call(context, permit.stage)
        if (
            call["owner_nonce"] != permit.owner_nonce
            or _digest(_binding(call)) != permit.call_digest
        ):
            raise QuestionStoreError("call_invalid")
        snapshot = {
            "contract_version": "general_question_known_response_v1",
            "binding": _binding(call),
            "received_at": _time(received_at),
            "response_model": call["model"],
            "raw_sdk_response": copy.deepcopy(response),
        }
        if control_time(snapshot["received_at"]) < control_time(call["started_at"]):
            raise QuestionStoreError("response_invalid")
        stored = self.store._objects.create(
            f"requests/{permit.request_id}/calls/{permit.stage}.json", snapshot
        )
        pointer = {
            "generation": str(stored.generation),
            "digest": hashlib.sha256(stored.raw).hexdigest(),
        }

        def bind(current):
            if _binding(current) != _binding(call) or current["response"] not in (None, pointer):
                raise QuestionStoreError("response_invalid")
            current["response"] = pointer

        context = self._update(permit.request_id, permit.stage, scope, bind)
        return self._snapshot(context, permit.stage)

    def read_response(self, request_id, *, stage, scope):
        context, _, _ = self._context(request_id, scope)
        return self._snapshot(context, stage)

    def recover_response(self, request_id, *, stage, scope):
        context, _, _ = self._context(request_id, scope)
        call = self._call(context, stage)
        if call["response"] is not None:
            return self._snapshot(context, stage)
        stored = self.store._objects.read(f"requests/{request_id}/calls/{stage}.json")
        if stored is None:
            raise QuestionStoreError("response_unknown")
        pointer = {
            "generation": str(stored.generation),
            "digest": hashlib.sha256(stored.raw).hexdigest(),
        }
        probe = copy.deepcopy(context)
        probe["admission"]["execution"]["calls"][stage]["response"] = pointer
        self._snapshot(probe, stage)

        def bind(current):
            if _binding(current) != _binding(call) or current["response"] not in (None, pointer):
                raise QuestionStoreError("response_invalid")
            current["response"] = pointer

        return self._snapshot(self._update(request_id, stage, scope, bind), stage)

    def persist_usage(self, request_id, *, stage, scope, persist):
        context, _, _ = self._context(request_id, scope)
        call = self._call(context, stage)
        snapshot = self._snapshot(context, stage)
        budget = self._budget(context, stage)
        permit = budget.prepare_call(stage, "", lambda _: call["counted_input_tokens"])
        response = self._response(snapshot)
        received = control_time(snapshot["received_at"])
        event = budget.record_response(permit, response, lambda event: event, recorded_at=received)
        if call["usage_status"] == "acknowledged":
            pointer = call["usage"]
            known = self.store._objects.read(
                f"requests/{request_id}/usage/{stage}.json", generation=int(pointer["generation"])
            )
            if (
                known is None
                or hashlib.sha256(known.raw).hexdigest() != pointer["digest"]
                or known.value != _event_json(event)
            ):
                raise QuestionStoreError("usage_unresolved")
            return event
        stored = self.store._objects.create(
            f"requests/{request_id}/usage/{stage}.json", _event_json(event)
        )
        pointer = {
            "generation": str(stored.generation),
            "digest": hashlib.sha256(stored.raw).hexdigest(),
        }
        usage_nonce = str(uuid4())

        def pending(current):
            if current["usage"] not in (None, pointer):
                raise QuestionStoreError("usage_unresolved")
            current["usage"] = pointer
            if current["usage_status"] == "acknowledged":
                return
            if current["usage_status"] == "pending" and current["usage_owner_nonce"] != usage_nonce:
                current["metering_failed"] = True
            current["usage_status"] = "pending"
            current["usage_owner_nonce"] = usage_nonce

        prepared = self._update(request_id, stage, scope, pending)
        if self._call(prepared, stage)["usage_status"] == "acknowledged":
            return event
        try:
            if persist(event) != event:
                raise MeteringPersistenceError("usage acknowledgement mismatch")
        except Exception as exc:

            def failed(current):
                current["metering_failed"] = True
                current["usage_status"] = "failed"

            self._update(request_id, stage, scope, failed)
            raise QuestionStoreError("metering_failed") from exc

        def acknowledge(current):
            current["usage_status"] = "acknowledged"

        self._update(request_id, stage, scope, acknowledge)
        return event
