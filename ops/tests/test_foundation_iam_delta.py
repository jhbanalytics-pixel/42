import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from ops.deploy.iam_delta import (
    MANIFEST_SHA256,
    diff_against_readback,
    load_iam_delta,
    validate_iam_delta,
)
from ops.deploy.resource_guard import load_resource_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
DELTA_PATH = REPO_ROOT / "ops" / "deploy" / "iam_delta_v1.json"
# The native readback evidence lives outside the checkout. R03_EVIDENCE_DIR names
# it; unset, every test that reads it skips with one reason.
EVIDENCE_DIR = os.environ.get("R03_EVIDENCE_DIR")
EVIDENCE_SKIP = "native readback evidence not named by R03_EVIDENCE_DIR on this machine"
MANIFEST_PATH = Path(
    os.environ.get(
        "IAM_DELTA_MANIFEST_PATH",
        REPO_ROOT / "ops" / "deploy" / "resource_manifest.json",
    )
)
DECISION_RECORD = "R03_DECISIONS_APPROVAL_20260913.json"

PROJECT = "ogilvy-trends-v2"
PROJECT_RESOURCE = f"//cloudresourcemanager.googleapis.com/projects/{PROJECT}"
RUN_PARENT = f"//run.googleapis.com/projects/{PROJECT}/locations/us-central1"
SERVICE = RUN_PARENT + "/services/listening-post-staging"
DAILY_JOB = RUN_PARENT + "/jobs/intelligence-42-daily-staging"
PRICE_POLICY_JOB = RUN_PARENT + "/jobs/intelligence-42-price-policy-staging"
PRICE_POLICY_SCHEDULER = (
    f"//cloudscheduler.googleapis.com/projects/{PROJECT}/locations/us-central1/"
    "jobs/intelligence-42-price-policy-staging"
)
BRAIN_JOB = RUN_PARENT + "/jobs/intelligence-42-brain-staging"
LEGACY_JOB = RUN_PARENT + "/jobs/trends-engine-oi-brain-staging"
DATASETS = f"//bigquery.googleapis.com/projects/{PROJECT}/datasets"
STAGING = DATASETS + "/trends_v2_staging"
APPROVALS = DATASETS + "/trends_v2_staging_approvals"
QA_DATASET = DATASETS + "/trends_v2_staging_qa"
CANONICAL_JSON_UDF = APPROVALS + "/routines/fn_is_canonical_execution_json_v1"
FUNDED_DATASET = DATASETS + "/trends_v2_staging_funded"
PRODUCTION_DATASET = DATASETS + "/trends_v2"
SIMILAR_DATASET = DATASETS + "/trends_v2_staging_evidence"
BUCKET = "//storage.googleapis.com/projects/_/buckets/listening-post-staging-cache"
SECRET_VERSION = (
    f"//secretmanager.googleapis.com/projects/{PROJECT}/secrets/"
    "SOCIALCRAWL_OGILVY_API_KEY/versions/1"
)
PASSCODE_CONTAINER = (
    f"//secretmanager.googleapis.com/projects/{PROJECT}/secrets/ui-passcode-staging"
)
QUEUE = (
    f"//cloudtasks.googleapis.com/projects/{PROJECT}/locations/us-central1/"
    "queues/oi-general-question-staging"
)
LEGACY_ROUTINES = (
    "sp_approve_open_intelligence_execution_v1",
    "sp_disable_open_intelligence_execution_approval_v1",
    "sp_consume_open_intelligence_execution_v1",
    "sp_consume_open_intelligence_source_snapshot_v1",
    "sp_import_open_intelligence_execution_bootstrap_v1",
    "sp_record_open_intelligence_execution_result_v1",
)
FORBIDDEN_ROLES = (
    "roles/owner",
    "roles/editor",
    "roles/run.admin",
    "roles/run.developer",
    "roles/iam.securityAdmin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/secretmanager.admin",
    "roles/bigquery.admin",
    "roles/storage.admin",
)


def _member(short):
    return f"serviceAccount:{short}@{PROJECT}.iam.gserviceaccount.com"


def _account(short):
    return (
        f"//iam.googleapis.com/projects/{PROJECT}/serviceAccounts/"
        f"{short}@{PROJECT}.iam.gserviceaccount.com"
    )


APP = _member("listening-post-staging")
BRAIN = _member("intelligence-42-brain")
QA = _member("intelligence-42-qa")
SCHEDULER = _member("intelligence-42-scheduler")
PRICE_POLICY = _member("intelligence-42-price-policy")
DEPLOY = _member("intelligence-42-deploy")
FRESHNESS = _member("intelligence-42-freshness")
BUILD = _member("intelligence-42-build")
INGEST = _member("intelligence-42-ingest")
APPLY = _member("intelligence-42-apply")
MIGRATION = _member("intelligence-42-migration")
ORCHESTRATION = _member("intelligence-42-orchestration")
# The dedicated execution identities that still hold an operation. Since amendment d
# intelligence-42-exposure, apply, proof and release hold none; the daily job runs
# their operations under intelligence-42-orchestration.
EXECUTION = tuple(
    _member("intelligence-42-" + short)
    for short in (
        "brain",
        "funded",
        "migration",
    )
)
EXECUTION_JOBS = {
    _member("intelligence-42-" + short): RUN_PARENT + "/jobs/intelligence-42-" + job
    for short, job in (
        ("brain", "brain-staging"),
        ("funded", "funded-pilot-staging"),
        ("migration", "migration-staging"),
        ("orchestration", "daily-staging"),
    )
}
DAILY_ROUTINES = (
    "sp_derive_open_intelligence_daily_execution_v1",
    "sp_select_open_intelligence_daily_derivation_v1",
    "sp_consume_open_intelligence_daily_derivation_v1",
    "sp_cancel_open_intelligence_daily_derivation_v1",
    "sp_read_open_intelligence_daily_derivation_v1",
    "sp_read_open_intelligence_daily_chain_v1",
    "sp_record_open_intelligence_daily_result_v1",
)
RECURRING_GRANT_ROUTINES = (
    "sp_approve_open_intelligence_recurring_grant_v2",
    "sp_read_open_intelligence_recurring_grant_v2",
    "sp_disable_open_intelligence_recurring_grant_v2",
)
LEDGER_CONDITION = (
    "resource.type == 'storage.googleapis.com/Object' && resource.name == "
    "'projects/_/buckets/listening-post-staging-cache/objects/open-intelligence/v2/"
    "staging/general-questions/allowances/general_cultural_question_staging_eval_v1/"
    "ledger.json'"
)
ALERT_PREFIX_CONDITION = (
    "resource.type == 'storage.googleapis.com/Object' && resource.name.startsWith("
    "'projects/_/buckets/listening-post-staging-cache/objects/intelligence-42/"
    "staging/freshness/alerts/')"
)
JOB_USER = "roles/bigquery.jobUser"
PRECEDENT = (
    "manifest_precedent: job creation only, no data access; matches the existing"
    " runtime identities in r03-project-iam-readback-0805.json"
)
QUERY_IDENTITIES = tuple(
    _member("intelligence-42-" + short)
    for short in (
        "brain",
        "freshness",
        "funded",
        "ingest",
        "migration",
        "orchestration",
        "qa",
    )
)


