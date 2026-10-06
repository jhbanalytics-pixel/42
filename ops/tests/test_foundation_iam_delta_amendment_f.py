"""Provisioning amendment f: the daily account reads the funded run's record.

Albert chose "Yes, both" at 08:15 UTC on 24 September, which carries a small amendment
f approved later by digest. The route A bridge capture runs as the daily account
intelligence-42-orchestration on intelligence-42-daily-staging and, at run time, binds
each collection receipt to the funded run: it submits _RESULT_SQL_V2 as the caller
(bridge_native_clients.ledger_reader), a direct statement over the three v2 ledger
tables that no routine authorization covers, and it reads the funded execution with
one Cloud Run v2 GET (BridgeFundedExecutions.read), which needs run.executions.get.

Amendment f stands in the same delta file under its own approval block, so
amendment e's stamp stays in force, byte for byte, over exactly the bytes it covered:
taking the amendments list out gives the stamped amendment e file, and taking its
block back to proposed gives the bytes Albert's phrase covered. Albert approved
amendment f by the digest of the file with f proposed, a792f21e, with his go in
session_01SGzy35qkbHF8xf2AFuo5nW at 2026-09-25T05:36:23Z; f's block carries that
stamp, and taking it back to proposed gives exactly the approved bytes. Every
expected value is a literal in this file.
"""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from ops.deploy.iam_delta import (
    MANIFEST_SHA256,
    load_iam_delta,
    validate_iam_delta,
)
from ops.deploy.resource_guard import load_resource_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
DELTA_PATH = REPO_ROOT / "ops" / "deploy" / "iam_delta_v1.json"
OPS_MANIFEST_PATH = REPO_ROOT / "ops" / "deploy" / "resource_manifest.json"
ENGINE_MANIFEST_PATH = (
    REPO_ROOT / "engine" / "configs" / "open_intelligence" / "resource_manifest_v1.json"
)

MANIFEST_BYTES_SHA256 = (
    "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09"
)
# Amendment e: the bytes Albert's phrase covered, and the bytes under his stamp.
E_PROPOSED_SHA256 = "33ad00c0254fd0e514c56bf17b98571f95608ab30604a2523499a7ddf37bda9f"
E_STAMPED_SHA256 = "a2bdcfdbe19a94e76699e7cac696f086084cb59a2bbf2102cabafb85aaec3ef1"
# The file with amendment f proposed: the digest Albert approved amendment f by.
F_PROPOSED_SHA256 = "a792f21e3340258a0c2dd017e5f6c6a2d7cb1242952359bb7a3cec2087673a22"
# The file under amendment f's stamp. Amendment g, proposed, now stands after f, so the
# committed file is this with amendment g added; taken out, it is exactly these bytes.
F_STAMPED_SHA256 = "29d0f741a5295bddf2ef0e4cbce8c4a2c53bdfd5164b869878bf8f36a221131b"
# Amendment g's seventh row, the daily account's Vertex role, moved it from 812d0b06.
G_PROPOSED_SHA256 = "edbb789d269451fa41f5b30826e55c575cb0f2236b768bca4cfd5d362e048335"
G_PROPOSED_REPORT = {"amendment": "g", "bindings": 7, "state": "proposed"}

