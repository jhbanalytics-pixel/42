import copy
import hashlib
import json

import pytest

from src.api import client_purpose, dossier_artifacts, narrative_report
from tests.unit import dossier_pdf_fakes


def client_read():
    return {
        "contract_version": "intelligence_dossier_v1",
        "investigation_id": "inv_fixture",
        "concise_answer": "An admitted answer.",
        "claims": [
            {
                "claim_id": "clm_1",
                "kind": "observation",
                "text": "An admitted finding.",
                "citations": ["ev_1"],
            }
        ],
        "evidence": [{"evidence_id": "ev_1", "published_at": "2026-09-01T00:00:00Z"}],
        "excluded_count": 0,
        "excluded_reasons": [],
        "artifact_readiness": {"state": "client_ready", "failed": []},
    }


def build_v2(**overrides):
    arguments = {
        "investigation_id": "inv_fixture",
        "scope_digest": "a" * 64,
        "dossier_version": "b" * 64,
        "selected_claim_ids": ["clm_1"],
        "client_read": client_read(),
        "format": "html",
        "content_kind": "narrative_report",
        "preset": narrative_report.general_preset("ogilvy_default"),
    }
    arguments.update(overrides)
    return dossier_artifacts.build_artifact(**arguments)


def test_v2_roundtrip_has_exact_record_manifest_and_export_digest_shapes():
    artifact = build_v2()

    assert tuple(artifact) == dossier_artifacts.ARTIFACT_V2_FIELDS
    assert artifact["contract_version"] == "dossier_artifact_v2"
    assert artifact["content_kind"] == "narrative_report"
    assert tuple(artifact["manifest"]) == dossier_artifacts.BINDING_FIELDS
    assert set(artifact["manifest"]["export_digests"]) == {
        "html",
        "client_read",
        "preset",
        "report",
    }
    assert (
        artifact["manifest"]["export_digests"]["client_read"]
        == hashlib.sha256(
            dossier_artifacts.record_bytes(artifact["client_read"])
        ).hexdigest()
    )
    assert artifact["manifest"]["export_digests"][
        "preset"
    ] == narrative_report.canonical_digest(artifact["preset"])
    assert artifact["manifest"]["export_digests"][
        "report"
    ] == narrative_report.canonical_digest(artifact["report"])
    assert dossier_artifacts.parse_artifact(artifact) == artifact


def test_v2_refuses_raw_projection_and_incomplete_selected_claims():
    raw = client_read() | {"decision": "approved"}
    with pytest.raises(dossier_artifacts.ArtifactInvalid):
        build_v2(client_read=raw)
    with pytest.raises(dossier_artifacts.ArtifactInvalid, match="selection"):
        build_v2(selected_claim_ids=[])


@pytest.mark.parametrize("selection", [None, "clm_1", [1]])
def test_v2_refuses_malformed_selection_with_the_artifact_error(selection):
    with pytest.raises(dossier_artifacts.ArtifactInvalid, match="selection"):
        build_v2(selected_claim_ids=selection)


def test_v2_refuses_extra_export_digest_and_self_consistent_report_or_html_tampering():
    artifact = build_v2()
    extra = copy.deepcopy(artifact)
    extra["manifest"]["export_digests"]["pdf"] = "0" * 64
    with pytest.raises(dossier_artifacts.ArtifactInvalid):
        dossier_artifacts.parse_artifact(extra)

    report = copy.deepcopy(artifact)
    report["report"]["concise_answer"] = "A substituted answer."
    report["manifest"]["export_digests"]["report"] = narrative_report.canonical_digest(
        report["report"]
    )
    _reseal(report)
    with pytest.raises(dossier_artifacts.ArtifactInvalid):
        dossier_artifacts.parse_artifact(report)

    changed_html = copy.deepcopy(artifact)
    changed_html["html"] += "<p>substituted</p>"
    changed_html["manifest"]["export_digests"]["html"] = hashlib.sha256(
        changed_html["html"].encode("utf-8")
    ).hexdigest()
    _reseal(changed_html)
    with pytest.raises(dossier_artifacts.ArtifactInvalid):
        dossier_artifacts.parse_artifact(changed_html)


