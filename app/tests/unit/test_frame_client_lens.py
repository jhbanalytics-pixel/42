"""The client lens on the investigation frame, bound through dossier, artifact and export.

A frame names either no lens (general 42, the spelling every stored object
already has) or exactly one lens binding envelope. A named lens joins the
investigation id and the scope digest; general 42 keeps both unchanged, which
test_frame_lens_general_identity.py proves byte for byte. The creation route
resolves the lens under the resolved scope before any storage read and refuses
a lens outside that scope, an unknown lens and a disabled lens, with no
fallback to general 42 or to anything else. The Client Read HTML and the PDF
made from it name the lens as the Ask export does: by its registry label while
the registry holds it enabled for the scope at the same configuration, and
otherwise by its stored identity.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import yaml
from pypdf import PdfReader

from src.api import (
    ask_export,
    client_lenses,
    dossier_artifacts,
    dossier_store,
    investigation_scopes,
    investigations,
    main,
    workspace_scope,
)
from tests.unit import dossier_pdf_fakes
from tests.unit.test_dossier_resolver import RECEIPTS, dossier
from tests.unit.test_dossier_store import PREFIX, body_of, publication
from tests.unit.test_frame_lens_general_identity import retained
from tests.unit.test_investigation_api import Bucket as ApiBucket
from tests.unit.test_investigation_api import request_payload, set_foundation
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
    stored_record,
    workspace,
    workspace_fixture,
)
from tests.unit.test_main_dossier_review_routes import Bucket as ReviewBucket
from tests.unit.test_main_dossier_review_routes import client, detail

_JOURNEY_FIXTURES = (
    _SHARED_FIXTURES,
    renderer,
    review_environment,
    review_environment_fixture,
    sessions,
    workspace,
    workspace_fixture,
)
_REAL_RESOLVE = workspace_scope.resolve_workspace_scope

BSA_LENS = "bsa_pulse_lens"
BSA_DIGEST = "e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf"
BSA_LABEL = "Brand South Africa Pulse"
ENVELOPE = {
    "lens_binding_version": "client_lens_binding_v1",
    "client_lens_id": BSA_LENS,
    "configuration_digest": BSA_DIGEST,
}
QUESTION = "Which emerging behaviour should the brand act on in six weeks?"


def frame(**overrides):
    values = {
        "client_scope_id": "bsa_pulse",
        "market_scope": ("za", "ng", "ke"),
        "brand_config_id": "bsa",
        "audience_lens_ids": (),
        "theme_id": None,
        "run_id": "investigation_run_001",
        "contract_version": "2.1.0",
        "decision_question": QUESTION,
        "time_horizon_days": 42,
        "brand_context": None,
        "known_assumptions": (),
        "change_my_mind_if": (),
        "research_role_id": None,
        "research_role_version": None,
        "output_mode": "internal_working_paper",
    }
    values.update(overrides)
    return investigations.InvestigationFrame(**values)


def lensed(**overrides):
    return frame(client_lens=dict(ENVELOPE), **overrides)


def storage_record(value):
    response = investigations.build_plan_ready(
        frame=value,
        plan=investigations.default_plan_for_frame(value),
        investigation_id=investigations.investigation_id_for_frame(value),
        created_at=datetime(2026, 9, 25, 9, 0, tzinfo=UTC),
        active_role_versions={},
    )
    return investigations.build_investigation_storage_record(value, response)


def _registry(tmp_path, monkeypatch, mutate):
    document = yaml.safe_load(client_lenses.CLIENT_LENSES_PATH.read_text(encoding="utf-8"))
    mutate(document)
    path = tmp_path / "client_lenses.yaml"
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr(client_lenses, "CLIENT_LENSES_PATH", path)
    monkeypatch.setattr(
        client_lenses, "CONFIG_DIGEST", hashlib.sha256(path.read_bytes()).hexdigest()
    )
    return path


def disable_bsa(tmp_path, monkeypatch):
    return _registry(
        tmp_path, monkeypatch, lambda doc: doc["lenses"][BSA_LENS].update(enabled=False)
    )


# The frame


def test_a_named_lens_is_stored_on_the_frame_as_its_exact_envelope():
    value = lensed()
    assert dict(value.client_lens) == ENVELOPE
    with pytest.raises(TypeError):
        value.client_lens["client_lens_id"] = "other"
    assert frame().client_lens is None


def test_a_named_lens_changes_the_id_and_the_same_lens_and_digest_reproduce_it():
    general = investigations.investigation_id_for_frame(frame())
    bsa = investigations.investigation_id_for_frame(lensed())
    assert bsa != general
    assert investigations.investigation_id_for_frame(lensed()) == bsa
    repinned = frame(client_lens={**ENVELOPE, "configuration_digest": "0" * 64})
    assert investigations.investigation_id_for_frame(repinned) not in {general, bsa}
    other = frame(client_lens={**ENVELOPE, "client_lens_id": "another_lens"})
    assert investigations.investigation_id_for_frame(other) not in {general, bsa}


def test_the_lensed_id_hashes_the_general_payload_plus_the_one_lens_key():
    value = lensed()
    payload = investigations.investigation_frame_payload(frame())
    payload = {
        key: tuple(item) if isinstance(item, list) else item for key, item in payload.items()
    }
    payload["client_lens"] = ENVELOPE
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert investigations.investigation_id_for_frame(value) == (
        "inv_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )


def test_the_payload_carries_the_lens_key_only_when_a_lens_is_named():
    general = investigations.investigation_frame_payload(frame())
    assert "client_lens" not in general
    payload = investigations.investigation_frame_payload(lensed())
    assert list(payload)[:-1] == list(general)
    assert payload["client_lens"] == ENVELOPE
    assert type(payload["client_lens"]) is dict
    assert investigations.investigation_frame_from_payload(payload) == lensed()
    assert investigations.investigation_frame_from_payload(general) == frame()


@pytest.mark.parametrize(
    "lens",
    [
        None,
        {},
        "bsa_pulse_lens",
        [ENVELOPE],
        {key: ENVELOPE[key] for key in ("lens_binding_version", "client_lens_id")},
        {**ENVELOPE, "label": BSA_LABEL},
        {**ENVELOPE, "lens_binding_version": "client_lens_binding_v2"},
        {**ENVELOPE, "client_lens_id": ""},
        {**ENVELOPE, "client_lens_id": None},
        {**ENVELOPE, "client_lens_id": 7},
        {**ENVELOPE, "configuration_digest": BSA_DIGEST.upper()},
        {**ENVELOPE, "configuration_digest": BSA_DIGEST[:63]},
        {**ENVELOPE, "configuration_digest": None},
    ],
)
def test_a_stored_frame_lens_other_than_one_exact_envelope_is_refused(lens):
    payload = investigations.investigation_frame_payload(frame())
    payload["client_lens"] = lens
    with pytest.raises(ValueError, match="stored investigation frame is invalid"):
        investigations.investigation_frame_from_payload(payload)


@pytest.mark.parametrize(
    "lens",
    [
        {},
        {**ENVELOPE, "label": BSA_LABEL},
        {**ENVELOPE, "lens_binding_version": "client_lens_binding_v2"},
        {**ENVELOPE, "client_lens_id": ""},
        {**ENVELOPE, "configuration_digest": "A" * 64},
        "bsa_pulse_lens",
    ],
)
def test_a_frame_lens_other_than_one_exact_envelope_cannot_be_built(lens):
    with pytest.raises(ValueError, match="client lens"):
        frame(client_lens=lens)


def test_a_stored_lensed_record_round_trips_and_its_response_carries_the_lens():
    record = storage_record(lensed())
    assert record["frame"]["client_lens"] == ENVELOPE
    assert record["response"]["client_lens"] == ENVELOPE
    read_frame, response = investigations.validate_investigation_storage_record(
        json.loads(json.dumps(record))
    )
    assert read_frame == lensed()
    assert response["client_lens"] == ENVELOPE
    assert "client_lens" not in storage_record(frame())["response"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda record: record["response"].pop("client_lens"),
        lambda record: record["response"].__setitem__("client_lens", None),
        lambda record: record["response"].__setitem__(
            "client_lens", {**ENVELOPE, "configuration_digest": "0" * 64}
        ),
        lambda record: record["frame"].pop("client_lens"),
    ],
)
def test_a_stored_response_whose_lens_disagrees_with_its_frame_is_refused(mutate):
    record = json.loads(json.dumps(storage_record(lensed())))
    mutate(record)
    with pytest.raises(ValueError):
        investigations.validate_investigation_storage_record(record)


def test_a_general_response_that_names_a_lens_is_refused():
    record = json.loads(json.dumps(storage_record(frame())))
    record["response"]["client_lens"] = ENVELOPE
    with pytest.raises(ValueError):
        investigations.validate_investigation_storage_record(record)


# The scope binding


def test_the_frame_binding_folds_the_lens_into_the_scope_digest_only_when_named():
    general = workspace_scope.ScopeBinding.from_frame(frame())
    bsa = workspace_scope.ScopeBinding.from_frame(lensed())
    assert general.client_lens.is_general
    assert bsa.client_lens.values == ENVELOPE
    assert bsa.scope_digest != general.scope_digest
    assert bsa.scope_digest == workspace_scope.ScopeBinding.from_values(
        general.values, client_lens=ENVELOPE
    ).scope_digest
    assert general.scope_digest == dossier_store.canonical_digest(general.values)


def test_a_stored_lensed_investigation_resolves_under_its_scope_with_the_lensed_digest(
    monkeypatch,
):
    from tests.unit.test_workspace_scope import Bucket as ScopeBucket

    record = storage_record(lensed())
    investigation_id = record["response"]["investigation_id"]
    name = f"{PREFIX}investigations/{investigation_id}.json"
    bucket = ScopeBucket({name: json.dumps(record).encode()})
    monkeypatch.setattr(workspace_scope, "_workspace_bucket", lambda: bucket)
    # The journey fixtures stand in for the resolver; this reads through the real one.
    resolved = _REAL_RESOLVE(investigation_id, client_scope_id="bsa_pulse")
    assert resolved.frame == lensed()
    assert resolved.scope_digest == workspace_scope.ScopeBinding.from_frame(lensed()).scope_digest
    assert resolved.binding.client_lens.values == ENVELOPE
    with pytest.raises(workspace_scope.WorkspaceScopeError) as refused:
        _REAL_RESOLVE(investigation_id, client_scope_id="ogilvy_default")
    assert refused.value.code == "scope_invalid"


# Creation


def bsa_request(**overrides):
    return request_payload(client_scope_id="bsa_pulse", brand_config_id="bsa", **overrides)


def test_the_bsa_lens_under_the_bsa_scope_creates_one_lensed_investigation(monkeypatch):
    bucket = ApiBucket()
    set_foundation(monkeypatch, bucket)
    created = client().post("/api/v2/investigations", json=bsa_request(client_lens_id=BSA_LENS))
    assert created.status_code == 200, created.text
    data = created.json()
    assert data["client_lens"] == ENVELOPE
    assert data["investigation_id"] == investigations.investigation_id_for_frame(lensed())
    stored = json.loads(bucket.objects[f"{PREFIX}investigations/{data['investigation_id']}.json"])
    assert stored["frame"]["client_lens"] == ENVELOPE
    again = client().post("/api/v2/investigations", json=bsa_request(client_lens_id=BSA_LENS))
    assert again.json() == data
    assert bucket.upload_count == 1
    general = client().post("/api/v2/investigations", json=bsa_request())
    assert general.status_code == 200
    assert "client_lens" not in general.json()
    assert general.json()["investigation_id"] == investigations.investigation_id_for_frame(frame())
    assert general.json()["investigation_id"] != data["investigation_id"]
    named_none = client().post("/api/v2/investigations", json=bsa_request(client_lens_id=None))
    assert named_none.json() == general.json()
    assert bucket.upload_count == 2


def refused_before_storage(monkeypatch, body):
    reads = []
    set_foundation(monkeypatch, ApiBucket())
    monkeypatch.setattr(main, "_investigation_bucket", lambda: reads.append(True))
    response = client().post("/api/v2/investigations", json=body)
    assert reads == []
    return response


@pytest.mark.parametrize(
    "body",
    [
        # Another client's lens: the default scope is not the lens's scope.
        request_payload(client_lens_id=BSA_LENS),
        request_payload(brand_config_id=None, client_lens_id=BSA_LENS),
        # A lens the registry does not hold.
        bsa_request(client_lens_id="unknown_lens"),
        bsa_request(client_lens_id=""),
    ],
)
def test_a_lens_outside_the_scope_or_unknown_is_refused_before_storage(monkeypatch, body):
    refused = refused_before_storage(monkeypatch, body)
    assert (refused.status_code, detail(refused)["code"]) == (404, "scope_invalid")


def test_a_disabled_lens_is_refused_before_storage_and_nothing_falls_back(
    tmp_path, monkeypatch
):
    disable_bsa(tmp_path, monkeypatch)
    refused = refused_before_storage(monkeypatch, bsa_request(client_lens_id=BSA_LENS))
    assert (refused.status_code, detail(refused)["code"]) == (404, "scope_invalid")
    assert "client_lens" not in refused.text


def test_a_lens_id_that_is_not_a_string_is_refused(monkeypatch):
    refused = refused_before_storage(monkeypatch, bsa_request(client_lens_id=7))
    assert refused.status_code in (400, 422)


# Dossier


def lensed_dossier():
    value = dossier()
    value["investigation_id"] = investigations.investigation_id_for_frame(lensed())
    value["frame"] = investigations.investigation_frame_payload(lensed())
    value["scope"] = {field: value["frame"][field] for field in dossier_store.SCOPE_FIELDS}
    return value


def test_a_lensed_dossier_validates_and_its_scope_digest_carries_the_lens():
    value = lensed_dossier()
    record = dossier_store.parse_dossier_body(body_of(value))
    assert record["frame"]["client_lens"] == ENVELOPE
    assert dossier_store.scope_digest_for_record(record) == (
        workspace_scope.ScopeBinding.from_frame(lensed()).scope_digest
    )
    general = dossier()
    general_frame = investigations.investigation_frame_from_payload(general["frame"])
    assert dossier_store.scope_digest_for_record(general) == (
        workspace_scope.ScopeBinding.from_frame(general_frame).scope_digest
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda frame_payload: frame_payload.pop("client_lens"),
        lambda frame_payload: frame_payload["client_lens"].__setitem__(
            "configuration_digest", "0" * 64
        ),
    ],
)
def test_a_dossier_whose_frame_lens_was_altered_fails_its_identity(change):
    value = lensed_dossier()
    change(value["frame"])
    with pytest.raises(dossier_store.DossierRecordInvalid) as refused:
        dossier_store.parse_dossier_body(body_of(value))
    assert refused.value.code == "dossier_identity_mismatch"


def test_a_general_dossier_that_gains_a_lens_fails_its_identity():
    value = dossier()
    value["frame"]["client_lens"] = dict(ENVELOPE)
    with pytest.raises(dossier_store.DossierRecordInvalid) as refused:
        dossier_store.parse_dossier_body(body_of(value))
    assert refused.value.code == "dossier_identity_mismatch"


# Artifact approval


def test_an_approval_under_one_lens_never_approves_the_other_binding(monkeypatch):
    dossier_pdf_fakes.install_font(monkeypatch)
    entry = next(item for item in retained()["artifacts"] if item["name"] == "bsa_pulse_scope_no_lens")
    general_manifest = entry["record"]["manifest"]
    lensed_scope = workspace_scope.ScopeBinding.from_values(
        workspace_scope.ScopeBinding.from_frame(frame(run_id="journey_other_scope")).values,
        client_lens=ENVELOPE,
    ).scope_digest
    assert lensed_scope != general_manifest["scope_digest"]
    lensed_manifest = {**general_manifest, "scope_digest": lensed_scope}
    for approved, other in ((general_manifest, lensed_manifest), (lensed_manifest, general_manifest)):
        decision = {**approved, "state": "approved"}
        assert dossier_artifacts.is_artifact_approved(manifest=approved, decision=decision)
        assert not dossier_artifacts.is_artifact_approved(manifest=other, decision=decision)


# The Client Read export


def client_read_of(value):
    from src.api import dossier_resolver, intelligence_dossier

    return intelligence_dossier.read_dossier(
        value["investigation_id"], dossier_resolver.projection_record(value, [])
    )


def rendered(client_scope_id="bsa_pulse", lens=ENVELOPE):
    return dossier_artifacts.render_html(
        client_read_of(lensed_dossier()),
        title=QUESTION,
        client_lens=lens,
        client_scope_id=client_scope_id,
    )


LENS_LINE = "Client lens: " + BSA_LABEL
STORED_LINE = "Client lens: " + BSA_LENS
CONFIGURATION_LINE = f"Client lens configuration: {BSA_LENS}, {BSA_DIGEST}"


def test_the_client_read_names_an_enabled_lens_in_scope_in_the_ask_export_words(monkeypatch):
    dossier_pdf_fakes.install_font(monkeypatch)
    lines = dossier_pdf_fakes.page_text(rendered().encode("utf-8"))
    assert lines[:3] == ["Client Read", QUESTION, LENS_LINE]
    assert lines[-1] == CONFIGURATION_LINE
    assert ask_export._lens_lines({"client_lens": ENVELOPE}, "bsa_pulse") == (
        LENS_LINE,
        CONFIGURATION_LINE,
    )
    assert ask_export.GENERAL_LENS_LINE not in lines


def test_the_client_read_names_a_lens_outside_its_scope_by_its_stored_identity(monkeypatch):
    dossier_pdf_fakes.install_font(monkeypatch)
    lines = dossier_pdf_fakes.page_text(rendered(client_scope_id="ogilvy_default").encode("utf-8"))
    assert STORED_LINE in lines and CONFIGURATION_LINE in lines
    assert LENS_LINE not in lines and ask_export.GENERAL_LENS_LINE not in lines


def test_the_client_read_names_a_disabled_lens_by_its_stored_identity(tmp_path, monkeypatch):
    dossier_pdf_fakes.install_font(monkeypatch)
    disable_bsa(tmp_path, monkeypatch)
    lines = dossier_pdf_fakes.page_text(rendered().encode("utf-8"))
    assert STORED_LINE in lines and CONFIGURATION_LINE in lines
    assert LENS_LINE not in lines and ask_export.GENERAL_LENS_LINE not in lines


def test_the_client_read_names_nothing_for_general_42(monkeypatch):
    dossier_pdf_fakes.install_font(monkeypatch)
    value = dossier()
    html = dossier_artifacts.render_html(
        client_read_of(value), title=QUESTION, client_lens=None, client_scope_id="ogilvy_default"
    )
    assert html == dossier_artifacts.render_html(client_read_of(value), title=QUESTION)
    assert "Client lens" not in html


# The handler journey under the lens


@pytest.fixture
def lensed_workspace(monkeypatch, workspace):
    """A BSA lensed investigation read through the real scope resolver under bsa_pulse."""
    bucket = ReviewBucket()
    value = storage_record(lensed())
    investigation_id = value["response"]["investigation_id"]
    bucket.replace(
        f"{PREFIX}investigations/{investigation_id}.json",
        json.dumps(value).encode("utf-8"),
    )
    record = lensed_dossier()
    created = dossier_store.create_dossier(
        bucket=bucket,
        prefix=PREFIX,
        body=body_of(record),
        publication=publication(),
        pointer_writer=lambda pointer: dossier_store.publish_pointer(
            pointer, bucket=bucket, prefix=PREFIX
        ),
    )
    monkeypatch.setattr(main, "_investigation_bucket", lambda: bucket)
    monkeypatch.setattr(workspace_scope, "_workspace_bucket", lambda: bucket)
    monkeypatch.setattr(workspace_scope, "resolve_workspace_scope", _REAL_RESOLVE)
    monkeypatch.setattr(investigation_scopes, "default_client_scope_id", lambda path=None: "bsa_pulse")
    scope = _REAL_RESOLVE(investigation_id)
    return SimpleNamespace(
        bucket=bucket,
        dossier=record,
        scope=scope,
        version=created["publication"]["dossier_version"],
        baseline=list(bucket.uploads),
        review=f"/api/internal/v2/investigations/{investigation_id}/dossier/review",
    )


def pdf_text(data):
    reader = PdfReader(io.BytesIO(data), strict=True)
    return " ".join(" ".join(page.extract_text().split()) for page in reader.pages)


def test_a_lensed_investigation_is_reviewed_approved_and_exported_with_its_lens_bound(
    lensed_workspace, renderer
):
    ws = lensed_workspace
    lensed_digest = workspace_scope.ScopeBinding.from_frame(lensed()).scope_digest
    assert ws.scope.scope_digest == lensed_digest
    assert current_pointer(ws)["scope_digest"] == lensed_digest
    selected = select(ws, ws.version, ["clm_1"])
    assert selected.status_code == 200, selected.text
    version = selected.json()["dossier_version"]
    assert stored_record(ws, version)["frame"]["client_lens"] == ENVELOPE
    assert current_pointer(ws)["scope_digest"] == lensed_digest
    approve_claim(ws, version, "clm_1", RECEIPTS[0])
    prepared = prepare(ws, version)
    assert prepared.status_code == 200, prepared.text
    result = prepared.json()
    artifact = stored_artifact(ws, result["artifact_id"])
    assert artifact["manifest"]["scope_digest"] == lensed_digest
    approver, approver_ready = reviewer_for(ws, "client")
    approved = approver.post(ws.review, json=artifact_command(ws, version, result), headers=approver_ready)
    assert approved.status_code == 200, approved.text
    html = export(ws, result["artifact_id"], "html", result["artifact_version"])
    pdf = export(ws, result["artifact_id"], "pdf", result["artifact_version"])
    assert (html.status_code, pdf.status_code) == (200, 200)
    assert html.content == artifact["html"].encode("utf-8")
    lines = dossier_pdf_fakes.page_text(html.content)
    assert LENS_LINE in lines and CONFIGURATION_LINE in lines
    assert LENS_LINE in pdf_text(pdf.content)
    assert CONFIGURATION_LINE in pdf_text(pdf.content)
    assert ask_export.GENERAL_LENS_LINE not in pdf_text(pdf.content)


def test_a_lensed_artifact_is_not_readable_under_another_scope(lensed_workspace, renderer, monkeypatch):
    ws = lensed_workspace
    result = prepare(ws, ws.version).json()
    monkeypatch.setattr(
        investigation_scopes, "default_client_scope_id", lambda path=None: "ogilvy_default"
    )
    uploads = list(ws.bucket.uploads)
    for extension in ("html", "pdf"):
        refused = export(ws, result["artifact_id"], extension, result["artifact_version"])
        assert (refused.status_code, detail(refused)["code"]) == (404, "scope_invalid")
        assert BSA_LENS not in refused.text
    assert ws.bucket.uploads == uploads


def test_a_general_artifact_scope_never_matches_its_lensed_twin(lensed_workspace, renderer):
    ws = lensed_workspace
    general = replace(ws.scope.frame, client_lens=None)
    assert workspace_scope.ScopeBinding.from_frame(general).scope_digest != ws.scope.scope_digest
