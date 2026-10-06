"""Owner run words for the approved provisioning delta's non routine rows, proved
over a fake transport: which rows are in scope and which are refused, the exact
requests a dry run renders, add only etag guarded writes, a fresh readback that
fails closed, and a second apply that adds nothing.
"""

import copy
import functools
import hashlib
import http.client
import io
import json
import re
from pathlib import Path

import pytest

from ops.deploy import iam_delta_bindings as bindings

REPO_ROOT = Path(__file__).resolve().parents[2]
# Amendment d as approved; ops/deploy/iam_delta_v1.json now carries amendment e, so the
# approved amendment d bytes and manifest are retained under engine/configs.
DELTA_PATH = (
    REPO_ROOT / "engine" / "configs" / "open_intelligence" / "iam_delta_amendment_d.json"
)
DELTA_SHA256 = "1d2e8ead5a7c17c51b53ca640ed4c295294aef204ce4abeee9f03c7e822d5e3e"

PROJECT = "ogilvy-trends-v2"
SUFFIX = "@ogilvy-trends-v2.iam.gserviceaccount.com"
FUNDED = "serviceAccount:intelligence-42-funded" + SUFFIX
BRAIN = "serviceAccount:intelligence-42-brain" + SUFFIX
PRICE_POLICY = "serviceAccount:intelligence-42-price-policy" + SUFFIX
DEPLOY = "serviceAccount:intelligence-42-deploy" + SUFFIX
BUILD = "serviceAccount:intelligence-42-build" + SUFFIX
OWNER = "user:albert.meintjes@ogilvy.co.za"
APP = "serviceAccount:listening-post-staging" + SUFFIX

SECRET_CONDITION = (
    "resource.type == 'secretmanager.googleapis.com/SecretVersion' && "
    "resource.name == 'projects/590353929363/secrets/SOCIALCRAWL_OGILVY_API_KEY/versions/1'"
)

BQ = "https://bigquery.googleapis.com/bigquery/v2/projects/ogilvy-trends-v2/datasets/"
CRM = "https://cloudresourcemanager.googleapis.com/v1/projects/ogilvy-trends-v2"
RUN = "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/"
SECRETS = "https://secretmanager.googleapis.com/v1/projects/ogilvy-trends-v2/secrets/"
STORAGE = "https://storage.googleapis.com/storage/v1/b/"
IAM = "https://iam.googleapis.com/v1/projects/ogilvy-trends-v2/serviceAccounts/"
TASKS = "https://cloudtasks.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/"
REGISTRY = (
    "https://artifactregistry.googleapis.com/v1/projects/ogilvy-trends-v2/"
    "locations/us-central1/"
)

STAGING = BQ + "trends_v2_staging"
FUNDED_DATASET = BQ + "trends_v2_staging_funded"
FUNDED_JOB = RUN + "jobs/intelligence-42-funded-pilot-staging"
SECRET = SECRETS + "SOCIALCRAWL_OGILVY_API_KEY"
V3 = "options.requestedPolicyVersion=3"

FUNDED_ROWS = [
    {
        "condition": None,
        "member": FUNDED,
        "resource": "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/trends_v2_staging",
        "role": "roles/bigquery.dataEditor",
    },
    {
        "condition": None,
        "member": FUNDED,
        "resource": (
            "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/"
            "trends_v2_staging_funded"
        ),
        "role": "roles/bigquery.dataEditor",
    },
    {
        "condition": None,
        "member": FUNDED,
        "resource": "//cloudresourcemanager.googleapis.com/projects/ogilvy-trends-v2",
        "role": "roles/bigquery.jobUser",
    },
    {
        "condition": None,
        "member": FUNDED,
        "resource": (
            "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/"
            "jobs/intelligence-42-funded-pilot-staging"
        ),
        "role": "roles/run.viewer",
    },
    {
        "condition": SECRET_CONDITION,
        "member": FUNDED,
        "resource": (
            "//secretmanager.googleapis.com/projects/ogilvy-trends-v2/secrets/"
            "SOCIALCRAWL_OGILVY_API_KEY/versions/1"
        ),
        "role": "roles/secretmanager.secretAccessor",
    },
]


class FakeCloud:
    """IAM policies and dataset access lists keyed by the pinned request URLs."""

    def __init__(self):
        self.policies = {}
        self.datasets = {}
        self.requests = []
        self.drop_writes = False
        self.lose_on_write = None
        self.fail_write_at = None
        self.serial = 0

    def _etag(self):
        self.serial += 1
        return f"etag-{self.serial}"

    def seed_policy(self, base, bindings_=(), **extra):
        self.policies[base] = {
            "bindings": copy.deepcopy(list(bindings_)),
            "etag": self._etag(),
            "version": 1,
            **extra,
        }

    def seed_dataset(self, base, access=()):
        self.datasets[base] = {
            "access": copy.deepcopy(list(access)),
            "etag": self._etag(),
        }

    def __call__(self, request):
        body = None if request["body"] is None else json.loads(request["body"])
        self.requests.append(
            {
                "method": request["method"],
                "url": request["url"],
                "headers": dict(request["headers"]),
                "body": body,
            }
        )
        method, url = request["method"], request["url"]
        if url.startswith(BQ):
            base, _, query = url.partition("?")
            if query != "accessPolicyVersion=3":
                return _json(400, {"error": "query"})
            dataset = self.datasets.get(base)
            if dataset is None:
                return _json(404, {"error": "not found"})
            if method == "GET":
                return _json(200, copy.deepcopy(dataset))
            if method == "PATCH":
                if request["headers"].get("If-Match") != dataset["etag"]:
                    return _json(412, {"error": "precondition"})
                return self._write(dataset, "access", body["access"])
            return _json(405, {})
        if url.startswith(STORAGE):
            base, _, query = url.partition("/iam")
            policy = self.policies.get(base)
            if policy is None:
                return _json(404, {"error": "not found"})
            if method == "GET" and query == "?optionsRequestedPolicyVersion=3":
                return _json(200, copy.deepcopy(policy))
            if method == "PUT" and query == "":
                return self._set(base, body)
            return _json(400, {"error": "query"})
        if ":getIamPolicy" in url:
            base, _, query = url.partition(":getIamPolicy")
            policy = self.policies.get(base)
            if policy is None:
                return _json(404, {"error": "not found"})
            return _json(200, copy.deepcopy(policy))
        if url.endswith(":setIamPolicy") and method == "POST":
            base = url[: -len(":setIamPolicy")]
            if base not in self.policies:
                return _json(404, {"error": "not found"})
            return self._set(base, body["policy"])
        return _json(400, {"error": "unknown"})

    def _fail(self):
        if self.fail_write_at is None:
            return False
        self.fail_write_at -= 1
        return self.fail_write_at < 0

    def _set(self, base, policy):
        current = self.policies[base]
        if self._fail():
            return _json(500, {"error": "backend"})
        if policy.get("etag") != current["etag"]:
            return _json(409, {"error": "ABORTED etag"})
        echoed = copy.deepcopy(policy)
        if not self.drop_writes:
            stored = copy.deepcopy(policy)
            stored["etag"] = self._etag()
            if self.lose_on_write is not None:
                role, member = self.lose_on_write
                for item in stored["bindings"]:
                    if item["role"] == role and member in item["members"]:
                        item["members"].remove(member)
            self.policies[base] = stored
        return _json(200, echoed)

    def _write(self, dataset, key, value):
        if self._fail():
            return _json(500, {"error": "backend"})
        echoed = {**copy.deepcopy(dataset), key: copy.deepcopy(value)}
        if not self.drop_writes:
            dataset[key] = copy.deepcopy(value)
            dataset["etag"] = self._etag()
        return _json(200, echoed)

    def writes(self):
        return [
            item
            for item in self.requests
            if item["method"] in {"PATCH", "PUT"}
            or item["url"].endswith(":setIamPolicy")
        ]


