import copy
import importlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from google.genai import types

from tests.unit import test_general_question_store as f


def setup():
    module = importlib.import_module("src.analysis.open_intelligence.general_question_calls")
    store, bucket = f.store()
    f.activate_fixture(bucket)
    _, request, intake = f.prepared()
    store.admit(request, intake, scope=f.scope(), now=f.NOW)
    return module, module.GeneralQuestionCalls(store), bucket, request["request_id"]


def claim(calls, request_id, stage="planning", **overrides):
    values = {
        "stage": stage,
        "scope": f.scope(),
        "input_digest": "a" * 64,
        "system_instruction_digest": "b" * 64,
        "response_schema_digest": "c" * 64,
        "counted_input_tokens": 500,
        "now": f.NOW + timedelta(seconds=2 if stage == "answering" else 0),
    }
    values.update(overrides)
    return calls.claim_call(request_id, **values)


def native(*, complete=True, output=100):
    return types.GenerateContentResponse(
        model_version="gemini-3.5-flash",
        candidates=[
            types.Candidate(content=types.Content(parts=[types.Part(text="not parsed JSON")]))
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=500,
            candidates_token_count=output,
            thoughts_token_count=20,
            total_token_count=520 + output,
        )
        if complete
        else None,
    ).model_dump(mode="json")


def test_one_claim_even_after_lost_ack_and_matching_later_invocation():
    module, calls, bucket, rid = setup()
    bucket.lose_ack.add(f.PREFIX + f.LEDGER)
    permit = claim(calls, rid)
    assert permit.max_output_tokens == 800
    assert permit.model == "gemini-3.5-flash"
    with pytest.raises(module.QuestionStoreError, match="call_already_started"):
        claim(calls, rid)
    assert json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])["reserved_microusd"] == 100000


def test_racing_claims_issue_only_one_submission_permit():
    module, calls, bucket, rid = setup()
    barrier = threading.Barrier(2)
    guard = threading.Lock()
    attempts = [0]

    def race(name):
        if name == f.PREFIX + f.LEDGER:
            with guard:
                attempts[0] += 1
                wait = attempts[0] <= 2
            if wait:
                barrier.wait(timeout=3)

    bucket.before_upload = race

    def invoke(_):
        try:
            return claim(calls, rid)
        except module.QuestionStoreError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, range(2)))
    assert sum(isinstance(value, module.CallSubmissionPermit) for value in results) == 1
    assert results.count("call_already_started") == 1


def test_native_response_before_parse_and_exact_usage_gates_answering():
    module, calls, _, rid = setup()
    permit = claim(calls, rid)
    response = native()
    snapshot = calls.record_response(
        permit, scope=f.scope(), response=response, received_at=f.NOW + timedelta(seconds=1)
    )
    assert snapshot["raw_sdk_response"] == response
    assert calls.read_response(rid, stage="planning", scope=f.scope()) == snapshot
    with pytest.raises(module.QuestionStoreError, match="usage_unresolved"):
        claim(calls, rid, "answering")
    events = []
    event = calls.persist_usage(
        rid, stage="planning", scope=f.scope(), persist=lambda e: events.append(e) or e
    )
    assert event.completion_tokens == 120
    assert event.run_id == "question_" + rid.replace("-", "")
    assert event.market is None
    assert event.recorded_at == f.NOW + timedelta(seconds=1)
    answer = claim(calls, rid, "answering")
    assert answer.max_output_tokens == 3880
    again = calls.persist_usage(
        rid, stage="planning", scope=f.scope(), persist=lambda e: events.append(e) or e
    )
    assert again == event
    assert all(e == event for e in events)


