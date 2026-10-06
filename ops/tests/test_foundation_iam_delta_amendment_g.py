"""Provisioning amendment g: the one final amendment, proposed.

Albert froze amendments on 25 September: amendment g carries every grant the rest of
the plan needs that no earlier block carries, and after it there is no other round.
It stands after amendment f in the delta's amendments list under its own proposed
block, so amendment e's stamp and amendment f's stamp keep covering exactly the bytes
they covered: taking amendment g out gives the file under f's stamp, 29d0f741.

Its seven rows only add. Two extend the serving identity's time bound on the retained
custom roles QuestionVertexPredict and QuestionControlReplace from 30 September to 31
December, beside the retained bindings, which are left to lapse; neither role is
defined or changed. One binds QuestionVertexPredict to the daily account at project
level under the same 31 December bound, because daily_composition_apply funds up to
100 model calls and the compose calls Vertex AI as that account. Four open the route A capture's artifact bucket
ogilvy-trends-v2-oi-source-artifacts-staging: the daily account reloads the bucket,
reads inputs/ and captures/ and creates under captures/, and the serving identity reads
inputs/ and captures/. The bucket is not a row of the reviewed deploy manifest
(33ed5155), which amendment g leaves byte for byte; the validator admits it for
amendment g's rows alone, through one pinned row equal to the bridge generation
manifest's row. Every expected value is a literal in this file.
"""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from ops.deploy import iam_delta
from ops.deploy.iam_delta import (
    CUSTOM_ROLE_PERMISSIONS,
    MANIFEST_SHA256,
    load_iam_delta,
    validate_iam_delta,
)
from ops.deploy.resource_guard import load_resource_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
DELTA_PATH = REPO_ROOT / "ops" / "deploy" / "iam_delta_v1.json"
OPS_MANIFEST_PATH = REPO_ROOT / "ops" / "deploy" / "resource_manifest.json"
CONFIG = REPO_ROOT / "engine" / "configs" / "open_intelligence"
ENGINE_MANIFEST_PATH = CONFIG / "resource_manifest_v1.json"
BRIDGE_MANIFEST_PATH = CONFIG / "resource_manifest_bridge_v3.json"

MANIFEST_BYTES_SHA256 = (
    "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09"
)
# Tightening the date regex in the bridge policy moved the manifest from b7553041.
BRIDGE_MANIFEST_SHA256 = (
    "592ed45ffdc1a0a10371d197bc4cc2057372b12f845b7ea9c0280bfd68b37efe"
)
# Amendment e's stamped bytes and the file under amendment f's stamp.
E_STAMPED_SHA256 = "a2bdcfdbe19a94e76699e7cac696f086084cb59a2bbf2102cabafb85aaec3ef1"
F_STAMPED_SHA256 = "29d0f741a5295bddf2ef0e4cbce8c4a2c53bdfd5164b869878bf8f36a221131b"
# The file with amendment g proposed: the digest Albert's go on amendment g names.
# Amendment g's seventh row, the daily account's Vertex role, moved it from 812d0b06.
G_PROPOSED_SHA256 = "edbb789d269451fa41f5b30826e55c575cb0f2236b768bca4cfd5d362e048335"

PROPOSED = {
    "applied": False,
    "approved_at": None,
    "approved_by": None,
    "state": "proposed",
}
G_STAMP = {
    "applied": False,
    "approved_at": "2026-09-25T09:00:00+00:00",
    "approved_by": "Albert Meintjes, test stamp over amendment g",
    "state": "approved",
}

