"""Artifact prepare and the versioned artifact read, plus the research export scope binding."""

from __future__ import annotations

import hashlib
import json

import pytest
from src.api import (
    bq,
    dossier_artifacts,
    dossier_review_store,
    dossier_store,
    intelligence_dossier,
    investigation_scopes,
    main,
    workspace_scope,
)
from tests.unit.test_dossier_resolver import claim, dossier, resolved_scope
from tests.unit.test_dossier_store import PREFIX, rewrite_top_payload, top_payload
from tests.unit.test_main_dossier_review_routes import (
    ORIGIN,
    SUBJECTS,
    claim_command,
    client,
    detail,
    make_bsa_workspace,
    pointer_name,
    reviewer,
    rewrite_pointer,
    stored_pointer,
    untouched_storage,
)
from tests.unit.test_main_dossier_review_routes import (
    review_environment as review_environment_fixture,
)
from tests.unit.test_main_dossier_review_routes import workspace as workspace_fixture

# pytest resolves the imported fixtures by parameter name; naming them here
# keeps the imports visibly in use for the linter.
_SHARED_FIXTURES = (review_environment_fixture, workspace_fixture)


@pytest.fixture(autouse=True)
def review_environment(review_environment_fixture):
    return review_environment_fixture


@pytest.fixture
def workspace(workspace_fixture):
    return workspace_fixture


def prepare_url(ws):
    return (
        f"/api/internal/v2/investigations/{ws.scope.investigation_id}/artifacts/prepare"
    )


def read_url(ws, artifact_id):
    return f"/api/v2/investigations/{ws.scope.investigation_id}/artifacts/{artifact_id}/read"


def preview_url(ws, artifact_id):
    return (
        f"/api/internal/v2/investigations/{ws.scope.investigation_id}"
        f"/artifacts/{artifact_id}/preview"
    )


def prepare_body(ws, **overrides):
    value = {
        "contract_version": "dossier_artifact_v1",
        "dossier_version": ws.version,
        "format": "html",
    }
    value.update(overrides)
    return value


def prepare_body_v2(ws, **overrides):
    value = {
        "contract_version": "dossier_artifact_v2",
        "dossier_version": ws.version,
        "format": "html",
        "content_kind": "narrative_report",
        "preset_id": "general_42_v1",
    }
    value.update(overrides)
    return value


def preview_body(result, **overrides):
    value = {
        "contract_version": "dossier_artifact_preview_v1",
        "artifact_version": result["artifact_version"],
    }
    value.update(overrides)
    return value


def artifact_name(ws, artifact_id):
    return f"{PREFIX}artifacts/{ws.scope.investigation_id}/{artifact_id}.json"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return dossier_store._canonical(value)


def prepared(monkeypatch, ws):
    browser, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    response = browser.post(prepare_url(ws), json=prepare_body(ws), headers=ready)
    assert response.status_code == 200, response.text
    return browser, ready, response.json()


def prepared_v2(monkeypatch, ws):
    browser, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    response = browser.post(prepare_url(ws), json=prepare_body_v2(ws), headers=ready)
    assert response.status_code == 200, response.text
    return browser, ready, response.json()


# Prepare


def test_prepare_requires_the_editor_session_before_storage(monkeypatch, workspace):
    untouched_storage(monkeypatch)
    none = client().post(
        prepare_url(workspace), json=prepare_body(workspace), headers=ORIGIN
    )
    assert (none.status_code, detail(none)["code"]) == (403, "review_role_required")
    approver, ready = reviewer(monkeypatch, SUBJECTS["claims"])
    untouched_storage(monkeypatch)
    wrong = approver.post(
        prepare_url(workspace), json=prepare_body(workspace), headers=ready
    )
    assert (wrong.status_code, detail(wrong)["code"]) == (403, "review_role_required")
    editor, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    untouched_storage(monkeypatch)
    cross = editor.post(
        prepare_url(workspace),
        json=prepare_body(workspace),
        headers={
            "Origin": "https://evil.test",
            "X-Review-CSRF": ready["X-Review-CSRF"],
        },
    )
    assert (cross.status_code, detail(cross)["message"]) == (400, "origin_mismatch")


@pytest.mark.parametrize(
    "broken",
    [
        {"format": "pdf"},
        {"contract_version": "dossier_review_v1"},
        {"dossier_version": "b" * 63},
        {"extra": True},
        {"principal_ref": "human:5f2c9a1b"},
    ],
)
def test_prepare_body_grammar_is_exact_and_refused_before_storage(
    monkeypatch, workspace, broken
):
    editor, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    untouched_storage(monkeypatch)
    refused = editor.post(
        prepare_url(workspace), json=prepare_body(workspace) | broken, headers=ready
    )
    assert (refused.status_code, detail(refused)["code"]) == (
        400,
        "review_request_invalid",
    )