PROPOSED = {
    "applied": False,
    "approved_at": None,
    "approved_by": None,
    "state": "proposed",
}
E_STAMP = {
    "applied": False,
    "approved_at": "2026-09-24T06:53:16+00:00",
    "approved_by": (
        "Albert Meintjes, in the session chat, document 36b26fca392ed3267e67419ed03adcbd242fa4714008bc85e96ea6284533031c; "
        "amendment b PROVISIONING_DELTA_AMENDMENT_B_APPROVAL_20260914.json, document ea2579481ab074ce175f14e34db9e799d60ffc546ed3a8bd675e6f79bcf194df; "
        "amendment c PROVISIONING_DELTA_AMENDMENT_C_APPROVAL_20260914.json, document 3dce68ab20ecb238e10e15ea3e80bc962edb038d0d1f222e7e4ef72a7989e6ca; "
        "amendment d PROVISIONING_DELTA_AMENDMENT_D_APPROVAL_20260920.json, document 35023bcfc88eae60fe042e053dcf558a4bc375d2de881f8ff8a5543f61e9d13f; "
        "amendment e in the session chat, delta 33ad00c0254fd0e514c56bf17b98571f95608ab30604a2523499a7ddf37bda9f "
        "at commit b24e27cb01a89ad2631af0f3a0e9b076006b4cb9"
    ),
    "state": "approved",
}
F_APPROVED = {
    "applied": False,
    "approved_at": "2026-09-25T05:36:23+00:00",
    "approved_by": (
        "Albert Meintjes, go in session_01SGzy35qkbHF8xf2AFuo5nW "
        "at 2026-09-25T05:36:23Z"
    ),
    "state": "approved",
}
F_STAMP = {
    "applied": False,
    "approved_at": "2026-09-24T12:00:00+00:00",
    "approved_by": "Albert Meintjes, test stamp over amendment f",
    "state": "approved",
}

PROJECT = "ogilvy-trends-v2"
APPROVALS = (
    f"//bigquery.googleapis.com/projects/{PROJECT}/datasets/trends_v2_staging_approvals"
)
RUN_PARENT = f"//run.googleapis.com/projects/{PROJECT}/locations/us-central1"
PILOT_JOB = RUN_PARENT + "/jobs/intelligence-42-funded-pilot-staging"
DAILY_JOB = RUN_PARENT + "/jobs/intelligence-42-daily-staging"
INGEST_JOB = RUN_PARENT + "/jobs/intelligence-42-ingest-staging"
LEDGER_TABLES = tuple(
    APPROVALS + "/tables/open_intelligence_execution_" + name + "_v2"
    for name in ("approvals", "consumptions", "results")
)
VIEWER = "roles/bigquery.dataViewer"
EDITOR = "roles/bigquery.dataEditor"
RUN_VIEWER = "roles/run.viewer"


def _member(short):
    return f"serviceAccount:{short}@{PROJECT}.iam.gserviceaccount.com"


ORCHESTRATION = _member("intelligence-42-orchestration")
APP = _member("listening-post-staging")
OTHER_MEMBERS = tuple(
    _member("intelligence-42-" + short)
    for short in (
        "brain",
        "funded",
        "migration",
        "freshness",
        "qa",
        "ingest",
        "scheduler",
        "price-policy",
    )
)

# (member, resource, role, condition) for every binding amendment f proposes.
AMENDMENT_F_BINDINGS = [
    *((ORCHESTRATION, table, VIEWER, None) for table in LEDGER_TABLES),
    (ORCHESTRATION, PILOT_JOB, RUN_VIEWER, None),
]


@pytest.fixture(scope="module")
def manifest():
    return load_resource_manifest(OPS_MANIFEST_PATH, expected_sha256=MANIFEST_SHA256)


@pytest.fixture
def delta():
    return load_iam_delta(DELTA_PATH)


