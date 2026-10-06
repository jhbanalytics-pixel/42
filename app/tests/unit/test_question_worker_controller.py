import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from datetime import UTC, datetime, timedelta

import pytest

from src.api.question_worker_controller import QuestionWorker
from src.api.question_worker_process import WorkerProcessError
from src.api.question_worker_result import ResultVerificationError
from tests.unit.test_question_worker_bundle import ENTRYPOINT, LP_COMMIT, make_bundle
from tests.unit.test_question_worker_protocol import admission
from tests.unit.test_question_worker_result import EXPECTED, HASHES
from tests.unit.test_question_worker_store import VersionedStore


posix_only = pytest.mark.skipif(
    os.name != "posix", reason="Actual engine process requires target Linux"
)


def create_worker(tmp_path, *, state="unavailable", operation="status", expired=False):
    if operation == "execute":
        reply = {
            key: EXPECTED[key]
            for key in ["request_id", "request_digest", "intake_digest"]
        } | {
            "result_digest": HASHES["unavailable"],
            "result_generation": "17",
            "state": "terminal",
        }
    elif operation == "status":
        reply = EXPECTED | {
            "contract_version": "general_question_status_v1",
            "state": state,
            "deadline_at": (
                datetime.now(UTC) + timedelta(seconds=-60 if expired else 60)
            ).isoformat(),
            "result_digest": HASHES["unavailable"] if state != "admitted" else None,
            "result_generation": "17" if state != "admitted" else None,
        }
    else:
        reply = {
            "contract_version": "general_question_admission_result_v1",
            "job_id": "chat_" + EXPECTED["request_id"].replace("-", ""),
            "invocation": EXPECTED,
            "request_generation": "11",
            "intake_generation": "12",
            "deadline_at": "2026-09-06T20:03:00Z",
        }
    captured = tmp_path / "received.json"
    source = (
        "import json,sys\nvalue=json.load(sys.stdin)\nassert sys.argv[1:] == ['--operation',"
        + repr(operation)
        + "]\nopen("
        + repr(str(captured))
        + ",'w',encoding='utf-8').write(json.dumps(value,ensure_ascii=False))\nprint(json.dumps("
        + repr(reply)
        + "))\n"
    )
    root, stamp, digest = make_bundle(tmp_path, {ENTRYPOINT: source.encode("utf-8")})
    store = VersionedStore()
    capacity = threading.Lock()
    worker = QuestionWorker(
        bundle_root=root,
        interpreter=Path(sys.executable),
        stamp_path=stamp,
        lp_commit=LP_COMMIT,
        bundle_digest=digest,
        storage_client=store,
        try_acquire=lambda: capacity.acquire(blocking=False),
        release=capacity.release,
    )
    return worker, store, capacity, captured


def status_payload():
    return {
        "contract_version": "general_question_status_request_v1",
        "request_id": EXPECTED["request_id"],
        "scope": admission()["scope"],
    }


def call(worker, operation, payload, deadline=None):
    return asyncio.run(
        worker.run(
            operation,
            payload,
            deadline=time.monotonic() + 10 if deadline is None else deadline,
        )
    )


@pytest.mark.parametrize("deadline", [-1, float("nan"), True])
def test_invalid_deadline_never_launches_or_acquires_capacity(tmp_path, deadline):
    worker, store, capacity, captured = create_worker(tmp_path)
    with pytest.raises(WorkerProcessError):
        call(worker, "execute", EXPECTED, deadline)
    assert not captured.exists() and not capacity.locked() and store.reads == []


@posix_only
def test_execute_joins_bundle_process_and_exact_result_store(tmp_path):
    worker, store, capacity, captured = create_worker(tmp_path, operation="execute")
    outcome = call(worker, "execute", EXPECTED)
    assert outcome.result_record["response"]["reason"] == "request_expired"
    assert outcome.engine_reply.reply["result_generation"] == "17"
    assert store.reads == ["metadata", "body"]
    assert json.loads(captured.read_text()) == EXPECTED
    assert not capacity.locked()