def test_prepare_writes_one_artifact_and_submits_it_for_review(monkeypatch, workspace):
    editor, ready, result = prepared(monkeypatch, workspace)
    assert tuple(result) == (
        "contract_version",
        "investigation_id",
        "dossier_version",
        "artifact_id",
        "artifact_version",
        "state",
    )
    assert result["contract_version"] == "dossier_artifact_v1"
    assert result["investigation_id"] == workspace.scope.investigation_id
    assert result["dossier_version"] == workspace.version
    assert result["state"] == "pending_review"
    assert result["artifact_id"] == "art_" + result["artifact_version"][:16]

    stored = json.loads(
        workspace.bucket.objects[artifact_name(workspace, result["artifact_id"])][0]
    )
    assert set(stored) == set(dossier_artifacts.ARTIFACT_FIELDS)
    assert stored["format"] == "html"
    manifest = stored["manifest"]
    assert set(manifest) == set(dossier_artifacts.BINDING_FIELDS)
    assert manifest["investigation_id"] == workspace.scope.investigation_id
    assert manifest["scope_digest"] == workspace.scope.scope_digest
    assert manifest["dossier_version"] == workspace.version
    assert manifest["artifact_version"] == result["artifact_version"]
    assert manifest["selection_digest"] == sha256(canonical(["clm_1", "clm_2"]))
    pdf_name = artifact_name(workspace, result["artifact_id"])[: -len(".json")] + ".pdf"
    assert manifest["export_digests"] == {
        "html": sha256(stored["html"].encode("utf-8")),
        "client_read": sha256(dossier_artifacts.record_bytes(stored["client_read"])),
        "pdf": sha256(workspace.bucket.objects[pdf_name][0]),
    }
    intelligence_dossier.validate_dossier_payload(stored["client_read"])
    assert stored["client_read"]["investigation_id"] == workspace.scope.investigation_id
    assert stored["html"].startswith("<!doctype html>")
    assert "Claim clm_1" in stored["html"]
    assert "clm_8" not in stored["html"]
    assert "Limitation: internal" not in stored["html"]
    assert dossier_artifacts.parse_artifact(stored) == stored

    entry = stored_pointer(workspace)["resources"]["artifact:" + result["artifact_id"]]
    assert entry["state"] == "pending_review"
    assert entry["resource_version"] == result["artifact_version"]
    decisions = dossier_review_store.list_decisions(
        bucket=workspace.bucket,
        prefix=PREFIX,
        investigation_id=workspace.scope.investigation_id,
        dossier_version=workspace.version,
    )
    assert [item["decision_id"] for item in decisions] == entry["applied"]
    assert decisions[0]["action"] == "submit"
    assert decisions[0]["support_review"] is None
    assert (
        decisions[0]["principal_ref"]
        == "human:" + sha256(SUBJECTS["editor"].encode("utf-8"))[:8]
    )

    again = editor.post(
        prepare_url(workspace), json=prepare_body(workspace), headers=ready
    )
    assert again.status_code == 200
    assert again.json() == result
    uploads = workspace.bucket.uploads[len(workspace.baseline) :]
    assert [name for name, _ in uploads] == [
        pdf_name,
        artifact_name(workspace, result["artifact_id"]),
        f"{PREFIX}decisions/{workspace.scope.investigation_id}/{workspace.version}/{entry['applied'][0]}.json",
        pointer_name(workspace),
    ]
    other, other_ready = reviewer(monkeypatch, SUBJECTS["editor"])
    same = other.post(
        prepare_url(workspace), json=prepare_body(workspace), headers=other_ready
    )
    assert same.status_code == 200
    assert same.json() == result
    assert workspace.bucket.uploads[len(workspace.baseline) :] == uploads


def test_prepare_for_a_version_that_is_not_current_or_storage_that_fails_writes_nothing(
    monkeypatch, workspace
):
    editor, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    stale = editor.post(
        prepare_url(workspace),
        json=prepare_body(workspace, dossier_version="f" * 64),
        headers=ready,
    )
    assert (stale.status_code, detail(stale)["code"]) == (
        409,
        "review_version_conflict",
    )
    assert workspace.bucket.uploads == workspace.baseline
    other = editor.post(
        "/api/internal/v2/investigations/inv_other/artifacts/prepare",
        json=prepare_body(workspace),
        headers=ready,
    )
    assert (other.status_code, detail(other)["code"]) == (404, "scope_invalid")
    workspace.bucket.read_failing = True
    down = editor.post(
        prepare_url(workspace), json=prepare_body(workspace), headers=ready
    )
    assert (down.status_code, detail(down)["code"]) == (
        503,
        "review_storage_unavailable",
    )
    assert workspace.bucket.uploads == workspace.baseline


def test_bsa_prepare_refuses_a_prohibited_client_read_without_writes_and_permits_a_control(
    monkeypatch,
):
    prohibited = make_bsa_workspace(monkeypatch, prohibited=True)
    editor, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    refused = editor.post(prepare_url(prohibited), json=prepare_body(prohibited), headers=ready)

    assert (refused.status_code, detail(refused)["code"]) == (
        403,
        "client_purpose_refused_at_export",
    )
    assert prohibited.bucket.uploads == prohibited.baseline
    assert pointer_name(prohibited) not in prohibited.bucket.objects

    permitted = make_bsa_workspace(monkeypatch, prohibited=False)
    editor, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    accepted = editor.post(prepare_url(permitted), json=prepare_body(permitted), headers=ready)

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state"] == "pending_review"