def _canonical(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _key(row):
    return (row["member"], row["resource"], row["role"], row["condition"])


def _refuses(delta, manifest, code):
    with pytest.raises(ValueError, match=f"^{code}$"):
        validate_iam_delta(delta, manifest)


def _row(member, resource, role, condition=None, purpose="derived: amendment f"):
    return {
        "condition": condition,
        "member": member,
        "purpose": purpose,
        "resource": resource,
        "role": role,
    }


def _amendment(delta):
    # Amendment f by name: amendment g stands after it since the final amendment.
    (entry,) = [item for item in delta["amendments"] if item["amendment"] == "f"]
    return entry


def _without_g(delta):
    """The file as amendment f's stamp left it, before amendment g was proposed."""

    assert [item["amendment"] for item in delta["amendments"]] == ["f", "g"]
    delta["amendments"].pop()
    return delta


# 1. The file: amendment e's stamp stands, amendment f stands beside it under its own.


def test_the_file_is_amendment_e_under_its_stamp_plus_amendment_f_under_its_stamp():
    raw = DELTA_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == G_PROPOSED_SHA256
    assert _canonical(json.loads(raw)) == raw
    parsed = _without_g(json.loads(raw))
    assert hashlib.sha256(_canonical(parsed)).hexdigest() == F_STAMPED_SHA256
    assert parsed["approval"] == E_STAMP
    assert list(parsed["approval"]) == sorted(E_STAMP)
    # The bytes Albert approved amendment f by, with f's block taken back to proposed.
    f_covered = deepcopy(parsed)
    _amendment(f_covered)["approval"] = deepcopy(PROPOSED)
    assert hashlib.sha256(_canonical(f_covered)).hexdigest() == F_PROPOSED_SHA256
    without_f = {key: value for key, value in parsed.items() if key != "amendments"}
    # Amendment e's stamped bytes, which the apply of 24 September was bound to.
    assert hashlib.sha256(_canonical(without_f)).hexdigest() == E_STAMPED_SHA256
    # And the bytes Albert's phrase covered, with e's block taken back to proposed.
    covered = {**without_f, "approval": PROPOSED}
    assert hashlib.sha256(_canonical(covered)).hexdigest() == E_PROPOSED_SHA256
    for path in (OPS_MANIFEST_PATH, ENGINE_MANIFEST_PATH):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == MANIFEST_BYTES_SHA256


def test_amendment_f_is_stamped_and_carries_exactly_its_four_rows(delta):
    entry = _amendment(delta)
    assert sorted(entry) == ["amendment", "approval", "bindings"]
    assert entry["amendment"] == "f"
    # Equality, so no field can drift under Albert's stamp.
    assert entry["approval"] == F_APPROVED
    assert list(entry["approval"]) == sorted(F_APPROVED)
    assert [_key(row) for row in entry["bindings"]] == AMENDMENT_F_BINDINGS
    for row in entry["bindings"]:
        assert row["purpose"].startswith("derived: "), row
        assert row["purpose"].endswith("(amendment f)"), row
    main = {_key(row) for row in delta["bindings"]}
    assert not main & set(AMENDMENT_F_BINDINGS)


def test_the_purposes_name_the_reads_the_capture_makes(delta):
    rows = {row["resource"]: row["purpose"] for row in _amendment(delta)["bindings"]}
    for table in LEDGER_TABLES:
        text = rows[table]
        assert "_RESULT_SQL_V2" in text
        assert "ledger_reader" in text
        assert table.rsplit("/", 1)[1] in text
        assert "no routine authorization covers it" in text
    assert "run.executions.get" in rows[PILOT_JOB]
    assert "BridgeFundedExecutions.read" in rows[PILOT_JOB]


def test_the_delta_validates_and_reports_amendment_f_approved(delta, manifest):
    report = validate_iam_delta(delta, manifest)
    assert report["ok"] is True
    # The main bindings are amendment e's 134, unchanged.
    assert report["bindings"] == 134
    assert report["amendments"] == [
        {"amendment": "f", "bindings": 4, "state": "approved"},
        G_PROPOSED_REPORT,
    ]


def test_amendment_f_validates_taken_back_to_proposed(delta, manifest):
    _amendment(delta)["approval"] = deepcopy(PROPOSED)
    report = validate_iam_delta(delta, manifest)
    assert report["amendments"] == [
        {"amendment": "f", "bindings": 4, "state": "proposed"},
        G_PROPOSED_REPORT,
    ]


def test_a_delta_without_amendments_reports_none(delta, manifest):
    del delta["amendments"]
    report = validate_iam_delta(delta, manifest)
    assert "amendments" not in report
    assert report["bindings"] == 134


def test_amendment_f_validates_under_its_own_stamp(delta, manifest):
    _amendment(delta)["approval"] = deepcopy(F_STAMP)
    report = validate_iam_delta(delta, manifest)
    assert report["amendments"] == [
        {"amendment": "f", "bindings": 4, "state": "approved"},
        G_PROPOSED_REPORT,
    ]


# 2. The approval chain: an amendment's block has the shape of the root block, and
# an amendment is approved only on top of an approved file and approved earlier ones.


@pytest.mark.parametrize(
    "approval",
    [
        {**PROPOSED, "approved_at": "2026-09-24T12:00:00+00:00"},
        {**PROPOSED, "approved_by": "Albert Meintjes"},
        {**F_STAMP, "approved_at": None},
        {**F_STAMP, "approved_by": None},
        {**PROPOSED, "state": "pending"},
        {key: value for key, value in PROPOSED.items() if key != "applied"},
        {**PROPOSED, "note": "x"},
        None,
    ],
)
def test_a_malformed_amendment_approval_is_refused(delta, manifest, approval):
    _amendment(delta)["approval"] = approval
    _refuses(delta, manifest, "iam_delta_invalid")


def test_an_applied_amendment_is_refused(delta, manifest):
    _amendment(delta)["approval"] = {**F_STAMP, "applied": True}
    _refuses(delta, manifest, "iam_delta_applied")


def test_an_amendment_approved_over_a_proposed_file_is_refused(delta, manifest):
    delta["approval"] = deepcopy(PROPOSED)
    _refuses(delta, manifest, "iam_delta_invalid")
    _amendment(delta)["approval"] = deepcopy(PROPOSED)
    validate_iam_delta(delta, manifest)
    _amendment(delta)["approval"] = deepcopy(F_STAMP)
    _refuses(delta, manifest, "iam_delta_invalid")


def test_a_later_amendment_is_approved_only_after_the_earlier_one(delta, manifest):
    _amendment(delta)["approval"] = deepcopy(PROPOSED)
    # Amendment g is proposed after f in the file; the later amendment here is h.
    later = {
        "amendment": "h",
        "approval": deepcopy(F_STAMP),
        "bindings": [_row(ORCHESTRATION, INGEST_JOB, RUN_VIEWER)],
    }
    delta["amendments"].append(later)
    _refuses(delta, manifest, "iam_delta_invalid")
    later["approval"] = deepcopy(PROPOSED)
    report = validate_iam_delta(delta, manifest)
    assert [item["amendment"] for item in report["amendments"]] == ["f", "g", "h"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda entry: entry.update(note="x"),
        lambda entry: entry.pop("bindings"),
        lambda entry: entry.update(bindings=[]),
        lambda entry: entry.update(bindings={}),
        lambda entry: entry.update(amendment="F"),
        lambda entry: entry.update(amendment="ff"),
        lambda entry: entry.update(amendment=None),
    ],
    ids=["extra_key", "no_bindings", "empty", "not_a_list", "upper", "long", "none"],
)
def test_a_malformed_amendment_is_refused(delta, manifest, mutate):
    mutate(_amendment(delta))
    _refuses(delta, manifest, "iam_delta_invalid")


