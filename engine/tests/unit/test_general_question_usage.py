import copy
import hashlib
import importlib
import json
from datetime import timedelta

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes

from tests.unit import test_general_question_calls as c
from tests.unit import test_general_question_store as f


def read(calls, rid):
    module = importlib.import_module("src.analysis.open_intelligence.general_question_usage")
    return module.read_question_usage(calls.store, request_id=rid, scope=f.scope())


def respond(calls, rid, stage="planning", response=None, acknowledge=True):
    permit = c.claim(calls, rid, stage)
    calls.record_response(
        permit,
        scope=f.scope(),
        response=c.native() if response is None else response,
        received_at=f.NOW + timedelta(seconds=3 if stage == "answering" else 1),
    )
    event = None
    if acknowledge:
        event = calls.persist_usage(rid, stage=stage, scope=f.scope(), persist=lambda event: event)
    return permit, event


def test_no_calls_keeps_reservation_and_observed_zero_without_writes():
    _, calls, bucket, rid = c.setup()
    before = copy.deepcopy(bucket.objects)
    value = read(calls, rid)
    assert value == {
        "status": "resolved",
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "usage_receipt_ids": [],
        "call_receipt_ids": [],
        "reservation_ids": [rid],
        "reserved_cost_usd": "0.100000",
        "reason": None,
    }
    assert bucket.objects == before


def test_held_call_is_unknown_and_references_existing_marker():
    _, calls, bucket, rid = c.setup()
    permit = c.claim(calls, rid)
    before = copy.deepcopy(bucket.objects)
    value = read(calls, rid)
    assert value["status"] == "unresolved"
    assert value["reason"] == "response_usage_unavailable"
    assert all(value[key] is None for key in ("model_calls", "input_tokens", "output_tokens"))
    assert value["call_receipt_ids"] == [permit.call_digest]
    assert value["reservation_ids"] == [rid]
    assert value["usage_receipt_ids"] == []
    assert bucket.objects == before


def test_exact_two_stage_usage_includes_thought_tokens_and_is_read_only():
    _, calls, bucket, rid = c.setup()
    planning, p_event = respond(calls, rid)
    answering, a_event = respond(calls, rid, "answering")
    before = copy.deepcopy(bucket.objects)
    value = read(calls, rid)
    assert value["status"] == "resolved"
    assert (value["model_calls"], value["input_tokens"], value["output_tokens"]) == (2, 1000, 240)
    assert value["call_receipt_ids"] == [planning.call_digest, answering.call_digest]
    assert value["usage_receipt_ids"] == [p_event.usage_id, a_event.usage_id]
    assert value["reason"] is None
    assert read(calls, rid) == value
    assert bucket.objects == before


def test_unknown_second_call_does_not_turn_known_first_stage_into_total():
    _, calls, _, rid = c.setup()
    _, event = respond(calls, rid)
    c.claim(calls, rid, "answering")
    value = read(calls, rid)
    assert all(value[key] is None for key in ("model_calls", "input_tokens", "output_tokens"))
    assert value["usage_receipt_ids"] == [event.usage_id]
    assert len(value["call_receipt_ids"]) == 2


@pytest.mark.parametrize(
    "missing", ["thoughts_token_count", "prompt_token_count", "total_token_count"]
)
def test_missing_usage_preserves_independently_observed_counters(missing):
    _, calls, _, rid = c.setup()
    response = c.native()
    response["usage_metadata"].pop(missing)
    respond(calls, rid, response=response, acknowledge=False)
    value = read(calls, rid)
    assert value["status"] == "unresolved"
    assert value["reason"] == "response_usage_unavailable"
    assert value["model_calls"] == 1
    assert value["input_tokens"] == (None if missing == "prompt_token_count" else 500)
    assert value["output_tokens"] == (None if missing == "thoughts_token_count" else 120)


