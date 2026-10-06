"""Validator rules provisioning amendment E needs, each widened by one case.

Each widened rule is shown accepting its one new row on a copy of the delta as it
stood before amendment E (the committed delta with the amendment E bindings, which
the rows test pins as literals, taken out, and the function's authorization given
back its role), and the neighbouring rows it must keep refusing are shown
refusing. Every expected value is a literal in this file or read from a source
other than the delta under validation.
"""

import ast
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

from ops.deploy import iam_delta
from ops.deploy.iam_delta import (
    MANIFEST_SHA256,
    diff_against_readback,
    load_iam_delta,
    validate_iam_delta,
)
from ops.deploy.resource_guard import load_resource_manifest
from ops.tests.test_foundation_iam_delta_amendment_e_rows import AMENDMENT_E_BINDINGS

REPO_ROOT = Path(__file__).resolve().parents[2]
DELTA_PATH = REPO_ROOT / "ops" / "deploy" / "iam_delta_v1.json"
OPS_MANIFEST_PATH = REPO_ROOT / "ops" / "deploy" / "resource_manifest.json"
ENGINE_MANIFEST_PATH = (
    REPO_ROOT / "engine" / "configs" / "open_intelligence" / "resource_manifest_v1.json"
)
MIGRATION_PATH = (
    REPO_ROOT
    / "engine"
    / "scripts"
    / "migrations"
    / "create_open_intelligence_execution_approval_store.py"
)
ROUTINE_SQL_DIR = REPO_ROOT / "engine" / "infra" / "bigquery_routines"

DELTA_SHA256 = "a2bdcfdbe19a94e76699e7cac696f086084cb59a2bbf2102cabafb85aaec3ef1"
AMENDMENT_F_PROPOSED_SHA256 = (
    "a792f21e3340258a0c2dd017e5f6c6a2d7cb1242952359bb7a3cec2087673a22"
)
AMENDMENT_F_STAMPED_SHA256 = (
    "29d0f741a5295bddf2ef0e4cbce8c4a2c53bdfd5164b869878bf8f36a221131b"
)
# Amendment g, the final amendment, stands proposed after f: the committed file is the
# f stamped file with g added, and with g taken out it is exactly the f stamped bytes.
# Amendment g's seventh row, the daily account's Vertex role, moved it from 812d0b06.
AMENDMENT_G_PROPOSED_SHA256 = (
    "edbb789d269451fa41f5b30826e55c575cb0f2236b768bca4cfd5d362e048335"
)
PROPOSED_DELTA_SHA256 = (
    "33ad00c0254fd0e514c56bf17b98571f95608ab30604a2523499a7ddf37bda9f"
)
PROPOSED_APPROVAL = {
    "applied": False,
    "approved_at": None,
    "approved_by": None,
    "state": "proposed",
}
MANIFEST_BYTES_SHA256 = (
    "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09"
)

PROJECT = "ogilvy-trends-v2"
PROJECT_RESOURCE = f"//cloudresourcemanager.googleapis.com/projects/{PROJECT}"
RUN_PARENT = f"//run.googleapis.com/projects/{PROJECT}/locations/us-central1"
DAILY_JOB = RUN_PARENT + "/jobs/intelligence-42-daily-staging"
BRAIN_JOB = RUN_PARENT + "/jobs/intelligence-42-brain-staging"
DATASETS = f"//bigquery.googleapis.com/projects/{PROJECT}/datasets"
STAGING = DATASETS + "/trends_v2_staging"
SOURCES = DATASETS + "/intelligence_42_sources_staging"
APPROVALS = DATASETS + "/trends_v2_staging_approvals"
FN = APPROVALS + "/routines/fn_is_canonical_execution_json_v1"
BUCKETS = "//storage.googleapis.com/projects/_/buckets/"
APPROVALS_BUCKET = BUCKETS + "ogilvy-trends-v2-execution-approvals-staging"
CACHE_BUCKET = BUCKETS + "listening-post-staging-cache"
OBJECT_HEAD = "resource.type == 'storage.googleapis.com/Object' && resource.name"
DAILY_CONDITION = (
    OBJECT_HEAD + ".startsWith('projects/_/buckets/"
    "ogilvy-trends-v2-execution-approvals-staging/objects/42/daily/')"
)
DAILY_OBJECTS = (
    "projects/_/buckets/ogilvy-trends-v2-execution-approvals-staging/objects/42/daily/"
)
# The daily account replaces, under a generation match, only its slot control objects
# and the collection day index; it creates every other object it writes once and
# reads its records, the operator's grants and the workload rates. Each role opens by
# an allow list, so a prefix added under 42/daily/ later opens nothing.
DAILY_REPLACE_CLAUSES = (
    (
        f"(resource.name.startsWith('{DAILY_OBJECTS}slots/')"
        " && resource.name.endsWith('/control.json'))"
    ),
    f"resource.name.startsWith('{DAILY_OBJECTS}collection/days/')",
)
DAILY_CREATE_PREFIXES = (
    "authority/",
    "certification/",
    "slots/",
    "collection/receipts/",
    "source_runs/",
    "captures/",
    "products/",
    "artifact-sets/",
    "execution-observations/",
)
DAILY_READ_PREFIXES = (*DAILY_CREATE_PREFIXES, "grants/", "workload-rates/")


def _daily_prefixes(prefixes):
    return " || ".join(
        f"resource.name.startsWith('{DAILY_OBJECTS}{prefix}')" for prefix in prefixes
    )


DAILY_USER_CONDITION = (
    "resource.type == 'storage.googleapis.com/Object' && ("
    + " || ".join(DAILY_REPLACE_CLAUSES)
    + ")"
)
DAILY_RECORD_CREATE_CONDITION = (
    "resource.type == 'storage.googleapis.com/Object' && ("
    + _daily_prefixes(DAILY_CREATE_PREFIXES)
    + ")"
)
DAILY_RECORD_READ_CONDITION = (
    "resource.type == 'storage.googleapis.com/Object' && ("
    + _daily_prefixes(DAILY_READ_PREFIXES)
    + ")"
)
# The round 8 deny list: everything under 42/daily/ but three prefixes.
DAILY_DENY_LIST_CONDITION = (
    OBJECT_HEAD
    + f".startsWith('{DAILY_OBJECTS}')"
    + f" && !resource.name.startsWith('{DAILY_OBJECTS}authority/')"
    + f" && !resource.name.startsWith('{DAILY_OBJECTS}certification/')"
    + f" && !resource.name.startsWith('{DAILY_OBJECTS}grants/')"
)
ALERT_CONDITION = (
    OBJECT_HEAD + ".startsWith('projects/_/buckets/listening-post-staging-cache/"
    "objects/intelligence-42/staging/freshness/alerts/')"
)
SNAPSHOT_ROLE = f"projects/{PROJECT}/roles/SourceSnapshotCreate"
# Proposal B8's custom role option: datasets.get on the source dataset beside the
# four table permissions. The native readback confirms the list before any use.
SNAPSHOT_PERMISSIONS = (
    "bigquery.datasets.get",
    "bigquery.tables.create",
    "bigquery.tables.createSnapshot",
    "bigquery.tables.get",
    "bigquery.tables.getData",
)
SNAPSHOT_TOPIC = "source_snapshot_role_definition"
# The serving identity reads each capture clone's creating job, which another
# identity ran, so jobs.get alone at project level (round 6).
CAPTURE_JOB_ROLE = f"projects/{PROJECT}/roles/CaptureJobRead"
CAPTURE_JOB_PERMISSIONS = ("bigquery.jobs.get",)
CAPTURE_JOB_TOPIC = "capture_job_read_role_definition"
BUILDS_VIEWER = "roles/cloudbuild.builds.viewer"
OVERRIDES = "roles/run.jobsExecutorWithOverrides"
OBJECT_USER = "roles/storage.objectUser"
OBJECT_CREATOR = "roles/storage.objectCreator"
OBJECT_VIEWER = "roles/storage.objectViewer"
VIEWER = "roles/bigquery.dataViewer"
OPERATOR = "user:albert.meintjes@ogilvy.co.za"
RECONCILE = "sp_reconcile_open_intelligence_daily_consumption_v1"
OPERATOR_ROUTINES = (
    "sp_approve_open_intelligence_execution_v2",
    "sp_approve_open_intelligence_execution_v3",
    "sp_disable_open_intelligence_execution_approval_v2",
    "fn_is_canonical_execution_json_v1",
    "sp_approve_open_intelligence_recurring_grant_v2",
    "sp_read_open_intelligence_recurring_grant_v2",
    "sp_disable_open_intelligence_recurring_grant_v2",
    RECONCILE,
)


