"""The working review journey through the handler: selection, review, prepare, approval and export."""

from __future__ import annotations

import hashlib
import io
import json

import pytest
from pypdf import PdfReader

from src.api import (
    ask_export,
    dossier_artifacts,
    dossier_resolver,
    dossier_review_store,
    dossier_store,
    main,
    workspace_scope,
)
from tests.unit import dossier_pdf_fakes
from tests.unit.test_dossier_resolver import RECEIPTS, claim_record, dossier, resolved_scope
from tests.unit.test_dossier_store import PREFIX, top_payload
from tests.unit.test_main_dossier_review_routes import (
    ORIGIN,
    SUBJECTS,
    claim_command,
    client,
    detail,
    reviewer,
    untouched_storage,
)
from tests.unit.test_main_dossier_review_routes import (
    review_environment as review_environment_fixture,
)
from tests.unit.test_main_dossier_review_routes import workspace as workspace_fixture

_SHARED_FIXTURES = (review_environment_fixture, workspace_fixture)


@pytest.fixture(autouse=True)
def review_environment(review_environment_fixture):
    return review_environment_fixture


@pytest.fixture
def workspace(workspace_fixture):
    return workspace_fixture


@pytest.fixture
def renderer(monkeypatch):
    return dossier_pdf_fakes.install(monkeypatch)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def selection_url(ws):
    return f"/api/internal/v2/investigations/{ws.scope.investigation_id}/dossier/selection"


def prepare_url(ws):
    return f"/api/internal/v2/investigations/{ws.scope.investigation_id}/artifacts/prepare"


def export_url(ws, artifact_id, extension):
    return (
        f"/api/v2/investigations/{ws.scope.investigation_id}"
        f"/artifacts/{artifact_id}/export.{extension}"
    )


def selection_body(expected, ids):
    return {
        "contract_version": "dossier_review_v1",
        "expected_dossier_version": expected,
        "selected_claim_ids": list(ids),
    }


def prepare_body(version):
    return {"contract_version": "dossier_artifact_v1", "dossier_version": version, "format": "html"}


def approve_claim(ws, version, claim_id, receipt):
    browser, ready = reviewer_for(ws, "claims")
    response = browser.post(
        ws.review,
        json=claim_command(
            ws,
            claim_id,
            dossier_version=version,
            idempotency_key=f"idem-{claim_id}-{version[:8]}",
            support_review={"verdict": "supported", "receipt_ids": [receipt], "note": "checked"},
        ),
        headers=ready,
    )
    assert response.status_code == 200, response.text
    return response.json()


def artifact_command(ws, version, prepared, action="approve", key="idem-artifact"):
    return claim_command(
        ws,
        dossier_version=version,
        resource="artifact",
        resource_id=prepared["artifact_id"],
        resource_version=prepared["artifact_version"],
        action=action,
        expected_state="pending_review",
        idempotency_key=key,
        support_review=None,
    )


_SESSIONS = {}


def reviewer_for(ws, who):
    return _SESSIONS[who]


@pytest.fixture(autouse=True)
def sessions(monkeypatch, workspace):
    _SESSIONS.clear()
    for who in ("editor", "claims", "client", "relationships"):
        _SESSIONS[who] = reviewer(monkeypatch, SUBJECTS[who])
    yield _SESSIONS
    _SESSIONS.clear()


def select(ws, expected, ids, who="editor"):
    browser, ready = reviewer_for(ws, who)
    return browser.post(selection_url(ws), json=selection_body(expected, ids), headers=ready)


def prepare(ws, version, who="editor"):
    browser, ready = reviewer_for(ws, who)
    return browser.post(prepare_url(ws), json=prepare_body(version), headers=ready)


def export(ws, artifact_id, extension, version, **kwargs):
    return client().post(
        export_url(ws, artifact_id, extension), params={"artifact_version": version}, **kwargs
    )


def stored_record(ws, version):
    name = f"{PREFIX}dossiers/{ws.scope.investigation_id}/{version}.json"
    return json.loads(ws.bucket.objects[name][0])


def current_pointer(ws):
    return top_payload(
        ws.bucket, dossier_store.pointer_directory(PREFIX, ws.scope.investigation_id)
    )


def stored_artifact(ws, artifact_id):
    name = f"{PREFIX}artifacts/{ws.scope.investigation_id}/{artifact_id}.json"
    return json.loads(ws.bucket.objects[name][0])


def stored_pdf(ws, artifact_id):
    return ws.bucket.objects[f"{PREFIX}artifacts/{ws.scope.investigation_id}/{artifact_id}.pdf"][0]


def pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data), strict=True)
    return " ".join(" ".join(page.extract_text().split()) for page in reader.pages)


def pdf_metadata(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data), strict=True)
    values = [f"{key} {value}" for key, value in (reader.metadata or {}).items()]
    if "/Metadata" in reader.trailer["/Root"]:
        values.append("xmp")
    return " ".join(values).lower()


def allowed_lines(client_read: dict, title: str | None = None) -> list[str]:
    """The page text the export may carry: named by its question, dates in words, no readiness."""
    lines = ["Client Read", title or "Investigation " + client_read["investigation_id"]]
    if client_read["concise_answer"]:
        lines += ["Answer", client_read["concise_answer"]]
    if client_read["claims"]:
        lines.append("Findings")
        for claim in client_read["claims"]:
            lines += [claim["kind"], claim["text"]]
            if claim["citations"]:
                lines.append("Sources: " + ", ".join(claim["citations"]))
    if client_read["evidence"]:
        lines.append("Evidence")
        for item in client_read["evidence"]:
            lines += [item["evidence_id"], ask_export._date_text(item["published_at"])]
    if client_read["excluded_count"]:
        lines.append(f"{client_read['excluded_count']} item(s) withheld from this read.")
    return lines


# Selection


def test_selection_creates_a_new_immutable_version_and_moves_the_pointer(workspace):
    before = dict(workspace.bucket.objects)
    response = select(workspace, workspace.version, ["clm_1", "clm_3"])
    assert response.status_code == 200, response.text
    result = response.json()
    assert tuple(result) == (
        "contract_version",
        "investigation_id",
        "dossier_version",
        "decision_id",
        "state",
    )
    assert result["contract_version"] == "dossier_review_v1"
    assert result["investigation_id"] == workspace.scope.investigation_id
    assert result["decision_id"] is None
    assert result["state"] == "draft"
    version = result["dossier_version"]
    assert version != workspace.version
    # Nothing that existed changed, the pointer's earlier entries included.
    for name, stored in before.items():
        assert workspace.bucket.objects[name] == stored
    record = stored_record(workspace, version)
    assert record["review_state"]["selected_claim_ids"] == ["clm_1", "clm_3"]
    assert record["predecessor_version"] == workspace.version
    old = stored_record(workspace, workspace.version)
    assert old["review_state"]["selected_claim_ids"] == ["clm_1", "clm_2"]
    assert {key: value for key, value in record.items() if key not in {"review_state", "predecessor_version", "created_at"}} == {
        key: value for key, value in old.items() if key not in {"review_state", "predecessor_version", "created_at"}
    }
    pointer = current_pointer(workspace)
    assert pointer["dossier_version"] == version
    assert pointer["predecessor_version"] == workspace.version
    resolved = dossier_resolver.resolve_dossier(workspace.scope, bucket=workspace.bucket)
    assert resolved["state"] == "ready"
    assert resolved["dossier_version"] == version
    publication = resolved["publication"]
    assert publication["publisher_ref"] == "human:" + sha256(SUBJECTS["editor"].encode())[:8]
    assert "@" not in json.dumps(publication)

    stale = select(workspace, workspace.version, ["clm_1"])
    assert (stale.status_code, detail(stale)["code"]) == (409, "review_version_conflict")
    assert current_pointer(workspace)["dossier_version"] == version
    same = select(workspace, version, ["clm_1", "clm_3"])
    assert same.status_code == 200
    assert same.json() == result


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"contract_version": "dossier_review_v1", "selected_claim_ids": ["clm_1"]},
        {"contract_version": "dossier_review_v2", "expected_dossier_version": "a" * 64, "selected_claim_ids": []},
        {"contract_version": "dossier_review_v1", "expected_dossier_version": "short", "selected_claim_ids": []},
        {"contract_version": "dossier_review_v1", "expected_dossier_version": "a" * 64, "selected_claim_ids": "clm_1"},
        {"contract_version": "dossier_review_v1", "expected_dossier_version": "a" * 64, "selected_claim_ids": ["clm_1", "clm_1"]},
        {"contract_version": "dossier_review_v1", "expected_dossier_version": "a" * 64, "selected_claim_ids": [1]},
        {
            "contract_version": "dossier_review_v1",
            "expected_dossier_version": "a" * 64,
            "selected_claim_ids": ["clm_1"],
            "principal_ref": "human:00000000",
        },
    ],
)
def test_a_selection_outside_the_grammar_is_refused_before_storage(monkeypatch, body):
    browser, ready = reviewer_for(None, "editor")
    untouched_storage(monkeypatch)
    refused = browser.post(
        "/api/internal/v2/investigations/inv_x/dossier/selection", json=body, headers=ready
    )
    assert (refused.status_code, detail(refused)["code"]) == (400, "review_request_invalid")


