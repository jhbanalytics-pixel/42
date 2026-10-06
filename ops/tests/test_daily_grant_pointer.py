"""The owner's render step for the daily grant and its pointer.

The grant binds the image digest, so it is rendered after the image is built, from
that digest. The render writes the four files the owner installs and prints every
digest the install and its readback compare against. Everything here is offline.
"""

import hashlib
import importlib
import io
import json
from datetime import UTC, date, datetime

import pytest

from ops.runners import daily_grant_pointer, managed_runtime
from ops.tests.test_foundation_scheduler import (
    APPROVER_HASH,
    FakeObjectClient,
    FakeReservation,
    _build,
    _config,
    _manifest,
    _readback,
    _succeeding_handlers,
)

IMAGE_DIGEST = "sha256:" + "8" * 64
VALID_FROM = date(2026, 9, 25)
FILES = ("grant.json", "grant-object.json", "proposal.md", "pointer.json")


def _engine():
    return managed_runtime.engine_daily_adapter()


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _render(tmp_path, **overrides):
    arguments = {
        "image_digest": IMAGE_DIGEST,
        "valid_from": VALID_FROM,
        "sequence": 1,
        "output_dir": tmp_path,
    }
    arguments.update(overrides)
    return daily_grant_pointer.render(**arguments)


def test_render_binds_the_built_image_and_the_active_generation(tmp_path):
    engine = _engine()
    phrases = importlib.import_module(
        "src.analysis.open_intelligence.recurring_grant_phrases"
    )
    summary = _render(tmp_path)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(FILES)
    grant_bytes = (tmp_path / "grant.json").read_bytes()
    grant = json.loads(grant_bytes)
    assert grant_bytes == phrases.canonical_grant_bytes(grant)
    assert grant == engine.recurring_grant.validate_recurring_grant(grant)
    digest = engine.recurring_grant.grant_digest(grant)
    assert phrases.grant_digest(grant) == digest
    assert grant["permitted_image_digests"] == [IMAGE_DIGEST]
    assert grant["issuing_principal"] == APPROVER_HASH
    assert (
        grant["resource_manifest_digest"]
        == engine.execution_generations.ACTIVE_GENERATION_PAIR[1]
    )
    assert (
        grant["source_policy_digest"]
        == _config()["daily_profile"]["source_policy_digest"]
    )
    assert grant["valid_from"] == "2026-09-25T00:00:00+00:00"
    assert grant["valid_until"] == "2026-10-25T00:00:00+00:00"
    assert grant["permitted_cutoffs"][0] == "2026-09-25"
    assert grant["revocation_state"] == "active"
    assert grant == engine.recurring_grant.proposed_grant(
        identities=_manifest()["identities"],
        issuing_principal=APPROVER_HASH,
        resource_manifest_digest=engine.execution_generations.ACTIVE_GENERATION_PAIR[1],
        source_policy_digest=_config()["daily_profile"]["source_policy_digest"],
        valid_from=datetime(2026, 9, 25, tzinfo=UTC),
        permitted_image_digests=[IMAGE_DIGEST],
    )
    proposal = (tmp_path / "proposal.md").read_bytes()
    assert proposal == engine.recurring_grant.render_grant_proposal(grant).encode(
        "utf-8"
    )
    proposal_sha256 = _sha256(proposal)
    assert summary["grant_digest"] == digest
    assert summary["grant_id"] == grant["grant_id"] == "recurring_execution_20260925_v1"
    assert summary["proposal_sha256"] == proposal_sha256
    assert summary["approval_phrase"] == (
        "Approve recurring execution grant " + proposal_sha256
    )
    assert summary["approval_phrase_sha256"] == phrases.approval_phrase_sha256(
        proposal_sha256
    )
    assert summary["revocation_phrase"] == (
        "Revoke recurring execution grant recurring_execution_20260925_v1"
    )
    assert summary["image_digest"] == IMAGE_DIGEST
    assert summary["bucket"] == engine.daily_store.EVIDENCE_BUCKET
    assert summary["files"] == {
        name: _sha256((tmp_path / name).read_bytes()) for name in FILES
    }
    assert summary["objects"] == {
        "grant": {
            "name": f"42/daily/grants/{digest}.json",
            "file": "grant-object.json",
            "sha256": summary["files"]["grant-object.json"],
        },
        "pointer": {
            "name": managed_runtime.grant_pointer_name(IMAGE_DIGEST, 1),
            "file": "pointer.json",
            "sha256": summary["files"]["pointer.json"],
        },
    }


