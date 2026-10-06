"""Render the daily recurring grant and its pointer from a built image digest.

The grant binds the image digest, so it exists only after the image is built, and the
image carries no grant pin. The owner renders four files here, offline:

grant.json is the canonical grant the approve routine records (the approve-grant
input of engine/scripts/staging/approve_open_intelligence_execution.py).
grant-object.json is the object the daily job reads at 42/daily/grants/<digest>.json.
proposal.md is the proposal the approval phrase names by its sha256.
pointer.json is the create once pointer at 42/daily/grants/pointers/<image>/<n>.json
naming the grant digest and the approval phrase digest.

The printed summary names every object, every file digest, the approval phrase and the
revocation phrase, so the install and its readback compare against values derived
here and nowhere else. Nothing is written to a bucket or a table by this module. It
sits beside the runtime rather than in ops/deploy, whose reviewed file listing is
pinned with the approved provisioning delta.

Usage: python -m ops.runners.daily_grant_pointer render IMAGE_DIGEST VALID_FROM SEQUENCE
OUTPUT_DIR, with VALID_FROM the approval day as YYYY-MM-DD and SEQUENCE the pointer
number for that image, 1 for its first grant.
"""

import hashlib
import importlib
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from ops.deploy.resource_guard import load_resource_manifest
from ops.runners.managed_runtime import (
    GRANT_PREFIX,
    canonical_bytes,
    engine_daily_adapter,
    grant_pointer_bytes,
    grant_pointer_name,
    load_runtime_configuration,
)

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_FILE = ROOT / "infra" / "runtime" / "daily-staging.json"
RESOURCE_MANIFEST = ROOT / "ops" / "deploy" / "resource_manifest.json"
FILES = ("grant.json", "grant-object.json", "proposal.md", "pointer.json")


def _refuse(code):
    raise ValueError(code)


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def render(*, image_digest, valid_from, sequence, output_dir) -> dict:
    """Write the four files for one image and return the summary of their digests."""
    grant_pointer_name(image_digest, sequence)
    if type(valid_from) is not date:
        _refuse("valid_from_invalid")
    output = Path(output_dir)
    if output.exists() and any(output.iterdir()):
        _refuse("output_exists")
    config = load_runtime_configuration(RUNTIME_FILE)
    resources = load_resource_manifest(
        RESOURCE_MANIFEST, expected_sha256=config["resource_manifest_sha256"]
    )
    engine = engine_daily_adapter()
    phrases = importlib.import_module(
        "src.analysis.open_intelligence.recurring_grant_phrases"
    )
    approval = importlib.import_module(
        "src.analysis.open_intelligence.execution_approval"
    )
    # The approve routine and the durable reader admit only a grant naming the manifest
    # of the active generation, so the grant names that manifest.
    grant = engine.recurring_grant.proposed_grant(
        identities=resources["identities"],
        issuing_principal=approval._APPROVED_BY,
        resource_manifest_digest=engine.execution_generations.ACTIVE_GENERATION_PAIR[1],
        source_policy_digest=config["daily_profile"]["source_policy_digest"],
        valid_from=datetime.combine(valid_from, datetime.min.time(), tzinfo=UTC),
        permitted_image_digests=[image_digest],
    )
    digest = engine.recurring_grant.grant_digest(grant)
    if phrases.grant_digest(grant) != digest:
        _refuse("grant_digest_disagreement")
    proposal = engine.recurring_grant.render_grant_proposal(grant).encode("utf-8")
    proposal_sha256 = _sha256(proposal)
    phrase_sha256 = phrases.approval_phrase_sha256(proposal_sha256)
    contents = {
        "grant.json": phrases.canonical_grant_bytes(grant),
        "grant-object.json": canonical_bytes({"grant": grant}),
        "proposal.md": proposal,
        "pointer.json": grant_pointer_bytes(
            image_digest=image_digest,
            sequence=sequence,
            recurring_grant_digest=digest,
            approval_phrase_sha256=phrase_sha256,
        ),
    }
    output.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        with (output / name).open("xb") as stream:
            stream.write(contents[name])
    files = {name: _sha256(contents[name]) for name in FILES}
    return {
        "status": "rendered",
        "image_digest": image_digest,
        "bucket": engine.daily_store.EVIDENCE_BUCKET,
        "grant_id": grant["grant_id"],
        "grant_digest": digest,
        "valid_from": grant["valid_from"],
        "valid_until": grant["valid_until"],
        "proposal_sha256": proposal_sha256,
        "approval_phrase": phrases.approval_phrase(proposal_sha256),
        "approval_phrase_sha256": phrase_sha256,
        "revocation_phrase": phrases.revocation_phrase(grant["grant_id"]),
        "files": files,
        "objects": {
            "grant": {
                "name": f"{GRANT_PREFIX}/{digest}.json",
                "file": "grant-object.json",
                "sha256": files["grant-object.json"],
            },
            "pointer": {
                "name": grant_pointer_name(image_digest, sequence),
                "file": "pointer.json",
                "sha256": files["pointer.json"],
            },
        },
    }


def _parse(argv):
    if len(argv) != 5 or argv[0] != "render":
        _refuse("arguments_invalid")
    _command, image_digest, valid_from, sequence, output_dir = argv
    try:
        day = date.fromisoformat(valid_from)
    except ValueError:
        _refuse("valid_from_invalid")
    if day.isoformat() != valid_from:
        _refuse("valid_from_invalid")
    if not sequence.isdigit() or sequence != str(int(sequence)):
        _refuse("grant_pointer_invalid")
    return {
        "image_digest": image_digest,
        "valid_from": day,
        "sequence": int(sequence),
        "output_dir": Path(output_dir),
    }


def main(argv=None, *, out=None) -> int:
    stream = sys.stdout if out is None else out
    try:
        summary = render(**_parse(list(sys.argv[1:] if argv is None else argv)))
    except ValueError as exc:
        stream.write(
            json.dumps({"status": "refused", "error": str(exc)}, sort_keys=True) + "\n"
        )
        return 1
    stream.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