def _json(status, value):
    return {
        "status": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(value).encode("utf-8"),
    }


def _adapter(cloud, *, writes=False):
    return bindings.Adapter(
        "adc", transport=cloud, token_source=lambda: "fake-token", writes=writes
    )


def _funded_cloud():
    cloud = FakeCloud()
    cloud.seed_dataset(
        STAGING,
        [
            {"role": "OWNER", "specialGroup": "projectOwners"},
            {"role": "READER", "userByEmail": "listening-post-staging" + SUFFIX},
        ],
    )
    cloud.seed_dataset(
        FUNDED_DATASET, [{"role": "OWNER", "specialGroup": "projectOwners"}]
    )
    cloud.seed_policy(
        CRM,
        [
            {"role": "roles/owner", "members": [OWNER]},
            {"role": "roles/bigquery.jobUser", "members": [APP]},
            {
                "role": "projects/ogilvy-trends-v2/roles/QuestionVertexPredict",
                "members": [APP],
                "condition": {
                    "title": "until end of september",
                    "expression": "request.time < timestamp('2026-09-30T23:59:59Z')",
                },
            },
        ],
        auditConfigs=[
            {"service": "allServices", "auditLogConfigs": [{"logType": "DATA_READ"}]}
        ],
        version=3,
    )
    cloud.seed_policy(FUNDED_JOB, [{"role": "roles/run.invoker", "members": [OWNER]}])
    cloud.seed_policy(
        SECRET, [{"role": "roles/secretmanager.admin", "members": [OWNER]}]
    )
    return cloud


@functools.cache
def _targets(members=("intelligence-42-funded",)):
    return bindings.load_targets(members=list(members))


def _strip(row):
    return {key: row[key] for key in ("condition", "member", "resource", "role")}


# Scope: exactly the delta's non routine rows


def test_the_pinned_delta_is_the_approved_amendment_d_file():
    assert bindings.DELTA_SHA256 == DELTA_SHA256
    assert hashlib.sha256(DELTA_PATH.read_bytes()).hexdigest() == DELTA_SHA256


def test_funded_targets_are_the_five_non_routine_delta_rows():
    targets = _targets()
    rows = sorted((_strip(row.as_dict()) for row in targets.rows), key=json.dumps)
    assert rows == sorted(FUNDED_ROWS, key=json.dumps)
    # The five routine rows of the funded identity stay with the iam-v2 words.
    assert targets.routine_rows == 5
    assert targets.refused == []


def test_secret_row_targets_the_secret_and_keeps_the_version_condition_verbatim():
    targets = _targets()
    (row,) = [row for row in targets.rows if row.kind == "secret_version"]
    assert row.target == (
        "//secretmanager.googleapis.com/projects/ogilvy-trends-v2/secrets/"
        "SOCIALCRAWL_OGILVY_API_KEY"
    )
    assert row.condition == SECRET_CONDITION


def test_whole_delta_scope_counts_refusals_and_project_level_rows():
    targets = bindings.load_targets()
    # 99 rows: 30 routine rows go to the iam-v2 words, 69 are non routine.
    assert targets.routine_rows == 30
    assert len(targets.rows) + len(targets.refused) == 69
    assert [
        (item["member"], item["role"], item["reason"]) for item in targets.refused
    ] == [
        (PRICE_POLICY, "roles/run.developer", "unresolved:price_policy_custom_run_role")
    ]
    project = targets.project_level
    assert len(project) == 8
    assert all(item["approved"] for item in project)
    assert {(item["role"], item["basis"]) for item in project} == {
        ("roles/bigquery.jobUser", "manifest_precedent"),
        ("roles/logging.logWriter", "build_project_log_writer"),
    }
    assert [
        item["member"] for item in project if item["role"] == "roles/logging.logWriter"
    ] == [BUILD]
    # The project level proposal the delta leaves unresolved is reported, not granted.
    assert targets.project_level_unresolved == [
        {
            "members": sorted(
                [
                    BRAIN,
                    FUNDED,
                    "serviceAccount:intelligence-42-migration" + SUFFIX,
                    "serviceAccount:intelligence-42-orchestration" + SUFFIX,
                ]
            ),
            "role": "roles/cloudbuild.builds.viewer",
            "topic": "cloudbuild_builds_viewer",
        }
    ]


def test_deploy_row_on_the_service_is_not_caught_by_the_price_policy_topic():
    targets = bindings.load_targets(members=["intelligence-42-deploy"])
    service = (
        "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/"
        "services/listening-post-staging"
    )
    assert (DEPLOY, "roles/run.developer", service) in {
        (row.member, row.role, row.resource) for row in targets.rows
    }
    assert targets.refused == []


def test_an_unknown_member_is_refused():
    with pytest.raises(ValueError, match="iam_delta_bindings_member_unknown"):
        bindings.load_targets(members=["intelligence-42-nobody"])
    with pytest.raises(ValueError, match="iam_delta_bindings_member_unknown"):
        bindings.load_targets(members=["listening-post-staging"])


def test_a_delta_whose_bytes_differ_from_the_pin_is_refused(tmp_path):
    changed = tmp_path / "iam_delta_v1.json"
    changed.write_bytes(
        DELTA_PATH.read_bytes().replace(b'"applied": false', b'"applied": false ')
    )
    with pytest.raises(ValueError, match="iam_delta_bindings_delta_digest_mismatch"):
        bindings.load_targets(delta_path=changed)


def _delta():
    return json.loads(DELTA_PATH.read_bytes())


def _manifest():
    return bindings.load_manifest()


def test_an_unknown_unresolved_topic_refuses_the_whole_run():
    delta = _delta()
    delta["unresolved"].append({"members": [], "statement": "new", "topic": "surprise"})
    with pytest.raises(ValueError, match="iam_delta_bindings_unresolved_unmapped"):
        bindings.select_targets(delta, _manifest(), DELTA_SHA256)


