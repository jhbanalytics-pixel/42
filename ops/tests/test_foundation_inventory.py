import hashlib
import hmac
import json
import os
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from ops.deploy.production_baseline import (
    capture_baseline,
    compare_baselines,
    write_capture,
)
from ops.deploy.production_baseline import (
    main as baseline_main,
)
from ops.deploy.repo_gate import validate_repo

GIT = os.environ.get("GIT_EXECUTABLE") or shutil.which("git") or "git"
REMOTE_URL = "https://github.com/jhbanalytics-pixel/42-Ogilvy-Intelligence.git"
FIXTURE_FINGERPRINT_A = "sha256:" + hashlib.sha256(b"fixture-session-a").hexdigest()
FIXTURE_FINGERPRINT_B = "sha256:" + hashlib.sha256(b"fixture-session-b").hexdigest()
UNSET = object()


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [GIT, "-C", str(root), *args], check=check, capture_output=True
    )


def cloned_repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", "-b", "main", str(seed))
    git(seed, "config", "user.name", "Inventory Test")
    git(seed, "config", "user.email", "inventory@example.invalid")
    (seed / "tracked.txt").write_text("original\n", encoding="utf-8")
    git(seed, "add", "tracked.txt")
    git(seed, "commit", "-m", "seed")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    git(tmp_path, "clone", str(remote), str(clone))
    return remote, clone


def test_missing_repositories_never_equal(tmp_path: Path) -> None:
    for name in ("missing-engine", "missing-app"):
        with pytest.raises(ValueError, match="repository_missing"):
            validate_repo(tmp_path / name, REMOTE_URL, "main", True)


def test_real_clone_is_accepted_and_reports_nonempty_heads(tmp_path: Path) -> None:
    remote, clone = cloned_repo(tmp_path)

    result = validate_repo(clone, str(remote), "main", True)

    assert result == {
        "head": git(clone, "rev-parse", "HEAD").stdout.decode("utf-8").strip(),
        "remote_head": git(clone, "rev-parse", "origin/main").stdout.decode("utf-8").strip(),
        "branch": "main",
        "status": "clean",
    }


def test_wrong_remote_is_rejected(tmp_path: Path) -> None:
    _, clone = cloned_repo(tmp_path)
    git(clone, "remote", "set-url", "origin", REMOTE_URL)

    with pytest.raises(ValueError, match="remote_mismatch"):
        validate_repo(clone, "https://github.com/example/other.git", "main", True)


def test_detached_head_is_rejected(tmp_path: Path) -> None:
    remote, clone = cloned_repo(tmp_path)
    git(clone, "checkout", "--detach")

    with pytest.raises(ValueError, match="detached_head"):
        validate_repo(clone, str(remote), "main", True)


def test_unborn_head_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "unborn"
    git(tmp_path, "init", "-b", "main", str(root))
    git(root, "remote", "add", "origin", str(tmp_path / "remote.git"))

    with pytest.raises(ValueError, match="invalid_head"):
        validate_repo(root, str(tmp_path / "remote.git"), "main", True)


def test_missing_upstream_is_rejected(tmp_path: Path) -> None:
    remote, clone = cloned_repo(tmp_path)
    git(clone, "branch", "--unset-upstream")

    with pytest.raises(ValueError, match="upstream_missing"):
        validate_repo(clone, str(remote), "main", True)


def test_wrong_branch_is_rejected(tmp_path: Path) -> None:
    remote, clone = cloned_repo(tmp_path)
    git(clone, "checkout", "-b", "other")

    with pytest.raises(ValueError, match="branch_mismatch"):
        validate_repo(clone, str(remote), "main", True)


def test_local_head_must_equal_the_remote_branch(tmp_path: Path) -> None:
    remote, clone = cloned_repo(tmp_path)
    git(clone, "config", "user.name", "Inventory Test")
    git(clone, "config", "user.email", "inventory@example.invalid")
    (clone / "tracked.txt").write_text("second\n", encoding="utf-8")
    git(clone, "commit", "-am", "local only")

    with pytest.raises(ValueError, match="head_mismatch"):
        validate_repo(clone, str(remote), "main", True)


