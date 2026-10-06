"""The operator timeline past publication: artifact preparation, review and export events.

Each event is a closed question_artifact_event_v1 record linked to the
investigation or the question request it belongs to, and none carries artifact
bytes, answer text, question text or source bodies.
"""

from __future__ import annotations

import json

import pytest
from src.api import operator_timeline, pdf_exporter
from starlette.requests import Request
from src.api.question_worker_process import WorkerProcessError
from tests.unit.test_ask_answer_export import RID, observe
from tests.unit.test_main_artifact_routes import (
    prepare_body,
    prepare_body_v2,
    prepare_url,
    prepared_v2,
    preview_body,
    preview_url,
    read_url,
)
from tests.unit.test_main_dossier_review_routes import (
    ORIGIN,
    SUBJECTS,
    claim_command,
    client,
    reviewer,
)
from tests.unit.test_main_dossier_review_routes import (
    review_environment as review_environment_fixture,
)
from tests.unit.test_main_dossier_review_routes import workspace as workspace_fixture

_SHARED_FIXTURES = (review_environment_fixture, workspace_fixture)
LOGGER = "listening_post.question_worker"
FIELDS = {
    "event_version",
    "request_id",
    "investigation_id",
    "artifact_id",
    "artifact_version",
    "dossier_version",
    "stage",
    "event",
    "action",
    "format",
    "state",
    "occurred_at",
    "reason_code",
}


@pytest.fixture
def review_environment(review_environment_fixture):
    return review_environment_fixture


@pytest.fixture
def workspace(workspace_fixture):
    return workspace_fixture


def events(caplog):
    found = []
    for row in caplog.records:
        message = row.getMessage()
        if not message.startswith("{"):
            continue
        value = json.loads(message)
        if value.get("event_version") == "question_artifact_event_v1":
            assert set(value) == FIELDS
            found.append(value)
    return found


def approve(monkeypatch, ws, result, key="idem-timeline-approve"):
    approver, ready = reviewer(monkeypatch, SUBJECTS["client"])
    return approver.post(
        ws.review,
        json=claim_command(
            ws,
            resource="artifact",
            resource_id=result["artifact_id"],
            resource_version=result["artifact_version"],
            action="approve",
            expected_state="pending_review",
            idempotency_key=key,
            support_review=None,
        ),
        headers=ready,
    )


# The contract


def valid_event(**changes):
    value = {
        "event_version": "question_artifact_event_v1",
        "request_id": None,
        "investigation_id": "inv_timeline",
        "artifact_id": "art_" + "0" * 16,
        "artifact_version": "a" * 64,
        "dossier_version": "b" * 64,
        "stage": "artifact_review",
        "event": "completed",
        "action": "approve",
        "format": "html",
        "state": "approved",
        "occurred_at": "2026-09-25T10:00:00.000000Z",
        "reason_code": None,
    }
    value.update(changes)
    return value


def test_the_artifact_event_contract_is_closed():
    assert operator_timeline.validate_artifact_event(valid_event()) == valid_event()
    for change in [
        {"reason_code": "free text with spaces", "event": "failed"},
        {"reason_code": None, "event": "failed"},
        {"reason_code": "review_role_required"},
        {"request_id": None, "investigation_id": None},
        {"investigation_id": "PRIVATE question text"},
        {"stage": "artifact_publication"},
        {"action": "export_pdf"},
        {"format": "docx"},
        {"state": "published"},
        {"artifact_version": "not a digest"},
        {"occurred_at": "yesterday"},
    ]:
        with pytest.raises(ValueError):
            operator_timeline.validate_artifact_event(valid_event(**change))
    with pytest.raises(ValueError):
        operator_timeline.validate_artifact_event(valid_event() | {"html": "<p>x</p>"})


def test_an_event_keeps_no_malformed_caller_value_and_links_to_something():
    record = operator_timeline.artifact_event(
        stage="artifact_export",
        action="read",
        event="failed",
        investigation_id="inv_timeline",
        artifact_id="PRIVATE body",
        artifact_version="<script>",
        reason_code="Free Text",
    )
    assert record["artifact_id"] is None
    assert record["artifact_version"] is None
    assert record["reason_code"] == "artifact_refused"
    assert (
        operator_timeline.artifact_event(
            stage="artifact_export",
            action="export_html",
            event="failed",
            request_id="not-a-uuid",
            reason_code="request_invalid",
        )
        is None
    )