def test_a_row_under_an_unresolved_topic_is_refused_not_granted():
    delta = _delta()
    ingest = "serviceAccount:intelligence-42-ingest" + SUFFIX
    delta["bindings"].append(
        {
            "condition": SECRET_CONDITION,
            "member": ingest,
            "purpose": "derived: test",
            "resource": FUNDED_ROWS[4]["resource"],
            "role": "roles/secretmanager.secretAccessor",
        }
    )
    targets = bindings.select_targets(
        delta,
        _manifest(),
        DELTA_SHA256,
        members=["intelligence-42-ingest"],
        validate=False,
    )
    assert [item["reason"] for item in targets.refused] == [
        "unresolved:ingestion_source_credentials"
    ]
    assert all(row.kind != "secret_version" for row in targets.rows)


def test_rows_of_a_kind_this_tool_cannot_write_are_refused_not_fatal():
    delta = _delta()
    scheduler = "serviceAccount:intelligence-42-scheduler" + SUFFIX
    delta["bindings"].append(
        {
            "condition": None,
            "member": scheduler,
            "purpose": "derived: test",
            "resource": (
                "//cloudscheduler.googleapis.com/projects/ogilvy-trends-v2/locations/"
                "us-central1/jobs/intelligence-42-daily-staging"
            ),
            "role": "roles/cloudscheduler.viewer",
        }
    )
    delta["bindings"].append(
        {
            "condition": None,
            "member": FUNDED,
            "purpose": "derived: test",
            "resource": (
                "//secretmanager.googleapis.com/projects/ogilvy-trends-v2/secrets/"
                "SOCIALCRAWL_OGILVY_API_KEY"
            ),
            "role": "roles/secretmanager.secretAccessor",
        }
    )
    targets = bindings.select_targets(
        delta,
        _manifest(),
        DELTA_SHA256,
        members=["intelligence-42-scheduler", "intelligence-42-funded"],
        validate=False,
    )
    assert sorted((item["member"], item["reason"]) for item in targets.refused) == [
        (FUNDED, "kind_unsupported"),
        (scheduler, "unresolved:cloud_scheduler_job_iam"),
    ]


def test_a_project_level_row_outside_the_approved_classes_is_refused():
    delta = _delta()
    delta["bindings"].append(
        {
            "condition": None,
            "member": FUNDED,
            "purpose": "derived: test",
            "resource": "//cloudresourcemanager.googleapis.com/projects/ogilvy-trends-v2",
            "role": "roles/run.viewer",
        }
    )
    targets = bindings.select_targets(
        delta,
        _manifest(),
        DELTA_SHA256,
        members=["intelligence-42-funded"],
        validate=False,
    )
    assert [(item["role"], item["reason"]) for item in targets.refused] == [
        ("roles/run.viewer", "project_level_unapproved")
    ]
    assert {(item["role"], item["approved"]) for item in targets.project_level} == {
        ("roles/bigquery.jobUser", True),
        ("roles/run.viewer", False),
    }


def test_a_job_user_row_without_the_precedent_label_is_not_approved():
    delta = _delta()
    for row in delta["bindings"]:
        if row["member"] == FUNDED and row["role"] == "roles/bigquery.jobUser":
            row["purpose"] = "derived: relabelled"
    targets = bindings.select_targets(
        delta,
        _manifest(),
        DELTA_SHA256,
        members=["intelligence-42-funded"],
        validate=False,
    )
    assert [item["reason"] for item in targets.refused] == ["project_level_unapproved"]


def test_a_secret_condition_naming_another_version_is_refused():
    delta = _delta()
    for row in delta["bindings"]:
        if row["role"] == "roles/secretmanager.secretAccessor":
            row["condition"] = row["condition"].replace("/versions/1'", "/versions/2'")
    with pytest.raises(ValueError, match="iam_delta_bindings_condition_invalid"):
        bindings.select_targets(
            delta,
            _manifest(),
            DELTA_SHA256,
            members=["intelligence-42-funded"],
            validate=False,
        )


def test_the_delta_is_validated_before_selection():
    delta = _delta()
    delta["bindings"][42]["role"] = "roles/bigquery.admin"
    with pytest.raises(ValueError, match="iam_delta_role_forbidden"):
        bindings.select_targets(delta, _manifest(), DELTA_SHA256)


# Request building: every URL from a pinned grammar


@pytest.mark.parametrize(
    "resource",
    [
        "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/jobs/x:setIamPolicy",
        "//run.googleapis.com/projects/other-project/locations/us-central1/jobs/intelligence-42-brain-staging",
        "//run.googleapis.com/projects/ogilvy-trends-v2/locations/europe-west1/jobs/intelligence-42-brain-staging",
        "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/trends_v2?x=1",
        "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/trends_v2/tables/t",
        "//secretmanager.googleapis.com/projects/ogilvy-trends-v2/secrets/S/versions/1/x",
        "//storage.googleapis.com/projects/_/buckets/a/../b",
        "//cloudresourcemanager.googleapis.com/projects/ogilvy-trends-v2/x",
        "//iam.googleapis.com/projects/ogilvy-trends-v2/serviceAccounts/a@evil.example.com",
    ],
)
def test_a_resource_outside_the_pinned_grammar_never_reaches_a_url(resource):
    with pytest.raises(ValueError, match="resource_name_invalid"):
        bindings.build_request("get_policy", resource=resource)


def test_read_requests_are_the_pinned_rest_calls():
    expected = {
        "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/trends_v2_staging": (
            "GET",
            STAGING + "?accessPolicyVersion=3",
            None,
        ),
        "//cloudresourcemanager.googleapis.com/projects/ogilvy-trends-v2": (
            "POST",
            CRM + ":getIamPolicy",
            {"options": {"requestedPolicyVersion": 3}},
        ),
        FUNDED_ROWS[3]["resource"]: ("GET", FUNDED_JOB + ":getIamPolicy?" + V3, None),
        (
            "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/"
            "services/listening-post-staging"
        ): ("GET", RUN + "services/listening-post-staging:getIamPolicy?" + V3, None),
        FUNDED_ROWS[4]["resource"]: ("GET", SECRET + ":getIamPolicy?" + V3, None),
        "//storage.googleapis.com/projects/_/buckets/listening-post-staging-cache": (
            "GET",
            STORAGE
            + "listening-post-staging-cache/iam?optionsRequestedPolicyVersion=3",
            None,
        ),
        "//iam.googleapis.com/projects/ogilvy-trends-v2/serviceAccounts/intelligence-42-funded"
        + SUFFIX: (
            "POST",
            IAM + "intelligence-42-funded" + SUFFIX + ":getIamPolicy?" + V3,
            None,
        ),
        (
            "//cloudtasks.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/"
            "queues/oi-general-question-staging"
        ): (
            "POST",
            TASKS + "queues/oi-general-question-staging:getIamPolicy",
            {"options": {"requestedPolicyVersion": 3}},
        ),
        (
            "//artifactregistry.googleapis.com/projects/ogilvy-trends-v2/locations/"
            "us-central1/repositories/intelligence-42"
        ): ("GET", REGISTRY + "repositories/intelligence-42:getIamPolicy?" + V3, None),
    }
    for resource, (method, url, body) in expected.items():
        request = bindings.build_request("get_policy", resource=resource)
        assert (request["method"], request["url"], request.get("json")) == (
            method,
            url,
            body,
        ), resource