def test_the_html_export_escapes_source_text_and_loads_nothing_remote(
    monkeypatch, workspace
):
    hostile = dossier()
    claims = hostile["admitted_answer"]["response"]["intelligence"]["claims"]
    claims[0] = claim("clm_1", "observation", claims[0]["receipt_ids"]) | {
        "text": '<script src="https://evil.test/x.js"></script><img src=x onerror=alert(1)>'
    }
    client_read = intelligence_dossier.read_dossier(
        hostile["investigation_id"],
        __import__(
            "src.api.dossier_resolver", fromlist=["projection_record"]
        ).projection_record(hostile, []),
    )
    html = dossier_artifacts.render_html(client_read)
    assert "<script" not in html
    assert "<img" not in html
    assert "<link" not in html
    assert "&lt;script src=&quot;https://evil.test/x.js&quot;&gt;" in html
    assert 'src="' not in html
    assert "onerror=alert(1)&gt;" in html
    assert "generator" not in html.lower()
    assert html.count("http") == html.count("https://evil.test/x.js")


@pytest.mark.parametrize(
    "broken",
    [
        {"format": "report"},
        {"format": "pdf"},
        {"content_kind": "client_read"},
        {"preset_id": "bsa_v1"},
        {"extra": True},
    ],
)
def test_v2_prepare_request_grammar_refuses_before_storage(
    monkeypatch, workspace, broken
):
    editor, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    untouched_storage(monkeypatch)
    refused = editor.post(
        prepare_url(workspace),
        json=prepare_body_v2(workspace) | broken,
        headers=ready,
    )
    assert (refused.status_code, detail(refused)["code"]) == (
        400,
        "narrative_request_invalid",
    )


def test_v2_prepare_uses_the_complete_ordered_admitted_selection(monkeypatch, workspace):
    approver, approver_ready = reviewer(monkeypatch, SUBJECTS["claims"])
    approved = approver.post(
        workspace.review,
        json=claim_command(workspace, "clm_1"),
        headers=approver_ready,
    )
    assert approved.status_code == 200, approved.text
    _editor, _ready, result = prepared_v2(monkeypatch, workspace)
    stored = json.loads(
        workspace.bucket.objects[artifact_name(workspace, result["artifact_id"])][0]
    )
    assert result["contract_version"] == "dossier_artifact_v2"
    assert stored["contract_version"] == "dossier_artifact_v2"
    assert stored["content_kind"] == "narrative_report"
    assert stored["preset"]["client_scope_id"] == workspace.scope.frame.client_scope_id
    assert stored["report"]["selected_claim_ids"] == ["clm_1"]
    assert stored["manifest"]["selection_digest"] == sha256(canonical(["clm_1"]))
    assert set(stored["manifest"]["export_digests"]) == {
        "html",
        "client_read",
        "preset",
        "report",
    }
    assert "Claim clm_1" in stored["html"]
    assert "clm_8" not in stored["html"]
    assert "Limitation: internal" not in stored["html"]
    assert "2026-08-20T00:00:00Z" in stored["html"]
    assert "<pre>" not in stored["html"]
    assert dossier_artifacts.parse_artifact(stored) == stored


def test_v2_prepare_refuses_configured_client_without_identity_rights(monkeypatch):
    workspace = make_bsa_workspace(monkeypatch, prohibited=False)
    editor, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    refused = editor.post(
        prepare_url(workspace), json=prepare_body_v2(workspace), headers=ready
    )
    assert (refused.status_code, detail(refused)["code"]) == (
        503,
        "identity_assets_unavailable",
    )
    assert workspace.bucket.uploads == workspace.baseline
    assert pointer_name(workspace) not in workspace.bucket.objects


def test_v2_read_withholds_html_until_exact_approval(monkeypatch, workspace):
    _editor, _ready, result = prepared_v2(monkeypatch, workspace)
    url = read_url(workspace, result["artifact_id"])
    withheld = client().post(
        url, params={"artifact_version": result["artifact_version"]}
    )
    assert withheld.status_code == 409
    assert withheld.json() == {
        "detail": {
            "code": "artifact_approval_required",
            "message": "Exact artifact review is required.",
        }
    }
    assert "html" not in withheld.text.lower()
    approver, approver_ready = reviewer(monkeypatch, SUBJECTS["client"])
    approval = approver.post(
        workspace.review,
        json=claim_command(
            workspace,
            resource="artifact",
            resource_id=result["artifact_id"],
            resource_version=result["artifact_version"],
            action="approve",
            expected_state="pending_review",
            idempotency_key="idem-v2-art-approve",
            support_review=None,
        ),
        headers=approver_ready,
    )
    assert approval.status_code == 200, approval.text
    admitted = client().post(
        url, params={"artifact_version": result["artifact_version"]}
    )
    assert admitted.status_code == 200, admitted.text
    assert tuple(admitted.json()) == (
        "contract_version",
        "investigation_id",
        "dossier_version",
        "artifact_id",
        "artifact_version",
        "format",
        "content_kind",
        "state",
        "manifest",
        "client_read",
        "html",
    )
    assert admitted.json()["contract_version"] == "dossier_artifact_v2"
    assert admitted.json()["content_kind"] == "narrative_report"
    assert admitted.json()["state"] == "approved"
    assert admitted.json()["html"].startswith("<!doctype html>")


def test_v2_read_refuses_cross_scope_before_disclosing_content(monkeypatch, workspace):
    _editor, _ready, result = prepared_v2(monkeypatch, workspace)
    foreign = workspace_scope.ResolvedWorkspaceScope(
        investigation_id=workspace.scope.investigation_id,
        frame=workspace.scope.frame,
        response={},
        scope_digest="9" * 64,
    )
    monkeypatch.setattr(
        workspace_scope,
        "resolve_workspace_scope",
        lambda investigation_id, **kwargs: foreign,
    )
    refused = client().post(
        read_url(workspace, result["artifact_id"]),
        params={"artifact_version": result["artifact_version"]},
    )
    assert (refused.status_code, detail(refused)["code"]) == (404, "scope_invalid")
    assert "html" not in refused.text.lower()
    assert "client_read" not in refused.text


