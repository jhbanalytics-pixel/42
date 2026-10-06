import hashlib
import json
from datetime import UTC, datetime

import pytest

from src.api.question_worker_deadline import deadline_from_request
from src.api.question_worker_process import WorkerProcessError
from src.api.question_worker_protocol import WorkerProtocolError
from tests.unit.test_question_worker_result import EXPECTED, FIXTURES, canonical


def raw_request():
    return (FIXTURES / "general-question-request.json").read_bytes()


def cutoff(data=None, *, invocation=None, at="2026-09-06T20:01:00Z"):
    return deadline_from_request(
        raw_request() if data is None else data,
        EXPECTED if invocation is None else invocation,
        now=datetime.fromisoformat(at),
        monotonic_now=100.0,
    )


def test_producer_request_uses_original_admission_time_not_dispatch_time():
    assert (
        hashlib.sha256(raw_request()).hexdigest()
        == "c47d9325daf581cb59faf617ae9726706ad068161a11a3a296efc7212418c172"
    )
    assert cutoff() == 280.0
    assert cutoff(at="2026-09-06T20:03:30Z") == 130.0


@pytest.mark.parametrize("at", ["2026-09-06T20:04:00Z", "2026-09-07T20:00:00Z"])
def test_expired_request_never_gets_another_execution_window(at):
    with pytest.raises(WorkerProcessError, match="worker_deadline_reached"):
        cutoff(at=at)


def test_request_one_microsecond_before_policy_boundary_keeps_its_remaining_window():
    assert cutoff(at="2026-09-06T20:03:59.999999Z") == pytest.approx(100.000001)


def test_future_admission_is_refused():
    with pytest.raises(WorkerProtocolError, match="worker_request_future"):
        cutoff(at="2026-09-06T19:59:59Z")


@pytest.mark.parametrize("field", ["request_id", "request_digest", "policy_digest"])
def test_task_binding_must_match_original_request(field):
    changed = EXPECTED | {
        field: "00000000-0000-0000-0000-000000000002"
        if field == "request_id"
        else "f" * 64
    }
    with pytest.raises(WorkerProtocolError):
        cutoff(invocation=changed)


@pytest.mark.parametrize(
    "change",
    [
        {"question": "different"},
        {"as_of": "2026-09-06T20:02:00.000000Z"},
        {"request_digest": "f" * 64},
        {"extra": True},
        {"contract_version": "other"},
    ],
)
def test_tampered_request_cannot_move_the_supervisor_cutoff(change):
    with pytest.raises(WorkerProtocolError):
        cutoff(canonical(json.loads(raw_request()) | change))


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-09-06 20:00:00Z",
        "2026-09-06T20:00:00+02:00",
        "2026-02-30T20:00:00Z",
        "2026-09-06T20:00:00.0000001Z",
    ],
)
def test_even_rehashed_request_requires_producer_timestamp_shape(stamp):
    value = json.loads(raw_request())
    value["as_of"] = stamp
    value.pop("request_digest")
    digest = hashlib.sha256(canonical(value)).hexdigest()
    value["request_digest"] = digest
    with pytest.raises(WorkerProtocolError):
        cutoff(canonical(value), invocation=EXPECTED | {"request_digest": digest})


def test_noncanonical_request_bytes_are_refused():
    with pytest.raises(WorkerProtocolError):
        cutoff(raw_request() + b"\n")


def test_clock_pair_must_be_valid():
    with pytest.raises(WorkerProcessError):
        deadline_from_request(
            raw_request(),
            EXPECTED,
            now=datetime(2026, 9, 6, tzinfo=UTC),
            monotonic_now=float("nan"),
        )
