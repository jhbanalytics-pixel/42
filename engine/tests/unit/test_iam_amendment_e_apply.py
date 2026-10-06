"""Owner run amendment e words: iam-e-plan, iam-e-dry-run and iam-e-apply DIGEST.

The words grant exactly the rows the provisioning delta adds over the retained
amendment d delta, whatever those rows are: custom roles held to an exact permission
set, routine authorizations, routine, dataset, table, bucket, Cloud Run job and
project bindings. The delta is bound by the digest of the bytes read at run time, and
the apply takes the approved digest as its argument. Writes add only, carry the etag
they read and are read back, over faked BigQuery, Storage, Cloud Run, IAM and
Resource Manager transports at the session seam the amendment d words use.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from scripts.migrations import create_open_intelligence_execution_approval_store as migration

ENGINE = Path(__file__).resolve().parents[2]
# A copy of ops/deploy/iam_delta_v1.json as proposed at 33ad00c0 (round 10). The words carry no pin
# of it, so a later round needs no change here; tests/repository reads the live file.
FIXTURE = ENGINE / "tests" / "fixtures" / "iam_delta_amendment_e.json"
BASE = ENGINE / "configs" / "open_intelligence" / "iam_delta_amendment_d.json"
MANIFEST = ENGINE / "configs" / "open_intelligence" / "resource_manifest_v1.json"
DELTA_SHA256 = "33ad00c0254fd0e514c56bf17b98571f95608ab30604a2523499a7ddf37bda9f"
BASE_SHA256 = "1d2e8ead5a7c17c51b53ca640ed4c295294aef204ce4abeee9f03c7e822d5e3e"
MANIFEST_SHA256 = "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09"

PROJECT = "ogilvy-trends-v2"
BQ = "//bigquery.googleapis.com/"
APPROVALS = f"projects/{PROJECT}/datasets/trends_v2_staging_approvals"
BQ_API = "https://bigquery.googleapis.com/bigquery/v2/"
APPROVALS_URL = f"{BQ_API}{APPROVALS}"
SOURCES_URL = f"{BQ_API}projects/{PROJECT}/datasets/intelligence_42_sources_staging"
PROJECT_URL = f"https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT}"
STORAGE_URL = "https://storage.googleapis.com/storage/v1/b/"
RUN_URL = f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/us-central1/jobs/"
ROLES_URL = f"https://iam.googleapis.com/v1/projects/{PROJECT}/roles"
DAILY_JOB = "intelligence-42-daily-staging"
INGEST_JOB = "intelligence-42-ingest-staging"
FUNDED_JOB = "intelligence-42-funded-pilot-staging"
APPROVALS_BUCKET = "ogilvy-trends-v2-execution-approvals-staging"
CACHE_BUCKET = "listening-post-staging-cache"
SA = f"@{PROJECT}.iam.gserviceaccount.com"
ALBERT = "user:albert.meintjes@ogilvy.co.za"
BRAIN = f"serviceAccount:intelligence-42-brain{SA}"
ORCHESTRATION = f"serviceAccount:intelligence-42-orchestration{SA}"
PRICE_POLICY = f"serviceAccount:intelligence-42-price-policy{SA}"
LEGACY_BUILDS = f"serviceAccount:legacy-builds{SA}"
DEPLOY = f"serviceAccount:intelligence-42-deploy{SA}"
LISTENING = f"serviceAccount:listening-post-staging{SA}"
JOBS_GET_ID = "intelligence42JobsGet"
JOBS_GET = f"projects/{PROJECT}/roles/{JOBS_GET_ID}"
TABLES = (
    "open_intelligence_execution_approvals_v2",
    "open_intelligence_execution_consumptions_v2",
    "open_intelligence_execution_results_v2",
)
TABLE_KEYS = [f"trends_v2_staging_approvals/tables/{name}" for name in TABLES]
TABLE_RESOURCES = [f"{BQ}projects/{PROJECT}/datasets/{key}" for key in TABLE_KEYS]
CAPTURE_JOB_READ_ID = "CaptureJobRead"
CAPTURE_JOB_READ = f"projects/{PROJECT}/roles/{CAPTURE_JOB_READ_ID}"
INGEST_RESOURCE = f"//run.googleapis.com/projects/{PROJECT}/locations/us-central1/jobs/{INGEST_JOB}"
SECRET_TOKEN = "ya29.fake-access-token-never-printed"
UDF = "fn_is_canonical_execution_json_v1"
RECONCILE = "sp_reconcile_open_intelligence_daily_consumption_v1"
SNAPSHOT_V3 = "sp_consume_open_intelligence_source_snapshot_v3"
BUILDS_VIEWER = "roles/cloudbuild.builds.viewer"
OPERATOR_ROUTINES = {
    "sp_approve_open_intelligence_execution_v2",
    "sp_approve_open_intelligence_execution_v3",
    "sp_disable_open_intelligence_execution_approval_v2",
    UDF,
    "sp_approve_open_intelligence_recurring_grant_v2",
    "sp_read_open_intelligence_recurring_grant_v2",
    "sp_disable_open_intelligence_recurring_grant_v2",
    RECONCILE,
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _key(row: dict) -> str:
    return json.dumps(row, sort_keys=True)


def _added(group: str) -> list[dict]:
    base = {_key(row) for row in _load(BASE)[group]}
    return [row for row in _load(FIXTURE)[group] if _key(row) not in base]


def _added_bindings(prefix: str) -> list[dict]:
    return [row for row in _added("bindings") if row["resource"].startswith(prefix)]


ROUTINE_ROWS = _added_bindings(BQ + APPROVALS + "/routines/")
DATASET_ROWS = [
    row
    for row in _added_bindings(BQ + f"projects/{PROJECT}/datasets/")
    if "/routines/" not in row["resource"] and "/tables/" not in row["resource"]
]
TABLE_ROWS = [row for row in _added("bindings") if "/tables/" in row["resource"]]
BUCKET_ROWS = _added_bindings("//storage.googleapis.com/")
PROJECT_ROWS = _added_bindings("//cloudresourcemanager.googleapis.com/")
JOB_ROWS = _added_bindings("//run.googleapis.com/")
(DAILY_ROW,) = [row for row in JOB_ROWS if row["resource"].endswith("/" + DAILY_JOB)]


class _Response:
    def __init__(self, status: int, payload: object):
        self.status_code = status
        self._payload = deepcopy(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> object:
        return deepcopy(self._payload)


class _FakeCloud:
    """Datasets, routines, tables, buckets, jobs, custom roles and the project, etag guarded."""

    def __init__(self):
        self._counter = 0
        self.datasets = {
            APPROVALS_URL: {
                "etag": self._next("approvals"),
                "access": [
                    {"role": "OWNER", "specialGroup": "projectOwners"},
                    {
                        "role": "roles/bigquery.routineDataEditor",
                        "routine": {
                            "projectId": PROJECT,
                            "datasetId": "trends_v2_staging_approvals",
                            "routineId": "sp_consume_open_intelligence_execution_v2",
                        },
                    },
                    # Amendment d landed the function's entry without a role.
                    {
                        "routine": {
                            "projectId": PROJECT,
                            "datasetId": "trends_v2_staging_approvals",
                            "routineId": UDF,
                        }
                    },
                ],
            },
            SOURCES_URL: {
                "etag": self._next("sources"),
                "access": [
                    {"role": "OWNER", "specialGroup": "projectOwners"},
                    {"role": "WRITER", "userByEmail": f"intelligence-42-ingest{SA}"},
                ],
            },
        }
        self.routine_types = {UDF: "SCALAR_FUNCTION"}
        self.absent_routines = {RECONCILE, SNAPSHOT_V3}
        self.routine_policies = {
            UDF: {
                "etag": self._next("udf"),
                "version": 1,
                "bindings": [{"role": "roles/bigquery.dataViewer", "members": [ALBERT]}],
            }
        }
        self.tables = {
            key: {"etag": self._next("table"), "version": 1, "bindings": []} for key in TABLE_KEYS
        }
        self.jobs = {
            name: {
                "etag": self._next("job"),
                "version": 1,
                "bindings": [{"role": "roles/run.developer", "members": [DEPLOY]}],
            }
            for name in (DAILY_JOB, FUNDED_JOB)
        }
        self.roles: dict[str, dict] = {}
        self.buckets = {
            APPROVALS_BUCKET: {
                "etag": self._next("bucket"),
                "version": 3,
                "bindings": [
                    {"role": "roles/storage.admin", "members": [ALBERT]},
                    {
                        "role": "roles/storage.objectViewer",
                        "members": [ORCHESTRATION],
                        "condition": {
                            "title": "kept",
                            "expression": "resource.name.startsWith('projects/_/buckets/"
                            f"{APPROVALS_BUCKET}/objects/42/other/')",
                        },
                    },
                ],
            },
            CACHE_BUCKET: {
                "etag": self._next("bucket"),
                "version": 1,
                "bindings": [{"role": "roles/storage.legacyBucketOwner", "members": [ALBERT]}],
            },
        }
        self.project = {
            "etag": self._next("project"),
            "version": 3,
            "bindings": [
                {"role": "roles/owner", "members": [ALBERT]},
                {"role": BUILDS_VIEWER, "members": [LEGACY_BUILDS]},
                {
                    "role": "roles/bigquery.jobUser",
                    "members": [BRAIN],
                    "condition": {
                        "title": "t",
                        "expression": "request.time < timestamp('2030-01-01T00:00:00Z')",
                    },
                },
            ],
            "auditConfigs": [
                {"service": "allServices", "auditLogConfigs": [{"logType": "DATA_READ"}]}
            ],
        }
        self.writes: list[tuple[str, str, object]] = []
        self.reads: list[tuple[str, str]] = []
        self.refuse_writes: set[str] = set()
        self.drop_writes: set[str] = set()
        self.bump_etag_before_write: set[str] = set()
        # The kinds whose policy read answers a missing resource with an empty policy
        # instead of an error. Cloud Run does, as the apply on 35dd1c2 saw for a job
        # that did not exist; a table or bucket is added here to prove the words rely
        # on no policy read to say a resource exists.
        self.empty_policy_when_missing: set[str] = {"runtime_job"}
        # A status the GET of a resource itself answers in place of the resource.
        self.existence_errors: dict[str, int] = {}

    def _next(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-etag-{self._counter}"

    def install_all_routines(self) -> None:
        self.absent_routines = set()

    def install_ingest_job(self) -> None:
        self.jobs[INGEST_JOB] = {
            "etag": self._next("job"),
            "version": 1,
            "bindings": [{"role": "roles/run.developer", "members": [DEPLOY]}],
        }

    def add_role(self, role_id: str, permissions: list[str], **fields) -> None:
        self.roles[role_id] = {
            "name": f"projects/{PROJECT}/roles/{role_id}",
            "title": "live",
            "description": "",
            "includedPermissions": list(permissions),
            "stage": "GA",
            "etag": self._next("role"),
            **fields,
        }

    def _policy_store(self, url: str, verb: str):
        """The dict and key holding the policy the url names, or None when it is absent."""

        base = url.removesuffix(":" + verb)
        if base == PROJECT_URL:
            return self.__dict__, "project"
        if base.startswith(APPROVALS_URL + "/routines/"):
            name = base.removeprefix(APPROVALS_URL + "/routines/")
            if name in self.absent_routines:
                return None
            self.routine_policies.setdefault(name, {"etag": self._next("routine")})
            return self.routine_policies, name
        if base.startswith(BQ_API) and "/tables/" in base:
            key = base.removeprefix(f"{BQ_API}projects/{PROJECT}/datasets/")
            return (self.tables, key) if key in self.tables else None
        if base.startswith(RUN_URL):
            name = base.removeprefix(RUN_URL)
            return (self.jobs, name) if name in self.jobs else None
        return None

    def _empty_policy(self) -> dict:
        return {"etag": self._next("empty")}

    def get(self, url, params=None, **_kwargs):
        self.reads.append(("get", url))
        if url in self.existence_errors:
            return _Response(self.existence_errors[url], {"error": "refused"})
        if url.startswith(RUN_URL) and ":" not in url.removeprefix(RUN_URL):
            name = url.removeprefix(RUN_URL)
            if name in self.jobs:
                return _Response(200, {"name": url.removeprefix("https://run.googleapis.com/v2/")})
            return _Response(404, {"error": {"code": 404, "status": "NOT_FOUND"}})
        if url.startswith(f"{BQ_API}projects/{PROJECT}/datasets/") and "/tables/" in url:
            key = url.removeprefix(f"{BQ_API}projects/{PROJECT}/datasets/")
            if key in self.tables:
                return _Response(200, {"id": f"{PROJECT}:{key}", "type": "TABLE"})
            return _Response(404, {"error": "not found"})
        if url.startswith(STORAGE_URL) and "/" not in url.removeprefix(STORAGE_URL):
            bucket = url.removeprefix(STORAGE_URL)
            if bucket in self.buckets:
                return _Response(200, {"name": bucket})
            return _Response(404, {"error": "not found"})
        if url.startswith(APPROVALS_URL + "/routines/"):
            name = url.removeprefix(APPROVALS_URL + "/routines/")
            if name in self.absent_routines:
                return _Response(404, {"error": "not found"})
            return _Response(200, {"routineType": self.routine_types.get(name, "PROCEDURE")})
        if url in self.datasets:
            return _Response(200, self.datasets[url])
        if url.startswith(STORAGE_URL) and url.endswith("/iam"):
            bucket = url.removeprefix(STORAGE_URL).removesuffix("/iam")
            if (params or {}).get("optionsRequestedPolicyVersion") != 3:
                return _Response(400, {"error": "conditions need version 3"})
            if bucket in self.buckets:
                return _Response(200, self.buckets[bucket])
            if "bucket" in self.empty_policy_when_missing:
                return _Response(200, self._empty_policy())
        if url.startswith(RUN_URL) and url.endswith(":getIamPolicy"):
            if (params or {}).get("options.requestedPolicyVersion") != 3:
                return _Response(400, {"error": "conditions need version 3"})
            store = self._policy_store(url, "getIamPolicy")
            if store is not None:
                return _Response(200, store[0][store[1]])
            if "runtime_job" in self.empty_policy_when_missing:
                return _Response(200, self._empty_policy())
        if url.startswith(ROLES_URL + "/"):
            role = self.roles.get(url.removeprefix(ROLES_URL + "/"))
            if role is not None:
                return _Response(200, role)
        return _Response(404, {"error": "not found"})

    def patch(self, url, *, params=None, json=None, headers=None):
        self.writes.append(
            ("patch", url, {"params": params, "json": deepcopy(json), "headers": headers})
        )
        if url.startswith(ROLES_URL + "/"):
            return self._update_role(url, params, json)
        if url not in self.datasets:
            return _Response(404, {"error": "not found"})
        dataset = self.datasets[url]
        if url in self.bump_etag_before_write:
            dataset["etag"] = self._next("concurrent")
        if (headers or {}).get("If-Match") != dataset["etag"]:
            return _Response(412, {"error": "precondition failed"})
        if url in self.refuse_writes:
            return _Response(403, {"error": "refused"})
        if url in self.drop_writes:
            return _Response(200, dataset)
        for entry in json["access"]:
            routine = entry.get("routine")
            if (
                routine
                and "role" in entry
                and self.routine_types.get(routine["routineId"]) == "SCALAR_FUNCTION"
            ):
                return _Response(400, {"error": "Role is not supported for routine"})
        dataset["access"] = deepcopy(json["access"])
        dataset["etag"] = self._next("dataset")
        return _Response(200, dataset)

    def _update_role(self, url, params, body):
        role = self.roles.get(url.removeprefix(ROLES_URL + "/"))
        if role is None:
            return _Response(404, {"error": "not found"})
        if url in self.bump_etag_before_write:
            role["etag"] = self._next("concurrent")
        if params != {"updateMask": "includedPermissions"}:
            return _Response(400, {"error": "update mask"})
        if body.get("etag") != role["etag"]:
            return _Response(409, {"error": "etag mismatch"})
        if url in self.refuse_writes:
            return _Response(403, {"error": "refused"})
        if url in self.drop_writes:
            return _Response(200, role)
        role["includedPermissions"] = list(body["includedPermissions"])
        role["etag"] = self._next("role")
        return _Response(200, role)

    def _create_role(self, body):
        role_id = body["roleId"]
        url = f"{ROLES_URL}/{role_id}"
        if role_id in self.roles:
            return _Response(409, {"error": "already exists"})
        if url in self.refuse_writes:
            return _Response(403, {"error": "refused"})
        if url in self.drop_writes:
            return _Response(200, {})
        self.add_role(
            role_id,
            body["role"]["includedPermissions"],
            **{key: value for key, value in body["role"].items() if key != "includedPermissions"},
        )
        return _Response(200, self.roles[role_id])

    def post(self, url, json=None, **_kwargs):
        if url == ROLES_URL:
            self.writes.append(("post", url, deepcopy(json)))
            return self._create_role(json)
        if url.endswith(":getIamPolicy"):
            self.reads.append(("post", url))
            assert (json or {}).get("options", {}).get("requestedPolicyVersion") == 3
            assert not url.startswith(RUN_URL)
            store = self._policy_store(url, "getIamPolicy")
            if store is None:
                if "/tables/" in url and "table" in self.empty_policy_when_missing:
                    return _Response(200, self._empty_policy())
                return _Response(404, {"error": "not found"})
            return _Response(200, store[0][store[1]])
        if url.endswith(":setIamPolicy"):
            self.writes.append(("post", url, deepcopy(json)))
            policy = dict(json["policy"])
            store = self._policy_store(url, "setIamPolicy")
            if store is None:
                return _Response(404, {"error": "not found"})
            container, key = store
            current = container[key]
            if url in self.bump_etag_before_write:
                current["etag"] = self._next("concurrent")
            if policy.get("etag") != current.get("etag"):
                return _Response(409, {"error": "etag mismatch"})
            if url in self.refuse_writes:
                return _Response(403, {"error": "refused"})
            if url in self.drop_writes:
                return _Response(200, current)
            for item in policy.get("bindings", ()):
                role = item["role"]
                custom = role.startswith(f"projects/{PROJECT}/roles/")
                if custom and role.rsplit("/", 1)[1] not in self.roles:
                    return _Response(400, {"error": "role does not exist"})
            policy["etag"] = self._next("set")
            container[key] = policy
            return _Response(200, policy)
        return _Response(404, {"error": "not found"})

    # Storage

    def put(self, url, json=None, **_kwargs):
        self.writes.append(("put", url, deepcopy(json)))
        bucket = url.removeprefix(STORAGE_URL).removesuffix("/iam")
        if bucket not in self.buckets or not url.endswith("/iam"):
            return _Response(404, {"error": "not found"})
        current = self.buckets[bucket]
        if url in self.bump_etag_before_write:
            current["etag"] = self._next("concurrent")
        if json.get("etag") != current["etag"]:
            return _Response(412, {"error": "precondition failed"})
        if url in self.refuse_writes:
            return _Response(403, {"error": "refused"})
        if url in self.drop_writes:
            return _Response(200, current)
        if any("condition" in item for item in json["bindings"]) and json.get("version") != 3:
            return _Response(400, {"error": "conditions need version 3"})
        policy = deepcopy(json)
        policy["etag"] = self._next("bucket")
        self.buckets[bucket] = policy
        return _Response(200, policy)

    # Views

    def grants(self) -> set[tuple[str, str, str, str]]:
        """(resource, role, member, condition expression) for every grant held."""

        rows = set()
        policies = [(f"routine:{name}", policy) for name, policy in self.routine_policies.items()]
        policies += [(f"table:{key}", policy) for key, policy in self.tables.items()]
        policies += [(f"job:{name}", policy) for name, policy in self.jobs.items()]
        policies += [(f"bucket:{name}", policy) for name, policy in self.buckets.items()]
        policies.append(("project", self.project))
        for resource, policy in policies:
            for item in policy.get("bindings", ()):
                expression = (item.get("condition") or {}).get("expression", "")
                for member in item["members"]:
                    rows.add((resource, item["role"], member, expression))
        for url, dataset in self.datasets.items():
            for entry in dataset["access"]:
                rows.add((f"dataset:{url}", "", json.dumps(entry, sort_keys=True), ""))
        for role_id, role in self.roles.items():
            rows.add((f"custom_role:{role_id}", "", ",".join(role["includedPermissions"]), ""))
        return rows


class _Credentials:
    token = SECRET_TOKEN


@pytest.fixture
def cloud(monkeypatch):
    fake = _FakeCloud()
    monkeypatch.setattr(migration, "IAM_E_DELTA_PATH", FIXTURE)
    monkeypatch.setattr(migration, "_load_credentials", lambda: _Credentials())
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: fake)
    return fake


def _write_delta(tmp_path, monkeypatch, delta: dict) -> str:
    """Stand in for a delta with different bytes, and return the digest of those bytes."""

    path = tmp_path / "iam_delta_v1.json"
    raw = (json.dumps(delta, indent=2) + "\n").encode("utf-8")
    path.write_bytes(raw)
    monkeypatch.setattr(migration, "IAM_E_DELTA_PATH", path)
    return hashlib.sha256(raw).hexdigest()


def _approve(delta: dict) -> None:
    delta["approval"] = {
        "applied": False,
        "approved_at": "2026-09-24T08:00:00+00:00",
        "approved_by": "Albert Meintjes, amendment e",
        "state": "approved",
    }


def _approved(tmp_path, monkeypatch, mutate=None) -> str:
    delta = _load(FIXTURE)
    _approve(delta)
    if mutate is not None:
        mutate(delta)
    return _write_delta(tmp_path, monkeypatch, delta)


def _run(capsys, *words: str) -> tuple[int, dict]:
    code = migration.main(list(words))
    captured = capsys.readouterr()
    assert SECRET_TOKEN not in captured.out + captured.err
    text = captured.out if code == 0 else captured.err
    return code, json.loads(text)


def _states(receipt: dict) -> dict[tuple[str, str, str, str], str]:
    return {
        (row["kind"], row["member"], row["resource"], row["role"]): row["state"]
        for row in receipt["rows"]
        if row["kind"] not in {"authorization", "custom_role"}
    }


def _short(resource: str) -> str:
    return resource.rsplit("/", 1)[1]


def _counts(target: int, **states: int) -> dict[str, int]:
    return {
        "target": target,
        **dict.fromkeys(
            ("present", "missing", "differs", "routine_missing", "resource_missing"), 0
        ),
        **states,
    }


def _planned(**writes: int) -> dict[str, int]:
    keys = (
        "custom_role_writes",
        "dataset_access_patches",
        "routine_policy_sets",
        "table_policy_sets",
        "bucket_policy_sets",
        "run_job_policy_sets",
        "project_policy_sets",
    )
    return {**dict.fromkeys(keys, 0), **writes}


# The inputs: the base is pinned, the delta is bound by the digest of its bytes


def test_the_base_is_pinned_and_the_delta_and_manifest_are_bound_by_digest_not_a_pin():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == DELTA_SHA256
    # No digest of the amendment e delta or its manifest is compiled into the words.
    assert not hasattr(migration, "IAM_E_DELTA_SHA256")
    assert not hasattr(migration, "IAM_E_MANIFEST_SHA256")
    assert hashlib.sha256(MANIFEST.read_bytes()).hexdigest() == MANIFEST_SHA256
    assert _load(FIXTURE)["resource_manifest_sha256"] == MANIFEST_SHA256
    assert migration.IAM_E_BASE_PATH == BASE
    assert migration.IAM_E_BASE_SHA256 == BASE_SHA256
    assert hashlib.sha256(BASE.read_bytes()).hexdigest() == BASE_SHA256


def test_the_targets_are_exactly_the_rows_amendment_e_adds(cloud):
    targets = migration.iam_e_targets()
    assert targets.delta_sha256 == DELTA_SHA256
    assert targets.manifest_sha256 == MANIFEST_SHA256
    # The delta binds CaptureJobRead and defines no custom role, so the role is held to
    # the permission list the delta validator pins for it.
    assert targets.custom_roles == (
        migration.IamECustomRole(
            CAPTURE_JOB_READ,
            CAPTURE_JOB_READ_ID,
            CAPTURE_JOB_READ_ID,
            migration._IAM_E_PINNED_DESCRIPTION,
            ("bigquery.jobs.get",),
        ),
    )
    authorizations = {
        (item.routine, item.role, item.routine_type) for item in targets.authorizations
    }
    assert authorizations == {
        (_short(row["routine"]), row["role"], row.get("routine_type"))
        for row in _added("routine_authorizations")
    }
    assert authorizations == {
        (RECONCILE, "roles/bigquery.routineDataEditor", None),
        (SNAPSHOT_V3, "roles/bigquery.routineDataEditor", None),
        (UDF, None, "SCALAR_FUNCTION"),
    }

    def rows(kind):
        return {
            (row.member, row.resource, row.role, row.condition)
            for row in targets.bindings
            if row.kind == kind
        }

    def expected(rows_):
        return {(row["member"], row["resource"], row["role"], row["condition"]) for row in rows_}

    assert rows("routine") == expected(ROUTINE_ROWS)
    assert rows("dataset") == expected(DATASET_ROWS)
    assert rows("bucket") == expected(BUCKET_ROWS)
    assert rows("project") == expected(PROJECT_ROWS)
    assert rows("runtime_job") == expected(JOB_ROWS)
    assert rows("table") == expected(TABLE_ROWS)
    counts = {
        kind: len(rows(kind))
        for kind in ("routine", "dataset", "table", "bucket", "project", "runtime_job")
    }
    assert counts == {
        "routine": 13,
        "dataset": 1,
        "table": 3,
        "bucket": 11,
        "project": 5,
        "runtime_job": 2,
    }
    assert len(targets.bindings) == len(_added("bindings")) == 35
    assert {row.ident for row in targets.bindings if row.kind == "table"} == set(TABLE_KEYS)
    # The operator is bound on the eight operator routines and nowhere else.
    operator = {_short(row.resource) for row in targets.bindings if row.member == ALBERT}
    assert operator == OPERATOR_ROUTINES
    assert {(row.member, row.role) for row in targets.bindings if row.kind == "project"} == {
        *((row["member"], BUILDS_VIEWER) for row in PROJECT_ROWS if row["role"] == BUILDS_VIEWER),
        (LISTENING, CAPTURE_JOB_READ),
    }


# Plan: read only, present and missing per row


def test_plan_reports_each_row_present_missing_or_routine_missing_and_writes_nothing(cloud, capsys):
    code, receipt = _run(capsys, "iam-e-plan")
    assert code == 0
    assert cloud.writes == []
    assert receipt["mode"] == "plan"
    assert receipt["delta_sha256"] == DELTA_SHA256
    assert receipt["base_delta_sha256"] == BASE_SHA256
    assert receipt["resource_manifest_sha256"] == MANIFEST_SHA256
    assert receipt["approval_state"] == "proposed"
    assert receipt["apply_ready"] is False
    assert receipt["routine_missing"] == sorted([RECONCILE, SNAPSHOT_V3])
    assert receipt["resource_missing"] == []
    states = _states(receipt)
    reconcile = BQ + APPROVALS + "/routines/" + RECONCILE
    snapshot = BQ + APPROVALS + "/routines/" + SNAPSHOT_V3
    udf = BQ + APPROVALS + "/routines/" + UDF
    assert states[("routine", ALBERT, reconcile, "roles/bigquery.dataViewer")] == "routine_missing"
    assert (
        states[("routine", ORCHESTRATION, snapshot, "roles/bigquery.dataViewer")]
        == "routine_missing"
    )
    # Albert's existing grant on the function is the H2 row, present already.
    assert states[("routine", ALBERT, udf, "roles/bigquery.dataViewer")] == "present"
    assert states[("routine", BRAIN, udf, "roles/bigquery.dataViewer")] == "missing"
    auth = {row["routine"]: row for row in receipt["rows"] if row["kind"] == "authorization"}
    assert auth[UDF]["state"] == "present"
    assert auth[UDF]["written_role"] is None
    assert auth[RECONCILE]["state"] == "routine_missing"
    assert auth[SNAPSHOT_V3]["state"] == "routine_missing"
    for row in PROJECT_ROWS + BUCKET_ROWS + DATASET_ROWS + TABLE_ROWS + JOB_ROWS:
        kind = {"//c": "project", "//s": "bucket", "//b": "dataset", "//r": "runtime_job"}[
            row["resource"][:3]
        ]
        kind = "table" if "/tables/" in row["resource"] else kind
        assert states[(kind, row["member"], row["resource"], row["role"])] == "missing"
    assert receipt["counts"] == {
        "authorization": _counts(3, present=1, routine_missing=2),
        "routine": _counts(13, present=1, missing=10, routine_missing=2),
        "dataset": _counts(1, missing=1),
        "bucket": _counts(11, missing=11),
        "table": _counts(3, missing=3),
        "runtime_job": _counts(2, missing=2),
        "project": _counts(5, missing=5),
        "custom_role": _counts(1, missing=1),
    }
    (role,) = [row for row in receipt["rows"] if row["kind"] == "custom_role"]
    assert role == {
        "kind": "custom_role",
        "role": CAPTURE_JOB_READ,
        "permissions": ["bigquery.jobs.get"],
        "live_permissions": None,
        "state": "missing",
    }
    # The job row is read like every other row, from the job's own policy.
    assert ("get", RUN_URL + DAILY_JOB + ":getIamPolicy") in cloud.reads


# Dry run: computes the writes, sends none, prints full policies


def test_dry_run_prints_every_full_policy_it_would_write_and_sends_nothing(cloud, capsys):
    cloud.install_all_routines()
    code, receipt = _run(capsys, "iam-e-dry-run")
    assert code == 0
    assert cloud.writes == []
    assert receipt["writes_planned"] == _planned(
        custom_role_writes=1,
        dataset_access_patches=2,
        routine_policy_sets=9,
        table_policy_sets=3,
        bucket_policy_sets=2,
        run_job_policy_sets=2,
        project_policy_sets=1,
    )
    policies = {(item["kind"], item["resource"]): item for item in receipt["policies_to_write"]}
    assert policies[("custom_role", CAPTURE_JOB_READ_ID)]["policy"] == {
        "roleId": CAPTURE_JOB_READ_ID,
        "role": {
            "title": CAPTURE_JOB_READ_ID,
            "description": migration._IAM_E_PINNED_DESCRIPTION,
            "includedPermissions": ["bigquery.jobs.get"],
            "stage": "GA",
        },
    }
    udf = policies[("routine", UDF)]["policy"]
    assert {
        "role": "roles/bigquery.dataViewer",
        "members": sorted(
            [
                ALBERT,
                BRAIN,
                f"serviceAccount:intelligence-42-funded{SA}",
                f"serviceAccount:intelligence-42-migration{SA}",
            ]
        ),
    } in udf["bindings"]
    project = policies[("project", PROJECT)]["policy"]
    viewers = [item for item in project["bindings"] if item["role"] == BUILDS_VIEWER]
    assert viewers == [
        {
            "role": BUILDS_VIEWER,
            "members": sorted(
                [
                    LEGACY_BUILDS,
                    *(row["member"] for row in PROJECT_ROWS if row["role"] == BUILDS_VIEWER),
                ]
            ),
        }
    ]
    assert {"role": CAPTURE_JOB_READ, "members": [LISTENING]} in project["bindings"]
    assert {"role": "roles/owner", "members": [ALBERT]} in project["bindings"]
    assert project["auditConfigs"] == cloud.project["auditConfigs"]
    assert "etag" not in project
    cache = policies[("bucket", CACHE_BUCKET)]["policy"]
    assert cache["version"] == 3
    conditions = {
        item["condition"]["expression"] for item in cache["bindings"] if "condition" in item
    }
    assert conditions == {
        row["condition"] for row in BUCKET_ROWS if CACHE_BUCKET in row["resource"]
    }
    for item in cache["bindings"]:
        if "condition" in item:
            assert item["condition"]["title"].startswith("42 iam delta v1 ")
    for key in TABLE_KEYS:
        assert policies[("table", key)]["policy"]["bindings"] == [
            {"role": "roles/bigquery.dataViewer", "members": [LISTENING]}
        ]
    assert policies[("runtime_job", FUNDED_JOB)]["policy"]["bindings"] == [
        {"role": "roles/run.developer", "members": [DEPLOY]},
        {"role": "roles/run.viewer", "members": [LISTENING]},
    ]
    job = policies[("runtime_job", DAILY_JOB)]["policy"]
    assert job["bindings"] == [
        {"role": "roles/run.developer", "members": [DEPLOY]},
        {"role": "roles/run.jobsExecutorWithOverrides", "members": [ORCHESTRATION]},
    ]
    sources = policies[("dataset", "intelligence_42_sources_staging")]["policy"]
    assert sources["access"][:2] == cloud.datasets[SOURCES_URL]["access"]
    assert sources["access"][2:] == [
        {"role": "READER", "userByEmail": f"intelligence-42-orchestration{SA}"}
    ]
    approvals = policies[("authorization", "trends_v2_staging_approvals")]["policy"]
    added = approvals["access"][len(cloud.datasets[APPROVALS_URL]["access"]) :]
    assert sorted(entry["routine"]["routineId"] for entry in added) == sorted(
        [RECONCILE, SNAPSHOT_V3]
    )
    assert all(entry["role"] == "roles/bigquery.routineDataEditor" for entry in added)
    assert len(receipt["planned_payload_sha256"]) == 64
    assert receipt["delta_sha256"] == DELTA_SHA256


# Apply: bound to the approved digest, owner account, additive, etag guarded, read back


@pytest.mark.parametrize(
    "words",
    [
        ["iam-e-apply"],
        ["iam-e-apply", DELTA_SHA256.upper()],
        ["iam-e-apply", DELTA_SHA256[:63]],
        ["iam-e-apply", DELTA_SHA256, DELTA_SHA256],
        ["iam-e-plan", DELTA_SHA256],
        ["iam-e-dry-run", DELTA_SHA256],
    ],
    ids=["apply_bare", "upper", "short", "two", "plan_with_digest", "dry_run_with_digest"],
)
def test_each_word_takes_exactly_its_arguments_and_the_apply_needs_the_digest(cloud, capsys, words):
    code, error = _run(capsys, *words)
    assert code == 2
    assert error == {"error": "execution_approval_iam_e_digest_required"}
    assert cloud.reads == []
    assert cloud.writes == []


def test_an_apply_with_any_digest_but_that_of_the_bytes_read_is_refused_before_any_read(
    cloud, capsys, tmp_path, monkeypatch
):
    approved = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    # The digest of the bytes Albert was shown, after the file changed under it.
    for digest in (DELTA_SHA256, "0" * 64, approved[:-1] + ("0" if approved[-1] != "0" else "1")):
        code, error = _run(capsys, "iam-e-apply", digest)
        assert code == 1
        assert error == {"error": "execution_approval_iam_e_delta_digest_mismatch"}
    assert cloud.reads == []
    assert cloud.writes == []


def test_plan_and_dry_run_report_the_digest_of_whatever_bytes_they_read(
    cloud, capsys, tmp_path, monkeypatch
):
    digest = _approved(tmp_path, monkeypatch)
    for word in ("iam-e-plan", "iam-e-dry-run"):
        code, receipt = _run(capsys, word)
        assert code == 0
        assert receipt["delta_sha256"] == digest != DELTA_SHA256
        assert receipt["approval_state"] == "approved"


def test_apply_refuses_a_delta_that_is_not_approved_before_reading_anything(
    cloud, capsys, tmp_path, monkeypatch
):
    delta = _load(FIXTURE)
    delta["approval"] = {
        "applied": False,
        "approved_at": None,
        "approved_by": None,
        "state": "proposed",
    }
    digest = _write_delta(tmp_path, monkeypatch, delta)
    cloud.install_all_routines()
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_delta_unapproved"}
    assert cloud.writes == []
    assert cloud.reads == []


def test_apply_refuses_while_a_routine_is_missing_and_sends_no_write(
    cloud, capsys, tmp_path, monkeypatch
):
    digest = _approved(tmp_path, monkeypatch)
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_routine_missing"
    assert error["routine_missing"] == sorted([RECONCILE, SNAPSHOT_V3])
    assert error["resource_missing"] == []
    assert cloud.writes == []


def test_apply_refuses_while_the_job_a_row_names_is_missing_and_sends_no_write(
    cloud, capsys, tmp_path, monkeypatch
):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    del cloud.jobs[DAILY_JOB]
    code, plan = _run(capsys, "iam-e-plan")
    assert code == 0
    job = DAILY_ROW
    assert _states(plan)[("runtime_job", job["member"], job["resource"], job["role"])] == (
        "resource_missing"
    )
    assert plan["resource_missing"] == [job["resource"]]
    assert plan["apply_ready"] is False
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error == {
        "error": "execution_approval_iam_e_resource_missing",
        "routine_missing": [],
        "resource_missing": [job["resource"]],
    }
    assert cloud.writes == []


def test_apply_adds_exactly_the_rows_with_read_etags_and_removes_nothing(
    cloud, capsys, tmp_path, monkeypatch
):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    before = cloud.grants()
    etags = {
        "approvals": cloud.datasets[APPROVALS_URL]["etag"],
        "sources": cloud.datasets[SOURCES_URL]["etag"],
        "project": cloud.project["etag"],
        "job": cloud.jobs[DAILY_JOB]["etag"],
        APPROVALS_BUCKET: cloud.buckets[APPROVALS_BUCKET]["etag"],
        CACHE_BUCKET: cloud.buckets[CACHE_BUCKET]["etag"],
    }
    code, receipt = _run(capsys, "iam-e-apply", digest)
    assert code == 0, receipt
    assert receipt["delta_sha256"] == digest
    patches = {url: body for verb, url, body in cloud.writes if verb == "patch"}
    assert patches[APPROVALS_URL]["headers"] == {"If-Match": etags["approvals"]}
    assert patches[SOURCES_URL]["headers"] == {"If-Match": etags["sources"]}
    puts = {url: body for verb, url, body in cloud.writes if verb == "put"}
    assert puts[STORAGE_URL + APPROVALS_BUCKET + "/iam"]["etag"] == etags[APPROVALS_BUCKET]
    assert puts[STORAGE_URL + CACHE_BUCKET + "/iam"]["etag"] == etags[CACHE_BUCKET]
    sets = {url: body for verb, url, body in cloud.writes if verb == "post"}
    assert sets[PROJECT_URL + ":setIamPolicy"]["policy"]["etag"] == etags["project"]
    assert sets[RUN_URL + DAILY_JOB + ":setIamPolicy"]["policy"]["etag"] == etags["job"]
    assert len(cloud.writes) == 20
    # The custom role is the first write, and the project policy that names it the last.
    assert cloud.writes[0] == ("post", ROLES_URL, cloud.writes[0][2])
    assert cloud.writes[0][2]["roleId"] == CAPTURE_JOB_READ_ID
    assert cloud.writes[-1][1] == PROJECT_URL + ":setIamPolicy"
    after = cloud.grants()
    # Nothing that was held before is gone, and what is new is exactly the delta rows.
    kept = {row for row in before if not row[0].startswith("dataset:")}
    assert kept <= after
    for url in (APPROVALS_URL, SOURCES_URL):
        old = [json.dumps(entry, sort_keys=True) for entry in _FakeCloud().datasets[url]["access"]]
        new = [json.dumps(entry, sort_keys=True) for entry in cloud.datasets[url]["access"]]
        assert new[: len(old)] == old
    added = {row for row in after - before if not row[0].startswith("dataset:")}
    expected = set()
    for row in ROUTINE_ROWS:
        if row["member"] == ALBERT and row["resource"].endswith(UDF):
            continue
        expected.add((f"routine:{_short(row['resource'])}", row["role"], row["member"], ""))
    for row in BUCKET_ROWS:
        expected.add(
            (f"bucket:{_short(row['resource'])}", row["role"], row["member"], row["condition"])
        )
    for row in PROJECT_ROWS:
        expected.add(("project", row["role"], row["member"], ""))
    for row in JOB_ROWS:
        expected.add((f"job:{_short(row['resource'])}", row["role"], row["member"], ""))
    for row in TABLE_ROWS:
        key = row["resource"].removeprefix(f"{BQ}projects/{PROJECT}/datasets/")
        expected.add((f"table:{key}", row["role"], row["member"], ""))
    expected.add((f"custom_role:{CAPTURE_JOB_READ_ID}", "", "bigquery.jobs.get", ""))
    assert added == expected
    assert (f"routine:{UDF}", "roles/bigquery.dataViewer", ALBERT, "") in after
    assert cloud.project["auditConfigs"] == _FakeCloud().project["auditConfigs"]
    assert cloud.project["version"] == 3
    assert receipt["applied"]["failed_at"] is None
    assert len(receipt["applied"]["writes"]) == 20
    assert receipt["counts"]["project"] == {
        "target": 5,
        "present_before": 0,
        "added": 5,
        "present_after": 5,
    }
    assert receipt["counts"]["custom_role"] == {
        "target": 1,
        "present_before": 0,
        "added": 1,
        "present_after": 1,
    }
    assert receipt["counts"]["runtime_job"] == {
        "target": 2,
        "present_before": 0,
        "added": 2,
        "present_after": 2,
    }
    assert receipt["counts"]["table"] == {
        "target": 3,
        "present_before": 0,
        "added": 3,
        "present_after": 3,
    }
    assert receipt["counts"]["authorization"] == {
        "target": 3,
        "present_before": 1,
        "added": 2,
        "present_after": 3,
    }
    # The plan run afterwards is the readback: every row is present.
    code, plan = _run(capsys, "iam-e-plan")
    assert code == 0
    assert {row["state"] for row in plan["rows"]} == {"present"}
    assert plan["readback_sha256"] == receipt["readback_sha256"]


def test_a_second_apply_adds_nothing_and_sends_no_write(cloud, capsys, tmp_path, monkeypatch):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    assert _run(capsys, "iam-e-apply", digest)[0] == 0
    first = cloud.grants()
    cloud.writes.clear()
    code, receipt = _run(capsys, "iam-e-apply", digest)
    assert code == 0
    assert cloud.writes == []
    assert cloud.grants() == first
    assert receipt["writes_planned"] == _planned()
    assert all(item["added"] == 0 for item in receipt["counts"].values())


def test_a_failure_mid_apply_names_what_already_landed_and_a_rerun_adds_the_rest(
    cloud, capsys, tmp_path, monkeypatch
):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    failing = STORAGE_URL + APPROVALS_BUCKET + "/iam"
    cloud.refuse_writes.add(failing)
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_write_refused"
    applied = error["applied"]
    assert applied["failed_at"] == {"kind": "bucket", "resource": APPROVALS_BUCKET}
    landed = [(item["kind"], item["resource"]) for item in applied["writes"]]
    assert landed[:2] == [
        ("custom_role", CAPTURE_JOB_READ_ID),
        ("authorization", "trends_v2_staging_approvals"),
    ]
    assert ("dataset", "intelligence_42_sources_staging") in landed
    assert all(("table", key) in landed for key in TABLE_KEYS)
    assert landed[-1] == ("bucket", CACHE_BUCKET)
    assert len(landed) == 16
    assert ("bucket", APPROVALS_BUCKET) not in landed
    assert ("runtime_job", DAILY_JOB) not in landed
    assert ("project", PROJECT) not in landed
    cloud.refuse_writes.clear()
    cloud.writes.clear()
    code, receipt = _run(capsys, "iam-e-apply", digest)
    assert code == 0
    sent = [(verb, url) for verb, url, _body in cloud.writes]
    assert sent == [
        ("put", failing),
        ("post", RUN_URL + DAILY_JOB + ":setIamPolicy"),
        ("post", RUN_URL + FUNDED_JOB + ":setIamPolicy"),
        ("post", PROJECT_URL + ":setIamPolicy"),
    ]
    assert receipt["counts"]["bucket"] == {
        "target": 11,
        "present_before": 8,
        "added": 3,
        "present_after": 11,
    }


def test_a_write_that_does_not_land_fails_the_readback_with_what_was_sent(
    cloud, capsys, tmp_path, monkeypatch
):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    cloud.drop_writes.add(PROJECT_URL + ":setIamPolicy")
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_readback_mismatch"
    assert error["applied"]["failed_at"] == "readback"
    assert {"kind": "project", "resource": PROJECT} in error["applied"]["writes"]


def test_a_readback_that_lost_an_existing_grant_fails_closed(cloud, capsys, tmp_path, monkeypatch):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    landing = cloud.put

    def losing(url, json=None, **kwargs):
        response = landing(url, json=json, **kwargs)
        bucket = url.removeprefix(STORAGE_URL).removesuffix("/iam")
        if bucket == CACHE_BUCKET:
            # The write lands every row, and the owner binding read before is gone.
            cloud.buckets[bucket]["bindings"] = [
                item
                for item in cloud.buckets[bucket]["bindings"]
                if item["role"] != "roles/storage.legacyBucketOwner"
            ]
        return response

    monkeypatch.setattr(cloud, "put", losing)
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_readback_mismatch"
    assert error["applied"]["failed_at"] == "readback"
    assert len(error["applied"]["writes"]) == 20


def test_a_readback_that_lost_an_existing_dataset_entry_fails_closed(
    cloud, capsys, tmp_path, monkeypatch
):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    landing = cloud.patch

    def losing(url, **kwargs):
        response = landing(url, **kwargs)
        if url == SOURCES_URL:
            cloud.datasets[url]["access"] = [
                entry for entry in cloud.datasets[url]["access"] if entry.get("role") != "WRITER"
            ]
        return response

    monkeypatch.setattr(cloud, "patch", losing)
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_readback_mismatch"


@pytest.mark.parametrize(
    ("url", "kind", "resource", "held"),
    [
        (PROJECT_URL + ":setIamPolicy", "project", PROJECT, "project"),
        (RUN_URL + DAILY_JOB + ":setIamPolicy", "runtime_job", DAILY_JOB, f"job:{DAILY_JOB}"),
        (SOURCES_URL, "dataset", "intelligence_42_sources_staging", f"dataset:{SOURCES_URL}"),
    ],
    ids=["project", "job", "dataset"],
)
def test_a_concurrent_change_after_the_read_is_refused_by_the_etag(
    cloud, capsys, tmp_path, monkeypatch, url, kind, resource, held
):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    cloud.bump_etag_before_write.add(url)
    before = {row for row in cloud.grants() if row[0] == held}
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_write_refused"
    assert error["applied"]["failed_at"] == {"kind": kind, "resource": resource}
    assert {row for row in cloud.grants() if row[0] == held} == before


def test_receipts_and_errors_carry_no_token_and_no_etag(cloud, capsys, tmp_path, monkeypatch):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    for words in (["iam-e-plan"], ["iam-e-dry-run"], ["iam-e-apply", digest]):
        code = migration.main(words)
        captured = capsys.readouterr()
        assert code == 0
        assert SECRET_TOKEN not in captured.out + captured.err
        assert "etag" not in captured.out


# Refusals of the inputs and the rows


def test_a_manifest_whose_bytes_differ_from_the_digest_the_delta_names_is_refused(
    cloud, capsys, tmp_path, monkeypatch
):
    path = tmp_path / "resource_manifest_v1.json"
    path.write_bytes(MANIFEST.read_bytes() + b"\n")
    monkeypatch.setattr(migration, "RESOURCE_MANIFEST_PATH", path)
    code, error = _run(capsys, "iam-e-plan")
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_manifest_digest_mismatch"}
    assert cloud.reads == []


@pytest.mark.parametrize(
    "digest",
    ["ee809a4e81dec5242ea71ddd703990c5e0a9e237613e11135e069be0ea51ad96", None, 7],
)
def test_a_delta_naming_another_manifest_is_refused(cloud, capsys, tmp_path, monkeypatch, digest):
    delta = _load(FIXTURE)
    delta["resource_manifest_sha256"] = digest
    _write_delta(tmp_path, monkeypatch, delta)
    code, error = _run(capsys, "iam-e-plan")
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_manifest_digest_mismatch"}


def test_a_changed_base_delta_is_refused(cloud, capsys, tmp_path, monkeypatch):
    path = tmp_path / "iam_delta_amendment_d.json"
    path.write_bytes(BASE.read_bytes() + b"\n")
    monkeypatch.setattr(migration, "IAM_E_BASE_PATH", path)
    code, error = _run(capsys, "iam-e-plan")
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_delta_digest_mismatch"}


def test_a_delta_that_is_not_json_is_refused(cloud, capsys, tmp_path, monkeypatch):
    path = tmp_path / "iam_delta_v1.json"
    path.write_bytes(b"[]\n")
    monkeypatch.setattr(migration, "IAM_E_DELTA_PATH", path)
    code, error = _run(capsys, "iam-e-plan")
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_delta_invalid"}


def _mutated(tmp_path, monkeypatch, capsys, mutate, word="iam-e-plan"):
    delta = _load(FIXTURE)
    mutate(delta)
    _write_delta(tmp_path, monkeypatch, delta)
    return _run(capsys, word)


def _row(delta: dict, predicate) -> dict:
    (row,) = [row for row in delta["bindings"] if predicate(row)]
    return row


def _sources_row(row):
    return row["member"] == ORCHESTRATION and row["resource"].endswith(
        "/intelligence_42_sources_staging"
    )


def _udf_row(member):
    return lambda row: row["member"] == member and row["resource"].endswith("/" + UDF)


def _project_row(member):
    return lambda row: row["member"] == member and row["role"] == BUILDS_VIEWER


def _job_row(row):
    return row["resource"].endswith("/" + DAILY_JOB) and row["role"].endswith("WithOverrides")


@pytest.mark.parametrize(
    "member",
    [
        "user:someone.else@ogilvy.co.za",
        "group:operators@ogilvy.co.za",
        "domain:ogilvy.co.za",
        "allAuthenticatedUsers",
        f"serviceAccount:not-in-the-manifest{SA}",
        "serviceAccount:intelligence-42-brain@another-project.iam.gserviceaccount.com",
    ],
)
def test_a_member_outside_the_manifest_identities_and_the_operator_is_refused(
    cloud, capsys, tmp_path, monkeypatch, member
):
    def mutate(delta):
        _row(delta, _udf_row(BRAIN))["member"] = member

    code, error = _mutated(tmp_path, monkeypatch, capsys, mutate)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_member_refused"}
    assert cloud.reads == []


@pytest.mark.parametrize(
    "resource",
    [
        BQ + APPROVALS + "/routines/sp_consume_open_intelligence_execution_v2",
        f"//storage.googleapis.com/projects/_/buckets/{CACHE_BUCKET}",
        f"//cloudresourcemanager.googleapis.com/projects/{PROJECT}",
        f"//run.googleapis.com/projects/{PROJECT}/locations/us-central1/jobs/{DAILY_JOB}",
    ],
)
def test_the_operator_anywhere_but_the_eight_operator_routines_is_refused(
    cloud, capsys, tmp_path, monkeypatch, resource
):
    def mutate(delta):
        row = _row(delta, _project_row(BRAIN))
        row["member"] = ALBERT
        row["resource"] = resource
        if "/routines/" in resource:
            row["role"] = "roles/bigquery.dataViewer"
        if "run.googleapis" in resource:
            row["role"] = "roles/run.viewer"
        if "storage" in resource:
            row["role"] = "roles/storage.objectViewer"
            row["condition"] = (
                "resource.name.startsWith('projects/_/buckets/listening-post-staging-cache/objects/x/')"
            )

    code, error = _mutated(tmp_path, monkeypatch, capsys, mutate)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_member_refused"}


@pytest.mark.parametrize(
    ("predicate", "role"),
    [
        (_project_row(BRAIN), "roles/owner"),
        (_project_row(BRAIN), "roles/cloudbuild.builds.editor"),
        (_project_row(BRAIN), "roles/bigquery.dataViewer"),
        (_udf_row(BRAIN), "roles/bigquery.dataEditor"),
        (_udf_row(BRAIN), "roles/bigquery.admin"),
        (_sources_row, "roles/owner"),
        (_sources_row, "READER"),
        (_sources_row, "roles/bigquery.admin"),
        (_job_row, "roles/run.admin"),
        (_job_row, "roles/run.developer"),
        (_job_row, "roles/iam.serviceAccountUser"),
        (
            lambda row: (
                row["resource"].endswith(CACHE_BUCKET)
                and row["condition"].endswith("release-index.json'")
            ),
            "roles/storage.admin",
        ),
        (
            lambda row: (
                row["resource"].endswith(CACHE_BUCKET)
                and row["condition"].endswith("release-index.json'")
            ),
            "roles/owner",
        ),
    ],
)
def test_a_role_outside_what_its_kind_admits_is_refused(
    cloud, capsys, tmp_path, monkeypatch, predicate, role
):
    def mutate(delta):
        _row(delta, predicate)["role"] = role

    code, error = _mutated(tmp_path, monkeypatch, capsys, mutate)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_role_refused"}


def _manifest_with(tmp_path, monkeypatch, *names: str) -> str:
    """Stand in for a manifest naming more resources, and return the digest of its bytes."""

    manifest = _load(MANIFEST)
    manifest["resources"].extend({"name": name, "actions": ["read"]} for name in names)
    raw = json.dumps(manifest).encode("utf-8")
    path = tmp_path / "resource_manifest_v1.json"
    path.write_bytes(raw)
    monkeypatch.setattr(migration, "RESOURCE_MANIFEST_PATH", path)
    return hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    ("resource", "role"),
    [
        (
            f"//bigquery.googleapis.com/projects/{PROJECT}/datasets/intelligence_42_sources",
            "roles/bigquery.dataViewer",
        ),
        (
            f"//bigquery.googleapis.com/projects/{PROJECT}/datasets/trends_v2_prod_approvals",
            "roles/bigquery.dataViewer",
        ),
        (
            f"//bigquery.googleapis.com/projects/{PROJECT}/datasets/intelligence_42_sources/tables/raw",
            "roles/bigquery.dataViewer",
        ),
        (
            f"//run.googleapis.com/projects/{PROJECT}/locations/us-central1/jobs/intelligence-42-ingest",
            "roles/run.viewer",
        ),
        (
            f"//run.googleapis.com/projects/{PROJECT}/locations/us-central1/jobs/intelligence-42-ingest-production-staging",
            "roles/run.viewer",
        ),
    ],
    ids=["dataset", "prod_dataset", "table", "job", "prod_job"],
)
def test_a_production_resource_is_refused_even_when_the_manifest_names_it(
    cloud, capsys, tmp_path, monkeypatch, resource, role
):
    digest = _manifest_with(tmp_path, monkeypatch, resource)

    def mutate(delta):
        delta["resource_manifest_sha256"] = digest
        row = _row(delta, _sources_row)
        row["resource"] = resource
        row["role"] = role

    code, error = _mutated(tmp_path, monkeypatch, capsys, mutate)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_resource_refused"}


@pytest.mark.parametrize(
    "resource",
    [
        f"//bigquery.googleapis.com/projects/{PROJECT}/datasets/trends_v2_production",
        f"//bigquery.googleapis.com/projects/{PROJECT}/datasets/intelligence_42_sources_staging_copy",
        "//bigquery.googleapis.com/projects/another-project/datasets/intelligence_42_sources_staging",
        f"//bigquery.googleapis.com/projects/{PROJECT}/datasets/intelligence_42_sources_staging/tables/raw",
        f"//bigquery.googleapis.com/projects/{PROJECT}/datasets/Intelligence_42_sources_staging",
        f"//run.googleapis.com/projects/{PROJECT}/locations/us-central1/jobs/intelligence-42-unlisted-staging",
        f"//run.googleapis.com/projects/{PROJECT}/locations/europe-west1/jobs/{DAILY_JOB}",
        f"//run.googleapis.com/projects/{PROJECT}/locations/us-central1/services/listening-post-staging",
    ],
)
def test_a_resource_outside_the_manifest_or_the_grammars_is_refused(
    cloud, capsys, tmp_path, monkeypatch, resource
):
    def mutate(delta):
        _row(delta, _sources_row)["resource"] = resource

    code, error = _mutated(tmp_path, monkeypatch, capsys, mutate)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_resource_refused"}


def test_a_routine_the_manifest_does_not_name_is_refused(cloud, capsys, tmp_path, monkeypatch):
    def mutate(delta):
        _row(delta, _udf_row(BRAIN))["resource"] = BQ + APPROVALS + "/routines/sp_unlisted_v9"

    code, error = _mutated(tmp_path, monkeypatch, capsys, mutate)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_resource_refused"}


def _cache_row(row):
    return row["resource"].endswith(CACHE_BUCKET) and row["condition"].endswith(
        "release-index.json'"
    )


@pytest.mark.parametrize(
    ("predicate", "condition"),
    [
        (_cache_row, None),
        (_cache_row, ""),
        (
            _cache_row,
            " resource.name.startsWith('projects/_/buckets/listening-post-staging-cache/objects/x/')",
        ),
        (_cache_row, "resource.name.startsWith('projects/_/buckets/another-bucket/objects/x/')"),
        (
            _cache_row,
            "resource.name.startsWith('projects/_/buckets/listening-post-staging-cache/objects/x/')"
            " || resource.name.startsWith('projects/_/buckets/listening-post-staging-cacheX/objects/')",
        ),
        (_cache_row, "true"),
        (_udf_row(BRAIN), "request.time < timestamp('2030-01-01T00:00:00Z')"),
        (_project_row(BRAIN), "request.time < timestamp('2030-01-01T00:00:00Z')"),
        (_sources_row, "request.time < timestamp('2030-01-01T00:00:00Z')"),
        (_job_row, "request.time < timestamp('2030-01-01T00:00:00Z')"),
    ],
)
def test_a_condition_the_row_kind_does_not_admit_is_refused(
    cloud, capsys, tmp_path, monkeypatch, predicate, condition
):
    def mutate(delta):
        _row(delta, predicate)["condition"] = condition

    code, error = _mutated(tmp_path, monkeypatch, capsys, mutate)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_condition_refused"}


def test_a_delta_that_drops_an_amendment_d_binding_is_refused(cloud, capsys, tmp_path, monkeypatch):
    def mutate(delta):
        base = {_key(row) for row in _load(BASE)["bindings"]}
        index = next(i for i, row in enumerate(delta["bindings"]) if _key(row) in base)
        del delta["bindings"][index]

    code, error = _mutated(tmp_path, monkeypatch, capsys, mutate)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_delta_invalid"}


def test_a_delta_that_drops_an_amendment_d_authorization_without_restating_it_is_refused(
    cloud, capsys, tmp_path, monkeypatch
):
    def mutate(delta):
        delta["routine_authorizations"] = [
            row for row in delta["routine_authorizations"] if not row["routine"].endswith("/" + UDF)
        ]

    code, error = _mutated(tmp_path, monkeypatch, capsys, mutate)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_delta_invalid"}


def _udf_authorization(delta: dict) -> dict:
    (row,) = [row for row in delta["routine_authorizations"] if row["routine"].endswith("/" + UDF)]
    return row


@pytest.mark.parametrize(
    "mutate",
    [
        lambda delta: delta["approval"].update(applied=True),
        lambda delta: delta["approval"].update(state="rejected"),
        lambda delta: delta["approval"].update(state="approved"),
        lambda delta: delta.update(project="another-project"),
        lambda delta: delta.update(contract_version="42_iam_delta_v2"),
        lambda delta: delta.update(bindings={}),
        lambda delta: delta["bindings"].append(deepcopy(delta["bindings"][-1])),
        lambda delta: delta["bindings"].append(
            {**deepcopy(delta["bindings"][-1]), "purpose": "the same grant, restated"}
        ),
        lambda delta: delta["bindings"][-1].update(extra=1),
        lambda delta: _udf_authorization(delta).update(routine_type="PROCEDURE"),
        lambda delta: _udf_authorization(delta).pop("routine_type"),
        lambda delta: delta["routine_authorizations"][-1].update(routine_type="SCALAR_FUNCTION"),
        lambda delta: delta["routine_authorizations"][0].update(role="roles/bigquery.admin"),
        lambda delta: delta["routine_authorizations"][0].update(
            dataset="//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/trends_v2_staging"
        ),
        lambda delta: delta.update(custom_roles={}),
    ],
    ids=[
        "applied",
        "rejected",
        "approved_without_record",
        "project",
        "contract",
        "bindings_not_a_list",
        "duplicate",
        "duplicate_grant",
        "extra_key",
        "function_type_other",
        "roleless_without_type",
        "type_beside_a_role",
        "authorization_role",
        "authorization_dataset",
        "custom_roles_not_a_list",
    ],
)
def test_a_malformed_delta_is_refused(cloud, capsys, tmp_path, monkeypatch, mutate):
    code, error = _mutated(tmp_path, monkeypatch, capsys, mutate)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_delta_invalid"}


def test_a_row_on_a_must_not_grant_routine_is_refused(cloud, capsys, tmp_path, monkeypatch):
    def mutate(delta):
        _row(delta, _udf_row(BRAIN))["resource"] = delta["must_not_grant"][0]["resource"]

    code, error = _mutated(tmp_path, monkeypatch, capsys, mutate)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_delta_invalid"}


def test_a_kind_the_words_do_not_serve_is_refused(cloud, capsys, tmp_path, monkeypatch):
    def mutate(delta):
        row = _row(delta, _project_row(BRAIN))
        row["resource"] = (
            f"//secretmanager.googleapis.com/projects/{PROJECT}/secrets/SOCIALCRAWL_OGILVY_API_KEY"
        )
        row["role"] = "roles/secretmanager.secretAccessor"

    code, error = _mutated(tmp_path, monkeypatch, capsys, mutate)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_resource_refused"}


# Refusals of the live state


def test_a_function_row_on_a_routine_that_is_a_procedure_live_is_refused(cloud, capsys):
    cloud.install_all_routines()
    cloud.routine_types[UDF] = "PROCEDURE"
    code, error = _run(capsys, "iam-e-plan")
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_routine_type_mismatch"}


def test_a_role_row_on_a_routine_that_is_a_function_live_is_refused(cloud, capsys):
    cloud.install_all_routines()
    cloud.routine_types[RECONCILE] = "SCALAR_FUNCTION"
    code, error = _run(capsys, "iam-e-plan")
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_routine_type_mismatch"}


def test_a_function_entry_holding_a_role_live_is_refused(cloud, capsys):
    cloud.install_all_routines()
    for entry in cloud.datasets[APPROVALS_URL]["access"]:
        if entry.get("routine", {}).get("routineId") == UDF:
            entry["role"] = "roles/bigquery.routineDataViewer"
    code, error = _run(capsys, "iam-e-plan")
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_authorization_role_mismatch"}


# The send path guard: only delta rows, only additions, the project untouched otherwise


def test_the_send_path_refuses_a_write_that_adds_a_member_outside_the_rows(
    cloud, capsys, tmp_path, monkeypatch
):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    original = migration._iam_e_revised_policy

    def widening(policy, additions):
        revised = original(policy, additions)
        revised["bindings"].append({"role": "roles/storage.admin", "members": [PRICE_POLICY]})
        return revised

    monkeypatch.setattr(migration, "_iam_e_revised_policy", widening)
    for words in (["iam-e-dry-run"], ["iam-e-apply", digest]):
        code, error = _run(capsys, *words)
        assert code == 1
        assert error == {"error": "execution_approval_iam_e_write_refused"}
    assert cloud.writes == []


def test_the_send_path_refuses_a_policy_carrying_another_etag_or_losing_a_binding(
    cloud, capsys, tmp_path, monkeypatch
):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    original = migration._iam_e_revised_policy
    for change in (
        lambda policy: policy.update(etag="*"),
        lambda policy: policy["bindings"].pop(0),
    ):

        def altering(policy, additions, change=change):
            revised = original(policy, additions)
            if any(row.kind == "runtime_job" for row in additions):
                change(revised)
            return revised

        monkeypatch.setattr(migration, "_iam_e_revised_policy", altering)
        code, error = _run(capsys, "iam-e-apply", digest)
        assert code == 1
        assert error == {"error": "execution_approval_iam_e_write_refused"}
    assert cloud.writes == []


@pytest.mark.parametrize(
    "change",
    [
        lambda policy: policy["bindings"][0]["members"].remove(ALBERT),
        lambda policy: policy["bindings"][2].pop("condition"),
        lambda policy: policy["bindings"][2]["condition"].update(expression="true"),
        lambda policy: policy.pop("auditConfigs"),
        lambda policy: policy.update(version=1),
        lambda policy: policy["bindings"][1].update(role="roles/cloudbuild.builds.editor"),
        # The grant survives in a new binding, but the binding it was read in changed.
        lambda policy: (
            policy["bindings"][0]["members"].remove(ALBERT),
            policy["bindings"].append({"role": "roles/owner", "members": [ALBERT]}),
        ),
        # Same expression, so the same grant, but the kept binding itself was rewritten.
        lambda policy: policy["bindings"][2]["condition"].update(title="renamed"),
        lambda policy: policy.update(version="3"),
    ],
    ids=[
        "member_removed",
        "condition_dropped",
        "condition_changed",
        "audit_dropped",
        "version",
        "role",
        "member_moved",
        "condition_title",
        "version_type",
    ],
)
def test_the_project_write_is_refused_if_any_existing_binding_would_change(
    cloud, capsys, tmp_path, monkeypatch, change
):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    original = migration._iam_e_revised_policy

    def altering(policy, additions):
        revised = original(policy, additions)
        if any(row.kind == "project" for row in additions):
            change(revised)
        return revised

    monkeypatch.setattr(migration, "_iam_e_revised_policy", altering)
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_project_binding_changed"}
    assert cloud.writes == []


def test_the_send_path_refuses_a_dataset_write_that_changes_an_existing_entry(
    cloud, capsys, tmp_path, monkeypatch
):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    original = migration._iam_e_revised_access

    def rewriting(access, entries):
        revised = original(access, entries)
        revised[1] = {"role": "READER", "userByEmail": f"intelligence-42-ingest{SA}"}
        return revised

    monkeypatch.setattr(migration, "_iam_e_revised_access", rewriting)
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_write_refused"}
    assert cloud.writes == []


# Every row kind, read from the delta: custom roles, tables, jobs, and a project row that
# names a custom role, in the shape a later round may take for the serving identity


def _binding(member: str, resource: str, role: str) -> dict:
    return {
        "condition": None,
        "member": member,
        "purpose": "test row",
        "resource": resource,
        "role": role,
    }


def _jobs_get_role(permissions=("bigquery.jobs.get",)) -> dict:
    return {
        "role": JOBS_GET,
        "title": "42 serving jobs get",
        "description": "Reads the state of a BigQuery job the serving identity started.",
        "permissions": list(permissions),
    }


def _serving_rows() -> list[dict]:
    # A later round's shape: a role the delta defines, and a grant on another job.
    return [
        _binding(LISTENING, f"//cloudresourcemanager.googleapis.com/projects/{PROJECT}", JOBS_GET),
        _binding(LISTENING, INGEST_RESOURCE, "roles/run.viewer"),
    ]


def _serving_delta(tmp_path, monkeypatch, *, approve=True, role=None, rows=None) -> str:
    delta = _load(FIXTURE)
    delta["custom_roles"] = [_jobs_get_role() if role is None else role]
    delta["bindings"].extend(_serving_rows() if rows is None else rows)
    if approve:
        _approve(delta)
    return _write_delta(tmp_path, monkeypatch, delta)


def _serving_cloud(cloud) -> None:
    """Routines and the ingest job installed, and the round 6 role already live."""

    cloud.install_all_routines()
    cloud.install_ingest_job()
    cloud.add_role(CAPTURE_JOB_READ_ID, ["bigquery.jobs.get"])


def _kinds(receipt: dict, kind: str) -> list[dict]:
    return [row for row in receipt["rows"] if row["kind"] == kind]


def test_the_serving_rows_are_read_from_the_delta_as_their_own_kinds(
    cloud, capsys, tmp_path, monkeypatch
):
    _serving_delta(tmp_path, monkeypatch, approve=False)
    _serving_cloud(cloud)
    targets = migration.iam_e_targets()
    assert [item.role for item in targets.custom_roles] == [JOBS_GET, CAPTURE_JOB_READ]
    assert targets.custom_roles[0] == migration.IamECustomRole(
        JOBS_GET,
        JOBS_GET_ID,
        "42 serving jobs get",
        "Reads the state of a BigQuery job the serving identity started.",
        ("bigquery.jobs.get",),
    )
    serving = {
        (row.kind, row.ident, row.role) for row in targets.bindings if row.member == LISTENING
    }
    assert serving == {
        ("project", PROJECT, CAPTURE_JOB_READ),
        ("project", PROJECT, JOBS_GET),
        *(("table", key, "roles/bigquery.dataViewer") for key in TABLE_KEYS),
        ("runtime_job", INGEST_JOB, "roles/run.viewer"),
        ("runtime_job", FUNDED_JOB, "roles/run.viewer"),
    }
    code, plan = _run(capsys, "iam-e-plan")
    assert code == 0
    assert _kinds(plan, "custom_role") == [
        {
            "kind": "custom_role",
            "role": JOBS_GET,
            "permissions": ["bigquery.jobs.get"],
            "live_permissions": None,
            "state": "missing",
        },
        {
            "kind": "custom_role",
            "role": CAPTURE_JOB_READ,
            "permissions": ["bigquery.jobs.get"],
            "live_permissions": ["bigquery.jobs.get"],
            "state": "present",
        },
    ]
    assert plan["counts"]["custom_role"] == _counts(2, present=1, missing=1)
    assert plan["counts"]["table"] == _counts(3, missing=3)
    assert plan["counts"]["runtime_job"] == _counts(3, missing=3)
    assert plan["counts"]["project"] == _counts(6, missing=6)
    for key in TABLE_KEYS:
        assert ("post", f"{APPROVALS_URL.rsplit('/', 1)[0]}/{key}:getIamPolicy") in cloud.reads
    assert ("get", f"{ROLES_URL}/{JOBS_GET_ID}") in cloud.reads


def test_apply_creates_the_role_then_binds_tables_job_and_project_and_a_rerun_adds_nothing(
    cloud, capsys, tmp_path, monkeypatch
):
    digest = _serving_delta(tmp_path, monkeypatch)
    _serving_cloud(cloud)
    table_etags = {key: cloud.tables[key]["etag"] for key in TABLE_KEYS}
    code, dry = _run(capsys, "iam-e-dry-run")
    assert code == 0
    assert dry["writes_planned"] == _planned(
        custom_role_writes=1,
        dataset_access_patches=2,
        routine_policy_sets=9,
        table_policy_sets=3,
        bucket_policy_sets=2,
        run_job_policy_sets=3,
        project_policy_sets=1,
    )
    views = {(item["kind"], item["resource"]): item["policy"] for item in dry["policies_to_write"]}
    assert views[("custom_role", JOBS_GET_ID)] == {
        "roleId": JOBS_GET_ID,
        "role": {
            "title": "42 serving jobs get",
            "description": "Reads the state of a BigQuery job the serving identity started.",
            "includedPermissions": ["bigquery.jobs.get"],
            "stage": "GA",
        },
    }
    code, receipt = _run(capsys, "iam-e-apply", digest)
    assert code == 0, receipt
    # The role exists before anything names it, and the project policy is the last write.
    assert cloud.writes[0] == ("post", ROLES_URL, views[("custom_role", JOBS_GET_ID)])
    assert cloud.writes[-1][1] == PROJECT_URL + ":setIamPolicy"
    assert cloud.roles[JOBS_GET_ID]["includedPermissions"] == ["bigquery.jobs.get"]
    for key in TABLE_KEYS:
        sent = [body for verb, url, body in cloud.writes if url.endswith(f"/{key}:setIamPolicy")]
        assert sent == [
            {
                "policy": {
                    "etag": table_etags[key],
                    "version": 1,
                    "bindings": [{"role": "roles/bigquery.dataViewer", "members": [LISTENING]}],
                }
            }
        ]
    grants = cloud.grants()
    assert ("project", JOBS_GET, LISTENING, "") in grants
    assert (f"job:{INGEST_JOB}", "roles/run.viewer", LISTENING, "") in grants
    assert (f"job:{INGEST_JOB}", "roles/run.developer", DEPLOY, "") in grants
    assert receipt["counts"]["custom_role"] == {
        "target": 2,
        "present_before": 1,
        "added": 1,
        "present_after": 2,
    }
    assert receipt["counts"]["table"] == {
        "target": 3,
        "present_before": 0,
        "added": 3,
        "present_after": 3,
    }
    cloud.writes.clear()
    code, again = _run(capsys, "iam-e-apply", digest)
    assert code == 0
    assert cloud.writes == []
    assert again["writes_planned"] == _planned()


def test_a_live_role_whose_permissions_differ_is_updated_to_exactly_the_set_under_its_etag(
    cloud, capsys, tmp_path, monkeypatch
):
    digest = _serving_delta(tmp_path, monkeypatch)
    _serving_cloud(cloud)
    cloud.add_role(JOBS_GET_ID, ["bigquery.jobs.list", "bigquery.jobs.get"])
    etag = cloud.roles[JOBS_GET_ID]["etag"]
    code, plan = _run(capsys, "iam-e-plan")
    assert code == 0
    (row,) = [row for row in _kinds(plan, "custom_role") if row["role"] == JOBS_GET]
    assert row["state"] == "differs"
    assert row["live_permissions"] == ["bigquery.jobs.get", "bigquery.jobs.list"]
    code, receipt = _run(capsys, "iam-e-apply", digest)
    assert code == 0, receipt
    patches = [body for verb, url, body in cloud.writes if url == f"{ROLES_URL}/{JOBS_GET_ID}"]
    assert patches == [
        {
            "params": {"updateMask": "includedPermissions"},
            "json": {"includedPermissions": ["bigquery.jobs.get"], "etag": etag},
            "headers": None,
        }
    ]
    assert not any(url == ROLES_URL for _verb, url, _body in cloud.writes)
    assert cloud.roles[JOBS_GET_ID]["includedPermissions"] == ["bigquery.jobs.get"]
    assert receipt["counts"]["custom_role"] == {
        "target": 2,
        "present_before": 1,
        "added": 1,
        "present_after": 2,
    }


def test_a_live_role_that_already_holds_exactly_the_set_is_left_alone(
    cloud, capsys, tmp_path, monkeypatch
):
    digest = _serving_delta(tmp_path, monkeypatch)
    _serving_cloud(cloud)
    cloud.add_role(JOBS_GET_ID, ["bigquery.jobs.get"])
    code, receipt = _run(capsys, "iam-e-apply", digest)
    assert code == 0, receipt
    assert not any(ROLES_URL in url for _verb, url, _body in cloud.writes)
    assert receipt["counts"]["custom_role"]["present_before"] == 2


def test_a_role_update_that_lands_short_fails_the_readback(cloud, capsys, tmp_path, monkeypatch):
    digest = _serving_delta(tmp_path, monkeypatch)
    _serving_cloud(cloud)
    cloud.add_role(JOBS_GET_ID, ["bigquery.jobs.get", "bigquery.jobs.list"])
    cloud.drop_writes.add(f"{ROLES_URL}/{JOBS_GET_ID}")
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_readback_mismatch"
    assert {"kind": "custom_role", "resource": JOBS_GET_ID} in error["applied"]["writes"]


def test_a_role_changed_after_the_read_is_refused_by_its_etag(cloud, capsys, tmp_path, monkeypatch):
    digest = _serving_delta(tmp_path, monkeypatch)
    _serving_cloud(cloud)
    cloud.add_role(JOBS_GET_ID, ["bigquery.jobs.get", "bigquery.jobs.list"])
    cloud.bump_etag_before_write.add(f"{ROLES_URL}/{JOBS_GET_ID}")
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_write_refused"
    assert error["applied"] == {
        "writes": [],
        "failed_at": {"kind": "custom_role", "resource": JOBS_GET_ID},
    }
    assert cloud.roles[JOBS_GET_ID]["includedPermissions"] == [
        "bigquery.jobs.get",
        "bigquery.jobs.list",
    ]


@pytest.mark.parametrize(
    "fields",
    [{"deleted": True}, {"stage": "DISABLED"}],
    ids=["deleted", "disabled"],
)
def test_a_deleted_or_disabled_live_role_is_never_brought_back(
    cloud, capsys, tmp_path, monkeypatch, fields
):
    _serving_delta(tmp_path, monkeypatch)
    _serving_cloud(cloud)
    cloud.add_role(JOBS_GET_ID, ["bigquery.jobs.get"], **fields)
    code, error = _run(capsys, "iam-e-plan")
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_custom_role_unusable"}


def test_a_live_role_under_another_name_fails_closed(cloud, capsys, tmp_path, monkeypatch):
    _serving_delta(tmp_path, monkeypatch)
    _serving_cloud(cloud)
    cloud.add_role(JOBS_GET_ID, ["bigquery.jobs.get"], name=f"projects/{PROJECT}/roles/other")
    code, error = _run(capsys, "iam-e-plan")
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_readback_mismatch"}


@pytest.mark.parametrize(
    "permission",
    [
        "iam.roles.create",
        "iam.serviceAccounts.actAs",
        "resourcemanager.projects.setIamPolicy",
        "bigquery.datasets.setIamPolicy",
        "run.jobs.setIamPolicy",
        "orgpolicy.policy.set",
        "serviceusage.services.enable",
    ],
)
def test_a_custom_role_carrying_an_escalating_permission_is_refused(
    cloud, capsys, tmp_path, monkeypatch, permission
):
    _serving_delta(
        tmp_path,
        monkeypatch,
        approve=False,
        role=_jobs_get_role(sorted(["bigquery.jobs.get", permission])),
    )
    code, error = _run(capsys, "iam-e-plan")
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_role_refused"}
    assert cloud.reads == []


@pytest.mark.parametrize(
    "role",
    [
        {**_jobs_get_role(), "permissions": ["bigquery.jobs.list", "bigquery.jobs.get"]},
        {**_jobs_get_role(), "permissions": ["bigquery.jobs.get", "bigquery.jobs.get"]},
        {**_jobs_get_role(), "permissions": []},
        {**_jobs_get_role(), "permissions": "bigquery.jobs.get"},
        {**_jobs_get_role(), "permissions": ["bigquery jobs get"]},
        {**_jobs_get_role(), "permissions": [7]},
        {**_jobs_get_role(), "role": "projects/another-project/roles/intelligence42JobsGet"},
        {**_jobs_get_role(), "role": "roles/bigquery.jobUser"},
        {**_jobs_get_role(), "role": f"projects/{PROJECT}/roles/x"},
        {**_jobs_get_role(), "title": ""},
        {**_jobs_get_role(), "title": 7},
        {**_jobs_get_role(), "description": "x" * 257},
        {**_jobs_get_role(), "stage": "GA"},
        {key: value for key, value in _jobs_get_role().items() if key != "description"},
        "projects/ogilvy-trends-v2/roles/intelligence42JobsGet",
    ],
    ids=[
        "unsorted",
        "duplicate",
        "empty",
        "not_a_list",
        "malformed_permission",
        "permission_not_a_string",
        "other_project",
        "predefined",
        "short_id",
        "empty_title",
        "title_not_a_string",
        "long_description",
        "extra_key",
        "missing_key",
        "not_an_object",
    ],
)
def test_a_malformed_custom_role_is_refused(cloud, capsys, tmp_path, monkeypatch, role):
    _serving_delta(tmp_path, monkeypatch, approve=False, role=role, rows=[])
    code, error = _run(capsys, "iam-e-plan")
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_delta_invalid"}


def test_a_custom_role_defined_twice_is_refused(cloud, capsys, tmp_path, monkeypatch):
    digest = _manifest_with(tmp_path, monkeypatch)
    delta = _load(FIXTURE)
    delta["resource_manifest_sha256"] = digest
    delta["custom_roles"] = [_jobs_get_role(), _jobs_get_role(["bigquery.jobs.list"])]
    _write_delta(tmp_path, monkeypatch, delta)
    code, error = _run(capsys, "iam-e-plan")
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_delta_invalid"}


@pytest.mark.parametrize(
    "rows",
    [
        # A custom role the delta does not define, on the project.
        [
            _binding(
                LISTENING,
                f"//cloudresourcemanager.googleapis.com/projects/{PROJECT}",
                f"projects/{PROJECT}/roles/intelligence42Undefined",
            )
        ],
        # A defined custom role on a routine, where only the read role is admitted.
        [_binding(BRAIN, BQ + APPROVALS + "/routines/" + UDF, JOBS_GET)],
        # A predefined project role beside the builds viewer.
        [
            _binding(
                LISTENING,
                f"//cloudresourcemanager.googleapis.com/projects/{PROJECT}",
                "roles/bigquery.jobUser",
            )
        ],
        # A table role beyond read and edit.
        [_binding(LISTENING, TABLE_RESOURCES[0], "roles/bigquery.dataOwner")],
    ],
    ids=["undefined_custom", "custom_on_routine", "project_predefined", "table_owner"],
)
def test_a_role_the_delta_does_not_define_or_the_kind_does_not_admit_is_refused(
    cloud, capsys, tmp_path, monkeypatch, rows
):
    _serving_delta(tmp_path, monkeypatch, approve=False, rows=rows)
    code, error = _run(capsys, "iam-e-plan")
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_role_refused"}


def test_a_condition_on_a_table_row_is_refused(cloud, capsys, tmp_path, monkeypatch):
    row = _binding(LISTENING, TABLE_RESOURCES[0], "roles/bigquery.dataViewer")
    row["condition"] = "request.time < timestamp('2030-01-01T00:00:00Z')"
    _serving_delta(tmp_path, monkeypatch, approve=False, rows=[row])
    code, error = _run(capsys, "iam-e-plan")
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_condition_refused"}


def test_a_missing_table_is_reported_and_refuses_the_apply(cloud, capsys, tmp_path, monkeypatch):
    digest = _serving_delta(tmp_path, monkeypatch)
    _serving_cloud(cloud)
    del cloud.tables[TABLE_KEYS[1]]
    code, plan = _run(capsys, "iam-e-plan")
    assert code == 0
    assert plan["resource_missing"] == [TABLE_RESOURCES[1]]
    assert plan["counts"]["table"] == _counts(3, missing=2, resource_missing=1)
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_resource_missing"
    assert error["resource_missing"] == [TABLE_RESOURCES[1]]
    assert cloud.writes == []


def test_the_send_path_refuses_a_role_body_beyond_the_defined_set(
    cloud, capsys, tmp_path, monkeypatch
):
    digest = _serving_delta(tmp_path, monkeypatch)
    _serving_cloud(cloud)
    original = migration._iam_e_role_body

    def widening(item, live):
        body = original(item, live)
        permissions = (
            body["role"]["includedPermissions"] if live is None else body["includedPermissions"]
        )
        permissions.append("bigquery.tables.getData")
        return body

    monkeypatch.setattr(migration, "_iam_e_role_body", widening)
    for words in (["iam-e-dry-run"], ["iam-e-apply", digest]):
        code, error = _run(capsys, *words)
        assert code == 1
        assert error == {"error": "execution_approval_iam_e_write_refused"}
    cloud.add_role(JOBS_GET_ID, ["bigquery.jobs.list"])
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_write_refused"}
    assert cloud.writes == []


@pytest.mark.parametrize(
    "change",
    [
        lambda body: body.update(etag="*"),
        lambda body: body.update(title="renamed"),
    ],
    ids=["etag", "extra_field"],
)
def test_the_send_path_refuses_a_role_update_off_its_read_etag_or_beyond_its_permissions(
    cloud, capsys, tmp_path, monkeypatch, change
):
    digest = _serving_delta(tmp_path, monkeypatch)
    _serving_cloud(cloud)
    cloud.add_role(JOBS_GET_ID, ["bigquery.jobs.list"])
    original = migration._iam_e_role_body

    def altering(item, live):
        body = original(item, live)
        change(body)
        return body

    monkeypatch.setattr(migration, "_iam_e_role_body", altering)
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_write_refused"}
    assert cloud.writes == []


SNAPSHOT_ROLE = f"projects/{PROJECT}/roles/SourceSnapshotCreate"
SNAPSHOT_PERMISSIONS = [
    "bigquery.datasets.get",
    "bigquery.tables.create",
    "bigquery.tables.createSnapshot",
    "bigquery.tables.get",
    "bigquery.tables.getData",
]


def test_a_changed_sources_row_is_followed_as_the_delta_states_it(
    cloud, capsys, tmp_path, monkeypatch
):
    # B8 could have bound the custom snapshot role instead: the words take its
    # permission list from the validator's pin and create it before the dataset entry.
    def mutate(delta):
        _row(delta, _sources_row)["role"] = SNAPSHOT_ROLE

    digest = _approved(tmp_path, monkeypatch, mutate)
    cloud.install_all_routines()
    code, receipt = _run(capsys, "iam-e-apply", digest)
    assert code == 0, receipt
    assert cloud.datasets[SOURCES_URL]["access"][-1] == {
        "role": SNAPSHOT_ROLE,
        "userByEmail": f"intelligence-42-orchestration{SA}",
    }
    assert cloud.roles["SourceSnapshotCreate"]["includedPermissions"] == SNAPSHOT_PERMISSIONS
    kinds = [item["kind"] for item in receipt["applied"]["writes"]]
    assert kinds.index("custom_role") < kinds.index("dataset")
    assert receipt["counts"]["dataset"] == {
        "target": 1,
        "present_before": 0,
        "added": 1,
        "present_after": 1,
    }


@pytest.mark.parametrize(
    "permissions",
    [SNAPSHOT_PERMISSIONS[:-1], sorted([*SNAPSHOT_PERMISSIONS, "bigquery.tables.updateData"])],
    ids=["narrower", "wider"],
)
def test_a_delta_definition_that_departs_from_the_validator_pin_is_refused(
    cloud, capsys, tmp_path, monkeypatch, permissions
):
    def mutate(delta):
        delta["custom_roles"] = [
            {
                "role": SNAPSHOT_ROLE,
                "title": "42 source snapshot create",
                "description": "Snapshots the collection tables into the bridge dataset.",
                "permissions": permissions,
            }
        ]
        _row(delta, _sources_row)["role"] = SNAPSHOT_ROLE

    code, error = _mutated(tmp_path, monkeypatch, capsys, mutate)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_delta_invalid"}


def test_a_delta_definition_equal_to_the_pin_is_used_as_the_delta_states_it(
    cloud, capsys, tmp_path, monkeypatch
):
    def mutate(delta):
        delta["custom_roles"] = [
            {
                "role": SNAPSHOT_ROLE,
                "title": "42 source snapshot create",
                "description": "Snapshots the collection tables into the bridge dataset.",
                "permissions": SNAPSHOT_PERMISSIONS,
            }
        ]
        _row(delta, _sources_row)["role"] = SNAPSHOT_ROLE

    delta = _load(FIXTURE)
    mutate(delta)
    _write_delta(tmp_path, monkeypatch, delta)
    roles = {item.role: item for item in migration.iam_e_targets().custom_roles}
    assert roles[SNAPSHOT_ROLE].title == "42 source snapshot create"
    assert roles[SNAPSHOT_ROLE].permissions == tuple(SNAPSHOT_PERMISSIONS)


def test_a_custom_role_neither_defined_nor_pinned_is_refused_on_a_dataset(
    cloud, capsys, tmp_path, monkeypatch
):
    def mutate(delta):
        _row(delta, _sources_row)["role"] = f"projects/{PROJECT}/roles/SourceSnapshotOther"

    code, error = _mutated(tmp_path, monkeypatch, capsys, mutate)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_role_refused"}


def test_a_pinned_role_no_new_row_binds_is_not_created(cloud, capsys):
    # SourceSnapshotCreate is pinned but unbound, so the words leave it alone.
    targets = migration.iam_e_targets()
    assert SNAPSHOT_ROLE not in {item.role for item in targets.custom_roles}


@pytest.mark.parametrize(
    "change",
    [
        lambda body: body.update(roleId="intelligence42Other"),
        lambda body: body.update(extra=1),
        lambda body: body["role"].update(stage="BETA"),
        lambda body: body["role"].update(title="renamed"),
        lambda body: body["role"].update(etag="*"),
    ],
    ids=["role_id", "extra_key", "stage", "title", "role_key"],
)
def test_the_send_path_refuses_a_role_creation_beyond_its_definition(
    cloud, capsys, tmp_path, monkeypatch, change
):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    original = migration._iam_e_role_body

    def altering(item, live):
        body = original(item, live)
        change(body)
        return body

    monkeypatch.setattr(migration, "_iam_e_role_body", altering)
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error == {"error": "execution_approval_iam_e_write_refused"}
    assert cloud.writes == []


# A resource that does not exist is found by reading the resource itself, before its
# policy, whatever the policy read answers. The apply on 35dd1c2 met a Cloud Run job
# that did not exist: its policy read answered an empty policy, the plan and the dry run
# reported nothing missing, and the apply stopped at the job's write with code 5 after
# 18 writes had landed.

(FUNDED_ROW,) = [row for row in JOB_ROWS if row["resource"].endswith("/" + FUNDED_JOB)]
CACHE_RESOURCE = f"//storage.googleapis.com/projects/_/buckets/{CACHE_BUCKET}"
(SOURCES_RESOURCE,) = [row["resource"] for row in DATASET_ROWS]


def test_a_missing_job_whose_policy_read_answers_an_empty_policy_is_reported_before_any_write(
    cloud, capsys, tmp_path, monkeypatch
):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    del cloud.jobs[FUNDED_JOB]
    # The answers the apply met: an empty policy from the policy read, code 5 from the job.
    policy_read = cloud.get(
        RUN_URL + FUNDED_JOB + ":getIamPolicy", params={"options.requestedPolicyVersion": 3}
    )
    assert policy_read.status_code == 200
    assert policy_read.json().get("bindings", []) == []
    assert cloud.get(RUN_URL + FUNDED_JOB).status_code == 404
    cloud.reads.clear()
    receipts = {}
    for word in ("iam-e-plan", "iam-e-dry-run"):
        code, receipt = _run(capsys, word)
        assert code == 0
        assert receipt["resource_missing"] == [FUNDED_ROW["resource"]]
        assert receipt["routine_missing"] == []
        assert receipt["apply_ready"] is False
        assert receipt["counts"]["runtime_job"] == _counts(2, missing=1, resource_missing=1)
        receipts[word] = receipt
    assert _states(receipts["iam-e-plan"])[
        ("runtime_job", FUNDED_ROW["member"], FUNDED_ROW["resource"], FUNDED_ROW["role"])
    ] == ("resource_missing")
    # The dry run plans no write to the job that does not exist.
    assert receipt["writes_planned"]["run_job_policy_sets"] == 1
    assert ("runtime_job", FUNDED_JOB) not in {
        (item["kind"], item["resource"]) for item in receipt["policies_to_write"]
    }
    # Each job is read before its policy, and a job that is not there has no policy read.
    daily = cloud.reads.index(("get", RUN_URL + DAILY_JOB))
    assert daily < cloud.reads.index(("get", RUN_URL + DAILY_JOB + ":getIamPolicy"))
    assert ("get", RUN_URL + FUNDED_JOB) in cloud.reads
    assert ("get", RUN_URL + FUNDED_JOB + ":getIamPolicy") not in cloud.reads
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error == {
        "error": "execution_approval_iam_e_resource_missing",
        "routine_missing": [],
        "resource_missing": [FUNDED_ROW["resource"]],
    }
    assert cloud.writes == []


def _drop_table(cloud) -> str:
    del cloud.tables[TABLE_KEYS[1]]
    return TABLE_RESOURCES[1]


def _drop_bucket(cloud) -> str:
    del cloud.buckets[CACHE_BUCKET]
    return CACHE_RESOURCE


def _drop_job(cloud) -> str:
    del cloud.jobs[FUNDED_JOB]
    return FUNDED_ROW["resource"]


def _drop_dataset(cloud) -> str:
    del cloud.datasets[SOURCES_URL]
    return SOURCES_RESOURCE


@pytest.mark.parametrize("answer", ["empty_policy", "not_found"])
@pytest.mark.parametrize(
    ("kind", "drop"),
    [
        ("table", _drop_table),
        ("bucket", _drop_bucket),
        ("runtime_job", _drop_job),
        ("dataset", _drop_dataset),
    ],
    ids=["table", "bucket", "runtime_job", "dataset"],
)
def test_each_missing_resource_is_reported_by_plan_and_dry_run_and_refuses_the_apply(
    cloud, capsys, tmp_path, monkeypatch, kind, drop, answer
):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    cloud.empty_policy_when_missing = (
        {"table", "bucket", "runtime_job"} if answer == "empty_policy" else set()
    )
    resource = drop(cloud)
    target = len([row for row in _added("bindings") if row["resource"] == resource])
    assert target
    for word in ("iam-e-plan", "iam-e-dry-run"):
        code, receipt = _run(capsys, word)
        assert code == 0, receipt
        assert receipt["resource_missing"] == [resource]
        assert receipt["apply_ready"] is False
        assert receipt["counts"][kind]["resource_missing"] == target
    # The dry run plans no write to the resource that does not exist.
    assert not any(
        resource.endswith("/" + item["resource"]) for item in receipt["policies_to_write"]
    )
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error == {
        "error": "execution_approval_iam_e_resource_missing",
        "routine_missing": [],
        "resource_missing": [resource],
    }
    assert cloud.writes == []


def test_a_rerun_from_the_observed_partial_state_adds_exactly_the_pending_rows(
    cloud, capsys, tmp_path, monkeypatch
):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    # The state the apply on 35dd1c2 left: 18 writes landed, the funded job write failed
    # with code 5, and the project write was never sent.
    cloud.refuse_writes.add(RUN_URL + FUNDED_JOB + ":setIamPolicy")
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error["applied"]["failed_at"] == {"kind": "runtime_job", "resource": FUNDED_JOB}
    assert len(error["applied"]["writes"]) == 18
    cloud.refuse_writes.clear()
    del cloud.jobs[FUNDED_JOB]
    partial = cloud.grants()
    pending = {(FUNDED_ROW["member"], FUNDED_ROW["resource"], FUNDED_ROW["role"])} | {
        (row["member"], row["resource"], row["role"]) for row in PROJECT_ROWS
    }
    assert len(pending) == 6
    code, plan = _run(capsys, "iam-e-plan")
    assert code == 0
    assert plan["resource_missing"] == [FUNDED_ROW["resource"]]
    bindings = [row for row in plan["rows"] if row["kind"] not in {"authorization", "custom_role"}]
    assert len(bindings) == 35
    for row in bindings:
        if (row["member"], row["resource"], row["role"]) in pending:
            expected = "resource_missing" if row["kind"] == "runtime_job" else "missing"
        else:
            expected = "present"
        assert row["state"] == expected, row
    assert {
        row["state"] for row in plan["rows"] if row["kind"] in {"authorization", "custom_role"}
    } == {"present"}
    # While the job is missing the apply refuses and sends nothing.
    cloud.writes.clear()
    code, error = _run(capsys, "iam-e-apply", digest)
    assert code == 1
    assert error["error"] == "execution_approval_iam_e_resource_missing"
    assert error["resource_missing"] == [FUNDED_ROW["resource"]]
    assert cloud.writes == []
    assert cloud.grants() == partial
    # The job is deployed, and the rerun adds exactly the six pending rows.
    cloud.jobs[FUNDED_JOB] = {
        "etag": "funded-deployed",
        "version": 1,
        "bindings": [{"role": "roles/run.developer", "members": [DEPLOY]}],
    }
    before = cloud.grants()
    code, receipt = _run(capsys, "iam-e-apply", digest)
    assert code == 0, receipt
    sent = [(verb, url) for verb, url, _body in cloud.writes]
    assert sent == [
        ("post", RUN_URL + FUNDED_JOB + ":setIamPolicy"),
        ("post", PROJECT_URL + ":setIamPolicy"),
    ]
    (funded_body,) = [body for _verb, url, body in cloud.writes if FUNDED_JOB in url]
    assert funded_body == {
        "policy": {
            "etag": "funded-deployed",
            "version": 1,
            "bindings": [
                {"role": "roles/run.developer", "members": [DEPLOY]},
                {"role": FUNDED_ROW["role"], "members": [FUNDED_ROW["member"]]},
            ],
        }
    }
    added = cloud.grants() - before
    assert before <= cloud.grants()
    assert added == {(f"job:{FUNDED_JOB}", FUNDED_ROW["role"], FUNDED_ROW["member"], "")} | {
        ("project", row["role"], row["member"], "") for row in PROJECT_ROWS
    }
    assert receipt["writes_planned"] == _planned(run_job_policy_sets=1, project_policy_sets=1)
    assert receipt["counts"]["runtime_job"] == {
        "target": 2,
        "present_before": 1,
        "added": 1,
        "present_after": 2,
    }
    assert receipt["counts"]["project"] == {
        "target": 5,
        "present_before": 0,
        "added": 5,
        "present_after": 5,
    }
    for kind, value in receipt["counts"].items():
        if kind not in {"runtime_job", "project"}:
            assert value["added"] == 0, kind
            assert value["present_before"] == value["target"], kind
    cloud.writes.clear()
    code, again = _run(capsys, "iam-e-apply", digest)
    assert code == 0
    assert again["writes_planned"] == _planned()
    assert cloud.writes == []


@pytest.mark.parametrize("status", [403, 500])
@pytest.mark.parametrize(
    "url",
    [
        RUN_URL + FUNDED_JOB,
        f"{APPROVALS_URL}/tables/{TABLES[1]}",
        STORAGE_URL + CACHE_BUCKET,
        SOURCES_URL,
    ],
    ids=["runtime_job", "table", "bucket", "dataset"],
)
def test_an_existence_read_that_answers_any_other_error_refuses_and_reports_nothing_missing(
    cloud, capsys, tmp_path, monkeypatch, url, status
):
    digest = _approved(tmp_path, monkeypatch)
    cloud.install_all_routines()
    # Only the read of the resource itself fails; its policy read would still answer.
    cloud.existence_errors[url] = status
    for words in (("iam-e-plan",), ("iam-e-dry-run",), ("iam-e-apply", digest)):
        code, error = _run(capsys, *words)
        assert code == 1, words
        assert error == {"error": "execution_approval_bootstrap_unapproved"}, words
    assert ("get", url) in cloud.reads
    assert cloud.writes == []