def test_v2_client_read_refuses_a_stale_current_dossier(monkeypatch, workspace):
    _editor, _ready, result = prepared_v2(monkeypatch, workspace)
    approver, approver_ready = reviewer(monkeypatch, SUBJECTS["client"])
    approval = approver.post(
        workspace.review,
        json=claim_command(
            workspace,
            resource="artifact",
            resource_id=result["artifact_id"],
            resource_version=result["artifact_version"],
            action="approve",
            expected_state="pending_review",
            idempotency_key="approve-report",
            support_review=None,
        ),
        headers=approver_ready,
    )
    assert approval.status_code == 200, approval.text
    current_log = dossier_store.pointer_directory(PREFIX, workspace.scope.investigation_id)
    current = top_payload(workspace.bucket, current_log)
    current["dossier_version"] = "9" * 64
    rewrite_top_payload(workspace.bucket, current_log, current)

    refused = client().post(
        read_url(workspace, result["artifact_id"]),
        params={"artifact_version": result["artifact_version"]},
    )

    assert (refused.status_code, detail(refused)["code"]) == (
        409,
        "review_version_conflict",
    )
    assert "html" not in refused.text.lower()


@pytest.mark.parametrize("subject", [SUBJECTS["editor"], SUBJECTS["client"]])
def test_authorized_preview_returns_exact_pending_bytes_without_a_write(
    monkeypatch, workspace, subject
):
    _editor, _ready, result = prepared_v2(monkeypatch, workspace)
    stored = json.loads(
        workspace.bucket.objects[artifact_name(workspace, result["artifact_id"])][0]
    )
    decisions_before = dossier_review_store.list_decisions(
        bucket=workspace.bucket,
        prefix=PREFIX,
        investigation_id=workspace.scope.investigation_id,
        dossier_version=workspace.version,
    )
    uploads_before = list(workspace.bucket.uploads)
    browser, ready = reviewer(monkeypatch, subject)

    response = browser.post(
        preview_url(workspace, result["artifact_id"]),
        json=preview_body(result),
        headers=ready,
    )

    assert response.status_code == 200, response.text
    assert tuple(response.json()) == (
        "contract_version",
        "investigation_id",
        "dossier_version",
        "artifact_id",
        "artifact_version",
        "format",
        "content_kind",
        "state",
        "manifest",
        "client_read",
        "html",
        "delivery",
    )
    assert response.json() == {
        "contract_version": "dossier_artifact_preview_v1",
        "investigation_id": workspace.scope.investigation_id,
        "dossier_version": workspace.version,
        "artifact_id": result["artifact_id"],
        "artifact_version": result["artifact_version"],
        "format": "html",
        "content_kind": "narrative_report",
        "state": "pending_review",
        "manifest": stored["manifest"],
        "client_read": stored["client_read"],
        "html": stored["html"],
        "delivery": "review_preview",
    }
    assert workspace.bucket.uploads == uploads_before
    assert dossier_review_store.list_decisions(
        bucket=workspace.bucket,
        prefix=PREFIX,
        investigation_id=workspace.scope.investigation_id,
        dossier_version=workspace.version,
    ) == decisions_before
    client_read = client().post(
        read_url(workspace, result["artifact_id"]),
        params={"artifact_version": result["artifact_version"]},
    )
    assert (client_read.status_code, detail(client_read)["code"]) == (
        409,
        "artifact_approval_required",
    )


def test_preview_rejects_roles_forged_ids_and_caller_scope_before_storage(
    monkeypatch, workspace
):
    bare = client().post(
        preview_url(workspace, "art_0000000000000000"),
        json={
            "contract_version": "dossier_artifact_preview_v1",
            "artifact_version": "0" * 64,
        },
        headers=ORIGIN,
    )
    assert (bare.status_code, detail(bare)["code"]) == (
        403,
        "review_role_required",
    )
    claims, claims_ready = reviewer(monkeypatch, SUBJECTS["claims"])
    denied = claims.post(
        preview_url(workspace, "art_0000000000000000"),
        json={
            "contract_version": "dossier_artifact_preview_v1",
            "artifact_version": "0" * 64,
        },
        headers=claims_ready,
    )
    assert (denied.status_code, detail(denied)["code"]) == (
        403,
        "review_role_required",
    )
    editor, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    untouched_storage(monkeypatch)
    forged = editor.post(
        preview_url(workspace, "art_forged"),
        json={
            "contract_version": "dossier_artifact_preview_v1",
            "artifact_version": "0" * 64,
        },
        headers=ready,
    )
    assert (forged.status_code, detail(forged)["code"]) == (404, "scope_invalid")
    widened = editor.post(
        preview_url(workspace, "art_0000000000000000"),
        json={
            "contract_version": "dossier_artifact_preview_v1",
            "artifact_version": "0" * 64,
            "client_scope_id": "other_client",
        },
        headers=ready,
    )
    assert (widened.status_code, detail(widened)["code"]) == (
        400,
        "narrative_request_invalid",
    )