def test_failed_usage_can_reconcile_but_never_enables_answering():
    module, calls, _, rid = setup()
    permit = claim(calls, rid)
    calls.record_response(
        permit, scope=f.scope(), response=native(), received_at=f.NOW + timedelta(seconds=1)
    )
    attempted = []

    def failed(event):
        attempted.append(event)
        raise TimeoutError("unknown acknowledgement")

    with pytest.raises(module.QuestionStoreError, match="metering_failed"):
        calls.persist_usage(rid, stage="planning", scope=f.scope(), persist=failed)
    recovered = calls.persist_usage(rid, stage="planning", scope=f.scope(), persist=lambda e: e)
    assert recovered == attempted[0]
    with pytest.raises(module.QuestionStoreError, match="metering_failed"):
        claim(calls, rid, "answering")


def test_incomplete_usage_retains_entire_native_snapshot():
    module, calls, _, rid = setup()
    permit = claim(calls, rid)
    response = native(complete=False)
    calls.record_response(
        permit, scope=f.scope(), response=response, received_at=f.NOW + timedelta(seconds=1)
    )
    assert (
        calls.read_response(rid, stage="planning", scope=f.scope())["raw_sdk_response"] == response
    )
    with pytest.raises(module.QuestionStoreError, match="usage_unknown"):
        calls.persist_usage(rid, stage="planning", scope=f.scope(), persist=lambda e: e)
    with pytest.raises(module.QuestionStoreError, match="usage_unresolved"):
        claim(calls, rid, "answering")


def test_raw_http_mode_usage_is_metered_before_invalid_plan_text_is_rejected():
    from src.analysis.open_intelligence.general_question_response import question_response_text

    _module, calls, _, rid = setup()
    permit = claim(calls, rid)
    raw_response = {
        "sdk_http_response": {
            "body": json.dumps(
                {
                    "modelVersion": "gemini-3.5-flash",
                    "usageMetadata": {
                        "promptTokenCount": 500,
                        "candidatesTokenCount": 100,
                        "thoughtsTokenCount": 20,
                        "totalTokenCount": 620,
                    },
                    "candidates": [{"content": {"parts": [{"text": "not JSON"}]}}],
                }
            )
        }
    }
    snapshot = calls.record_response(
        permit, scope=f.scope(), response=raw_response, received_at=f.NOW + timedelta(seconds=1)
    )
    assert snapshot["raw_sdk_response"] == raw_response
    event = calls.persist_usage(rid, stage="planning", scope=f.scope(), persist=lambda event: event)
    assert event.prompt_tokens == 500
    assert event.completion_tokens == 120
    with pytest.raises(ValueError):
        question_response_text(raw_response)
    assert calls.read_response(rid, stage="planning", scope=f.scope()) == snapshot


def test_response_conflict_and_late_claim_do_not_create_another_permit(monkeypatch):
    module, calls, bucket, rid = setup()
    elapsed = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: elapsed[0])

    def slow(name):
        if name == f.PREFIX + f.LEDGER:
            elapsed[0] += 5

    bucket.before_upload = slow
    with pytest.raises(module.QuestionStoreError, match="request_expired"):
        claim(calls, rid, now=f.NOW + timedelta(seconds=239))
    with pytest.raises(module.QuestionStoreError, match="call_already_started"):
        claim(calls, rid)


def test_altered_known_response_and_scope_are_refused():
    module, calls, _, rid = setup()
    permit = claim(calls, rid)
    response = native()
    calls.record_response(
        permit, scope=f.scope(), response=response, received_at=f.NOW + timedelta(seconds=1)
    )
    changed = copy.deepcopy(response)
    changed["response_id"] = "different"
    with pytest.raises(module.QuestionStoreError, match="run_id_conflict"):
        calls.record_response(
            permit, scope=f.scope(), response=changed, received_at=f.NOW + timedelta(seconds=1)
        )
    with pytest.raises(module.QuestionStoreError, match="scope_invalid"):
        calls.read_response(rid, stage="planning", scope={**f.scope(), "client_scope_id": "other"})


