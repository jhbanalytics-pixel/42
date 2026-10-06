import hashlib
import json
import re
from pathlib import Path

from ops.deploy.resource_guard import LEDGER_TABLES, _resource_kind, assert_allowed

MANIFEST_SHA256 = "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09"
_CONTRACT_VERSION = "42_iam_delta_v1"
_MAX_DELTA_BYTES = 4 * 1024 * 1024
_ROOT_KEYS = {
    "approval",
    "bindings",
    "contract_version",
    "create",
    "existing",
    "must_not_grant",
    "origin_registry_sha256",
    "project",
    "project_number",
    "region",
    "resource_manifest_sha256",
    "retained",
    "routine_authorizations",
    "proof_obligations",
    "unresolved",
}
# An amendment proposed after an approval stands in this list under its own approval
# block, so the stamp beneath it keeps covering exactly the bytes it covered. The
# acting paths (runtime_jobs.py, runtime_schedulers.py) read the root block and the
# root bindings alone, so a proposed amendment's rows reach no acting path; the iam-e
# words of the execution store migration grant them once every block is approved.
_AMENDMENTS_KEY = "amendments"
_AMENDMENT_KEYS = {"amendment", "approval", "bindings"}
_AMENDMENT_NAME = re.compile(r"[a-z]")
_APPROVAL_KEYS = {"applied", "approved_at", "approved_by", "state"}
_BINDING_KEYS = {"condition", "member", "purpose", "resource", "role"}
_RETAINED_KEYS = _BINDING_KEYS - {"purpose"} | {"finding", "readback"}
_EXISTENCE = {"absent_by_native_discovery", "absent_from_native_listing", "not_probed"}
_DEPLOYMENT_IDENTITIES = frozenset({"build", "deploy"})
_FORBIDDEN_RUNTIME_ROLES = frozenset(
    {
        "roles/owner",
        "roles/editor",
        "roles/run.admin",
        "roles/run.developer",
        "roles/iam.securityAdmin",
        "roles/resourcemanager.projectIamAdmin",
        "roles/secretmanager.admin",
        "roles/bigquery.admin",
        "roles/storage.admin",
    }
)
_ROLE_ACTION = {
    "projects/ogilvy-trends-v2/roles/QuestionControlReplace": "write",
    "projects/ogilvy-trends-v2/roles/SourceSnapshotCreate": "write",
    "projects/ogilvy-trends-v2/roles/CaptureJobRead": "read",
    "roles/artifactregistry.reader": "read",
    "roles/artifactregistry.writer": "write",
    "roles/bigquery.dataEditor": "write",
    "roles/bigquery.dataViewer": "read",
    "roles/bigquery.jobUser": "write",
    "roles/cloudbuild.builds.viewer": "read",
    "roles/cloudbuild.connectionViewer": "read",
    "roles/cloudtasks.viewer": "read",
    "roles/iam.serviceAccountUser": "deploy",
    "roles/logging.logWriter": "write",
    "roles/run.developer": "deploy",
    "roles/run.invoker": "invoke",
    "roles/run.jobsExecutorWithOverrides": "invoke",
    "roles/run.viewer": "read",
    "roles/secretmanager.secretAccessor": "read",
    "roles/storage.objectCreator": "write",
    "roles/storage.objectUser": "write",
    "roles/storage.objectViewer": "read",
}
_JOB_USER = "roles/bigquery.jobUser"
_RUNTIME_PROJECT_ROLE_ALLOWLIST = frozenset({_JOB_USER})
_PRECEDENT_LABEL = "manifest_precedent:"
_READ_ROLES = frozenset(
    role for role, action in _ROLE_ACTION.items() if action == "read"
)
_ROUTINE_AUTH_ROLES = frozenset(
    {"roles/bigquery.routineDataEditor", "roles/bigquery.routineDataViewer"}
)
_APPROVALS_DATASET = (
    "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/"
    "trends_v2_staging_approvals"
)
_LEGACY_ROUTINES = frozenset(
    _APPROVALS_DATASET + "/routines/" + name
    for name in (
        "sp_approve_open_intelligence_execution_v1",
        "sp_disable_open_intelligence_execution_approval_v1",
        "sp_consume_open_intelligence_execution_v1",
        "sp_consume_open_intelligence_source_snapshot_v1",
        "sp_import_open_intelligence_execution_bootstrap_v1",
        "sp_record_open_intelligence_execution_result_v1",
    )
)
_LEGACY_DATASETS = frozenset(
    name.rsplit("/routines/", 1)[0] for name in _LEGACY_ROUTINES
)
_LEDGER_OBJECT = (
    "open-intelligence/v2/staging/general-questions/allowances/"
    "general_cultural_question_staging_eval_v1/ledger.json"
)
_OBJECT_PREFIX_ALLOWLIST = {
    "listening-post-staging-cache": ("intelligence-42/staging/freshness/alerts/",)
}
_APPROVALS_BUCKET_NAME = "ogilvy-trends-v2-execution-approvals-staging"
_APPROVALS_BUCKET = (
    "//storage.googleapis.com/projects/_/buckets/" + _APPROVALS_BUCKET_NAME
)
_DAILY_OBJECTS = f"projects/_/buckets/{_APPROVALS_BUCKET_NAME}/objects/42/daily/"
_OBJECT_TYPE = "resource.type == 'storage.googleapis.com/Object'"
# Under 42/daily/ the daily account (the managed runtime on
# intelligence-42-daily-staging) replaces exactly two kinds of object, each under a
# generation match: its slot control objects (daily_store.py) and the collection day
# index (daily_native_clients.py). Every other object it writes is created once under
# ifGenerationMatch 0 and never replaced or deleted: the authority and certification
# records, the slot stage records, permits and results, the collection receipts, the
# source runs, the captures, the product completion records, the artifact sets and the
# execution observations. It reads those, the operator's grants and revocations
# (managed_runtime.py) and the workload rates (daily_cost_policy.py), and lists
# nothing. Each role opens by an allow list of prefixes, so a prefix added under
# 42/daily/ later opens nothing until a row names it.
_DAILY_REPLACED = (
    (
        f"(resource.name.startsWith('{_DAILY_OBJECTS}slots/')"
        " && resource.name.endsWith('/control.json'))"
    ),
    f"resource.name.startsWith('{_DAILY_OBJECTS}collection/days/')",
)
_DAILY_CREATED_ONCE = (
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
# The replace row already reads the slot controls and the day index. The read row
# holds 11 prefixes, 11 logical operators in all, inside the 12 an IAM condition takes.
_DAILY_READ_ONLY = ("grants/", "workload-rates/")


def _any_prefix(prefixes):
    return " || ".join(
        f"resource.name.startsWith('{_DAILY_OBJECTS}{prefix}')" for prefix in prefixes
    )


_DAILY_USER_CONDITION = f"{_OBJECT_TYPE} && ({' || '.join(_DAILY_REPLACED)})"
_DAILY_RECORD_CREATE_CONDITION = (
    f"{_OBJECT_TYPE} && ({_any_prefix(_DAILY_CREATED_ONCE)})"
)
_DAILY_RECORD_READ_CONDITION = (
    f"{_OBJECT_TYPE} && ({_any_prefix((*_DAILY_CREATED_ONCE, *_DAILY_READ_ONLY))})"
)
# The approvals bucket opens only for the three rows amendment E proposes (B7), each
# pinned by member, role and condition.
_ROLE_EXACT_CONDITIONS = {
    (_APPROVALS_BUCKET_NAME, "roles/storage.objectUser"): (_DAILY_USER_CONDITION,),
    (_APPROVALS_BUCKET_NAME, "roles/storage.objectCreator"): (
        _DAILY_RECORD_CREATE_CONDITION,
    ),
    (_APPROVALS_BUCKET_NAME, "roles/storage.objectViewer"): (
        _DAILY_RECORD_READ_CONDITION,
    ),
}
_EXACT_OBJECT_ALLOWLIST = {"listening-post-staging-cache": (_LEDGER_OBJECT,)}
_CACHE_BUCKET_NAME = "listening-post-staging-cache"
_CACHE_OBJECTS = f"projects/_/buckets/{_CACHE_BUCKET_NAME}/objects/"
_RENEWAL_ROOT = "open-intelligence/v2/staging/general-questions/"
# The managed pricing renewal job (refresh_question_policy.py renew-unattended)
# reads its release index, its grant, the pinned evidence, the active policy and
# binding, and creates the evidence, policy and binding once under
# ifGenerationMatch 0. It never creates the index or the grant, and replaces
# nothing but the ledger it already holds.
_RENEWAL_READ_OBJECTS = ("renewal/release-index.json", "renewal/unattended-grant.json")
_RENEWAL_PREFIXES = ("renewal/sources/", "policies/", "deployments/")
_RENEWAL_READ_CONDITIONS = tuple(
    f"{_OBJECT_TYPE} && resource.name == '{_CACHE_OBJECTS}{_RENEWAL_ROOT}{name}'"
    for name in _RENEWAL_READ_OBJECTS
) + tuple(
    f"{_OBJECT_TYPE} && resource.name.startsWith('{_CACHE_OBJECTS}{_RENEWAL_ROOT}{prefix}')"
    for prefix in _RENEWAL_PREFIXES
)
_RENEWAL_CREATE_CONDITIONS = _RENEWAL_READ_CONDITIONS[len(_RENEWAL_READ_OBJECTS) :]
_ROLE_EXACT_CONDITIONS[(_CACHE_BUCKET_NAME, "roles/storage.objectViewer")] = (
    _RENEWAL_READ_CONDITIONS
)
_ROLE_EXACT_CONDITIONS[(_CACHE_BUCKET_NAME, "roles/storage.objectCreator")] = (
    _RENEWAL_CREATE_CONDITIONS
)
_SERVICE = (
    "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/"
    "services/listening-post-staging"
)
_DAILY_JOB = (
    "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/"
    "jobs/intelligence-42-daily-staging"
)
_PRICE_POLICY_JOB = (
    "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/"
    "jobs/intelligence-42-price-policy-staging"
)
_SCHEDULED_JOBS = frozenset({_DAILY_JOB, _PRICE_POLICY_JOB})
_QA_DATASET = (
    "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/trends_v2_staging_qa"
)
_LEDGER_CONDITION = (
    "resource.type == 'storage.googleapis.com/Object' && resource.name == "
    f"'projects/_/buckets/listening-post-staging-cache/objects/{_LEDGER_OBJECT}'"
)
_QUEUE = (
    "//cloudtasks.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/"
    "queues/oi-general-question-staging"
)
_REPOSITORY = (
    "//artifactregistry.googleapis.com/projects/ogilvy-trends-v2/locations/"
    "us-central1/repositories/intelligence-42"
)
_PROJECT = "//cloudresourcemanager.googleapis.com/projects/ogilvy-trends-v2"
_SOURCES_DATASET = (
    "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/"
    "intelligence_42_sources_staging"
)
_ORCHESTRATION_MEMBER = (
    "serviceAccount:intelligence-42-orchestration@"
    "ogilvy-trends-v2.iam.gserviceaccount.com"
)
_BUILDS_VIEWER = "roles/cloudbuild.builds.viewer"
_SNAPSHOT_ROLE = "projects/ogilvy-trends-v2/roles/SourceSnapshotCreate"
# The create snapshots only role (amendment E B8, the custom role option). Its
# permission list is pinned here: the four table permissions and datasets.get on
# the source dataset, as proposal B8 lists them. A binding of the role needs the
# proof obligation below, and a role definition read back is checked against the
# pin before the binding counts as proven. Which of these BigQuery needs is a
# belief until the first native snapshot confirms it.
_CAPTURE_JOB_ROLE = "projects/ogilvy-trends-v2/roles/CaptureJobRead"
# The serving identity's read of each capture clone's creating job (round 6).
# Another identity ran those jobs, and roles/bigquery.jobUser carries
# bigquery.jobs.create only, so the role holds bigquery.jobs.get and nothing else.
CUSTOM_ROLE_PERMISSIONS = {
    _SNAPSHOT_ROLE: (
        "bigquery.datasets.get",
        "bigquery.tables.create",
        "bigquery.tables.createSnapshot",
        "bigquery.tables.get",
        "bigquery.tables.getData",
    ),
    _CAPTURE_JOB_ROLE: ("bigquery.jobs.get",),
}
_APP_MEMBER = (
    "serviceAccount:listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
)
_BUILD_READERS = frozenset(
    f"serviceAccount:intelligence-42-{short}@ogilvy-trends-v2.iam.gserviceaccount.com"
    for short in ("brain", "funded", "migration", "orchestration")
)
# Each role amendment E adds is bound in exactly the rows it proposes; any other
# member or resource is refused. Members are pinned, not read from the delta.
_ROLE_SHAPES = {
    _BUILDS_VIEWER: frozenset((member, _PROJECT, None) for member in _BUILD_READERS),
    "roles/run.jobsExecutorWithOverrides": frozenset(
        {(_ORCHESTRATION_MEMBER, _DAILY_JOB, None)}
    ),
    _SNAPSHOT_ROLE: frozenset({(_ORCHESTRATION_MEMBER, _SOURCES_DATASET, None)}),
    _CAPTURE_JOB_ROLE: frozenset({(_APP_MEMBER, _PROJECT, None)}),
}
_LEDGER_TABLES = frozenset(
    _APPROVALS_DATASET + "/tables/" + name for name in LEDGER_TABLES
)
_PILOT_JOB = (
    "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/"
    "jobs/intelligence-42-funded-pilot-staging"
)
_RUN_VIEWER = "roles/run.viewer"
_VERTEX_ROLE = "projects/ogilvy-trends-v2/roles/QuestionVertexPredict"
_REPLACE_ROLE = "projects/ogilvy-trends-v2/roles/QuestionControlReplace"
_ARTIFACT_BUCKET_NAME = "ogilvy-trends-v2-oi-source-artifacts-staging"
_ARTIFACT_BUCKET = (
    "//storage.googleapis.com/projects/_/buckets/" + _ARTIFACT_BUCKET_NAME
)
_ARTIFACT_OBJECTS = f"projects/_/buckets/{_ARTIFACT_BUCKET_NAME}/objects/"
_UNTIL_DECEMBER = "request.time < timestamp('2026-12-31T23:59:59Z')"
_ARTIFACT_READ_CONDITION = (
    f"{_OBJECT_TYPE} && (resource.name.startsWith('{_ARTIFACT_OBJECTS}inputs/')"
    f" || resource.name.startsWith('{_ARTIFACT_OBJECTS}captures/'))"
)
# Amendment f (Albert, 24 September): the route A bridge capture runs as the daily
# account and binds each collection receipt to the funded run, reading the approval
# ledger chain with _RESULT_SQL_V2 as the caller and the funded execution with one
# Cloud Run GET. These four rows are the only rows beyond the pinned members on a
# ledger table or on the pilot job's viewers, and they open only inside amendment f.
# Amendment g (Albert's freeze, 25 September), the one final amendment. The serving
# identity's retained custom roles QuestionVertexPredict and QuestionControlReplace end
# on 30 September; g binds each again, to the same member on the same resource, bounded
# by 31 December, and defines neither role. The route A capture runs as the daily
# account and reloads the artifact bucket (storage.buckets.get), reads inputs/ and
# captures/ and creates under captures/ once; the serving identity reads inputs/ and
# captures/ to admit a capture. daily_composition_apply funds up to 100 model calls and
# the compose calls Vertex AI as the daily account, which holds no Vertex role, so g
# binds QuestionVertexPredict to it at project level under the same 31 December bound.
# Each row is pinned exactly and opens only inside g.
_AMENDMENT_ROWS = {
    "f": frozenset(
        {
            *(
                (_ORCHESTRATION_MEMBER, table, "roles/bigquery.dataViewer", None)
                for table in _LEDGER_TABLES
            ),
            (_ORCHESTRATION_MEMBER, _PILOT_JOB, _RUN_VIEWER, None),
        }
    ),
    "g": frozenset(
        {
            (_APP_MEMBER, _PROJECT, _VERTEX_ROLE, _UNTIL_DECEMBER),
            (
                _APP_MEMBER,
                "//storage.googleapis.com/projects/_/buckets/listening-post-staging-cache",
                _REPLACE_ROLE,
                f"{_LEDGER_CONDITION} && {_UNTIL_DECEMBER}",
            ),
            (
                _ORCHESTRATION_MEMBER,
                _ARTIFACT_BUCKET,
                "roles/storage.bucketViewer",
                (
                    "resource.type == 'storage.googleapis.com/Bucket' && resource.name == "
                    f"'projects/_/buckets/{_ARTIFACT_BUCKET_NAME}'"
                ),
            ),
            (
                _ORCHESTRATION_MEMBER,
                _ARTIFACT_BUCKET,
                "roles/storage.objectViewer",
                _ARTIFACT_READ_CONDITION,
            ),
            (
                _ORCHESTRATION_MEMBER,
                _ARTIFACT_BUCKET,
                "roles/storage.objectCreator",
                f"{_OBJECT_TYPE} && resource.name.startsWith('{_ARTIFACT_OBJECTS}captures/')",
            ),
            (
                _APP_MEMBER,
                _ARTIFACT_BUCKET,
                "roles/storage.objectViewer",
                _ARTIFACT_READ_CONDITION,
            ),
            (_ORCHESTRATION_MEMBER, _PROJECT, _VERTEX_ROLE, _UNTIL_DECEMBER),
        }
    ),
}
# The artifact bucket is not a row of the reviewed deploy manifest this file binds by
# digest, which amendment g leaves byte for byte. It is a row of the bridge generation
# manifest (engine resource_manifest_bridge_v3.json, the active pair), and this pin is
# held equal to that row by test. Only an amendment named here may name it, and only in
# its pinned rows.
_AMENDMENT_RESOURCES = {
    "g": ({"actions": ["read", "write"], "name": _ARTIFACT_BUCKET},)
}
# Roles no main row may bind, admitted only in an amendment's pinned rows, with the
# action each carries.
_AMENDMENT_ROLE_ACTION = {
    _VERTEX_ROLE: "write",
    "roles/storage.bucketViewer": "read",
}
# The jobs whose Cloud Run executions the serving identity reads, each with every
# member that may hold run.viewer on it: the serving identity and the job's own
# account, which already views it.
_SERVING_EXECUTION_JOBS = {
    _PILOT_JOB: frozenset(
        {
            _APP_MEMBER,
            "serviceAccount:intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com",
        }
    )
}
# The serving identity's grants are retained, not granted again, except these
# pinned rows: the capture job read, table level dataViewer on each v2 ledger
# table, and run.viewer on the funded pilot job for its Cloud Run execution read.
_APP_ROWS = frozenset(
    {
        (_PROJECT, _CAPTURE_JOB_ROLE, None),
        *((job, _RUN_VIEWER, None) for job in _SERVING_EXECUTION_JOBS),
        *((table, "roles/bigquery.dataViewer", None) for table in _LEDGER_TABLES),
    }
)
# Project level roles a runtime identity may hold beyond jobUser, each only in
# the rows its shape pins.
_PINNED_PROJECT_ROLES = frozenset({_BUILDS_VIEWER, _CAPTURE_JOB_ROLE})
# The bucket row's condition is checked by the bucket allowlist, so its shape pins
# member and resource only.
_ROLE_BUCKET_SHAPES = {
    "roles/storage.objectUser": frozenset({(_ORCHESTRATION_MEMBER, _APPROVALS_BUCKET)})
}
_PRICE_POLICY_MEMBER = "serviceAccount:intelligence-42-price-policy@ogilvy-trends-v2.iam.gserviceaccount.com"
# Every condition opened per role above belongs to one member: the daily account
# on the approvals bucket, the price policy account on the renewal objects.
_PINNED_CONDITION_MEMBERS = {
    (bucket, role, condition): (
        _ORCHESTRATION_MEMBER
        if bucket == _APPROVALS_BUCKET_NAME
        else _PRICE_POLICY_MEMBER
    )
    for (bucket, role), conditions in _ROLE_EXACT_CONDITIONS.items()
    for condition in conditions
}
_APPROVALS_BUCKET_ROWS = frozenset(
    (member, role, condition)
    for (bucket, role, condition), member in _PINNED_CONDITION_MEMBERS.items()
    if bucket == _APPROVALS_BUCKET_NAME
)
_OPERATOR_MEMBER = "user:albert.meintjes@ogilvy.co.za"
# The migration's V2_OPERATOR_ROUTINES and the crash recovery routine (amendment E
# H1 to H5). The operator may view these routines and nothing else.
_OPERATOR_ROUTINES = frozenset(
    _APPROVALS_DATASET + "/routines/" + name
    for name in (
        "sp_approve_open_intelligence_execution_v2",
        "sp_approve_open_intelligence_execution_v3",
        "sp_disable_open_intelligence_execution_approval_v2",
        "fn_is_canonical_execution_json_v1",
        "sp_approve_open_intelligence_recurring_grant_v2",
        "sp_read_open_intelligence_recurring_grant_v2",
        "sp_disable_open_intelligence_recurring_grant_v2",
        "sp_reconcile_open_intelligence_daily_consumption_v1",
    )
)
_OPERATOR_SHAPE = frozenset(
    (routine, "roles/bigquery.dataViewer", None) for routine in _OPERATOR_ROUTINES
)
# The service accounts that may also view an operator routine, pinned by routine:
# the runtime readers assert canonical JSON through the function, and the daily
# parent reads its recurring grant. Every other operator routine is the
# operator's alone (final review of amendment E).
_OPERATOR_ROUTINE_SERVICE_MEMBERS = {
    _APPROVALS_DATASET + "/routines/fn_is_canonical_execution_json_v1": frozenset(
        f"serviceAccount:intelligence-42-{short}@ogilvy-trends-v2.iam.gserviceaccount.com"
        for short in ("brain", "funded", "migration", "orchestration")
    ),
    _APPROVALS_DATASET
    + "/routines/sp_read_open_intelligence_recurring_grant_v2": frozenset(
        {_ORCHESTRATION_MEMBER}
    ),
}
# Albert chose the standard read only role for the daily account on the
# collection dataset (23 September 18:26 UTC, not dataEditor). The custom
# snapshot role stays admissible only through its own obligation and readback.
_ORCHESTRATION_SOURCES_ROLES = frozenset({"roles/bigquery.dataViewer", _SNAPSHOT_ROLE})
# The SocialCrawl key is wave1_pilot's only secret (successor-wave1-pilot.json), so
# its one approved version is bound to the funded account and to no other member.
_SOCIALCRAWL_KEY = (
    "//secretmanager.googleapis.com/projects/ogilvy-trends-v2/secrets/"
    "SOCIALCRAWL_OGILVY_API_KEY/versions/1"
)
_SOCIALCRAWL_KEY_MEMBER = (
    "serviceAccount:intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com"
)
# BigQuery refuses a role on a function's authorized routine entry. The type of
# each function routine is pinned here from its installed SQL; the delta must
# record the same type on a role-less entry, and no other routine may be role-less.
_FUNCTION_ROUTINES = {
    _APPROVALS_DATASET + "/routines/fn_is_canonical_execution_json_v1": (
        "SCALAR_FUNCTION"
    )
}
_BUILD_SHAPE = frozenset(
    {
        (_REPOSITORY, "roles/artifactregistry.writer", None),
        (_PROJECT, "roles/logging.logWriter", None),
    }
)
_INGESTION_SHAPE = frozenset(
    {
        (_SOURCES_DATASET, "roles/bigquery.dataEditor", None),
        (_PROJECT, _JOB_USER, None),
    }
)
_DEPLOY_ROLE_CLASSES = frozenset(
    {
        ("run", "roles/run.developer"),
        ("service_account", "roles/iam.serviceAccountUser"),
        ("artifactregistry", "roles/artifactregistry.reader"),
        ("cloudtasks", "roles/cloudtasks.viewer"),
    }
)
_PROOF_TOPIC_SECRET = "secret_version_condition"
_PROOF_TOPIC_CHAIN = "chain_reader_v2_callers"
_PROOF_TOPIC_CUSTOM_ROLE = "source_snapshot_role_definition"
_PROOF_TOPIC_LEDGER_TABLES = "serving_ledger_table_reads"
_PROOF_TOPIC_SERVING_EXECUTION = "serving_pilot_execution_read"
# Each custom role is proven by an obligation under its own topic.
_CUSTOM_ROLE_TOPICS = {
    _SNAPSHOT_ROLE: _PROOF_TOPIC_CUSTOM_ROLE,
    _CAPTURE_JOB_ROLE: "capture_job_read_role_definition",
}
# Whole permission names only, so tables.create is not found inside
# tables.createSnapshot.
_PERMISSION_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_.])[a-z]+\.[a-zA-Z]+\.[a-zA-Z]+(?![A-Za-z0-9_])"
)
_CHAIN_READER_V2 = (
    _APPROVALS_DATASET + "/routines/sp_read_open_intelligence_execution_result_chain_v2"
)
_PRICE_POLICY_SHAPE = frozenset(
    {
        (_QUEUE, "roles/cloudtasks.viewer", None),
        ("app", "roles/iam.serviceAccountUser", None),
        (_SERVICE, "roles/run.developer", None),
        (
            "//storage.googleapis.com/projects/_/buckets/listening-post-staging-cache",
            "roles/storage.objectViewer",
            _LEDGER_CONDITION,
        ),
        (
            "//storage.googleapis.com/projects/_/buckets/listening-post-staging-cache",
            "projects/ogilvy-trends-v2/roles/QuestionControlReplace",
            _LEDGER_CONDITION,
        ),
        *(
            (
                "//storage.googleapis.com/projects/_/buckets/listening-post-staging-cache",
                "roles/storage.objectViewer",
                condition,
            )
            for condition in _RENEWAL_READ_CONDITIONS
        ),
        *(
            (
                "//storage.googleapis.com/projects/_/buckets/listening-post-staging-cache",
                "roles/storage.objectCreator",
                condition,
            )
            for condition in _RENEWAL_CREATE_CONDITIONS
        ),
    }
)
_BROAD_PROJECT_ROLES = _FORBIDDEN_RUNTIME_ROLES | {
    "roles/artifactregistry.writer",
    "roles/bigquery.dataEditor",
    "roles/cloudbuild.builds.editor",
    "roles/iam.serviceAccountUser",
    "roles/storage.objectAdmin",
}
_BUCKET_OBJECT_ROLES = frozenset(
    {
        "roles/storage.admin",
        "roles/storage.legacyBucketOwner",
        "roles/storage.legacyBucketReader",
        "roles/storage.legacyBucketWriter",
        "roles/storage.legacyObjectOwner",
        "roles/storage.legacyObjectReader",
        "roles/storage.objectAdmin",
        "roles/storage.objectCreator",
        "roles/storage.objectUser",
        "roles/storage.objectViewer",
    }
)
_ACCESS_ROLES = {
    "OWNER": "roles/bigquery.dataOwner",
    "READER": "roles/bigquery.dataViewer",
    "WRITER": "roles/bigquery.dataEditor",
}