def _approval_record(engine, grant, proposal_sha256):
    """The durable approval row the approve routine records for this grant, typed by
    the engine's own mirror of the read routine, which refuses a row it would not."""
    execution_approval = importlib.import_module(
        "src.analysis.open_intelligence.execution_approval"
    )
    phrases = importlib.import_module(
        "src.analysis.open_intelligence.recurring_grant_phrases"
    )
    generation = execution_approval.execution_generations.active_generation()
    digest = engine.recurring_grant.grant_digest(grant)
    approved_at = datetime(2026, 9, 25, 8, 30, tzinfo=UTC)
    return execution_approval.RecurringGrantApproval(
        approval_contract_version="open_intelligence_execution_approval_v2",
        approval_id=execution_approval.approval_id_v2(
            digest,
            APPROVER_HASH,
            approved_at,
            origin_registry_sha256=generation.origin_registry_sha256,
            resource_manifest_sha256=generation.resource_manifest_sha256,
        ),
        manifest_version="42_recurring_execution_grant_v1",
        operation="recurring_grant_v1",
        contract_sha256=proposal_sha256,
        manifest_sha256=digest,
        canonical_manifest_json=phrases.canonical_grant_bytes(grant).decode("utf-8"),
        approved_by=APPROVER_HASH,
        approved_at=approved_at,
        expires_at=datetime.fromisoformat(grant["valid_until"]),
        approval_phrase_sha256=phrases.approval_phrase_sha256(proposal_sha256),
        origin_registry_sha256=generation.origin_registry_sha256,
        resource_manifest_sha256=generation.resource_manifest_sha256,
        revocation_state="active",
        revoked_at=None,
        mode="new_consume",
        registry=generation.registry,
        expected_resource_manifest_sha256=generation.resource_manifest_sha256,
    )


def test_the_rendered_objects_pass_the_runtime_and_the_durable_reader(tmp_path):
    engine = _engine()
    summary = _render(tmp_path)
    grant = json.loads((tmp_path / "grant.json").read_bytes())
    record = _approval_record(engine, grant, summary["proposal_sha256"])
    assert record.approval_phrase_sha256 == summary["approval_phrase_sha256"]
    client = FakeObjectClient()
    for entry in summary["objects"].values():
        client.objects[entry["name"]] = ((tmp_path / entry["file"]).read_bytes(), 1)
    pins = managed_runtime.read_grant_pointer(client, IMAGE_DIGEST)
    assert pins == {
        "recurring_grant_digest": summary["grant_digest"],
        "approval_phrase_sha256": summary["approval_phrase_sha256"],
    }
    approval = {
        "approved_by": record.approved_by,
        "approved_at": record.approved_at,
        "approval_phrase_sha256": record.approval_phrase_sha256,
        "manifest_sha256": record.manifest_sha256,
    }
    loader = managed_runtime._native_grant_loader(
        engine, client, {summary["grant_digest"]: approval}.get
    )
    assert loader(pins) == grant
    # The cycle for the built image runs on these objects alone, on a cutoff the
    # grant permits, with no pin in the image.
    config = _config()
    config["jobs"]["intelligence-42-daily-staging"]["mode"] = "daily"
    repository = config["jobs"]["intelligence-42-daily-staging"]["image_repository"]
    now = datetime(2026, 9, 26, 4, 30, tzinfo=UTC)
    wiring = {
        "engine": engine,
        "profile": config["daily_profile"],
        "grant_loader": loader,
        "object_client": client,
        "stage_handlers": _succeeding_handlers([]),
        "now": lambda: now,
        "environment": "staging",
    }
    invocation = _build(
        config, readback=_readback(config, image=f"{repository}@{IMAGE_DIGEST}")
    )
    receipt = managed_runtime.run_managed_mode(
        invocation, authority=FakeReservation(), daily=wiring
    )
    assert receipt["status"] == "release_pending"
    assert receipt["grant_digest"] == summary["grant_digest"]


def test_render_refuses_bad_inputs_and_never_overwrites(tmp_path):
    for overrides in (
        {"image_digest": "8" * 64},
        {"image_digest": "sha256:" + "8" * 63},
        {"sequence": 0},
        {"sequence": managed_runtime.MAX_GRANT_POINTERS + 1},
        {"valid_from": "2026-09-25"},
    ):
        target = tmp_path / str(len(list(tmp_path.iterdir())))
        with pytest.raises(ValueError):
            _render(target, **overrides)
        assert not target.exists() or list(target.iterdir()) == []
    _render(tmp_path / "once")
    before = {path.name: path.read_bytes() for path in (tmp_path / "once").iterdir()}
    with pytest.raises(ValueError, match="^output_exists$"):
        _render(tmp_path / "once", sequence=2)
    after = {path.name: path.read_bytes() for path in (tmp_path / "once").iterdir()}
    assert after == before


def test_the_command_line_renders_with_positional_arguments(tmp_path):
    out = io.StringIO()
    code = daily_grant_pointer.main(
        ["render", IMAGE_DIGEST, "2026-09-25", "2", str(tmp_path)], out=out
    )
    assert code == 0
    printed = json.loads(out.getvalue())
    assert printed["status"] == "rendered"
    assert printed["objects"]["pointer"]["name"] == managed_runtime.grant_pointer_name(
        IMAGE_DIGEST, 2
    )
    assert json.loads((tmp_path / "pointer.json").read_bytes())["sequence"] == 2
    refused = io.StringIO()
    assert (
        daily_grant_pointer.main(
            ["render", IMAGE_DIGEST, "2026-9-25", "1", str(tmp_path / "x")],
            out=refused,
        )
        == 1
    )
    assert json.loads(refused.getvalue())["status"] == "refused"