def test_orphan_known_response_can_bind_after_crash_without_another_permit(monkeypatch):
    module, calls, _, rid = setup()
    permit = claim(calls, rid)
    original = calls.store._objects.compare_control
    monkeypatch.setattr(calls.store._objects, "compare_control", lambda *args: False)
    with pytest.raises(module.QuestionStoreError, match="control_conflict"):
        calls.record_response(
            permit, scope=f.scope(), response=native(), received_at=f.NOW + timedelta(seconds=1)
        )
    monkeypatch.setattr(calls.store._objects, "compare_control", original)
    snapshot = calls.recover_response(rid, stage="planning", scope=f.scope())
    assert snapshot["raw_sdk_response"] == native()
    with pytest.raises(module.QuestionStoreError, match="call_already_started"):
        claim(calls, rid)


@pytest.mark.parametrize(
    "mutation",
    [
        "model",
        "boolean_usage",
        "over_budget",
        "ack_mismatch",
        "missing_thoughts",
        "missing_total",
        "wrong_total",
    ],
)
def test_unsafe_usage_never_unlocks_answering(mutation):
    module, calls, _, rid = setup()
    permit = claim(calls, rid)
    response = native(output=900 if mutation == "over_budget" else 100)
    if mutation == "model":
        response["model_version"] = "unapproved-model"
    if mutation == "boolean_usage":
        response["usage_metadata"]["prompt_token_count"] = True
    if mutation == "missing_thoughts":
        response["usage_metadata"]["thoughts_token_count"] = None
    if mutation == "missing_total":
        response["usage_metadata"]["total_token_count"] = None
    if mutation == "wrong_total":
        response["usage_metadata"]["total_token_count"] = 999
    calls.record_response(
        permit, scope=f.scope(), response=response, received_at=f.NOW + timedelta(seconds=1)
    )
    assert (
        calls.read_response(rid, stage="planning", scope=f.scope())["raw_sdk_response"] == response
    )
    if mutation == "over_budget":
        calls.persist_usage(rid, stage="planning", scope=f.scope(), persist=lambda e: e)
    else:
        with pytest.raises(module.QuestionStoreError):
            calls.persist_usage(
                rid,
                stage="planning",
                scope=f.scope(),
                persist=lambda e: None if mutation == "ack_mismatch" else e,
            )
    with pytest.raises(module.QuestionStoreError):
        claim(calls, rid, "answering")


def test_answering_rechecks_metering_failure_after_cas_conflict():
    module, calls, bucket, rid = setup()
    permit = claim(calls, rid)
    calls.record_response(
        permit, scope=f.scope(), response=native(), received_at=f.NOW + timedelta(seconds=1)
    )
    calls.persist_usage(rid, stage="planning", scope=f.scope(), persist=lambda e: e)
    altered = [False]

    def race(name):
        if name == f.PREFIX + f.LEDGER and not altered[0]:
            altered[0] = True
            control = json.loads(bucket.objects[name][1])
            control["requests"][rid]["execution"]["calls"]["planning"]["metering_failed"] = True
            bucket.seed(f.LEDGER, control)

    bucket.before_upload = race
    with pytest.raises(module.QuestionStoreError, match="metering_failed"):
        claim(calls, rid, "answering")
    control = json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])
    assert set(control["requests"][rid]["execution"]["calls"]) == {"planning"}