def _invalid(code="iam_delta_invalid"):
    raise ValueError(code)


def _closed(value, keys, code="iam_delta_invalid"):
    if type(value) is not dict or set(value) != keys:
        _invalid(code)


def _text(value):
    return type(value) is str and value.strip() == value and bool(value)


def _kind(name, manifest):
    if type(name) is not str:
        return None
    return _resource_kind(
        name,
        project=manifest["project"],
        project_number=manifest["project_number"],
        region=manifest["region"],
    )


def _bucket_conditions(resource, role):
    bucket = resource.rsplit("/", 1)[1]
    head = "resource.type == 'storage.googleapis.com/Object' && resource.name"
    allowed = set()
    for prefix in _OBJECT_PREFIX_ALLOWLIST.get(bucket, ()):
        allowed.add(
            f"{head}.startsWith('projects/_/buckets/{bucket}/objects/{prefix}')"
        )
    for obj in _EXACT_OBJECT_ALLOWLIST.get(bucket, ()):
        allowed.add(f"{head} == 'projects/_/buckets/{bucket}/objects/{obj}'")
    allowed.update(_ROLE_EXACT_CONDITIONS.get((bucket, role), ()))
    return allowed


def _secret_condition(resource, manifest):
    tail = resource.split("/secrets/", 1)[1]
    return (
        "resource.type == 'secretmanager.googleapis.com/SecretVersion' && "
        f"resource.name == 'projects/{manifest['project_number']}/secrets/{tail}'"
    )