def test_selection_is_refused_before_any_write_without_an_editor_session(monkeypatch, workspace):
    body = selection_body(workspace.version, ["clm_1"])
    url = selection_url(workspace)
    browser, ready = reviewer_for(workspace, "claims")
    wrong_role = browser.post(url, json=body, headers=ready)
    assert (wrong_role.status_code, detail(wrong_role)["code"]) == (403, "review_role_required")
    editor, editor_ready = reviewer_for(workspace, "editor")
    wrong_session = browser.post(url, json=body, headers={**ORIGIN, "X-Review-CSRF": editor_ready["X-Review-CSRF"]})
    assert (wrong_session.status_code, detail(wrong_session)["message"]) == (400, "csrf_mismatch")
    passcode_only = client().post(url, json=body, headers={**ORIGIN, "X-Passcode": "s3cret"})
    assert (passcode_only.status_code, detail(passcode_only)["code"]) == (403, "review_role_required")
    service = client().post(
        url,
        json=body,
        headers={**ORIGIN, "Authorization": "Bearer aaaa.bbbb.cccc", "X-Review-CSRF": "x" * 43},
    )
    assert (service.status_code, detail(service)["code"]) == (403, "review_role_required")
    cross = editor.post(url, json=body, headers={**editor_ready, "Origin": "https://evil.test"})
    assert (cross.status_code, detail(cross)["code"]) == (400, "review_request_invalid")
    unknown = editor.post(url, json=selection_body(workspace.version, ["clm_1", "clm_8"]), headers=editor_ready)
    assert (unknown.status_code, detail(unknown)["code"]) == (400, "review_request_invalid")
    other = editor.post(
        "/api/internal/v2/investigations/inv_other/dossier/selection", json=body, headers=editor_ready
    )
    assert (other.status_code, detail(other)["code"]) == (404, "scope_invalid")
    assert workspace.scope.investigation_id not in other.text
    assert workspace.bucket.uploads == workspace.baseline


def test_two_editors_selecting_at_once_get_one_version_and_one_conflict(monkeypatch, workspace):
    second, second_ready = reviewer(monkeypatch, SUBJECTS["editor_two"])
    outcomes = {}
    pointer = dossier_store.pointer_directory(PREFIX, workspace.scope.investigation_id)
    workspace.bucket.before_upload.append(
        (
            lambda name: name.startswith(pointer),
            lambda: outcomes.__setitem__(
                "second",
                second.post(
                    selection_url(workspace),
                    json=selection_body(workspace.version, ["clm_3"]),
                    headers=second_ready,
                ),
            ),
        )
    )
    first = select(workspace, workspace.version, ["clm_1"])
    assert outcomes["second"].status_code == 200, outcomes["second"].text
    assert (first.status_code, detail(first)["code"]) == (409, "review_version_conflict")
    assert current_pointer(workspace)["dossier_version"] == outcomes["second"].json()["dossier_version"]


# The journey