@pytest.mark.parametrize(
    "mutation",
    [
        "failed_false",
        "unknown_usage",
        "unknown_owner",
        "unknown_both",
        "pending_response",
        "pending_usage",
        "pending_owner",
        "acknowledged_response",
        "acknowledged_usage",
        "acknowledged_owner",
        "failed_response",
        "failed_usage",
        "failed_owner",
    ],
)
def test_corrupt_usage_state_refuses_recovery_before_sink_or_new_claim(mutation):
    module, calls, bucket, rid = setup()
    permit = claim(calls, rid)
    calls.record_response(
        permit, scope=f.scope(), response=native(), received_at=f.NOW + timedelta(seconds=1)
    )

    def fail(_event):
        raise TimeoutError("lost acknowledgement")

    with pytest.raises(module.QuestionStoreError, match="metering_failed"):
        calls.persist_usage(rid, stage="planning", scope=f.scope(), persist=fail)
    control = json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])
    call = control["requests"][rid]["execution"]["calls"]["planning"]
    if mutation == "failed_false":
        call["metering_failed"] = False
    elif mutation.startswith("unknown"):
        call["usage_status"] = "unknown"
        call["metering_failed"] = False
        if mutation == "unknown_usage":
            call["usage_owner_nonce"] = None
        elif mutation == "unknown_owner":
            call["usage"] = None
    else:
        status, missing = mutation.split("_")
        call["usage_status"] = status
        call[{"response": "response", "usage": "usage", "owner": "usage_owner_nonce"}[missing]] = (
            None
        )
    bucket.seed(f.LEDGER, control)
    before = bucket.objects[f.PREFIX + f.LEDGER]
    uploads = len(bucket.uploads)
    sink_calls = []
    with pytest.raises(module.QuestionStoreError, match="control_invalid"):
        calls.persist_usage(
            rid,
            stage="planning",
            scope=f.scope(),
            persist=lambda event: sink_calls.append(event) or event,
        )
    with pytest.raises(module.QuestionStoreError, match="control_invalid"):
        claim(calls, rid, "answering")
    assert sink_calls == []
    assert len(bucket.uploads) == uploads
    assert bucket.objects[f.PREFIX + f.LEDGER] == before


def test_valid_pending_usage_reconciles_without_reopening_generation():
    module, calls, bucket, rid = setup()
    permit = claim(calls, rid)
    calls.record_response(
        permit, scope=f.scope(), response=native(), received_at=f.NOW + timedelta(seconds=1)
    )

    class Interrupted(BaseException):
        pass

    def interrupted(_event):
        raise Interrupted()

    with pytest.raises(Interrupted):
        calls.persist_usage(rid, stage="planning", scope=f.scope(), persist=interrupted)
    control = json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])
    call = control["requests"][rid]["execution"]["calls"]["planning"]
    assert call["usage_status"] == "pending"
    assert call["metering_failed"] is False
    calls.persist_usage(rid, stage="planning", scope=f.scope(), persist=lambda event: event)
    control = json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])
    call = control["requests"][rid]["execution"]["calls"]["planning"]
    assert call["usage_status"] == "acknowledged"
    assert call["metering_failed"] is True
    with pytest.raises(module.QuestionStoreError, match="metering_failed"):
        claim(calls, rid, "answering")


def test_acknowledged_planning_still_limits_cumulative_answering_input():
    module, calls, _, rid = setup()
    permit = claim(calls, rid)
    calls.record_response(
        permit, scope=f.scope(), response=native(), received_at=f.NOW + timedelta(seconds=1)
    )
    calls.persist_usage(rid, stage="planning", scope=f.scope(), persist=lambda event: event)
    with pytest.raises(module.QuestionStoreError, match="budget_exhausted"):
        claim(calls, rid, "answering", counted_input_tokens=31501)


@pytest.mark.parametrize("thoughts", [None, 765])
def test_native_reconciled_thought_usage_is_metered(thoughts):
    _, calls, _, rid = setup()
    prompt, candidate = (1787, 439) if thoughts is None else (1806, 19)
    usage = {
        "promptTokenCount": prompt,
        "candidatesTokenCount": candidate,
        "totalTokenCount": prompt + candidate + (thoughts or 0),
    }
    if thoughts is not None:
        usage["thoughtsTokenCount"] = thoughts
    wire = {
        "sdk_http_response": {
            "body": json.dumps(
                {
                    "modelVersion": "gemini-3.5-flash",
                    "usageMetadata": usage,
                    "candidates": [{"finishReason": "STOP" if thoughts is None else "MAX_TOKENS"}],
                }
            )
        }
    }
    permit = claim(calls, rid, counted_input_tokens=prompt)
    calls.record_response(
        permit, scope=f.scope(), response=wire, received_at=f.NOW + timedelta(seconds=1)
    )
    events = []
    calls.persist_usage(
        rid, stage="planning", scope=f.scope(), persist=lambda event: events.append(event) or event
    )
    assert len(events) == 1
    assert calls.read_response(rid, stage="planning", scope=f.scope())["raw_sdk_response"] == wire
    assert calls._response(
        calls.read_response(rid, stage="planning", scope=f.scope())
    ).completion_tokens == candidate + (thoughts or 0)