def _members(manifest):
    # Since amendment d the identities block is not a bijection: the five daily
    # operation keys and the orchestration key name one account, so a member maps
    # to every key naming it.
    members = {}
    for key, name in manifest["identities"].items():
        members.setdefault("serviceAccount:" + name.rsplit("/", 1)[1], set()).add(key)
    return {member: frozenset(keys) for member, keys in members.items()}


def _check_binding(row, manifest, rows, members, pinned=frozenset()):
    _closed(row, _BINDING_KEYS)
    if not all(_text(row[key]) for key in ("member", "purpose", "resource", "role")):
        _invalid()
    condition = row["condition"]
    if condition is not None and not _text(condition):
        _invalid()
    purpose = row["purpose"]
    if not purpose.startswith(("manifest:", "derived:", _PRECEDENT_LABEL)):
        _invalid("iam_delta_purpose_unlabelled")
    resource, role = row["resource"], row["role"]
    identities = members.get(row["member"])
    if identities is None:
        # One pinned human, on the operator rows only; any other human, or this
        # one anywhere else, stays unknown. The operator names no identity key.
        operator = row["member"] == _OPERATOR_MEMBER
        if not operator or (resource, role, condition) not in _OPERATOR_SHAPE:
            _invalid("iam_delta_member_unknown")
        identities = frozenset()
    if resource in _LEGACY_ROUTINES:
        _invalid("iam_delta_must_not_grant")
    if (
        resource in _OPERATOR_ROUTINES
        and row["member"] != _OPERATOR_MEMBER
        and row["member"] not in _OPERATOR_ROUTINE_SERVICE_MEMBERS.get(resource, ())
    ):
        _invalid("iam_delta_role_scope")
    if (
        resource == _SOURCES_DATASET
        and row["member"] == _ORCHESTRATION_MEMBER
        and role not in _ORCHESTRATION_SOURCES_ROLES
    ):
        _invalid("iam_delta_role_scope")
    kind = _kind(resource, manifest)
    if resource not in rows:
        if kind in {"bigquery_dataset", "bigquery_routine", "run"}:
            _invalid("iam_delta_production_target")
        _invalid("iam_delta_resource_unknown")
    # An amendment opens beyond the rules below only in the rows it pins, exactly as
    # pinned: amendment f on the ledger tables and the pilot job, amendment g on the
    # serving identity's extended time bounds and on the artifact bucket.
    opened = (row["member"], resource, role, condition) in pinned
    if (
        "app" in identities
        and (resource, role, condition) not in _APP_ROWS
        and not opened
    ):
        _invalid("iam_delta_app_regrant")
    # The ledger tables are the serving identity's alone, and a job it reads
    # executions of is viewed by no member beyond its pinned set.
    if kind == "bigquery_table" and row["member"] != _APP_MEMBER and not opened:
        _invalid("iam_delta_role_scope")
    if (
        role == _RUN_VIEWER
        and row["member"] not in _SERVING_EXECUTION_JOBS.get(resource, (row["member"],))
        and not opened
    ):
        _invalid("iam_delta_role_scope")
    runtime = not identities & _DEPLOYMENT_IDENTITIES
    if runtime and role in _FORBIDDEN_RUNTIME_ROLES:
        carve_out = "price_policy" in identities and role == "roles/run.developer"
        if not carve_out or resource != _SERVICE:
            _invalid("iam_delta_role_forbidden")
    shape = (row["member"], resource, condition)
    pinned_project = role in _PINNED_PROJECT_ROLES and shape in _ROLE_SHAPES[role]
    if (
        runtime
        and kind == "project"
        and role not in _RUNTIME_PROJECT_ROLE_ALLOWLIST
        and not pinned_project
        and not opened
    ):
        _invalid("iam_delta_project_level_forbidden")
    action = _ROLE_ACTION.get(role)
    if action is None and opened:
        action = _AMENDMENT_ROLE_ACTION.get(role)
    if action is None:
        _invalid("iam_delta_role_unknown")
    if role in _ROLE_SHAPES and shape not in _ROLE_SHAPES[role]:
        _invalid("iam_delta_role_scope")
    if role in _ROLE_BUCKET_SHAPES and shape[:2] not in _ROLE_BUCKET_SHAPES[role]:
        _invalid("iam_delta_role_scope")
    if "build" in identities and role.startswith("roles/run."):
        _invalid("iam_delta_build_scope")
    if "deploy" in identities and role in {
        "roles/artifactregistry.writer",
        "roles/logging.logWriter",
    }:
        _invalid("iam_delta_deploy_scope")
    deployment_accounts = {manifest["identities"][k] for k in _DEPLOYMENT_IDENTITIES}
    if role == "roles/iam.serviceAccountUser" and resource in deployment_accounts:
        _invalid("iam_delta_actas_deployment_account")
    if "build" in identities and (resource, role, condition) not in _BUILD_SHAPE:
        _invalid("iam_delta_build_scope")
    if "deploy" in identities and (
        condition is not None or (kind, role) not in _DEPLOY_ROLE_CLASSES
    ):
        _invalid("iam_delta_deploy_scope")
    if (
        "ingestion" in identities
        and (resource, role, condition) not in _INGESTION_SHAPE
    ):
        _invalid("iam_delta_ingestion_scope")
    if runtime and resource in _LEGACY_DATASETS:
        _invalid("iam_delta_legacy_reach")
    precedent = role == _JOB_USER
    if precedent and kind != "project":
        _invalid("iam_delta_role_scope")
    if precedent and not purpose.startswith(_PRECEDENT_LABEL):
        _invalid("iam_delta_precedent_required")
    if purpose.startswith(_PRECEDENT_LABEL) and not precedent:
        _invalid("iam_delta_purpose_unlabelled")
    try:
        assert_allowed(resource, action, manifest)
    except ValueError:
        _invalid("iam_delta_action_not_in_manifest")
    if kind == "bucket":
        if condition is None:
            _invalid("iam_delta_condition_required")
        if condition not in _bucket_conditions(resource, role) and not opened:
            _invalid("iam_delta_condition_not_allowlisted")
        pinned = _PINNED_CONDITION_MEMBERS.get(
            (resource.rsplit("/", 1)[1], role, condition)
        )
        if pinned is not None and row["member"] != pinned:
            _invalid("iam_delta_role_scope")
        if (
            resource == _APPROVALS_BUCKET
            and (row["member"], role, condition) not in _APPROVALS_BUCKET_ROWS
        ):
            _invalid("iam_delta_role_scope")
    elif kind == "secret_version":
        if condition is None:
            _invalid("iam_delta_condition_required")
        if condition != _secret_condition(resource, manifest):
            _invalid("iam_delta_condition_not_allowlisted")
        if resource == _SOCIALCRAWL_KEY and row["member"] != _SOCIALCRAWL_KEY_MEMBER:
            _invalid("iam_delta_role_scope")
    elif condition is not None and not opened:
        _invalid("iam_delta_condition_not_allowlisted")
    if (
        "qa" in identities
        and not precedent
        and (resource, role)
        != (
            _QA_DATASET,
            "roles/bigquery.dataEditor",
        )
    ):
        _invalid("iam_delta_qa_scope")
    if "scheduler" in identities and (
        resource not in _SCHEDULED_JOBS or role != "roles/run.invoker"
    ):
        _invalid("iam_delta_scheduler_scope")
    if "freshness" in identities and not precedent and role not in _READ_ROLES:
        if role != "roles/storage.objectCreator" or ".startsWith(" not in condition:
            _invalid("iam_delta_freshness_scope")
    if "price_policy" in identities:
        target = "app" if resource == manifest["identities"]["app"] else resource
        if (target, role, condition) not in _PRICE_POLICY_SHAPE:
            _invalid("iam_delta_price_policy_scope")
    return identities


