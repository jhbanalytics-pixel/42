"""The root delta carries amendment f as the iam-e words' unit tests build it.

The unit tests of amendment f build the delta from the amendment e fixture under
amendment e's stamp plus ``tests/fixtures/iam_delta_amendment_f.json``, because
``ops/deploy/iam_delta_v1.json`` is outside the engine build context. This check runs
over the checkout and holds the root delta to that construction: amendment e's stamped
bytes with the amendments list taken out, and amendment f's rows equal to the fixture's
whatever its approval block says, so stamping f moves nothing here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.migrations import create_open_intelligence_execution_approval_store as migration

ENGINE_ROOT = Path(__file__).resolve().parents[2]
DELTA = ENGINE_ROOT.parent / "ops" / "deploy" / "iam_delta_v1.json"
F_FIXTURE = ENGINE_ROOT / "tests" / "fixtures" / "iam_delta_amendment_f.json"
# Amendment g, the final amendment, stands after f; its unit tests build it from this.
G_FIXTURE = ENGINE_ROOT / "tests" / "fixtures" / "iam_delta_amendment_g.json"
E_STAMPED_SHA256 = "a2bdcfdbe19a94e76699e7cac696f086084cb59a2bbf2102cabafb85aaec3ef1"


def _canonical(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _without_approvals(entries: list) -> list:
    return [{key: value for key, value in entry.items() if key != "approval"} for entry in entries]


def test_the_root_delta_is_amendment_e_under_its_stamp_plus_the_fixture_amendments():
    raw = DELTA.read_bytes()
    delta = json.loads(raw)
    assert _canonical(delta) == raw
    amendments = delta.pop("amendments")
    assert hashlib.sha256(_canonical(delta)).hexdigest() == E_STAMPED_SHA256
    fixture = [
        *json.loads(F_FIXTURE.read_text(encoding="utf-8")),
        *json.loads(G_FIXTURE.read_text(encoding="utf-8")),
    ]
    assert _without_approvals(amendments) == _without_approvals(fixture)
    assert [entry["amendment"] for entry in amendments] == ["f", "g"]


def test_the_iam_e_words_serve_the_root_amendment_rows():
    targets = migration.iam_e_targets()
    served = {(row.member, row.resource, row.role, row.condition) for row in targets.bindings}
    entries = json.loads(DELTA.read_bytes())["amendments"]
    rows = {
        (row["member"], row["resource"], row["role"], row["condition"])
        for entry in entries
        for row in entry["bindings"]
    }
    # Amendment f's four rows and amendment g's seven.
    assert len(rows) == 11
    assert rows <= served
    approved = all(entry["approval"]["state"] == "approved" for entry in entries)
    assert targets.approval_state == ("approved" if approved else "proposed")
