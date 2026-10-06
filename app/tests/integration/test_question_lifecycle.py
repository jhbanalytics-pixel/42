"""Controlled question lifecycle cases: real store CAS in the engine, fake provider IO, routes in-process.

The engine half runs under the configured engine interpreter against the in-memory
generation-checked bucket the engine unit suites already use. The app half drives the
routes layer with a durable admission double and a barrier before queue dispatch.
"""

import asyncio
import json
import os
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

from src.api.question_worker_process import WorkerProcessError
from tests.unit.test_general_question_routes import DurableAdmissionWorker
from tests.unit.test_general_question_routes import module as routes_module
from tests.unit.test_question_worker_result import EXPECTED

# Fake provider usage on every completed call: 50 prompt tokens, 10 candidate and 5 thought
# tokens, which the engine meters as 15 completion tokens. The requests that complete exactly one
# metered call are fixed by the scenario: 2 (queue wait), 5 (cancel after start, late response
# accounted), 8 (ambiguous publication), 9 (restart, late response accounted), 10 (pointer absent
# then recovered) and 12 (projection interrupted then recovered). Requests 6, 7 and 11 hold with
# unknown usage and every other request never reaches the provider.
PROVIDER_TOKENS = {"prompt": 50, "candidates": 10, "thoughts": 5}
METERED_COMPLETION_TOKENS = PROVIDER_TOKENS["candidates"] + PROVIDER_TOKENS["thoughts"]
SETTLED_REQUESTS = (2, 5, 8, 9, 10, 12)
CALLS_PER_SETTLED_REQUEST = 1
STAGE_EVENT_FIELDS = {
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

ENGINE_HARNESS = r'''
import asyncio
import json
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx

from src.analysis.open_intelligence import general_question_calls as calls_module
from src.analysis.open_intelligence import general_question_execution as execution
from src.analysis.open_intelligence import general_question_runtime as runtime
from src.analysis.open_intelligence.general_question_control import (
    REQUEST_MICROUSD,
    QuestionStoreError,
)
from src.analysis.open_intelligence.general_question_policy import build_intake_context
from src.analysis.open_intelligence.general_question_request import normalize_question_request
from src.analysis.open_intelligence.general_question_usage import read_question_usage
from tests.unit import test_general_question_runtime as runtime_fixture
from tests.unit import test_general_question_store as f

store, bucket, first_invocation, identity, credentials = runtime_fixture.setup_runtime()
SCOPE = f.scope()
PROVIDER_TOKENS = {"prompt": 50, "candidates": 10, "thoughts": 5}
LOCK = threading.Lock()
EVENTS = []
PERMITS = []
GENERATES = []
GATE = {"hold": None, "started": None}
DEATH = {"at": None, "request_id": None}
JUMP = [0.0]
LOSE_RESULT_ACK = set()
REPORT = {"cases": {}}


class Died(BaseException):
    """A worker process ended at a real boundary; nothing after it runs."""


def stamp(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def admission_reason(code):
    """The reason the app route records when admission fails with this code.

    Stage reasons are one closed vocabulary. The route in
    app/src/api/general_question_routes.py records a run id conflict as
    request_conflict, keeps a code inside the vocabulary, and records every
    other admission failure, control_conflict among them, as worker_failed.
    """
    if code == "run_id_conflict":
        return "request_conflict"
    return code if code in execution.STAGE_REASON_CODES else "worker_failed"


def admission_event(request_id, event, reason=None, started=None):
    started = started or time.monotonic()
    record = execution.validate_stage_event(
        {
            "event_version": "question_stage_event_v1",
            "request_id": request_id,
            "invocation_id": str(uuid4()),
            "policy_digest": store.policy["policy_digest"],
            "deployment_digest": store.deployment_digest,
            "stage": "admission",
            "event": event,
            "occurred_at": stamp(f.NOW),
            "elapsed_ms": min(600000, int((time.monotonic() - started) * 1000)),
            "reason_code": reason,
            "usage_state": "reserved" if event == "completed" else "none",
        }
    )
    with LOCK:
        EVENTS.append(record)


def handler(request):
    if request.url.path.endswith(":countTokens"):
        return httpx.Response(200, json={"totalTokens": 50})
    with LOCK:
        GENERATES.append(request.url.path)
    if DEATH["at"] == "provider":
        raise Died()
    hold = GATE["hold"]
    if hold is not None:
        GATE["started"].set()
        hold.wait(timeout=60)
    body = {
        "modelVersion": store.policy["model"],
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "role": "model",
                    "parts": [{"text": json.dumps(runtime_fixture.plan_draft())}],
                },
            }
        ],
        "usageMetadata": {
            "promptTokenCount": PROVIDER_TOKENS["prompt"],
            "candidatesTokenCount": PROVIDER_TOKENS["candidates"],
            "thoughtsTokenCount": PROVIDER_TOKENS["thoughts"],
            "totalTokenCount": sum(PROVIDER_TOKENS.values()),
        },
    }
    return httpx.Response(200, json=body)


httpx.AsyncHTTPTransport = lambda **_kwargs: httpx.MockTransport(handler)
real_connect = socket.socket.connect


def loopback_only(sock, address, *args):
    # The event loop self-pipe is loopback; any other destination is a test failure.
    if not (isinstance(address, tuple) and str(address[0]) in ("127.0.0.1", "::1")):
        raise RuntimeError("network connection attempted")
    return real_connect(sock, address, *args)


socket.socket.connect = loopback_only
execution.build_general_question_snapshot = lambda *a, **k: {
    "contract_version": "general_question_snapshot_build_v1",
    "status": "coverage_gap",
    "snapshot": None,
    "reason": "evidence_insufficient",
    "missing_work": ["no_matching_evidence"],
}
real_monotonic = runtime.monotonic
# One shared clock: a stalled worker sees wall time move on like every other process.
runtime.monotonic = lambda: real_monotonic() + JUMP[0]
execution.monotonic = lambda: real_monotonic() + JUMP[0]
original_claim = calls_module.GeneralQuestionCalls.claim_call


def recording_claim(self, request_id, **kwargs):
    permit = original_claim(self, request_id, **kwargs)
    with LOCK:
        PERMITS.append([permit.request_id, permit.stage, permit.owner_nonce])
    if DEATH["at"] == "timeout_after_ack" and request_id == DEATH["request_id"]:
        JUMP[0] = 61.0
    return permit


calls_module.GeneralQuestionCalls.claim_call = recording_claim
original_update = calls_module.GeneralQuestionCalls._update


def dying_update(self, request_id, stage, scope, mutate):
    if DEATH["at"] == "pointer" and request_id == DEATH["request_id"]:
        raise Died()
    return original_update(self, request_id, stage, scope, mutate)


calls_module.GeneralQuestionCalls._update = dying_update
original_persist_plan = runtime._persist_plan


def dying_persist_plan(store_, context, snapshot):
    if DEATH["at"] == "projection" and context["request"]["request_id"] == DEATH["request_id"]:
        raise Died()
    return original_persist_plan(store_, context, snapshot)


runtime._persist_plan = dying_persist_plan
original_persist_usage = execution.persist_question_usage_event


def failing_persist_usage(event, **kwargs):
    if DEATH["at"] == "usage_write" and event.run_id.endswith(DEATH["request_id"].replace("-", "")):
        raise TimeoutError("usage sink unavailable")
    return original_persist_usage(event, **kwargs)


execution.persist_question_usage_event = failing_persist_usage
original_upload = f.Blob.upload_from_string


def ambiguous_upload(self, data, **kwargs):
    original_upload(self, data, **kwargs)
    if "/results/" in self.name and any(rid in self.name for rid in LOSE_RESULT_ACK):
        LOSE_RESULT_ACK.discard(next(rid for rid in LOSE_RESULT_ACK if rid in self.name))
        raise TimeoutError("acknowledgement lost after commit")


f.Blob.upload_from_string = ambiguous_upload


def rid(index):
    return str(UUID(int=index))


def admit(index, *, intake=None, scope=None):
    _, request, prepared_intake = f.prepared(index)
    return store.admit(request, intake or prepared_intake, scope=scope or SCOPE, now=f.NOW)


def invocation_for(request_id):
    admission = store.read_request(request_id, scope=SCOPE)["admission"]
    return {
        "contract_version": "general_cultural_question_v1",
        "request_id": request_id,
        **{key: admission[key] for key in ("request_digest", "intake_digest", "policy_digest", "deployment_digest")},
    }


def execute(request_id, *, seconds):
    async def go():
        return await execution.execute_general_question(
            invocation_for(request_id),
            store=store,
            runtime_identity=identity,
            credentials=credentials,
            now=f.NOW + timedelta(seconds=seconds),
            stage_events=EVENTS.append,
        )

    try:
        return asyncio.run(go())
    except QuestionStoreError as error:
        return {"error": error.code}
    except Died:
        return {"died": True}


def status(request_id, *, seconds):
    return store.status(request_id, scope=SCOPE, now=f.NOW + timedelta(seconds=seconds))


def status_after(ack, *, seconds):
    """Status at the scenario time, or at the result's own stamp when that is later.

    The store stamps a result at the injected clock plus the real time the
    execution took, and refuses a status read earlier than that stamp. A read a
    fixed second after execution therefore failed whenever a loaded machine took
    longer than a second, so the read never precedes the result it reads.
    """
    recorded = datetime.fromisoformat(record_of(ack)["recorded_at"].replace("Z", "+00:00"))
    now = max(f.NOW + timedelta(seconds=seconds), recorded)
    return store.status(ack["request_id"], scope=SCOPE, now=now)


def record_of(ack):
    stored = store._objects.read(
        f"requests/{ack['request_id']}/results/{ack['result_digest']}.json",
        generation=int(ack["result_generation"]),
    )
    return stored.value


def permits_for(request_id):
    return [row for row in PERMITS if row[0] == request_id]


def attempt_admission(index):
    started = time.monotonic()
    admission_event(rid(index), "entered", started=started)
    try:
        admit(index)
    except QuestionStoreError as error:
        admission_event(rid(index), "failed", admission_reason(error.code), started=started)
        return error.code
    admission_event(rid(index), "completed", started=started)
    return "admitted"


def case_admission_conflict_exhausted():
    """Every ledger write meets a newer generation, so admission runs out of retries.

    Under load the twelve users race reaches this by chance; here a competing
    writer bumps the ledger before each conditional put, so it happens every run.
    """
    name = f.PREFIX + f.LEDGER

    def compete(uploading):
        if uploading != name:
            return
        with bucket.lock:
            _, raw = bucket.objects[name]
            bucket.counter += 1
            bucket.objects[name] = (bucket.counter, raw)
            bucket.versions[(name, bucket.counter)] = raw

    bucket.before_upload = compete
    try:
        outcome = attempt_admission(40)
    finally:
        bucket.before_upload = None
    REPORT["cases"]["admission_conflict_exhausted"] = {"outcome": outcome}


def case_twelve_users():
    barrier = threading.Barrier(12)
    count = [0]

    def race(name):
        if name != f.PREFIX + f.LEDGER:
            return
        with LOCK:
            count[0] += 1
            wait = count[0] <= 12
        if wait:
            barrier.wait(timeout=10)

    bucket.before_upload = race
    attempt = attempt_admission

    with ThreadPoolExecutor(max_workers=12) as pool:
        first_round = list(pool.map(attempt, range(2, 14)))
    bucket.before_upload = None
    retried = {}
    for index, outcome in zip(range(2, 14), first_round):
        if outcome != "admitted":
            retried[rid(index)] = attempt(index)
    ledger = json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])
    REPORT["cases"]["twelve_users"] = {
        "first_round": first_round,
        "retried": retried,
        "admitted": sorted(ledger["requests"]),
        "reserved_microusd": ledger["reserved_microusd"],
        "deadlines": sorted({row["deadline_at"] for row in ledger["requests"].values()}),
    }


def case_four_duplicates():
    barrier = threading.Barrier(4)
    count = [0]

    def race(name):
        if name != f.PREFIX + f.LEDGER:
            return
        with LOCK:
            count[0] += 1
            wait = count[0] <= 4
        if wait:
            barrier.wait(timeout=10)

    bucket.before_upload = race
    with ThreadPoolExecutor(max_workers=4) as pool:
        admissions = list(pool.map(lambda _: admit(14), range(4)))
    bucket.before_upload = None
    admission_event(rid(14), "entered")
    admission_event(rid(14), "completed")
    _, request, _ = f.prepared(14)
    changed = build_intake_context(request, selected_market="ng")
    try:
        admit(14, intake=changed)
        conflict = None
    except QuestionStoreError as error:
        conflict = error.code
    ledger = json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])
    REPORT["cases"]["four_duplicates"] = {
        "identical": all(item == admissions[0] for item in admissions),
        "count": len(admissions),
        "conflict": conflict,
        "ledger_entries": sum(1 for key in ledger["requests"] if key == rid(14)),
        "ledger_uploads": sum(1 for name, _ in bucket.uploads if name == f.PREFIX + f.LEDGER),
    }


def case_parent_from_another_scope():
    other = dict(SCOPE, client_scope_id="other_scope")
    policy, _, _ = f.prepared()
    parent = normalize_question_request(
        {"message": "Parent question in another client scope"},
        scope=other,
        request_id=rid(15),
        admitted_at=f.NOW,
        policy_digest=policy["policy_digest"],
    )
    store.admit(parent, build_intake_context(parent, selected_market="za"), scope=other, now=f.NOW)
    admission_event(rid(15), "entered")
    admission_event(rid(15), "completed")
    try:
        store.read_request(rid(15), scope=SCOPE)
        refusal = None
    except QuestionStoreError as error:
        refusal = error.code
    try:
        store.cancel(rid(15), scope=SCOPE, now=f.NOW)
        cancel_refusal = None
    except QuestionStoreError as error:
        cancel_refusal = error.code
    REPORT["cases"]["parent_from_another_scope"] = {
        "read_refusal": refusal,
        "cancel_refusal": cancel_refusal,
        "visible_in_own_scope": store.read_request(rid(15), scope=other) is not None,
    }


def case_queue_wait_most_of_deadline():
    observed = []
    original = runtime.next_stage_deadline

    def spy(admitted_deadline, now, stage_seconds):
        value = original(admitted_deadline, now, stage_seconds)
        observed.append([stamp(admitted_deadline), stamp(now), stage_seconds, stamp(value)])
        return value

    runtime.next_stage_deadline = spy
    ack = execute(rid(2), seconds=230)
    runtime.next_stage_deadline = original
    REPORT["cases"]["queue_wait_most_of_deadline"] = {
        "ack_state": ack.get("state"),
        "reason": record_of(ack)["response"]["reason"],
        "stage_deadlines": observed,
        "deadline_at": status_after(ack, seconds=231)["deadline_at"],
        "permits": len(permits_for(rid(2))),
    }


def case_expired_before_dispatch():
    ack = execute(rid(3), seconds=241)
    REPORT["cases"]["expired_before_dispatch"] = {
        "ack_state": ack.get("state"),
        "reason": record_of(ack)["response"]["reason"],
        "permits": len(permits_for(rid(3))),
        "usage": read_question_usage(store, request_id=rid(3), scope=SCOPE),
    }


def case_cancel_before_dispatch():
    cancelled = store.cancel(rid(4), scope=SCOPE, now=f.NOW + timedelta(seconds=5))
    ack = execute(rid(4), seconds=10)
    REPORT["cases"]["cancel_before_dispatch"] = {
        "cancel_state": cancelled["state"],
        "ack_state": ack.get("state"),
        "same_record": ack.get("result_digest") == cancelled["result_digest"],
        "reason": record_of(ack)["response"]["reason"],
        "permits": len(permits_for(rid(4))),
        "generates_before": len(GENERATES),
    }


def case_cancel_after_remote_call_start():
    GATE["hold"], GATE["started"] = threading.Event(), threading.Event()
    box = {}
    thread = threading.Thread(target=lambda: box.update(value=execute(rid(5), seconds=5)))
    thread.start()
    assert GATE["started"].wait(timeout=60)
    cancelled = store.cancel(rid(5), scope=SCOPE, now=f.NOW + timedelta(seconds=5))
    held_usage = read_question_usage(store, request_id=rid(5), scope=SCOPE)
    GATE["hold"].set()
    thread.join(timeout=60)
    GATE["hold"] = GATE["started"] = None
    final = status(rid(5), seconds=30)
    REPORT["cases"]["cancel_after_remote_call_start"] = {
        "cancel_state": cancelled["state"],
        "held_usage_status": held_usage["status"],
        "held_usage_reason": held_usage["reason"],
        "worker_ack_state": box["value"].get("state"),
        "final_state": final["state"],
        "reason": record_of(final)["response"]["reason"],
        "final_usage": read_question_usage(store, request_id=rid(5), scope=SCOPE),
        "permits": len(permits_for(rid(5))),
    }


def case_timeout_after_provider_acknowledgement():
    DEATH.update(at="timeout_after_ack", request_id=rid(6))
    first = execute(rid(6), seconds=5)
    DEATH.update(at=None, request_id=None)
    JUMP[0] = 0.0
    second = execute(rid(6), seconds=70)
    REPORT["cases"]["timeout_after_provider_acknowledgement"] = {
        "first_state": first.get("state"),
        "first_reason": record_of(first)["response"]["reason"],
        "second_state": second.get("state"),
        "same_record": first.get("result_digest") == second.get("result_digest"),
        "usage": read_question_usage(store, request_id=rid(6), scope=SCOPE),
        "permits": len(permits_for(rid(6))),
    }


def case_usage_write_failure():
    DEATH.update(at="usage_write", request_id=rid(7))
    ack = execute(rid(7), seconds=5)
    DEATH.update(at=None, request_id=None)
    ledger = json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])
    call = ledger["requests"][rid(7)]["execution"]["calls"]["planning"]
    REPORT["cases"]["usage_write_failure"] = {
        "ack_state": ack.get("state"),
        "reason": record_of(ack)["response"]["reason"],
        "usage_status": call["usage_status"],
        "metering_failed": call["metering_failed"],
        "reserved_microusd": ledger["requests"][rid(7)]["reserved_microusd"],
        "usage": read_question_usage(store, request_id=rid(7), scope=SCOPE),
        "permits": len(permits_for(rid(7))),
    }


def case_ambiguous_object_publication():
    LOSE_RESULT_ACK.add(rid(8))
    ack = execute(rid(8), seconds=5)
    result_uploads = [name for name, _ in bucket.uploads if f"requests/{rid(8)}/results/" in name]
    REPORT["cases"]["ambiguous_object_publication"] = {
        "ack_state": ack.get("state"),
        "result_uploads": len(result_uploads),
        "status_state": status_after(ack, seconds=6)["state"],
        "same_digest": status_after(ack, seconds=6)["result_digest"] == ack.get("result_digest"),
        "permits": len(permits_for(rid(8))),
    }


def case_worker_restart_and_late_response():
    GATE["hold"], GATE["started"] = threading.Event(), threading.Event()
    box = {}
    thread = threading.Thread(target=lambda: box.update(value=execute(rid(9), seconds=5)))
    thread.start()
    assert GATE["started"].wait(timeout=60)
    restarted = execute(rid(9), seconds=100)
    held = read_question_usage(store, request_id=rid(9), scope=SCOPE)
    JUMP[0] = 95.0
    GATE["hold"].set()
    thread.join(timeout=60)
    JUMP[0] = 0.0
    GATE["hold"] = GATE["started"] = None
    final = status(rid(9), seconds=120)
    REPORT["cases"]["worker_restart_and_late_response"] = {
        "restart_state": restarted.get("state"),
        "restart_reason": record_of(restarted)["response"]["reason"],
        "held_usage_status": held["status"],
        "late_worker_ack_state": box["value"].get("state"),
        "final_state": final["state"],
        "final_usage": read_question_usage(store, request_id=rid(9), scope=SCOPE),
        "permits": len(permits_for(rid(9))),
    }


def case_recovery_boundaries():
    outcomes = {}
    for index, boundary in ((10, "pointer"), (11, "provider"), (12, "projection")):
        DEATH.update(at=boundary, request_id=rid(index))
        died = execute(rid(index), seconds=5)
        DEATH.update(at=None, request_id=None)
        ledger = json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])
        call = ledger["requests"][rid(index)]["execution"]["calls"].get("planning")
        stored_bytes = f.PREFIX + f"requests/{rid(index)}/calls/planning.json" in bucket.objects
        recovered = execute(rid(index), seconds=20)
        outcomes[boundary] = {
            "died": died.get("died", False),
            "pointer_before_recovery": None if call is None else call["response"] is not None,
            "bytes_before_recovery": stored_bytes,
            "state": recovered.get("state"),
            "reason": record_of(recovered)["response"]["reason"],
            "usage": read_question_usage(store, request_id=rid(index), scope=SCOPE),
            "permits": len(permits_for(rid(index))),
            "reserved_microusd": ledger["requests"][rid(index)]["reserved_microusd"],
        }
    REPORT["cases"]["recovery_boundaries"] = outcomes


admission_event(first_invocation["request_id"], "entered")
admission_event(first_invocation["request_id"], "completed")
for case in (
    case_twelve_users,
    case_four_duplicates,
    case_parent_from_another_scope,
    case_queue_wait_most_of_deadline,
    case_expired_before_dispatch,
    case_cancel_before_dispatch,
    case_cancel_after_remote_call_start,
    case_timeout_after_provider_acknowledgement,
    case_usage_write_failure,
    case_ambiguous_object_publication,
    case_worker_restart_and_late_response,
    case_recovery_boundaries,
    case_admission_conflict_exhausted,
):
    case()

ledger = json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])
pricing = store.policy["pricing"]
settled = {}
for request_id, row in ledger["requests"].items():
    for stage, call in row.get("execution", {}).get("calls", {}).items():
        if call["usage_status"] != "acknowledged":
            continue
        stored = store._objects.read(
            f"requests/{request_id}/usage/{stage}.json", generation=int(call["usage"]["generation"])
        )
        event = stored.value
        cost = Decimal(event["prompt_tokens"]) * Decimal(pricing["input_usd_per_million"]) + Decimal(
            event["completion_tokens"]
        ) * Decimal(pricing["output_usd_per_million"])
        settled[request_id] = str(Decimal(settled.get(request_id, "0")) + cost)
usage = {}
states = {}
for request_id in ledger["requests"]:
    if ledger["requests"][request_id]["client_scope_id"] != SCOPE["client_scope_id"]:
        continue
    usage[request_id] = read_question_usage(store, request_id=request_id, scope=SCOPE)
    states[request_id] = status(request_id, seconds=300)["state"]
REPORT.update(
    events=EVENTS,
    permits=PERMITS,
    generates=len(GENERATES),
    ledger={"requests": sorted(ledger["requests"]), "reserved_microusd": ledger["reserved_microusd"]},
    settled_microusd=settled,
    pricing={key: pricing[key] for key in ("input_usd_per_million", "output_usd_per_million")},
    provider_tokens=PROVIDER_TOKENS,
    usage=usage,
    states=states,
    request_microusd=REQUEST_MICROUSD,
)
sys.stdout.write(json.dumps(REPORT, sort_keys=True))
sys.stdout.flush()
'''


@pytest.fixture(scope="module")
def engine():
    root = os.environ.get("GENERAL_QUESTION_ENGINE_TEST_ROOT")
    interpreter = os.environ.get("GENERAL_QUESTION_ENGINE_TEST_PYTHON")
    if not root or not interpreter:
        pytest.skip(
            "GENERAL_QUESTION_ENGINE_TEST_ROOT and _PYTHON select the engine checkout"
        )
    assert Path(
        root, "src/analysis/open_intelligence/general_question_execution.py"
    ).is_file()
    result = subprocess.run(
        [interpreter, "-c", ENGINE_HARNESS],
        cwd=root,
        capture_output=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")[-6000:]
    return json.loads(result.stdout.decode("utf-8"))


def rid(index):
    return str(UUID(int=index))


def expected_settled_microusd(pricing):
    """Scenario constant: tokens per metered call times the policy price, per settled request."""
    per_call = Decimal(PROVIDER_TOKENS["prompt"]) * Decimal(
        pricing["input_usd_per_million"]
    ) + Decimal(METERED_COMPLETION_TOKENS) * Decimal(pricing["output_usd_per_million"])
    per_request = per_call * CALLS_PER_SETTLED_REQUEST
    return {rid(index): per_request for index in SETTLED_REQUESTS}


def settled_cost_reconciles(settled, pricing):
    expected = expected_settled_microusd(pricing)
    observed = {request_id: Decimal(value) for request_id, value in settled.items()}
    return observed == expected and sum(observed.values()) == sum(expected.values())


def events_for(report, request_id):
    return [
        (e["stage"], e["event"], e["reason_code"])
        for e in report["events"]
        if e["request_id"] == request_id
    ]


def test_twelve_simultaneous_users_each_hold_exactly_one_reservation(engine):
    case = engine["cases"]["twelve_users"]
    assert set(case["first_round"]) <= {"admitted", "control_conflict"}
    assert all(value == "admitted" for value in case["retried"].values())
    assert set(rid(index) for index in range(2, 14)) <= set(case["admitted"])
    assert case["reserved_microusd"] == 100000 * len(case["admitted"])
    assert case["deadlines"] == ["2026-09-06T20:04:00.000000Z"]


def test_admission_that_exhausts_its_retries_records_a_closed_stage_reason(engine):
    """The route records an admission failure outside the stage vocabulary as worker_failed."""
    case = engine["cases"]["admission_conflict_exhausted"]
    assert case["outcome"] == "control_conflict"
    assert events_for(engine, rid(40)) == [
        ("admission", "entered", None),
        ("admission", "failed", "worker_failed"),
    ]
    assert rid(40) not in engine["ledger"]["requests"]


def test_four_duplicate_submissions_join_one_durable_request(engine):
    case = engine["cases"]["four_duplicates"]
    assert case["identical"] is True
    assert case["count"] == 4
    assert case["ledger_entries"] == 1
    assert case["conflict"] == "run_id_conflict"


def test_parent_from_another_scope_is_invisible_and_uncancellable(engine):
    case = engine["cases"]["parent_from_another_scope"]
    assert case["read_refusal"] == "scope_invalid"
    assert case["cancel_refusal"] == "scope_invalid"
    assert case["visible_in_own_scope"] is True


def test_queue_wait_consuming_most_of_the_deadline_keeps_the_original_deadline(engine):
    case = engine["cases"]["queue_wait_most_of_deadline"]
    assert case["ack_state"] == "unavailable"
    assert case["reason"] == "evidence_insufficient"
    assert case["deadline_at"] == "2026-09-06T20:04:00.000000Z"
    assert case["stage_deadlines"]
    for admitted_deadline, now, stage_seconds, value in case["stage_deadlines"]:
        assert admitted_deadline == "2026-09-06T20:04:00.000000Z"
        assert stage_seconds == 60
        assert now >= "2026-09-06T20:03:50.000000Z"
        assert value == admitted_deadline
    assert case["permits"] == 1
    assert events_for(engine, rid(2)) == [
        ("admission", "entered", None),
        ("admission", "completed", None),
        ("planning", "entered", None),
        ("planning", "completed", None),
        ("retrieval", "entered", None),
        ("retrieval", "failed", "evidence_insufficient"),
        ("publication", "entered", None),
        ("publication", "completed", None),
    ]


def test_expired_before_dispatch_publishes_without_any_provider_permit(engine):
    case = engine["cases"]["expired_before_dispatch"]
    assert case["ack_state"] == "unavailable"
    assert case["reason"] == "request_expired"
    assert case["permits"] == 0
    assert case["usage"]["model_calls"] == 0
    assert case["usage"]["reserved_cost_usd"] == "0.100000"
    assert events_for(engine, rid(3))[2:] == [
        ("publication", "entered", None),
        ("publication", "completed", None),
    ]


def test_cancellation_before_dispatch_starts_no_work(engine):
    case = engine["cases"]["cancel_before_dispatch"]
    assert case["cancel_state"] == "unavailable"
    assert case["ack_state"] == "unavailable"
    assert case["same_record"] is True
    assert case["reason"] == "request_cancelled"
    assert case["permits"] == 0


def test_cancellation_after_remote_call_start_holds_then_accounts_the_late_response(
    engine,
):
    case = engine["cases"]["cancel_after_remote_call_start"]
    assert case["cancel_state"] == "held"
    assert case["held_usage_status"] == "unresolved"
    assert case["held_usage_reason"] == "response_usage_unavailable"
    assert case["worker_ack_state"] == "unavailable"
    assert case["final_state"] == "unavailable"
    assert case["reason"] == "request_cancelled"
    assert case["final_usage"]["status"] == "resolved"
    assert case["final_usage"]["model_calls"] == 1
    assert case["permits"] == 1
    assert events_for(engine, rid(5))[2:] == [
        ("planning", "entered", None),
        ("planning", "completed", None),
        ("retrieval", "entered", None),
        ("retrieval", "failed", "request_terminal"),
        ("publication", "entered", None),
        ("publication", "completed", None),
    ]


def test_timeout_after_provider_acknowledgement_holds_the_unknown_reservation(engine):
    case = engine["cases"]["timeout_after_provider_acknowledgement"]
    assert case["first_state"] == "held"
    assert case["first_reason"] == "model_timeout"
    assert case["second_state"] == "held"
    assert case["same_record"] is True
    assert case["usage"]["status"] == "unresolved"
    assert case["usage"]["model_calls"] is None
    assert case["permits"] == 1
    assert events_for(engine, rid(6))[2:] == [
        ("planning", "entered", None),
        ("planning", "held", "model_timeout"),
        ("publication", "entered", None),
        ("publication", "held", "model_timeout"),
        ("publication", "entered", None),
        ("publication", "held", "response_usage_unavailable"),
    ]


def test_usage_write_failure_is_held_with_the_reservation_kept(engine):
    case = engine["cases"]["usage_write_failure"]
    assert case["ack_state"] == "held"
    assert case["reason"] == "metering_persistence_failed"
    assert case["usage_status"] == "failed"
    assert case["metering_failed"] is True
    assert case["reserved_microusd"] == 100000
    assert case["usage"]["status"] == "unresolved"
    assert case["permits"] == 1


def test_ambiguous_object_publication_lands_exactly_once(engine):
    case = engine["cases"]["ambiguous_object_publication"]
    assert case["ack_state"] == "unavailable"
    assert case["result_uploads"] == 1
    assert case["status_state"] == "unavailable"
    assert case["same_digest"] is True
    assert case["permits"] == 1


def test_worker_restart_holds_then_the_late_response_is_accounted_once(engine):
    case = engine["cases"]["worker_restart_and_late_response"]
    assert case["restart_state"] == "held"
    assert case["restart_reason"] == "response_usage_unavailable"
    assert case["held_usage_status"] == "unresolved"
    assert case["late_worker_ack_state"] == "unavailable"
    assert case["final_state"] == "unavailable"
    assert case["final_usage"]["status"] == "resolved"
    assert case["final_usage"]["model_calls"] == 1
    assert case["permits"] == 1
    observed = events_for(engine, rid(9))[2:]
    assert observed.count(("planning", "held", "response_usage_unavailable")) == 1
    assert observed.count(("retrieval", "failed", "request_terminal")) == 1
    assert observed.count(("planning", "entered", None)) == 2


def test_recovery_boundaries_reread_durable_state_before_generation(engine):
    case = engine["cases"]["recovery_boundaries"]
    pointer = case["pointer"]
    assert pointer["died"] is True
    assert pointer["pointer_before_recovery"] is False
    assert pointer["bytes_before_recovery"] is True
    assert pointer["state"] == "unavailable"
    assert pointer["usage"]["status"] == "resolved"
    assert pointer["usage"]["model_calls"] == 1
    assert pointer["permits"] == 1
    lost = case["provider"]
    assert lost["died"] is True
    assert lost["pointer_before_recovery"] is False
    assert lost["bytes_before_recovery"] is False
    assert lost["state"] == "held"
    assert lost["reason"] == "response_usage_unavailable"
    assert lost["usage"]["status"] == "unresolved"
    assert lost["permits"] == 1
    assert lost["reserved_microusd"] == 100000
    projection = case["projection"]
    assert projection["died"] is True
    assert projection["pointer_before_recovery"] is True
    assert projection["bytes_before_recovery"] is True
    assert projection["state"] == "unavailable"
    assert projection["usage"]["status"] == "resolved"
    assert projection["permits"] == 1


def test_provider_permits_are_unique_and_never_exceed_two_per_request(engine):
    permits = engine["permits"]
    nonces = [nonce for _, _, nonce in permits]
    assert len(set(nonces)) == len(nonces)
    per_request = Counter((request_id, stage) for request_id, stage, _ in permits)
    assert all(count == 1 for count in per_request.values())
    assert all(
        count <= 2
        for count in Counter(request_id for request_id, _, _ in permits).values()
    )
    # Every provider call holds a permit; the one permit without a call timed out before submission.
    assert engine["generates"] == len(permits) - 1


def test_final_ledger_reconciles_to_the_complete_event_set(engine):
    events = engine["events"]
    assert all(set(event) == STAGE_EVENT_FIELDS for event in events)
    assert all(event["event_version"] == "question_stage_event_v1" for event in events)
    admitted = {
        e["request_id"]
        for e in events
        if e["stage"] == "admission" and e["event"] == "completed"
    }
    assert sorted(admitted) == engine["ledger"]["requests"]
    assert engine["ledger"]["reserved_microusd"] == 100000 * len(admitted)
    for stage_event in events:
        if stage_event["event"] in ("failed", "held"):
            assert stage_event["reason_code"] is not None
        else:
            assert stage_event["reason_code"] is None
    held = {
        request_id
        for request_id, usage in engine["usage"].items()
        if usage["status"] == "unresolved"
    }
    assert held == {
        request_id for request_id, state in engine["states"].items() if state == "held"
    }
    assert held == {rid(6), rid(7), rid(11)}
    assert settled_cost_reconciles(engine["settled_microusd"], engine["pricing"])
    settled_requests = {
        request_id
        for request_id, usage in engine["usage"].items()
        if usage["status"] == "resolved" and usage["model_calls"]
    }
    assert set(engine["settled_microusd"]) == settled_requests
    assert settled_requests.isdisjoint(held)
    last_publication = {}
    for stage_event in events:
        if stage_event["stage"] == "publication" and stage_event["event"] != "entered":
            last_publication[stage_event["request_id"]] = stage_event["event"]
    assert {
        request_id for request_id, event in last_publication.items() if event == "held"
    } == held


def test_exact_settled_cost_matches_the_scenario_constant(engine):
    assert engine["provider_tokens"] == PROVIDER_TOKENS
    expected = expected_settled_microusd(engine["pricing"])
    assert set(engine["settled_microusd"]) == {rid(index) for index in SETTLED_REQUESTS}
    per_request = next(iter(expected.values()))
    assert per_request > 0
    for request_id, value in engine["settled_microusd"].items():
        assert Decimal(value) == per_request
        assert engine["usage"][request_id]["model_calls"] == CALLS_PER_SETTLED_REQUEST
        assert engine["usage"][request_id]["input_tokens"] == PROVIDER_TOKENS["prompt"]
        assert engine["usage"][request_id]["output_tokens"] == METERED_COMPLETION_TOKENS
    assert sum(
        Decimal(v) for v in engine["settled_microusd"].values()
    ) == per_request * len(SETTLED_REQUESTS)
    assert settled_cost_reconciles(engine["settled_microusd"], engine["pricing"])
    # Mutation checks: the reconciliation must notice one micro USD, a lost row and an extra row.
    inflated = dict(engine["settled_microusd"])
    first = rid(SETTLED_REQUESTS[0])
    inflated[first] = str(Decimal(inflated[first]) + Decimal(1))
    assert not settled_cost_reconciles(inflated, engine["pricing"])
    dropped = dict(engine["settled_microusd"])
    dropped.pop(first)
    assert not settled_cost_reconciles(dropped, engine["pricing"])
    extra = dict(engine["settled_microusd"])
    extra[rid(6)] = engine["settled_microusd"][first]
    assert not settled_cost_reconciles(extra, engine["pricing"])


def service_with(queue, worker=None):
    worker = worker or DurableAdmissionWorker()
    service = routes_module().GeneralQuestionRoutes(
        worker,
        queue,
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    return service, worker


def run_with_dispatch_slots(submissions, *, slots):
    """Run the submissions with as many dispatch threads as they hold at once.

    Queue dispatch runs on the loop's default executor, and that executor is
    sized from the host's processor count. A case that holds every submission
    at a barrier needs one thread per submission, so it provides them here
    instead of depending on how many processors the machine happens to have.
    """

    async def main():
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=slots)
        )
        return await submissions()

    return asyncio.run(main())


def barrier_queue(parties):
    barrier = threading.Barrier(parties)
    dispatched = []
    lock = threading.Lock()

    def enqueue(invocation, **kwargs):
        barrier.wait(timeout=20)
        with lock:
            dispatched.append(invocation["request_id"])
        return {"state": "verified"}

    return SimpleNamespace(enqueue=enqueue), dispatched


def test_routes_four_duplicate_submissions_meet_at_the_dispatch_barrier_as_one_request():
    queue, dispatched = barrier_queue(4)
    service, worker = service_with(queue)
    body = {
        "message": "What is changing for Gen Z in South Africa?",
        "history": [],
        "market": "za",
        "idempotency_key": "browser-turn-0000000a",
    }

    async def submit_all():
        return await asyncio.gather(*(service.start(dict(body)) for _ in range(4)))

    results = run_with_dispatch_slots(submit_all, slots=4)
    assert len({result["job_id"] for result in results}) == 1
    assert len(worker.durable) == 1
    request_id = next(iter(worker.durable))
    assert dispatched == [request_id] * 4
    assert results[0]["job_id"] == "chat_" + request_id.replace("-", "")


def test_routes_twelve_simultaneous_users_get_twelve_distinct_durable_requests():
    queue, dispatched = barrier_queue(12)
    service, worker = service_with(queue)

    async def submit_all():
        return await asyncio.gather(
            *(
                service.start(
                    {
                        "message": f"Question from user {index}",
                        "history": [],
                        "market": "za",
                        "idempotency_key": f"user-{index:02d}-turn-0001",
                    }
                )
                for index in range(12)
            )
        )

    results = run_with_dispatch_slots(submit_all, slots=12)
    assert len({result["job_id"] for result in results}) == 12
    assert len(worker.durable) == 12
    assert sorted(dispatched) == sorted(worker.durable)


def test_routes_retry_after_browser_reload_recovers_the_same_request():
    service, worker = service_with(
        SimpleNamespace(enqueue=lambda *a, **k: {"state": "verified"})
    )
    body = {
        "message": "Question before reload",
        "history": [],
        "market": "za",
        "idempotency_key": "browser-turn-0000000b",
    }
    before = asyncio.run(service.start(body))
    request_id = str(UUID(hex=before["job_id"][5:]))
    worker.records[request_id] = {
        "state": "unavailable",
        "response": {
            "answer": "No admissible evidence matched this request.",
            "sources": [],
            "error": True,
            "reason": "evidence_insufficient",
            "intelligence": {"status": "unavailable"},
        },
    }
    after = asyncio.run(service.start(dict(body)))
    assert after == before
    surfaced = asyncio.run(service.status(after["job_id"]))
    assert surfaced["lifecycle"]["request_id"] == request_id
    assert surfaced["lifecycle"]["state"] == "insufficient_evidence"
    assert len(worker.durable) == 1


def test_routes_parent_from_another_scope_is_refused_before_dispatch():
    class ScopedWorker(DurableAdmissionWorker):
        async def run(self, operation, payload, **kwargs):
            if operation == "admit" and payload.get("parent_request_id") is not None:
                raise WorkerProcessError("parent_scope_mismatch")
            return await super().run(operation, payload, **kwargs)

    dispatched = []
    queue = SimpleNamespace(
        enqueue=lambda invocation, **kwargs: (
            dispatched.append(invocation) or {"state": "verified"}
        )
    )
    service, worker = service_with(queue, ScopedWorker())
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(general_question_routes=service))
    )
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            routes_module().start_route(
                request,
                {
                    "message": "Follow up on a parent from another client",
                    "history": [],
                    "market": "za",
                    "parent_request_id": "ad30efed-dd99-49da-8113-3943e289fba0",
                    "idempotency_key": "browser-turn-0000000c",
                },
            )
        )
    assert raised.value.status_code == 409
    assert raised.value.detail == "parent_context_unavailable"
    assert dispatched == []
    assert worker.durable == {}