@pytest.fixture(scope="module")
def manifest():
    if not MANIFEST_PATH.is_file():
        pytest.skip(f"reviewed resource manifest not present at {MANIFEST_PATH}")
    return load_resource_manifest(MANIFEST_PATH, expected_sha256=MANIFEST_SHA256)


@pytest.fixture
def delta():
    return load_iam_delta(DELTA_PATH)


def _evidence_path(name):
    if EVIDENCE_DIR is None:
        pytest.skip(EVIDENCE_SKIP)
    directory = Path(EVIDENCE_DIR)
    if not directory.is_dir():
        pytest.fail(f"R03_EVIDENCE_DIR names a missing directory: {directory}")
    path = directory / name
    if not path.is_file():
        pytest.skip(EVIDENCE_SKIP)
    return path


@pytest.fixture(scope="module")
def summary():
    path = _evidence_path("r03-native-discovery-summary.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _binding(member, resource, role, condition=None, purpose="derived: mutation"):
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


def _first(delta, **fields):
    for row in delta["bindings"]:
        if all(row[key] == value for key, value in fields.items()):
            return row
    raise AssertionError(f"no binding with {fields}")


def test_real_delta_validates_against_reviewed_manifest(delta, manifest):
    report = validate_iam_delta(delta, manifest)
    assert report["ok"] is True
    assert report["contract_version"] == "42_iam_delta_v1"
    assert report["resource_manifest_sha256"] == MANIFEST_SHA256
    assert report["bindings"] == len(delta["bindings"]) > 0
    assert (
        report["derived"] + report["manifest_fact"] + report["precedent"]
        == report["bindings"]
    )
    assert report["precedent"] == len(QUERY_IDENTITIES)
    assert report["create"] + report["existing"] == len(manifest["resources"])
    assert report["must_not_grant"] == len(LEGACY_ROUTINES)
    assert report["applied"] is False


def test_refuses_member_outside_manifest_identities(delta, manifest):
    delta["bindings"].append(
        _binding(
            _member("trends-engine-oi-brain"), STAGING, "roles/bigquery.dataViewer"
        )
    )
    _refuses(delta, manifest, "iam_delta_member_unknown")


def test_refuses_user_member_even_for_manifest_resource(delta, manifest):
    delta["bindings"].append(
        _binding(
            "user:albert.meintjes@ogilvy.co.za", STAGING, "roles/bigquery.dataViewer"
        )
    )
    _refuses(delta, manifest, "iam_delta_member_unknown")


@pytest.mark.parametrize("resource", (SIMILAR_DATASET, PRODUCTION_DATASET))
def test_refuses_dataset_outside_manifest_as_production(delta, manifest, resource):
    delta["bindings"].append(_binding(BRAIN, resource, "roles/bigquery.dataViewer"))
    _refuses(delta, manifest, "iam_delta_production_target")


def test_refuses_legacy_job_as_production(delta, manifest):
    delta["bindings"].append(_binding(SCHEDULER, LEGACY_JOB, "roles/run.invoker"))
    _refuses(delta, manifest, "iam_delta_production_target")


def test_refuses_resource_outside_manifest(delta, manifest):
    delta["bindings"].append(
        _binding(
            DEPLOY,
            f"//cloudtasks.googleapis.com/projects/{PROJECT}/locations/us-central1/"
            "queues/oi-general-question",
            "roles/cloudtasks.viewer",
        )
    )
    _refuses(delta, manifest, "iam_delta_resource_unknown")


@pytest.mark.parametrize("role", FORBIDDEN_ROLES)
def test_refuses_forbidden_role_on_runtime_identity(delta, manifest, role):
    delta["bindings"].append(_binding(BRAIN, BRAIN_JOB, role))
    _refuses(delta, manifest, "iam_delta_role_forbidden")


def test_refuses_price_policy_run_developer_outside_service(delta, manifest):
    row = _first(delta, member=PRICE_POLICY, role="roles/run.developer")
    assert row["resource"] == SERVICE
    row["resource"] = RUN_PARENT + "/jobs/intelligence-42-price-policy-staging"
    _refuses(delta, manifest, "iam_delta_role_forbidden")


def test_refuses_project_level_binding_for_runtime_identity(delta, manifest):
    delta["bindings"].append(
        _binding(BRAIN, PROJECT_RESOURCE, "roles/bigquery.dataViewer")
    )
    _refuses(delta, manifest, "iam_delta_project_level_forbidden")


def test_refuses_second_project_level_role_for_runtime_member(delta, manifest):
    assert _first(delta, member=BRAIN, resource=PROJECT_RESOURCE, role=JOB_USER)
    delta["bindings"].append(
        _binding(BRAIN, PROJECT_RESOURCE, "roles/logging.logWriter", purpose=PRECEDENT)
    )
    _refuses(delta, manifest, "iam_delta_project_level_forbidden")


def test_job_user_precedent_class_is_exactly_the_query_identities(delta, manifest):
    validate_iam_delta(delta, manifest)
    rows = [row for row in delta["bindings"] if row["role"] == JOB_USER]
    assert sorted(row["member"] for row in rows) == sorted(QUERY_IDENTITIES)
    assert {row["resource"] for row in rows} == {PROJECT_RESOURCE}
    assert all(row["purpose"].startswith("manifest_precedent:") for row in rows)
    assert all(row["condition"] is None for row in rows)
    assert not {BUILD, DEPLOY, SCHEDULER, APP} & {row["member"] for row in rows}
    assert not [
        row for row in delta["unresolved"] if row["topic"] == "bigquery_jobs_create"
    ]


def test_refuses_job_user_without_precedent_label(delta, manifest):
    row = _first(delta, member=BRAIN, role=JOB_USER)
    row["purpose"] = "derived: the operation runs queries"
    _refuses(delta, manifest, "iam_delta_precedent_required")


def test_refuses_precedent_label_on_another_role(delta, manifest):
    row = _first(delta, member=QA, role="roles/bigquery.dataEditor")
    row["purpose"] = PRECEDENT
    _refuses(delta, manifest, "iam_delta_purpose_unlabelled")


def test_refuses_job_user_outside_project(delta, manifest):
    delta["bindings"].append(_binding(BRAIN, STAGING, JOB_USER, purpose=PRECEDENT))
    _refuses(delta, manifest, "iam_delta_role_scope")


def test_refuses_job_user_for_scheduler(delta, manifest):
    delta["bindings"].append(
        _binding(SCHEDULER, PROJECT_RESOURCE, JOB_USER, purpose=PRECEDENT)
    )
    _refuses(delta, manifest, "iam_delta_scheduler_scope")


def test_refuses_job_user_for_app(delta, manifest):
    delta["bindings"].append(
        _binding(APP, PROJECT_RESOURCE, JOB_USER, purpose=PRECEDENT)
    )
    _refuses(delta, manifest, "iam_delta_app_regrant")


def test_refuses_unknown_role(delta, manifest):
    delta["bindings"].append(_binding(BRAIN, STAGING, "roles/bigquery.dataOwner"))
    _refuses(delta, manifest, "iam_delta_role_unknown")


@pytest.mark.parametrize("routine", LEGACY_ROUTINES)
def test_refuses_must_not_grant_routine(delta, manifest, routine):
    delta["bindings"].append(
        _binding(BRAIN, APPROVALS + "/routines/" + routine, "roles/bigquery.dataViewer")
    )
    _refuses(delta, manifest, "iam_delta_must_not_grant")


def test_refuses_dataset_level_grant_reaching_legacy_routines(delta, manifest):
    delta["bindings"].append(_binding(BRAIN, APPROVALS, "roles/bigquery.dataViewer"))
    _refuses(delta, manifest, "iam_delta_legacy_reach")


def test_refuses_duplicate_binding(delta, manifest):
    delta["bindings"].append(deepcopy(delta["bindings"][0]))
    _refuses(delta, manifest, "iam_delta_duplicate_binding")


def test_refuses_condition_with_altered_prefix(delta, manifest):
    row = _first(delta, member=FRESHNESS, role="roles/storage.objectCreator")
    row["condition"] = row["condition"].replace("freshness/alerts/", "")
    _refuses(delta, manifest, "iam_delta_condition_not_allowlisted")


def test_refuses_condition_naming_another_bucket(delta, manifest):
    row = _first(delta, member=FRESHNESS, role="roles/storage.objectCreator")
    row["condition"] = row["condition"].replace(
        "listening-post-staging-cache", "listening-post-cache"
    )
    _refuses(delta, manifest, "iam_delta_condition_not_allowlisted")


def test_refuses_condition_on_dataset(delta, manifest):
    row = _first(delta, member=QA, role="roles/bigquery.dataEditor")
    row["condition"] = "request.time < timestamp('2026-09-30T23:59:59Z')"
    _refuses(delta, manifest, "iam_delta_condition_not_allowlisted")


def test_refuses_unconditional_bucket_grant(delta, manifest):
    row = _first(delta, member=FRESHNESS, role="roles/storage.objectCreator")
    row["condition"] = None
    _refuses(delta, manifest, "iam_delta_condition_required")


def test_refuses_secret_binding_without_version_condition(delta, manifest):
    row = _first(delta, resource=SECRET_VERSION)
    row["condition"] = None
    _refuses(delta, manifest, "iam_delta_condition_required")


def test_refuses_secret_condition_for_other_version(delta, manifest):
    row = _first(delta, resource=SECRET_VERSION)
    row["condition"] = row["condition"].replace("/versions/1'", "/versions/2'")
    _refuses(delta, manifest, "iam_delta_condition_not_allowlisted")


def test_refuses_qa_binding_outside_qa_dataset(delta, manifest):
    delta["bindings"].append(_binding(QA, STAGING, "roles/bigquery.dataViewer"))
    _refuses(delta, manifest, "iam_delta_qa_scope")


def test_refuses_scheduler_invoker_outside_scheduled_jobs(delta, manifest):
    delta["bindings"].append(_binding(SCHEDULER, BRAIN_JOB, "roles/run.invoker"))
    _refuses(delta, manifest, "iam_delta_scheduler_scope")


def test_scheduler_invokes_both_scheduled_jobs_and_nothing_else(delta, manifest):
    validate_iam_delta(delta, manifest)
    rows = [row for row in delta["bindings"] if row["member"] == SCHEDULER]
    assert sorted((row["resource"], row["role"]) for row in rows) == [
        (DAILY_JOB, "roles/run.invoker"),
        (PRICE_POLICY_JOB, "roles/run.invoker"),
    ]
    assert all(row["purpose"].startswith("manifest:") for row in rows)
    assert all(row["condition"] is None for row in rows)
    invokers = [
        row["member"]
        for row in delta["bindings"]
        if row["resource"] == PRICE_POLICY_JOB and row["role"] == "roles/run.invoker"
    ]
    assert invokers == [SCHEDULER]
    created = {row["resource"] for row in delta["create"]}
    assert PRICE_POLICY_SCHEDULER in created and CANONICAL_JSON_UDF in created


def test_canonical_json_udf_is_authorized_role_less_as_a_function(delta, manifest):
    # Amendment e: BigQuery refused a role on the function's authorized routine
    # entry at the first native apply, so the entry records no role and the
    # routine type instead.
    validate_iam_delta(delta, manifest)
    entries = [
        row
        for row in delta["routine_authorizations"]
        if row["routine"] == CANONICAL_JSON_UDF
    ]
    assert entries == [
        {
            "dataset": APPROVALS,
            "role": None,
            "routine": CANONICAL_JSON_UDF,
            "routine_type": "SCALAR_FUNCTION",
        }
    ]


def test_freshness_alert_sink_is_bound_on_the_prefix_and_closed(delta, manifest):
    validate_iam_delta(delta, manifest)
    rows = [
        row for row in delta["bindings"] if row["condition"] == ALERT_PREFIX_CONDITION
    ]
    assert sorted(row["role"] for row in rows) == [
        "roles/storage.objectCreator",
        "roles/storage.objectViewer",
    ]
    assert {row["member"] for row in rows} == {FRESHNESS}
    assert {row["resource"] for row in rows} == {BUCKET}
    assert all(DECISION_RECORD in row["purpose"] for row in rows)
    assert "freshness_alert_sink" not in {row["topic"] for row in delta["unresolved"]}


def test_refuses_app_regrant(delta, manifest):
    delta["bindings"].append(_binding(APP, QUEUE, "roles/cloudtasks.viewer"))
    _refuses(delta, manifest, "iam_delta_app_regrant")


def test_refuses_unlabelled_purpose(delta, manifest):
    delta["bindings"][0]["purpose"] = "coordinator needs it"
    _refuses(delta, manifest, "iam_delta_purpose_unlabelled")


PROPOSED_APPROVAL = {
    "applied": False,
    "approved_at": None,
    "approved_by": None,
    "state": "proposed",
}

STAMPED_APPROVAL = {
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


def test_live_delta_approval_block_is_exactly_the_amendment_e_stamp(manifest):
    # Read from the bytes on disk, not through the loader, so the pin holds the
    # document itself. The block carries amendment e's stamp, written after
    # Albert's phrase over the proposed delta's sha256 at b24e27cb; equality, so no
    # field can drift under the stamp.
    raw = json.loads(DELTA_PATH.read_bytes())
    assert raw["approval"] == STAMPED_APPROVAL
    assert list(raw["approval"]) == sorted(STAMPED_APPROVAL)
    validate_iam_delta(raw, manifest)


PROPOSED_DELTA_SHA256 = (
    "33ad00c0254fd0e514c56bf17b98571f95608ab30604a2523499a7ddf37bda9f"
)
STAMPED_DELTA_SHA256 = (
    "a2bdcfdbe19a94e76699e7cac696f086084cb59a2bbf2102cabafb85aaec3ef1"
)
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


def test_live_delta_bytes_are_exactly_the_amendment_e_proposal_under_its_stamp():
    # Albert's phrase covered the proposed delta at 33ad00c0. The committed file is
    # written indent 2, sorted keys, one trailing newline, the form the recipe
    # hashes with git cat-file. With the stamp taken back to the proposed block the
    # bytes are exactly the covered bytes, so the stamp moved nothing else; the
    # stamped bytes are pinned too. Any other change moves them and needs a new
    # approval. Amendment f stands beside the stamp in its own amendments list, under
    # its own block, so with that list taken out the bytes are the stamped bytes. F's
    # block carries Albert's stamp over a792f21e; with it taken back to proposed the
    # whole file is exactly the bytes he approved.
    raw_bytes = DELTA_PATH.read_bytes()
    assert hashlib.sha256(raw_bytes).hexdigest() == AMENDMENT_G_PROPOSED_SHA256
    committed = json.loads(raw_bytes)
    assert [entry["amendment"] for entry in committed["amendments"]] == ["f", "g"]
    committed["amendments"].pop()
    raw_bytes = (json.dumps(committed, indent=2, sort_keys=True) + "\n").encode()
    assert hashlib.sha256(raw_bytes).hexdigest() == AMENDMENT_F_STAMPED_SHA256
    parsed = json.loads(raw_bytes)
    assert (json.dumps(parsed, indent=2, sort_keys=True) + "\n").encode() == raw_bytes
    f_covered = deepcopy(parsed)
    f_covered["amendments"][0]["approval"] = PROPOSED_APPROVAL
    f_covered_bytes = (json.dumps(f_covered, indent=2, sort_keys=True) + "\n").encode()
    assert hashlib.sha256(f_covered_bytes).hexdigest() == AMENDMENT_F_PROPOSED_SHA256
    assert [entry["amendment"] for entry in parsed.pop("amendments")] == ["f"]
    stamped_bytes = (json.dumps(parsed, indent=2, sort_keys=True) + "\n").encode()
    assert hashlib.sha256(stamped_bytes).hexdigest() == STAMPED_DELTA_SHA256
    covered = {**parsed, "approval": PROPOSED_APPROVAL}
    covered_bytes = (json.dumps(covered, indent=2, sort_keys=True) + "\n").encode()
    assert hashlib.sha256(covered_bytes).hexdigest() == PROPOSED_DELTA_SHA256


def test_an_approved_stamp_needs_both_fields_and_a_proposed_block_neither(
    delta, manifest
):
    delta["approval"].update(
        approved_at="2026-09-14T05:55:43+00:00", approved_by="anyone", state="proposed"
    )
    _refuses(delta, manifest, "iam_delta_invalid")
    delta["approval"].update(approved_at=None, approved_by=None, state="approved")
    _refuses(delta, manifest, "iam_delta_invalid")


def test_refuses_applied_delta(delta, manifest):
    delta["approval"]["applied"] = True
    _refuses(delta, manifest, "iam_delta_applied")


def test_refuses_manifest_digest_mismatch(delta, manifest):
    delta["resource_manifest_sha256"] = "0" * 64
    _refuses(delta, manifest, "iam_delta_manifest_digest_mismatch")


def test_refuses_incomplete_coverage(delta, manifest):
    delta["create"].pop()
    _refuses(delta, manifest, "iam_delta_coverage_incomplete")


def test_refuses_conflicting_classification(delta, manifest):
    delta["existing"].append(
        {
            "evidence": ["r03-native-project.json"],
            "resource": delta["create"][0]["resource"],
        }
    )
    _refuses(delta, manifest, "iam_delta_classification_conflict")


def test_refuses_incomplete_must_not_grant(delta, manifest):
    delta["must_not_grant"].pop()
    _refuses(delta, manifest, "iam_delta_must_not_grant_incomplete")


def test_refuses_routine_authorization_for_legacy_routine(delta, manifest):
    delta["routine_authorizations"].append(
        {
            "dataset": APPROVALS,
            "role": "roles/bigquery.routineDataEditor",
            "routine": APPROVALS + "/routines/" + LEGACY_ROUTINES[0],
        }
    )
    _refuses(delta, manifest, "iam_delta_must_not_grant")


def test_refuses_retained_entry_for_new_identity(delta, manifest):
    delta["retained"][0]["member"] = BRAIN
    _refuses(delta, manifest, "iam_delta_retained_not_app")


def test_refuses_unknown_root_key(delta, manifest):
    delta["notes"] = []
    _refuses(delta, manifest, "iam_delta_invalid")


def _native_tail(resource):
    if "/secrets/" in resource:
        return "secrets/" + resource.split("/secrets/", 1)[1]
    if "/routines/" in resource:
        return '"routineId": "' + resource.rsplit("/routines/", 1)[1] + '"'
    return "projects/" + resource.split("/projects/", 1)[1]


def _evidence_text(name):
    return _evidence_path(name).read_text(encoding="utf-8")


def test_create_resources_absent_from_discovery(delta, summary):
    listed = (
        json.dumps(summary["existing_resources"])
        + summary["app_secret_version_current"]
    )
    absent = summary["new_resources_absent"]
    assert len(delta["create"]) == 43
    for row in delta["create"]:
        tail = _native_tail(row["resource"])
        assert tail not in listed, row["resource"]
        if row["existence"] == "absent_by_native_discovery":
            assert tail in absent, row["resource"]
        if row["existence"] == "absent_from_native_listing":
            for name in row["evidence"]:
                assert tail not in _evidence_text(name), (row["resource"], name)
    explicit = [
        r for r in delta["create"] if r["existence"] == "absent_by_native_discovery"
    ]
    assert sorted(_native_tail(r["resource"]) for r in explicit) == sorted(absent)


def test_existing_resources_present_in_discovery(delta, summary):
    listed = (
        json.dumps(summary["existing_resources"])
        + summary["app_secret_version_current"]
    )
    assert len(delta["existing"]) == 17
    for row in delta["existing"]:
        tail = _native_tail(row["resource"])
        if "r03-native-discovery-summary.json" in row["evidence"]:
            assert tail in listed, row["resource"]
        else:
            numbered = tail.replace(f"projects/{PROJECT}", "projects/590353929363")
            for name in row["evidence"]:
                text = _evidence_text(name)
                assert tail in text or numbered in text, (row["resource"], name)
    summary_backed = [
        r
        for r in delta["existing"]
        if "r03-native-discovery-summary.json" in r["evidence"]
    ]
    assert len(summary_backed) == len(summary["existing_resources"]) + 1


def _native_readback():
    policies = {}
    for resource, name in (
        (PROJECT_RESOURCE, "r03-native-project-iam.json"),
        (QUEUE, "r03-native-queue-iam.json"),
        (BUCKET, "r03-native-evidence-bucket-iam.json"),
        (_account("listening-post-staging"), "r03-native-app-identity-iam.json"),
        (PASSCODE_CONTAINER, "r03-native-passcode-iam.json"),
        (APPROVALS, "r03-native-dataset-trends_v2_staging_approvals.json"),
        (STAGING, "r03-native-dataset-trends_v2_staging.json"),
        (FUNDED_DATASET, "r03-native-dataset-trends_v2_staging_funded.json"),
    ):
        policies[resource] = json.loads(_evidence_text(name))
    return {"policies": policies}


def _flagged(report, resource, member, role):
    return [
        row
        for row in report["existing_broad_grants_not_isolated"]
        if row["resource"] == resource
        and row["member"] == member
        and row["role"] == role
    ]


def test_diff_flags_retained_permission_findings(delta, manifest):
    validate_iam_delta(delta, manifest)
    report = diff_against_readback(delta, _native_readback())
    deployer = _member("cloudbuild-deployer")
    assert _flagged(report, PROJECT_RESOURCE, deployer, "roles/run.developer")
    assert _flagged(report, PROJECT_RESOURCE, deployer, "roles/artifactregistry.writer")
    assert _flagged(report, BUCKET, APP, "roles/storage.objectCreator")
    assert _flagged(report, BUCKET, APP, "roles/storage.objectViewer")
    replace = _flagged(
        report, BUCKET, APP, f"projects/{PROJECT}/roles/QuestionControlReplace"
    )
    assert replace and replace[0]["condition"] is not None
    for routine in LEGACY_ROUTINES:
        rows = _flagged(
            report,
            APPROVALS,
            "routine:" + APPROVALS + "/routines/" + routine,
            "roles/bigquery.routineDataEditor",
        )
        assert rows, routine
    assert report["retained_absent"] == []
    assert not report["present"]
    missing = {
        (row["resource"], row["member"], row["role"]) for row in report["missing"]
    }
    assert (
        PROJECT_RESOURCE,
        _member("intelligence-42-build"),
        "roles/logging.logWriter",
    ) in missing
    assert {
        (PROJECT_RESOURCE, member, JOB_USER) for member in QUERY_IDENTITIES
    } <= missing
    unproven = {row["resource"] for row in report["unproven"]}
    assert SERVICE in unproven and BRAIN_JOB in unproven
    assert all(row["reason"] == "unread" for row in report["unproven"])


def test_diff_never_treats_refused_read_as_empty(delta):
    readback = {"policies": {SERVICE: {"refused": True, "error": "PERMISSION_DENIED"}}}
    report = diff_against_readback(delta, readback)
    assert not [row for row in report["missing"] if row["resource"] == SERVICE]
    refused = [row for row in report["unproven"] if row["resource"] == SERVICE]
    assert refused and refused[0]["reason"] == "refused"
    assert refused[0]["expected"] == len(
        [row for row in delta["bindings"] if row["resource"] == SERVICE]
    )


def test_diff_reports_present_extra_and_unexpected(delta):
    readback = {
        "policies": {
            BRAIN_JOB: {
                "etag": "AAA=",
                "bindings": [
                    {"role": "roles/run.developer", "members": [DEPLOY]},
                    {"role": "roles/run.invoker", "members": [QA]},
                    {
                        "role": "roles/run.viewer",
                        "members": ["user:someone@example.com"],
                    },
                ],
            },
            DAILY_JOB: {"etag": "BBB="},
        }
    }
    report = diff_against_readback(delta, readback)
    present = {(row["member"], row["role"]) for row in report["present"]}
    assert (DEPLOY, "roles/run.developer") in present
    assert [row["member"] for row in report["extra"]] == [QA]
    assert [row["member"] for row in report["unexpected"]] == [
        "user:someone@example.com"
    ]
    daily_missing = {
        row["member"] for row in report["missing"] if row["resource"] == DAILY_JOB
    }
    assert daily_missing == {
        DEPLOY,
        SCHEDULER,
        _member("intelligence-42-orchestration"),
    }
    assert not [
        row for row in report["unproven"] if row["resource"] in (BRAIN_JOB, DAILY_JOB)
    ]


def test_diff_normalizes_dataset_access_shape(delta):
    readback = {
        "policies": {
            QA_DATASET: {
                "access": [
                    {"role": "WRITER", "userByEmail": QA.split(":", 1)[1]},
                    {"role": "READER", "specialGroup": "projectReaders"},
                ]
            }
        }
    }
    report = diff_against_readback(delta, readback)
    assert [(row["member"], row["role"]) for row in report["present"]] == [
        (QA, "roles/bigquery.dataEditor")
    ]
    assert [row["member"] for row in report["unexpected"]] == [
        "specialGroup:projectReaders"
    ]
    assert not [row for row in report["missing"] if row["resource"] == QA_DATASET]


def test_diff_refuses_malformed_readback(delta):
    with pytest.raises(ValueError, match="^iam_readback_invalid$"):
        diff_against_readback(delta, {"policies": [1, 2]})
    with pytest.raises(ValueError, match="^iam_readback_invalid$"):
        diff_against_readback(delta, {"policies": {BRAIN_JOB: {"bindings": "no"}}})


def _routine(name):
    return APPROVALS + "/routines/" + name


def test_execution_identities_hold_the_four_runtime_routines(delta, manifest):
    validate_iam_delta(delta, manifest)
    for member in EXECUTION:
        routines = {
            row["resource"].rsplit("/", 1)[1]
            for row in delta["bindings"]
            if row["member"] == member and "/routines/" in row["resource"]
        }
        expected = {
            # Amendment e: the v2 routines assert canonical JSON through it.
            CANONICAL_JSON_UDF.rsplit("/", 1)[1],
            "sp_consume_open_intelligence_execution_v2",
            "sp_record_open_intelligence_execution_result_v2",
            "sp_read_open_intelligence_execution_approval_v2",
            "sp_read_open_intelligence_execution_approval_v3",
            "sp_read_open_intelligence_execution_result_v2",
        }
        assert routines == expected, member
    readers = [
        r for r in delta["bindings"] if r["resource"].endswith("_result_chain_v1")
    ]
    assert readers == []
    approval = _first(
        delta,
        member=MIGRATION,
        resource=_routine("sp_read_open_intelligence_execution_approval_v2"),
    )
    assert "_default_approval_reader" in approval["purpose"]
    result = _first(
        delta,
        member=MIGRATION,
        resource=_routine("sp_read_open_intelligence_execution_result_v2"),
    )
    assert "_default_result_reader" in result["purpose"]


def test_orchestration_holds_the_daily_job_routines_and_nothing_retired(
    delta, manifest
):
    validate_iam_delta(delta, manifest)
    routines = {
        row["resource"].rsplit("/", 1)[1]
        for row in delta["bindings"]
        if row["member"] == ORCHESTRATION and "/routines/" in row["resource"]
    }
    assert routines == {
        CANONICAL_JSON_UDF.rsplit("/", 1)[1],
        "sp_consume_open_intelligence_execution_v2",
        "sp_consume_open_intelligence_source_snapshot_v2",
        "sp_read_open_intelligence_execution_approval_v2",
        "sp_read_open_intelligence_execution_approval_v3",
        "sp_read_open_intelligence_execution_result_chain_v2",
        "sp_read_open_intelligence_execution_result_v2",
        "sp_record_open_intelligence_execution_result_v2",
        # Amendment e: the daily parent admits its grant row before it derives.
        "sp_read_open_intelligence_recurring_grant_v2",
        # Amendment e: the capture path consumes its v3 plan.
        "sp_consume_open_intelligence_source_snapshot_v3",
        *DAILY_ROUTINES,
    }
    # The recurring grant routines are operator invoked; the one service account
    # row is orchestration's view of the read routine (amendment e).
    for name in RECURRING_GRANT_ROUTINES:
        members = {
            row["member"]
            for row in delta["bindings"]
            if row["resource"] == _routine(name)
        }
        expected = {"user:albert.meintjes@ogilvy.co.za"}
        if name == "sp_read_open_intelligence_recurring_grant_v2":
            expected.add(ORCHESTRATION)
        assert members == expected, name
    editor = _first(
        delta, member=ORCHESTRATION, resource=STAGING, role="roles/bigquery.dataEditor"
    )
    assert "amendment d" in editor["purpose"]
    authorized = {
        row["routine"].rsplit("/", 1)[1]: row["role"]
        for row in delta["routine_authorizations"]
    }
    for name in (*DAILY_ROUTINES, *RECURRING_GRANT_ROUTINES):
        assert name in authorized, name
    for name in (
        "sp_read_open_intelligence_recurring_grant_v2",
        "sp_select_open_intelligence_daily_derivation_v1",
        "sp_read_open_intelligence_daily_derivation_v1",
        "sp_read_open_intelligence_daily_chain_v1",
    ):
        assert authorized[name] == "roles/bigquery.routineDataViewer", name
    created = {row["resource"] for row in delta["create"]}
    assert {
        _routine(name) for name in (*DAILY_ROUTINES, *RECURRING_GRANT_ROUTINES)
    } <= created
    retired = {
        _member("intelligence-42-" + short)
        for short in ("apply", "exposure", "proof", "release")
    }
    assert not [row for row in delta["bindings"] if row["member"] in retired]
    assert not any(set(entry["members"]) & retired for entry in delta["unresolved"])


def test_execution_identities_view_their_own_job(delta):
    for member, job in EXECUTION_JOBS.items():
        row = _first(delta, member=member, resource=job, role="roles/run.viewer")
        if member == ORCHESTRATION:
            assert "reads back its own execution" in row["purpose"]
        else:
            assert "r03-legacy-quiescence-iam-map.md" in row["purpose"]
    # Amendment e closed the build history topic with four project level rows.
    topics = {row["topic"] for row in delta["unresolved"]}
    assert "cloudbuild_builds_viewer" not in topics
    assert sorted(
        (r["member"], r["resource"], r["condition"])
        for r in delta["bindings"]
        if r["role"] == "roles/cloudbuild.builds.viewer"
    ) == sorted(
        (_member("intelligence-42-" + short), PROJECT_RESOURCE, None)
        for short in ("brain", "funded", "migration", "orchestration")
    )


def test_deploy_attaches_no_scheduler_account(delta):
    attached = {
        row["resource"].rsplit("/", 1)[1].split("@", 1)[0]
        for row in delta["bindings"]
        if row["member"] == DEPLOY and row["role"] == "roles/iam.serviceAccountUser"
    }
    assert "intelligence-42-scheduler" not in attached
    assert len(attached) == 12 and "listening-post-staging" in attached
    item = next(
        row for row in delta["unresolved"] if row["topic"] == "cloud_scheduler_job_iam"
    )
    assert "actAs" in item["statement"]


def test_diff_accepts_authorized_view_entries(delta):
    policy = json.loads(
        _evidence_text("r03-native-dataset-trends_v2_staging_funded.json")
    )
    report = diff_against_readback(delta, {"policies": {FUNDED_DATASET: policy}})
    funded = _member("intelligence-42-funded")
    assert [(row["member"], row["role"]) for row in report["missing"]] == [
        (funded, "roles/bigquery.dataEditor")
    ]
    views = [row for row in report["unexpected"] if row["member"].startswith("view:")]
    assert len(views) == 3 and {row["role"] for row in views} == {"authorized_view"}


def test_refuses_unbound_scheduler(delta, manifest):
    delta["bindings"] = [row for row in delta["bindings"] if row["member"] != SCHEDULER]
    _refuses(delta, manifest, "iam_delta_identity_unbound")


def test_refuses_unbound_runtime_identities(delta, manifest):
    keep = {BUILD, DEPLOY}
    delta["bindings"] = [row for row in delta["bindings"] if row["member"] in keep]
    _refuses(delta, manifest, "iam_delta_identity_unbound")


DAILY_OPERATION_KEYS = (
    "execution_collection_exposure_issue",
    "execution_source_snapshot_capture",
    "execution_r3_apply",
    "execution_r3_proof_issue",
    "execution_r3_release",
)


def test_five_daily_keys_sharing_the_bound_orchestration_account_are_bound(
    delta, manifest
):
    orchestration = manifest["identities"]["orchestration"]
    assert all(
        manifest["identities"][key] == orchestration for key in DAILY_OPERATION_KEYS
    )
    assert any(row["member"] == ORCHESTRATION for row in delta["bindings"])
    report = validate_iam_delta(delta, manifest)
    assert set(DAILY_OPERATION_KEYS) | {"orchestration"} <= set(
        report["identities_bound"]
    )
    assert report["identities_unbound"] == []


def test_key_naming_an_account_without_a_binding_still_refuses_unbound(delta, manifest):
    delta["bindings"] = [
        row for row in delta["bindings"] if row["member"] != ORCHESTRATION
    ]
    _refuses(delta, manifest, "iam_delta_identity_unbound")


def test_member_absent_from_identities_still_refuses_member_unknown(delta, manifest):
    for short in ("apply", "exposure", "proof", "release"):
        assert not any(
            row["member"] == _member("intelligence-42-" + short)
            for row in delta["bindings"]
        ), short
    delta["bindings"].append(_binding(APPLY, STAGING, "roles/bigquery.dataViewer"))
    _refuses(delta, manifest, "iam_delta_member_unknown")


def test_refuses_build_with_run_role(delta, manifest):
    delta["bindings"].append(_binding(BUILD, BRAIN_JOB, "roles/run.viewer"))
    _refuses(delta, manifest, "iam_delta_build_scope")


def test_refuses_deploy_with_registry_write(delta, manifest):
    repo = (
        f"//artifactregistry.googleapis.com/projects/{PROJECT}/locations/us-central1/"
        "repositories/intelligence-42"
    )
    delta["bindings"].append(_binding(DEPLOY, repo, "roles/artifactregistry.writer"))
    _refuses(delta, manifest, "iam_delta_deploy_scope")


def test_refuses_deploy_with_log_writer(delta, manifest):
    delta["bindings"].append(
        _binding(DEPLOY, PROJECT_RESOURCE, "roles/logging.logWriter")
    )
    _refuses(delta, manifest, "iam_delta_deploy_scope")


@pytest.mark.parametrize("account", ("intelligence-42-build", "intelligence-42-deploy"))
def test_refuses_actas_on_deployment_accounts(delta, manifest, account):
    delta["bindings"].append(
        _binding(DEPLOY, _account(account), "roles/iam.serviceAccountUser")
    )
    _refuses(delta, manifest, "iam_delta_actas_deployment_account")


def test_refuses_ingest_actas(delta, manifest):
    delta["bindings"].append(
        _binding(
            INGEST, _account("listening-post-staging"), "roles/iam.serviceAccountUser"
        )
    )
    _refuses(delta, manifest, "iam_delta_ingestion_scope")


def test_refuses_price_policy_outside_its_shape(delta, manifest):
    delta["bindings"].append(
        _binding(PRICE_POLICY, STAGING, "roles/bigquery.dataEditor")
    )
    _refuses(delta, manifest, "iam_delta_price_policy_scope")


def test_refuses_price_policy_job_user(delta, manifest):
    delta["bindings"].append(
        _binding(PRICE_POLICY, PROJECT_RESOURCE, JOB_USER, purpose=PRECEDENT)
    )
    _refuses(delta, manifest, "iam_delta_price_policy_scope")


RENEWAL_HEAD = "resource.type == 'storage.googleapis.com/Object' && resource.name"
RENEWAL = (
    "projects/_/buckets/listening-post-staging-cache/objects/"
    "open-intelligence/v2/staging/general-questions/"
)


def test_price_policy_exact_shape(delta, manifest):
    validate_iam_delta(delta, manifest)
    rows = {
        (row["resource"], row["role"], row["condition"])
        for row in delta["bindings"]
        if row["member"] == PRICE_POLICY
    }
    assert rows == {
        (QUEUE, "roles/cloudtasks.viewer", None),
        (_account("listening-post-staging"), "roles/iam.serviceAccountUser", None),
        (SERVICE, "roles/run.developer", None),
        (BUCKET, "roles/storage.objectViewer", LEDGER_CONDITION),
        (BUCKET, f"projects/{PROJECT}/roles/QuestionControlReplace", LEDGER_CONDITION),
        # Amendment e: the managed renewal job reads its index, grant, evidence,
        # policies and bindings, and creates evidence, policies and bindings once.
        *(
            (
                BUCKET,
                "roles/storage.objectViewer",
                RENEWAL_HEAD + f" == '{RENEWAL}{name}'",
            )
            for name in ("renewal/release-index.json", "renewal/unattended-grant.json")
        ),
        *(
            (BUCKET, role, RENEWAL_HEAD + f".startsWith('{RENEWAL}{prefix}')")
            for role in ("roles/storage.objectViewer", "roles/storage.objectCreator")
            for prefix in ("renewal/sources/", "policies/", "deployments/")
        ),
    }
    topics = {row["topic"] for row in delta["unresolved"]}
    assert "price_policy_custom_run_role" in topics


def test_build_holds_no_connection_read(delta):
    roles = {row["role"] for row in delta["bindings"] if row["member"] == BUILD}
    assert roles == {"roles/artifactregistry.writer", "roles/logging.logWriter"}


def test_diff_reports_app_reader_as_retained(delta):
    policy = json.loads(_evidence_text("r03-native-dataset-trends_v2_staging.json"))
    report = diff_against_readback(delta, {"policies": {STAGING: policy}})
    reader = [
        row
        for row in report["retained_present"]
        if row["member"] == APP and row["role"] == "roles/bigquery.dataViewer"
    ]
    assert reader and not [row for row in report["extra"] if row["member"] == APP]
    assert not [row for row in report["retained_absent"] if row["resource"] == STAGING]


def test_secret_condition_is_a_proof_obligation_not_a_decision(delta):
    topics = {row["topic"] for row in delta["unresolved"]}
    assert "secret_version_condition" not in topics
    assert {row["topic"] for row in delta["proof_obligations"]} >= {
        "secret_version_condition"
    }
    assert "freshness_alert_sink" not in topics
    creds = next(
        row
        for row in delta["unresolved"]
        if row["topic"] == "ingestion_source_credentials"
    )
    assert "manifest" in creds["statement"]


def test_refuses_malformed_proof_obligation(delta, manifest):
    delta["proof_obligations"].append({"topic": "x"})
    _refuses(delta, manifest, "iam_delta_invalid")


def _apply_job_account():
    return _account("intelligence-42-apply")


def test_refuses_build_actas_on_runtime_account(delta, manifest):
    delta["bindings"].append(
        _binding(BUILD, _apply_job_account(), "roles/iam.serviceAccountUser")
    )
    _refuses(delta, manifest, "iam_delta_build_scope")


def test_refuses_build_with_data_plane_role(delta, manifest):
    delta["bindings"].append(_binding(BUILD, STAGING, "roles/bigquery.dataEditor"))
    _refuses(delta, manifest, "iam_delta_build_scope")


def test_refuses_deploy_with_data_plane_role(delta, manifest):
    delta["bindings"].append(_binding(DEPLOY, STAGING, "roles/bigquery.dataEditor"))
    _refuses(delta, manifest, "iam_delta_deploy_scope")


def test_refuses_deploy_with_secret_read(delta, manifest):
    row = deepcopy(_first(delta, resource=SECRET_VERSION))
    row["member"] = DEPLOY
    delta["bindings"].append(row)
    _refuses(delta, manifest, "iam_delta_deploy_scope")


def test_refuses_ingest_outside_its_shape(delta, manifest):
    delta["bindings"].append(_binding(INGEST, STAGING, "roles/bigquery.dataEditor"))
    _refuses(delta, manifest, "iam_delta_ingestion_scope")


def test_build_deploy_and_ingest_exact_shapes(delta, manifest):
    validate_iam_delta(delta, manifest)
    rows = {
        member: {
            (row["resource"], row["role"], row["condition"])
            for row in delta["bindings"]
            if row["member"] == member
        }
        for member in (BUILD, INGEST)
    }
    repo = (
        f"//artifactregistry.googleapis.com/projects/{PROJECT}/locations/us-central1/"
        "repositories/intelligence-42"
    )
    assert rows[BUILD] == {
        (repo, "roles/artifactregistry.writer", None),
        (PROJECT_RESOURCE, "roles/logging.logWriter", None),
    }
    assert rows[INGEST] == {
        (
            DATASETS + "/intelligence_42_sources_staging",
            "roles/bigquery.dataEditor",
            None,
        ),
        (PROJECT_RESOURCE, JOB_USER, None),
    }
    deploy_roles = {row["role"] for row in delta["bindings"] if row["member"] == DEPLOY}
    assert deploy_roles == {
        "roles/run.developer",
        "roles/iam.serviceAccountUser",
        "roles/artifactregistry.reader",
        "roles/cloudtasks.viewer",
    }


def test_refuses_empty_proof_obligations_with_secret_condition_row(delta, manifest):
    assert _first(delta, resource=SECRET_VERSION)["condition"] is not None
    delta["proof_obligations"] = []
    _refuses(delta, manifest, "iam_delta_proof_obligation_missing")


def test_refuses_missing_secret_proof_obligation(delta, manifest):
    delta["proof_obligations"] = [
        row
        for row in delta["proof_obligations"]
        if row["topic"] != "secret_version_condition"
    ]
    _refuses(delta, manifest, "iam_delta_proof_obligation_missing")


def test_chain_reader_callers_are_a_proof_obligation(delta, manifest):
    validate_iam_delta(delta, manifest)
    item = next(
        row
        for row in delta["proof_obligations"]
        if row["topic"] == "chain_reader_v2_callers"
    )
    for name in (
        "release_open_intelligence_run.py",
        "issue_r3_execution_proof.py",
        "sp_read_open_intelligence_execution_result_chain_v2",
        "P3",
    ):
        assert name in item["statement"]
    delta["proof_obligations"] = [
        row
        for row in delta["proof_obligations"]
        if row["topic"] != "chain_reader_v2_callers"
    ]
    _refuses(delta, manifest, "iam_delta_proof_obligation_missing")