def test_preview_refuses_wrong_version_and_scope_without_content(monkeypatch, workspace):
    _editor, _ready, result = prepared_v2(monkeypatch, workspace)
    browser, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    wrong = browser.post(
        preview_url(workspace, result["artifact_id"]),
        json=preview_body(result, artifact_version="9" * 64),
        headers=ready,
    )
    assert (wrong.status_code, detail(wrong)["code"]) == (404, "scope_invalid")
    foreign = workspace_scope.ResolvedWorkspaceScope(
        investigation_id=workspace.scope.investigation_id,
        frame=workspace.scope.frame,
        response={},
        scope_digest="9" * 64,
    )
    monkeypatch.setattr(
        workspace_scope,
        "resolve_workspace_scope",
        lambda investigation_id, **kwargs: foreign,
    )
    scoped = browser.post(
        preview_url(workspace, result["artifact_id"]),
        json=preview_body(result),
        headers=ready,
    )
    assert (scoped.status_code, detail(scoped)["code"]) == (404, "scope_invalid")
    assert "html" not in wrong.text.lower()
    assert "html" not in scoped.text.lower()


@pytest.mark.parametrize(
    ("field", "status", "code"),
    [
        ("dossier_version", 409, "review_version_conflict"),
        ("scope_digest", 404, "scope_invalid"),
    ],
)
def test_preview_refuses_a_stale_or_foreign_dossier_pointer_without_content(
    monkeypatch, workspace, field, status, code
):
    _editor, _ready, result = prepared_v2(monkeypatch, workspace)
    current_log = dossier_store.pointer_directory(PREFIX, workspace.scope.investigation_id)
    current = top_payload(workspace.bucket, current_log)
    current[field] = "9" * 64
    rewrite_top_payload(workspace.bucket, current_log, current)
    browser, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    refused = browser.post(
        preview_url(workspace, result["artifact_id"]),
        json=preview_body(result),
        headers=ready,
    )
    assert (refused.status_code, detail(refused)["code"]) == (status, code)
    assert "html" not in refused.text.lower()


@pytest.mark.parametrize("corrupt", ["artifact", "decision"])
def test_preview_refuses_corrupt_artifact_or_applied_review_binding(
    monkeypatch, workspace, corrupt
):
    _editor, _ready, result = prepared_v2(monkeypatch, workspace)
    if corrupt == "artifact":
        name = artifact_name(workspace, result["artifact_id"])
        stored = json.loads(workspace.bucket.objects[name][0])
        stored["html"] += "<p>changed</p>"
        workspace.bucket.replace(name, canonical(stored))
    else:
        entry = stored_pointer(workspace)["resources"][
            "artifact:" + result["artifact_id"]
        ]
        decision_name = dossier_review_store.decision_object_name(
            PREFIX,
            workspace.scope.investigation_id,
            workspace.version,
            entry["applied"][-1],
        )
        workspace.bucket.replace(decision_name, b"{}")
    browser, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    refused = browser.post(
        preview_url(workspace, result["artifact_id"]),
        json=preview_body(result),
        headers=ready,
    )
    assert (refused.status_code, detail(refused)["code"]) == (
        503,
        "review_storage_unavailable",
    )
    assert "html" not in refused.text.lower()


@pytest.mark.parametrize("mutation", ["missing_submit", "reordered"])
@pytest.mark.parametrize("reader", ["preview", "client_read"])
def test_v2_disclosure_requires_the_complete_committed_review_chain(
    monkeypatch, workspace, mutation, reader
):
    _editor, _ready, result = prepared_v2(monkeypatch, workspace)
    approver, approver_ready = reviewer(monkeypatch, SUBJECTS["client"])
    approval = approver.post(
        workspace.review,
        json=claim_command(
            workspace,
            resource="artifact",
            resource_id=result["artifact_id"],
            resource_version=result["artifact_version"],
            action="approve",
            expected_state="pending_review",
            idempotency_key="repair-chain-approve",
            support_review=None,
        ),
        headers=approver_ready,
    )
    assert approval.status_code == 200, approval.text
    pointer = stored_pointer(workspace)
    entry = pointer["resources"]["artifact:" + result["artifact_id"]]
    assert len(entry["applied"]) == 2
    if mutation == "missing_submit":
        entry["applied"] = entry["applied"][-1:]
    else:
        entry["applied"].reverse()
    rewrite_pointer(workspace, pointer)

    if reader == "preview":
        refused = approver.post(
            preview_url(workspace, result["artifact_id"]),
            json=preview_body(result),
            headers=approver_ready,
        )
        expected_code = "review_storage_unavailable"
    else:
        refused = client().post(
            read_url(workspace, result["artifact_id"]),
            params={"artifact_version": result["artifact_version"]},
        )
        expected_code = "workspace_unavailable"
    assert (refused.status_code, detail(refused)["code"]) == (503, expected_code)
    assert "html" not in refused.text.lower()


