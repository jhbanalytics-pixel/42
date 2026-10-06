"""Every stored general 42 identity recomputes byte for byte from retained fixtures.

Investigation ids name stored objects, pointers, dossier versions and
approvals, and artifact versions name exact export bytes. A frame that names no
client lens must keep producing exactly what it produced before the lens could
be named. The retained fixture general_42_identities.json was captured at
7d16fd188c8a8e6ceda0cb851ee83d15725368b3, before the frame carried a lens, and
records for each kind the stored bytes and the ids derived from them. These
tests rebuild every one of them from the fixture's own inputs through today's
code and compare the bytes, so any change to a general 42 id, digest or export
fails here.

The retained Evidence Room fixture workspaces/investigation_plan_v1.json
carries one stored investigation id as well, and it is checked the same way.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from src.api import (
    dossier_artifacts,
    dossier_store,
    investigation_store,
    investigations,
    workspace_scope,
)
from tests.unit import dossier_pdf_fakes
from tests.unit.test_dossier_resolver import RECEIPTS
from tests.unit.test_main_dossier_journey_routes import (
    _SHARED_FIXTURES,
    approve_claim,
    artifact_command,
    current_pointer,
    export,
    prepare,
    renderer,
    review_environment,
    review_environment_fixture,
    reviewer_for,
    select,
    sessions,
    stored_artifact,
    stored_pdf,
    stored_record,
    workspace,
    workspace_fixture,
)

_JOURNEY_FIXTURES = (
    _SHARED_FIXTURES,
    renderer,
    review_environment,
    review_environment_fixture,
    sessions,
    workspace,
    workspace_fixture,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RETAINED = FIXTURES / "frame_lens" / "general_42_identities.json"
PLAN_FIXTURE = FIXTURES / "workspaces" / "investigation_plan_v1.json"


def retained() -> dict:
    return json.loads(RETAINED.read_text(encoding="utf-8"))


def ordered_bytes(value: object) -> bytes:
    """The bytes as written, key order included, so a moved key is a change too."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _names(kind: str) -> list[str]:
    return [entry["name"] for entry in retained()[kind]]


def _entry(kind: str, name: str) -> dict:
    return next(entry for entry in retained()[kind] if entry["name"] == name)


def test_the_retained_fixture_was_captured_before_the_frame_carried_a_lens():
    value = retained()
    assert value["provenance"]["captured_at_commit"] == "7d16fd188c8a8e6ceda0cb851ee83d15725368b3"
    for entry in value["frames"]:
        assert "client_lens" not in entry["frame"]
        assert "client_lens" not in entry["storage_record"]["frame"]
        assert "client_lens" not in entry["storage_record"]["response"]


@pytest.mark.parametrize("name", _names("frames"))
def test_a_stored_general_frame_recomputes_its_id_payload_scope_and_record_bytes(name):
    entry = _entry("frames", name)
    frame = investigations.investigation_frame_from_payload(entry["frame"])
    assert investigations.investigation_id_for_frame(frame) == entry["investigation_id"]
    assert ordered_bytes(investigations.investigation_frame_payload(frame)) == ordered_bytes(
        entry["frame"]
    )
    assert workspace_scope.ScopeBinding.from_frame(frame).scope_digest == entry["scope_digest"]
    response = investigations.build_plan_ready(
        frame=frame,
        plan=investigations.default_plan_for_frame(frame),
        investigation_id=entry["investigation_id"],
        created_at=datetime.fromisoformat(entry["created_at"].replace("Z", "+00:00")),
        active_role_versions=entry["active_role_versions"],
    )
    record = investigations.build_investigation_storage_record(frame, response)
    stored = investigation_store.canonical_investigation_bytes(record)
    assert stored == entry["stored_bytes"].encode("utf-8")
    assert sha256(stored) == entry["stored_sha256"]
    assert ordered_bytes(record) == ordered_bytes(entry["storage_record"])
    # The stored object reads back to the same frame and response.
    read_frame, read_response = investigations.validate_investigation_storage_record(
        json.loads(entry["stored_bytes"])
    )
    assert read_frame == frame
    assert read_response == entry["storage_record"]["response"]


def test_the_retained_evidence_room_investigation_id_recomputes():
    fixture = json.loads(PLAN_FIXTURE.read_text(encoding="utf-8"))
    status = fixture["status"]
    frame = investigations.InvestigationFrame(
        client_scope_id=status["client_scope_id"],
        market_scope=tuple(status["market_scope"]),
        brand_config_id=status["brand_config_id"],
        audience_lens_ids=tuple(status["audience_lens_ids"]),
        theme_id=status["theme_id"],
        run_id=status["run_id"],
        contract_version=status["contract_version"],
        decision_question=fixture["question"],
        # The two frame values the Evidence Room fixture does not display.
        time_horizon_days=42,
        brand_context=None,
        known_assumptions=(),
        change_my_mind_if=(),
        research_role_id=status["research_role_id"],
        research_role_version=status["research_role_version"],
        output_mode="internal_working_paper",
    )
    assert investigations.investigation_id_for_frame(frame) == fixture["investigationId"]
    assert investigations.investigation_id_for_frame(frame) == status["investigation_id"]