# plan


def test_plan_reads_only_and_reports_every_missing_row():
    cloud = _funded_cloud()
    receipt = bindings.run("plan", _targets(), _adapter(cloud))
    assert cloud.writes() == []
    assert receipt["mode"] == "plan"
    assert receipt["delta_sha256"] == DELTA_SHA256
    assert receipt["rows"]["target"] == 5
    assert receipt["rows"]["present"] == []
    assert sorted(
        (_strip(row) for row in receipt["rows"]["missing"]), key=json.dumps
    ) == sorted(FUNDED_ROWS, key=json.dumps)
    assert receipt["rows"]["absent"] == []


def test_plan_matches_legacy_dataset_roles_and_iam_member_entries():
    cloud = _funded_cloud()
    cloud.datasets[STAGING]["access"].append(
        {"role": "WRITER", "userByEmail": "intelligence-42-funded" + SUFFIX}
    )
    cloud.datasets[FUNDED_DATASET]["access"].append(
        {"role": "roles/bigquery.dataEditor", "iamMember": FUNDED}
    )
    receipt = bindings.run("plan", _targets(), _adapter(cloud))
    assert sorted(
        row["resource"].rsplit("/", 1)[1] for row in receipt["rows"]["present"]
    ) == [
        "trends_v2_staging",
        "trends_v2_staging_funded",
    ]


def test_an_unconditional_grant_does_not_satisfy_the_version_pinned_row():
    cloud = _funded_cloud()
    cloud.policies[SECRET]["bindings"].append(
        {"role": "roles/secretmanager.secretAccessor", "members": [FUNDED]}
    )
    receipt = bindings.run("plan", _targets(), _adapter(cloud))
    missing = [row for row in receipt["rows"]["missing"] if row["condition"]]
    assert [row["condition"] for row in missing] == [SECRET_CONDITION]
    assert receipt["broader_grants"] == [
        {
            "member": FUNDED,
            "resource": FUNDED_ROWS[4]["resource"],
            "role": "roles/secretmanager.secretAccessor",
            "held": "unconditional",
        }
    ]


def test_a_conditional_grant_does_not_satisfy_an_unconditional_row():
    cloud = _funded_cloud()
    cloud.policies[FUNDED_JOB]["bindings"].append(
        {
            "role": "roles/run.viewer",
            "members": [FUNDED],
            "condition": {
                "title": "t",
                "expression": "request.time < timestamp('2030-01-01T00:00:00Z')",
            },
        }
    )
    cloud.policies[FUNDED_JOB]["version"] = 3
    receipt = bindings.run("plan", _targets(), _adapter(cloud))
    assert FUNDED_ROWS[3]["resource"] in {
        row["resource"] for row in receipt["rows"]["missing"]
    }


def test_a_conditional_grant_with_the_same_expression_counts_whatever_its_title():
    cloud = _funded_cloud()
    cloud.policies[SECRET]["bindings"].append(
        {
            "role": "roles/secretmanager.secretAccessor",
            "members": [FUNDED],
            "condition": {"title": "hand made", "expression": SECRET_CONDITION},
        }
    )
    cloud.policies[SECRET]["version"] = 3
    receipt = bindings.run("plan", _targets(), _adapter(cloud))
    assert FUNDED_ROWS[4]["resource"] in {
        row["resource"] for row in receipt["rows"]["present"]
    }


def test_an_absent_resource_is_reported_and_a_refused_read_fails_closed():
    cloud = _funded_cloud()
    del cloud.policies[FUNDED_JOB]
    receipt = bindings.run("plan", _targets(), _adapter(cloud))
    assert [row["resource"] for row in receipt["rows"]["absent"]] == [
        FUNDED_ROWS[3]["resource"]
    ]

    class Denied(FakeCloud):
        def __call__(self, request):
            response = super().__call__(request)
            if request["url"].startswith(SECRET):
                return _json(403, {"error": "denied"})
            return response

    denied = Denied()
    denied.__dict__.update(copy.deepcopy(_funded_cloud().__dict__))
    with pytest.raises(bindings.NativeError):
        bindings.run("plan", _targets(), _adapter(denied))


# dry run


def test_dry_run_renders_the_exact_add_only_requests_and_sends_none():
    cloud = _funded_cloud()
    receipt = bindings.run("dry_run", _targets(), _adapter(cloud))
    assert cloud.writes() == []
    requests = {item["url"]: item for item in receipt["requests"]}
    assert set(requests) == {
        STAGING + "?accessPolicyVersion=3",
        FUNDED_DATASET + "?accessPolicyVersion=3",
        CRM + ":setIamPolicy",
        FUNDED_JOB + ":setIamPolicy",
        SECRET + ":setIamPolicy",
    }
    staging = requests[STAGING + "?accessPolicyVersion=3"]
    assert staging["method"] == "PATCH"
    assert staging["headers"] == {"If-Match": cloud.datasets[STAGING]["etag"]}
    assert staging["body"] == {
        "access": [
            {"role": "OWNER", "specialGroup": "projectOwners"},
            {"role": "READER", "userByEmail": "listening-post-staging" + SUFFIX},
            {"role": "WRITER", "userByEmail": "intelligence-42-funded" + SUFFIX},
        ]
    }
    project = requests[CRM + ":setIamPolicy"]["body"]["policy"]
    before = cloud.policies[CRM]
    assert project["etag"] == before["etag"]
    # The project policy was read at version 3 and holds a conditional binding.
    assert project["version"] == 3
    assert project["auditConfigs"] == before["auditConfigs"]
    # The member joins the existing unconditional binding; nothing else moves.
    assert project["bindings"] == [
        before["bindings"][0],
        {"role": "roles/bigquery.jobUser", "members": [APP, FUNDED]},
        before["bindings"][2],
    ]
    secret = requests[SECRET + ":setIamPolicy"]["body"]["policy"]
    assert secret["version"] == 3
    assert secret["etag"] == cloud.policies[SECRET]["etag"]
    assert secret["bindings"] == [
        {"role": "roles/secretmanager.admin", "members": [OWNER]},
        {
            "role": "roles/secretmanager.secretAccessor",
            "members": [FUNDED],
            "condition": {
                "title": bindings.condition_title(SECRET_CONDITION),
                "expression": SECRET_CONDITION,
            },
        },
    ]
    assert receipt["writes_planned"] == 5
    assert re.fullmatch(r"[0-9a-f]{64}", receipt["planned_payload_sha256"])