@pytest.mark.parametrize("value", [[], {}, None, "f"])
def test_the_amendments_list_is_a_non_empty_list(delta, manifest, value):
    delta["amendments"] = value
    _refuses(delta, manifest, "iam_delta_invalid")


def test_amendments_must_be_named_in_order_and_once(delta, manifest):
    again = deepcopy(_amendment(delta))
    again["bindings"] = [_row(ORCHESTRATION, INGEST_JOB, RUN_VIEWER)]
    delta["amendments"].append(again)
    _refuses(delta, manifest, "iam_delta_invalid")
    again["amendment"] = "c"
    _refuses(delta, manifest, "iam_delta_invalid")
    # Amendment g already stands after f, so a second g is refused and the next is h.
    again["amendment"] = "g"
    _refuses(delta, manifest, "iam_delta_invalid")
    again["amendment"] = "h"
    # Amendment g is proposed, so an approved h after it is refused; proposed, it stands.
    _refuses(delta, manifest, "iam_delta_invalid")
    again["approval"] = deepcopy(delta["amendments"][-2]["approval"])
    assert delta["amendments"][-2]["amendment"] == "g"
    assert again["approval"]["state"] == "proposed"
    validate_iam_delta(delta, manifest)


def test_an_amendment_row_that_restates_a_main_row_is_refused(delta, manifest):
    (daily,) = [
        row
        for row in delta["bindings"]
        if (row["member"], row["resource"], row["role"])
        == (ORCHESTRATION, DAILY_JOB, RUN_VIEWER)
    ]
    _amendment(delta)["bindings"].append(deepcopy(daily))
    _refuses(delta, manifest, "iam_delta_duplicate_binding")