@pytest.mark.parametrize("kind", ["tracked", "untracked"])
def test_dirty_and_untracked_files_are_rejected(tmp_path: Path, kind: str) -> None:
    remote, clone = cloned_repo(tmp_path)
    target = clone / ("tracked.txt" if kind == "tracked" else "untracked.txt")
    target.write_text("changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="repository_dirty"):
        validate_repo(clone, str(remote), "main", True)


def test_unmerged_entries_are_rejected_even_when_clean_is_not_required(tmp_path: Path) -> None:
    remote, clone = cloned_repo(tmp_path)
    git(clone, "config", "user.name", "Inventory Test")
    git(clone, "config", "user.email", "inventory@example.invalid")
    git(clone, "checkout", "-b", "side")
    (clone / "tracked.txt").write_text("side\n", encoding="utf-8")
    git(clone, "commit", "-am", "side")
    git(clone, "checkout", "main")
    (clone / "tracked.txt").write_text("main\n", encoding="utf-8")
    git(clone, "commit", "-am", "main")
    assert git(clone, "merge", "side", check=False).returncode != 0

    with pytest.raises(ValueError, match="repository_unmerged"):
        validate_repo(clone, str(remote), "main", False)


def test_git_command_failure_is_not_treated_as_empty_state(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("not a git directory\n", encoding="utf-8")

    with pytest.raises(ValueError, match="git_command_failed"):
        validate_repo(broken, REMOTE_URL, "main", True)


def inventory() -> dict:
    return {
        "project": "ogilvy-trends-v2",
        "regions": {
            "run": ["us-central1"],
            "scheduler": ["us-central1"],
            "tasks": ["us-central1"],
            "bigquery": ["US"],
        },
    }


class FakeNativeClient:
    def __init__(
        self,
        denied_suffix: str | None = None,
        repeated_page: bool = False,
        omit_table_schema: bool = False,
        principal: str | None = "reader@example.invalid",
        credential_fingerprint: str = FIXTURE_FINGERPRINT_A,
        current_session_fingerprint: str | None = None,
        malformed_services: bool = False,
        omit_iam_bindings: bool = False,
        omit_iam_bindings_with_etag: bool = False,
        omit_dataset_access: bool = False,
        omit_queue_settings: bool = False,
        omit_run_image: bool = False,
        secret_error: bool = False,
        empty_iam_without_etag: bool = False,
        evidence_extra: dict | None = None,
        iam_bindings_override=UNSET,
        schema_fields_override=UNSET,
    ):
        self.denied_suffix = denied_suffix
        self.repeated_page = repeated_page
        self.omit_table_schema = omit_table_schema
        self.principal = principal
        self.credential_fingerprint = credential_fingerprint
        self.current_session_fingerprint = (
            current_session_fingerprint or credential_fingerprint
        )
        self.malformed_services = malformed_services
        self.omit_iam_bindings = omit_iam_bindings
        self.omit_iam_bindings_with_etag = omit_iam_bindings_with_etag
        self.omit_dataset_access = omit_dataset_access
        self.omit_queue_settings = omit_queue_settings
        self.omit_run_image = omit_run_image
        self.secret_error = secret_error
        self.empty_iam_without_etag = empty_iam_without_etag
        self.evidence_extra = evidence_extra or {}
        self.iam_bindings_override = iam_bindings_override
        self.schema_fields_override = schema_fields_override
        self.calls: list[tuple[str, str, dict, dict | None]] = []

    def authenticated_principal_evidence(self) -> dict | None:
        if self.principal is None:
            return None
        material = {
            "principal": self.principal,
            "credential_fingerprint": self.credential_fingerprint,
            "acquisition": "trusted_test_fixture",
            "no_fallback": True,
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        return {
            **material,
            "evidence_digest": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
            **self.evidence_extra,
        }

    def session_fingerprint(self) -> str:
        return self.current_session_fingerprint

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        params = params or {}
        self.calls.append((method, url, params, json_body))
        if self.secret_error and url.endswith("/services"):
            error = RuntimeError(
                "Bearer FORBIDDEN_BEARER api_key=FORBIDDEN_API_KEY X-Passcode: FORBIDDEN_HEADER"
            )
            error.response = type("Response", (), {"status_code": 403})()
            raise error
        if self.denied_suffix and url.endswith(self.denied_suffix):
            raise PermissionError("403 denied")
        if url == "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/services":
            if not params.get("pageToken"):
                rows: list[object] = [
                    {
                        "name": "projects/ogilvy-trends-v2/locations/us-central1/services/prod-service"
                    }
                ]
                if self.malformed_services:
                    rows.extend(["bad-item", 7])
                return {
                    "services": rows,
                    "nextPageToken": "service-page-2",
                }
            return {
                "services": [{"name": "projects/ogilvy-trends-v2/locations/us-central1/services/listening-post-staging"}],
                "nextPageToken": "service-page-2" if self.repeated_page else "",
            }
        if url == "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/jobs":
            return {"jobs": [{"name": "projects/ogilvy-trends-v2/locations/us-central1/jobs/prod-job"}]}
        if url == "https://cloudscheduler.googleapis.com/v1/projects/ogilvy-trends-v2/locations/us-central1/jobs":
            return {"jobs": [{"name": "projects/ogilvy-trends-v2/locations/us-central1/jobs/daily"}]}
        if url == "https://cloudtasks.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/queues":
            return {"queues": [{"name": "projects/ogilvy-trends-v2/locations/us-central1/queues/questions"}]}
        if url == "https://bigquery.googleapis.com/bigquery/v2/projects/ogilvy-trends-v2/datasets":
            return {
                "datasets": [{
                    "datasetReference": {"projectId": "ogilvy-trends-v2", "datasetId": "prod"},
                    "location": "US",
                }]
            }
        if url == "https://bigquery.googleapis.com/bigquery/v2/projects/ogilvy-trends-v2/datasets/prod":
            payload = {
                "datasetReference": {"projectId": "ogilvy-trends-v2", "datasetId": "prod"},
                "location": "US",
                "access": [{"role": "READER", "specialGroup": "projectReaders"}],
            }
            if self.omit_dataset_access:
                payload.pop("access")
            return payload
        if url == "https://bigquery.googleapis.com/bigquery/v2/projects/ogilvy-trends-v2/datasets/prod/tables":
            if not params.get("pageToken"):
                return {
                    "tables": [{"tableReference": {"projectId": "ogilvy-trends-v2", "datasetId": "prod", "tableId": "events"}}],
                    "nextPageToken": "table-page-2",
                }
            return {
                "tables": [{"tableReference": {"projectId": "ogilvy-trends-v2", "datasetId": "prod", "tableId": "briefs"}}]
            }
        if "/datasets/prod/tables/" in url and not url.endswith(":getIamPolicy"):
            table = url.rsplit("/", 1)[-1]
            payload = {
                "tableReference": {"projectId": "ogilvy-trends-v2", "datasetId": "prod", "tableId": table},
                "schema": {"fields": [{"name": "id", "type": "STRING", "mode": "REQUIRED"}]},
                "timePartitioning": {"type": "DAY", "field": "trend_date"},
                "clustering": {"fields": ["market"]},
                "lastModifiedTime": "volatile",
            }
            if self.schema_fields_override is not UNSET:
                payload["schema"]["fields"] = deepcopy(self.schema_fields_override)
            if self.omit_table_schema:
                payload.pop("schema")
            return payload
        if url.endswith("/services/prod-service"):
            payload = {
                "name": "projects/ogilvy-trends-v2/locations/us-central1/services/prod-service",
                "template": {"containers": [{"image": "registry/prod@sha256:111", "env": [{"name": "API_TOKEN", "value": "literal-secret"}]}]},
                "updateTime": "volatile",
                "observedGeneration": "9",
                "conditions": [{"type": "Ready", "state": "CONDITION_SUCCEEDED"}],
            }
            if self.omit_run_image:
                payload["template"]["containers"][0].pop("image")
            return payload
        if url.endswith("/services/listening-post-staging"):
            return {
                "name": "projects/ogilvy-trends-v2/locations/us-central1/services/listening-post-staging",
                "template": {"containers": [{"image": "registry/staging@sha256:222", "env": [{"name": "API_TOKEN", "valueSource": {"secretKeyRef": {"secret": "api-token", "version": "3"}}}]}]},
            }
        if url.endswith("/jobs/prod-job"):
            return {
                "name": "projects/ogilvy-trends-v2/locations/us-central1/jobs/prod-job",
                "template": {"template": {"containers": [{"image": "registry/job@sha256:333"}]}},
            }
        if url.endswith("/jobs/daily"):
            return {
                "name": "projects/ogilvy-trends-v2/locations/us-central1/jobs/daily",
                "schedule": "0 6 * * *",
                "state": "ENABLED",
                "httpTarget": {
                    "uri": "https://example.invalid/run",
                    "headers": {"X-Passcode": "unusual-secret"},
                    "body": "c2VjcmV0",
                },
            }
        if url.endswith("/queues/questions"):
            payload = {
                "name": "projects/ogilvy-trends-v2/locations/us-central1/queues/questions",
                "state": "PAUSED",
                "rateLimits": {"maxConcurrentDispatches": 5},
                "retryConfig": {"maxAttempts": 5},
            }
            if self.omit_queue_settings:
                payload.pop("rateLimits")
            return payload
        if url.endswith(":getIamPolicy"):
            if "cloudscheduler.googleapis.com" in url:
                raise AssertionError("Cloud Scheduler has no job IAM method")
            if "cloudtasks.googleapis.com" in url and method != "POST":
                raise AssertionError("Cloud Tasks queue IAM requires POST")
            payload = {
                "version": 3,
                "bindings": [
                    {
                        "role": "roles/viewer",
                        "members": ["group:readers@example.invalid"],
                    }
                ],
            }
            if self.omit_iam_bindings:
                payload.pop("bindings")
            if self.omit_iam_bindings_with_etag:
                payload.pop("bindings")
                payload["etag"] = "anchored-policy-etag"
            if self.empty_iam_without_etag:
                payload["bindings"] = []
            if self.iam_bindings_override is not UNSET:
                payload["bindings"] = deepcopy(self.iam_bindings_override)
            return payload
        raise AssertionError(f"unexpected native read: {method} {url} {params} {json_body}")


class EmptyNativeClient(FakeNativeClient):
    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        self.calls.append((method, url, params or {}, json_body))
        return {}


class PaginatedQueuesClient(FakeNativeClient):
    def __init__(self, *, first_page_missing: bool = False, terminal_error: bool = False):
        super().__init__()
        self.first_page_missing = first_page_missing
        self.terminal_error = terminal_error

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        params = params or {}
        queue_list_url = (
            "https://cloudtasks.googleapis.com/v2/projects/ogilvy-trends-v2/"
            "locations/us-central1/queues"
        )
        if url != queue_list_url:
            return super().request_json(
                method,
                url,
                params=params,
                json_body=json_body,
            )
        self.calls.append((method, url, params, json_body))
        if not params.get("pageToken"):
            if self.first_page_missing:
                return {}
            return {
                "queues": [
                    {
                        "name": (
                            "projects/ogilvy-trends-v2/locations/us-central1/"
                            "queues/questions"
                        )
                    }
                ],
                "nextPageToken": "retained-next-page",
            }
        assert params == {"pageToken": "retained-next-page"}
        if self.terminal_error:
            raise TimeoutError("retained terminal timeout")
        return {}


def capture(client: FakeNativeClient | None = None) -> dict:
    return capture_baseline(
        inventory(),
        client or FakeNativeClient(),
        identity="reader@example.invalid",
        private_hash_key=b"private-test-key",
        observed_at="2026-09-12T10:00:00+00:00",
    )


def test_capture_enumerates_all_all_declared_resources_and_pages() -> None:
    client = FakeNativeClient()
    result = capture(client)

    assert result["complete"] is True
    assert [row["name"] for row in result["resources"]["services"]] == [
        "projects/ogilvy-trends-v2/locations/us-central1/services/listening-post-staging",
        "projects/ogilvy-trends-v2/locations/us-central1/services/prod-service",
    ]
    assert [row["name"] for row in result["resources"]["tables"]] == [
        "projects/ogilvy-trends-v2/datasets/prod/tables/briefs",
        "projects/ogilvy-trends-v2/datasets/prod/tables/events",
    ]
    prod_service = next(
        row for row in result["resources"]["services"] if row["name"].endswith("prod-service")
    )
    assert "observedGeneration" in prod_service["field_coverage"]
    assert prod_service["status_observed"] == {
        "observedGeneration": "9",
        "ready": {"type": "Ready", "state": "CONDITION_SUCCEEDED"},
    }
    assert result["coverage"] == {
        "jobs": {"regions": ["us-central1"], "count": 1},
        "services": {"regions": ["us-central1"], "count": 2},
        "schedulers": {"regions": ["us-central1"], "count": 1},
        "queues": {"regions": ["us-central1"], "count": 1},
        "datasets": {"regions": ["US"], "count": 1},
        "tables": {"regions": ["US"], "count": 2},
        "iam": {"count": 7},
    }
    assert result["iam_capabilities"] == {
        "project": "resource_manager_policy",
        "jobs": "cloud_run_per_resource_policy",
        "services": "cloud_run_per_resource_policy",
        "schedulers": "project_policy_only_provider_has_no_job_iam_method",
        "queues": "cloud_tasks_per_resource_policy",
        "datasets": "full_dataset_access_metadata",
        "tables": "bigquery_per_resource_policy",
    }
    dataset_list_call = next(
        call
        for call in client.calls
        if call[1].endswith("/datasets")
    )
    assert dataset_list_call[2]["all"] == "true"
    iam_calls = [call for call in client.calls if call[1].endswith(":getIamPolicy")]
    assert all(
        (
            call[3] == {"options": {"requestedPolicyVersion": 3}}
            if call[0] == "POST"
            else call[2] == {"options.requestedPolicyVersion": "3"}
        )
        for call in iam_calls
    )


def test_repeated_pagination_token_marks_capture_partial() -> None:
    result = capture(FakeNativeClient(repeated_page=True))

    assert result["complete"] is False
    assert any(failure["code"] == "pagination_cycle" for failure in result["failures"])


def test_missing_collection_fields_cannot_certify_empty_baseline() -> None:
    result = capture(EmptyNativeClient())

    assert result["complete"] is False
    assert sum(
        failure["code"] == "collection_missing" for failure in result["failures"]
    ) == 5
    assert result["coverage"]["iam"]["count"] == 0


def test_cloud_tasks_empty_terminal_page_retains_queue_and_records_normalization() -> None:
    client = PaginatedQueuesClient()

    result = capture(client)

    queue_list_calls = [
        call
        for call in client.calls
        if call[1].endswith("/locations/us-central1/queues")
    ]
    assert result["complete"] is True
    assert [row["name"] for row in result["resources"]["queues"]] == [
        "projects/ogilvy-trends-v2/locations/us-central1/queues/questions"
    ]
    assert result["transport_normalizations"] == [
        {
            "operation": "list_queues",
            "resource": (
                "https://cloudtasks.googleapis.com/v2/projects/ogilvy-trends-v2/"
                "locations/us-central1/queues"
            ),
            "collection": "queues",
            "normalization": "omitted_empty_terminal_collection",
            "prior_page_token_digest": (
                "sha256:6b907c6e1cca5e0075ee1f85845f6c6ac5c31f37ff9bf32e6"
                "155be61332bf5b8"
            ),
            "original_response_digest": (
                "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310"
                "c060f61caaff8a"
            ),
        }
    ]
    assert [call[2] for call in queue_list_calls] == [
        {},
        {"pageToken": "retained-next-page"},
    ]


def test_cloud_tasks_missing_first_page_collection_remains_failure() -> None:
    result = capture(PaginatedQueuesClient(first_page_missing=True))

    assert result["complete"] is False
    assert "transport_normalizations" not in result
    assert any(
        failure["operation"] == "list_queues"
        and failure["code"] == "collection_missing"
        for failure in result["failures"]
    )


def test_cloud_tasks_terminal_page_failure_remains_failure() -> None:
    result = capture(PaginatedQueuesClient(terminal_error=True))

    assert result["complete"] is False
    assert "transport_normalizations" not in result
    assert any(
        failure["operation"] == "list_queues"
        and failure["code"] == "timeout"
        for failure in result["failures"]
    )


def test_every_malformed_collection_item_is_recorded() -> None:
    result = capture(FakeNativeClient(malformed_services=True))

    assert result["complete"] is False
    malformed = [
        failure
        for failure in result["failures"]
        if failure["code"] == "collection_item_invalid"
    ]
    assert len(malformed) == 2
    assert {failure["item_index"] for failure in malformed} == {1, 2}


@pytest.mark.parametrize(
    ("client", "operation", "field"),
    [
        (FakeNativeClient(omit_run_image=True), "services", "image"),
        (FakeNativeClient(omit_queue_settings=True), "queues", "rateLimits"),
        (FakeNativeClient(omit_dataset_access=True), "datasets", "access"),
        (FakeNativeClient(omit_table_schema=True), "tables", "schema"),
        (FakeNativeClient(omit_iam_bindings=True), "iam", "bindings"),
    ],
)
def test_missing_required_nested_metadata_marks_capture_partial(
    client: FakeNativeClient, operation: str, field: str
) -> None:
    result = capture(client)

    assert result["complete"] is False
    assert any(
        failure["operation"] == operation
        and failure["code"] == "required_field_missing"
        and failure["field"] == field
        for failure in result["failures"]
    )


def test_etag_anchored_omitted_iam_bindings_are_explicitly_normalized() -> None:
    result = capture(FakeNativeClient(omit_iam_bindings_with_etag=True))
    raw_policy = {"version": 3, "etag": "anchored-policy-etag"}
    raw_digest = "sha256:" + hashlib.sha256(
        json.dumps(raw_policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    assert result["complete"] is True
    assert all(row["observed"]["bindings"] == [] for row in result["resources"]["iam"])
    for row in result["resources"]["iam"]:
        normalization = row["transport_normalizations"]
        assert len(normalization) == 1
        assert normalization[0]["field"] == "bindings"
        assert normalization[0]["method"] in {"GET", "POST"}
        assert normalization[0]["resource"] == row["name"]
        assert normalization[0]["reason"] == "provider_omitted_empty_repeated_field"
        assert normalization[0]["raw_response_digest"] == raw_digest


def test_unanchored_empty_iam_policy_marks_capture_partial() -> None:
    result = capture(FakeNativeClient(empty_iam_without_etag=True))

    assert result["complete"] is False
    assert any(
        failure["operation"] == "iam"
        and failure["code"] == "policy_anchor_missing"
        for failure in result["failures"]
    )


@pytest.mark.parametrize(
    ("bindings", "field_path"),
    [
        (["not-an-object"], "bindings[0]"),
        ([None], "bindings[0]"),
        ([["not-an-object"]], "bindings[0]"),
        ([{"role": "roles/viewer", "members": "not-a-list"}], "bindings[0].members"),
        ([{"role": "roles/viewer", "members": [None]}], "bindings[0].members[0]"),
        ([{"role": "", "members": []}], "bindings[0].role"),
        (
            [
                {
                    "role": "roles/viewer",
                    "members": ["group:readers@example.invalid"],
                    "condition": "not-an-object",
                }
            ],
            "bindings[0].condition",
        ),
        (
            [
                {
                    "role": "roles/viewer",
                    "members": ["group:readers@example.invalid"],
                    "condition": {"title": "bounded"},
                }
            ],
            "bindings[0].condition.expression",
        ),
        (
            [
                {
                    "role": "roles/viewer",
                    "members": ["group:readers@example.invalid"],
                    "condition": {"expression": "request.time < timestamp('2030-01-01T00:00:00Z')", "title": 7},
                }
            ],
            "bindings[0].condition.title",
        ),
    ],
)
def test_malformed_iam_binding_children_mark_capture_partial(
    bindings, field_path: str
) -> None:
    result = capture(FakeNativeClient(iam_bindings_override=bindings))

    assert result["complete"] is False
    assert any(
        failure["operation"] == "iam"
        and failure["code"] == "field_malformed"
        and failure["field"] == field_path
        for failure in result["failures"]
    )


def test_valid_conditional_iam_binding_is_preserved() -> None:
    condition = {
        "title": "bounded read",
        "description": "Expires after the approved window",
        "expression": "request.time < timestamp('2030-01-01T00:00:00Z')",
        "location": "policy.json:12",
    }
    bindings = [
        {
            "role": "roles/viewer",
            "members": ["group:readers@example.invalid"],
            "condition": condition,
        }
    ]

    result = capture(FakeNativeClient(iam_bindings_override=bindings))

    assert result["complete"] is True
    assert all(
        row["observed"]["bindings"][0]["condition"] == condition
        for row in result["resources"]["iam"]
    )


@pytest.mark.parametrize(
    ("fields", "field_path"),
    [
        (["not-an-object"], "schema.fields[0]"),
        ([None], "schema.fields[0]"),
        ([{"type": "STRING"}], "schema.fields[0].name"),
        ([{"name": "id"}], "schema.fields[0].type"),
        ([{"name": "id", "type": "STRING", "mode": 7}], "schema.fields[0].mode"),
        (
            [{"name": "root", "type": "RECORD", "fields": "not-a-list"}],
            "schema.fields[0].fields",
        ),
        (
            [
                {
                    "name": "root",
                    "type": "RECORD",
                    "fields": [
                        {
                            "name": "child",
                            "type": "RECORD",
                            "fields": [{"name": "", "type": "STRING"}],
                        }
                    ],
                }
            ],
            "schema.fields[0].fields[0].fields[0].name",
        ),
    ],
)
def test_malformed_recursive_schema_children_mark_capture_partial(
    fields, field_path: str
) -> None:
    result = capture(FakeNativeClient(schema_fields_override=fields))

    assert result["complete"] is False
    assert any(
        failure["operation"] == "tables"
        and failure["code"] == "field_malformed"
        and failure["field"] == field_path
        for failure in result["failures"]
    )


def test_valid_nested_record_schema_is_preserved() -> None:
    fields = [
        {
            "name": "root",
            "type": "RECORD",
            "mode": "NULLABLE",
            "description": "Nested source identity",
            "fields": [
                {
                    "name": "child",
                    "type": "STRING",
                    "mode": "REPEATED",
                    "policyTags": {"names": ["projects/example/locations/us/taxonomies/1/policyTags/2"]},
                }
            ],
        }
    ]

    result = capture(FakeNativeClient(schema_fields_override=fields))

    assert result["complete"] is True
    assert all(
        row["observed"]["schema"]["fields"] == fields
        for row in result["resources"]["tables"]
    )


def test_denied_iam_read_is_recorded_and_marks_capture_partial() -> None:
    result = capture(FakeNativeClient(denied_suffix="services/prod-service:getIamPolicy"))

    assert result["complete"] is False
    assert any(
        failure["operation"] == "iam"
        and failure["reason_code"] == "permission_denied"
        and failure["exception_category"] == "permission"
        for failure in result["failures"]
    )


def test_denied_table_iam_read_is_recorded_and_marks_capture_partial() -> None:
    result = capture(FakeNativeClient(denied_suffix="tables/events:getIamPolicy"))

    assert result["complete"] is False
    assert any(
        failure["operation"] == "iam"
        and failure["resource"].endswith("tables/events")
        and failure["reason_code"] == "permission_denied"
        for failure in result["failures"]
    )


def test_missing_table_schema_is_recorded_and_marks_capture_partial() -> None:
    result = capture(FakeNativeClient(omit_table_schema=True))

    assert result["complete"] is False
    assert any(
        failure["operation"] == "tables"
        and failure["code"] == "required_field_missing"
        and failure["field"] == "schema"
        for failure in result["failures"]
    )


@pytest.mark.parametrize(
    "client",
    [
        FakeNativeClient(principal=None),
        FakeNativeClient(principal="other@example.invalid"),
        FakeNativeClient(current_session_fingerprint=FIXTURE_FINGERPRINT_B),
        FakeNativeClient(credential_fingerprint="Bearer FORBIDDEN_EVIDENCE"),
    ],
)
def test_unbound_identity_or_changed_session_refuses_before_native_read(
    client: FakeNativeClient,
) -> None:
    with pytest.raises(
        ValueError,
        match=r"principal_evidence|principal_mismatch|session_binding_mismatch",
    ):
        capture(client)
    assert client.calls == []


def test_capture_binds_authenticated_principal_and_session_evidence() -> None:
    result = capture(FakeNativeClient(evidence_extra={"token": "FORBIDDEN_EVIDENCE"}))

    assert result["identity_evidence"] == {
        "principal": "reader@example.invalid",
        "credential_fingerprint": FIXTURE_FINGERPRINT_A,
        "acquisition": "trusted_test_fixture",
        "no_fallback": True,
        "evidence_digest": result["identity_evidence"]["evidence_digest"],
    }
    assert result["identity_evidence"]["evidence_digest"].startswith("sha256:")
    assert "FORBIDDEN_EVIDENCE" not in json.dumps(result)


def test_exception_diagnostics_cannot_enter_receipt_or_cli_stdout(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    client = FakeNativeClient(secret_error=True)
    result = capture(client)
    encoded = json.dumps(result, sort_keys=True)

    assert result["complete"] is False
    assert "FORBIDDEN_BEARER" not in encoded
    assert "FORBIDDEN_API_KEY" not in encoded
    assert "FORBIDDEN_HEADER" not in encoded
    failure = next(row for row in result["failures"] if row["operation"] == "list_services")
    assert failure["reason_code"] == "permission_denied"
    assert failure["exception_category"] == "http"
    assert failure["http_status"] == 403

    inventory_path = tmp_path / "inventory.json"
    output = tmp_path / "partial.json"
    inventory_path.write_text(json.dumps(inventory()), encoding="utf-8")
    monkeypatch.setenv("R00_BASELINE_HASH_KEY", "private-test-key")
    assert baseline_main(
        [
            "capture",
            "--inventory",
            str(inventory_path),
            "--output",
            str(output),
            "--identity",
            "reader@example.invalid",
        ],
        client_factory=lambda identity: FakeNativeClient(secret_error=True),
    ) == 2
    stdout = capsys.readouterr().out
    assert "FORBIDDEN" not in stdout
    assert "FORBIDDEN" not in output.read_text(encoding="utf-8")


@pytest.mark.parametrize("missing", ["run", "scheduler", "tasks", "bigquery"])
def test_missing_required_inventory_coverage_is_rejected(missing: str) -> None:
    invalid = inventory()
    invalid["regions"].pop(missing)

    with pytest.raises(ValueError, match=f"inventory_missing:{missing}"):
        capture_baseline(
            invalid,
            FakeNativeClient(),
            identity="reader@example.invalid",
            private_hash_key=b"private-test-key",
            observed_at="2026-09-12T10:00:00+00:00",
        )


def test_literal_secrets_are_privately_hashed_and_secret_references_survive() -> None:
    result = capture()
    encoded = json.dumps(result, sort_keys=True)
    expected = hmac.new(b"private-test-key", b"literal-secret", hashlib.sha256).hexdigest()
    header_hash = hmac.new(b"private-test-key", b"unusual-secret", hashlib.sha256).hexdigest()
    body_hash = hmac.new(b"private-test-key", b"c2VjcmV0", hashlib.sha256).hexdigest()

    assert "literal-secret" not in encoded
    assert "unusual-secret" not in encoded
    assert "c2VjcmV0" not in encoded
    assert f"hmac-sha256:{expected}" in encoded
    assert f"hmac-sha256:{header_hash}" in encoded
    assert f"hmac-sha256:{body_hash}" in encoded
    assert '"X-Passcode"' in encoded
    assert '"secret": "api-token"' in encoded
    assert '"version": "3"' in encoded


def test_existing_output_is_rejected_without_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "baseline.json"
    output.write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError, match="output_exists"):
        write_capture(output, capture())
    assert output.read_text(encoding="utf-8") == "existing"


def test_comparator_reports_altered_image_but_ignores_volatile_times() -> None:
    before = capture()
    before["resources"]["tables"][0]["observed"]["numRows"] = "10"
    after = deepcopy(before)
    after["observed_at"] = "2026-09-13T10:00:00+00:00"
    after["resources"]["services"][0]["observed"]["updateTime"] = "later"
    after["resources"]["services"][0]["observed"]["template"]["containers"][0]["image"] = "registry/staging@sha256:changed"
    after["resources"]["tables"][0]["observed"]["numRows"] = "99"

    assert compare_baselines(before, after) == [{
        "category": "services",
        "name": "projects/ogilvy-trends-v2/locations/us-central1/services/listening-post-staging",
        "change": "protected_configuration_changed",
    }]


def test_comparator_refuses_partial_capture() -> None:
    partial = capture(FakeNativeClient(denied_suffix="services/prod-service:getIamPolicy"))

    with pytest.raises(ValueError, match="baseline_incomplete"):
        compare_baselines(capture(), partial)


def test_capture_cli_uses_injected_native_client_and_writes_new_output(tmp_path: Path, monkeypatch) -> None:
    inventory_path = tmp_path / "inventory.json"
    output = tmp_path / "baseline.json"
    inventory_path.write_text(json.dumps(inventory()), encoding="utf-8")
    monkeypatch.setenv("R00_BASELINE_HASH_KEY", "private-test-key")

    exit_code = baseline_main(
        ["capture", "--inventory", str(inventory_path), "--output", str(output), "--identity", "reader@example.invalid"],
        client_factory=lambda identity: FakeNativeClient(),
    )

    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["complete"] is True