def test_dry_run_never_sends_a_write_even_if_asked_through_the_adapter():
    cloud = _funded_cloud()
    adapter = _adapter(cloud)
    with pytest.raises(ValueError, match="iam_delta_bindings_write_not_allowed"):
        adapter.perform(
            "set_policy",
            resource=FUNDED_ROWS[3]["resource"],
            before=cloud.policies[FUNDED_JOB],
            additions=[],
        )
    assert cloud.requests == []


# apply


def test_apply_adds_only_missing_rows_keeps_every_member_and_second_run_adds_nothing():
    cloud = _funded_cloud()
    cloud.datasets[FUNDED_DATASET]["access"].append(
        {"role": "WRITER", "userByEmail": "intelligence-42-funded" + SUFFIX}
    )
    before_project = copy.deepcopy(cloud.policies[CRM])
    receipt = bindings.run("apply", _targets(), _adapter(cloud, writes=True))
    assert receipt["rows"] == {
        "target": 5,
        "present_before": 1,
        "added": 4,
        "present_after": 5,
        "absent": [],
    }
    assert receipt["complete"] is True
    assert len(cloud.writes()) == 4
    after_project = cloud.policies[CRM]
    for binding in before_project["bindings"]:
        match = [
            item
            for item in after_project["bindings"]
            if item["role"] == binding["role"]
            and item.get("condition") == binding.get("condition")
        ]
        assert match and set(binding["members"]) <= set(match[0]["members"])
    assert after_project["auditConfigs"] == before_project["auditConfigs"]

    cloud.requests.clear()
    second = bindings.run("apply", _targets(), _adapter(cloud, writes=True))
    assert cloud.writes() == []
    assert second["rows"]["added"] == 0
    assert second["rows"]["present_after"] == 5
    assert second["readback_sha256"] == receipt["readback_sha256"]


def _whole_cloud(targets):
    cloud = FakeCloud()
    for target in sorted({row.target for row in targets.rows}):
        url = bindings.build_request("get_policy", resource=target)["url"]
        if url.startswith(BQ):
            cloud.seed_dataset(
                url.partition("?")[0],
                [{"role": "OWNER", "specialGroup": "projectOwners"}],
            )
        elif url.startswith(STORAGE):
            cloud.seed_policy(
                url.partition("/iam")[0],
                [
                    {
                        "role": "roles/storage.legacyBucketOwner",
                        "members": ["projectOwner:" + PROJECT],
                    }
                ],
                kind="storage#policy",
                resourceId="projects/_/buckets/listening-post-staging-cache",
            )
        else:
            cloud.seed_policy(
                url.partition(":getIamPolicy")[0],
                [{"role": "roles/owner", "members": [OWNER]}],
            )
    return cloud


def test_whole_delta_apply_grants_every_approved_row_once():
    targets = bindings.load_targets()
    cloud = _whole_cloud(targets)
    receipt = bindings.run("apply", targets, _adapter(cloud, writes=True))
    assert receipt["rows"]["target"] == 68
    assert receipt["rows"]["added"] == 68
    assert receipt["rows"]["present_after"] == 68
    assert receipt["complete"] is True
    bucket = cloud.policies[STORAGE + "listening-post-staging-cache"]
    assert bucket["version"] == 3
    assert bucket["kind"] == "storage#policy"
    assert bucket["bindings"][0] == {
        "role": "roles/storage.legacyBucketOwner",
        "members": ["projectOwner:" + PROJECT],
    }
    conditional = [item for item in bucket["bindings"] if "condition" in item]
    # Five conditional rows; the two ledger readers share one role and condition.
    assert len(conditional) == 4
    assert sum(len(item["members"]) for item in conditional) == 5
    cloud.requests.clear()
    second = bindings.run("apply", targets, _adapter(cloud, writes=True))
    assert cloud.writes() == []
    assert second["rows"]["added"] == 0
    # The price policy row the delta leaves unresolved was never written.
    service = cloud.policies[RUN + "services/listening-post-staging"]
    assert PRICE_POLICY not in {
        m for item in service["bindings"] for m in item["members"]
    }


def test_apply_sends_the_read_etag_so_a_concurrent_change_refuses():
    cloud = _funded_cloud()
    adapter = _adapter(cloud, writes=True)
    original = cloud.__call__

    def racing(request):
        if request["url"] == FUNDED_JOB + ":setIamPolicy":
            cloud.policies[FUNDED_JOB]["etag"] = "moved"
        return original(request)

    adapter.transport = racing
    with pytest.raises(bindings.WriteRefusal) as caught:
        bindings.run("apply", _targets(), adapter)
    assert caught.value.applied["failed_at"] == FUNDED_ROWS[3]["resource"]


def test_apply_fails_closed_when_the_readback_does_not_show_the_write():
    cloud = _funded_cloud()
    cloud.drop_writes = True
    with pytest.raises(
        bindings.WriteRefusal, match="iam_delta_bindings_readback_mismatch"
    ) as caught:
        bindings.run("apply", _targets(), _adapter(cloud, writes=True))
    assert caught.value.applied["writes"]
    assert caught.value.applied["failed_at"] == "readback"


def test_apply_fails_closed_when_an_existing_member_vanished():
    cloud = _funded_cloud()
    cloud.lose_on_write = ("roles/owner", OWNER)
    with pytest.raises(
        bindings.WriteRefusal, match="iam_delta_bindings_readback_mismatch"
    ):
        bindings.run("apply", _targets(), _adapter(cloud, writes=True))


def test_a_failed_write_names_what_already_landed():
    cloud = _funded_cloud()
    cloud.fail_write_at = 2
    with pytest.raises(
        bindings.WriteRefusal, match="iam_delta_bindings_write_refused"
    ) as caught:
        bindings.run("apply", _targets(), _adapter(cloud, writes=True))
    applied = caught.value.applied
    assert len(applied["writes"]) == 2
    assert applied["failed_at"] not in applied["writes"]


def test_apply_skips_an_absent_resource_and_reports_the_run_incomplete():
    cloud = _funded_cloud()
    del cloud.policies[FUNDED_JOB]
    receipt = bindings.run("apply", _targets(), _adapter(cloud, writes=True))
    assert receipt["complete"] is False
    assert [row["resource"] for row in receipt["rows"]["absent"]] == [
        FUNDED_ROWS[3]["resource"]
    ]
    assert receipt["rows"]["added"] == 4
    assert FUNDED_JOB + ":setIamPolicy" not in {item["url"] for item in cloud.writes()}