def _check_classification(delta, rows):
    seen = {}
    for list_name, keys in (
        ("create", {"evidence", "existence", "note", "resource"}),
        ("existing", {"evidence", "resource"}),
    ):
        entries = delta[list_name]
        if type(entries) is not list:
            _invalid()
        for entry in entries:
            _closed(entry, keys)
            resource = entry["resource"]
            if resource not in rows:
                _invalid("iam_delta_resource_unknown")
            evidence = entry["evidence"]
            if (
                type(evidence) is not list
                or not evidence
                or not all(map(_text, evidence))
            ):
                _invalid()
            if list_name == "create":
                if entry["existence"] not in _EXISTENCE or not _text(entry["note"]):
                    _invalid()
            if resource in seen:
                _invalid("iam_delta_classification_conflict")
            seen[resource] = list_name
    if set(seen) != set(rows):
        _invalid("iam_delta_coverage_incomplete")


def _check_must_not_grant(delta, manifest):
    entries = delta["must_not_grant"]
    if type(entries) is not list:
        _invalid()
    listed = set()
    for entry in entries:
        _closed(entry, {"applies_to", "reason", "resource"})
        if not all(_text(entry[key]) for key in entry):
            _invalid()
        if _kind(entry["resource"], manifest) != "bigquery_routine":
            _invalid()
        listed.add(entry["resource"])
    if not _LEGACY_ROUTINES <= listed:
        _invalid("iam_delta_must_not_grant_incomplete")
    return listed


