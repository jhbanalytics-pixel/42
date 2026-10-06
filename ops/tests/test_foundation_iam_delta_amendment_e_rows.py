"""The rows amendment E writes into the committed delta.

Every expected row is spelled out here as a literal, never read from the delta
under test. The resource manifest moves with this amendment: it gains the owner
reconciliation routine, the v3 source snapshot routine and the read only Cloud
Build builds collection, and the delta classifies each and binds the two routines.
"""

import hashlib
import json
import re
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
PROJECT = "ogilvy-trends-v2"
PROJECT_RESOURCE = f"//cloudresourcemanager.googleapis.com/projects/{PROJECT}"
DAILY_JOB = (
    f"//run.googleapis.com/projects/{PROJECT}/locations/us-central1/"
    "jobs/intelligence-42-daily-staging"
)
DATASETS = f"//bigquery.googleapis.com/projects/{PROJECT}/datasets"
SOURCES = DATASETS + "/intelligence_42_sources_staging"
APPROVALS = DATASETS + "/trends_v2_staging_approvals"
BUCKET = "//storage.googleapis.com/projects/_/buckets/ogilvy-trends-v2-execution-approvals-staging"
DAILY = (
    "projects/_/buckets/ogilvy-trends-v2-execution-approvals-staging/objects/42/daily/"
)
OBJECT = "resource.type == 'storage.googleapis.com/Object'"
USER_CONDITION = (
    f"{OBJECT} && ((resource.name.startsWith('{DAILY}slots/')"
    " && resource.name.endsWith('/control.json'))"
    f" || resource.name.startsWith('{DAILY}collection/days/'))"
)
_CREATED_ONCE = (
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
CREATE_CONDITION = (
    f"{OBJECT} && ("
    + " || ".join(f"resource.name.startsWith('{DAILY}{p}')" for p in _CREATED_ONCE)
    + ")"
)
READ_CONDITION = (
    f"{OBJECT} && ("
    + " || ".join(
        f"resource.name.startsWith('{DAILY}{p}')"
        for p in (*_CREATED_ONCE, "grants/", "workload-rates/")
    )
    + ")"
)
CACHE_BUCKET = (
    "//storage.googleapis.com/projects/_/buckets/listening-post-staging-cache"
)
RENEWAL_ROOT = (
    "projects/_/buckets/listening-post-staging-cache/objects/"
    "open-intelligence/v2/staging/general-questions/"
)
VIEWER = "roles/bigquery.dataViewer"
OPERATOR = "user:albert.meintjes@ogilvy.co.za"
RECONCILE = "sp_reconcile_open_intelligence_daily_consumption_v1"
SNAPSHOT_V3 = "sp_consume_open_intelligence_source_snapshot_v3"


def _member(short):
    return f"serviceAccount:intelligence-42-{short}@{PROJECT}.iam.gserviceaccount.com"


def _routine(name):
    return APPROVALS + "/routines/" + name


FN = _routine("fn_is_canonical_execution_json_v1")
ORCHESTRATION = _member("orchestration")

# (member, resource, role, condition) for every binding amendment E adds.
AMENDMENT_E_BINDINGS = {
    (_member("brain"), FN, VIEWER, None),
    (_member("funded"), FN, VIEWER, None),
    (_member("migration"), FN, VIEWER, None),
    (
        ORCHESTRATION,
        _routine("sp_read_open_intelligence_recurring_grant_v2"),
        VIEWER,
        None,
    ),
    (ORCHESTRATION, DAILY_JOB, "roles/run.jobsExecutorWithOverrides", None),
    (ORCHESTRATION, BUCKET, "roles/storage.objectUser", USER_CONDITION),
    (ORCHESTRATION, BUCKET, "roles/storage.objectCreator", CREATE_CONDITION),
    (ORCHESTRATION, BUCKET, "roles/storage.objectViewer", READ_CONDITION),
    (ORCHESTRATION, SOURCES, VIEWER, None),
    (ORCHESTRATION, _routine(SNAPSHOT_V3), VIEWER, None),
    *(
        (_member(short), PROJECT_RESOURCE, "roles/cloudbuild.builds.viewer", None)
        for short in ("brain", "funded", "migration", "orchestration")
    ),
    *(
        (OPERATOR, _routine(name), VIEWER, None)
        for name in (
            "sp_approve_open_intelligence_execution_v2",
            "fn_is_canonical_execution_json_v1",
            "sp_approve_open_intelligence_execution_v3",
            "sp_disable_open_intelligence_execution_approval_v2",
            "sp_approve_open_intelligence_recurring_grant_v2",
            "sp_read_open_intelligence_recurring_grant_v2",
            "sp_disable_open_intelligence_recurring_grant_v2",
            RECONCILE,
        )
    ),
}
PRICE_POLICY = _member("price-policy")
AMENDMENT_E_BINDINGS |= {
    *(
        (
            PRICE_POLICY,
            CACHE_BUCKET,
            "roles/storage.objectViewer",
            f"{OBJECT} && resource.name == '{RENEWAL_ROOT}renewal/{name}'",
        )
        for name in ("release-index.json", "unattended-grant.json")
    ),
    *(
        (
            PRICE_POLICY,
            CACHE_BUCKET,
            role,
            f"{OBJECT} && resource.name.startsWith('{RENEWAL_ROOT}{prefix}')",
        )
        for role in ("roles/storage.objectViewer", "roles/storage.objectCreator")
        for prefix in ("renewal/sources/", "policies/", "deployments/")
    ),
}
APP = f"serviceAccount:listening-post-staging@{PROJECT}.iam.gserviceaccount.com"
CAPTURE_JOB_ROLE = f"projects/{PROJECT}/roles/CaptureJobRead"
# Round 6: the serving identity reads each capture clone's creating job.
AMENDMENT_E_BINDINGS |= {(APP, PROJECT_RESOURCE, CAPTURE_JOB_ROLE, None)}
# Round 7: the three v2 ledger tables and, since round 7b, the funded pilot job's
# execution.
LEDGER_TABLES = tuple(
    APPROVALS + "/tables/open_intelligence_execution_" + name + "_v2"
    for name in ("approvals", "consumptions", "results")
)
INGEST_JOB = (
    f"//run.googleapis.com/projects/{PROJECT}/locations/us-central1/"
    "jobs/intelligence-42-ingest-staging"
)
PILOT_JOB = (
    f"//run.googleapis.com/projects/{PROJECT}/locations/us-central1/"
    "jobs/intelligence-42-funded-pilot-staging"
)
AMENDMENT_E_BINDINGS |= {(APP, table, VIEWER, None) for table in LEDGER_TABLES}
AMENDMENT_E_BINDINGS |= {(APP, PILOT_JOB, "roles/run.viewer", None)}
BINDINGS_BEFORE = 99


@pytest.fixture(scope="module")
def manifest():
    return load_resource_manifest(OPS_MANIFEST_PATH, expected_sha256=MANIFEST_SHA256)


@pytest.fixture
def delta():
    return load_iam_delta(DELTA_PATH)


def _keys(delta):
    return [
        (row["member"], row["resource"], row["role"], row["condition"])
        for row in delta["bindings"]
    ]


BUILDS = f"//cloudbuild.googleapis.com/projects/{PROJECT}/locations/us-central1/builds"
MANIFEST_ROWS = [
    {"actions": ["deploy", "invoke", "read"], "name": _routine(RECONCILE)},
    {"actions": ["deploy", "invoke", "read"], "name": _routine(SNAPSHOT_V3)},
    {"actions": ["read"], "name": BUILDS},
    *({"actions": ["read"], "name": table} for table in LEDGER_TABLES),
]
FIXTURE_MANIFEST_PATH = (
    REPO_ROOT
    / "ops"
    / "tests"
    / "fixtures"
    / "managed_runtime"
    / "resource_manifest.json"
)


def test_the_manifest_moves_with_amendment_e(manifest):
    for path in (OPS_MANIFEST_PATH, ENGINE_MANIFEST_PATH, FIXTURE_MANIFEST_PATH):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == MANIFEST_BYTES_SHA256
    assert MANIFEST_SHA256 == MANIFEST_BYTES_SHA256
    rows = {row["name"]: row for row in manifest["resources"]}
    assert len(rows) == len(manifest["resources"]) == 76
    for row in MANIFEST_ROWS:
        assert rows[row["name"]] == row
    assert manifest["origin_registry_sha256"] == (
        "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179"
    )


def test_amendment_e_delta_validates_under_albert_s_stamp(delta, manifest):
    report = validate_iam_delta(delta, manifest)
    assert report["identities_unbound"] == []
    assert report["bindings"] == BINDINGS_BEFORE + len(AMENDMENT_E_BINDINGS) == 134
    assert report["create"] + report["existing"] == len(manifest["resources"])
    assert (report["create"], report["existing"]) == (59, 17)
    assert delta["approval"] == {
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


def test_amendment_e_adds_exactly_its_bindings(delta):
    keys = _keys(delta)
    assert len(keys) == len(set(keys))
    assert AMENDMENT_E_BINDINGS <= set(keys)
    purposes = {
        (row["member"], row["resource"], row["role"], row["condition"]): row["purpose"]
        for row in delta["bindings"]
    }
    for key in AMENDMENT_E_BINDINGS:
        assert purposes[key].startswith("derived: "), key


def test_the_operator_holds_exactly_the_eight_routine_views(delta):
    rows = [key for key in _keys(delta) if key[0] == OPERATOR]
    assert len(rows) == 8
    assert all(key[2] == VIEWER and key[3] is None for key in rows)
    assert _routine(RECONCILE) in {key[1] for key in rows}


def test_only_the_operator_is_bound_on_the_reconcile_routine(delta):
    assert [key[0] for key in _keys(delta) if key[1] == _routine(RECONCILE)] == [
        OPERATOR
    ]


def test_only_the_daily_account_is_bound_on_the_v3_capture_routine(delta):
    assert [key for key in _keys(delta) if key[1] == _routine(SNAPSHOT_V3)] == [
        (ORCHESTRATION, _routine(SNAPSHOT_V3), VIEWER, None)
    ]


def test_nothing_is_bound_on_the_builds_collection(delta):
    assert not [key for key in _keys(delta) if key[1].startswith(BUILDS)]


def test_the_daily_account_cannot_delete_its_records_or_the_grants(delta):
    rows = [key for key in _keys(delta) if key[1] == BUCKET]
    assert sorted(rows) == sorted(
        key for key in AMENDMENT_E_BINDINGS if key[1] == BUCKET
    )
    user = [key for key in rows if key[2] == "roles/storage.objectUser"]
    assert user == [(ORCHESTRATION, BUCKET, "roles/storage.objectUser", USER_CONDITION)]
    for prefix in ("authority/", "certification/", "grants/"):
        assert f"'{DAILY}{prefix}'" not in USER_CONDITION
    assert "!" not in USER_CONDITION


def test_the_renewal_job_creates_once_and_reads_only_its_objects(delta):
    rows = sorted(
        (key[2], key[3])
        for key in _keys(delta)
        if key[0] == PRICE_POLICY and key[1] == CACHE_BUCKET
    )
    ledger = (
        f"{OBJECT} && resource.name == '{RENEWAL_ROOT}"
        "allowances/general_cultural_question_staging_eval_v1/ledger.json'"
    )
    assert rows == sorted(
        [
            (f"projects/{PROJECT}/roles/QuestionControlReplace", ledger),
            ("roles/storage.objectViewer", ledger),
            *(
                (role, condition)
                for (member, resource, role, condition) in AMENDMENT_E_BINDINGS
                if member == PRICE_POLICY
            ),
        ]
    )
    creates = [condition for role, condition in rows if role.endswith("objectCreator")]
    assert len(creates) == 3
    assert not [c for c in creates if "release-index" in c or "unattended-grant" in c]


def test_no_write_on_the_collection_dataset_and_no_custom_role(delta):
    on_sources = {(key[0], key[2]) for key in _keys(delta) if key[1] == SOURCES}
    assert (ORCHESTRATION, VIEWER) in on_sources
    assert (ORCHESTRATION, "roles/bigquery.dataEditor") not in on_sources
    assert f"projects/{PROJECT}/roles/SourceSnapshotCreate" not in {
        key[2] for key in _keys(delta)
    }


def test_the_ingest_job_gets_no_socialcrawl_key(delta):
    ingest = _member("ingest")
    assert not [
        key for key in _keys(delta) if key[0] == ingest and "/secrets/" in key[1]
    ]
    item = next(
        row
        for row in delta["unresolved"]
        if row["topic"] == "ingestion_source_credentials"
    )
    assert "no SocialCrawl key" in item["statement"]


def test_the_function_authorization_is_role_less(delta):
    rows = [row for row in delta["routine_authorizations"] if row["routine"] == FN]
    assert rows == [
        {
            "dataset": APPROVALS,
            "role": None,
            "routine": FN,
            "routine_type": "SCALAR_FUNCTION",
        }
    ]
    assert len(delta["routine_authorizations"]) == 23


def test_the_reconcile_and_v3_routines_write_through_their_authorizations(delta):
    for name in (RECONCILE, SNAPSHOT_V3):
        rows = [
            row
            for row in delta["routine_authorizations"]
            if row["routine"] == _routine(name)
        ]
        assert rows == [
            {
                "dataset": APPROVALS,
                "role": "roles/bigquery.routineDataEditor",
                "routine": _routine(name),
            }
        ]


def test_the_new_manifest_rows_are_classified(delta):
    create = {row["resource"]: row for row in delta["create"]}
    for name in (RECONCILE, SNAPSHOT_V3):
        row = create[_routine(name)]
        assert row["existence"] == "not_probed"
        assert row["evidence"] == [
            "r03-native-dataset-trends_v2_staging_approvals.json"
        ]
        assert "apply-v2" in row["note"] and "amendment e" in row["note"]
    # No native read names the builds collection itself, so it is not existing.
    builds = create[BUILDS]
    assert builds["existence"] == "not_probed"
    assert builds["evidence"] == ["r03-native-build-connections.json"]
    assert "read only" in builds["note"] and "nothing is bound" in builds["note"]
    assert BUILDS not in {row["resource"] for row in delta["existing"]}
    location = BUILDS.rsplit("/", 1)[0]
    assert location in {row["resource"] for row in delta["existing"]}
    for table in LEDGER_TABLES:
        row = create[table]
        assert row["existence"] == "not_probed"
        assert row["evidence"] == [
            "r03-native-dataset-trends_v2_staging_approvals.json"
        ]
        assert "create_open_intelligence_execution_approval_store.py" in row["note"]


def test_closed_topics_leave_and_no_manifest_row_is_pending(delta):
    topics = [row["topic"] for row in delta["unresolved"]]
    assert set(topics) == {
        "amendment_e_applier_pending",
        "cloud_scheduler_job_iam",
        "ingestion_source_credentials",
        "daily_collection_dataset_write",
        "ingest_receipt_ledger_pending",
        "price_policy_custom_run_role",
        "serving_bridge_reads",
    }
    assert not [row for row in delta["unresolved"] if RECONCILE in row["statement"]]
    assert not [row for row in delta["unresolved"] if SNAPSHOT_V3 in row["statement"]]


def test_amendment_e_proof_obligations(delta):
    topics = {row["topic"] for row in delta["proof_obligations"]}
    assert topics == {
        "capture_job_read_role_definition",
        "chain_reader_v2_callers",
        "serving_ledger_table_reads",
        "serving_pilot_execution_read",
        "daily_bucket_create_once",
        "daily_child_override_bound",
        "secret_version_condition",
        "source_snapshot_read_permissions",
    }
    statement = next(
        row["statement"]
        for row in delta["proof_obligations"]
        if row["topic"] == "source_snapshot_read_permissions"
    )
    for permission in (
        "bigquery.tables.get",
        "bigquery.tables.getData",
        "bigquery.tables.createSnapshot",
        "bigquery.datasets.get",
        "bigquery.tables.create",
        "bigquery.tables.updateData",
    ):
        assert permission in statement


def _without_routine_rows(delta, manifest):
    """The delta and manifest with the two new routine rows taken out, bindings kept."""
    shrunk = deepcopy(manifest)
    gone = {_routine(RECONCILE), _routine(SNAPSHOT_V3)}
    shrunk["resources"] = [
        row for row in shrunk["resources"] if row["name"] not in gone
    ]
    canonical = json.dumps(shrunk, sort_keys=True, separators=(",", ":")).encode()
    delta["resource_manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
    delta["create"] = [row for row in delta["create"] if row["resource"] not in gone]
    delta["routine_authorizations"] = [
        row for row in delta["routine_authorizations"] if row["routine"] not in gone
    ]
    return shrunk


def test_the_routine_rows_validate_against_the_moved_manifest(delta, manifest):
    report = validate_iam_delta(delta, manifest)
    assert report["create"] + report["existing"] == len(manifest["resources"])
    keys = set(_keys(delta))
    assert (OPERATOR, _routine(RECONCILE), VIEWER, None) in keys
    assert (ORCHESTRATION, _routine(SNAPSHOT_V3), VIEWER, None) in keys


@pytest.mark.parametrize("name", (RECONCILE, SNAPSHOT_V3))
def test_the_routine_bindings_refuse_without_their_manifest_rows(delta, manifest, name):
    shrunk = _without_routine_rows(delta, manifest)
    other = SNAPSHOT_V3 if name == RECONCILE else RECONCILE
    delta["bindings"] = [
        row for row in delta["bindings"] if row["resource"] != _routine(other)
    ]
    with pytest.raises(ValueError, match="^iam_delta_production_target$"):
        validate_iam_delta(delta, shrunk)


def test_the_delta_validates_once_both_routine_bindings_leave(delta, manifest):
    shrunk = _without_routine_rows(delta, manifest)
    gone = {_routine(RECONCILE), _routine(SNAPSHOT_V3)}
    delta["bindings"] = [
        row for row in delta["bindings"] if row["resource"] not in gone
    ]
    report = validate_iam_delta(delta, shrunk)
    assert report["bindings"] == 132


def test_the_delta_names_the_iam_e_words_as_its_applier_and_their_order(delta):
    """The iam-e words on fix/42-iam-amendment-e-apply apply this delta. The
    statement says so, says the apply takes the digest of the approved bytes and
    refuses while a routine it binds is missing, gives the native order, and keeps
    the reason the ade24ba iam-v2 tool is not used for it. It no longer says no
    tool can apply amendment e."""
    items = [
        row
        for row in delta["unresolved"]
        if row["topic"] == "amendment_e_applier_pending"
    ]
    assert len(items) == 1
    item = items[0]
    assert item["members"] == []
    statement = item["statement"]
    assert "No tool can yet apply" not in statement
    assert "once integration/bridge-stack-1 carries it" not in statement
    assert "can be approved but not applied" not in statement
    for text in (
        "iam-e-plan",
        "iam-e-dry-run",
        "iam-e-apply",
        "fix/42-iam-amendment-e-apply",
        "sha256 of the approved bytes",
        "routine_missing",
        "V2_ROUTINE_SPECS",
        "integration/bridge-stack-1",
        "are installed by apply-v2 from their V2_ROUTINE_SPECS entries on the same branch",
        "the release, then apply-v2, then iam-e-apply",
        "ade24ba",
        "1d2e8ead",
        "delta_invalid",
        "ops/deploy/runtime_jobs.py",
        "records Albert's approval and both routines exist",
    ):
        assert text in statement, text


def test_the_collection_dataset_row_records_alberts_read_only_choice(delta):
    """Albert, 23 September 18:26 UTC, chose the standard read only role on the
    collection dataset over the custom snapshot role; the row says so and no
    longer calls it a lead decision."""
    rows = [
        row
        for row in delta["bindings"]
        if (row["member"], row["resource"]) == (ORCHESTRATION, SOURCES)
    ]
    assert [row["role"] for row in rows] == [VIEWER]
    purpose = rows[0]["purpose"]
    assert "Albert, 23 September 18:26 UTC" in purpose
    assert "standard read only role over the custom snapshot role" in purpose
    assert "lead decision" not in purpose


def test_the_v3_snapshot_destination_needs_no_delete_permission(delta):
    """Lead ruling, pending Albert's card: the v3 snapshots carry no
    expiration_timestamp, so the destination needs only create and updateData,
    and no bigquery.tables.deleteSnapshot; no binding is added for it."""
    statement = next(
        row["statement"]
        for row in delta["proof_obligations"]
        if row["topic"] == "source_snapshot_read_permissions"
    )
    for text in (
        "no expiration_timestamp",
        "bigquery.tables.create",
        "bigquery.tables.updateData",
        "bigquery.tables.deleteSnapshot",
        "https://cloud.google.com/bigquery/docs/table-snapshots-create",
        "source_estate_bridge_plan.py",
        "lands separately",
    ):
        assert text in statement, text
    assert "if BigQuery asks for it" not in statement
    assert not [
        row
        for row in delta["bindings"]
        if row["role"] in ("roles/bigquery.dataOwner", "roles/bigquery.admin")
    ]


def test_the_serving_identity_reads_capture_jobs_by_a_one_permission_role(delta):
    rows = {key for key in _keys(delta) if key[0] == APP}
    assert rows == {
        (APP, PROJECT_RESOURCE, CAPTURE_JOB_ROLE, None),
        (APP, PILOT_JOB, "roles/run.viewer", None),
        *((APP, table, VIEWER, None) for table in LEDGER_TABLES),
    }
    statement = next(
        row["statement"]
        for row in delta["proof_obligations"]
        if row["topic"] == "capture_job_read_role_definition"
    )
    for text in (
        "bigquery.jobs.get",
        "bigquery.jobs.create",
        "roles/bigquery.jobUser",
        "bridge_native_clients.py",
        "read_job",
        "general_question_context_admission.py",
        "source_estate_bridge_result.py",
        "user_email",
        "https://cloud.google.com/bigquery/docs/managing-jobs",
        "any job in the project",
    ):
        assert text in statement, text


def test_the_serving_reads_not_added_are_recorded(delta):
    items = [
        row for row in delta["unresolved"] if row["topic"] == "serving_bridge_reads"
    ]
    assert len(items) == 1
    assert items[0]["members"] == [APP]
    statement = items[0]["statement"]
    for text in (
        "ogilvy-trends-v2-oi-source-artifacts-staging",
        "inputs/",
        "captures/",
        "owner readback",
        "42/daily/source_runs/",
        "42/daily/products/",
        "daily chain",
        "conditional",
        "no row",
    ):
        assert text in statement, text
    for text in ("iam_delta_legacy_reach", "_UnavailableDailyChain", "not needed yet"):
        assert text not in statement, text
    buckets = {
        key[1] for key in _keys(delta) if key[0] == APP and "/buckets/" in key[1]
    }
    assert buckets == set()


def test_the_ledger_table_reads_cite_their_lines(delta):
    statement = next(
        row["statement"]
        for row in delta["proof_obligations"]
        if row["topic"] == "serving_ledger_table_reads"
    )
    for text in (
        "bridge_native_clients.py",
        "ledger_reader",
        "approval_reader",
        "general_question_context_queries.py",
        "_RESULT_SQL_V2",
        "_APPROVAL_SQL_V2",
        "open_intelligence_execution_approvals_v2",
        "open_intelligence_execution_consumptions_v2",
        "open_intelligence_execution_results_v2",
        "line 940",
        "line 958",
        "iam_delta_legacy_reach",
        "roles/bigquery.dataViewer",
    ):
        assert text in statement, text


def test_the_pilot_execution_read_points_at_the_pending_lane(delta):
    statement = next(
        row["statement"]
        for row in delta["proof_obligations"]
        if row["topic"] == "serving_pilot_execution_read"
    )
    for text in (
        "feat/42-bridge-run-record-binding",
        "feat/42-bridge-production-clients",
        "pending",
        "run.executions.get",
        "roles/run.viewer",
        "intelligence-42-funded-pilot-staging",
        "open_intelligence_execution_results_v2",
        "https://cloud.google.com/run/docs/reference/iam/roles",
        "no predefined role narrower",
    ):
        assert text in statement, text


def test_the_serving_identity_views_no_ingest_job(delta):
    assert not [key for key in _keys(delta) if key[0] == APP and key[1] == INGEST_JOB]


def test_the_ingest_receipt_ledger_waits_with_no_row(delta):
    ingest = _member("ingest")
    assert not [key for key in _keys(delta) if key[0] == ingest and key[1] == BUCKET]
    item = next(
        row
        for row in delta["unresolved"]
        if row["topic"] == "ingest_receipt_ledger_pending"
    )
    assert item["members"] == [ingest]
    for text in (
        "origin/fix/42-ingest-receipt-ledger",
        "03db4fe",
        "infra/runtime",
        "42/daily/collection/receipts/",
        "42/daily/collection/days/",
        "storage.objects.delete",
        "no row",
    ):
        assert text in item["statement"], text


def test_the_daily_collect_write_waits_for_albert(delta):
    item = next(
        row
        for row in delta["unresolved"]
        if row["topic"] == "daily_collection_dataset_write"
    )
    assert item["members"] == [ORCHESTRATION]
    for text in (
        "intelligence_42_sources_staging",
        "roles/bigquery.dataViewer",
        "B8",
        "18:26",
        "cannot write",
        "Albert",
        "before any daily run",
    ):
        assert text in item["statement"], text
    on_sources = {(key[0], key[2]) for key in _keys(delta) if key[1] == SOURCES}
    assert (ORCHESTRATION, "roles/bigquery.dataEditor") not in on_sources


# A model of the IAM condition subset the bucket rows use: resource.type equality,
# resource.name.startsWith and resource.name.endsWith over string literals, with !, &&,
# || and parentheses. Anything else in a condition fails the model, so a row can only
# pass these checks in a shape the model reads.
_CONDITION_TOKEN = re.compile(
    r"\s*(?:(?P<type>resource\.type == '(?P<type_value>[^']*)')"
    r"|(?P<call>resource\.name\.(?P<function>startsWith|endsWith)\('(?P<argument>[^']*)'\))"
    r"|(?P<op>&&|\|\||!|\(|\)))"
)


def _condition_tokens(expression):
    tokens, position = [], 0
    while position < len(expression.rstrip()):
        match = _CONDITION_TOKEN.match(expression, position)
        assert match is not None, expression[position:]
        tokens.append(match)
        position = match.end()
    return tokens


def _condition_holds(expression, object_name):
    tokens = _condition_tokens(expression)
    index = 0
    name = (
        "projects/_/buckets/ogilvy-trends-v2-execution-approvals-staging/objects/"
        + object_name
    )

    def peek():
        return tokens[index].group("op") if index < len(tokens) else None

    def primary():
        nonlocal index
        token = tokens[index]
        index += 1
        if token.group("op") == "!":
            return not primary()
        if token.group("op") == "(":
            value = disjunction()
            assert peek() == ")"
            index += 1
            return value
        if token.group("type"):
            return token.group("type_value") == "storage.googleapis.com/Object"
        assert token.group("call"), token.group(0)
        if token.group("function") == "startsWith":
            return name.startswith(token.group("argument"))
        return name.endswith(token.group("argument"))

    def conjunction():
        nonlocal index
        value = primary()
        while peek() == "&&":
            index += 1
            value = primary() and value
        return value

    def disjunction():
        nonlocal index
        value = conjunction()
        while peek() == "||":
            index += 1
            value = conjunction() or value
        return value

    value = disjunction()
    assert index == len(tokens)
    return value


# What each predefined role on these rows lets the daily account do to one object.
# Replacing an existing object needs storage.objects.delete beside create.
_ROLE_ACTIONS = {
    "roles/storage.objectViewer": {"get"},
    "roles/storage.objectCreator": {"create"},
    "roles/storage.objectUser": {"get", "create", "replace", "delete"},
}


def _daily_actions(delta, object_name):
    actions = set()
    for member, resource, role, condition in _keys(delta):
        if (
            member == ORCHESTRATION
            and resource == BUCKET
            and _condition_holds(condition, object_name)
        ):
            actions |= _ROLE_ACTIONS[role]
    return actions


OPERATION = "a" * 64
REPLACED_OBJECTS = (
    f"42/daily/slots/{OPERATION}/control.json",
    "42/daily/collection/days/2026-09-24.json",
)
CREATED_ONCE_OBJECTS = (
    "42/daily/authority/attempt.json",
    "42/daily/certification/run.json",
    f"42/daily/slots/{OPERATION}/stages/collect/1-1.json",
    f"42/daily/slots/{OPERATION}/units/u/permit.json",
    f"42/daily/slots/{OPERATION}/units/u/result.json",
    "42/daily/collection/receipts/execution.json",
    "42/daily/source_runs/run.json",
    "42/daily/captures/capture.json",
    f"42/daily/products/{OPERATION}/completion-v1.json",
    f"42/daily/artifact-sets/{OPERATION}.json",
    "42/daily/execution-observations/observation.json",
)
READ_ONLY_OBJECTS = (
    "42/daily/grants/grant.json",
    "42/daily/grants/grant/revocation.json",
    "42/daily/workload-rates/current.json",
)
OUTSIDE_OBJECTS = (
    "42/daily/new-prefix/object.json",
    "42/daily/slots-other/control.json",
    "42/daily/collection/other/object.json",
    "42/daily/control.json",
    "42/other/object.json",
    "open-intelligence/approvals/object.json",
)


def test_the_daily_account_replaces_only_the_slot_controls_and_day_index(delta):
    for name in REPLACED_OBJECTS:
        assert _daily_actions(delta, name) == {"get", "create", "replace", "delete"}, (
            name
        )
    for name in CREATED_ONCE_OBJECTS:
        assert _daily_actions(delta, name) == {"get", "create"}, name
    for name in READ_ONLY_OBJECTS:
        assert _daily_actions(delta, name) == {"get"}, name
    for name in OUTSIDE_OBJECTS:
        assert _daily_actions(delta, name) == set(), name


def test_the_condition_model_reads_the_shapes_it_is_given():
    head = "resource.type == 'storage.googleapis.com/Object' && "
    root = "projects/_/buckets/ogilvy-trends-v2-execution-approvals-staging/objects/"
    starts = f"resource.name.startsWith('{root}42/daily/slots/')"
    ends = "resource.name.endsWith('/control.json')"
    assert _condition_holds(
        head + f"({starts} && {ends})", "42/daily/slots/x/control.json"
    )
    assert not _condition_holds(
        head + f"({starts} && {ends})", "42/daily/slots/x/y.json"
    )
    assert _condition_holds(head + f"!{ends}", "42/daily/slots/x/y.json")
    assert _condition_holds(head + f"({ends} || {starts})", "42/daily/slots/x/y.json")
    with pytest.raises(AssertionError):
        _condition_holds(head + "resource.name.matches('.*')", "42/daily/x")