def test_an_amendment_row_listed_twice_is_refused(delta, manifest):
    entry = _amendment(delta)
    entry["bindings"].append(deepcopy(entry["bindings"][0]))
    _refuses(delta, manifest, "iam_delta_duplicate_binding")


def test_an_amendment_row_meets_every_rule_a_main_row_meets(delta, manifest):
    entry = _amendment(delta)
    entry["bindings"].append(_row(ORCHESTRATION, INGEST_JOB, "roles/run.developer"))
    _refuses(delta, manifest, "iam_delta_role_forbidden")
    entry["bindings"][-1] = _row(ORCHESTRATION, INGEST_JOB, RUN_VIEWER, purpose="x")
    _refuses(delta, manifest, "iam_delta_purpose_unlabelled")
    entry["bindings"][-1] = _row(_member("nobody-at-all"), INGEST_JOB, RUN_VIEWER)
    _refuses(delta, manifest, "iam_delta_member_unknown")


# 3. The new reach is pinned to amendment f's four rows and nothing near them.


@pytest.mark.parametrize("member", OTHER_MEMBERS)
def test_no_other_member_reads_a_ledger_table_in_an_amendment(delta, manifest, member):
    _amendment(delta)["bindings"][0]["member"] = member
    _refuses(delta, manifest, "iam_delta_role_scope")


@pytest.mark.parametrize("member", tuple(m for m in OTHER_MEMBERS if "funded" not in m))
def test_no_other_member_views_the_pilot_job_in_an_amendment(delta, manifest, member):
    _amendment(delta)["bindings"][3]["member"] = member
    _refuses(delta, manifest, "iam_delta_role_scope")


def test_the_daily_account_gets_no_write_on_a_ledger_table(delta, manifest):
    _amendment(delta)["bindings"][1]["role"] = EDITOR
    _refuses(delta, manifest, "iam_delta_role_scope")


def test_the_daily_account_row_on_a_ledger_table_takes_no_condition(delta, manifest):
    _amendment(delta)["bindings"][2]["condition"] = (
        "request.time < timestamp('2027-01-01T00:00:00Z')"
    )
    _refuses(delta, manifest, "iam_delta_role_scope")


def test_the_daily_account_rows_open_only_inside_amendment_f(delta, manifest):
    rows = _amendment(delta)["bindings"]
    del delta["amendments"]
    for row in rows:
        delta["bindings"].append(deepcopy(row))
        _refuses(delta, manifest, "iam_delta_role_scope")
        delta["bindings"].pop()


def test_the_daily_account_rows_open_only_inside_an_amendment_named_f(delta, manifest):
    # On the file as f's stamp left it, so the renamed block is the only g.
    _without_g(delta)
    _amendment(delta)["amendment"] = "g"
    _refuses(delta, manifest, "iam_delta_role_scope")


def test_the_daily_account_still_gets_no_ledger_dataset_row(delta, manifest):
    _amendment(delta)["bindings"].append(_row(ORCHESTRATION, APPROVALS, VIEWER))
    _refuses(delta, manifest, "iam_delta_legacy_reach")