def _check_routine_authorizations(delta, manifest, rows, forbidden):
    entries = delta["routine_authorizations"]
    if type(entries) is not list:
        _invalid()
    seen = set()
    role_less = set()
    for entry in entries:
        if type(entry) is dict and "routine_type" in entry:
            # A function's entry: no role, and the routine type recorded.
            _closed(entry, {"dataset", "role", "routine", "routine_type"})
            if entry["role"] is not None:
                _invalid()
            if not all(_text(entry[key]) for key in ("dataset", "routine")):
                _invalid()
        else:
            _closed(entry, {"dataset", "role", "routine"})
            if not all(_text(entry[key]) for key in entry):
                _invalid()
        routine, dataset = entry["routine"], entry["dataset"]
        if routine in forbidden:
            _invalid("iam_delta_must_not_grant")
        if routine not in rows or "deploy" not in rows[routine]:
            _invalid("iam_delta_production_target")
        if _kind(routine, manifest) != "bigquery_routine" or dataset not in rows:
            _invalid()
        if routine.rsplit("/routines/", 1)[0] != dataset:
            _invalid()
        if entry["role"] is None:
            pinned = _FUNCTION_ROUTINES.get(routine)
            if pinned is None or entry["routine_type"] != pinned:
                _invalid("iam_delta_role_unknown")
            role_less.add(routine)
        elif entry["role"] not in _ROUTINE_AUTH_ROLES:
            _invalid("iam_delta_role_unknown")
        key = (dataset, routine, entry["role"])
        if key in seen:
            _invalid("iam_delta_duplicate_binding")
        seen.add(key)
    # A role-less function entry is the routine's only entry.
    if any(routine in role_less for _dataset, routine, role in seen if role):
        _invalid("iam_delta_duplicate_binding")