def _member(short):
    return f"serviceAccount:{short}@{PROJECT}.iam.gserviceaccount.com"


BRAIN = _member("intelligence-42-brain")
FUNDED = _member("intelligence-42-funded")
MIGRATION = _member("intelligence-42-migration")
ORCHESTRATION = _member("intelligence-42-orchestration")
FRESHNESS = _member("intelligence-42-freshness")
QA = _member("intelligence-42-qa")
INGEST = _member("intelligence-42-ingest")
SCHEDULER = _member("intelligence-42-scheduler")
PRICE_POLICY = _member("intelligence-42-price-policy")
BUILD = _member("intelligence-42-build")
DEPLOY = _member("intelligence-42-deploy")
APP = _member("listening-post-staging")
BUILD_READERS = (BRAIN, FUNDED, MIGRATION, ORCHESTRATION)


@pytest.fixture(scope="module")
def manifest():
    return load_resource_manifest(OPS_MANIFEST_PATH, expected_sha256=MANIFEST_SHA256)


@pytest.fixture
def delta():
    value = load_iam_delta(DELTA_PATH)
    value["bindings"] = [
        row
        for row in value["bindings"]
        if (row["member"], row["resource"], row["role"], row["condition"])
        not in AMENDMENT_E_BINDINGS
    ]
    function = _authorization(value, FN)
    del function["routine_type"]
    function["role"] = "roles/bigquery.routineDataViewer"
    return value


def _binding(member, resource, role, condition=None, purpose="derived: amendment e"):
    return {
        "condition": condition,
        "member": member,
        "purpose": purpose,
        "resource": resource,
        "role": role,
    }


def _refuses(delta, manifest, code):
    with pytest.raises(ValueError, match=f"^{code}$"):
        validate_iam_delta(delta, manifest)


def _routine(name):
    return APPROVALS + "/routines/" + name


def _authorization(delta, routine):
    rows = [row for row in delta["routine_authorizations"] if row["routine"] == routine]
    assert len(rows) == 1, routine
    return rows[0]


def _role_less(delta, routine=FN, routine_type="SCALAR_FUNCTION"):
    row = _authorization(delta, routine)
    row["role"] = None
    row["routine_type"] = routine_type
    return row


# The manifest moves with amendment E; the delta bytes are the proposal under
# Albert's stamp, so with the block taken back to proposed they are the covered bytes.


def test_delta_and_manifest_bytes_are_pinned():
    # Amendment f stands beside the stamp in its own amendments list under Albert's
    # own stamp; with that list taken out the file is amendment e's stamped bytes,
    # with f's block taken back to proposed it is the a792f21e Albert approved, and
    # the whole file is pinned by its own digest.
    raw_bytes = DELTA_PATH.read_bytes()
    assert hashlib.sha256(raw_bytes).hexdigest() == AMENDMENT_G_PROPOSED_SHA256
    committed = json.loads(raw_bytes)
    assert [entry["amendment"] for entry in committed["amendments"]] == ["f", "g"]
    committed["amendments"].pop()
    raw_bytes = (json.dumps(committed, indent=2, sort_keys=True) + "\n").encode()
    assert hashlib.sha256(raw_bytes).hexdigest() == AMENDMENT_F_STAMPED_SHA256
    parsed = json.loads(raw_bytes)
    f_covered = json.loads(raw_bytes)
    f_covered["amendments"][0]["approval"] = PROPOSED_APPROVAL
    f_covered_bytes = (json.dumps(f_covered, indent=2, sort_keys=True) + "\n").encode()
    assert hashlib.sha256(f_covered_bytes).hexdigest() == AMENDMENT_F_PROPOSED_SHA256
    assert [entry["amendment"] for entry in parsed.pop("amendments")] == ["f"]
    stamped_bytes = (json.dumps(parsed, indent=2, sort_keys=True) + "\n").encode()
    assert hashlib.sha256(stamped_bytes).hexdigest() == DELTA_SHA256
    covered = {**parsed, "approval": PROPOSED_APPROVAL}
    covered_bytes = (json.dumps(covered, indent=2, sort_keys=True) + "\n").encode()
    assert hashlib.sha256(covered_bytes).hexdigest() == PROPOSED_DELTA_SHA256
    for path in (OPS_MANIFEST_PATH, ENGINE_MANIFEST_PATH):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == MANIFEST_BYTES_SHA256
    assert MANIFEST_SHA256 == MANIFEST_BYTES_SHA256


def test_committed_delta_still_validates(delta, manifest):
    report = validate_iam_delta(load_iam_delta(DELTA_PATH), manifest)
    assert report["ok"] is True
    assert report["identities_unbound"] == []
    before = validate_iam_delta(delta, manifest)
    assert report["bindings"] == before["bindings"] + len(AMENDMENT_E_BINDINGS)


# 1. A function's routine authorization may carry no role.


def test_function_authorization_is_role_less_when_recorded_as_function(delta, manifest):
    _role_less(delta)
    report = validate_iam_delta(delta, manifest)
    assert report["routine_authorizations"] == len(delta["routine_authorizations"])


def test_role_less_authorization_without_recorded_type_refuses(delta, manifest):
    # The fn_ prefix is not evidence of a function.
    _authorization(delta, FN)["role"] = None
    _refuses(delta, manifest, "iam_delta_invalid")


@pytest.mark.parametrize(
    "routine_type",
    ("PROCEDURE", "TABLE_VALUED_FUNCTION", "scalar_function", "", None, 1),
)
def test_role_less_authorization_with_another_type_refuses(
    delta, manifest, routine_type
):
    # The pinned type of the canonical JSON function is SCALAR_FUNCTION; a
    # recorded type that disagrees with it is refused, not believed.
    _role_less(delta, routine_type=routine_type)
    _refuses(delta, manifest, "iam_delta_role_unknown")


def test_role_less_authorization_on_a_procedure_refuses_whatever_it_records(
    delta, manifest
):
    _role_less(
        delta,
        routine=_routine("sp_read_open_intelligence_execution_result_v2"),
        routine_type="SCALAR_FUNCTION",
    )
    _refuses(delta, manifest, "iam_delta_role_unknown")


@pytest.mark.parametrize(
    "role", ("roles/bigquery.routineDataViewer", "roles/bigquery.routineDataEditor")
)
def test_recorded_type_beside_a_role_refuses(delta, manifest, role):
    row = _authorization(delta, FN)
    row["role"] = role
    row["routine_type"] = "SCALAR_FUNCTION"
    _refuses(delta, manifest, "iam_delta_invalid")


def test_role_less_function_beside_a_role_for_it_refuses(delta, manifest):
    kept = deepcopy(_authorization(delta, FN))
    _role_less(delta)
    delta["routine_authorizations"].append(kept)
    _refuses(delta, manifest, "iam_delta_duplicate_binding")


def test_role_less_authorization_with_an_extra_key_refuses(delta, manifest):
    _role_less(delta)["note"] = "x"
    _refuses(delta, manifest, "iam_delta_invalid")


@pytest.mark.parametrize(
    "role,code",
    (
        ("", "iam_delta_invalid"),
        (" roles/bigquery.routineDataViewer", "iam_delta_invalid"),
        ("roles/bigquery.dataViewer", "iam_delta_role_unknown"),
        ("roles/bigquery.routineDataOwner", "iam_delta_role_unknown"),
    ),
)
def test_role_bearing_authorization_rules_are_unchanged(delta, manifest, role, code):
    _authorization(delta, FN)["role"] = role
    _refuses(delta, manifest, code)