@pytest.mark.parametrize("name", _names("dossiers"))
def test_a_stored_general_dossier_recomputes_its_version_identity_and_scope(name):
    entry = _entry("dossiers", name)
    body = entry["body"].encode("utf-8")
    record = dossier_store.parse_dossier_body(body)
    assert dossier_store.dossier_version(body) == entry["dossier_version"]
    assert dossier_store.dossier_version_for_record(record) == entry["dossier_version"]
    assert dossier_store._canonical(record) == body
    assert dossier_store.scope_digest_for_record(record) == entry["scope_digest"]
    frame = investigations.investigation_frame_from_payload(record["frame"])
    assert investigations.investigation_id_for_frame(frame) == record["investigation_id"]
    assert workspace_scope.ScopeBinding.from_frame(frame).scope_digest == entry["scope_digest"]


@pytest.mark.parametrize("name", _names("artifacts"))
def test_a_stored_general_artifact_recomputes_its_export_bytes_and_ids(monkeypatch, name):
    dossier_pdf_fakes.install_font(monkeypatch)
    entry = _entry("artifacts", name)
    inputs = entry["inputs"]
    html = dossier_artifacts.render_html(inputs["client_read"], title=inputs["title"])
    assert html == entry["record"]["html"]
    assert sha256(html.encode("utf-8")) == entry["record"]["manifest"]["export_digests"]["html"]
    built = dossier_artifacts.build_artifact(
        investigation_id=inputs["investigation_id"],
        scope_digest=inputs["scope_digest"],
        dossier_version=inputs["dossier_version"],
        selected_claim_ids=inputs["selected_claim_ids"],
        client_read=inputs["client_read"],
        format="html",
        pdf_digest=inputs["pdf_digest"],
        title=inputs["title"],
    )
    assert ordered_bytes(built) == ordered_bytes(entry["record"])
    assert built["artifact_id"] == entry["artifact_id"]
    assert built["artifact_version"] == entry["artifact_version"]
    assert ordered_bytes(dossier_artifacts.parse_artifact(entry["record"])) == ordered_bytes(
        entry["record"]
    )


def test_a_stored_general_artifact_with_its_pdf_recomputes_through_the_export_path(monkeypatch):
    dossier_pdf_fakes.install_font(monkeypatch)
    entry = retained()["export"]
    inputs = entry["inputs"]
    renderer_calls = []

    def render(html_bytes):
        renderer_calls.append(html_bytes)
        return dossier_pdf_fakes.synthetic_pdf(dossier_pdf_fakes.page_text(html_bytes))

    built = dossier_artifacts.build_artifact_exports(
        investigation_id=inputs["investigation_id"],
        scope_digest=inputs["scope_digest"],
        dossier_version=inputs["dossier_version"],
        selected_claim_ids=inputs["selected_claim_ids"],
        client_read=inputs["client_read"],
        purpose_policy=None,
        render_pdf=render,
        title=inputs["title"],
    )
    assert renderer_calls == [entry["html"].encode("utf-8")]
    assert sha256(built["pdf"]) == entry["pdf_sha256"]
    assert ordered_bytes(built["artifact"]["manifest"]) == ordered_bytes(entry["manifest"])
    assert built["artifact"]["artifact_id"] == entry["artifact_id"]


def replay_journey(ws) -> dict:
    """The handler journey of test_the_complete_review_journey_exports_exactly_the_approved_bytes."""
    selected = select(ws, ws.version, ["clm_1"])
    assert selected.status_code == 200, selected.text
    version = selected.json()["dossier_version"]
    approve_claim(ws, version, "clm_1", RECEIPTS[0])
    prepared = prepare(ws, version)
    assert prepared.status_code == 200, prepared.text
    result = prepared.json()
    approver, approver_ready = reviewer_for(ws, "client")
    approved = approver.post(
        ws.review, json=artifact_command(ws, version, result), headers=approver_ready
    )
    assert approved.status_code == 200, approved.text
    html = export(ws, result["artifact_id"], "html", result["artifact_version"])
    pdf = export(ws, result["artifact_id"], "pdf", result["artifact_version"])
    assert (html.status_code, pdf.status_code) == (200, 200)
    artifact = stored_artifact(ws, result["artifact_id"])
    return {
        "investigation_id": ws.scope.investigation_id,
        "published_version": ws.version,
        "published_body_sha256": sha256(dossier_store._canonical(ws.dossier)),
        "selected_version": version,
        "selected_record_sha256": sha256(dossier_store._canonical(stored_record(ws, version))),
        "pointer_scope_digest": current_pointer(ws)["scope_digest"],
        "artifact_id": result["artifact_id"],
        "artifact_version": result["artifact_version"],
        "manifest": artifact["manifest"],
        "html": html.content.decode("utf-8"),
        "pdf_sha256": sha256(pdf.content),
        "stored_pdf_sha256": sha256(stored_pdf(ws, result["artifact_id"])),
    }


def test_the_general_review_journey_recomputes_every_stored_version_id_and_export_byte(
    workspace, renderer
):
    replayed = replay_journey(workspace)
    expected = retained()["journey"]
    assert ordered_bytes(replayed) == ordered_bytes(expected)
    frame = investigations.investigation_frame_from_payload(workspace.dossier["frame"])
    assert (
        workspace_scope.ScopeBinding.from_frame(frame).scope_digest
        == expected["pointer_scope_digest"]
    )