def test_v2_nested_non_text_digest_is_a_bounded_artifact_error():
    artifact = build_v2()
    artifact["report"]["scope_digest"] = 7

    with pytest.raises(dossier_artifacts.ArtifactInvalid):
        dossier_artifacts.parse_artifact(artifact)


@pytest.mark.parametrize(
    ("path", "invalid"),
    [
        (("report", "sections", 0, "state"), []),
        (("report", "sections", 0, "state"), {}),
        (("report", "sections", 1, "items", 0, "kind"), []),
        (("report", "sections", 1, "items", 0, "kind"), {}),
        (("format",), []),
        (("format",), {}),
        (("client_read", "excluded_reasons"), None),
    ],
)
def test_v2_malformed_structural_values_are_bounded_artifact_errors(path, invalid):
    artifact = build_v2()
    target = artifact
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = invalid
    with pytest.raises(dossier_artifacts.ArtifactInvalid):
        dossier_artifacts.parse_artifact(artifact)


def _reseal(artifact):
    identity = {
        "investigation_id": artifact["investigation_id"],
        "scope_digest": artifact["manifest"]["scope_digest"],
        "dossier_version": artifact["dossier_version"],
        "selection_digest": artifact["manifest"]["selection_digest"],
        "export_digests": artifact["manifest"]["export_digests"],
        "format": artifact["format"],
    }
    version = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    artifact["manifest"]["artifact_version"] = version
    artifact["artifact_version"] = version
    artifact["artifact_id"] = "art_" + version[:16]


def test_changed_preset_produces_a_new_exact_review_identity():
    original = build_v2()
    changed = build_v2(preset=narrative_report.general_preset("other_scope"))
    decision = dict(original["manifest"], state="approved")

    assert changed["artifact_version"] != original["artifact_version"]
    assert dossier_artifacts.is_artifact_approved(
        manifest=original["manifest"], decision=decision
    )
    assert not dossier_artifacts.is_artifact_approved(
        manifest=changed["manifest"], decision=decision
    )


@pytest.mark.parametrize("source", ["excluded_reasons", "failed"])
def test_v2_purpose_gate_covers_all_variable_limitations_before_html_or_digest(
    monkeypatch, source
):
    value = client_read()
    prohibited = "Election messaging reached undecided voters in Durban."
    if source == "excluded_reasons":
        value["excluded_count"] = 1
        value["excluded_reasons"] = [prohibited]
    else:
        value["artifact_readiness"] = {
            "state": "blocked",
            "failed": [prohibited],
        }
    policy = client_purpose.load_client_purpose_policy("bsa")
    monkeypatch.setattr(
        narrative_report,
        "render_html",
        lambda report: (_ for _ in ()).throw(AssertionError("rendered before gate")),
    )

    with pytest.raises(client_purpose.ClientPurposeProhibited) as caught:
        build_v2(client_read=value, purpose_policy=policy)

    assert caught.value.code == client_purpose.EXPORT_REFUSED
    assert "electoral_purpose" in caught.value.purpose_codes


def test_v1_roundtrip_and_client_read_byte_order_are_unchanged(monkeypatch):
    dossier_pdf_fakes.install_font(monkeypatch)
    value = client_read()
    artifact = dossier_artifacts.build_artifact(
        investigation_id="inv_fixture",
        scope_digest="a" * 64,
        dossier_version="b" * 64,
        selected_claim_ids=["clm_1"],
        client_read=value,
        format="html",
    )

    assert artifact["contract_version"] == "dossier_artifact_v1"
    assert tuple(artifact) == dossier_artifacts.ARTIFACT_FIELDS
    assert tuple(artifact["client_read"]) == tuple(value)
    assert dossier_artifacts.parse_artifact(artifact) == artifact
    reordered = dict(reversed(tuple(artifact.items())))
    assert dossier_artifacts.parse_artifact(reordered) == artifact