PROJECT = "ogilvy-trends-v2"
PROJECT_RESOURCE = f"//cloudresourcemanager.googleapis.com/projects/{PROJECT}"
CACHE = "//storage.googleapis.com/projects/_/buckets/listening-post-staging-cache"
APPROVALS_BUCKET = "//storage.googleapis.com/projects/_/buckets/ogilvy-trends-v2-execution-approvals-staging"
ARTIFACTS = "//storage.googleapis.com/projects/_/buckets/ogilvy-trends-v2-oi-source-artifacts-staging"
ARTIFACT_OBJECTS = (
    "projects/_/buckets/ogilvy-trends-v2-oi-source-artifacts-staging/objects/"
)
VERTEX_ROLE = f"projects/{PROJECT}/roles/QuestionVertexPredict"
REPLACE_ROLE = f"projects/{PROJECT}/roles/QuestionControlReplace"
OBJECT = "resource.type == 'storage.googleapis.com/Object'"
UNTIL = "request.time < timestamp('2026-12-31T23:59:59Z')"
LEDGER = (
    "projects/_/buckets/listening-post-staging-cache/objects/open-intelligence/v2/staging/"
    "general-questions/allowances/general_cultural_question_staging_eval_v1/ledger.json"
)
LEDGER_CONDITION = f"{OBJECT} && resource.name == '{LEDGER}' && {UNTIL}"
BUCKET_CONDITION = (
    "resource.type == 'storage.googleapis.com/Bucket' && resource.name == "
    "'projects/_/buckets/ogilvy-trends-v2-oi-source-artifacts-staging'"
)
READ_CONDITION = (
    f"{OBJECT} && (resource.name.startsWith('{ARTIFACT_OBJECTS}inputs/')"
    f" || resource.name.startsWith('{ARTIFACT_OBJECTS}captures/'))"
)
CREATE_CONDITION = (
    f"{OBJECT} && resource.name.startsWith('{ARTIFACT_OBJECTS}captures/')"
)
ARTIFACT_ROW = {"actions": ["read", "write"], "name": ARTIFACTS}


def _member(short):
    return f"serviceAccount:{short}@{PROJECT}.iam.gserviceaccount.com"


APP = _member("listening-post-staging")
ORCHESTRATION = _member("intelligence-42-orchestration")
FUNDED = _member("intelligence-42-funded")

