"""Exact-version artifact approval: a decision approves one manifest and nothing near it."""

from __future__ import annotations

import pytest

from src.api.dossier_artifacts import is_artifact_approved


def binding(**overrides):
    value = dict(
        investigation_id="inv_fixture",
        scope_digest="a" * 64,
        dossier_version="b" * 64,
        artifact_version="c" * 64,
        selection_digest="d" * 64,
        export_digests={"html": "e" * 64, "pdf": "f" * 64},
    )
    value.update(overrides)
    return value


def test_approval_cannot_move_to_a_changed_selection():
    binding = dict(
        investigation_id="inv_fixture",
        scope_digest="a" * 64,
        dossier_version="b" * 64,
        artifact_version="c" * 64,
        selection_digest="d" * 64,
        export_digests={"html": "e" * 64, "pdf": "f" * 64},
    )
    approval = dict(binding, state="approved")
    assert is_artifact_approved(manifest=binding, decision=approval)
    assert not is_artifact_approved(
        manifest=dict(binding, selection_digest="0" * 64), decision=approval
    )


@pytest.mark.parametrize(
    "change",
    [
        {"investigation_id": "inv_other"},
        {"scope_digest": "0" * 64},
        {"dossier_version": "0" * 64},
        {"artifact_version": "0" * 64},
        {"selection_digest": "0" * 64},
        {"export_digests": {"html": "0" * 64, "pdf": "f" * 64}},
        {"export_digests": {"html": "e" * 64, "pdf": "0" * 64}},
        {"export_digests": {"html": "e" * 64}},
        {"export_digests": {"html": "e" * 64, "pdf": "f" * 64, "docx": "1" * 64}},
        {"export_digests": {}},
    ],
    ids=[
        "investigation",
        "scope",
        "dossier_version",
        "artifact_version",
        "selection",
        "html_digest",
        "pdf_digest",
        "missing_format",
        "extra_format",
        "no_exports",
    ],
)
def test_each_manifest_field_must_match_the_decision_exactly(change):
    approval = dict(binding(), state="approved")
    assert not is_artifact_approved(manifest=binding(**change), decision=approval)
    assert not is_artifact_approved(
        manifest=binding(), decision=dict(approval, **change)
    )


@pytest.mark.parametrize(
    "state", ["pending_review", "rejected", "draft", "APPROVED", "", None]
)
def test_only_an_approved_decision_state_approves(state):
    decision = dict(binding(), state=state)
    assert not is_artifact_approved(manifest=binding(), decision=decision)
    assert not is_artifact_approved(manifest=binding(), decision=binding())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("export_digests"),
        lambda value: value.pop("selection_digest"),
        lambda value: value.update(dossier_version="B" * 64),
        lambda value: value.update(artifact_version="c" * 63),
        lambda value: value.update(scope_digest=None),
        lambda value: value.update(investigation_id=""),
        lambda value: value.update(
            export_digests={"html": "not a digest", "pdf": "f" * 64}
        ),
        lambda value: value.update(export_digests=["e" * 64]),
    ],
    ids=[
        "no_exports",
        "no_selection",
        "uppercase_version",
        "short_version",
        "null_scope",
        "blank_investigation",
        "bad_export_digest",
        "exports_not_a_map",
    ],
)
def test_a_malformed_manifest_never_approves_even_when_the_decision_matches_it(mutate):
    manifest = binding()
    mutate(manifest)
    decision = dict(manifest, state="approved")
    assert not is_artifact_approved(manifest=manifest, decision=decision)


def test_non_records_are_refused_rather_than_compared():
    assert not is_artifact_approved(
        manifest=None, decision=dict(binding(), state="approved")
    )
    assert not is_artifact_approved(manifest=binding(), decision=None)
    assert not is_artifact_approved(manifest=[], decision=[])
    with pytest.raises(TypeError):
        is_artifact_approved(binding(), dict(binding(), state="approved"))


def test_extra_decision_fields_do_not_disturb_the_comparison():
    decision = dict(
        binding(),
        state="approved",
        decision_id="dec_1",
        principal_ref="human:0a1b2c3d",
        recorded_at="2026-09-12T00:00:00Z",
    )
    assert is_artifact_approved(manifest=binding(), decision=decision)
    assert not is_artifact_approved(
        manifest=dict(binding(), extra="x"), decision=decision
    )