# Dossier artifacts, linked to the investigation


def test_prepare_review_and_export_leave_one_linked_event_each(
    monkeypatch, workspace, caplog
):
    caplog.set_level("INFO", logger=LOGGER)
    _editor, _ready, result = prepared_v2(monkeypatch, workspace)
    approval = approve(monkeypatch, workspace, result)
    assert approval.status_code == 200, approval.text
    read = client().post(
        read_url(workspace, result["artifact_id"]),
        params={"artifact_version": result["artifact_version"]},
    )
    assert read.status_code == 200, read.text
    recorded = events(caplog)
    assert [(e["stage"], e["action"], e["event"], e["state"]) for e in recorded] == [
        ("artifact_preparation", "prepare", "completed", "pending_review"),
        ("artifact_review", "approve", "completed", "approved"),
        ("artifact_export", "read", "completed", "approved"),
    ]
    for event in recorded:
        assert event["investigation_id"] == workspace.scope.investigation_id
        assert event["request_id"] is None
        assert event["artifact_id"] == result["artifact_id"]
        assert event["artifact_version"] == result["artifact_version"]
        assert event["dossier_version"] == workspace.version
        assert event["format"] == "html"
        assert event["reason_code"] is None
    text = caplog.text
    assert read.json()["html"][:200] not in text
    assert "<!doctype" not in text.lower()


def test_a_preview_is_a_review_event_and_a_withheld_read_is_a_failed_export(
    monkeypatch, workspace, caplog
):
    _editor, _ready, result = prepared_v2(monkeypatch, workspace)
    caplog.set_level("INFO", logger=LOGGER)
    caplog.clear()
    browser, ready = reviewer(monkeypatch, SUBJECTS["client"])
    preview = browser.post(
        preview_url(workspace, result["artifact_id"]),
        json=preview_body(result),
        headers=ready,
    )
    assert preview.status_code == 200, preview.text
    withheld = client().post(
        read_url(workspace, result["artifact_id"]),
        params={"artifact_version": result["artifact_version"]},
    )
    assert withheld.status_code == 409
    recorded = events(caplog)
    assert [
        (e["stage"], e["action"], e["event"], e["state"], e["reason_code"])
        for e in recorded
    ] == [
        ("artifact_review", "preview", "completed", "pending_review", None),
        ("artifact_export", "read", "failed", None, "artifact_approval_required"),
    ]
    assert all(e["investigation_id"] == workspace.scope.investigation_id for e in recorded)
    assert recorded[1]["artifact_id"] == result["artifact_id"]
    assert "<!doctype" not in caplog.text.lower()


def test_a_refused_prepare_is_a_failed_preparation_event(monkeypatch, workspace, caplog):
    caplog.set_level("INFO", logger=LOGGER)
    approver, ready = reviewer(monkeypatch, SUBJECTS["claims"])
    refused = approver.post(
        prepare_url(workspace), json=prepare_body(workspace), headers=ready
    )
    assert refused.status_code == 403
    (event,) = events(caplog)
    assert (event["stage"], event["action"], event["event"]) == (
        "artifact_preparation",
        "prepare",
        "failed",
    )
    assert event["reason_code"] == "review_role_required"
    assert event["investigation_id"] == workspace.scope.investigation_id
    assert event["artifact_id"] is None


def test_a_claim_review_is_not_an_artifact_event(monkeypatch, workspace, caplog):
    caplog.set_level("INFO", logger=LOGGER)
    approver, ready = reviewer(monkeypatch, SUBJECTS["claims"])
    decided = approver.post(workspace.review, json=claim_command(workspace), headers=ready)
    assert decided.status_code == 200, decided.text
    assert events(caplog) == []


def test_a_refused_artifact_review_is_a_failed_review_event(monkeypatch, workspace, caplog):
    _editor, _ready, result = prepared_v2(monkeypatch, workspace)
    caplog.set_level("INFO", logger=LOGGER)
    caplog.clear()
    editor, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    refused = editor.post(
        workspace.review,
        json=claim_command(
            workspace,
            resource="artifact",
            resource_id=result["artifact_id"],
            resource_version=result["artifact_version"],
            action="approve",
            expected_state="pending_review",
            idempotency_key="idem-timeline-editor",
            support_review=None,
        ),
        headers=ready,
    )
    assert refused.status_code == 403
    (event,) = events(caplog)
    assert (event["stage"], event["action"], event["event"]) == (
        "artifact_review",
        "approve",
        "failed",
    )
    assert event["artifact_id"] == result["artifact_id"]
    assert event["state"] is None
    assert event["reason_code"] == refused.json()["detail"]["code"]