def test_failed_metering_retains_measured_counts_and_can_reconcile_without_generation():
    module, calls, bucket, rid = c.setup()
    respond(calls, rid, acknowledge=False)

    def fail(_event):
        raise TimeoutError("synthetic metering failure")

    with pytest.raises(module.QuestionStoreError, match="metering_failed"):
        calls.persist_usage(rid, stage="planning", scope=f.scope(), persist=fail)
    before = copy.deepcopy(bucket.objects)
    value = read(calls, rid)
    assert value["reason"] == "metering_persistence_failed"
    assert (value["model_calls"], value["input_tokens"], value["output_tokens"]) == (1, 500, 120)
    assert value["usage_receipt_ids"] == []
    assert bucket.objects == before
    event = calls.persist_usage(rid, stage="planning", scope=f.scope(), persist=lambda e: e)
    assert read(calls, rid)["usage_receipt_ids"] == [event.usage_id]
    assert read(calls, rid)["status"] == "resolved"
    with pytest.raises(module.QuestionStoreError, match="metering_failed"):
        c.claim(calls, rid, "answering")


@pytest.mark.parametrize("mutation", ["missing_generation", "hash", "coherent_event"])
def test_acknowledged_usage_requires_exact_response_event_readback(mutation):
    module, calls, bucket, rid = c.setup()
    respond(calls, rid)
    control = json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])
    pointer = control["requests"][rid]["execution"]["calls"]["planning"]["usage"]
    name = f.PREFIX + f"requests/{rid}/usage/planning.json"
    version_key = (name, int(pointer["generation"]))
    if mutation == "missing_generation":
        del bucket.versions[version_key]
    else:
        value = json.loads(bucket.versions[version_key])
        value["prompt_tokens"] += 1
        raw = canonical_bytes(value)
        bucket.versions[version_key] = raw
        if mutation == "coherent_event":
            pointer["digest"] = hashlib.sha256(raw).hexdigest()
            bucket.seed(f.LEDGER, control)
    with pytest.raises(module.QuestionStoreError):
        read(calls, rid)


@pytest.mark.parametrize("value", [True, -1, 999, "620"])
def test_invalid_or_contradictory_usage_total_is_refused(value):
    module, calls, _, rid = c.setup()
    response = c.native()
    response["usage_metadata"]["total_token_count"] = value
    respond(calls, rid, response=response, acknowledge=False)
    with pytest.raises(module.QuestionStoreError):
        read(calls, rid)


def test_read_failure_cannot_become_zero_usage():
    module, calls, bucket, rid = c.setup()
    bucket.fail_reads = True
    with pytest.raises(module.QuestionStoreError):
        read(calls, rid)


def test_new_call_during_projection_refuses_stale_zero(monkeypatch):
    module, calls, _, rid = c.setup()
    original = calls.store.read_request
    reads = [0]

    def concurrent_read(*args, **kwargs):
        reads[0] += 1
        if reads[0] == 2:
            monkeypatch.setattr(calls.store, "read_request", original)
            c.claim(calls, rid)
        return original(*args, **kwargs)

    monkeypatch.setattr(calls.store, "read_request", concurrent_read)
    with pytest.raises(module.QuestionStoreError, match="control_conflict"):
        read(calls, rid)


def test_result_selected_between_projection_reads_is_a_stale_read_not_a_conflict(monkeypatch):
    """The projection reads the admission twice. A publication landing between the two
    reads moves only the result pointer, which this projection never reads: that is a
    stale read for the caller to repeat, not a conflict."""
    _, calls, _, rid = c.setup()
    respond(calls, rid)
    result = importlib.import_module("src.analysis.open_intelligence.general_question_result")
    expected = read(calls, rid)
    original = calls.store.read_request
    reads = [0]

    def concurrent_read(*args, **kwargs):
        reads[0] += 1
        if reads[0] == 2:
            monkeypatch.setattr(calls.store, "read_request", original)
            context = original(rid, scope=f.scope())
            calls.store.publish_result(
                rid,
                scope=f.scope(),
                response=result.unavailable_response(
                    context["request"], read(calls, rid), reason="request_expired"
                ),
                state="unavailable",
                now=f.NOW,
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(calls.store, "read_request", concurrent_read)
    assert read(calls, rid) == expected
    assert reads[0] == 2