@pytest.mark.parametrize("reader", ["preview", "client_read"])
@pytest.mark.parametrize(
    ("path", "invalid"),
    [
        (("report", "scope_digest"), 7),
        (("report", "sections", 0, "state"), []),
        (("report", "sections", 1, "items", 0, "kind"), {}),
        (("format",), []),
        (("client_read", "excluded_reasons"), None),
    ],
)
def test_v2_routes_bound_nested_digest_corruption_without_html(
    monkeypatch, workspace, reader, path, invalid
):
    approver, approver_ready = reviewer(monkeypatch, SUBJECTS["claims"])
    approved = approver.post(
        workspace.review,
        json=claim_command(workspace, "clm_1"),
        headers=approver_ready,
    )
    assert approved.status_code == 200, approved.text
    editor, ready, result = prepared_v2(monkeypatch, workspace)
    name = artifact_name(workspace, result["artifact_id"])
    artifact = json.loads(workspace.bucket.objects[name][0])
    target = artifact
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = invalid
    workspace.bucket.replace(name, dossier_artifacts.record_bytes(artifact))

    if reader == "preview":
        refused = editor.post(
            preview_url(workspace, result["artifact_id"]),
            json=preview_body(result),
            headers=ready,
        )
        expected_code = "review_storage_unavailable"
    else:
        refused = client().post(
            read_url(workspace, result["artifact_id"]),
            params={"artifact_version": result["artifact_version"]},
        )
        expected_code = "workspace_unavailable"
    assert (refused.status_code, detail(refused)["code"]) == (503, expected_code)
    assert "html" not in refused.text.lower()


# The versioned read


def test_read_requires_the_exact_artifact_version_and_the_scope(monkeypatch, workspace):
    _editor, _ready, result = prepared(monkeypatch, workspace)
    reader = client()
    url = read_url(workspace, result["artifact_id"])
    missing = reader.post(url)
    assert (missing.status_code, detail(missing)["code"]) == (404, "scope_invalid")
    wrong = reader.post(url, params={"artifact_version": "e" * 64})
    assert (wrong.status_code, detail(wrong)["code"]) == (404, "scope_invalid")
    malformed = reader.post(url, params={"artifact_version": "not-a-digest"})
    assert (malformed.status_code, detail(malformed)["code"]) == (404, "scope_invalid")
    widened = reader.post(
        url, params={"artifact_version": result["artifact_version"], "scope": "x"}
    )
    assert (widened.status_code, detail(widened)["code"]) == (404, "scope_invalid")
    body = reader.post(
        url, params={"artifact_version": result["artifact_version"]}, json={}
    )
    assert (body.status_code, detail(body)["code"]) == (
        400,
        "workspace_request_invalid",
    )
    absent = reader.post(
        read_url(workspace, "art_0000000000000000"),
        params={"artifact_version": result["artifact_version"]},
    )
    assert (absent.status_code, detail(absent)["code"]) == (404, "scope_invalid")

    found = reader.post(url, params={"artifact_version": result["artifact_version"]})
    assert found.status_code == 200, found.text
    payload = found.json()
    assert tuple(payload) == (
        "contract_version",
        "investigation_id",
        "dossier_version",
        "artifact_id",
        "artifact_version",
        "format",
        "state",
        "manifest",
        "client_read",
        "html",
    )
    assert payload["state"] == "pending_review"
    stored = json.loads(
        workspace.bucket.objects[artifact_name(workspace, result["artifact_id"])][0]
    )
    assert payload["html"] == stored["html"]
    assert payload["client_read"] == stored["client_read"]
    assert payload["manifest"] == stored["manifest"]
    assert found.headers["cache-control"] == "private, no-store"


def test_read_refuses_a_cross_scope_artifact_with_the_same_code_as_absence(
    monkeypatch, workspace
):
    _editor, _ready, result = prepared(monkeypatch, workspace)
    name = artifact_name(workspace, result["artifact_id"])
    stored = json.loads(workspace.bucket.objects[name][0])
    foreign = resolved_scope(dossier())
    foreign = workspace_scope.ResolvedWorkspaceScope(
        investigation_id=foreign.investigation_id,
        frame=foreign.frame,
        response={},
        scope_digest="9" * 64,
    )
    monkeypatch.setattr(
        workspace_scope,
        "resolve_workspace_scope",
        lambda investigation_id, **kwargs: foreign,
    )
    refused = client().post(
        read_url(workspace, result["artifact_id"]),
        params={"artifact_version": result["artifact_version"]},
    )
    assert (refused.status_code, detail(refused)["code"]) == (404, "scope_invalid")
    assert "html" not in refused.text
    tampered = dict(stored, html=stored["html"] + "<p>changed</p>")
    workspace.bucket.replace(name, canonical(tampered))
    monkeypatch.setattr(
        workspace_scope,
        "resolve_workspace_scope",
        lambda investigation_id, **kwargs: workspace.scope,
    )
    broken = client().post(
        read_url(workspace, result["artifact_id"]),
        params={"artifact_version": result["artifact_version"]},
    )
    assert (broken.status_code, detail(broken)["code"]) == (
        503,
        "workspace_unavailable",
    )