@posix_only
@pytest.mark.parametrize("state", ["admitted", "unavailable"])
def test_status_reads_current_scope_without_taking_generation_capacity(tmp_path, state):
    worker, store, capacity, captured = create_worker(tmp_path, state=state)
    capacity.acquire()
    try:
        outcome = call(worker, "status", status_payload())
        assert outcome.engine_reply.reply["state"] == state
        assert (outcome.result_record is None) == (state == "admitted")
        assert store.reads == ([] if state == "admitted" else ["metadata", "body"])
        assert json.loads(captured.read_text())["scope"] == status_payload()["scope"]
        assert capacity.locked()
    finally:
        capacity.release()


@posix_only
def test_admission_preserves_original_transport_without_generation_or_result_read(
    tmp_path,
):
    worker, store, capacity, captured = create_worker(tmp_path, operation="admit")
    payload = admission() | {
        key: EXPECTED[key]
        for key in ["request_id", "policy_digest", "deployment_digest"]
    }
    capacity.acquire()
    try:
        outcome = call(worker, "admit", payload)
        assert (
            outcome.engine_reply.reply["job_id"]
            == "chat_00000000000000000000000000000001"
        )
        assert outcome.result_record is None and store.reads == []
        assert json.loads(captured.read_text(encoding="utf-8")) == payload
        assert len(payload["transport"]["history"]) == 40
        assert capacity.locked()
    finally:
        capacity.release()


@posix_only
def test_status_concrete_state_must_match_the_verified_record(tmp_path):
    worker, _, _, _ = create_worker(tmp_path, state="refused")
    with pytest.raises(ResultVerificationError, match="result_status_mismatch"):
        call(worker, "status", status_payload())


@posix_only
def test_expired_admitted_status_cannot_become_perpetual_pending(tmp_path):
    worker, store, _, _ = create_worker(tmp_path, state="admitted", expired=True)
    with pytest.raises(WorkerProcessError, match="worker_status_expired"):
        call(worker, "status", status_payload())
    assert store.reads == []


@posix_only
@pytest.mark.parametrize(
    "scope_change",
    [
        {"client_scope_id": "different"},
        {"market_scope": ["za"]},
        {"brand_config_id": "different"},
        {"theme_id": "different"},
    ],
)
def test_status_cannot_project_a_result_outside_current_authenticated_scope(
    tmp_path, scope_change
):
    worker, _, _, _ = create_worker(tmp_path)
    payload = status_payload()
    payload["scope"].update(scope_change)
    with pytest.raises(ResultVerificationError, match="result_scope_mismatch"):
        call(worker, "status", payload)


def test_waiting_for_capacity_uses_the_original_deadline(tmp_path):
    worker, store, capacity, captured = create_worker(tmp_path, operation="execute")
    capacity.acquire()
    try:
        with pytest.raises(WorkerProcessError, match="worker_deadline_reached"):
            call(worker, "execute", EXPECTED, time.monotonic() + 0.03)
        assert capacity.locked() and not captured.exists() and store.reads == []
    finally:
        capacity.release()


def test_cancellation_while_waiting_does_not_release_someone_elses_slot(tmp_path):
    worker, _, capacity, captured = create_worker(tmp_path, operation="execute")
    capacity.acquire()

    async def run():
        task = asyncio.create_task(
            worker.run("execute", EXPECTED, deadline=time.monotonic() + 10)
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(run())
        assert capacity.locked() and not captured.exists()
    finally:
        capacity.release()


@posix_only
def test_result_read_failure_releases_the_slot_without_acknowledgment(tmp_path):
    worker, store, capacity, _ = create_worker(tmp_path, operation="execute")
    store.generation = 18
    with pytest.raises(ResultVerificationError, match="result_generation_mismatch"):
        call(worker, "execute", EXPECTED)
    assert not capacity.locked()


@posix_only
def test_changed_bundle_never_reaches_the_child(tmp_path):
    worker, store, capacity, captured = create_worker(tmp_path, operation="execute")
    (worker.bundle_root / ENTRYPOINT).write_text(
        "raise RuntimeError('changed')", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="bundle_file_mismatch"):
        call(worker, "execute", EXPECTED)
    assert not captured.exists() and not capacity.locked() and store.reads == []