def test_the_send_path_refuses_an_addition_outside_the_targets():
    cloud = _funded_cloud()
    adapter = _adapter(cloud, writes=True)
    stray = bindings.Row(
        member=OWNER,
        role="roles/run.admin",
        resource=FUNDED_ROWS[3]["resource"],
        condition=None,
        kind="run",
        target=FUNDED_ROWS[3]["resource"],
    )
    with pytest.raises(ValueError, match="iam_delta_bindings_write_refused"):
        adapter.perform(
            "set_policy",
            resource=FUNDED_ROWS[3]["resource"],
            before=cloud.policies[FUNDED_JOB],
            additions=[stray],
            allowed=_targets().rows,
        )
    assert cloud.writes() == []


def test_the_send_path_refuses_an_addition_for_another_resource():
    cloud = _funded_cloud()
    adapter = _adapter(cloud, writes=True)
    (secret_row,) = [row for row in _targets().rows if row.kind == "secret_version"]
    with pytest.raises(ValueError, match="iam_delta_bindings_write_refused"):
        adapter.perform(
            "set_policy",
            resource=FUNDED_ROWS[3]["resource"],
            before=cloud.policies[FUNDED_JOB],
            additions=[secret_row],
            allowed=_targets().rows,
        )
    assert cloud.writes() == []


@pytest.mark.parametrize("target", [FUNDED_JOB, STAGING])
def test_the_send_path_refuses_a_policy_that_drops_an_existing_grant(
    monkeypatch, target
):
    cloud = _funded_cloud()
    real = bindings._revised

    def dropping(kind, before, additions):
        revised = real(kind, before, additions)
        if "access" in revised:
            revised["access"] = revised["access"][1:]
        else:
            revised["bindings"] = revised["bindings"][1:]
        return revised

    monkeypatch.setattr(bindings, "_revised", dropping)
    rows = {row.target: row for row in _targets().rows}
    resource = (
        FUNDED_ROWS[3]["resource"]
        if target == FUNDED_JOB
        else FUNDED_ROWS[0]["resource"]
    )
    before = (
        cloud.policies[FUNDED_JOB] if target == FUNDED_JOB else cloud.datasets[STAGING]
    )
    adapter = _adapter(cloud, writes=True)
    with pytest.raises(ValueError, match="iam_delta_bindings_write_refused"):
        adapter.perform(
            "set_policy",
            resource=resource,
            before=before,
            additions=[rows[resource]],
            allowed=_targets().rows,
        )
    assert cloud.writes() == []


def test_the_set_response_is_never_the_proof():
    cloud = _funded_cloud()
    cloud.drop_writes = True
    with pytest.raises(bindings.WriteRefusal):
        bindings.run("apply", _targets(), _adapter(cloud, writes=True))
    readbacks = [item for item in cloud.requests if item not in cloud.writes()]
    # A fresh read of every target follows the writes.
    assert len(readbacks) >= 10


# Owner run words


def test_words_map_to_modes():
    assert bindings.WORDS == {
        "iam-delta-plan": "plan",
        "iam-delta-dry-run": "dry_run",
        "iam-delta-apply": "apply",
    }


def test_main_emits_the_receipt_and_only_apply_may_write():
    cloud = _funded_cloud()
    made = []

    def factory(state, writes):
        made.append((state, writes))
        return _adapter(cloud, writes=writes)

    out = io.StringIO()
    code = bindings.main(
        ["iam-delta-dry-run", "--member", "intelligence-42-funded"],
        adapter_factory=factory,
        out=out,
    )
    assert code == 0
    assert json.loads(out.getvalue())["mode"] == "dry_run"
    assert cloud.writes() == []
    out = io.StringIO()
    code = bindings.main(
        [
            "iam-delta-apply",
            "--member",
            "intelligence-42-funded",
            "--adapter-state",
            "owner-gcloud",
        ],
        adapter_factory=factory,
        out=out,
    )
    assert code == 0
    assert json.loads(out.getvalue())["rows"]["present_after"] == 5
    assert made == [("owner-gcloud", False), ("owner-gcloud", True)]


def test_main_reports_a_refusal_as_one_json_line():
    cloud = _funded_cloud()
    cloud.drop_writes = True
    out = io.StringIO()
    code = bindings.main(
        ["iam-delta-apply", "--member", "intelligence-42-funded"],
        adapter_factory=lambda state, writes: _adapter(cloud, writes=writes),
        out=out,
    )
    assert code == 1
    payload = json.loads(out.getvalue())
    assert payload["error"] == "iam_delta_bindings_readback_mismatch"
    assert payload["applied"]["failed_at"] == "readback"
    out = io.StringIO()
    assert (
        bindings.main(
            ["iam-delta-plan", "--member", "nobody"], adapter_factory=None, out=out
        )
        == 1
    )
    assert json.loads(out.getvalue()) == {"error": "iam_delta_bindings_member_unknown"}


# Review round: the send path trusts only the pinned delta, keeps the policy
# version, and an incomplete apply is not a success.

PROJECT_RESOURCE = "//cloudresourcemanager.googleapis.com/projects/ogilvy-trends-v2"


@pytest.mark.parametrize("scoped", [True, False])
def test_the_send_path_refuses_a_row_outside_the_pinned_delta_even_if_passed_as_allowed(
    scoped,
):
    cloud = _funded_cloud()
    adapter = _adapter(cloud, writes=True)
    forged = bindings.Row(
        member=FUNDED,
        role="roles/resourcemanager.projectIamAdmin",
        resource=PROJECT_RESOURCE,
        condition=None,
        kind="project",
        target=PROJECT_RESOURCE,
    )
    allowed = [forged, *_targets().rows] if scoped else [forged]
    with pytest.raises(ValueError, match="iam_delta_bindings_write_refused"):
        adapter.perform(
            "set_policy",
            resource=PROJECT_RESOURCE,
            before=cloud.policies[CRM],
            additions=[forged],
            allowed=allowed,
        )
    assert cloud.writes() == []
    assert not [
        item
        for item in cloud.policies[CRM]["bindings"]
        if item["role"] == "roles/resourcemanager.projectIamAdmin"
    ]


def test_the_send_path_refuses_a_policy_version_below_what_was_read(monkeypatch):
    cloud = _funded_cloud()
    cloud.policies[FUNDED_JOB]["version"] = 3
    real = bindings._revised

    def downgrading(kind, before, additions):
        revised = real(kind, before, additions)
        revised["version"] = 1
        return revised

    monkeypatch.setattr(bindings, "_revised", downgrading)
    rows = {row.target: row for row in _targets().rows}
    adapter = _adapter(cloud, writes=True)
    with pytest.raises(ValueError, match="iam_delta_bindings_write_refused"):
        adapter.perform(
            "set_policy",
            resource=FUNDED_ROWS[3]["resource"],
            before=cloud.policies[FUNDED_JOB],
            additions=[rows[FUNDED_ROWS[3]["resource"]]],
            allowed=_targets().rows,
        )
    assert cloud.writes() == []


