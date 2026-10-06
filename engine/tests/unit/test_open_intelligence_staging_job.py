"""Boundary tests for the immutable Open Intelligence staging job definition."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from copy import deepcopy
from pathlib import Path
from types import ModuleType

import pytest

PROJECT = "ogilvy-trends-v2"
PROJECT_NUMBER = "590353929363"
REGION = "us-central1"
JOB = "trends-engine-open-intelligence-staging"
SERVICE_ACCOUNT = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
IMAGE = "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:" + "a" * 64
SOURCE_SHA = "b" * 40
GCLOUD = r"C:\CloudSDK\bin\gcloud.cmd"
ENV = {
    "GCP_PROJECT": PROJECT,
    "TRENDS_ENV": "staging",
    "BIGQUERY_DATASET": "trends_v2",
    "GEMINI_MODEL": "gemini-3.5-flash",
    "GEMINI_LOCATION": "global",
    "MAILER_V2_ENABLED": "false",
    "RECONCILE_ENABLED": "false",
}
ENV_ARG = ",".join(f"{key}={value}" for key, value in ENV.items())
LABEL_ARG = f"environment=staging,source-sha={SOURCE_SHA}"
FORBIDDEN_ANNOTATION_KEYS = (
    "run.googleapis.com/secrets",
    "run.googleapis.com/cloudsql-instances",
    "run.googleapis.com/vpc-access-connector",
    "run.googleapis.com/vpc-access-egress",
    "run.googleapis.com/network-interfaces",
)
MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "staging" / "deploy_open_intelligence_job.py"
)
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def deployer() -> ModuleType:
    assert MODULE_PATH.exists(), "production deployment definition is missing"
    spec = importlib.util.spec_from_file_location("staging_job_under_test", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_deployment_cli_starts_from_the_documented_repository_command() -> None:
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--image-digest" in result.stdout
    assert "--source-sha" in result.stdout


def describe_command(executable: str = GCLOUD) -> list[str]:
    return [
        executable,
        "run",
        "jobs",
        "describe",
        JOB,
        f"--project={PROJECT}",
        f"--region={REGION}",
        "--quiet",
        "--format=json",
    ]


def create_command(executable: str = GCLOUD) -> list[str]:
    return [
        executable,
        "run",
        "jobs",
        "create",
        JOB,
        f"--project={PROJECT}",
        f"--region={REGION}",
        f"--image={IMAGE}",
        f"--service-account={SERVICE_ACCOUNT}",
        "--command=python",
        "--args=scripts/run_rss_now.py",
        "--task-timeout=10800s",
        "--tasks=1",
        "--parallelism=1",
        "--max-retries=0",
        "--cpu=2",
        "--memory=8Gi",
        f"--set-env-vars={ENV_ARG}",
        f"--labels={LABEL_ARG}",
        "--quiet",
        "--format=none",
    ]


def update_command(executable: str = GCLOUD) -> list[str]:
    return [
        executable,
        "run",
        "jobs",
        "update",
        JOB,
        f"--project={PROJECT}",
        f"--region={REGION}",
        f"--image={IMAGE}",
        f"--service-account={SERVICE_ACCOUNT}",
        "--command=python",
        "--args=scripts/run_rss_now.py",
        "--task-timeout=10800s",
        "--tasks=1",
        "--parallelism=1",
        "--max-retries=0",
        "--cpu=2",
        "--memory=8Gi",
        f"--set-env-vars={ENV_ARG}",
        "--clear-secrets",
        "--clear-volume-mounts",
        "--clear-volumes",
        "--clear-cloudsql-instances",
        "--vpc-egress=private-ranges-only",
        "--clear-vpc-connector",
        "--clear-network",
        f"--update-labels={LABEL_ARG}",
        "--quiet",
        "--format=none",
    ]


def desired_resource() -> dict[str, object]:
    return {
        "apiVersion": "run.googleapis.com/v1",
        "kind": "Job",
        "metadata": {
            "name": JOB,
            "namespace": PROJECT_NUMBER,
            "selfLink": (f"/apis/run.googleapis.com/v1/namespaces/{PROJECT_NUMBER}/jobs/{JOB}"),
            "labels": {
                "cloud.googleapis.com/location": REGION,
                "environment": "staging",
                "source-sha": SOURCE_SHA,
            },
        },
        "spec": {
            "template": {
                "metadata": {
                    "labels": {
                        "environment": "staging",
                        "source-sha": SOURCE_SHA,
                    },
                    "annotations": {},
                },
                "spec": {
                    "taskCount": 1,
                    "parallelism": 1,
                    "template": {
                        "spec": {
                            "serviceAccountName": SERVICE_ACCOUNT,
                            "maxRetries": 0,
                            "timeoutSeconds": "10800",
                            "containers": [
                                {
                                    "image": IMAGE,
                                    "command": ["python"],
                                    "args": ["scripts/run_rss_now.py"],
                                    "env": [
                                        {"name": key, "value": value} for key, value in ENV.items()
                                    ],
                                    "resources": {"limits": {"cpu": "2", "memory": "8Gi"}},
                                }
                            ],
                        }
                    },
                },
            }
        },
    }


class StatefulJobRunner:
    """Local command fake with persistent nested Cloud Run job state."""

    def __init__(
        self,
        resource: dict[str, object] | None,
        *,
        head: str = SOURCE_SHA,
        dirty: bool = False,
        describe_error: str | None = None,
        describe_error_once: str | None = None,
        malformed_describe: bool = False,
        failure_mode: str | None = None,
        readback_mutation: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.resource = deepcopy(resource)
        self.head = head
        self.dirty = dirty
        self.describe_error = describe_error
        self.describe_error_once = describe_error_once
        self.malformed_describe = malformed_describe
        self.failure_mode = failure_mode
        self.readback_mutation = readback_mutation
        self.commands: list[list[str]] = []
        self.mutations: list[str] = []
        self.describe_count = 0

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        assert isinstance(command, list), "every subprocess boundary uses list args"
        self.commands.append(list(command))
        if command == ["git", "status", "--porcelain", "--untracked-files=no"]:
            return self._result(stdout=" M tracked.py\n" if self.dirty else "")
        if command == ["git", "rev-parse", "HEAD"]:
            return self._result(stdout=f"{self.head}\n")
        assert command[0] == GCLOUD
        args = list(command[1:])
        if args[:3] == ["run", "jobs", "describe"]:
            assert list(command) == describe_command()
            self.describe_count += 1
            if self.describe_count == 1 and self.describe_error_once is not None:
                return self._result(returncode=1, stderr=self.describe_error_once)
            if self.describe_error is not None:
                return self._result(returncode=1, stderr=self.describe_error)
            if self.malformed_describe:
                return self._result(stdout="{not-json")
            if self.resource is None:
                return self._result(
                    returncode=1,
                    stderr=(
                        f"ERROR: (gcloud.run.jobs.describe) NOT_FOUND: Job '{JOB}' was not found.\n"
                    ),
                )
            resource = deepcopy(self.resource)
            if self.describe_count > 1 and self.readback_mutation is not None:
                self.readback_mutation(resource)
            return self._result(stdout=json.dumps(resource))
        if args[:3] == ["run", "jobs", "create"]:
            assert list(command) == create_command()
            return self._mutate("create")
        if args[:3] == ["run", "jobs", "update"]:
            assert list(command) == update_command()
            return self._mutate("update")
        raise AssertionError(f"unexpected command: {command!r}")

    def _mutate(self, kind: str) -> subprocess.CompletedProcess[str]:
        self.mutations.append(kind)
        if self.failure_mode == "no_effect_error":
            return self._result(returncode=1, stderr="mutation failed")
        if self.failure_mode == "partial_then_error":
            self._apply_desired(kind)
            self.resource["spec"]["template"]["spec"]["template"]["spec"]["containers"][0][
                "resources"
            ]["limits"]["memory"] = "4Gi"
            return self._result(returncode=1, stderr="mutation result lost")
        self._apply_desired(kind)
        if self.failure_mode == "apply_then_error":
            return self._result(returncode=1, stderr="mutation result lost")
        return self._result()

    def _apply_desired(self, kind: str) -> None:
        annotations: dict[str, str] = {}
        if self.resource is not None:
            annotations = deepcopy(
                self.resource["spec"]["template"]["metadata"].get("annotations", {})
            )
        self.resource = desired_resource()
        if kind != "update":
            return
        for key in (
            "run.googleapis.com/secrets",
            "run.googleapis.com/cloudsql-instances",
            "run.googleapis.com/vpc-access-connector",
            "run.googleapis.com/vpc-access-egress",
            "run.googleapis.com/network-interfaces",
        ):
            annotations.pop(key, None)
        self.resource["spec"]["template"]["metadata"]["annotations"] = annotations

    @staticmethod
    def _result(
        *, returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], returncode, stdout, stderr)


class Resolver:
    def __init__(self, value: str | None = GCLOUD) -> None:
        self.value = value
        self.calls: list[str] = []

    def __call__(self, candidate: str) -> str | None:
        self.calls.append(candidate)
        return self.value


def test_dry_run_prints_exact_action_model_without_resolving_or_running(
    deployer: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("dry run crossed an executable boundary")

    assert (
        deployer.main(
            ["--image-digest", IMAGE, "--source-sha", SOURCE_SHA],
            runner=forbidden,
            resolver=forbidden,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "mode": "dry-run",
        "desired_snapshot": {
            "project": PROJECT,
            "region": REGION,
            "job": JOB,
            "service_account": SERVICE_ACCOUNT,
            "image": IMAGE,
            "source_sha": SOURCE_SHA,
            "command": ["python"],
            "args": ["scripts/run_rss_now.py"],
            "timeout_seconds": 10800,
            "tasks": 1,
            "parallelism": 1,
            "max_retries": 0,
            "cpu": "2",
            "memory": "8Gi",
            "environment": ENV,
            "labels": {"environment": "staging", "source-sha": SOURCE_SHA},
        },
        "actions": {
            "describe": describe_command("gcloud"),
            "create_if_missing": create_command("gcloud"),
            "update_if_present": update_command("gcloud"),
            "readback": describe_command("gcloud"),
        },
        "readback_checks": [
            "identity",
            "location",
            "service_account",
            "image_digest",
            "entrypoint",
            "task_policy",
            "resources",
            "exact_environment",
            "no_secrets_or_volumes",
            "no_cloudsql_or_vpc",
            "resource_labels",
            "template_labels",
        ],
        "rollback_plan": (
            "Deletion or previous immutable image restoration requires separate "
            "authorization. This script has no rollback execution path."
        ),
    }


@pytest.mark.parametrize(
    ("image", "sha", "message"),
    [
        (
            "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine:latest",
            SOURCE_SHA,
            "immutable image digest",
        ),
        (
            "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:" + "A" * 64,
            SOURCE_SHA,
            "immutable image digest",
        ),
        (IMAGE, "b" * 7, "full lowercase source SHA"),
        (IMAGE, "B" * 40, "full lowercase source SHA"),
    ],
)
def test_invalid_immutable_inputs_fail_before_any_dependency(
    deployer: ModuleType, image: str, sha: str, message: str
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("invalid input crossed a dependency boundary")

    with pytest.raises(deployer.DeploymentError, match=message):
        deployer.apply_job(image, sha, runner=forbidden, resolver=forbidden)


def test_missing_job_creates_then_second_apply_is_idempotent(
    deployer: ModuleType,
) -> None:
    runner = StatefulJobRunner(None)
    resolver = Resolver()
    first = deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=resolver)
    assert first == "created"
    assert runner.mutations == ["create"]
    assert runner.commands == [
        ["git", "status", "--porcelain", "--untracked-files=no"],
        ["git", "rev-parse", "HEAD"],
        describe_command(),
        create_command(),
        describe_command(),
    ]

    runner.commands.clear()
    second = deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=resolver)
    assert second == "unchanged"
    assert runner.mutations == ["create"]
    assert runner.commands == [
        ["git", "status", "--porcelain", "--untracked-files=no"],
        ["git", "rev-parse", "HEAD"],
        describe_command(),
    ]


def test_wrong_existing_job_updates_then_second_apply_is_idempotent(
    deployer: ModuleType,
) -> None:
    wrong = desired_resource()
    wrong["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["image"] = IMAGE.replace(
        "a" * 64, "c" * 64
    )
    runner = StatefulJobRunner(wrong)
    assert deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver()) == "updated"
    assert runner.mutations == ["update"]
    assert runner.commands[-2:] == [update_command(), describe_command()]

    runner.commands.clear()
    assert deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver()) == "unchanged"
    assert runner.mutations == ["update"]
    assert runner.commands[-1] == describe_command()


@pytest.mark.parametrize(
    "annotations",
    [
        {
            "run.googleapis.com/vpc-access-connector": "projects/p/locations/r/connectors/c",
            "run.googleapis.com/vpc-access-egress": "all-traffic",
        },
        {
            "run.googleapis.com/network-interfaces": (
                '[{"network":"projects/p/global/networks/n",'
                '"subnetwork":"projects/p/regions/r/subnetworks/s"}]'
            ),
            "run.googleapis.com/vpc-access-egress": "private-ranges-only",
        },
        {
            "run.googleapis.com/vpc-access-connector": "projects/p/locations/r/connectors/c",
            "run.googleapis.com/network-interfaces": '[{"network":"default"}]',
            "run.googleapis.com/vpc-access-egress": "all-traffic",
        },
    ],
    ids=["connector", "direct_vpc", "both"],
)
def test_update_clears_connector_direct_network_and_egress_then_skips(
    deployer: ModuleType, annotations: dict[str, str]
) -> None:
    resource = desired_resource()
    resource["spec"]["template"]["metadata"]["annotations"] = annotations
    runner = StatefulJobRunner(resource)

    assert deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver()) == "updated"
    assert runner.mutations == ["update"]
    assert runner.resource["spec"]["template"]["metadata"]["annotations"] == {}
    assert "--vpc-egress=private-ranges-only" in runner.commands[-2]
    assert "--clear-vpc-connector" in runner.commands[-2]
    assert "--clear-network" in runner.commands[-2]

    runner.commands.clear()
    assert deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver()) == "unchanged"
    assert runner.mutations == ["update"]
    assert runner.commands[-1] == describe_command()


def test_exact_existing_job_skips_mutation(deployer: ModuleType) -> None:
    runner = StatefulJobRunner(desired_resource())
    assert deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver()) == "unchanged"
    assert runner.mutations == []
    assert runner.describe_count == 1


@pytest.mark.parametrize(
    ("runner_kwargs", "message"),
    [
        ({"malformed_describe": True}, "malformed JSON"),
        ({"describe_error": "permission denied"}, "describe failed"),
        (
            {
                "describe_error": (
                    "ERROR: (gcloud.run.jobs.describe) NOT_FOUND: some other job was not found"
                )
            },
            "describe failed",
        ),
        (
            {
                "describe_error": (
                    f"ERROR: (gcloud.run.jobs.describe) NOT_FOUND: Job '{JOB}-copy' was not found"
                )
            },
            "describe failed",
        ),
    ],
)
def test_discovery_failure_never_mutates(
    deployer: ModuleType, runner_kwargs: dict[str, object], message: str
) -> None:
    runner = StatefulJobRunner(desired_resource(), **runner_kwargs)
    with pytest.raises(deployer.DeploymentError, match=message):
        deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver())
    assert runner.mutations == []


def test_observed_cannot_find_job_line_is_the_exact_missing_job_signal(
    deployer: ModuleType,
) -> None:
    result = subprocess.CompletedProcess(
        [],
        1,
        "",
        "ERROR: (gcloud.run.jobs.describe) Cannot find job "
        "[trends-engine-open-intelligence-staging].\n",
    )
    assert deployer._is_exact_not_found(result) is True


def test_existing_not_found_line_remains_an_exact_missing_job_signal(
    deployer: ModuleType,
) -> None:
    result = subprocess.CompletedProcess(
        [],
        1,
        "",
        "ERROR: (gcloud.run.jobs.describe) NOT_FOUND: Job "
        "'trends-engine-open-intelligence-staging' was not found.\n",
    )
    assert deployer._is_exact_not_found(result) is True


def test_exact_not_found_line_requires_exit_one(deployer: ModuleType) -> None:
    result = subprocess.CompletedProcess(
        [],
        2,
        "",
        "ERROR: (gcloud.run.jobs.describe) Cannot find job "
        "[trends-engine-open-intelligence-staging].\n",
    )
    assert deployer._is_exact_not_found(result) is False


@pytest.mark.parametrize(
    "stderr",
    [
        "ERROR: (gcloud.run.jobs.describe) Cannot find job [wrong-job].\n",
        "ERROR: (gcloud.run.jobs.describe) Cannot find job "
        "[trends-engine-open-intelligence-staging-copy].\n",
        "ERROR: (gcloud.run.jobs.describe) Cannot find job "
        "[copy-trends-engine-open-intelligence-staging].\n",
        "WARNING: context changed\nERROR: (gcloud.run.jobs.describe) Cannot find job "
        "[trends-engine-open-intelligence-staging].\n",
        "ERROR: (gcloud.run.jobs.describe) Cannot find job "
        "trends-engine-open-intelligence-staging.\n",
        "ERROR: (gcloud.run.jobs.describe) Cannot find job "
        "[trends-engine-open-intelligence-staging.\n",
        "ERROR: (gcloud.run.jobs.describe) Cannot find job "
        "[trends-engine-open-intelligence-staging]\n",
        "ERROR: (gcloud.run.jobs.describe) Cannot find job "
        "[projects/wrong/jobs/trends-engine-open-intelligence-staging].\n",
        "ERROR: (gcloud.run.jobs.describe) Cannot find job "
        "[trends-engine-open-intelligence-staging].\nextra line\n",
        "ERROR: (gcloud.run.jobs.describe) NOT_FOUND: another target mentioned "
        "trends-engine-open-intelligence-staging.\n",
    ],
)
def test_not_found_near_misses_remain_rejected(deployer: ModuleType, stderr: str) -> None:
    result = subprocess.CompletedProcess([], 1, "", stderr)
    assert deployer._is_exact_not_found(result) is False


def test_observed_cannot_find_job_discovery_creates_once_then_reads_back(
    deployer: ModuleType,
) -> None:
    runner = StatefulJobRunner(
        None,
        describe_error_once=(
            "ERROR: (gcloud.run.jobs.describe) Cannot find job "
            "[trends-engine-open-intelligence-staging].\n"
        ),
    )
    assert deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver()) == "created"
    assert runner.mutations == ["create"]
    assert runner.commands == [
        ["git", "status", "--porcelain", "--untracked-files=no"],
        ["git", "rev-parse", "HEAD"],
        describe_command(),
        create_command(),
        describe_command(),
    ]


def test_duplicate_containers_fail_discovery_before_mutation(deployer: ModuleType) -> None:
    resource = desired_resource()
    containers = resource["spec"]["template"]["spec"]["template"]["spec"]["containers"]
    containers.append(deepcopy(containers[0]))
    runner = StatefulJobRunner(resource)
    with pytest.raises(deployer.DeploymentError, match="exactly one container"):
        deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver())
    assert runner.mutations == []


@pytest.mark.parametrize(
    ("dirty", "head", "message"),
    [
        (True, SOURCE_SHA, "tracked tree is dirty"),
        (False, "c" * 40, "does not equal source SHA"),
    ],
)
def test_local_git_checks_fail_before_gcloud_resolution(
    deployer: ModuleType, dirty: bool, head: str, message: str
) -> None:
    runner = StatefulJobRunner(None, dirty=dirty, head=head)
    resolver = Resolver()
    with pytest.raises(deployer.DeploymentError, match=message):
        deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=resolver)
    assert resolver.calls == []
    assert runner.mutations == []


def test_missing_gcloud_fails_after_local_checks_without_discovery(
    deployer: ModuleType,
) -> None:
    runner = StatefulJobRunner(None)
    resolver = Resolver(None)
    with pytest.raises(deployer.DeploymentError, match="gcloud executable not found"):
        deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=resolver)
    assert runner.commands == [
        ["git", "status", "--porcelain", "--untracked-files=no"],
        ["git", "rev-parse", "HEAD"],
    ]


def test_mutation_failure_stops_before_readback(deployer: ModuleType) -> None:
    runner = StatefulJobRunner(None, failure_mode="no_effect_error")
    with pytest.raises(deployer.DeploymentError, match="create failed"):
        deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver())
    assert runner.mutations == ["create"]
    assert runner.describe_count == 1


@pytest.mark.parametrize("initial", [None, desired_resource()], ids=["create", "update"])
def test_retry_after_no_effect_mutation_failure_converges(
    deployer: ModuleType, initial: dict[str, object] | None
) -> None:
    if initial is not None:
        initial["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["image"] = (
            IMAGE.replace("a" * 64, "c" * 64)
        )
    runner = StatefulJobRunner(initial, failure_mode="no_effect_error")
    with pytest.raises(deployer.DeploymentError, match=r"gcloud (create|update) failed"):
        deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver())

    runner.failure_mode = None
    expected = "created" if initial is None else "updated"
    assert deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver()) == expected
    assert deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver()) == "unchanged"


@pytest.mark.parametrize("initial", [None, desired_resource()], ids=["create", "update"])
def test_accepted_complete_mutation_then_error_retry_skips_without_duplicate_mutation(
    deployer: ModuleType, initial: dict[str, object] | None
) -> None:
    if initial is not None:
        initial["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["image"] = (
            IMAGE.replace("a" * 64, "c" * 64)
        )
    runner = StatefulJobRunner(initial, failure_mode="apply_then_error")
    operation = "create" if initial is None else "update"
    with pytest.raises(deployer.DeploymentError, match=f"gcloud {operation} failed"):
        deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver())
    assert runner.mutations == [operation]
    assert runner.resource == desired_resource()

    runner.failure_mode = None
    runner.commands.clear()
    assert deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver()) == "unchanged"
    assert runner.mutations == [operation]
    assert runner.commands == [
        ["git", "status", "--porcelain", "--untracked-files=no"],
        ["git", "rev-parse", "HEAD"],
        describe_command(),
    ]

    runner.commands.clear()
    assert deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver()) == "unchanged"
    assert runner.mutations == [operation]
    assert runner.commands[-1] == describe_command()


def test_partially_applied_update_then_error_retry_updates_reads_back_and_skips(
    deployer: ModuleType,
) -> None:
    initial = desired_resource()
    initial["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["image"] = (
        IMAGE.replace("a" * 64, "c" * 64)
    )
    runner = StatefulJobRunner(initial, failure_mode="partial_then_error")
    with pytest.raises(deployer.DeploymentError, match="gcloud update failed"):
        deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver())
    assert runner.mutations == ["update"]
    assert runner.resource != desired_resource()

    runner.failure_mode = None
    runner.commands.clear()
    assert deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver()) == "updated"
    assert runner.mutations == ["update", "update"]
    assert runner.commands == [
        ["git", "status", "--porcelain", "--untracked-files=no"],
        ["git", "rev-parse", "HEAD"],
        describe_command(),
        update_command(),
        describe_command(),
    ]

    runner.commands.clear()
    assert deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver()) == "unchanged"
    assert runner.mutations == ["update", "update"]
    assert runner.commands[-1] == describe_command()


def test_subprocess_adapter_uses_list_args_with_shell_disabled(
    deployer: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(deployer.subprocess, "run", fake_run)
    command = ["gcloud", "run", "jobs", "describe", JOB]
    deployer._run_subprocess(command)
    assert observed == {
        "command": command,
        "kwargs": {
            "shell": False,
            "capture_output": True,
            "encoding": "utf-8",
            "check": False,
        },
    }


def _set(resource: dict[str, object], path: Sequence[object], value: object) -> None:
    cursor: object = resource
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value


READBACK_MUTATIONS: list[tuple[str, Callable[[dict[str, object]], None]]] = [
    ("job", lambda r: _set(r, ["metadata", "name"], "wrong-job")),
    ("project", lambda r: _set(r, ["metadata", "namespace"], "999999999999")),
    (
        "region",
        lambda r: _set(r, ["metadata", "labels", "cloud.googleapis.com/location"], "europe-west1"),
    ),
    (
        "service_account",
        lambda r: _set(
            r,
            ["spec", "template", "spec", "template", "spec", "serviceAccountName"],
            "wrong@example.com",
        ),
    ),
    (
        "image",
        lambda r: _set(
            r,
            ["spec", "template", "spec", "template", "spec", "containers", 0, "image"],
            IMAGE.replace("a" * 64, "c" * 64),
        ),
    ),
    (
        "command",
        lambda r: _set(
            r,
            ["spec", "template", "spec", "template", "spec", "containers", 0, "command"],
            ["bash"],
        ),
    ),
    (
        "args",
        lambda r: _set(
            r,
            ["spec", "template", "spec", "template", "spec", "containers", 0, "args"],
            ["wrong.py"],
        ),
    ),
    (
        "timeout",
        lambda r: _set(
            r,
            ["spec", "template", "spec", "template", "spec", "timeoutSeconds"],
            "60",
        ),
    ),
    ("tasks", lambda r: _set(r, ["spec", "template", "spec", "taskCount"], 2)),
    (
        "parallelism",
        lambda r: _set(r, ["spec", "template", "spec", "parallelism"], 2),
    ),
    (
        "retries",
        lambda r: _set(r, ["spec", "template", "spec", "template", "spec", "maxRetries"], 1),
    ),
    (
        "cpu",
        lambda r: _set(
            r,
            [
                "spec",
                "template",
                "spec",
                "template",
                "spec",
                "containers",
                0,
                "resources",
                "limits",
                "cpu",
            ],
            "1",
        ),
    ),
    (
        "memory",
        lambda r: _set(
            r,
            [
                "spec",
                "template",
                "spec",
                "template",
                "spec",
                "containers",
                0,
                "resources",
                "limits",
                "memory",
            ],
            "4Gi",
        ),
    ),
    (
        "env",
        lambda r: r["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"].append(
            {"name": "INHERITED", "value": "true"}
        ),
    ),
    (
        "secret",
        lambda r: r["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"].append(
            {"name": "SECRET", "valueFrom": {"secretKeyRef": {"name": "x", "key": "1"}}}
        ),
    ),
    (
        "secret_annotation",
        lambda r: _set(
            r,
            ["spec", "template", "metadata", "annotations"],
            {"run.googleapis.com/secrets": "SECRET:projects/p/secrets/s"},
        ),
    ),
    (
        "volume",
        lambda r: _set(
            r,
            ["spec", "template", "spec", "template", "spec", "volumes"],
            [{"name": "secret", "secret": {"secretName": "x"}}],
        ),
    ),
    (
        "resource_labels",
        lambda r: r["metadata"]["labels"].pop("source-sha"),
    ),
    (
        "template_labels",
        lambda r: r["spec"]["template"]["metadata"]["labels"].pop("source-sha"),
    ),
    (
        "cloudsql",
        lambda r: _set(
            r,
            ["spec", "template", "metadata", "annotations"],
            {"run.googleapis.com/cloudsql-instances": "project:region:db"},
        ),
    ),
    (
        "vpc",
        lambda r: _set(
            r,
            ["spec", "template", "metadata", "annotations"],
            {"run.googleapis.com/vpc-access-connector": "connector"},
        ),
    ),
    (
        "direct_vpc",
        lambda r: _set(
            r,
            ["spec", "template", "metadata", "annotations"],
            {
                "run.googleapis.com/network-interfaces": '[{"network":"default"}]',
                "run.googleapis.com/vpc-access-egress": "all-traffic",
            },
        ),
    ),
    (
        "vpc_egress",
        lambda r: _set(
            r,
            ["spec", "template", "metadata", "annotations"],
            {"run.googleapis.com/vpc-access-egress": "private-ranges-only"},
        ),
    ),
]


@pytest.mark.parametrize(
    ("field", "mutate"), READBACK_MUTATIONS, ids=[x[0] for x in READBACK_MUTATIONS]
)
def test_every_readback_mismatch_stops_after_create(
    deployer: ModuleType, field: str, mutate: Callable[[dict[str, object]], None]
) -> None:
    runner = StatefulJobRunner(None, readback_mutation=mutate)
    with pytest.raises(deployer.DeploymentError, match="readback mismatch"):
        deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver())
    assert runner.mutations == ["create"], field
    assert runner.describe_count == 2


def test_parser_rejects_missing_namespace_even_with_resource_path(
    deployer: ModuleType,
) -> None:
    resource = desired_resource()
    resource["metadata"].pop("namespace")
    with pytest.raises(deployer.DeploymentError, match="project identity"):
        deployer.parse_job_snapshot(resource)


def test_observed_v1_string_timeout_parses_to_seconds(deployer: ModuleType) -> None:
    resource = desired_resource()
    resource["spec"]["template"]["spec"]["template"]["spec"]["timeoutSeconds"] = "10800"
    snapshot = deployer.parse_job_snapshot(resource)
    assert snapshot.timeout_seconds == 10800


def test_observed_empty_cloudsql_annotation_is_semantically_cleared(
    deployer: ModuleType,
) -> None:
    resource = desired_resource()
    resource["spec"]["template"]["metadata"]["annotations"] = {
        "run.googleapis.com/cloudsql-instances": ""
    }
    snapshot = deployer.parse_job_snapshot(resource)
    assert snapshot.has_cloudsql is False
    assert deployer._snapshot_mismatches(snapshot, deployer.desired_job(IMAGE, SOURCE_SHA)) == ()


@pytest.mark.parametrize("key", FORBIDDEN_ANNOTATION_KEYS)
def test_empty_forbidden_annotation_values_are_semantically_cleared(
    deployer: ModuleType, key: str
) -> None:
    resource = desired_resource()
    resource["spec"]["template"]["metadata"]["annotations"] = {key: ""}
    snapshot = deployer.parse_job_snapshot(resource)
    assert snapshot.has_secret_refs is False
    assert snapshot.has_cloudsql is False
    assert snapshot.has_vpc is False
    assert deployer._snapshot_mismatches(snapshot, deployer.desired_job(IMAGE, SOURCE_SHA)) == ()


@pytest.mark.parametrize("key", FORBIDDEN_ANNOTATION_KEYS)
@pytest.mark.parametrize("value", [" ", "[]", "configured"])
def test_nonempty_forbidden_annotation_values_remain_configured(
    deployer: ModuleType, key: str, value: str
) -> None:
    resource = desired_resource()
    resource["spec"]["template"]["metadata"]["annotations"] = {key: value}
    snapshot = deployer.parse_job_snapshot(resource)
    if key == "run.googleapis.com/secrets":
        assert snapshot.has_secret_refs is True
        expected = "secret_refs"
    elif key == "run.googleapis.com/cloudsql-instances":
        assert snapshot.has_cloudsql is True
        expected = "cloudsql"
    else:
        assert snapshot.has_vpc is True
        expected = "vpc"
    assert expected in deployer._snapshot_mismatches(
        snapshot, deployer.desired_job(IMAGE, SOURCE_SHA)
    )


@pytest.mark.parametrize("key", FORBIDDEN_ANNOTATION_KEYS)
@pytest.mark.parametrize("value", [[], {}, None, 1, True])
def test_nonstring_forbidden_annotation_values_are_malformed(
    deployer: ModuleType, key: str, value: object
) -> None:
    resource = desired_resource()
    resource["spec"]["template"]["metadata"]["annotations"] = {key: value}
    with pytest.raises(deployer.DeploymentError, match="annotations"):
        deployer.parse_job_snapshot(resource)


def test_canonical_zero_string_timeout_parses_to_zero(deployer: ModuleType) -> None:
    resource = desired_resource()
    resource["spec"]["template"]["spec"]["template"]["spec"]["timeoutSeconds"] = "0"
    snapshot = deployer.parse_job_snapshot(resource)
    assert snapshot.timeout_seconds == 0


@pytest.mark.parametrize(
    "value",
    [
        "+10800",
        " 10800",
        "10800 ",
        "10800.0",
        "1e4",
        "",
        True,
        False,
        10800,
        10800.0,
        -1,
        "-1",
        "010800",
        "00",
        "١٠٨٠٠",
        None,
    ],
)
def test_timeout_rejects_noncanonical_or_nonstring_values(
    deployer: ModuleType, value: object
) -> None:
    resource = desired_resource()
    resource["spec"]["template"]["spec"]["template"]["spec"]["timeoutSeconds"] = value
    with pytest.raises(deployer.DeploymentError, match="timeoutSeconds"):
        deployer.parse_job_snapshot(resource)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (["spec", "template", "spec", "taskCount"], "1"),
        (["spec", "template", "spec", "parallelism"], "1"),
        (
            ["spec", "template", "spec", "template", "spec", "maxRetries"],
            "0",
        ),
    ],
    ids=["task_count", "parallelism", "max_retries"],
)
def test_non_timeout_integer_fields_stay_strict_json_integers(
    deployer: ModuleType, path: list[object], value: object
) -> None:
    resource = desired_resource()
    _set(resource, path, value)
    with pytest.raises(deployer.DeploymentError):
        deployer.parse_job_snapshot(resource)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda r: _set(r, ["apiVersion"], "run.googleapis.com/v2"), "apiVersion"),
        (lambda r: _set(r, ["kind"], "Service"), "kind"),
        (
            lambda r: (
                _set(r, ["metadata", "namespace"], "999999999999"),
                _set(
                    r,
                    ["metadata", "selfLink"],
                    f"/apis/run.googleapis.com/v1/namespaces/999999999999/jobs/{JOB}",
                ),
            ),
            "project identity",
        ),
        (
            lambda r: _set(
                r,
                ["metadata", "selfLink"],
                f"/apis/run.googleapis.com/v1/namespaces/{PROJECT_NUMBER}/jobs/wrong-job",
            ),
            "job identity",
        ),
    ],
)
def test_wrong_resource_identity_fails_discovery_before_mutation(
    deployer: ModuleType,
    mutation: Callable[[dict[str, object]], object],
    message: str,
) -> None:
    resource = desired_resource()
    mutation(resource)
    runner = StatefulJobRunner(resource)
    with pytest.raises(deployer.DeploymentError, match=message):
        deployer.apply_job(IMAGE, SOURCE_SHA, runner=runner, resolver=Resolver())
    assert runner.mutations == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.pop("spec"),
        lambda r: r["spec"]["template"]["spec"].pop("taskCount"),
        lambda r: r["spec"]["template"]["spec"]["template"]["spec"].pop("containers"),
        lambda r: r["metadata"].pop("namespace") and r["metadata"].pop("selfLink"),
        lambda r: r["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"].append(
            {"name": "GCP_PROJECT", "value": PROJECT}
        ),
    ],
)
def test_malformed_or_ambiguous_nested_resource_is_rejected(
    deployer: ModuleType, mutation: Callable[[dict[str, object]], object]
) -> None:
    resource = desired_resource()
    mutation(resource)
    with pytest.raises(deployer.DeploymentError, match=r"malformed|ambiguous"):
        deployer.parse_job_snapshot(resource)


def test_rollback_has_no_cli_or_execution_path(
    deployer: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        deployer.main(
            [
                "--image-digest",
                IMAGE,
                "--source-sha",
                SOURCE_SHA,
                "--rollback",
            ]
        )
    assert "unrecognized arguments: --rollback" in capsys.readouterr().err
