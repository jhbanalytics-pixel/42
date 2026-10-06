"""The stamp an amendment applied under a grant carries in the delta file.

After iam-e-apply-granted applies a proposed last amendment, one commit stamps that
amendment's block and nothing else: state approved, approved_at the go time in
+00:00 form, approved_by the grant's text byte for byte, applied still false. Taking
the stamped block back to proposed, with any later amendment removed, gives exactly
the bytes the grant names. Amendment f's go has the grant's form, so its stamp is the
worked example; every grant saved under docs/operations/iam-grants is held to the
same rule. Every expected value for amendment f is a literal in this file.
"""

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

from ops.deploy.iam_delta import MANIFEST_SHA256, load_iam_delta, validate_iam_delta
from ops.deploy.resource_guard import load_resource_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
DELTA_PATH = REPO_ROOT / "ops" / "deploy" / "iam_delta_v1.json"
OPS_MANIFEST_PATH = REPO_ROOT / "ops" / "deploy" / "resource_manifest.json"
GRANTS_DIR = REPO_ROOT / "docs" / "operations" / "iam-grants"

F_PROPOSED_SHA256 = "a792f21e3340258a0c2dd017e5f6c6a2d7cb1242952359bb7a3cec2087673a22"
F_STAMPED_SHA256 = "29d0f741a5295bddf2ef0e4cbce8c4a2c53bdfd5164b869878bf8f36a221131b"
# The grant amendment f's go would have carried, had it gone the granted path.
F_GRANT = {
    "amendment": "f",
    "approved_by": (
        "Albert Meintjes, go in session_01SGzy35qkbHF8xf2AFuo5nW at 2026-09-25T05:36:23Z"
    ),
    "contract_version": "42_iam_amendment_grant_v1",
    "delta_sha256": F_PROPOSED_SHA256,
    "expires_at": "2026-09-25T13:36:23Z",
    "granted_at": "2026-09-25T05:36:23Z",
    "grantor": "Albert Meintjes",
    "head_commit": "0" * 40,
    "purpose": "iam_amendment_apply",
    "resource_manifest_sha256": (
        "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09"
    ),
}
PROPOSED = {
    "applied": False,
    "approved_at": None,
    "approved_by": None,
    "state": "proposed",
}
GO = re.compile(
    r"Albert Meintjes, go in (session_[A-Za-z0-9]{8,64}) at "
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)",
    re.ASCII,
)


def _canonical(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _stamp(grant):
    return {
        "applied": False,
        "approved_at": grant["granted_at"].removesuffix("Z") + "+00:00",
        "approved_by": grant["approved_by"],
        "state": "approved",
    }


def _saved_grants():
    if not GRANTS_DIR.is_dir():
        return []
    return sorted(
        path
        for path in GRANTS_DIR.glob("*.json")
        if not path.name.endswith((".claim.json", ".receipt.json", ".lock.json"))
    )


def _grants():
    saved = [(path.name, path) for path in _saved_grants()]
    return [("amendment_f_go", None), *saved]


@pytest.fixture(scope="module")
def manifest():
    return load_resource_manifest(OPS_MANIFEST_PATH, expected_sha256=MANIFEST_SHA256)


def _covered(delta, amendment):
    """The file as the grant names it: the amendment proposed and last."""

    covered = deepcopy(_through(delta, amendment))
    covered["amendments"][-1]["approval"] = deepcopy(PROPOSED)
    return covered


def _through(delta, amendment):
    names = [entry["amendment"] for entry in delta["amendments"]]
    return {**delta, "amendments": delta["amendments"][: names.index(amendment) + 1]}


def test_amendment_f_s_stamp_is_the_stamp_its_grant_would_make():
    delta = load_iam_delta(DELTA_PATH)
    (entry,) = [item for item in delta["amendments"] if item["amendment"] == "f"]
    assert entry["approval"] == _stamp(F_GRANT)
    assert entry["approval"]["approved_at"] == "2026-09-25T05:36:23+00:00"
    # The file through amendment f is the stamped file Albert's go produced.
    stamped = _canonical(_through(delta, "f"))
    assert hashlib.sha256(stamped).hexdigest() == F_STAMPED_SHA256


def _check_saved(path):
    """A saved grant is canonical and sits beside the claim and receipt it spent."""

    raw = path.read_bytes()
    grant = json.loads(raw)
    assert _canonical(grant) == raw
    claim = json.loads(path.with_name(path.stem + ".claim.json").read_bytes())
    receipt = json.loads(path.with_name(path.stem + ".receipt.json").read_bytes())
    digest = hashlib.sha256(raw).hexdigest()
    assert claim["grant_sha256"] == digest
    assert claim["delta_sha256"] == grant["delta_sha256"]
    assert receipt["approval"] == {
        "path": "grant",
        "amendment": grant["amendment"],
        "grant_sha256": digest,
        "head_commit": grant["head_commit"],
        "approved_by": grant["approved_by"],
    }
    assert receipt["approval_state"] == "proposed"
    return grant


@pytest.mark.parametrize(
    ("name", "path"), _grants(), ids=[name for name, _path in _grants()]
)
def test_a_stamped_block_taken_back_to_proposed_gives_the_bytes_its_grant_names(
    name, path
):
    grant = F_GRANT if path is None else _check_saved(path)
    delta = load_iam_delta(DELTA_PATH)
    (entry,) = [
        item for item in delta["amendments"] if item["amendment"] == grant["amendment"]
    ]
    go = GO.fullmatch(grant["approved_by"])
    assert go is not None, name
    assert go.group(2) == grant["granted_at"]
    assert grant["grantor"] == "Albert Meintjes"
    # Only the block changes: approved, at the go, by the grant's text byte for byte.
    assert entry["approval"] == _stamp(grant)
    assert list(entry["approval"]) == sorted(entry["approval"])
    covered = _covered(delta, grant["amendment"])
    assert hashlib.sha256(_canonical(covered)).hexdigest() == grant["delta_sha256"]
    assert covered["resource_manifest_sha256"] == grant["resource_manifest_sha256"]


def test_a_stamped_block_still_refuses_applied_true(manifest):
    delta = load_iam_delta(DELTA_PATH)
    report = validate_iam_delta(deepcopy(delta), manifest)
    assert {"amendment": "f", "bindings": 4, "state": "approved"} in report["amendments"]
    (entry,) = [item for item in delta["amendments"] if item["amendment"] == "f"]
    entry["approval"] = {**_stamp(F_GRANT), "applied": True}
    with pytest.raises(ValueError, match="^iam_delta_applied$"):
        validate_iam_delta(delta, manifest)


def test_a_grant_stamp_validates_over_the_proposed_bytes(manifest):
    delta = load_iam_delta(DELTA_PATH)
    covered = _covered(delta, "f")
    report = validate_iam_delta(deepcopy(covered), manifest)
    assert report["amendments"][-1] == {"amendment": "f", "bindings": 4, "state": "proposed"}
    covered["amendments"][-1]["approval"] = _stamp(F_GRANT)
    assert covered == _through(delta, "f")
    report = validate_iam_delta(covered, manifest)
    assert report["amendments"][-1]["state"] == "approved"