def test_an_approval_binds_to_the_exact_artifact_version_only(monkeypatch, workspace):
    editor, ready, result = prepared(monkeypatch, workspace)
    approver, approver_ready = reviewer(monkeypatch, SUBJECTS["client"])
    approve = claim_command(
        workspace,
        resource="artifact",
        resource_id=result["artifact_id"],
        resource_version=result["artifact_version"],
        action="approve",
        expected_state="pending_review",
        idempotency_key="idem-art-approve",
        support_review=None,
    )
    wrong = approver.post(
        workspace.review,
        json=approve | {"resource_version": "e" * 64},
        headers=approver_ready,
    )
    assert (wrong.status_code, detail(wrong)["message"]) == (
        409,
        "resource_version_mismatch",
    )
    approved = approver.post(workspace.review, json=approve, headers=approver_ready)
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "approved"
    read = client().post(
        read_url(workspace, result["artifact_id"]),
        params={"artifact_version": result["artifact_version"]},
    )
    assert read.status_code == 200
    assert read.json()["state"] == "approved"
    decision = next(
        item
        for item in dossier_review_store.list_decisions(
            bucket=workspace.bucket,
            prefix=PREFIX,
            investigation_id=workspace.scope.investigation_id,
            dossier_version=workspace.version,
        )
        if item["decision_id"] == approved.json()["decision_id"]
    )
    manifest = read.json()["manifest"]
    assert dossier_artifacts.is_artifact_approved(
        manifest=manifest, decision=main._artifact_decision_binding(decision, manifest)
    )
    assert not dossier_artifacts.is_artifact_approved(
        manifest=dict(manifest, artifact_version="e" * 64),
        decision=main._artifact_decision_binding(decision, manifest),
    )
    replayed = editor.post(
        prepare_url(workspace), json=prepare_body(workspace), headers=ready
    )
    assert replayed.status_code == 200
    assert replayed.json() == result | {"state": "approved"}


def test_a_prepare_that_loses_the_pointer_to_another_resource_is_repeatable(monkeypatch, workspace):
    approver, approver_ready = reviewer(monkeypatch, SUBJECTS["claims"])
    editor, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    directory = f"{PREFIX}decisions/{workspace.scope.investigation_id}/{workspace.version}/"
    outcomes = {}
    workspace.bucket.before_upload.append(
        (
            lambda name: name.startswith(directory + "pointer/"),
            lambda: outcomes.__setitem__(
                "claim",
                approver.post(workspace.review, json=claim_command(workspace), headers=approver_ready),
            ),
        )
    )
    lost = editor.post(prepare_url(workspace), json=prepare_body(workspace), headers=ready)
    assert outcomes["claim"].status_code == 200
    assert (lost.status_code, detail(lost)["code"]) == (409, "review_version_conflict")
    assert "retry" in detail(lost)["message"]
    assert set(stored_pointer(workspace)["resources"]) == {"claim:clm_1"}
    other, other_ready = reviewer(monkeypatch, SUBJECTS["editor"])
    again = other.post(prepare_url(workspace), json=prepare_body(workspace), headers=other_ready)
    assert again.status_code == 200, again.text
    assert again.json()["state"] == "pending_review"
    entry = stored_pointer(workspace)["resources"]["artifact:" + again.json()["artifact_id"]]
    assert entry["state"] == "pending_review"
    assert len(entry["applied"]) == 1
    same = editor.post(prepare_url(workspace), json=prepare_body(workspace), headers=ready)
    assert same.status_code == 200
    assert same.json() == again.json()


def test_two_editors_preparing_the_same_content_settle_on_one_submit(monkeypatch, workspace):
    first, ready_first = reviewer(monkeypatch, SUBJECTS["editor"])
    second, ready_second = reviewer(monkeypatch, SUBJECTS["editor_two"])
    directory = f"{PREFIX}decisions/{workspace.scope.investigation_id}/{workspace.version}/"
    outcomes = {}
    workspace.bucket.before_upload.append(
        (
            lambda name: name.startswith(directory) and "/pointer/" not in name,
            lambda: outcomes.__setitem__(
                "second",
                second.post(prepare_url(workspace), json=prepare_body(workspace), headers=ready_second),
            ),
        )
    )
    outcomes["first"] = first.post(prepare_url(workspace), json=prepare_body(workspace), headers=ready_first)
    assert outcomes["second"].status_code == 200, outcomes["second"].text
    assert outcomes["second"].json()["state"] == "pending_review"
    assert (outcomes["first"].status_code, detail(outcomes["first"])["message"]) == (
        409,
        "resource state has moved",
    )
    artifact_id = outcomes["second"].json()["artifact_id"]
    entry = stored_pointer(workspace)["resources"]["artifact:" + artifact_id]
    assert len(entry["applied"]) == 1
    stored = {
        name
        for name in workspace.bucket.objects
        if name.startswith(directory) and "/pointer/" not in name
    }
    assert len(stored) == 2
    retry = first.post(prepare_url(workspace), json=prepare_body(workspace), headers=ready_first)
    assert retry.status_code == 200
    assert retry.json() == outcomes["second"].json()
    assert stored_pointer(workspace)["resources"]["artifact:" + artifact_id] == entry


def test_one_editor_preparing_twice_at_once_gets_one_decision_and_one_answer(monkeypatch, workspace):
    editor, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    directory = f"{PREFIX}decisions/{workspace.scope.investigation_id}/{workspace.version}/"
    outcomes = {}
    workspace.bucket.before_upload.append(
        (
            lambda name: name.startswith(directory) and "/pointer/" not in name,
            lambda: outcomes.__setitem__(
                "inner", editor.post(prepare_url(workspace), json=prepare_body(workspace), headers=ready)
            ),
        )
    )
    outer = editor.post(prepare_url(workspace), json=prepare_body(workspace), headers=ready)
    assert outcomes["inner"].status_code == 200
    assert outer.status_code == 200, outer.text
    assert outer.json() == outcomes["inner"].json()
    stored = [
        name
        for name in workspace.bucket.objects
        if name.startswith(directory) and "/pointer/" not in name
    ]
    assert len(stored) == 1
    entry = stored_pointer(workspace)["resources"]["artifact:" + outer.json()["artifact_id"]]
    assert len(entry["applied"]) == 1