def _check_retained(delta, manifest, members):
    entries = delta["retained"]
    if type(entries) is not list:
        _invalid()
    app = "serviceAccount:" + manifest["identities"]["app"].rsplit("/", 1)[1]
    for entry in entries:
        _closed(entry, _RETAINED_KEYS)
        if entry["member"] != app:
            _invalid("iam_delta_retained_not_app")
        if not _text(entry["resource"]) or not _text(entry["role"]):
            _invalid()
        if not _text(entry["readback"]) or _kind(entry["resource"], manifest) is None:
            _invalid()
        for key in ("condition", "finding"):
            if entry[key] is not None and not _text(entry[key]):
                _invalid()


def _check_proof_obligations(delta):
    entries = delta["proof_obligations"]
    if type(entries) is not list:
        _invalid()
    for entry in entries:
        _closed(entry, {"statement", "topic"})
        if not _text(entry["topic"]) or not _text(entry["statement"]):
            _invalid()


def _check_unresolved(delta, members):
    entries = delta["unresolved"]
    if type(entries) is not list:
        _invalid()
    for entry in entries:
        _closed(entry, {"members", "statement", "topic"})
        if not _text(entry["topic"]) or not _text(entry["statement"]):
            _invalid()
        if type(entry["members"]) is not list:
            _invalid()
        if any(member not in members for member in entry["members"]):
            _invalid("iam_delta_member_unknown")


def _check_custom_role_obligation(delta, pinned, topic):
    # A bound custom role carries an obligation under its own topic that names
    # every pinned permission, so the readback that proves it checks the list
    # this file pins.
    statements = [
        entry["statement"]
        for entry in delta["proof_obligations"]
        if entry["topic"] == topic
    ]
    named = [set(_PERMISSION_TOKEN.findall(text)) for text in statements]
    if not any(set(pinned) <= tokens for tokens in named):
        _invalid("iam_delta_proof_obligation_missing")


def _check_approval(approval):
    _closed(approval, _APPROVAL_KEYS)
    if approval["applied"] is not False:
        _invalid("iam_delta_applied")
    if approval["state"] not in {"proposed", "approved"}:
        _invalid()
    recorded = (
        approval["approved_by"] is not None and approval["approved_at"] is not None
    )
    if (approval["state"] == "approved") != recorded:
        _invalid()
    if approval["state"] == "proposed" and (
        approval["approved_by"] is not None or approval["approved_at"] is not None
    ):
        _invalid()
    return approval["state"]


def _amendment_scope(name, manifest, rows):
    """The manifest an amendment's rows are checked against: the reviewed manifest, plus
    the resource rows pinned for that amendment alone."""

    extra = _AMENDMENT_RESOURCES.get(name, ())
    if not extra:
        return manifest, rows
    if any(item["name"] in rows for item in extra):
        _invalid("resource_manifest_invalid")
    scope = {**manifest, "resources": [*manifest["resources"], *extra]}
    return scope, {**rows, **{item["name"]: item["actions"] for item in extra}}


def _check_amendments(delta, manifest, rows, members, seen):
    """Each amendment in order, its block and its rows; returns what each carries."""

    entries = delta[_AMENDMENTS_KEY]
    if type(entries) is not list or not entries:
        _invalid()
    # An amendment is approved only over an approved file and approved earlier ones.
    state = delta["approval"]["state"]
    names = []
    report = []
    for entry in entries:
        _closed(entry, _AMENDMENT_KEYS)
        name = entry["amendment"]
        if type(name) is not str or not _AMENDMENT_NAME.fullmatch(name):
            _invalid()
        if names and name <= names[-1]:
            _invalid()
        names.append(name)
        current = _check_approval(entry["approval"])
        if current == "approved" and state != "approved":
            _invalid()
        state = current
        bindings = entry["bindings"]
        if type(bindings) is not list or not bindings:
            _invalid()
        pinned = _AMENDMENT_ROWS.get(name, frozenset())
        scope, scope_rows = _amendment_scope(name, manifest, rows)
        for row in bindings:
            _check_binding(row, scope, scope_rows, members, pinned)
            key = (row["resource"], row["member"], row["role"], row["condition"])
            if key in seen:
                _invalid("iam_delta_duplicate_binding")
            seen.add(key)
        report.append({"amendment": name, "bindings": len(bindings), "state": current})
    return report