def test_routes_expired_before_dispatch_logs_the_queue_failure_and_keeps_the_job(
    caplog,
):
    caplog.set_level("INFO", logger="listening_post.question_worker")

    class ExpiredWorker(DurableAdmissionWorker):
        async def run(self, operation, payload, **kwargs):
            outcome = await super().run(operation, payload, **kwargs)
            if operation == "admit":
                outcome.engine_reply.reply["deadline_at"] = "2020-01-01T00:00:00Z"
            return outcome

    def enqueue(invocation, *, deadline):
        if deadline - time.monotonic() <= 0:
            raise ValueError("question_queue_deadline")
        pytest.fail("dispatched an expired request")

    service, _worker = service_with(SimpleNamespace(enqueue=enqueue), ExpiredWorker())
    result = asyncio.run(
        service.start(
            {
                "message": "Question that expired in the queue",
                "history": [],
                "market": "za",
                "idempotency_key": "browser-turn-0000000d",
            }
        )
    )
    assert result["job_id"].startswith("chat_")
    events = [
        json.loads(row.getMessage())
        for row in caplog.records
        if row.getMessage().startswith('{"deployment_digest":')
    ]
    assert [(e["stage"], e["event"], e["reason_code"]) for e in events] == [
        ("admission", "entered", None),
        ("admission", "completed", None),
        ("queue", "entered", None),
        ("queue", "failed", "queue_dispatch_unresolved"),
    ]