# The research binding, one loader for every reader


@pytest.mark.parametrize(
    "forged",
    [
        "ra_short",
        "RA_0123456789ABCDEF",
        "ra_0123456789abcdef0",
        "ra_0123456789abcde",
        " ra_0123456789abcdef",
    ],
)
def test_a_forged_research_export_id_is_refused_before_any_private_read(
    monkeypatch, forged
):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    reads = []
    monkeypatch.setattr(bq, "get_research_artifact", lambda value: reads.append(value))
    refused = client().get(f"/api/research/{forged}/export.html")
    assert refused.status_code == 404, (forged, refused.text)
    assert refused.json()["detail"]["code"] == "scope_invalid"
    assert reads == []


def test_the_research_export_reads_once_and_compares_the_stored_scope_with_the_server_scope(
    monkeypatch,
):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    server_scope = investigation_scopes.default_client_scope_id()
    rows = {
        "ra_0123456789abcdef": {
            "artifact_id": "ra_0123456789abcdef",
            "client_scope_id": server_scope,
            "synthesis": {"title": "Bound brief"},
            "evidence": [],
            "markets": ["za"],
        },
        "ra_fedcba9876543210": {
            "artifact_id": "ra_fedcba9876543210",
            "client_scope_id": "other_client",
            "synthesis": {"title": "Foreign brief"},
            "evidence": [],
            "markets": ["za"],
        },
        "ra_00000000000000ff": {
            "artifact_id": "ra_00000000000000ff",
            "synthesis": {"title": "Unbound brief"},
            "evidence": [],
            "markets": ["za"],
        },
    }
    reads = []

    def read(value):
        reads.append(value)
        return rows.get(value)

    monkeypatch.setattr(bq, "get_research_artifact", read)
    rendered = []
    monkeypatch.setattr(
        main,
        "_research_html_attachment",
        lambda doc_json, **kwargs: (
            rendered.append(doc_json)
            or main.HTMLResponse("<!doctype html><p>brief</p>")
        ),
    )
    positive = client().get("/api/research/ra_0123456789abcdef/export.html")
    assert positive.status_code == 200, positive.text
    assert reads == ["ra_0123456789abcdef"]
    assert len(rendered) == 1
    foreign = client().get("/api/research/ra_fedcba9876543210/export.html")
    assert (foreign.status_code, foreign.json()["detail"]["code"]) == (
        404,
        "scope_invalid",
    )
    unbound = client().get("/api/research/ra_00000000000000ff/export.html")
    assert (unbound.status_code, unbound.json()["detail"]["code"]) == (
        404,
        "scope_invalid",
    )
    assert "Foreign brief" not in foreign.text
    assert "Unbound brief" not in unbound.text
    assert reads == [
        "ra_0123456789abcdef",
        "ra_fedcba9876543210",
        "ra_00000000000000ff",
    ]
    assert len(rendered) == 1
    caller_label = client().get(
        "/api/research/ra_fedcba9876543210/export.html",
        params={"client_scope_id": "other_client"},
    )
    assert caller_label.status_code == 404
    # Every sibling reader goes through the same loader.
    for path in ("/evidence-pack.html", ""):
        bound = client().get("/api/research/ra_0123456789abcdef" + path)
        assert bound.status_code == 200, (path, bound.text)
        foreign_too = client().get("/api/research/ra_fedcba9876543210" + path)
        assert (foreign_too.status_code, foreign_too.json()["detail"]["code"]) == (404, "scope_invalid")
        unbound_too = client().get("/api/research/ra_00000000000000ff" + path)
        assert (unbound_too.status_code, unbound_too.json()["detail"]["code"]) == (404, "scope_invalid")
    assert "Foreign brief" not in foreign_too.text
    assert "Unbound brief" not in unbound_too.text
    refined = client().post(
        "/api/research/refine",
        json={"artifact_id": "ra_fedcba9876543210", "instruction": "shorter"},
    )
    assert (refined.status_code, refined.json()["detail"]["code"]) == (404, "scope_invalid")
    monkeypatch.setattr(
        bq,
        "list_recent_research_artifacts",
        lambda limit: [
            {"artifact_id": aid, "client_scope_id": row.get("client_scope_id"), "markets": ["za"]}
            for aid, row in rows.items()
        ],
    )
    recent = client().get("/api/research/recent")
    assert recent.status_code == 200
    assert recent.json() == {"artifacts": [{"artifact_id": "ra_0123456789abcdef", "markets": ["za"]}]}
    stamped = {}
    monkeypatch.setattr(bq, "_persist_research_artifact_gcs", lambda aid, row: stamped.update(row))
    monkeypatch.setenv("RESEARCH_PERSIST_BACKEND", "gcs")
    assert bq.insert_research_artifact({"synthesis": {"title": "New brief"}}).startswith("ra_")
    assert stamped["client_scope_id"] == server_scope
    monkeypatch.setattr(
        investigation_scopes,
        "default_client_scope_id",
        lambda path=None: "other_client",
    )
    now_foreign = client().get("/api/research/ra_0123456789abcdef/export.html")
    assert now_foreign.status_code == 404
    assert len(rendered) == 1