def validate_iam_delta(delta: dict, manifest: dict) -> dict:
    amended = type(delta) is dict and _AMENDMENTS_KEY in delta
    _closed(delta, _ROOT_KEYS | {_AMENDMENTS_KEY} if amended else _ROOT_KEYS)
    if delta["contract_version"] != _CONTRACT_VERSION:
        _invalid()
    if type(manifest) is not dict or type(manifest.get("resources")) is not list:
        _invalid("resource_manifest_invalid")
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    if delta["resource_manifest_sha256"] != hashlib.sha256(canonical).hexdigest():
        _invalid("iam_delta_manifest_digest_mismatch")
    if delta["origin_registry_sha256"] != manifest.get("origin_registry_sha256"):
        _invalid("iam_delta_manifest_digest_mismatch")
    for key in ("project", "project_number", "region"):
        if delta[key] != manifest.get(key):
            _invalid()
    approval = delta["approval"]
    _closed(approval, _APPROVAL_KEYS)
    if approval["applied"] is not False:
        _invalid("iam_delta_applied")
    if approval["state"] not in {"proposed", "approved"}:
        _invalid()
    recorded = (
        approval["approved_by"] is not None and approval["approved_at"] is not None
    )
    if (approval["state"] == "approved") != recorded:
        _invalid()

    rows = {}
    for row in manifest["resources"]:
        if type(row) is not dict or not _text(row.get("name")):
            _invalid("resource_manifest_invalid")
        rows[row["name"]] = row["actions"]
    members = _members(manifest)
    bindings = delta["bindings"]
    if type(bindings) is not list or not bindings:
        _invalid()
    bound = set()
    seen = set()
    derived = 0
    precedent = 0
    for row in bindings:
        bound |= _check_binding(row, manifest, rows, members)
        key = (row["resource"], row["member"], row["role"], row["condition"])
        if key in seen:
            _invalid("iam_delta_duplicate_binding")
        seen.add(key)
        derived += row["purpose"].startswith("derived:")
        precedent += row["purpose"].startswith(_PRECEDENT_LABEL)
    amendments = (
        _check_amendments(delta, manifest, rows, members, seen) if amended else None
    )
    if set(manifest["identities"]) - bound - {"app"}:
        _invalid("iam_delta_identity_unbound")
    _check_classification(delta, rows)
    forbidden = _check_must_not_grant(delta, manifest)
    _check_proof_obligations(delta)
    topics = {entry["topic"] for entry in delta["proof_obligations"]}
    secret_rows = any(
        _kind(row["resource"], manifest) == "secret_version" for row in bindings
    )
    if secret_rows and _PROOF_TOPIC_SECRET not in topics:
        _invalid("iam_delta_proof_obligation_missing")
    table_rows = any(row["resource"] in _LEDGER_TABLES for row in bindings)
    if table_rows and _PROOF_TOPIC_LEDGER_TABLES not in topics:
        _invalid("iam_delta_proof_obligation_missing")
    serving_view = any(
        (row["member"], row["role"]) == (_APP_MEMBER, _RUN_VIEWER)
        and row["resource"] in _SERVING_EXECUTION_JOBS
        for row in bindings
    )
    if serving_view and _PROOF_TOPIC_SERVING_EXECUTION not in topics:
        _invalid("iam_delta_proof_obligation_missing")
    chain_rows = any(row["resource"] == _CHAIN_READER_V2 for row in bindings)
    if chain_rows and _PROOF_TOPIC_CHAIN not in topics:
        _invalid("iam_delta_proof_obligation_missing")
    for role, pinned in CUSTOM_ROLE_PERMISSIONS.items():
        if any(row["role"] == role for row in bindings):
            _check_custom_role_obligation(delta, pinned, _CUSTOM_ROLE_TOPICS[role])
    _check_routine_authorizations(delta, manifest, rows, forbidden)
    _check_retained(delta, manifest, members)
    _check_unresolved(delta, members)
    report = {
        "ok": True,
        "contract_version": _CONTRACT_VERSION,
        "resource_manifest_sha256": delta["resource_manifest_sha256"],
        "origin_registry_sha256": delta["origin_registry_sha256"],
        "applied": False,
        "bindings": len(bindings),
        "derived": derived,
        "precedent": precedent,
        "manifest_fact": len(bindings) - derived - precedent,
        "create": len(delta["create"]),
        "existing": len(delta["existing"]),
        "must_not_grant": len(delta["must_not_grant"]),
        "retained": len(delta["retained"]),
        "routine_authorizations": len(delta["routine_authorizations"]),
        "unresolved": len(delta["unresolved"]),
        "proof_obligations": len(delta["proof_obligations"]),
        "identities_bound": sorted(bound),
        "identities_unbound": sorted(set(manifest["identities"]) - bound),
    }
    if amended:
        report["amendments"] = amendments
    return report


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _invalid()
        value[key] = item
    return value


def load_iam_delta(path) -> dict:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError:
        _invalid()
    if len(raw) > _MAX_DELTA_BYTES:
        _invalid()
    try:
        delta = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: _invalid(),
        )
    except (UnicodeError, json.JSONDecodeError, TypeError):
        _invalid()
    if type(delta) is not dict:
        _invalid()
    return delta


def _readback_invalid():
    raise ValueError("iam_readback_invalid")


def _access_member(entry, project):
    if "routine" in entry or "view" in entry or "dataset" in entry:
        for label in ("routine", "view", "dataset"):
            if label in entry:
                ref = entry[label]
                if type(ref) is not dict:
                    _readback_invalid()
                if label == "dataset":
                    ref = ref.get("dataset", ref)
                    return "dataset:" + (
                        f"//bigquery.googleapis.com/projects/{ref.get('projectId', project)}"
                        f"/datasets/{ref.get('datasetId')}"
                    )
                table = "routines" if label == "routine" else "tables"
                item = (
                    ref.get("routineId") if label == "routine" else ref.get("tableId")
                )
                return f"{label}:" + (
                    f"//bigquery.googleapis.com/projects/{ref.get('projectId', project)}"
                    f"/datasets/{ref.get('datasetId')}/{table}/{item}"
                )
    if "userByEmail" in entry:
        email = entry["userByEmail"]
        prefix = "serviceAccount:" if email.endswith("gserviceaccount.com") else "user:"
        return prefix + email
    if "groupByEmail" in entry:
        return "group:" + entry["groupByEmail"]
    if "domain" in entry:
        return "domain:" + entry["domain"]
    if "specialGroup" in entry:
        return "specialGroup:" + entry["specialGroup"]
    if "iamMember" in entry:
        return entry["iamMember"]
    _readback_invalid()