def test_the_complete_review_journey_exports_exactly_the_approved_bytes(workspace, renderer):
    selected = select(workspace, workspace.version, ["clm_1"])
    assert selected.status_code == 200, selected.text
    version = selected.json()["dossier_version"]
    approve_claim(workspace, version, "clm_1", RECEIPTS[0])

    prepared = prepare(workspace, version)
    assert prepared.status_code == 200, prepared.text
    result = prepared.json()
    assert tuple(result) == (
        "contract_version",
        "investigation_id",
        "dossier_version",
        "artifact_id",
        "artifact_version",
        "state",
    )
    assert result["contract_version"] == "dossier_artifact_v1"
    assert result["dossier_version"] == version
    assert result["state"] == "pending_review"
    artifact = stored_artifact(workspace, result["artifact_id"])
    pdf = stored_pdf(workspace, result["artifact_id"])
    assert [html for html, _root in renderer.calls] == [artifact["html"].encode("utf-8")]
    assert artifact["manifest"]["export_digests"] == {
        "html": sha256(artifact["html"].encode("utf-8")),
        "client_read": sha256(dossier_artifacts.record_bytes(artifact["client_read"])),
        "pdf": sha256(pdf),
    }
    assert [claim["claim_id"] for claim in artifact["client_read"]["claims"]] == ["clm_1"]

    for extension in ("html", "pdf"):
        early = export(workspace, result["artifact_id"], extension, result["artifact_version"])
        assert (early.status_code, detail(early)["code"]) == (409, "artifact_approval_required")
        assert "Claim clm_1" not in early.text

    approver, approver_ready = reviewer_for(workspace, "client")
    approved = approver.post(
        workspace.review, json=artifact_command(workspace, version, result), headers=approver_ready
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "approved"

    html = export(workspace, result["artifact_id"], "html", result["artifact_version"])
    assert html.status_code == 200, html.text
    assert html.content == artifact["html"].encode("utf-8")
    assert html.headers["content-type"] == "text/html; charset=utf-8"
    assert html.headers["content-disposition"] == (
        f'attachment; filename="42-client-read-{result["artifact_id"]}.html"'
    )
    assert html.headers["cache-control"] == "private, no-store"
    exported_pdf = export(workspace, result["artifact_id"], "pdf", result["artifact_version"])
    assert exported_pdf.status_code == 200
    assert exported_pdf.content == pdf
    assert exported_pdf.headers["content-type"] == "application/pdf"
    assert sha256(exported_pdf.content) == artifact["manifest"]["export_digests"]["pdf"]

    # What a reader extracts from either file is the allowed payload, and
    # nothing the projection withheld.
    allowed = allowed_lines(artifact["client_read"], workspace.dossier["frame"]["decision_question"])
    assert allowed[1] == "Which emerging behaviour should the brand act on in six weeks?"
    assert workspace.scope.investigation_id not in html.text
    assert dossier_pdf_fakes.page_text(html.content) == allowed
    assert pdf_text(exported_pdf.content) == " ".join(allowed)
    metadata = pdf_metadata(exported_pdf.content)
    assert not [name for name in dossier_pdf_fakes.TOOL_NAMES if name in metadata]
    assert "generator" not in html.text.lower()
    withheld = [
        claim_record(workspace.dossier, claim_id)["text"]
        for claim_id in ("clm_2", "clm_3", "clm_4", "clm_6", "clm_7", "clm_8")
    ] + ["Limitation: internal", "Does it hold outside Gauteng?", RECEIPTS[1], RECEIPTS[2]]
    for text in withheld:
        assert text not in html.text
        assert text not in pdf_text(exported_pdf.content)


def test_a_changed_selection_needs_fresh_approval_and_the_old_version_stays_readable(workspace, renderer):
    first = select(workspace, workspace.version, ["clm_1"]).json()["dossier_version"]
    approve_claim(workspace, first, "clm_1", RECEIPTS[0])
    old = prepare(workspace, first).json()
    approver, approver_ready = reviewer_for(workspace, "client")
    assert approver.post(
        workspace.review, json=artifact_command(workspace, first, old), headers=approver_ready
    ).status_code == 200

    moved = select(workspace, first, ["clm_1", "clm_3"])
    assert moved.status_code == 200
    second = moved.json()["dossier_version"]
    stale = approver.post(
        workspace.review,
        json=artifact_command(workspace, first, old, key="idem-late"),
        headers=approver_ready,
    )
    assert (stale.status_code, detail(stale)["code"]) == (409, "review_version_conflict")

    approve_claim(workspace, second, "clm_1", RECEIPTS[0])
    fresh = prepare(workspace, second)
    assert fresh.status_code == 200, fresh.text
    fresh = fresh.json()
    assert fresh["artifact_id"] != old["artifact_id"]
    assert fresh["state"] == "pending_review"
    blocked = export(workspace, fresh["artifact_id"], "pdf", fresh["artifact_version"])
    assert (blocked.status_code, detail(blocked)["code"]) == (409, "artifact_approval_required")
    for extension in ("html", "pdf"):
        historical = export(workspace, old["artifact_id"], extension, old["artifact_version"])
        assert historical.status_code == 200, historical.text
    assert (
        export(workspace, old["artifact_id"], "html", old["artifact_version"]).content
        == stored_artifact(workspace, old["artifact_id"])["html"].encode("utf-8")
    )
    approved = approver.post(
        workspace.review,
        json=artifact_command(workspace, second, fresh, key="idem-fresh"),
        headers=approver_ready,
    )
    assert approved.status_code == 200
    assert export(workspace, fresh["artifact_id"], "pdf", fresh["artifact_version"]).status_code == 200


def test_export_reads_need_the_exact_version_and_reveal_nothing_across_scopes(
    monkeypatch, workspace, renderer
):
    result = prepare(workspace, workspace.version).json()
    approver, approver_ready = reviewer_for(workspace, "client")
    assert approver.post(
        workspace.review, json=artifact_command(workspace, workspace.version, result), headers=approver_ready
    ).status_code == 200
    artifact_id, version = result["artifact_id"], result["artifact_version"]
    for extension in ("html", "pdf"):
        url = export_url(workspace, artifact_id, extension)
        missing = client().post(url)
        assert (missing.status_code, detail(missing)["code"]) == (404, "scope_invalid")
        wrong = export(workspace, artifact_id, extension, "e" * 64)
        assert (wrong.status_code, detail(wrong)["code"]) == (404, "scope_invalid")
        widened = client().post(url, params={"artifact_version": version, "scope": "x"})
        assert (widened.status_code, detail(widened)["code"]) == (404, "scope_invalid")
        body = client().post(url, params={"artifact_version": version}, json={})
        assert (body.status_code, detail(body)["code"]) == (400, "workspace_request_invalid")
        absent = export(workspace, "art_0000000000000000", extension, version)
        assert (absent.status_code, detail(absent)["code"]) == (404, "scope_invalid")
        forged = export(workspace, "../" + artifact_id, extension, version)
        assert forged.status_code == 404

    foreign = resolved_scope(dossier())
    foreign = workspace_scope.ResolvedWorkspaceScope(
        investigation_id="inv_" + "f" * 64,
        frame=foreign.frame,
        response={},
        scope_digest="9" * 64,
    )
    monkeypatch.setattr(
        workspace_scope,
        "resolve_workspace_scope",
        lambda investigation_id, **kwargs: foreign
        if investigation_id == foreign.investigation_id
        else workspace.scope,
    )
    for extension in ("html", "pdf"):
        crossed = client().post(
            f"/api/v2/investigations/{foreign.investigation_id}/artifacts/{artifact_id}/export.{extension}",
            params={"artifact_version": version},
        )
        assert (crossed.status_code, detail(crossed)["code"]) == (404, "scope_invalid")
        assert workspace.scope.investigation_id not in crossed.text
        assert "Client Read" not in crossed.text
    monkeypatch.setattr(
        workspace_scope, "resolve_workspace_scope", lambda investigation_id, **kwargs: foreign
    )
    rescoped = export(workspace, artifact_id, "html", version)
    assert (rescoped.status_code, detail(rescoped)["code"]) == (404, "scope_invalid")
    assert "Client Read" not in rescoped.text


def test_a_tampered_pdf_is_not_served(monkeypatch, workspace, renderer):
    result = prepare(workspace, workspace.version).json()
    approver, approver_ready = reviewer_for(workspace, "client")
    approver.post(
        workspace.review, json=artifact_command(workspace, workspace.version, result), headers=approver_ready
    )
    name = f"{PREFIX}artifacts/{workspace.scope.investigation_id}/{result['artifact_id']}.pdf"
    workspace.bucket.replace(name, workspace.bucket.objects[name][0] + b"%changed")
    tampered = export(workspace, result["artifact_id"], "pdf", result["artifact_version"])
    assert (tampered.status_code, detail(tampered)["code"]) == (503, "workspace_unavailable")
    del workspace.bucket.objects[name]
    gone = export(workspace, result["artifact_id"], "pdf", result["artifact_version"])
    assert (gone.status_code, detail(gone)["code"]) == (503, "workspace_unavailable")


def test_a_missing_citation_blocks_prepare_before_rendering_or_writing(workspace, renderer):
    version = select(workspace, workspace.version, ["clm_1", "clm_4"]).json()["dossier_version"]
    baseline = list(workspace.bucket.uploads)
    refused = prepare(workspace, version)
    assert (refused.status_code, detail(refused)["code"]) == (409, "artifact_citation_missing")
    assert renderer.calls == []
    assert workspace.bucket.uploads == baseline


@pytest.mark.parametrize("reason", ["pdf_browser_unavailable", "pdf_text_missing", "pdf_render_busy"])
def test_a_failed_pdf_leaves_no_sealed_artifact_and_a_plain_refusal(workspace, renderer, reason):
    renderer.refusal = reason
    refused = prepare(workspace, workspace.version)
    assert (refused.status_code, detail(refused)["code"]) == (503, "review_storage_unavailable")
    assert reason not in refused.text
    assert "pdf_" not in refused.text
    assert workspace.bucket.uploads == workspace.baseline
    assert not [name for name in workspace.bucket.objects if "/artifacts/" in name]


def test_a_pdf_that_cannot_be_stored_leaves_no_sealed_artifact(workspace, renderer):
    directory = f"{PREFIX}artifacts/{workspace.scope.investigation_id}/"
    original = workspace.bucket.blob

    def failing_blob(name):
        blob = original(name)
        if name.endswith(".pdf"):
            workspace.bucket.failing.add(name)
        return blob

    workspace.bucket.blob = failing_blob
    refused = prepare(workspace, workspace.version)
    assert (refused.status_code, detail(refused)["code"]) == (503, "review_storage_unavailable")
    assert not [name for name in workspace.bucket.objects if name.startswith(directory)]
    assert not [
        name for name in workspace.bucket.objects if "/decisions/" in name
    ]


def test_prepare_is_refused_before_any_write_without_an_editor_session(monkeypatch, workspace, renderer):
    body = prepare_body(workspace.version)
    url = prepare_url(workspace)
    browser, ready = reviewer_for(workspace, "client")
    wrong_role = browser.post(url, json=body, headers=ready)
    assert (wrong_role.status_code, detail(wrong_role)["code"]) == (403, "review_role_required")
    passcode_only = client().post(url, json=body, headers={**ORIGIN, "X-Passcode": "s3cret"})
    assert (passcode_only.status_code, detail(passcode_only)["code"]) == (403, "review_role_required")
    service = client().post(
        url,
        json=body,
        headers={**ORIGIN, "Authorization": "Bearer aaaa.bbbb.cccc", "X-Review-CSRF": "x" * 43},
    )
    assert (service.status_code, detail(service)["code"]) == (403, "review_role_required")
    assert renderer.calls == []
    assert workspace.bucket.uploads == workspace.baseline


def test_concurrent_reviews_on_one_artifact_give_one_success_and_one_conflict(
    monkeypatch, workspace, renderer
):
    result = prepare(workspace, workspace.version).json()
    first, first_ready = reviewer_for(workspace, "client")
    second, second_ready = reviewer(monkeypatch, SUBJECTS["client"])
    pointer = dossier_review_store.state_pointer_directory(
        PREFIX, workspace.scope.investigation_id, workspace.version
    )
    outcomes = {}
    workspace.bucket.before_upload.append(
        (
            lambda name: name.startswith(pointer),
            lambda: outcomes.__setitem__(
                "reject",
                second.post(
                    workspace.review,
                    json=artifact_command(workspace, workspace.version, result, "reject", "idem-reject"),
                    headers=second_ready,
                ),
            ),
        )
    )
    approve = first.post(
        workspace.review,
        json=artifact_command(workspace, workspace.version, result),
        headers=first_ready,
    )
    statuses = sorted([approve.status_code, outcomes["reject"].status_code])
    assert statuses == [200, 409]
    loser = approve if approve.status_code == 409 else outcomes["reject"]
    assert detail(loser)["code"] == "review_version_conflict"
    entry = top_payload(workspace.bucket, pointer)["resources"]["artifact:" + result["artifact_id"]]
    assert entry["state"] == "rejected"
    assert len(entry["applied"]) == 2
    refused = export(workspace, result["artifact_id"], "pdf", result["artifact_version"])
    assert (refused.status_code, detail(refused)["code"]) == (409, "artifact_approval_required")


def test_the_same_idempotency_key_returns_the_recorded_event(workspace, renderer):
    result = prepare(workspace, workspace.version).json()
    approver, approver_ready = reviewer_for(workspace, "client")
    command = artifact_command(workspace, workspace.version, result)
    first = approver.post(workspace.review, json=command, headers=approver_ready)
    assert first.status_code == 200
    uploads = list(workspace.bucket.uploads)
    again = approver.post(workspace.review, json=command, headers=approver_ready)
    assert again.status_code == 200
    assert again.json() == first.json()
    assert workspace.bucket.uploads == uploads
    decisions = dossier_review_store.list_decisions(
        bucket=workspace.bucket,
        prefix=PREFIX,
        investigation_id=workspace.scope.investigation_id,
        dossier_version=workspace.version,
    )
    assert [item["decision_id"] for item in decisions].count(first.json()["decision_id"]) == 1
    changed = approver.post(
        workspace.review, json=command | {"action": "reject"}, headers=approver_ready
    )
    assert (changed.status_code, detail(changed)["code"]) == (409, "review_version_conflict")


def test_preparing_again_reuses_the_stored_exports_without_rendering(workspace, renderer):
    first = prepare(workspace, workspace.version)
    assert first.status_code == 200
    uploads = list(workspace.bucket.uploads)
    again = prepare(workspace, workspace.version)
    assert again.status_code == 200
    assert again.json() == first.json()
    assert len(renderer.calls) == 1
    assert workspace.bucket.uploads == uploads
    assert dossier_store._digest(first.json()["artifact_version"])


# The working read


def working_url(ws):
    return f"/api/internal/v2/investigations/{ws.scope.investigation_id}/dossier/working"


class _SessionReader:
    """A reviewer's browser that sends the session's CSRF header and no Origin on a GET."""

    def __init__(self, browser, csrf):
        self.browser = browser
        self.csrf = csrf

    def get(self, url, headers=None, **kwargs):
        return self.browser.get(url, headers={"X-Review-CSRF": self.csrf, **(headers or {})}, **kwargs)


def working_reader(ws, who="editor"):
    browser, ready = reviewer_for(ws, who)
    return _SessionReader(browser, ready["X-Review-CSRF"])


def test_the_working_read_needs_a_review_session_before_any_storage(monkeypatch, workspace):
    browser, ready = reviewer_for(workspace, "claims")
    url = working_url(workspace)
    untouched_storage(monkeypatch)
    passcode_only = client().get(url, headers={"X-Passcode": "s3cret"})
    assert (passcode_only.status_code, detail(passcode_only)["code"]) == (403, "review_role_required")
    service = client().get(url, headers={"Authorization": "Bearer aaaa.bbbb.cccc", "X-Review-CSRF": "x" * 43})
    assert (service.status_code, detail(service)["code"]) == (403, "review_role_required")
    forged = client()
    forged.cookies.set("review_session", "forged-session-id")
    fixation = forged.get(url, headers={"X-Review-CSRF": "x" * 43})
    assert (fixation.status_code, detail(fixation)["code"]) == (403, "review_role_required")
    missing = browser.get(url)
    assert (missing.status_code, detail(missing)["message"]) == (400, "csrf_missing")
    wrong = browser.get(url, headers={"X-Review-CSRF": "x" * 43})
    assert (wrong.status_code, detail(wrong)["message"]) == (400, "csrf_mismatch")
    cross = browser.get(url, headers={"X-Review-CSRF": ready["X-Review-CSRF"], "Sec-Fetch-Site": "cross-site"})
    assert (cross.status_code, detail(cross)["code"]) == (400, "review_request_invalid")


def test_the_working_read_closes_when_review_is_not_configured(monkeypatch, workspace):
    browser, ready = reviewer_for(workspace, "claims")
    monkeypatch.delenv("REVIEW_SUBJECT_ALLOWLIST")
    untouched_storage(monkeypatch)
    closed = browser.get(working_url(workspace), headers={"X-Review-CSRF": ready["X-Review-CSRF"]})
    assert (closed.status_code, detail(closed)["code"]) == (503, "review_authority_unavailable")


def test_every_review_role_may_read_the_working_view(workspace):
    for who in ("editor", "claims", "client", "relationships"):
        response = working_reader(workspace, who).get(working_url(workspace))
        assert response.status_code == 200, (who, response.text)


def test_the_working_read_carries_versions_to_review_and_every_artifact(workspace, renderer):
    first = select(workspace, workspace.version, ["clm_1"]).json()["dossier_version"]
    working = working_reader(workspace).get(working_url(workspace))
    assert working.status_code == 200, working.text
    assert working.headers["cache-control"] == "private, no-store"
    body = working.json()
    assert tuple(body) == (
        "contract_version",
        "investigation_id",
        "dossier_version",
        "generation",
        "state",
        "dossier",
        "projection",
        "publication",
        "resource_versions",
        "artifacts",
    )
    assert body["contract_version"] == "dossier_working_v1"
    assert body["investigation_id"] == workspace.scope.investigation_id
    assert body["dossier_version"] == first
    assert body["state"] == "ready"
    assert body["resource_versions"] == dossier_resolver.resource_versions(
        stored_record(workspace, first)
    )
    assert body["artifacts"] == []
    assert [claim["selected"] for claim in body["projection"]["claims"]][:2] == [True, False]
    assert all(claim["contradicting_evidence_ids"] == [] for claim in body["projection"]["claims"])

    # The version the working read names is the one the review endpoint checks.
    browser, ready = reviewer_for(workspace, "claims")
    reviewed = browser.post(
        workspace.review,
        json=claim_command(
            workspace,
            "clm_1",
            dossier_version=first,
            resource_version=body["resource_versions"]["claim"]["clm_1"],
            idempotency_key="idem-from-working",
        ),
        headers=ready,
    )
    assert reviewed.status_code == 200, reviewed.text
    relationship = next(iter(body["resource_versions"]["relationship"].items()))
    linked, link_ready = reviewer_for(workspace, "relationships")
    reviewed_link = linked.post(
        workspace.review,
        json=claim_command(
            workspace,
            dossier_version=first,
            resource="relationship",
            resource_id=relationship[0],
            resource_version=relationship[1],
            idempotency_key="idem-link-from-working",
            support_review=None,
        ),
        headers=link_ready,
    )
    assert reviewed_link.status_code == 200, reviewed_link.text

    prepared = prepare(workspace, first).json()
    approver, approver_ready = reviewer_for(workspace, "client")
    approver.post(workspace.review, json=artifact_command(workspace, first, prepared), headers=approver_ready)
    after = working_reader(workspace).get(working_url(workspace)).json()
    assert after["projection"]["claims"][0]["status"] == "approved"
    assert after["artifacts"] == [
        {
            "artifact_id": prepared["artifact_id"],
            "artifact_version": prepared["artifact_version"],
            "dossier_version": first,
            "state": "approved",
        }
    ]

    second = select(workspace, first, ["clm_1", "clm_3"]).json()["dossier_version"]
    current = working_reader(workspace).get(working_url(workspace)).json()
    assert current["dossier_version"] == second
    assert current["projection"]["claims"][0]["status"] == "pending"
    assert current["artifacts"] == after["artifacts"]
    pinned = working_reader(workspace).get(working_url(workspace), params={"dossier_version": first})
    assert pinned.status_code == 200
    assert pinned.json()["dossier_version"] == first
    assert pinned.json()["projection"] == after["projection"]


def test_the_working_read_refuses_without_revealing_another_version_or_investigation(
    monkeypatch, workspace
):
    unknown = working_reader(workspace).get(working_url(workspace), params={"dossier_version": "e" * 64})
    assert (unknown.status_code, detail(unknown)["code"]) == (404, "scope_invalid")
    assert "e" * 64 not in unknown.text
    malformed = working_reader(workspace).get(working_url(workspace), params={"dossier_version": "short"})
    assert (malformed.status_code, detail(malformed)["code"]) == (400, "review_request_invalid")
    widened = working_reader(workspace).get(
        working_url(workspace), params={"dossier_version": workspace.version, "scope": "x"}
    )
    assert (widened.status_code, detail(widened)["code"]) == (400, "review_request_invalid")
    other = working_reader(workspace).get("/api/internal/v2/investigations/inv_other/dossier/working")
    assert (other.status_code, detail(other)["code"]) == (404, "scope_invalid")
    assert workspace.scope.investigation_id not in other.text
    crossed = working_reader(workspace).get(
        f"/api/internal/v2/investigations/inv_other/dossier/working",
        params={"dossier_version": workspace.version},
    )
    assert (crossed.status_code, detail(crossed)["code"]) == (404, "scope_invalid")
    cross_site = working_reader(workspace).get(working_url(workspace), headers={"Sec-Fetch-Site": "cross-site"})
    assert (cross_site.status_code, detail(cross_site)["code"]) == (400, "review_request_invalid")
    workspace.bucket.read_failing = True
    down = working_reader(workspace).get(working_url(workspace))
    assert (down.status_code, detail(down)["code"]) == (503, "review_storage_unavailable")
    workspace.bucket.read_failing = False
    assert workspace.bucket.uploads == workspace.baseline


def test_an_approved_artifact_read_carries_the_exact_stored_pdf(workspace, renderer):
    import base64

    result = prepare(workspace, workspace.version).json()
    url = f"/api/v2/investigations/{workspace.scope.investigation_id}/artifacts/{result['artifact_id']}/read"
    pending = client().post(url, params={"artifact_version": result["artifact_version"]})
    assert pending.status_code == 200
    assert "pdf" not in pending.json()
    approver, approver_ready = reviewer_for(workspace, "client")
    approver.post(
        workspace.review, json=artifact_command(workspace, workspace.version, result), headers=approver_ready
    )
    approved = client().post(url, params={"artifact_version": result["artifact_version"]})
    assert approved.status_code == 200
    payload = approved.json()
    assert tuple(payload)[-1] == "pdf"
    pdf = base64.b64decode(payload["pdf"], validate=True)
    assert pdf == stored_pdf(workspace, result["artifact_id"])
    assert sha256(pdf) == payload["manifest"]["export_digests"]["pdf"]
    assert payload["html"] == stored_artifact(workspace, result["artifact_id"])["html"]
    name = f"{PREFIX}artifacts/{workspace.scope.investigation_id}/{result['artifact_id']}.pdf"
    workspace.bucket.replace(name, pdf + b"%changed")
    tampered = client().post(url, params={"artifact_version": result["artifact_version"]})
    assert (tampered.status_code, detail(tampered)["code"]) == (503, "workspace_unavailable")
