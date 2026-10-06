"""Advisory browser delivery reports for rendered answers.

A browser reports that it rendered one answer with the request id, the rendered
response digest the status read served, the served asset version, the event
type and its own event time. The server checks the request is known in its
scope and that the digest is the one derived from the stored response bytes,
then records the report with its own receipt time. The report is telemetry
only: it never changes an answer, a lifecycle state or an approval, and a
request with no report reads as delivery unknown, never as delivered.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from src.api import dossier_approval, main, operator_timeline
from src.api.general_question_routes import GeneralQuestionRoutes
from src.api.question_worker_process import WorkerProcessError
from tests.unit.test_question_worker_result import raw_fixture

LOGGER = "listening_post.question_worker"
ROUTE = "/api/internal/v2/question-delivery"
DEADLINE_AT = "2099-01-01T00:00:00Z"
RAW = raw_fixture()
RECORD = json.loads(RAW)
REQUEST_ID = RECORD["request_id"]
JOB_ID = "chat_" + UUID(REQUEST_ID).hex
PASSCODE = "delivery-test-passcode"
HEADERS = {"X-Passcode": PASSCODE}


class Worker:
    def __init__(self, *, record=RECORD, error=None):
        self.record, self.error, self.calls = copy.deepcopy(record), error, []

    async def run(self, operation, payload, **kwargs):
        self.calls.append(operation)
        if self.error is not None:
            raise self.error
        assert operation == "status"
        return SimpleNamespace(
            engine_reply=SimpleNamespace(
                reply={
                    "state": "admitted" if self.record is None else self.record["state"],
                    "deadline_at": DEADLINE_AT,
                }
            ),
            result_record=self.record,
        )


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", PASSCODE)
    monkeypatch.delenv("LP_ALLOW_OPEN_GATE", raising=False)
    operator_timeline.DELIVERY_RATE_LIMIT.reset()
    operator_timeline.DELIVERY_LEDGER.reset()
    yield
    operator_timeline.DELIVERY_RATE_LIMIT.reset()
    operator_timeline.DELIVERY_LEDGER.reset()


def configure(monkeypatch, worker):
    service = GeneralQuestionRoutes(
        worker,
        SimpleNamespace(enqueue=lambda *args, **kwargs: {"state": "verified"}),
        policy_digest="a" * 64,
        deployment_digest="b" * 64,
    )
    monkeypatch.setattr(main.app.state, "general_question_routes", service, raising=False)
    return service


def stored_digest():
    return operator_timeline.rendered_response_digest(RECORD["response"])


def report(**changes):
    value = {
        "request_id": REQUEST_ID,
        "response_digest": stored_digest(),
        "asset_version": "index-AbC123.js",
        "event_type": "rendered",
        "client_event_at": "2026-09-25T10:00:00.123Z",
    }
    value.update(changes)
    return value


def client():
    return TestClient(main.app)


def post(body, **kwargs):
    data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    return client().post(
        ROUTE,
        content=data,
        headers={**HEADERS, "Content-Type": "application/json", **kwargs},
    )


def delivery(request_id=REQUEST_ID):
    return client().get(f"{ROUTE}/{request_id}", headers=HEADERS)


# The rendered response digest


def test_the_rendered_response_digest_is_taken_over_the_stored_response_bytes():
    canonical = json.dumps(
        RECORD["response"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert canonical in RAW
    assert stored_digest() == hashlib.sha256(canonical).hexdigest()


def test_a_terminal_status_read_names_the_digest_in_a_header_only(monkeypatch):
    configure(monkeypatch, Worker())
    served = client().get("/api/chat/status", params={"job_id": JOB_ID}, headers=HEADERS)
    assert served.status_code == 200
    assert served.headers["X-Question-Response-Digest"] == stored_digest()
    assert "response_digest" not in served.json()
    assert {key: value for key, value in served.json().items() if key != "lifecycle"} == (
        RECORD["response"]
    )
    configure(monkeypatch, Worker(record=None))
    pending = client().get("/api/chat/status", params={"job_id": JOB_ID}, headers=HEADERS)
    assert pending.status_code == 200
    assert pending.json()["pending"] is True
    assert "X-Question-Response-Digest" not in pending.headers


# Authentication, grammar and bounds


def test_the_report_requires_the_passcode_before_anything_is_read(monkeypatch):
    worker = Worker()
    configure(monkeypatch, worker)
    refused = client().post(ROUTE, json=report())
    assert refused.status_code == 401
    assert client().get(f"{ROUTE}/{REQUEST_ID}").status_code == 401
    assert worker.calls == []


@pytest.mark.parametrize(
    "body",
    [
        report() | {"answer": "PRIVATE answer text"},
        {key: value for key, value in report().items() if key != "asset_version"},
        report(request_id="chat_" + "0" * 32),
        report(request_id=REQUEST_ID.replace("-", "")),
        report(response_digest="A" * 64),
        report(asset_version="index js with spaces"),
        report(asset_version="x" * 65),
        report(event_type="healthy"),
        report(client_event_at="2026-09-25 10:00:00"),
        report(client_event_at="2026-13-45T10:00:00Z"),
        report(client_event_at=1758794400),
        [report()],
    ],
)
def test_only_the_five_named_fields_in_their_grammar_are_accepted(monkeypatch, body):
    worker = Worker()
    configure(monkeypatch, worker)
    refused = post(body)
    assert refused.status_code == 400
    assert refused.json()["detail"]["code"] == "delivery_report_invalid"
    assert worker.calls == []


def test_a_duplicated_field_and_a_body_that_is_not_json_are_refused(monkeypatch):
    worker = Worker()
    configure(monkeypatch, worker)
    text = json.dumps(report())
    duplicated = text[:-1] + ', "event_type": "render_failed"}'
    assert post(duplicated.encode("utf-8")).status_code == 400
    assert post(b"not json").status_code == 400
    assert worker.calls == []


def test_an_oversized_body_is_refused_before_it_is_parsed(monkeypatch):
    worker = Worker()
    configure(monkeypatch, worker)
    refused = post(b"{" + b" " * 2048 + b"}")
    assert refused.status_code == 413
    assert refused.json()["detail"]["code"] == "delivery_report_too_large"
    assert worker.calls == []


def test_reports_are_rate_limited_per_process_before_any_read(monkeypatch):
    worker = Worker()
    configure(monkeypatch, worker)
    monkeypatch.setattr(
        operator_timeline,
        "DELIVERY_RATE_LIMIT",
        operator_timeline.DeliveryRateLimit(maximum=3, window=60.0),
    )
    for _ in range(3):
        assert post(report()).status_code == 202
    reads = list(worker.calls)
    limited = post(report())
    assert limited.status_code == 429
    assert limited.json()["detail"]["code"] == "delivery_rate_limited"
    assert worker.calls == reads


def test_the_rate_window_slides_and_tracked_clients_are_bounded():
    limit = operator_timeline.DeliveryRateLimit(maximum=2, window=10.0, clients=2)
    limit.check("a", now=0.0)
    limit.check("a", now=1.0)
    with pytest.raises(operator_timeline.DeliveryRefused):
        limit.check("a", now=2.0)
    limit.check("a", now=10.5)
    limit.check("b", now=10.5)
    with pytest.raises(operator_timeline.DeliveryRefused):
        limit.check("c", now=10.5)


# Scope, identity and digest


def test_an_unknown_request_is_refused(monkeypatch):
    configure(monkeypatch, Worker(error=WorkerProcessError("request_unknown")))
    refused = post(report())
    assert refused.status_code == 404
    assert refused.json()["detail"]["code"] == "request_unknown"
    assert operator_timeline.DELIVERY_LEDGER.status(REQUEST_ID)["delivery"] == "unknown"


def test_a_request_outside_the_scope_is_refused(monkeypatch):
    configure(monkeypatch, Worker(error=WorkerProcessError("scope_invalid")))
    refused = post(report())
    assert refused.status_code == 404
    assert refused.json()["detail"]["code"] == "request_unknown"


@pytest.mark.parametrize(
    "code", ["request_unknown", "scope_invalid", "observation_request_unavailable"]
)
def test_the_reader_refuses_an_unknown_or_out_of_scope_request(monkeypatch, code):
    worker = Worker()
    configure(monkeypatch, worker)
    assert post(report()).status_code == 202
    worker.error = WorkerProcessError(code)
    refused = delivery()
    assert refused.status_code == 404
    assert refused.json()["detail"]["code"] == "request_unknown"
    assert "report" not in refused.text


def test_the_reader_reads_the_request_through_the_scope_bound_status(monkeypatch):
    worker = Worker()
    configure(monkeypatch, worker)
    assert delivery().status_code == 200
    assert worker.calls == ["status"]
    worker.error = RuntimeError("store down")
    unavailable = delivery()
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "delivery_unavailable"


def test_a_request_without_a_rendered_response_is_refused(monkeypatch):
    configure(monkeypatch, Worker(record=None))
    refused = post(report())
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "response_unavailable"
    assert delivery().json()["delivery"] == "unknown"
    assert delivery().status_code == 200


def test_a_digest_that_is_not_the_stored_one_is_refused(monkeypatch, caplog):
    caplog.set_level("INFO", logger=LOGGER)
    configure(monkeypatch, Worker())
    refused = post(report(response_digest="0" * 64))
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "response_digest_mismatch"
    assert delivery().json()["delivery"] == "unknown"
    assert "question_delivery_report_v1" not in caplog.text


def test_a_failed_store_read_is_unavailable_and_records_nothing(monkeypatch):
    configure(monkeypatch, Worker(error=RuntimeError("store down")))
    refused = post(report())
    assert refused.status_code == 503
    assert refused.json()["detail"]["code"] == "delivery_unavailable"
    assert operator_timeline.DELIVERY_LEDGER.status(REQUEST_ID)["delivery"] == "unknown"


# Recording and reading


def test_an_accepted_report_is_logged_with_exactly_the_named_fields(monkeypatch, caplog):
    caplog.set_level("INFO", logger=LOGGER)
    configure(monkeypatch, Worker())
    accepted = post(report())
    assert accepted.status_code == 202
    assert accepted.json() == {
        "contract_version": "question_delivery_status_v1",
        "request_id": REQUEST_ID,
        "delivery": "rendered",
        "authority": "advisory",
    }
    (record,) = [
        json.loads(row.getMessage())
        for row in caplog.records
        if '"event_version":"question_delivery_report_v1"' in row.getMessage()
    ]
    assert set(record) == {
        "event_version",
        "request_id",
        "response_digest",
        "asset_version",
        "event_type",
        "client_event_at",
        "received_at",
    }
    assert record["client_event_at"] == "2026-09-25T10:00:00.123Z"
    assert record["received_at"].endswith("Z")
    assert RECORD["response"]["answer"][:40] not in caplog.text


def test_the_reader_says_unknown_until_an_acknowledgement_arrives(monkeypatch):
    configure(monkeypatch, Worker())
    before = delivery()
    assert before.status_code == 200
    assert before.json() == {
        "contract_version": "question_delivery_status_v1",
        "request_id": REQUEST_ID,
        "delivery": "unknown",
        "authority": "advisory",
        "report": None,
    }
    assert post(report(event_type="render_failed")).status_code == 202
    after = delivery().json()
    assert after["delivery"] == "render_failed"
    assert after["report"]["response_digest"] == stored_digest()
    assert after["report"]["asset_version"] == "index-AbC123.js"
    assert set(after["report"]) == {
        "response_digest",
        "asset_version",
        "event_type",
        "client_event_at",
        "received_at",
    }


def test_the_reader_refuses_a_malformed_request_id(monkeypatch):
    worker = Worker()
    configure(monkeypatch, worker)
    refused = delivery("not-a-request")
    assert refused.status_code == 400
    assert refused.json()["detail"]["code"] == "delivery_request_invalid"
    assert worker.calls == []


def test_the_ledger_is_bounded():
    ledger = operator_timeline.DeliveryLedger(limit=2)
    ids = [str(UUID(int=index + 1)) for index in range(3)]
    for request_id in ids:
        ledger.record(
            {
                "request_id": request_id,
                "response_digest": "a" * 64,
                "asset_version": "v1",
                "event_type": "rendered",
                "client_event_at": "2026-09-25T10:00:00Z",
            }
        )
    assert ledger.status(ids[0])["delivery"] == "unknown"
    assert ledger.status(ids[2])["delivery"] == "rendered"


# Advisory only


def test_a_report_never_changes_the_answer_its_state_or_an_approval(monkeypatch):
    worker = Worker()
    service = configure(monkeypatch, worker)
    approvals = []
    monkeypatch.setattr(
        dossier_approval, "record_review", lambda **kwargs: approvals.append(kwargs)
    )
    before = asyncio.run(service.status(JOB_ID))
    for event_type in ("render_failed", "rendered"):
        assert post(report(event_type=event_type)).status_code == 202
    after = asyncio.run(service.status(JOB_ID))
    assert after == before
    assert after["lifecycle"]["state"] == RECORD["state"]
    assert set(worker.calls) == {"status"}
    assert worker.record == RECORD
    assert approvals == []
