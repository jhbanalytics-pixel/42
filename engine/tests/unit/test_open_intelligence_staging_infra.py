"""Boundary tests for the Open Intelligence staging infrastructure provisioner."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from google.api_core.exceptions import NotFound
from google.cloud import bigquery

PROJECT = "ogilvy-trends-v2"
LOCATION = "US"
JOB_ROLE = "roles/bigquery.jobUser"
ENGINE = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
CANARY = "trends-engine-canary@ogilvy-trends-v2.iam.gserviceaccount.com"
LISTENING_POST = "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
IDENTITIES = (ENGINE, CANARY, LISTENING_POST)
MAIN_DATASET = "trends_v2_staging"
QA_DATASET = "trends_v2_staging_qa"
MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "staging"
    / "provision_open_intelligence_staging.py"
)


def _gcloud_resolver(name: str) -> str | None:
    return "/usr/bin/gcloud" if name == "gcloud" else None


def _apply_plan(infra: ModuleType, *args, **kwargs):
    kwargs.setdefault("resolver", _gcloud_resolver)
    return infra.apply_plan(*args, **kwargs)


@pytest.fixture
def infra() -> ModuleType:
    assert MODULE_PATH.exists(), "production provisioner is missing"
    spec = importlib.util.spec_from_file_location("staging_infra_under_test", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class StatefulGcloudRunner:
    """Local command fake with persistent service-account and IAM state."""

    def __init__(
        self,
        *,
        service_accounts: Sequence[str] = (),
        job_user_members: Sequence[str] = (),
        fail_after_kind: str | None = None,
        malformed_json: bool = False,
        duplicate_role_binding: bool = False,
        hide_account_on_readback: str | None = None,
        hide_role_on_readback: str | None = None,
    ) -> None:
        self.service_accounts = set(service_accounts)
        self.job_user_members = {
            member.removeprefix("serviceAccount:") for member in job_user_members
        }
        self.fail_after_kind = fail_after_kind
        self.malformed_json = malformed_json
        self.duplicate_role_binding = duplicate_role_binding
        self.hide_account_on_readback = hide_account_on_readback
        self.hide_role_on_readback = hide_role_on_readback
        self.commands: list[list[str]] = []
        self.mutations: list[str] = []
        self.list_count = 0
        self.policy_read_count = 0

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        assert isinstance(command, list), "gcloud command must be a list"
        assert command
        assert command[0].lower().endswith(("gcloud", "gcloud.cmd"))
        self.commands.append(list(command))
        args = list(command[1:])

        if args[:3] == ["iam", "service-accounts", "list"]:
            self.list_count += 1
            accounts = sorted(self.service_accounts)
            if self.list_count > 1 and self.hide_account_on_readback:
                accounts = [email for email in accounts if email != self.hide_account_on_readback]
            return self._json([{"email": email} for email in accounts])

        if args[:3] == ["iam", "service-accounts", "create"]:
            account_id = args[3]
            project = args[args.index("--project") + 1]
            email = f"{account_id}@{project}.iam.gserviceaccount.com"
            self.service_accounts.add(email)
            return self._mutated("service_account")

        if args[:2] == ["projects", "get-iam-policy"]:
            self.policy_read_count += 1
            members = sorted(self.job_user_members)
            if self.policy_read_count > 1 and self.hide_role_on_readback:
                members = [email for email in members if email != self.hide_role_on_readback]
            binding = {
                "bindings": {
                    "members": [f"serviceAccount:{email}" for email in members],
                    "role": JOB_ROLE,
                }
            }
            payload = [binding] if members else []
            if self.duplicate_role_binding and payload:
                payload.append(deepcopy(binding))
            return self._json(payload)

        if args[:2] == ["projects", "add-iam-policy-binding"]:
            member = args[args.index("--member") + 1]
            role = args[args.index("--role") + 1]
            assert role == JOB_ROLE
            self.job_user_members.add(member.removeprefix("serviceAccount:"))
            return self._mutated("role")

        raise AssertionError(f"unexpected gcloud command: {args!r}")

    def _json(self, payload: object) -> subprocess.CompletedProcess[str]:
        stdout = "{" if self.malformed_json else json.dumps(payload)
        self.malformed_json = False
        return subprocess.CompletedProcess(self.commands[-1], 0, stdout, "")

    def _mutated(self, kind: str) -> subprocess.CompletedProcess[str]:
        self.mutations.append(kind)
        if self.fail_after_kind == kind:
            self.fail_after_kind = None
            return subprocess.CompletedProcess(
                self.commands[-1], 1, "", "fixture-secret-must-not-print"
            )
        return subprocess.CompletedProcess(self.commands[-1], 0, "", "")


class StatefulBigQueryClient:
    """Stateful fake using installed BigQuery Dataset and AccessEntry classes."""

    def __init__(
        self,
        datasets: Sequence[bigquery.Dataset] = (),
        *,
        fail_after_kind: str | None = None,
        hide_dataset_on_readback: str | None = None,
        remove_unrelated_on_readback: bool = False,
        raw_read_ids: Sequence[str] = (),
        raise_on_get: dict[str, Exception] | None = None,
    ) -> None:
        self.project = PROJECT
        self.datasets = {dataset.dataset_id: self._clone(dataset) for dataset in datasets}
        self.fail_after_kind = fail_after_kind
        self.hide_dataset_on_readback = hide_dataset_on_readback
        self.remove_unrelated_on_readback = remove_unrelated_on_readback
        self.raw_read_ids = set(raw_read_ids)
        self.raise_on_get = raise_on_get or {}
        self.calls: list[tuple[str, object]] = []
        self.mutations: list[str] = []
        self.get_counts: dict[str, int] = {}

    def get_dataset(self, dataset_ref: str) -> bigquery.Dataset:
        self.calls.append(("get_dataset", dataset_ref))
        dataset_id = dataset_ref.rsplit(".", 1)[-1]
        self.get_counts[dataset_id] = self.get_counts.get(dataset_id, 0) + 1
        if dataset_id in self.raise_on_get:
            raise self.raise_on_get[dataset_id]
        if dataset_id not in self.datasets:
            raise NotFound(f"fixture dataset missing: {dataset_ref}")
        if self.hide_dataset_on_readback == dataset_id and self.get_counts[dataset_id] > 1:
            raise NotFound(f"fixture readback missing: {dataset_ref}")
        if dataset_id in self.raw_read_ids:
            dataset = self.datasets[dataset_id]
        else:
            dataset = self._clone(self.datasets[dataset_id])
        if self.remove_unrelated_on_readback and self.get_counts[dataset_id] > 1:
            dataset.access_entries = [
                entry
                for entry in dataset.access_entries
                if entry.entity_id != "existing-group@example.com"
            ]
        return dataset

    def create_dataset(self, dataset: bigquery.Dataset) -> bigquery.Dataset:
        assert isinstance(dataset, bigquery.Dataset)
        self.calls.append(("create_dataset", dataset.full_dataset_id))
        self.datasets[dataset.dataset_id] = self._clone(dataset)
        return self._mutated("dataset", dataset)

    def update_dataset(self, dataset: bigquery.Dataset, fields: Sequence[str]) -> bigquery.Dataset:
        assert isinstance(dataset, bigquery.Dataset)
        assert list(fields) == ["access_entries"]
        self.calls.append(("update_dataset", dataset.full_dataset_id))
        self.datasets[dataset.dataset_id] = self._clone(dataset)
        return self._mutated("access", dataset)

    def _mutated(self, kind: str, dataset: bigquery.Dataset) -> bigquery.Dataset:
        self.mutations.append(kind)
        if self.fail_after_kind == kind:
            self.fail_after_kind = None
            raise RuntimeError("fixture BigQuery failure")
        return self._clone(dataset)

    @staticmethod
    def _clone(dataset: bigquery.Dataset) -> bigquery.Dataset:
        return bigquery.Dataset.from_api_repr(deepcopy(dataset.to_api_repr()))


def _dataset(
    dataset_id: str,
    *,
    project: str = PROJECT,
    location: str = LOCATION,
    table_expiry: int | None = None,
    partition_expiry: int | None = None,
    access: Sequence[bigquery.AccessEntry] = (),
) -> bigquery.Dataset:
    dataset = bigquery.Dataset(f"{project}.{dataset_id}")
    dataset.location = location
    dataset.default_table_expiration_ms = table_expiry
    dataset.default_partition_expiration_ms = partition_expiry
    dataset.access_entries = list(access)
    return dataset


def _unrelated_access() -> bigquery.AccessEntry:
    return bigquery.AccessEntry("READER", "groupByEmail", "existing-group@example.com")


def _unrelated_mapping_access(entity_type: str) -> bigquery.AccessEntry:
    entity_ids = {
        "view": {
            "projectId": PROJECT,
            "datasetId": "shared_views",
            "tableId": "approved_view",
        },
        "routine": {
            "projectId": PROJECT,
            "datasetId": "shared_routines",
            "routineId": "approved_routine",
        },
        "dataset": {
            "dataset": {
                "projectId": PROJECT,
                "datasetId": "shared_authorized_dataset",
            },
            "targetTypes": ["VIEWS"],
        },
    }
    return bigquery.AccessEntry(None, entity_type, entity_ids[entity_type])


def _entry_keys(dataset: bigquery.Dataset) -> set[tuple[object, object, object]]:
    return {(entry.role, entry.entity_type, entry.entity_id) for entry in dataset.access_entries}


def _entry_receipts(dataset: bigquery.Dataset) -> Counter[str]:
    return Counter(
        json.dumps(entry.to_api_repr(), sort_keys=True, separators=(",", ":"))
        for entry in dataset.access_entries
    )


def _assert_exact_state(
    runner: StatefulGcloudRunner,
    client: StatefulBigQueryClient,
    *,
    unrelated_account: str | None = None,
    unrelated_member: str | None = None,
    unrelated_qa_entries: Sequence[bigquery.AccessEntry] = (),
) -> None:
    expected_accounts = set(IDENTITIES)
    if unrelated_account:
        expected_accounts.add(unrelated_account)
    assert runner.service_accounts == expected_accounts

    expected_members = set(IDENTITIES)
    if unrelated_member:
        expected_members.add(unrelated_member)
    assert runner.job_user_members == expected_members
    assert set(client.datasets) == {MAIN_DATASET, QA_DATASET}

    expected_access = {
        MAIN_DATASET: [
            bigquery.AccessEntry("WRITER", "userByEmail", ENGINE),
            bigquery.AccessEntry("READER", "userByEmail", LISTENING_POST),
        ],
        QA_DATASET: [
            *unrelated_qa_entries,
            bigquery.AccessEntry("WRITER", "userByEmail", CANARY),
        ],
    }
    for dataset_id, dataset in client.datasets.items():
        assert dataset.project == PROJECT
        assert dataset.dataset_id == dataset_id
        assert dataset.location == LOCATION
        assert dataset.default_table_expiration_ms is None
        assert dataset.default_partition_expiration_ms is None
        assert _entry_receipts(dataset) == Counter(
            json.dumps(entry.to_api_repr(), sort_keys=True, separators=(",", ":"))
            for entry in expected_access[dataset_id]
        )

    assert Counter(runner.mutations) == {
        "service_account": 3,
        "role": 3,
    }
    assert Counter(client.mutations) == {"dataset": 1, "access": 2}


def _apply_from_empty(infra: ModuleType):
    runner = StatefulGcloudRunner()
    client = StatefulBigQueryClient()
    _apply_plan(infra, infra.build_plan(), runner=runner, client_factory=lambda: client)
    return runner, client


def _rendered_actions(output: str) -> list[dict[str, object]]:
    return [
        json.loads(line.removeprefix("ACTION "))
        for line in output.splitlines()
        if line.startswith("ACTION ")
    ]


def test_dry_run_prints_exact_plan_without_constructing_clients_or_subprocesses(
    infra, monkeypatch, capsys
):
    boundary_calls: list[str] = []

    def boundary_bomb(*args, **kwargs):
        boundary_calls.append("called")
        raise AssertionError("dry run crossed an apply boundary")

    monkeypatch.setattr(infra.subprocess, "run", boundary_bomb)
    monkeypatch.setattr(infra.bigquery, "Client", boundary_bomb)

    assert infra.main([], resolver=_gcloud_resolver) == 0
    output = capsys.readouterr().out
    assert boundary_calls == []
    assert PROJECT in output
    assert "Location: US" in output
    assert output.index(ENGINE) < output.index(CANARY) < output.index(LISTENING_POST)
    assert output.index(MAIN_DATASET) < output.index(QA_DATASET)
    assert "roles/bigquery.jobUser" in output
    assert "WRITER" in output
    assert "READER" in output
    assert "Forward actions" in output
    assert "Readback checks" in output
    assert "Rollback plan" in output
    assert "separate destructive authorization" in output
    assert "delete service account" not in output.lower()
    assert "delete dataset" not in output.lower()
    assert "remove-iam-policy-binding" not in output


@pytest.mark.parametrize("starting_state", ["empty", "partial", "idempotent"])
def test_dry_run_actions_exactly_match_apply_trace(infra, starting_state):
    executable = r"C:\Program Files\Google\Cloud SDK\bin\gcloud.cmd"

    def resolver(name: str) -> str | None:
        return executable if name == "gcloud.cmd" else None

    if starting_state == "empty":
        runner = StatefulGcloudRunner()
        client = StatefulBigQueryClient()
    elif starting_state == "partial":
        runner = StatefulGcloudRunner(service_accounts=[ENGINE], job_user_members=[ENGINE])
        client = StatefulBigQueryClient(
            [
                _dataset(
                    MAIN_DATASET,
                    access=[bigquery.AccessEntry("WRITER", "userByEmail", ENGINE)],
                )
            ]
        )
    else:
        runner = StatefulGcloudRunner()
        client = StatefulBigQueryClient()
        _apply_plan(
            infra,
            infra.build_plan(),
            runner=runner,
            client_factory=lambda: client,
            resolver=resolver,
        )

    rendered = _rendered_actions(infra.render_dry_run(infra.build_plan(), resolver=resolver))
    trace = []
    _apply_plan(
        infra,
        infra.build_plan(),
        runner=runner,
        client_factory=lambda: client,
        resolver=resolver,
        trace=trace,
    )

    assert rendered == [result.action.to_record() for result in trace]
    assert rendered == [
        action.to_record() for action in infra.build_actions(infra.build_plan(), resolver=resolver)
    ]
    assert all(
        isinstance(action["command"], list) for action in rendered if action["system"] == "gcloud"
    )
    assert any(
        action["system"] == "bigquery" and action["operation"] == "get_dataset"
        for action in rendered
    )
    mutation_statuses = [result.status for result in trace if result.action.phase == "mutate"]
    if starting_state == "empty":
        assert mutation_statuses == ["executed"] * len(mutation_statuses)
    elif starting_state == "idempotent":
        assert mutation_statuses == ["skipped"] * len(mutation_statuses)
    else:
        assert "executed" in mutation_statuses
        assert "skipped" in mutation_statuses


@pytest.mark.parametrize(
    ("available", "expected"),
    [
        (
            {"gcloud.cmd": r"C:\sdk\bin\gcloud.cmd"},
            r"C:\sdk\bin\gcloud.cmd",
        ),
        ({"gcloud": "/usr/bin/gcloud"}, "/usr/bin/gcloud"),
    ],
)
def test_gcloud_executable_resolution_is_injected(infra, available, expected):
    calls: list[str] = []

    def resolver(name: str) -> str | None:
        calls.append(name)
        return available.get(name)

    assert infra.resolve_gcloud_executable(resolver=resolver) == expected
    assert calls == (["gcloud.cmd"] if "gcloud.cmd" in available else ["gcloud.cmd", "gcloud"])


def test_missing_gcloud_executable_is_typed_without_subprocess(infra):
    with pytest.raises(infra.ProvisioningError, match="gcloud executable"):
        infra.resolve_gcloud_executable(resolver=lambda name: None)


def test_cli_rejects_rollback_execution_without_crossing_boundaries(infra):
    with pytest.raises(SystemExit):
        infra.main(
            ["--rollback"],
            runner=lambda command: pytest.fail("runner called"),
            client_factory=lambda: pytest.fail("client constructed"),
        )


def test_default_runner_uses_a_list_and_disables_shell(infra, monkeypatch):
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "[]", "")

    monkeypatch.setattr(infra.subprocess, "run", fake_run)
    executable = infra.resolve_gcloud_executable(resolver=_gcloud_resolver)
    infra._run_gcloud([executable, "version"])

    assert observed["command"] == [executable, "version"]
    assert observed["shell"] is False
    assert observed["capture_output"] is True
    assert observed["encoding"] == "utf-8"


def test_default_runner_rejects_a_shell_string_before_subprocess(infra, monkeypatch):
    monkeypatch.setattr(
        infra.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("subprocess called"),
    )

    with pytest.raises(infra.ProvisioningError, match="list"):
        infra._run_gcloud("gcloud projects get-iam-policy")


def test_apply_uses_exact_commands_and_reaches_desired_state(infra):
    runner, client = _apply_from_empty(infra)
    gcloud = infra.resolve_gcloud_executable(resolver=_gcloud_resolver)

    assert runner.commands == [
        [
            gcloud,
            "iam",
            "service-accounts",
            "list",
            "--project",
            PROJECT,
            "--format=json(email)",
        ],
        [
            gcloud,
            "projects",
            "get-iam-policy",
            PROJECT,
            "--flatten=bindings",
            f"--filter=bindings.role={JOB_ROLE}",
            "--format=json(bindings.role,bindings.members,bindings.condition)",
        ],
        *[
            [
                gcloud,
                "iam",
                "service-accounts",
                "create",
                email.split("@", 1)[0],
                "--project",
                PROJECT,
                "--quiet",
            ]
            for email in IDENTITIES
        ],
        *[
            [
                gcloud,
                "projects",
                "add-iam-policy-binding",
                PROJECT,
                "--member",
                f"serviceAccount:{email}",
                "--role",
                JOB_ROLE,
                "--condition=None",
                "--format=none",
                "--quiet",
            ]
            for email in IDENTITIES
        ],
        [
            gcloud,
            "iam",
            "service-accounts",
            "list",
            "--project",
            PROJECT,
            "--format=json(email)",
        ],
        [
            gcloud,
            "projects",
            "get-iam-policy",
            PROJECT,
            "--flatten=bindings",
            f"--filter=bindings.role={JOB_ROLE}",
            "--format=json(bindings.role,bindings.members,bindings.condition)",
        ],
    ]
    assert runner.service_accounts == set(IDENTITIES)
    assert runner.job_user_members == set(IDENTITIES)
    assert set(client.datasets) == {MAIN_DATASET, QA_DATASET}
    assert _entry_keys(client.datasets[MAIN_DATASET]) == {
        ("WRITER", "userByEmail", ENGINE),
        ("READER", "userByEmail", LISTENING_POST),
    }
    assert _entry_keys(client.datasets[QA_DATASET]) == {
        ("WRITER", "userByEmail", CANARY),
    }
    for dataset in client.datasets.values():
        assert dataset.project == PROJECT
        assert dataset.location == LOCATION
        assert dataset.default_table_expiration_ms is None
        assert dataset.default_partition_expiration_ms is None


def test_apply_cli_uses_the_stateful_boundaries(infra, capsys):
    runner = StatefulGcloudRunner()
    client = StatefulBigQueryClient()

    assert (
        infra.main(
            ["--apply"],
            runner=runner,
            client_factory=lambda: client,
            resolver=_gcloud_resolver,
        )
        == 0
    )

    assert runner.service_accounts == set(IDENTITIES)
    assert set(client.datasets) == {MAIN_DATASET, QA_DATASET}
    assert capsys.readouterr().out == (
        "Apply readback complete for the approved staging resources.\n"
    )


def test_apply_preserves_unrelated_iam_and_dataset_access(infra):
    unrelated_member = "unrelated-job-runner@example.iam.gserviceaccount.com"
    runner = StatefulGcloudRunner(job_user_members=[unrelated_member])
    unrelated = _unrelated_access()
    client = StatefulBigQueryClient(
        [
            _dataset(MAIN_DATASET, access=[unrelated]),
            _dataset(QA_DATASET, access=[unrelated]),
        ]
    )

    _apply_plan(infra, infra.build_plan(), runner=runner, client_factory=lambda: client)

    assert unrelated_member in runner.job_user_members
    assert (
        "READER",
        "groupByEmail",
        "existing-group@example.com",
    ) in _entry_keys(client.datasets[MAIN_DATASET])
    assert (
        "READER",
        "groupByEmail",
        "existing-group@example.com",
    ) in _entry_keys(client.datasets[QA_DATASET])


@pytest.mark.parametrize("entity_type", ["view", "routine", "dataset"])
def test_apply_preserves_unrelated_access_with_mapping_identity(infra, entity_type):
    runner = StatefulGcloudRunner(service_accounts=IDENTITIES, job_user_members=IDENTITIES)
    entry = _unrelated_mapping_access(entity_type)
    client = StatefulBigQueryClient(
        [
            _dataset(MAIN_DATASET, access=[entry]),
            _dataset(QA_DATASET, access=[entry]),
        ]
    )

    _apply_plan(infra, infra.build_plan(), runner=runner, client_factory=lambda: client)

    entry_receipt = json.dumps(entry.to_api_repr(), sort_keys=True, separators=(",", ":"))
    for dataset in client.datasets.values():
        assert entry_receipt in {
            json.dumps(entry.to_api_repr(), sort_keys=True, separators=(",", ":"))
            for entry in dataset.access_entries
        }


def test_second_apply_is_idempotent(infra):
    runner, client = _apply_from_empty(infra)
    first_mutations = (len(runner.mutations), len(client.mutations))

    _apply_plan(infra, infra.build_plan(), runner=runner, client_factory=lambda: client)

    assert (len(runner.mutations), len(client.mutations)) == first_mutations
    assert len(_entry_keys(client.datasets[MAIN_DATASET])) == 2
    assert len(_entry_keys(client.datasets[QA_DATASET])) == 1


@pytest.mark.parametrize("failure_kind", ["service_account", "role", "dataset", "access"])
def test_partial_first_run_recovers_without_deletion(infra, failure_kind):
    unrelated_account = "existing-service@ogilvy-trends-v2.iam.gserviceaccount.com"
    unrelated_member = "existing-job-runner@ogilvy-trends-v2.iam.gserviceaccount.com"
    unrelated_qa_entries = (
        _unrelated_access(),
        _unrelated_mapping_access("view"),
    )
    runner = StatefulGcloudRunner(
        service_accounts=[unrelated_account],
        job_user_members=[unrelated_member],
        fail_after_kind=(failure_kind if failure_kind in {"service_account", "role"} else None),
    )
    client = StatefulBigQueryClient(
        [_dataset(QA_DATASET, access=unrelated_qa_entries)],
        fail_after_kind=(failure_kind if failure_kind in {"dataset", "access"} else None),
    )

    with pytest.raises(infra.ProvisioningError):
        _apply_plan(infra, infra.build_plan(), runner=runner, client_factory=lambda: client)

    _apply_plan(infra, infra.build_plan(), runner=runner, client_factory=lambda: client)

    _assert_exact_state(
        runner,
        client,
        unrelated_account=unrelated_account,
        unrelated_member=unrelated_member,
        unrelated_qa_entries=unrelated_qa_entries,
    )
    mutation_counts = (len(runner.mutations), len(client.mutations))
    _apply_plan(infra, infra.build_plan(), runner=runner, client_factory=lambda: client)
    assert (len(runner.mutations), len(client.mutations)) == mutation_counts
    _assert_exact_state(
        runner,
        client,
        unrelated_account=unrelated_account,
        unrelated_member=unrelated_member,
        unrelated_qa_entries=unrelated_qa_entries,
    )
    assert not any(
        "delete" in " ".join(command).lower() or "remove-iam-policy-binding" in command
        for command in runner.commands
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda plan: replace(plan, project="wrong-project"), "project"),
        (lambda plan: replace(plan, location="EU"), "location"),
        (lambda plan: replace(plan, job_user_role="roles/editor"), "role"),
        (
            lambda plan: replace(
                plan,
                service_accounts=(
                    replace(plan.service_accounts[0], email="wrong@example.com"),
                    *plan.service_accounts[1:],
                ),
            ),
            "service account",
        ),
        (
            lambda plan: replace(
                plan,
                datasets=(
                    replace(plan.datasets[0], dataset_id="production"),
                    plan.datasets[1],
                ),
            ),
            "dataset",
        ),
        (
            lambda plan: replace(
                plan,
                datasets=(
                    replace(plan.datasets[0], default_table_expiration_ms=1),
                    plan.datasets[1],
                ),
            ),
            "expiry",
        ),
    ],
)
def test_desired_state_mutations_fail_before_any_boundary(infra, mutation: Callable, message: str):
    boundary_calls: list[str] = []

    def boundary_bomb(*args, **kwargs):
        boundary_calls.append("called")
        raise AssertionError("invalid plan crossed a boundary")

    with pytest.raises(infra.ProvisioningError, match=message):
        _apply_plan(
            infra,
            mutation(infra.build_plan()),
            runner=boundary_bomb,
            client_factory=boundary_bomb,
        )
    assert boundary_calls == []


@pytest.mark.parametrize(
    "dataset",
    [
        _dataset(MAIN_DATASET, project="wrong-project"),
        _dataset(MAIN_DATASET, location="EU"),
        _dataset(MAIN_DATASET, table_expiry=86_400_000),
        _dataset(MAIN_DATASET, partition_expiry=86_400_000),
    ],
)
def test_existing_dataset_metadata_mismatch_stops_before_access_edit(infra, dataset):
    runner = StatefulGcloudRunner(service_accounts=IDENTITIES, job_user_members=IDENTITIES)
    client = StatefulBigQueryClient([dataset])

    with pytest.raises(infra.ProvisioningError, match="dataset metadata"):
        _apply_plan(infra, infra.build_plan(), runner=runner, client_factory=lambda: client)

    assert not any(call[0] == "update_dataset" for call in client.calls)


def test_complete_discovery_precedes_every_mutation_when_later_qa_is_bad(infra):
    runner = StatefulGcloudRunner()
    client = StatefulBigQueryClient(
        [
            _dataset(
                QA_DATASET,
                access=[bigquery.AccessEntry("WRITER", "userByEmail", ENGINE)],
            )
        ]
    )

    with pytest.raises(infra.ProvisioningError, match="cross-boundary"):
        _apply_plan(infra, infra.build_plan(), runner=runner, client_factory=lambda: client)

    assert runner.mutations == []
    assert client.mutations == []
    assert [call for call in client.calls if call[0] == "get_dataset"] == [
        ("get_dataset", f"{PROJECT}.{MAIN_DATASET}"),
        ("get_dataset", f"{PROJECT}.{QA_DATASET}"),
    ]


def _malformed_dataset(case: str) -> bigquery.Dataset:
    dataset = bigquery.Dataset.from_api_repr(_dataset(MAIN_DATASET).to_api_repr())
    if case == "missing_identity":
        dataset._properties["datasetReference"].pop("projectId")
    elif case == "malformed_access":
        dataset._properties["access"] = [
            {
                "role": "READER",
                "userByEmail": "one@example.com",
                "groupByEmail": "two@example.com",
            }
        ]
    elif case == "malformed_view":
        dataset.access_entries = [
            bigquery.AccessEntry(
                None,
                "view",
                {"projectId": PROJECT, "datasetId": "shared"},
            )
        ]
    elif case == "malformed_routine":
        dataset.access_entries = [
            bigquery.AccessEntry(
                None,
                "routine",
                {"projectId": PROJECT, "datasetId": "shared"},
            )
        ]
    elif case == "malformed_dataset_mapping":
        dataset.access_entries = [
            bigquery.AccessEntry(
                None,
                "dataset",
                {
                    "dataset": {"projectId": PROJECT},
                    "targetTypes": ["VIEWS"],
                },
            )
        ]
    elif case == "nonserializable":
        dataset.access_entries = [bigquery.AccessEntry("READER", "userByEmail", object())]
    else:
        raise AssertionError(f"unknown malformed fixture: {case}")
    return dataset


@pytest.mark.parametrize(
    "case",
    [
        "missing_identity",
        "malformed_access",
        "malformed_view",
        "malformed_routine",
        "malformed_dataset_mapping",
        "nonserializable",
    ],
)
def test_malformed_installed_dataset_readback_is_typed_before_mutation(infra, case):
    runner = StatefulGcloudRunner()
    client = StatefulBigQueryClient(raw_read_ids=[MAIN_DATASET])
    client.datasets[MAIN_DATASET] = _malformed_dataset(case)

    with pytest.raises(infra.ProvisioningError, match="dataset readback") as error:
        _apply_plan(infra, infra.build_plan(), runner=runner, client_factory=lambda: client)

    assert "one@example.com" not in str(error.value)
    assert runner.mutations == []
    assert client.mutations == []


def test_raw_bigquery_readback_exception_is_typed_before_mutation(infra):
    runner = StatefulGcloudRunner()
    client = StatefulBigQueryClient(
        raise_on_get={MAIN_DATASET: RuntimeError("fixture-sensitive-readback")}
    )

    with pytest.raises(infra.ProvisioningError) as error:
        _apply_plan(infra, infra.build_plan(), runner=runner, client_factory=lambda: client)

    assert "fixture-sensitive-readback" not in str(error.value)
    assert runner.mutations == []
    assert client.mutations == []


@pytest.mark.parametrize(
    ("dataset_id", "entry"),
    [
        (MAIN_DATASET, bigquery.AccessEntry("READER", "userByEmail", CANARY)),
        (QA_DATASET, bigquery.AccessEntry("WRITER", "userByEmail", ENGINE)),
        (
            QA_DATASET,
            bigquery.AccessEntry("READER", "userByEmail", LISTENING_POST),
        ),
    ],
)
def test_cross_boundary_dedicated_access_fails_closed(infra, dataset_id, entry):
    runner = StatefulGcloudRunner(service_accounts=IDENTITIES, job_user_members=IDENTITIES)
    client = StatefulBigQueryClient([_dataset(dataset_id, access=[entry])])

    with pytest.raises(infra.ProvisioningError, match="cross-boundary"):
        _apply_plan(infra, infra.build_plan(), runner=runner, client_factory=lambda: client)

    assert not any(call[0] == "update_dataset" for call in client.calls)


@pytest.mark.parametrize(
    "access",
    [
        [
            bigquery.AccessEntry("READER", "userByEmail", ENGINE),
        ],
        [
            bigquery.AccessEntry("WRITER", "userByEmail", ENGINE),
            bigquery.AccessEntry("WRITER", "userByEmail", ENGINE),
        ],
    ],
)
def test_conflicting_or_duplicate_dedicated_access_fails_closed(infra, access):
    runner = StatefulGcloudRunner(service_accounts=IDENTITIES, job_user_members=IDENTITIES)
    client = StatefulBigQueryClient([_dataset(MAIN_DATASET, access=access)])

    with pytest.raises(infra.ProvisioningError, match="dedicated access"):
        _apply_plan(infra, infra.build_plan(), runner=runner, client_factory=lambda: client)


def test_duplicate_job_user_binding_fails_closed(infra):
    runner = StatefulGcloudRunner(
        service_accounts=IDENTITIES,
        job_user_members=IDENTITIES,
        duplicate_role_binding=True,
    )

    with pytest.raises(infra.ProvisioningError, match=r"duplicate.*role"):
        _apply_plan(
            infra,
            infra.build_plan(),
            runner=runner,
            client_factory=lambda: pytest.fail("client constructed"),
        )


def test_unrelated_dataset_access_removed_during_readback_is_rejected(infra):
    runner = StatefulGcloudRunner(service_accounts=IDENTITIES, job_user_members=IDENTITIES)
    unrelated = _unrelated_access()
    client = StatefulBigQueryClient(
        [
            _dataset(MAIN_DATASET, access=[unrelated]),
            _dataset(QA_DATASET, access=[unrelated]),
        ],
        remove_unrelated_on_readback=True,
    )

    with pytest.raises(infra.ProvisioningError, match="unrelated access"):
        _apply_plan(infra, infra.build_plan(), runner=runner, client_factory=lambda: client)


def test_unrelated_project_member_removed_during_readback_is_rejected(infra):
    unrelated = "unrelated-job-runner@example.iam.gserviceaccount.com"
    runner = StatefulGcloudRunner(
        service_accounts=IDENTITIES,
        job_user_members=[*IDENTITIES, unrelated],
        hide_role_on_readback=unrelated,
    )
    client = StatefulBigQueryClient(
        [
            _dataset(
                MAIN_DATASET,
                access=[
                    bigquery.AccessEntry("WRITER", "userByEmail", ENGINE),
                    bigquery.AccessEntry("READER", "userByEmail", LISTENING_POST),
                ],
            ),
            _dataset(
                QA_DATASET,
                access=[bigquery.AccessEntry("WRITER", "userByEmail", CANARY)],
            ),
        ]
    )

    with pytest.raises(infra.ProvisioningError, match="unrelated project IAM"):
        _apply_plan(
            infra,
            infra.build_plan(),
            runner=runner,
            client_factory=lambda: client,
        )


@pytest.mark.parametrize(
    "runner",
    [
        StatefulGcloudRunner(malformed_json=True),
        StatefulGcloudRunner(
            service_accounts=IDENTITIES,
            job_user_members=IDENTITIES,
            hide_account_on_readback=ENGINE,
        ),
        StatefulGcloudRunner(
            service_accounts=IDENTITIES,
            job_user_members=IDENTITIES,
            hide_role_on_readback=ENGINE,
        ),
    ],
)
def test_malformed_or_missing_gcloud_readback_stops(infra, runner):
    with pytest.raises(infra.ProvisioningError):
        _apply_plan(
            infra,
            infra.build_plan(),
            runner=runner,
            client_factory=lambda: StatefulBigQueryClient(),
        )


def test_missing_dataset_readback_stops(infra):
    runner = StatefulGcloudRunner()
    client = StatefulBigQueryClient(hide_dataset_on_readback=MAIN_DATASET)

    with pytest.raises(infra.ProvisioningError, match="readback"):
        _apply_plan(infra, infra.build_plan(), runner=runner, client_factory=lambda: client)


def test_gcloud_failure_is_typed_sanitized_and_stops_later_actions(infra):
    runner = StatefulGcloudRunner(fail_after_kind="service_account")
    client = StatefulBigQueryClient()
    secret = "fixture-secret-must-not-print"

    with pytest.raises(infra.ProvisioningError) as error:
        _apply_plan(
            infra,
            infra.build_plan(),
            runner=runner,
            client_factory=lambda: client,
        )

    assert secret not in str(error.value)
    assert runner.mutations == ["service_account"]
    assert client.mutations == []


def test_bigquery_failure_is_typed_and_stops_later_actions(infra):
    runner = StatefulGcloudRunner()
    client = StatefulBigQueryClient(fail_after_kind="dataset")

    with pytest.raises(infra.ProvisioningError):
        _apply_plan(infra, infra.build_plan(), runner=runner, client_factory=lambda: client)

    assert client.mutations == ["dataset"]


def test_desired_access_entries_use_installed_exact_shapes(infra):
    plan = infra.build_plan()
    main, qa = plan.datasets

    assert all(
        isinstance(entry, bigquery.AccessEntry)
        for dataset in plan.datasets
        for entry in dataset.access_entries
    )
    assert {(entry.role, entry.entity_type, entry.entity_id) for entry in main.access_entries} == {
        ("WRITER", "userByEmail", ENGINE),
        ("READER", "userByEmail", LISTENING_POST),
    }
    assert {(entry.role, entry.entity_type, entry.entity_id) for entry in qa.access_entries} == {
        ("WRITER", "userByEmail", CANARY)
    }


# --- Ambiguous access readback ------------------------------------------------
#
# The guard against a malformed access list used to be borrowed from
# google-cloud-bigquery, which raised on an entry carrying two identities. From
# 3.44 the library accepts that entry and silently keeps whichever identity it
# resolves last, so the second grantee stays in the payload while being
# invisible to entity_type and entity_id. Reading an ACL you cannot account for
# and then mutating on it is the failure this refuses.


def _two_identity_access() -> list[dict[str, str]]:
    return [
        {
            "role": "READER",
            "userByEmail": "one@example.com",
            "groupByEmail": "two@example.com",
        }
    ]


def test_an_access_entry_carrying_two_identities_is_refused(infra):
    with pytest.raises(ValueError, match="access entry identity ambiguous"):
        infra.validate_raw_access_entries(_two_identity_access())


def test_the_refusal_never_carries_a_grantee(infra):
    """A readback error travels further than the dataset it came from."""
    with pytest.raises(ValueError) as error:
        infra.validate_raw_access_entries(_two_identity_access())
    assert "one@example.com" not in str(error.value)
    assert "two@example.com" not in str(error.value)


@pytest.mark.parametrize(
    "entry",
    [
        {"role": "READER", "userByEmail": "one@example.com"},
        {"role": "WRITER", "groupByEmail": "two@example.com"},
        {"role": "OWNER", "specialGroup": "projectOwners"},
        {"view": {"projectId": "p", "datasetId": "d", "tableId": "t"}},
        {"routine": {"projectId": "p", "datasetId": "d", "routineId": "r"}},
    ],
    ids=["user", "group", "special", "view", "routine"],
)
def test_a_single_identity_entry_is_accepted(infra, entry):
    """The guard must not refuse the shapes the provisioner legitimately reads."""
    infra.validate_raw_access_entries([entry])


def test_an_entry_with_no_identity_at_all_is_refused(infra):
    with pytest.raises(ValueError, match="access entry identity missing"):
        infra.validate_raw_access_entries([{"role": "READER"}])


@pytest.mark.parametrize("raw", ["not a list", 42, None, {"role": "READER"}])
def test_an_access_payload_that_is_not_a_list_is_refused(infra, raw):
    """Asserted on the exact message. A string iterates into characters, so a
    loose match would pass on the per-entry check alone and never prove this
    one ran at all."""
    with pytest.raises(ValueError, match="dataset access list shape mismatch"):
        infra.validate_raw_access_entries(raw)


@pytest.mark.parametrize("entry", [None, ["role"], 42, {1: "x"}])
def test_a_non_mapping_access_entry_is_refused(infra, entry):
    with pytest.raises(ValueError, match="access entry shape mismatch"):
        infra.validate_raw_access_entries([entry])


def test_an_unrecognized_identity_form_is_refused(infra):
    """A grantee form this provisioner has never seen is not a grantee it can
    reason about, so it is refused rather than ignored."""
    with pytest.raises(ValueError, match="access entry identity unrecognized"):
        infra.validate_raw_access_entries([{"role": "READER", "userByFuture": "one@example.com"}])