@pytest.mark.parametrize(
    "mutation",
    [
        "gap",
        "missing_total",
        "null",
        "bool",
        "negative",
        "string",
        "bad_prompt",
        "tool_contradiction",
    ],
)
def test_absent_thought_inference_refuses_unproved_or_malformed_counters(mutation):
    module, calls, _, _ = setup()
    usage = {"promptTokenCount": 1787, "candidatesTokenCount": 439, "totalTokenCount": 2226}
    if mutation == "gap":
        usage["totalTokenCount"] += 1
    elif mutation == "missing_total":
        del usage["totalTokenCount"]
    elif mutation == "bad_prompt":
        usage["promptTokenCount"] = True
    elif mutation == "tool_contradiction":
        usage["toolUsePromptTokenCount"] = 1
    else:
        usage["thoughtsTokenCount"] = {"null": None, "bool": False, "negative": -1, "string": "0"}[
            mutation
        ]
    snapshot = {
        "binding": {"model": "gemini-3.5-flash"},
        "raw_sdk_response": {
            "sdk_http_response": {
                "body": json.dumps({"modelVersion": "gemini-3.5-flash", "usageMetadata": usage})
            }
        },
    }
    with pytest.raises(module.QuestionStoreError, match="usage_unknown"):
        calls._response(snapshot)


def test_terminal_missing_zero_usage_reconciles_without_replacing_result(monkeypatch):
    from src.analysis.open_intelligence.general_question_result import (
        observed_result_usage,
        unavailable_response,
    )
    from src.analysis.open_intelligence.general_question_usage import persist_question_usage_event

    module, calls, _, rid = setup()
    permit = claim(calls, rid, counted_input_tokens=1787)
    wire = {
        "sdk_http_response": {
            "body": json.dumps(
                {
                    "modelVersion": "gemini-3.5-flash",
                    "usageMetadata": {
                        "promptTokenCount": 1787,
                        "candidatesTokenCount": 439,
                        "totalTokenCount": 2226,
                    },
                    "candidates": [{"finishReason": "STOP"}],
                }
            )
        }
    }
    calls.record_response(
        permit, scope=f.scope(), response=wire, received_at=f.NOW + timedelta(seconds=1)
    )

    def legacy_refusal(*args):
        raise module.QuestionStoreError("usage_unknown")

    with monkeypatch.context() as old:
        old.setattr(calls, "_response", legacy_refusal)
        with pytest.raises(module.QuestionStoreError, match="usage_unknown"):
            calls.persist_usage(rid, stage="planning", scope=f.scope(), persist=lambda event: event)
    context = calls.store.read_request(rid, scope=f.scope())
    response = unavailable_response(
        context["request"],
        observed_result_usage(calls.store, request_id=rid, scope=f.scope()),
        reason="response_usage_unavailable",
    )
    result = calls.store.publish_result(
        rid, scope=f.scope(), response=response, state="held", now=f.NOW + timedelta(seconds=2)
    )
    result_key = f"requests/{rid}/results/{result['result_digest']}.json"
    before = calls.store._objects.read(result_key)
    _, control = calls.store._control()
    pointer = copy.deepcopy(control["requests"][rid]["result"])
    calls.persist_usage(
        rid,
        stage="planning",
        scope=f.scope(),
        persist=lambda event: persist_question_usage_event(
            event, store=calls.store, scope=f.scope()
        ),
    )
    after = calls.store._objects.read(result_key)
    assert after.raw == before.raw
    assert after.generation == before.generation
    _, control = calls.store._control()
    assert control["requests"][rid]["result"] == pointer
    assert len(control["requests"][rid]["execution"]["calls"]) == 1
    assert (
        control["requests"][rid]["execution"]["calls"]["planning"]["usage_status"] == "acknowledged"
    )
    assert calls.store._objects.read(f"requests/{rid}/usage/planning.json") is not None