# (member, resource, role, condition) for every binding amendment g proposes, in order.
AMENDMENT_G_BINDINGS = [
    (APP, PROJECT_RESOURCE, VERTEX_ROLE, UNTIL),
    (APP, CACHE, REPLACE_ROLE, LEDGER_CONDITION),
    (ORCHESTRATION, ARTIFACTS, "roles/storage.bucketViewer", BUCKET_CONDITION),
    (ORCHESTRATION, ARTIFACTS, "roles/storage.objectViewer", READ_CONDITION),
    (ORCHESTRATION, ARTIFACTS, "roles/storage.objectCreator", CREATE_CONDITION),
    (APP, ARTIFACTS, "roles/storage.objectViewer", READ_CONDITION),
    (ORCHESTRATION, PROJECT_RESOURCE, VERTEX_ROLE, UNTIL),
]
# The retained rows amendment g extends; they stay in the file as they were.
RETAINED_UNTIL_SEPTEMBER = [
    (
        APP,
        PROJECT_RESOURCE,
        VERTEX_ROLE,
        "request.time < timestamp('2026-09-30T23:59:59Z')",
    ),
    (
        APP,
        CACHE,
        REPLACE_ROLE,
        (
            f"{OBJECT} && resource.name == '{LEDGER}'"
            " && request.time < timestamp('2026-09-30T23:59:59Z')"
        ),
    ),
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


def _entry(delta, name):
    (entry,) = [item for item in delta["amendments"] if item["amendment"] == name]
    return entry


def _row(member, resource, role, condition, purpose="derived: amendment g"):
    return {
        "condition": condition,
        "member": member,
        "purpose": purpose,
        "resource": resource,
        "role": role,
    }


# 1. The file: e's and f's stamps stand, g stands after them, proposed.


def test_the_file_is_the_f_stamped_file_plus_amendment_g_proposed():
    raw = DELTA_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == G_PROPOSED_SHA256
    parsed = json.loads(raw)
    assert _canonical(parsed) == raw
    assert [entry["amendment"] for entry in parsed["amendments"]] == ["f", "g"]
    without_g = deepcopy(parsed)
    without_g["amendments"].pop()
    assert hashlib.sha256(_canonical(without_g)).hexdigest() == F_STAMPED_SHA256
    del without_g["amendments"]
    assert hashlib.sha256(_canonical(without_g)).hexdigest() == E_STAMPED_SHA256
    # The reviewed deploy manifest and the amendment e engine manifest keep their bytes.
    for path in (OPS_MANIFEST_PATH, ENGINE_MANIFEST_PATH):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == MANIFEST_BYTES_SHA256
    assert parsed["resource_manifest_sha256"] == MANIFEST_BYTES_SHA256


def test_amendment_g_is_proposed_and_carries_exactly_its_seven_rows(delta):
    entry = _entry(delta, "g")
    assert sorted(entry) == ["amendment", "approval", "bindings"]
    assert entry["approval"] == PROPOSED
    assert [_key(row) for row in entry["bindings"]] == AMENDMENT_G_BINDINGS
    for row in entry["bindings"]:
        assert row["purpose"].startswith("derived: "), row
        assert row["purpose"].endswith("(amendment g)"), row
    main = {_key(row) for row in delta["bindings"]}
    assert not main & set(AMENDMENT_G_BINDINGS)


def test_the_retained_rows_stay_and_g_only_adds_beside_them(delta):
    retained = [_key(row) for row in delta["retained"]]
    for key in RETAINED_UNTIL_SEPTEMBER:
        assert key in retained
    # Each extension differs from its retained row only in the time bound.
    for new, old in zip(AMENDMENT_G_BINDINGS[:2], RETAINED_UNTIL_SEPTEMBER):
        assert new[:3] == old[:3]
        assert new[3].replace("2026-12-31", "2026-09-30") == old[3]


def test_the_purposes_name_the_code_each_row_serves(delta):
    rows = [row["purpose"] for row in _entry(delta, "g")["bindings"]]
    assert "general_question_policy.py" in rows[0]
    assert "2026-09-30T23:59:59Z" in rows[0] and "2026-12-31T23:59:59Z" in rows[0]
    assert "allowance ledger" in rows[1]
    assert "SourceCaptureObjects.preflight" in rows[2]
    assert "storage.buckets.get" in rows[2]
    assert "SourceCaptureObjects.read_input" in rows[3]
    assert "SourceCaptureObjects.read_capture" in rows[3]
    assert "staging_source_profile.py" in rows[3]
    assert "SourceCaptureObjects.store_capture" in rows[4]
    assert "without storage.objects.delete" in rows[4]
    assert "SourceCaptureObjects.read_stored_capture" in rows[5]
    assert "bridge_native_clients.py" in rows[5]
    assert "daily_composition_apply" in rows[6]
    assert "native_model_sdk" in rows[6] and "daily_product_io.py" in rows[6]
    assert "2026-12-31T23:59:59Z" in rows[6]
    for text in (*rows[:2], rows[6]):
        assert "neither defined nor changed" in text


def test_the_delta_validates_and_reports_amendment_g_proposed(delta, manifest):
    report = validate_iam_delta(delta, manifest)
    assert report["ok"] is True
    assert report["bindings"] == 134
    assert report["amendments"] == [
        {"amendment": "f", "bindings": 4, "state": "approved"},
        {"amendment": "g", "bindings": 7, "state": "proposed"},
    ]


def test_amendment_g_validates_under_a_stamp(delta, manifest):
    _entry(delta, "g")["approval"] = deepcopy(G_STAMP)
    report = validate_iam_delta(delta, manifest)
    assert report["amendments"][-1] == {
        "amendment": "g",
        "bindings": 7,
        "state": "approved",
    }


def test_amendment_g_is_approved_only_over_an_approved_f(delta, manifest):
    _entry(delta, "f")["approval"] = deepcopy(PROPOSED)
    validate_iam_delta(delta, manifest)
    _entry(delta, "g")["approval"] = deepcopy(G_STAMP)
    _refuses(delta, manifest, "iam_delta_invalid")


# 2. No custom role is defined: both are live roles the serving identity already holds.


def test_amendment_g_defines_no_custom_role(delta):
    assert "custom_roles" not in delta
    assert VERTEX_ROLE not in CUSTOM_ROLE_PERMISSIONS
    assert REPLACE_ROLE not in CUSTOM_ROLE_PERMISSIONS


def _outside_g(index):
    # Outside g the serving identity's rows are regrants, the artifact bucket is
    # unknown, and the daily account's Vertex row is a project role no rule admits.
    if index < 2:
        return "iam_delta_app_regrant"
    if index == 6:
        return "iam_delta_project_level_forbidden"
    return "iam_delta_resource_unknown"


# 3. The artifact bucket: one pinned row, equal to the bridge manifest's, for g alone.


def test_the_artifact_bucket_pin_is_the_bridge_manifest_row():
    raw = BRIDGE_MANIFEST_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == BRIDGE_MANIFEST_SHA256
    bridge = json.loads(raw)
    assert [row for row in bridge["resources"] if row["name"] == ARTIFACTS] == [
        ARTIFACT_ROW
    ]
    assert iam_delta._AMENDMENT_RESOURCES == {"g": (ARTIFACT_ROW,)}
    ops = json.loads(OPS_MANIFEST_PATH.read_bytes())
    assert ARTIFACTS not in {row["name"] for row in ops["resources"]}


@pytest.mark.parametrize("index", range(7))
def test_no_g_row_opens_in_the_main_bindings(delta, manifest, index):
    row = deepcopy(_entry(delta, "g")["bindings"][index])
    delta["amendments"].pop()
    delta["bindings"].append(row)
    expected = _outside_g(index)
    _refuses(delta, manifest, expected)


@pytest.mark.parametrize("index", range(7))
def test_no_g_row_opens_in_amendment_f(delta, manifest, index):
    row = deepcopy(_entry(delta, "g")["bindings"][index])
    delta["amendments"].pop()
    _entry(delta, "f")["bindings"].append(row)
    expected = _outside_g(index)
    _refuses(delta, manifest, expected)


@pytest.mark.parametrize("index", range(7))
def test_no_g_row_opens_in_a_later_amendment(delta, manifest, index):
    entry = _entry(delta, "g")
    row = entry["bindings"].pop(index)
    delta["amendments"].append(
        {"amendment": "h", "approval": deepcopy(PROPOSED), "bindings": [row]}
    )
    expected = _outside_g(index)
    _refuses(delta, manifest, expected)


def test_the_pinned_bucket_row_opens_no_other_row_in_g(delta, manifest):
    # The artifact bucket is a resource amendment g may name, but only in its pinned rows.
    rows = _entry(delta, "g")["bindings"]
    rows.append(_row(FUNDED, ARTIFACTS, "roles/storage.objectViewer", READ_CONDITION))
    _refuses(delta, manifest, "iam_delta_condition_not_allowlisted")


def test_a_manifest_that_already_names_the_bucket_is_refused(delta, manifest):
    widened = {**manifest, "resources": [*manifest["resources"], dict(ARTIFACT_ROW)]}
    # A delta binds its manifest by digest, so a widened manifest fails there first;
    # the amendment pin never stands in for a row the manifest already carries.
    _refuses(delta, widened, "iam_delta_manifest_digest_mismatch")


# 4. Each row exactly as pinned: any change to member, role, resource or condition.


def _swap(index, **change):
    def mutate(entry):
        entry["bindings"][index].update(change)

    return mutate


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            _swap(0, condition="request.time < timestamp('2027-01-01T00:00:00Z')"),
            "iam_delta_app_regrant",
        ),
        (_swap(0, condition=None), "iam_delta_app_regrant"),
        # The daily account's own Vertex row is row 6, so this is that row again.
        (_swap(0, member=ORCHESTRATION), "iam_delta_duplicate_binding"),
        (_swap(0, role="roles/aiplatform.user"), "iam_delta_app_regrant"),
        (
            _swap(1, condition=f"{OBJECT} && resource.name == '{LEDGER}'"),
            "iam_delta_app_regrant",
        ),
        (
            _swap(1, condition=LEDGER_CONDITION.replace("2026-12-31", "2027-12-31")),
            "iam_delta_app_regrant",
        ),
        (_swap(1, resource=APPROVALS_BUCKET), "iam_delta_app_regrant"),
        (_swap(2, condition=READ_CONDITION), "iam_delta_role_unknown"),
        (_swap(2, member=APP), "iam_delta_app_regrant"),
        (_swap(2, member=FUNDED), "iam_delta_role_unknown"),
        (_swap(2, resource=CACHE), "iam_delta_role_unknown"),
        (_swap(2, role="roles/storage.legacyBucketReader"), "iam_delta_role_unknown"),
        (_swap(3, condition=OBJECT), "iam_delta_condition_not_allowlisted"),
        (
            _swap(3, condition=READ_CONDITION.replace("inputs/", "")),
            "iam_delta_condition_not_allowlisted",
        ),
        (_swap(3, resource=APPROVALS_BUCKET), "iam_delta_condition_not_allowlisted"),
        (_swap(3, resource=PROJECT_RESOURCE), "iam_delta_project_level_forbidden"),
        (_swap(4, condition=READ_CONDITION), "iam_delta_condition_not_allowlisted"),
        (_swap(4, role="roles/storage.objectUser"), "iam_delta_role_scope"),
        (_swap(4, role="roles/storage.objectAdmin"), "iam_delta_role_unknown"),
        (_swap(4, member=FUNDED), "iam_delta_condition_not_allowlisted"),
        (_swap(5, role="roles/storage.objectCreator"), "iam_delta_app_regrant"),
        (_swap(5, condition=CREATE_CONDITION), "iam_delta_app_regrant"),
        (_swap(5, member=FUNDED), "iam_delta_condition_not_allowlisted"),
        (
            _swap(6, condition="request.time < timestamp('2027-01-01T00:00:00Z')"),
            "iam_delta_project_level_forbidden",
        ),
        (_swap(6, condition=None), "iam_delta_project_level_forbidden"),
        (_swap(6, member=FUNDED), "iam_delta_project_level_forbidden"),
        (
            _swap(6, member=_member("intelligence-42-ingest")),
            "iam_delta_project_level_forbidden",
        ),
        (_swap(6, member=APP), "iam_delta_duplicate_binding"),
        (_swap(6, role="roles/aiplatform.user"), "iam_delta_project_level_forbidden"),
        (_swap(6, role=REPLACE_ROLE), "iam_delta_project_level_forbidden"),
        (_swap(6, resource=CACHE), "iam_delta_role_unknown"),
    ],
    ids=[
        "vertex_later_bound",
        "vertex_unbounded",
        "vertex_daily_account",
        "vertex_other_role",
        "replace_unbounded",
        "replace_later_bound",
        "replace_other_bucket",
        "bucket_read_on_objects",
        "bucket_read_serving",
        "bucket_read_funded",
        "bucket_read_cache",
        "bucket_read_legacy_role",
        "daily_read_whole_bucket",
        "daily_read_captures_only_widened",
        "daily_read_approvals_bucket",
        "daily_read_project",
        "daily_create_inputs",
        "daily_create_replace",
        "daily_create_admin",
        "daily_create_funded",
        "serving_create",
        "serving_read_captures_only",
        "serving_read_funded",
        "daily_vertex_later_bound",
        "daily_vertex_unbounded",
        "daily_vertex_funded",
        "daily_vertex_ingest",
        "daily_vertex_serving",
        "daily_vertex_other_role",
        "daily_vertex_replace_role",
        "daily_vertex_bucket",
    ],
)
def test_each_g_row_is_admitted_only_exactly_as_pinned(delta, manifest, mutate, code):
    mutate(_entry(delta, "g"))
    _refuses(delta, manifest, code)
