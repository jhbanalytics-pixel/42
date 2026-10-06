"""The amendment e words read the delta at the repository root, bound by its digest.

``ops/deploy/iam_delta_v1.json`` is outside the engine build context, so the unit
tests of the iam-e words run over a copy under ``tests/fixtures``. The words carry no
pin of the delta: the plan reports the digest of the bytes it read and the apply takes
the approved digest as its argument. This check runs over the checkout and holds the
words' path to the root delta, and holds that the delta as it stands now is one the
words accept, so a round that adds rows they cannot serve fails here first.
It also holds the words' copy of the custom role permission pins equal to the pins
the delta validator keeps.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from scripts.migrations import create_open_intelligence_execution_approval_store as migration

ENGINE_ROOT = Path(__file__).resolve().parents[2]
DELTA = ENGINE_ROOT.parent / "ops" / "deploy" / "iam_delta_v1.json"


def test_the_iam_e_words_read_the_root_delta_and_accept_it_as_it_stands():
    assert migration.IAM_E_DELTA_PATH.resolve() == DELTA.resolve()
    targets = migration.iam_e_targets()
    assert targets.delta_sha256 == hashlib.sha256(DELTA.read_bytes()).hexdigest()
    assert targets.bindings


def test_the_pinned_custom_role_permissions_equal_the_delta_validator_pins(monkeypatch):
    monkeypatch.syspath_prepend(str(ENGINE_ROOT.parent))
    from ops.deploy import iam_delta

    pinned = {
        role: tuple(permissions) for role, permissions in iam_delta.CUSTOM_ROLE_PERMISSIONS.items()
    }
    assert pinned == migration._IAM_E_PINNED_CUSTOM_ROLES