def _flatten(policy, project):
    if policy is None or (
        type(policy) is dict and (policy.get("refused") or "error" in policy)
    ):
        return None
    if type(policy) is not dict:
        _readback_invalid()
    rows = []
    if "bindings" in policy:
        if type(policy["bindings"]) is not list:
            _readback_invalid()
        for binding in policy["bindings"]:
            if type(binding) is not dict or not _text(binding.get("role")):
                _readback_invalid()
            members = binding.get("members")
            if type(members) is not list or not all(map(_text, members)):
                _readback_invalid()
            condition = binding.get("condition")
            expression = None
            if condition is not None:
                if type(condition) is not dict or not _text(
                    condition.get("expression")
                ):
                    _readback_invalid()
                expression = condition["expression"]
            rows.extend((binding["role"], member, expression) for member in members)
        return rows
    if "access" in policy:
        if type(policy["access"]) is not list:
            _readback_invalid()
        for entry in policy["access"]:
            if type(entry) is not dict:
                _readback_invalid()
            if "role" not in entry and "view" in entry:
                rows.append(("authorized_view", _access_member(entry, project), None))
                continue
            if "role" not in entry and set(entry) == {"routine"}:
                # A function's authorized routine entry comes back without a role.
                rows.append((None, _access_member(entry, project), None))
                continue
            if not _text(entry.get("role")):
                _readback_invalid()
            role = _ACCESS_ROLES.get(entry["role"], entry["role"])
            rows.append((role, _access_member(entry, project), None))
        return rows
    if "etag" in policy or "version" in policy:
        return rows
    _readback_invalid()


def _lookup_key(resource, project, project_number):
    key = resource.replace(f"/projects/{project_number}/", f"/projects/{project}/")
    if "//secretmanager.googleapis.com/" in key and "/versions/" in key:
        key = key.rsplit("/versions/", 1)[0]
    return key


def _report_row(resource, role, member, condition, **extra):
    row = {"condition": condition, "member": member, "resource": resource, "role": role}
    row.update(extra)
    return row


def diff_against_readback(delta: dict, readback: dict) -> dict:
    if type(readback) is not dict or type(readback.get("policies")) is not dict:
        _readback_invalid()
    if any(not _text(key) for key in readback["policies"]):
        _readback_invalid()
    definitions = readback.get("roles", {})
    if type(definitions) is not dict or any(not _text(key) for key in definitions):
        _readback_invalid()
    for role, definition in definitions.items():
        validate_custom_role_definition(role, definition)
    project, number = delta["project"], delta["project_number"]
    manifest_rows = {row["resource"] for row in delta["create"]}
    manifest_rows |= {row["resource"] for row in delta["existing"]}
    identities = {
        "serviceAccount:" + name.rsplit("/", 1)[1]
        for name in manifest_rows
        if "/serviceAccounts/" in name
    }
    expected = {}
    for row in delta["bindings"]:
        key = _lookup_key(row["resource"], project, number)
        expected.setdefault(key, []).append(row)
    retained = {}
    for row in delta["retained"]:
        key = _lookup_key(row["resource"], project, number)
        retained.setdefault(key, []).append(row)
    authorized = {
        (row["dataset"], "routine:" + row["routine"], row["role"])
        for row in delta["routine_authorizations"]
    }
    allowed_conditions = {
        key: {row["condition"] for row in rows if row["condition"] is not None}
        for key, rows in expected.items()
    }
    report = {
        name: []
        for name in (
            "present",
            "missing",
            "extra",
            "unexpected",
            "existing_broad_grants_not_isolated",
            "retained_present",
            "retained_absent",
            "unproven",
        )
    }
    policies = {}
    for key, policy in readback["policies"].items():
        policies[_lookup_key(key, project, number)] = _flatten(policy, project)
    for key, rows in policies.items():
        wanted = expected.get(key, [])
        kept = retained.get(key, [])
        if rows is None:
            report["unproven"].append(
                {"expected": len(wanted), "reason": "refused", "resource": key}
            )
            continue
        matched = set()
        kept_seen = set()
        bucket = key.startswith("//storage.googleapis.com/")
        for role, member, condition in rows:
            row = _report_row(key, role, member, condition)
            hit = [
                index
                for index, want in enumerate(wanted)
                if (want["member"], want["role"], want["condition"])
                == (member, role, condition)
            ]
            if hit:
                matched.add(hit[0])
                report["present"].append(row)
                continue
            retained_hit = False
            for index, want in enumerate(kept):
                if (want["member"], want["role"], want["condition"]) == (
                    member,
                    role,
                    condition,
                ):
                    kept_seen.add(index)
                    retained_hit = True
            if retained_hit:
                report["retained_present"].append(row)
            finding = None
            if key == f"//cloudresourcemanager.googleapis.com/projects/{project}":
                if role in _BROAD_PROJECT_ROLES:
                    finding = "project_wide_role"
            elif bucket and condition is None and role in _BUCKET_OBJECT_ROLES:
                finding = "whole_bucket_role"
            elif bucket and condition is not None:
                if condition not in allowed_conditions.get(key, set()):
                    finding = "conditional_grant_outside_allowlist"
            elif member.startswith("routine:"):
                routine = member.split(":", 1)[1]
                if (key, member, role) in authorized:
                    report["present"].append(row)
                    continue
                if routine in _LEGACY_ROUTINES or routine not in manifest_rows:
                    finding = "legacy_routine_authorization"
                else:
                    report["retained_present"].append(
                        _report_row(
                            key, role, member, condition, note="manifest_v1_routine"
                        )
                    )
                    continue
            if finding is not None:
                report["existing_broad_grants_not_isolated"].append(
                    _report_row(key, role, member, condition, finding=finding)
                )
            elif retained_hit:
                pass
            elif member in identities:
                report["extra"].append(row)
            else:
                report["unexpected"].append(row)
        for index, want in enumerate(wanted):
            if index not in matched:
                report["missing"].append(
                    _report_row(key, want["role"], want["member"], want["condition"])
                )
        for index, want in enumerate(kept):
            if index not in kept_seen:
                report["retained_absent"].append(
                    _report_row(key, want["role"], want["member"], want["condition"])
                )
    for key, wanted in expected.items():
        if key not in policies:
            report["unproven"].append(
                {"expected": len(wanted), "reason": "unread", "resource": key}
            )
    for role in CUSTOM_ROLE_PERMISSIONS:
        bound = [row for row in delta["bindings"] if row["role"] == role]
        if bound and role not in definitions:
            report["unproven"].append(
                {
                    "expected": len(bound),
                    "reason": "role_definition_unread",
                    "resource": role,
                }
            )
    for name, rows in report.items():
        rows.sort(key=lambda row: json.dumps(row, sort_keys=True))
    report["summary"] = {name: len(rows) for name, rows in report.items()}
    return report


def _custom_role_mismatch():
    raise ValueError("iam_custom_role_mismatch")


def validate_custom_role_definition(role: str, definition: dict) -> dict:
    """Check a custom role read back from IAM against its pinned permission list."""
    pinned = CUSTOM_ROLE_PERMISSIONS.get(role)
    if pinned is None or type(definition) is not dict:
        _custom_role_mismatch()
    if definition.get("name") != role:
        _custom_role_mismatch()
    if definition.get("deleted", False) is not False:
        _custom_role_mismatch()
    if definition.get("stage") == "DISABLED":
        _custom_role_mismatch()
    permissions = definition.get("includedPermissions")
    if type(permissions) is not list or not all(type(p) is str for p in permissions):
        _custom_role_mismatch()
    if len(permissions) != len(set(permissions)):
        _custom_role_mismatch()
    if sorted(permissions) != sorted(pinned):
        _custom_role_mismatch()
    return {"role": role, "permissions": sorted(pinned)}


__all__ = [
    "CUSTOM_ROLE_PERMISSIONS",
    "MANIFEST_SHA256",
    "diff_against_readback",
    "load_iam_delta",
    "validate_custom_role_definition",
    "validate_iam_delta",
]