def test_a_write_keeps_the_version_it_read_on_a_policy_without_conditions():
    cloud = _funded_cloud()
    cloud.policies[FUNDED_JOB]["version"] = 3
    receipt = bindings.run("dry_run", _targets(), _adapter(cloud))
    (job,) = [
        item
        for item in receipt["requests"]
        if item["url"] == FUNDED_JOB + ":setIamPolicy"
    ]
    assert job["body"]["policy"]["version"] == 3
    assert not [
        item for item in job["body"]["policy"]["bindings"] if "condition" in item
    ]


@pytest.mark.parametrize("version", [None, 1, 2])
def test_the_send_path_refuses_a_conditional_policy_not_at_version_3(
    monkeypatch, version
):
    cloud = _funded_cloud()
    real = bindings._revised

    def unversioned(kind, before, additions):
        revised = real(kind, before, additions)
        if version is None:
            revised.pop("version", None)
        else:
            revised["version"] = version
        return revised

    monkeypatch.setattr(bindings, "_revised", unversioned)
    (secret_row,) = [row for row in _targets().rows if row.kind == "secret_version"]
    adapter = _adapter(cloud, writes=True)
    with pytest.raises(ValueError, match="iam_delta_bindings_write_refused"):
        adapter.perform(
            "set_policy",
            resource=secret_row.target,
            before=cloud.policies[SECRET],
            additions=[secret_row],
            allowed=_targets().rows,
        )
    assert cloud.writes() == []


def test_an_apply_that_meets_an_absent_resource_exits_non_zero():
    cloud = _funded_cloud()
    del cloud.policies[FUNDED_JOB]
    out = io.StringIO()
    code = bindings.main(
        ["iam-delta-apply", "--member", "intelligence-42-funded"],
        adapter_factory=lambda state, writes: _adapter(cloud, writes=writes),
        out=out,
    )
    receipt = json.loads(out.getvalue())
    assert receipt["complete"] is False
    assert [row["resource"] for row in receipt["rows"]["absent"]] == [
        FUNDED_ROWS[3]["resource"]
    ]
    assert code == 3


DATASET_CONDITION = {
    "title": "until end of year",
    "expression": "request.time < timestamp('2030-01-01T00:00:00Z')",
}


def _conditional_dataset_cloud():
    cloud = _funded_cloud()
    cloud.datasets[STAGING]["access"].append(
        {
            "role": "roles/bigquery.dataViewer",
            "iamMember": "user:analyst@ogilvy.co.za",
            "condition": copy.deepcopy(DATASET_CONDITION),
        }
    )
    return cloud


def test_dry_run_keeps_a_conditional_dataset_entry_verbatim():
    cloud = _conditional_dataset_cloud()
    receipt = bindings.run("dry_run", _targets(), _adapter(cloud))
    (staging,) = [
        item
        for item in receipt["requests"]
        if item["url"] == STAGING + "?accessPolicyVersion=3"
    ]
    assert staging["body"]["access"][:3] == cloud.datasets[STAGING]["access"]
    assert staging["body"]["access"][2]["condition"] == DATASET_CONDITION


def test_the_send_path_refuses_a_dataset_that_loses_a_kept_condition(monkeypatch):
    cloud = _conditional_dataset_cloud()
    real = bindings._revised

    def unconditioned(kind, before, additions):
        revised = real(kind, before, additions)
        if "access" in revised:
            for entry in revised["access"]:
                entry.pop("condition", None)
        return revised

    monkeypatch.setattr(bindings, "_revised", unconditioned)
    rows = {row.target: row for row in _targets().rows}
    adapter = _adapter(cloud, writes=True)
    with pytest.raises(ValueError, match="iam_delta_bindings_write_refused"):
        adapter.perform(
            "set_policy",
            resource=FUNDED_ROWS[0]["resource"],
            before=cloud.datasets[STAGING],
            additions=[rows[FUNDED_ROWS[0]["resource"]]],
            allowed=_targets().rows,
        )
    assert cloud.writes() == []


@pytest.mark.parametrize("store", ["policy", "dataset"])
def test_a_read_without_an_etag_is_refused(store):
    cloud = _funded_cloud()
    if store == "policy":
        del cloud.policies[CRM]["etag"]
    else:
        del cloud.datasets[STAGING]["etag"]
    with pytest.raises(ValueError, match="iam_delta_bindings_etag_missing"):
        bindings.run("plan", _targets(), _adapter(cloud))
    assert cloud.writes() == []


def test_a_readback_without_an_etag_fails_closed():
    cloud = _funded_cloud()
    original = cloud.__call__
    wrote = []

    def stripping(request):
        response = original(request)
        if request["method"] in {"PATCH", "PUT"} or request["url"].endswith(
            ":setIamPolicy"
        ):
            wrote.append(request["url"])
        elif wrote and request["url"].startswith(CRM):
            payload = json.loads(response["body"])
            payload.pop("etag", None)
            response = _json(response["status"], payload)
        return response

    adapter = _adapter(cloud, writes=True)
    adapter.transport = stripping
    with pytest.raises(
        bindings.WriteRefusal, match="iam_delta_bindings_readback_mismatch"
    ) as caught:
        bindings.run("apply", _targets(), adapter)
    assert caught.value.applied["failed_at"] == "readback"


@pytest.mark.parametrize("mode", ["plan", "dry_run"])
def test_plan_and_dry_run_refuse_a_write_enabled_adapter(mode):
    cloud = _funded_cloud()
    with pytest.raises(ValueError, match="iam_delta_bindings_write_not_allowed"):
        bindings.run(mode, _targets(), _adapter(cloud, writes=True))
    assert cloud.requests == []


def test_a_must_not_grant_routine_row_is_refused_not_counted_as_routine():
    delta = _delta()
    (forbidden, *_rest) = [entry["resource"] for entry in delta["must_not_grant"]]
    delta["bindings"].append(
        {
            "condition": None,
            "member": FUNDED,
            "purpose": "derived: test",
            "resource": forbidden,
            "role": "roles/bigquery.dataViewer",
        }
    )
    targets = bindings.select_targets(
        delta,
        _manifest(),
        DELTA_SHA256,
        members=["intelligence-42-funded"],
        validate=False,
    )
    assert [(item["resource"], item["reason"]) for item in targets.refused] == [
        (forbidden, "must_not_grant")
    ]
    assert targets.routine_rows == 5