def test_a_refused_review_never_reads_the_body_or_logs_caller_bytes(
    monkeypatch, workspace, caplog
):
    _editor, _ready, result = prepared_v2(monkeypatch, workspace)
    caplog.set_level("INFO", logger=LOGGER)
    caplog.clear()
    reads = []
    original = Request.body

    async def recorded(self):
        reads.append(self.url.path)
        return await original(self)

    monkeypatch.setattr(Request, "body", recorded)
    command = claim_command(
        workspace,
        resource="artifact",
        resource_id=result["artifact_id"],
        resource_version=result["artifact_version"],
        action="approve",
        expected_state="pending_review",
        idempotency_key="idem-timeline-unbound",
        support_review=None,
    )
    for headers in ({}, ORIGIN, {"Origin": "https://elsewhere.example"}):
        refused = client().post(workspace.review, json=command, headers=headers)
        assert 400 <= refused.status_code < 500
    assert reads == []
    assert events(caplog) == []


def test_an_invalid_review_command_logs_no_event_from_its_bytes(
    monkeypatch, workspace, caplog
):
    browser, ready = reviewer(monkeypatch, SUBJECTS["client"])
    caplog.set_level("INFO", logger=LOGGER)
    caplog.clear()
    forged = {"resource": "artifact", "action": "approve", "resource_id": "art_" + "1" * 16}
    refused = browser.post(workspace.review, json=forged, headers=ready)
    assert refused.status_code == 400
    assert events(caplog) == []


def test_an_event_that_cannot_be_logged_never_breaks_the_route(monkeypatch, workspace):
    def broken(**_fields):
        raise RuntimeError("sink down")

    browser, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    monkeypatch.setattr(operator_timeline, "artifact_event", broken)
    response = browser.post(
        prepare_url(workspace), json=prepare_body_v2(workspace), headers=ready
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "pending_review"


# Answer exports, linked to the question request


@pytest.fixture
def open_gate(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)


def test_answer_exports_are_export_events_linked_to_the_request(
    monkeypatch, caplog, open_gate
):
    caplog.set_level("INFO", logger=LOGGER)
    observe(monkeypatch)
    monkeypatch.setattr(
        pdf_exporter, "render_stored_html_pdf", lambda _html, *, asset_root: b"%PDF-1.7"
    )
    browser = client()
    assert browser.get(f"/api/chat/answer/{RID}/export.html").status_code == 200
    assert browser.get(f"/api/chat/answer/{RID}/export.pdf").status_code == 200
    recorded = events(caplog)
    assert [(e["stage"], e["action"], e["event"], e["format"]) for e in recorded] == [
        ("artifact_export", "export_html", "completed", "html"),
        ("artifact_export", "export_pdf", "completed", "pdf"),
    ]
    assert all(e["request_id"] == RID for e in recorded)
    assert all(e["investigation_id"] is None for e in recorded)
    assert "Stored answer text." not in caplog.text
    assert "amapiano" not in caplog.text


def test_a_refused_answer_export_is_a_failed_export_event(monkeypatch, caplog, open_gate):
    caplog.set_level("INFO", logger=LOGGER)
    observe(monkeypatch, error=WorkerProcessError("observation_request_unavailable"))
    refused = client().get(f"/api/chat/answer/{RID}/export.html")
    assert refused.status_code == 404
    (event,) = events(caplog)
    assert (event["action"], event["event"], event["reason_code"]) == (
        "export_html",
        "failed",
        "request_unavailable",
    )
    assert event["request_id"] == RID


def test_a_malformed_export_reference_links_to_nothing_and_logs_no_event(
    monkeypatch, caplog, open_gate
):
    caplog.set_level("INFO", logger=LOGGER)
    observe(monkeypatch)
    refused = client().get("/api/chat/answer/not-a-request/export.html")
    assert refused.status_code == 400
    assert events(caplog) == []