def test_function_pin_matches_the_routine_sources(manifest):
    # Independent source: the SQL each function is installed from.
    kinds = {}
    for path in sorted(ROUTINE_SQL_DIR.glob("*.sql")):
        match = re.search(
            r"^CREATE OR REPLACE (TABLE FUNCTION|FUNCTION|PROCEDURE) `\{project\}\."
            r"\{dataset\}\.([A-Za-z0-9_]+)`",
            path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if match:
            kinds[match.group(2)] = match.group(1)
    manifest_routines = {
        row["name"] for row in manifest["resources"] if "/routines/" in row["name"]
    }
    functions = {
        _routine(name): (
            "TABLE_VALUED_FUNCTION" if kind == "TABLE FUNCTION" else "SCALAR_FUNCTION"
        )
        for name, kind in kinds.items()
        if kind != "PROCEDURE" and _routine(name) in manifest_routines
    }
    assert functions == {FN: "SCALAR_FUNCTION"}
    assert iam_delta._FUNCTION_ROUTINES == functions


def test_readback_matches_a_role_less_function_entry(delta, manifest):
    _role_less(delta)
    validate_iam_delta(delta, manifest)
    entry = {
        "routine": {
            "projectId": PROJECT,
            "datasetId": "trends_v2_staging_approvals",
            "routineId": "fn_is_canonical_execution_json_v1",
        }
    }
    report = diff_against_readback(
        delta, {"policies": {APPROVALS: {"access": [entry]}}}
    )
    assert report["present"] == [
        {
            "condition": None,
            "member": "routine:" + FN,
            "resource": APPROVALS,
            "role": None,
        }
    ]


@pytest.mark.parametrize(
    "entry",
    (
        {"userByEmail": "someone@example.com"},
        {"specialGroup": "projectReaders"},
        {"dataset": {"dataset": {"projectId": PROJECT, "datasetId": "x"}}},
        {"routine": {"routineId": "fn"}, "role": None},
        {"routine": {"routineId": "fn"}, "role": ""},
    ),
)
def test_readback_other_role_less_entries_still_refuse(delta, entry):
    with pytest.raises(ValueError, match="^iam_readback_invalid$"):
        diff_against_readback(delta, {"policies": {APPROVALS: {"access": [entry]}}})


@pytest.mark.parametrize(
    "extra",
    ({"x": 1}, {"iamMember": "allUsers"}, {"domain": "example.com"}),
)
def test_readback_role_less_routine_entry_with_an_extra_key_refuses(delta, extra):
    # A role-less entry is a function's authorized routine only when the routine
    # is its one key; anything beside it is not believed.
    entry = {
        "routine": {
            "projectId": PROJECT,
            "datasetId": "trends_v2_staging_approvals",
            "routineId": "fn_is_canonical_execution_json_v1",
        },
        **extra,
    }
    with pytest.raises(ValueError, match="^iam_readback_invalid$"):
        diff_against_readback(delta, {"policies": {APPROVALS: {"access": [entry]}}})


# 2. The role map and the create snapshots only role.


def test_role_map_gains_exactly_the_amendment_e_roles():
    assert {
        role: iam_delta._ROLE_ACTION[role]
        for role in (
            OVERRIDES,
            OBJECT_USER,
            BUILDS_VIEWER,
            SNAPSHOT_ROLE,
            CAPTURE_JOB_ROLE,
        )
    } == {
        OVERRIDES: "invoke",
        OBJECT_USER: "write",
        BUILDS_VIEWER: "read",
        SNAPSHOT_ROLE: "write",
        CAPTURE_JOB_ROLE: "read",
    }
    assert len(iam_delta._ROLE_ACTION) == 21
    assert iam_delta._ROUTINE_AUTH_ROLES == {
        "roles/bigquery.routineDataEditor",
        "roles/bigquery.routineDataViewer",
    }


def test_snapshot_role_permission_list_is_pinned():
    assert iam_delta.CUSTOM_ROLE_PERMISSIONS == {
        SNAPSHOT_ROLE: SNAPSHOT_PERMISSIONS,
        CAPTURE_JOB_ROLE: CAPTURE_JOB_PERMISSIONS,
    }


def _role_definition(**changes):
    definition = {
        "name": SNAPSHOT_ROLE,
        "includedPermissions": list(SNAPSHOT_PERMISSIONS),
        "stage": "GA",
        "etag": "BwX=",
    }
    definition.update(changes)
    return definition


def test_snapshot_role_definition_matching_the_pin_is_accepted():
    shuffled = list(reversed(SNAPSHOT_PERMISSIONS))
    assert iam_delta.validate_custom_role_definition(
        SNAPSHOT_ROLE, _role_definition(includedPermissions=shuffled)
    ) == {"role": SNAPSHOT_ROLE, "permissions": list(SNAPSHOT_PERMISSIONS)}


@pytest.mark.parametrize(
    "changes",
    (
        {"includedPermissions": [*SNAPSHOT_PERMISSIONS, "bigquery.tables.delete"]},
        {"includedPermissions": [*SNAPSHOT_PERMISSIONS, "bigquery.tables.updateData"]},
        {"includedPermissions": list(SNAPSHOT_PERMISSIONS[:3])},
        {"includedPermissions": [*SNAPSHOT_PERMISSIONS, SNAPSHOT_PERMISSIONS[0]]},
        {"includedPermissions": "bigquery.tables.create"},
        {"name": f"projects/{PROJECT}/roles/SourceSnapshotCreate2"},
        {"name": "projects/other/roles/SourceSnapshotCreate"},
        {"stage": "DISABLED"},
        {"deleted": True},
    ),
)
def test_snapshot_role_definition_off_the_pin_refuses(changes):
    with pytest.raises(ValueError, match="^iam_custom_role_mismatch$"):
        iam_delta.validate_custom_role_definition(
            SNAPSHOT_ROLE, _role_definition(**changes)
        )


def test_unpinned_custom_role_definition_refuses():
    name = f"projects/{PROJECT}/roles/QuestionControlReplace"
    with pytest.raises(ValueError, match="^iam_custom_role_mismatch$"):
        iam_delta.validate_custom_role_definition(name, _role_definition(name=name))


def _snapshot_obligation(permissions=SNAPSHOT_PERMISSIONS):
    return {
        "statement": (
            "Step 6 reads the SourceSnapshotCreate definition back and checks it "
            "against the pinned list: " + ", ".join(permissions) + "."
        ),
        "topic": SNAPSHOT_TOPIC,
    }


def test_orchestration_may_create_snapshots_in_the_sources_dataset(delta, manifest):
    delta["bindings"].append(_binding(ORCHESTRATION, SOURCES, SNAPSHOT_ROLE))
    delta["proof_obligations"].append(_snapshot_obligation())
    validate_iam_delta(delta, manifest)


def test_snapshot_role_binding_needs_its_definition_obligation(delta, manifest):
    delta["bindings"].append(_binding(ORCHESTRATION, SOURCES, SNAPSHOT_ROLE))
    _refuses(delta, manifest, "iam_delta_proof_obligation_missing")


@pytest.mark.parametrize("dropped", range(len(SNAPSHOT_PERMISSIONS)))
def test_snapshot_obligation_names_every_pinned_permission(delta, manifest, dropped):
    permissions = [p for i, p in enumerate(SNAPSHOT_PERMISSIONS) if i != dropped]
    delta["bindings"].append(_binding(ORCHESTRATION, SOURCES, SNAPSHOT_ROLE))
    delta["proof_obligations"].append(_snapshot_obligation(permissions))
    _refuses(delta, manifest, "iam_delta_proof_obligation_missing")


def test_snapshot_obligation_alone_binds_nothing(delta, manifest):
    delta["proof_obligations"].append(_snapshot_obligation())
    report = validate_iam_delta(delta, manifest)
    assert SNAPSHOT_ROLE not in {row["role"] for row in delta["bindings"]}
    assert report["proof_obligations"] == len(delta["proof_obligations"])


def _snapshot_bound(delta):
    delta["bindings"].append(_binding(ORCHESTRATION, SOURCES, SNAPSHOT_ROLE))
    delta["proof_obligations"].append(_snapshot_obligation())
    return delta


def test_readback_without_the_bound_role_definition_leaves_it_unproven(delta):
    report = diff_against_readback(_snapshot_bound(delta), {"policies": {}})
    assert {
        "expected": 1,
        "reason": "role_definition_unread",
        "resource": SNAPSHOT_ROLE,
    } in report["unproven"]


def test_readback_with_the_pinned_role_definition_proves_it(delta):
    report = diff_against_readback(
        _snapshot_bound(delta),
        {"policies": {}, "roles": {SNAPSHOT_ROLE: _role_definition()}},
    )
    assert SNAPSHOT_ROLE not in {row["resource"] for row in report["unproven"]}


@pytest.mark.parametrize(
    "roles",
    (
        {SNAPSHOT_ROLE: {"name": SNAPSHOT_ROLE, "includedPermissions": []}},
        {
            SNAPSHOT_ROLE: {
                "name": SNAPSHOT_ROLE,
                "includedPermissions": [
                    *SNAPSHOT_PERMISSIONS,
                    "bigquery.tables.updateData",
                ],
            }
        },
        {f"projects/{PROJECT}/roles/QuestionControlReplace": {"name": "x"}},
    ),
)
def test_readback_with_an_off_pin_role_definition_refuses(delta, roles):
    with pytest.raises(ValueError, match="^iam_custom_role_mismatch$"):
        diff_against_readback(_snapshot_bound(delta), {"policies": {}, "roles": roles})


@pytest.mark.parametrize("roles", ([], "x", {SNAPSHOT_ROLE: []}))
def test_readback_role_definitions_must_be_a_mapping(delta, roles):
    with pytest.raises(
        ValueError, match="^iam_(readback_invalid|custom_role_mismatch)$"
    ):
        diff_against_readback(delta, {"policies": {}, "roles": roles})


@pytest.mark.parametrize(
    "member,resource,condition,code",
    (
        (ORCHESTRATION, STAGING, None, "iam_delta_role_scope"),
        (ORCHESTRATION, APPROVALS, None, "iam_delta_role_scope"),
        (BRAIN, SOURCES, None, "iam_delta_role_scope"),
        (INGEST, SOURCES, None, "iam_delta_role_scope"),
        (
            ORCHESTRATION,
            SOURCES,
            "request.time < timestamp('2027-01-01T00:00:00Z')",
            "iam_delta_role_scope",
        ),
    ),
)
def test_snapshot_role_outside_its_one_row_refuses(
    delta, manifest, member, resource, condition, code
):
    delta["bindings"].append(_binding(member, resource, SNAPSHOT_ROLE, condition))
    _refuses(delta, manifest, code)


def test_daily_job_may_run_itself_with_overrides(delta, manifest):
    delta["bindings"].append(_binding(ORCHESTRATION, DAILY_JOB, OVERRIDES))
    validate_iam_delta(delta, manifest)


@pytest.mark.parametrize(
    "member,resource,condition",
    (
        (ORCHESTRATION, BRAIN_JOB, None),
        (BRAIN, BRAIN_JOB, None),
        (BRAIN, DAILY_JOB, None),
        (SCHEDULER, DAILY_JOB, None),
        (ORCHESTRATION, PROJECT_RESOURCE, None),
        (ORCHESTRATION, DAILY_JOB, "request.time < timestamp('2027-01-01T00:00:00Z')"),
    ),
)
def test_run_with_overrides_outside_its_one_row_refuses(
    delta, manifest, member, resource, condition
):
    delta["bindings"].append(_binding(member, resource, OVERRIDES, condition))
    code = (
        "iam_delta_project_level_forbidden"
        if resource == PROJECT_RESOURCE
        else "iam_delta_role_scope"
    )
    _refuses(delta, manifest, code)


# 3. Build history at project level for exactly four identities.


@pytest.mark.parametrize("member", BUILD_READERS)
def test_build_readers_may_view_builds_at_project_level(delta, manifest, member):
    delta["bindings"].append(_binding(member, PROJECT_RESOURCE, BUILDS_VIEWER))
    validate_iam_delta(delta, manifest)


def test_all_four_build_readers_together_validate(delta, manifest):
    for member in BUILD_READERS:
        delta["bindings"].append(_binding(member, PROJECT_RESOURCE, BUILDS_VIEWER))
    report = validate_iam_delta(delta, manifest)
    assert report["identities_unbound"] == ["app"]


@pytest.mark.parametrize("member", (FRESHNESS, QA, INGEST, SCHEDULER, PRICE_POLICY))
def test_other_runtime_identities_stay_off_project_level(delta, manifest, member):
    delta["bindings"].append(_binding(member, PROJECT_RESOURCE, BUILDS_VIEWER))
    _refuses(delta, manifest, "iam_delta_project_level_forbidden")


@pytest.mark.parametrize("member", (BUILD, DEPLOY))
def test_deployment_identities_do_not_gain_build_history(delta, manifest, member):
    delta["bindings"].append(_binding(member, PROJECT_RESOURCE, BUILDS_VIEWER))
    _refuses(delta, manifest, "iam_delta_role_scope")


def test_app_does_not_gain_build_history(delta, manifest):
    delta["bindings"].append(_binding(APP, PROJECT_RESOURCE, BUILDS_VIEWER))
    _refuses(delta, manifest, "iam_delta_app_regrant")


@pytest.mark.parametrize(
    "role",
    ("roles/run.viewer", "roles/cloudbuild.builds.editor", VIEWER, OVERRIDES),
)
def test_build_readers_gain_no_other_project_level_role(delta, manifest, role):
    delta["bindings"].append(_binding(BRAIN, PROJECT_RESOURCE, role))
    _refuses(delta, manifest, "iam_delta_project_level_forbidden")


@pytest.mark.parametrize(
    "resource,condition",
    (
        (DAILY_JOB, None),
        (STAGING, None),
        (PROJECT_RESOURCE, "request.time < timestamp('2027-01-01T00:00:00Z')"),
    ),
)
def test_build_history_outside_the_project_row_refuses(
    delta, manifest, resource, condition
):
    delta["bindings"].append(
        _binding(ORCHESTRATION, resource, BUILDS_VIEWER, condition)
    )
    code = "iam_delta_project_level_forbidden" if condition else "iam_delta_role_scope"
    _refuses(delta, manifest, code)


# 4. The approvals bucket daily prefix.


def test_orchestration_may_use_objects_under_the_daily_prefix(delta, manifest):
    delta["bindings"].append(
        _binding(ORCHESTRATION, APPROVALS_BUCKET, OBJECT_USER, DAILY_USER_CONDITION)
    )
    validate_iam_delta(delta, manifest)


def test_object_user_on_the_whole_daily_prefix_refuses(delta, manifest):
    # The whole prefix would let the daily account delete its create once records,
    # the operator's grant revocations and anything a later prefix holds.
    delta["bindings"].append(
        _binding(ORCHESTRATION, APPROVALS_BUCKET, OBJECT_USER, DAILY_CONDITION)
    )
    _refuses(delta, manifest, "iam_delta_condition_not_allowlisted")


def _daily_rows():
    return [
        _binding(ORCHESTRATION, APPROVALS_BUCKET, OBJECT_USER, DAILY_USER_CONDITION),
        _binding(
            ORCHESTRATION,
            APPROVALS_BUCKET,
            OBJECT_CREATOR,
            DAILY_RECORD_CREATE_CONDITION,
        ),
        _binding(
            ORCHESTRATION, APPROVALS_BUCKET, OBJECT_VIEWER, DAILY_RECORD_READ_CONDITION
        ),
    ]


def test_the_three_daily_rows_together_validate(delta, manifest):
    delta["bindings"].extend(_daily_rows())
    report = validate_iam_delta(delta, manifest)
    assert report["identities_unbound"] == ["app"]


@pytest.mark.parametrize(
    "condition",
    (
        DAILY_DENY_LIST_CONDITION,
        DAILY_USER_CONDITION.replace(" && resource.name.endsWith('/control.json')", ""),
        DAILY_USER_CONDITION.replace("endsWith('/control.json')", "endsWith('.json')"),
        DAILY_USER_CONDITION.replace("collection/days/", "collection/"),
        DAILY_USER_CONDITION.replace("slots/", ""),
        DAILY_USER_CONDITION.replace(" || ", " && "),
        DAILY_USER_CONDITION.replace(
            "storage.googleapis.com/Object' && (",
            "storage.googleapis.com/Object' || (",
        ),
        DAILY_USER_CONDITION + " || true",
        "(" + DAILY_USER_CONDITION + ")",
        DAILY_RECORD_CREATE_CONDITION,
        DAILY_RECORD_READ_CONDITION,
    ),
)
def test_daily_object_user_condition_is_exact(delta, manifest, condition):
    delta["bindings"].append(
        _binding(ORCHESTRATION, APPROVALS_BUCKET, OBJECT_USER, condition)
    )
    _refuses(delta, manifest, "iam_delta_condition_not_allowlisted")


@pytest.mark.parametrize(
    "role,condition",
    (
        (OBJECT_CREATOR, DAILY_RECORD_READ_CONDITION),
        (OBJECT_CREATOR, DAILY_USER_CONDITION),
        (OBJECT_CREATOR, DAILY_RECORD_CREATE_CONDITION.replace(" || ", " && ")),
        (
            OBJECT_CREATOR,
            DAILY_RECORD_CREATE_CONDITION.replace(
                "storage.googleapis.com/Object' && (",
                "storage.googleapis.com/Object' || (",
            ),
        ),
        (OBJECT_VIEWER, DAILY_RECORD_CREATE_CONDITION),
        (OBJECT_VIEWER, DAILY_USER_CONDITION),
        (
            OBJECT_CREATOR,
            DAILY_RECORD_CREATE_CONDITION.replace(
                f" || resource.name.startsWith('{DAILY_OBJECTS}execution-observations/')",
                "",
            ),
        ),
        (
            OBJECT_VIEWER,
            DAILY_RECORD_READ_CONDITION.replace(
                f" || resource.name.startsWith('{DAILY_OBJECTS}workload-rates/')", ""
            ),
        ),
        (
            OBJECT_VIEWER,
            DAILY_RECORD_READ_CONDITION.replace(
                "grants/')",
                "grants/') || resource.name.startsWith('" + DAILY_OBJECTS + "new/')",
            ),
        ),
        ("roles/storage.objectAdmin", DAILY_RECORD_CREATE_CONDITION),
    ),
)
def test_daily_record_conditions_are_exact_per_role(delta, manifest, role, condition):
    delta["bindings"].append(_binding(ORCHESTRATION, APPROVALS_BUCKET, role, condition))
    code = (
        "iam_delta_role_unknown"
        if role == "roles/storage.objectAdmin"
        else "iam_delta_condition_not_allowlisted"
    )
    _refuses(delta, manifest, code)


@pytest.mark.parametrize("member", (BRAIN, FUNDED, FRESHNESS, MIGRATION, INGEST))
@pytest.mark.parametrize("index", (0, 1, 2))
def test_daily_rows_are_the_daily_account_only(delta, manifest, member, index):
    row = _daily_rows()[index]
    row["member"] = member
    delta["bindings"].append(row)
    # The ingestion identity's own two row shape refuses it first.
    code = "iam_delta_ingestion_scope" if member == INGEST and index else None
    _refuses(delta, manifest, code or "iam_delta_role_scope")


@pytest.mark.parametrize(
    "condition",
    (
        DAILY_CONDITION.replace("42/daily/'", "42/daily'"),
        DAILY_CONDITION.replace("42/daily/'", "42/'"),
        DAILY_CONDITION.replace("42/daily/'", "'"),
        DAILY_CONDITION.replace("42/daily/'", "42/dailyx/'"),
        DAILY_CONDITION.replace("42/daily/'", "42/daily/x/'"),
        DAILY_CONDITION.replace("42/daily/'", "142/daily/'"),
        DAILY_CONDITION.replace("approvals-staging/", "approvals-staging-2/"),
        DAILY_CONDITION.replace("approvals-staging/", "approvals-stagingx/"),
        DAILY_CONDITION.replace(
            "ogilvy-trends-v2-execution-approvals-staging",
            "listening-post-staging-cache",
        ),
        DAILY_CONDITION + " || true",
        "true || " + DAILY_CONDITION,
        DAILY_CONDITION.replace(".startsWith(", ".endsWith("),
        DAILY_CONDITION.replace(
            "storage.googleapis.com/Object", "storage.googleapis.com/Bucket"
        ),
        " " + DAILY_CONDITION.strip(),
        ALERT_CONDITION,
    ),
)
def test_daily_prefix_match_is_exact(delta, manifest, condition):
    delta["bindings"].append(
        _binding(ORCHESTRATION, APPROVALS_BUCKET, OBJECT_USER, condition)
    )
    code = "iam_delta_invalid" if condition != condition.strip() else None
    _refuses(delta, manifest, code or "iam_delta_condition_not_allowlisted")


@pytest.mark.parametrize("suffix", ("-2", "x", "-staging"))
def test_a_longer_bucket_name_is_not_the_approvals_bucket(delta, manifest, suffix):
    bucket = APPROVALS_BUCKET + suffix
    condition = DAILY_CONDITION.replace(
        "approvals-staging/", "approvals-staging" + suffix + "/"
    )
    delta["bindings"].append(_binding(ORCHESTRATION, bucket, OBJECT_USER, condition))
    _refuses(delta, manifest, "iam_delta_resource_unknown")


def test_whole_approvals_bucket_refuses(delta, manifest):
    delta["bindings"].append(_binding(ORCHESTRATION, APPROVALS_BUCKET, OBJECT_USER))
    _refuses(delta, manifest, "iam_delta_condition_required")


@pytest.mark.parametrize(
    "member,role",
    (
        (ORCHESTRATION, "roles/storage.objectViewer"),
        (ORCHESTRATION, "roles/storage.objectCreator"),
        (BRAIN, "roles/storage.objectViewer"),
        (FRESHNESS, "roles/storage.objectCreator"),
    ),
)
def test_daily_prefix_opens_for_no_other_role(delta, manifest, member, role):
    delta["bindings"].append(_binding(member, APPROVALS_BUCKET, role, DAILY_CONDITION))
    _refuses(delta, manifest, "iam_delta_condition_not_allowlisted")


@pytest.mark.parametrize(
    "member,resource,condition",
    (
        (BRAIN, APPROVALS_BUCKET, DAILY_CONDITION),
        (FUNDED, APPROVALS_BUCKET, DAILY_CONDITION),
        (FRESHNESS, CACHE_BUCKET, ALERT_CONDITION),
        (ORCHESTRATION, CACHE_BUCKET, ALERT_CONDITION),
    ),
)
def test_object_user_outside_its_one_row_refuses(
    delta, manifest, member, resource, condition
):
    delta["bindings"].append(_binding(member, resource, OBJECT_USER, condition))
    _refuses(delta, manifest, "iam_delta_role_scope")


# 5. The managed pricing renewal job's object reads and create once writes.

RENEWAL_ROOT = (
    "projects/_/buckets/listening-post-staging-cache/objects/"
    "open-intelligence/v2/staging/general-questions/"
)
OBJECT_CREATOR = "roles/storage.objectCreator"
OBJECT_VIEWER = "roles/storage.objectViewer"


def _under(prefix):
    return OBJECT_HEAD + f".startsWith('{RENEWAL_ROOT}{prefix}')"


def _exactly(name):
    return OBJECT_HEAD + f" == '{RENEWAL_ROOT}{name}'"


RENEWAL_READS = (
    _exactly("renewal/release-index.json"),
    _exactly("renewal/unattended-grant.json"),
    _under("renewal/sources/"),
    _under("policies/"),
    _under("deployments/"),
)
RENEWAL_CREATES = (
    _under("renewal/sources/"),
    _under("policies/"),
    _under("deployments/"),
)


def _renewal_rows():
    return [
        *(
            _binding(PRICE_POLICY, CACHE_BUCKET, OBJECT_VIEWER, c)
            for c in RENEWAL_READS
        ),
        *(
            _binding(PRICE_POLICY, CACHE_BUCKET, OBJECT_CREATOR, c)
            for c in RENEWAL_CREATES
        ),
    ]


@pytest.mark.parametrize("index", range(8))
def test_price_policy_may_hold_each_renewal_object_row(delta, manifest, index):
    delta["bindings"].append(_renewal_rows()[index])
    validate_iam_delta(delta, manifest)


def test_the_eight_renewal_rows_together_validate(delta, manifest):
    before = validate_iam_delta(deepcopy(delta), manifest)
    delta["bindings"].extend(_renewal_rows())
    after = validate_iam_delta(delta, manifest)
    assert after["bindings"] == before["bindings"] + 8


@pytest.mark.parametrize(
    "role,condition,code",
    (
        (OBJECT_VIEWER, _under("renewal/"), "iam_delta_condition_not_allowlisted"),
        (OBJECT_VIEWER, _under(""), "iam_delta_condition_not_allowlisted"),
        (OBJECT_CREATOR, _under("renewal/"), "iam_delta_condition_not_allowlisted"),
        (
            OBJECT_CREATOR,
            _exactly("renewal/release-index.json"),
            "iam_delta_condition_not_allowlisted",
        ),
        (
            OBJECT_CREATOR,
            _exactly("renewal/unattended-grant.json"),
            "iam_delta_condition_not_allowlisted",
        ),
        (OBJECT_VIEWER, _under("policies"), "iam_delta_condition_not_allowlisted"),
        (
            OBJECT_VIEWER,
            _under("policies/") + " || true",
            "iam_delta_condition_not_allowlisted",
        ),
        ("roles/storage.objectUser", _under("policies/"), "iam_delta_role_scope"),
        (
            f"projects/{PROJECT}/roles/QuestionControlReplace",
            _under("policies/"),
            "iam_delta_condition_not_allowlisted",
        ),
    ),
)
def test_renewal_object_rows_are_exact(delta, manifest, role, condition, code):
    delta["bindings"].append(_binding(PRICE_POLICY, CACHE_BUCKET, role, condition))
    _refuses(delta, manifest, code)


@pytest.mark.parametrize("member", (FRESHNESS, ORCHESTRATION, BRAIN, QA, APP))
@pytest.mark.parametrize("index", range(8))
def test_renewal_object_rows_are_the_price_policy_account_only(
    delta, manifest, member, index
):
    row = _renewal_rows()[index]
    row["member"] = member
    delta["bindings"].append(row)
    code = "iam_delta_app_regrant" if member == APP else "iam_delta_role_scope"
    _refuses(delta, manifest, code)


# 6. One pinned human operator on the operator routines.


def _migration_operator_routines():
    tree = ast.parse(MIGRATION_PATH.read_text(encoding="utf-8"))
    names = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                names[target.id] = node.value
    items = []
    for element in names["V2_OPERATOR_ROUTINES"].elts:
        if isinstance(element, ast.Name):
            element = names[element.id]
        assert isinstance(element, ast.Constant) and type(element.value) is str
        items.append(element.value)
    return items


def test_operator_routines_are_the_migration_list_and_the_recovery_routine():
    migration = _migration_operator_routines()
    assert len(migration) == len(set(migration))
    assert set(migration) <= set(OPERATOR_ROUTINES)
    assert set(OPERATOR_ROUTINES) == set(migration) | {RECONCILE}
    assert iam_delta._OPERATOR_MEMBER == OPERATOR
    assert iam_delta._OPERATOR_ROUTINES == frozenset(
        _routine(name) for name in OPERATOR_ROUTINES
    )


@pytest.mark.parametrize("name", OPERATOR_ROUTINES)
def test_operator_may_view_each_operator_routine(delta, manifest, name):
    delta["bindings"].append(_binding(OPERATOR, _routine(name), VIEWER))
    report = validate_iam_delta(delta, manifest)
    assert "human_operator" not in report["identities_bound"]
    assert report["identities_unbound"] == ["app"]


def test_operator_rows_together_validate_and_bind_no_identity(delta, manifest):
    before = validate_iam_delta(deepcopy(delta), manifest)
    for name in OPERATOR_ROUTINES:
        delta["bindings"].append(_binding(OPERATOR, _routine(name), VIEWER))
    after = validate_iam_delta(delta, manifest)
    assert after["identities_bound"] == before["identities_bound"]
    assert after["bindings"] == before["bindings"] + 8


def test_operator_on_the_recovery_routine_validates_now_the_manifest_names_it(
    delta, manifest
):
    assert _routine(RECONCILE) in {row["name"] for row in manifest["resources"]}
    delta["bindings"].append(_binding(OPERATOR, _routine(RECONCILE), VIEWER))
    validate_iam_delta(delta, manifest)


def test_operator_on_the_recovery_routine_refuses_without_its_manifest_row(
    delta, manifest
):
    routine = _routine(RECONCILE)
    shrunk = deepcopy(manifest)
    shrunk["resources"] = [row for row in shrunk["resources"] if row["name"] != routine]
    canonical = json.dumps(shrunk, sort_keys=True, separators=(",", ":")).encode()
    delta["resource_manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
    delta["create"] = [row for row in delta["create"] if row["resource"] != routine]
    delta["routine_authorizations"] = [
        row for row in delta["routine_authorizations"] if row["routine"] != routine
    ]
    validate_iam_delta(deepcopy(delta), shrunk)
    delta["bindings"].append(_binding(OPERATOR, routine, VIEWER))
    _refuses(delta, shrunk, "iam_delta_production_target")


@pytest.mark.parametrize(
    "member",
    (
        "user:someone@ogilvy.co.za",
        "user:Albert.Meintjes@ogilvy.co.za",
        "user:albert.meintjes@ogilvy.co.za.evil.example",
        "user:albert.meintjes@ogilvy.co",
        "group:albert.meintjes@ogilvy.co.za",
        "domain:ogilvy.co.za",
        "serviceAccount:albert.meintjes@ogilvy.co.za",
    ),
)
def test_any_other_human_refuses(delta, manifest, member):
    delta["bindings"].append(
        _binding(member, _routine("sp_approve_open_intelligence_execution_v2"), VIEWER)
    )
    _refuses(delta, manifest, "iam_delta_member_unknown")


@pytest.mark.parametrize(
    "resource,role,condition",
    (
        (_routine("sp_consume_open_intelligence_execution_v2"), VIEWER, None),
        (_routine("sp_read_open_intelligence_execution_approval_v2"), VIEWER, None),
        (_routine("sp_approve_open_intelligence_execution_v1"), VIEWER, None),
        (STAGING, VIEWER, None),
        (APPROVALS, VIEWER, None),
        (
            _routine("sp_approve_open_intelligence_execution_v2"),
            "roles/bigquery.dataEditor",
            None,
        ),
        (
            _routine("sp_approve_open_intelligence_execution_v2"),
            VIEWER,
            "request.time < timestamp('2027-01-01T00:00:00Z')",
        ),
        (PROJECT_RESOURCE, BUILDS_VIEWER, None),
        (DAILY_JOB, "roles/run.invoker", None),
    ),
)
def test_operator_outside_the_operator_rows_refuses(
    delta, manifest, resource, role, condition
):
    delta["bindings"].append(_binding(OPERATOR, resource, role, condition))
    _refuses(delta, manifest, "iam_delta_member_unknown")


def test_operator_row_with_a_precedent_label_refuses(delta, manifest):
    row = _binding(
        OPERATOR,
        _routine("sp_approve_open_intelligence_execution_v2"),
        VIEWER,
        purpose="manifest_precedent: none",
    )
    delta["bindings"].append(row)
    _refuses(delta, manifest, "iam_delta_purpose_unlabelled")


def test_operator_is_still_unknown_in_unresolved_members(delta, manifest):
    delta["unresolved"][0]["members"].append(OPERATOR)
    _refuses(delta, manifest, "iam_delta_member_unknown")


# Final review pins: the collection dataset row, the operator routines and the key.
SOCIALCRAWL_KEY = (
    f"//secretmanager.googleapis.com/projects/{PROJECT}/secrets/"
    "SOCIALCRAWL_OGILVY_API_KEY/versions/"
)


@pytest.mark.parametrize(
    "role",
    (
        "roles/bigquery.dataEditor",
        "roles/bigquery.dataOwner",
        "roles/bigquery.metadataViewer",
    ),
)
def test_orchestration_holds_only_the_read_only_role_on_the_sources_dataset(
    delta, manifest, role
):
    delta["bindings"].append(_binding(ORCHESTRATION, SOURCES, role))
    _refuses(delta, manifest, "iam_delta_role_scope")


def test_orchestration_read_only_on_the_sources_dataset_still_validates(
    delta, manifest
):
    delta["bindings"].append(_binding(ORCHESTRATION, SOURCES, VIEWER))
    validate_iam_delta(delta, manifest)


OPERATOR_ONLY = [
    name
    for name in OPERATOR_ROUTINES
    if name
    not in (
        "fn_is_canonical_execution_json_v1",
        "sp_read_open_intelligence_recurring_grant_v2",
    )
]


@pytest.mark.parametrize("name", OPERATOR_ONLY)
@pytest.mark.parametrize(
    "member", (BRAIN, FUNDED, MIGRATION, ORCHESTRATION, FRESHNESS, QA)
)
def test_no_service_account_is_bound_on_an_operator_only_routine(
    delta, manifest, name, member
):
    delta["bindings"].append(_binding(member, _routine(name), VIEWER))
    _refuses(delta, manifest, "iam_delta_role_scope")


@pytest.mark.parametrize("member", (FRESHNESS, QA))
def test_only_the_runtime_readers_view_the_canonical_json_function(
    delta, manifest, member
):
    delta["bindings"].append(_binding(member, FN, VIEWER))
    _refuses(delta, manifest, "iam_delta_role_scope")


@pytest.mark.parametrize("member", (BRAIN, FUNDED, MIGRATION, FRESHNESS))
def test_only_orchestration_views_the_recurring_grant_read_routine(
    delta, manifest, member
):
    routine = _routine("sp_read_open_intelligence_recurring_grant_v2")
    delta["bindings"].append(_binding(member, routine, VIEWER))
    _refuses(delta, manifest, "iam_delta_role_scope")


def test_the_named_service_rows_on_operator_routines_still_validate(delta, manifest):
    for member in (BRAIN, FUNDED, MIGRATION):
        delta["bindings"].append(_binding(member, FN, VIEWER))
    delta["bindings"].append(
        _binding(
            ORCHESTRATION,
            _routine("sp_read_open_intelligence_recurring_grant_v2"),
            VIEWER,
        )
    )
    validate_iam_delta(delta, manifest)


@pytest.mark.parametrize("version", ("2", "latest", "0"))
def test_a_socialcrawl_key_binding_at_another_version_refuses(delta, manifest, version):
    """Pinned, not new: the manifest names version 1 only, so another version is not
    a manifest row, and a version 1 row whose condition names another version fails
    the exact condition."""
    delta["bindings"].append(
        _binding(
            FUNDED,
            SOCIALCRAWL_KEY + version,
            "roles/secretmanager.secretAccessor",
            "resource.type == 'secretmanager.googleapis.com/SecretVersion' && "
            f"resource.name == 'projects/590353929363/secrets/"
            f"SOCIALCRAWL_OGILVY_API_KEY/versions/{version}'",
        )
    )
    _refuses(delta, manifest, "iam_delta_resource_unknown")
    delta["bindings"][-1]["resource"] = SOCIALCRAWL_KEY + "1"
    _refuses(delta, manifest, "iam_delta_condition_not_allowlisted")


KEY_CONDITION = (
    "resource.type == 'secretmanager.googleapis.com/SecretVersion' && "
    "resource.name == 'projects/590353929363/secrets/SOCIALCRAWL_OGILVY_API_KEY/versions/1'"
)


@pytest.mark.parametrize(
    "member", (BRAIN, MIGRATION, FRESHNESS, ORCHESTRATION, INGEST, QA)
)
def test_only_the_funded_account_may_hold_the_socialcrawl_key(delta, manifest, member):
    delta["bindings"].append(
        _binding(
            member,
            SOCIALCRAWL_KEY + "1",
            "roles/secretmanager.secretAccessor",
            KEY_CONDITION,
        )
    )
    with pytest.raises(ValueError, match="^iam_delta_(role|ingestion|qa)_scope$"):
        validate_iam_delta(delta, manifest)


def test_the_funded_socialcrawl_key_row_still_validates(delta, manifest):
    rows = [
        row for row in delta["bindings"] if row["resource"] == SOCIALCRAWL_KEY + "1"
    ]
    assert [(row["member"], row["condition"]) for row in rows] == [
        (FUNDED, KEY_CONDITION)
    ]
    validate_iam_delta(delta, manifest)


# 10. The serving identity reads the capture jobs through a jobs.get only role.


def _capture_job_obligation(permissions=CAPTURE_JOB_PERMISSIONS):
    return {
        "statement": (
            "The CaptureJobRead definition is read back and checked against the "
            "pinned list: " + ", ".join(permissions) + "."
        ),
        "topic": CAPTURE_JOB_TOPIC,
    }


def _without_capture_job_obligation(delta):
    delta["proof_obligations"] = [
        row for row in delta["proof_obligations"] if row["topic"] != CAPTURE_JOB_TOPIC
    ]


def test_the_capture_job_read_role_holds_jobs_get_only():
    assert iam_delta.CUSTOM_ROLE_PERMISSIONS[CAPTURE_JOB_ROLE] == ("bigquery.jobs.get",)
    assert iam_delta.validate_custom_role_definition(
        CAPTURE_JOB_ROLE,
        {"name": CAPTURE_JOB_ROLE, "includedPermissions": ["bigquery.jobs.get"]},
    ) == {"role": CAPTURE_JOB_ROLE, "permissions": ["bigquery.jobs.get"]}


@pytest.mark.parametrize(
    "extra",
    (
        "bigquery.jobs.list",
        "bigquery.jobs.listAll",
        "bigquery.jobs.update",
        "bigquery.jobs.delete",
        "bigquery.jobs.create",
    ),
)
def test_the_capture_job_read_definition_off_the_pin_refuses(extra):
    with pytest.raises(ValueError, match="^iam_custom_role_mismatch$"):
        iam_delta.validate_custom_role_definition(
            CAPTURE_JOB_ROLE,
            {
                "name": CAPTURE_JOB_ROLE,
                "includedPermissions": ["bigquery.jobs.get", extra],
            },
        )


def test_the_serving_identity_may_read_jobs_at_project_level(delta, manifest):
    delta["bindings"].append(_binding(APP, PROJECT_RESOURCE, CAPTURE_JOB_ROLE))
    delta["proof_obligations"].append(_capture_job_obligation())
    report = validate_iam_delta(delta, manifest)
    assert "app" in report["identities_bound"]
    assert report["identities_unbound"] == []


def test_the_capture_job_read_row_needs_its_definition_obligation(delta, manifest):
    _without_capture_job_obligation(delta)
    delta["bindings"].append(_binding(APP, PROJECT_RESOURCE, CAPTURE_JOB_ROLE))
    _refuses(delta, manifest, "iam_delta_proof_obligation_missing")
    delta["proof_obligations"].append(_capture_job_obligation(()))
    _refuses(delta, manifest, "iam_delta_proof_obligation_missing")


def test_the_snapshot_obligation_does_not_prove_the_capture_job_role(delta, manifest):
    _without_capture_job_obligation(delta)
    delta["bindings"].append(_binding(APP, PROJECT_RESOURCE, CAPTURE_JOB_ROLE))
    obligation = _snapshot_obligation((*SNAPSHOT_PERMISSIONS, "bigquery.jobs.get"))
    delta["proof_obligations"].append(obligation)
    _refuses(delta, manifest, "iam_delta_proof_obligation_missing")


@pytest.mark.parametrize(
    "member",
    (
        BRAIN,
        FUNDED,
        MIGRATION,
        ORCHESTRATION,
        FRESHNESS,
        QA,
        INGEST,
        SCHEDULER,
        PRICE_POLICY,
        BUILD,
        DEPLOY,
    ),
)
def test_only_the_serving_identity_may_hold_the_capture_job_role(
    delta, manifest, member
):
    delta["bindings"].append(_binding(member, PROJECT_RESOURCE, CAPTURE_JOB_ROLE))
    delta["proof_obligations"].append(_capture_job_obligation())
    with pytest.raises(
        ValueError,
        match="^iam_delta_(role_scope|project_level_forbidden|ingestion_scope"
        "|qa_scope|scheduler_scope|price_policy_scope|build_scope|deploy_scope)$",
    ):
        validate_iam_delta(delta, manifest)


@pytest.mark.parametrize("resource", (STAGING, APPROVALS, SOURCES))
def test_the_capture_job_role_is_project_level_only(delta, manifest, resource):
    delta["bindings"].append(_binding(APP, resource, CAPTURE_JOB_ROLE))
    delta["proof_obligations"].append(_capture_job_obligation())
    _refuses(delta, manifest, "iam_delta_app_regrant")


def test_the_capture_job_role_takes_no_condition(delta, manifest):
    delta["bindings"].append(
        _binding(
            APP,
            PROJECT_RESOURCE,
            CAPTURE_JOB_ROLE,
            "request.time < timestamp('2027-01-01T00:00:00Z')",
        )
    )
    delta["proof_obligations"].append(_capture_job_obligation())
    _refuses(delta, manifest, "iam_delta_app_regrant")


@pytest.mark.parametrize(
    "role",
    (
        "roles/bigquery.jobUser",
        "roles/bigquery.dataViewer",
        BUILDS_VIEWER,
        SNAPSHOT_ROLE,
    ),
)
def test_the_serving_identity_gains_no_other_project_role(delta, manifest, role):
    delta["bindings"].append(_binding(APP, PROJECT_RESOURCE, role))
    delta["proof_obligations"].append(_capture_job_obligation())
    delta["proof_obligations"].append(_snapshot_obligation())
    _refuses(delta, manifest, "iam_delta_app_regrant")


# 11. The serving identity reads the three v2 ledger tables and the funded pilot
# job's execution (round 7, the job moved in round 7b).

LEDGER_TABLES = tuple(
    APPROVALS + "/tables/open_intelligence_execution_" + name + "_v2"
    for name in ("approvals", "consumptions", "results")
)
LEDGER_TOPIC = "serving_ledger_table_reads"
INGEST_JOB = RUN_PARENT + "/jobs/intelligence-42-ingest-staging"
PILOT_JOB = RUN_PARENT + "/jobs/intelligence-42-funded-pilot-staging"
RUN_VIEWER = "roles/run.viewer"
EXECUTION_TOPIC = "serving_pilot_execution_read"
OTHER_MEMBERS = (
    BRAIN,
    FUNDED,
    MIGRATION,
    ORCHESTRATION,
    FRESHNESS,
    QA,
    INGEST,
    SCHEDULER,
    PRICE_POLICY,
    BUILD,
    DEPLOY,
)


def _without_topic(delta, topic):
    delta["proof_obligations"] = [
        row for row in delta["proof_obligations"] if row["topic"] != topic
    ]


def _refuses_any(delta, manifest):
    with pytest.raises(ValueError, match="^iam_delta_[a-z_]+$") as caught:
        validate_iam_delta(delta, manifest)
    return caught.value.args[0]


@pytest.mark.parametrize("table", LEDGER_TABLES)
def test_the_serving_identity_may_read_each_ledger_table(delta, manifest, table):
    delta["bindings"].append(_binding(APP, table, VIEWER))
    report = validate_iam_delta(delta, manifest)
    assert "app" in report["identities_bound"]


def test_the_three_ledger_table_rows_together_validate(delta, manifest):
    for table in LEDGER_TABLES:
        delta["bindings"].append(_binding(APP, table, VIEWER))
    validate_iam_delta(delta, manifest)


def test_a_ledger_table_row_needs_its_obligation(delta, manifest):
    _without_topic(delta, LEDGER_TOPIC)
    delta["bindings"].append(_binding(APP, LEDGER_TABLES[0], VIEWER))
    _refuses(delta, manifest, "iam_delta_proof_obligation_missing")


@pytest.mark.parametrize("member", OTHER_MEMBERS)
def test_only_the_serving_identity_may_read_a_ledger_table(delta, manifest, member):
    delta["bindings"].append(_binding(member, LEDGER_TABLES[0], VIEWER))
    _refuses(delta, manifest, "iam_delta_role_scope")


@pytest.mark.parametrize(
    "role", ("roles/bigquery.dataEditor", CAPTURE_JOB_ROLE, RUN_VIEWER)
)
def test_the_ledger_tables_take_data_viewer_alone(delta, manifest, role):
    delta["bindings"].append(_binding(APP, LEDGER_TABLES[1], role))
    _refuses(delta, manifest, "iam_delta_app_regrant")
    delta["bindings"][-1]["member"] = BRAIN
    assert _refuses_any(delta, manifest) != "iam_delta_invalid"


def test_a_ledger_table_row_takes_no_condition(delta, manifest):
    delta["bindings"].append(
        _binding(
            APP,
            LEDGER_TABLES[2],
            VIEWER,
            "request.time < timestamp('2027-01-01T00:00:00Z')",
        )
    )
    _refuses(delta, manifest, "iam_delta_app_regrant")


def test_the_serving_identity_still_gets_no_ledger_dataset_row(delta, manifest):
    delta["bindings"].append(_binding(APP, APPROVALS, VIEWER))
    _refuses(delta, manifest, "iam_delta_app_regrant")


def test_the_serving_identity_may_view_the_pilot_job(delta, manifest):
    delta["bindings"].append(_binding(APP, PILOT_JOB, RUN_VIEWER))
    report = validate_iam_delta(delta, manifest)
    assert "app" in report["identities_bound"]


def test_the_pilot_job_view_needs_its_obligation(delta, manifest):
    _without_topic(delta, EXECUTION_TOPIC)
    delta["bindings"].append(_binding(APP, PILOT_JOB, RUN_VIEWER))
    _refuses(delta, manifest, "iam_delta_proof_obligation_missing")


@pytest.mark.parametrize(
    "member", tuple(member for member in OTHER_MEMBERS if member != FUNDED)
)
def test_only_the_serving_identity_may_view_the_pilot_job(delta, manifest, member):
    delta["bindings"].append(_binding(member, PILOT_JOB, RUN_VIEWER))
    _refuses(delta, manifest, "iam_delta_role_scope")


def test_the_funded_account_keeps_its_pilot_job_view(delta, manifest):
    rows = [
        (row["member"], row["condition"])
        for row in delta["bindings"]
        if (row["resource"], row["role"]) == (PILOT_JOB, RUN_VIEWER)
    ]
    assert rows == [(FUNDED, None)]
    validate_iam_delta(delta, manifest)


@pytest.mark.parametrize("job", (BRAIN_JOB, DAILY_JOB, INGEST_JOB))
def test_the_serving_identity_views_no_other_job(delta, manifest, job):
    delta["bindings"].append(_binding(APP, job, RUN_VIEWER))
    _refuses(delta, manifest, "iam_delta_app_regrant")


@pytest.mark.parametrize(
    "role", ("roles/run.invoker", "roles/run.developer", OVERRIDES)
)
def test_the_serving_identity_gains_no_other_role_on_the_pilot_job(
    delta, manifest, role
):
    delta["bindings"].append(_binding(APP, PILOT_JOB, role))
    _refuses(delta, manifest, "iam_delta_app_regrant")


def test_the_pilot_job_view_takes_no_condition(delta, manifest):
    delta["bindings"].append(
        _binding(
            APP,
            PILOT_JOB,
            RUN_VIEWER,
            "request.time < timestamp('2027-01-01T00:00:00Z')",
        )
    )
    _refuses(delta, manifest, "iam_delta_app_regrant")