def _flaky_writes(cloud, fail_on, *, reads_after=False):
    """A transport whose fail_on th write, or first read after a write, breaks."""
    counts = {"writes": 0}

    def transport(request):
        write = request["method"] in {"PATCH", "PUT"} or request["url"].endswith(
            ":setIamPolicy"
        )
        if write:
            counts["writes"] += 1
            if not reads_after and counts["writes"] == fail_on:
                raise http.client.IncompleteRead(b"")
        elif reads_after and counts["writes"]:
            raise http.client.IncompleteRead(b"")
        return cloud(request)

    return transport


def test_an_incomplete_read_during_a_write_still_names_the_writes_that_landed():
    cloud = _funded_cloud()
    adapter = _adapter(cloud, writes=True)
    adapter.transport = _flaky_writes(cloud, 2)
    with pytest.raises(
        bindings.WriteRefusal, match="iam_delta_bindings_write_refused"
    ) as caught:
        bindings.run("apply", _targets(), adapter)
    applied = caught.value.applied
    assert len(applied["writes"]) == 1
    assert applied["failed_at"] is not None
    assert applied["failed_at"] not in applied["writes"]

    cloud = _funded_cloud()
    adapter = _adapter(cloud, writes=True)
    adapter.transport = _flaky_writes(cloud, 2)
    out = io.StringIO()
    code = bindings.main(
        ["iam-delta-apply", "--member", "intelligence-42-funded"],
        adapter_factory=lambda state, writes: adapter,
        out=out,
    )
    assert code == 1
    payload = json.loads(out.getvalue())
    assert payload["error"] == "iam_delta_bindings_write_refused"
    assert len(payload["applied"]["writes"]) == 1


def test_an_incomplete_read_during_readback_fails_closed_with_the_writes_named():
    cloud = _funded_cloud()
    adapter = _adapter(cloud, writes=True)
    adapter.transport = _flaky_writes(cloud, None, reads_after=True)
    with pytest.raises(
        bindings.WriteRefusal, match="iam_delta_bindings_readback_mismatch"
    ) as caught:
        bindings.run("apply", _targets(), adapter)
    assert len(caught.value.applied["writes"]) == 5
    assert caught.value.applied["failed_at"] == "readback"


def test_an_incomplete_read_before_any_write_is_one_json_refusal():
    cloud = _funded_cloud()

    def broken(request):
        raise http.client.IncompleteRead(b"")

    adapter = _adapter(cloud)
    adapter.transport = broken
    out = io.StringIO()
    code = bindings.main(
        ["iam-delta-plan", "--member", "intelligence-42-funded"],
        adapter_factory=lambda state, writes: adapter,
        out=out,
    )
    assert code == 1
    assert "error" in json.loads(out.getvalue())


# Review round two: the checks that only a mutant showed untested.

KEPT_CONDITION = {
    "title": "keep",
    "expression": "request.time < timestamp('2030-01-01T00:00:00Z')",
}


def _conditional_job_cloud():
    cloud = _funded_cloud()
    cloud.policies[FUNDED_JOB]["bindings"].append(
        {
            "role": "roles/run.invoker",
            "members": ["user:analyst@ogilvy.co.za"],
            "condition": copy.deepcopy(KEPT_CONDITION),
        }
    )
    cloud.policies[FUNDED_JOB]["version"] = 3
    return cloud


def test_a_readback_where_a_kept_conditional_binding_came_back_unconditional_fails():
    cloud = _conditional_job_cloud()
    stored = cloud._set

    def broadening(base, policy):
        response = stored(base, policy)
        if base == FUNDED_JOB:
            for item in cloud.policies[base]["bindings"]:
                item.pop("condition", None)
        return response

    cloud._set = broadening
    out = io.StringIO()
    code = bindings.main(
        ["iam-delta-apply", "--member", "intelligence-42-funded"],
        adapter_factory=lambda state, writes: _adapter(cloud, writes=writes),
        out=out,
    )
    payload = json.loads(out.getvalue())
    assert code == 1
    assert payload["error"] == "iam_delta_bindings_readback_mismatch"
    assert payload["applied"]["failed_at"] == "readback"
    assert "complete" not in payload


@pytest.mark.parametrize("placement", ["new_binding", "existing_binding"])
def test_the_send_path_refuses_a_revised_policy_that_adds_more_than_the_rows(
    monkeypatch, placement
):
    cloud = _funded_cloud()
    real = bindings._revised

    def granting(kind, before, additions):
        revised = real(kind, before, additions)
        if placement == "new_binding":
            revised["bindings"].append({"role": "roles/owner", "members": [FUNDED]})
        else:
            revised["bindings"][0]["members"].append(FUNDED)
        return revised

    monkeypatch.setattr(bindings, "_revised", granting)
    rows = {row.target: row for row in _targets().rows}
    adapter = _adapter(cloud, writes=True)
    with pytest.raises(ValueError, match="iam_delta_bindings_write_refused"):
        adapter.perform(
            "set_policy",
            resource=FUNDED_ROWS[3]["resource"],
            before=cloud.policies[FUNDED_JOB],
            additions=[rows[FUNDED_ROWS[3]["resource"]]],
            allowed=_targets().rows,
        )
    assert cloud.writes() == []
    assert FUNDED not in {
        member
        for item in cloud.policies[FUNDED_JOB]["bindings"]
        for member in item["members"]
    }


@pytest.mark.parametrize(
    "change", [{"title": "changed"}, {"description": "added by nobody"}]
)
def test_the_send_path_refuses_a_revised_policy_that_changes_a_kept_condition(
    monkeypatch, change
):
    cloud = _conditional_job_cloud()
    real = bindings._revised

    def rewording(kind, before, additions):
        revised = real(kind, before, additions)
        for item in revised["bindings"]:
            if "condition" in item:
                item["condition"].update(change)
        return revised

    monkeypatch.setattr(bindings, "_revised", rewording)
    rows = {row.target: row for row in _targets().rows}
    adapter = _adapter(cloud, writes=True)
    with pytest.raises(ValueError, match="iam_delta_bindings_write_refused"):
        adapter.perform(
            "set_policy",
            resource=FUNDED_ROWS[3]["resource"],
            before=cloud.policies[FUNDED_JOB],
            additions=[rows[FUNDED_ROWS[3]["resource"]]],
            allowed=_targets().rows,
        )
    assert cloud.writes() == []


@pytest.mark.parametrize("allowed", ["funded_rows", "empty"])
def test_the_caller_allowed_rows_narrow_the_pinned_set(allowed):
    cloud = _funded_cloud()
    other = next(
        row
        for row in bindings.load_targets().rows
        if row.kind == "project" and row.member != FUNDED
    )
    assert other in bindings._pinned_rows()
    adapter = _adapter(cloud, writes=True)
    with pytest.raises(ValueError, match="iam_delta_bindings_write_refused"):
        adapter.perform(
            "set_policy",
            resource=PROJECT_RESOURCE,
            before=cloud.policies[CRM],
            additions=[other],
            allowed=_targets().rows if allowed == "funded_rows" else [],
        )
    assert cloud.writes() == []
