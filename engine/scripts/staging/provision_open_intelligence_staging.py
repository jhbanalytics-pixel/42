"""Fail-closed provisioner for approved Open Intelligence staging resources."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

PROJECT = "ogilvy-trends-v2"
LOCATION = "US"
JOB_USER_ROLE = "roles/bigquery.jobUser"
ENGINE_IDENTITY = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
CANARY_IDENTITY = "trends-engine-canary@ogilvy-trends-v2.iam.gserviceaccount.com"
LISTENING_POST_IDENTITY = "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
MAIN_DATASET = "trends_v2_staging"
QA_DATASET = "trends_v2_staging_qa"

_DEDICATED_IDENTITIES = frozenset({ENGINE_IDENTITY, CANARY_IDENTITY, LISTENING_POST_IDENTITY})
_MAPPING_ENTITY_KEYS = {
    "view": frozenset({"projectId", "datasetId", "tableId"}),
    "routine": frozenset({"projectId", "datasetId", "routineId"}),
}


class ProvisioningError(RuntimeError):
    """Raised when desired state or a resource readback cannot be proven."""


@dataclass(frozen=True)
class ServiceAccountSpec:
    account_id: str
    email: str


@dataclass(frozen=True)
class DatasetSpec:
    dataset_id: str
    default_table_expiration_ms: int | None
    default_partition_expiration_ms: int | None
    access_entries: tuple[bigquery.AccessEntry, ...]


@dataclass(frozen=True)
class ProvisionPlan:
    project: str
    location: str
    job_user_role: str
    service_accounts: tuple[ServiceAccountSpec, ...]
    datasets: tuple[DatasetSpec, ...]


@dataclass(frozen=True)
class AccessSnapshot:
    role: str | None
    entity_type: str
    entity_id: str | None
    receipt: str


@dataclass(frozen=True)
class DatasetSnapshot:
    project: str
    dataset_id: str
    location: str | None
    default_table_expiration_ms: int | None
    default_partition_expiration_ms: int | None
    access_entries: tuple[AccessSnapshot, ...]


@dataclass(frozen=True)
class Action:
    action_id: str
    phase: Literal["discover", "mutate", "readback"]
    system: Literal["gcloud", "bigquery"]
    operation: str
    target: str
    command: tuple[str, ...] | None = None
    condition: str | None = None

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.action_id,
            "phase": self.phase,
            "system": self.system,
            "operation": self.operation,
            "target": self.target,
            "command": list(self.command) if self.command is not None else None,
            "condition": self.condition,
        }


@dataclass(frozen=True)
class ActionResult:
    action: Action
    status: Literal["executed", "skipped", "failed"]


@dataclass
class _ApplyState:
    plan: ProvisionPlan
    runner: GcloudRunner
    client_factory: ClientFactory
    accounts: set[str] = field(default_factory=set)
    members: set[str] = field(default_factory=set)
    initial_accounts: set[str] = field(default_factory=set)
    initial_members: set[str] = field(default_factory=set)
    client: Any | None = None
    datasets: dict[str, bigquery.Dataset | None] = field(default_factory=dict)
    snapshots: dict[str, DatasetSnapshot | None] = field(default_factory=dict)
    unrelated_access: dict[str, Counter[str]] = field(default_factory=dict)


GcloudRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
ClientFactory = Callable[[], Any]
ExecutableResolver = Callable[[str], str | None]


def build_plan() -> ProvisionPlan:
    return ProvisionPlan(
        project=PROJECT,
        location=LOCATION,
        job_user_role=JOB_USER_ROLE,
        service_accounts=(
            ServiceAccountSpec("trends-engine-staging", ENGINE_IDENTITY),
            ServiceAccountSpec("trends-engine-canary", CANARY_IDENTITY),
            ServiceAccountSpec("listening-post-staging", LISTENING_POST_IDENTITY),
        ),
        datasets=(
            DatasetSpec(
                MAIN_DATASET,
                None,
                None,
                (
                    bigquery.AccessEntry("WRITER", "userByEmail", ENGINE_IDENTITY),
                    bigquery.AccessEntry("READER", "userByEmail", LISTENING_POST_IDENTITY),
                ),
            ),
            DatasetSpec(
                QA_DATASET,
                None,
                None,
                (bigquery.AccessEntry("WRITER", "userByEmail", CANARY_IDENTITY),),
            ),
        ),
    )


def _access_key(entry: bigquery.AccessEntry) -> tuple[object, object, object]:
    return entry.role, entry.entity_type, entry.entity_id


def _validate_plan(plan: ProvisionPlan) -> None:
    expected = build_plan()
    if plan.project != expected.project:
        raise ProvisioningError("project desired state mismatch")
    if plan.location != expected.location:
        raise ProvisioningError("location desired state mismatch")
    if plan.job_user_role != expected.job_user_role:
        raise ProvisioningError("role desired state mismatch")
    if plan.service_accounts != expected.service_accounts:
        raise ProvisioningError("service account desired state mismatch")
    if tuple(dataset.dataset_id for dataset in plan.datasets) != tuple(
        dataset.dataset_id for dataset in expected.datasets
    ):
        raise ProvisioningError("dataset desired state mismatch")
    for actual, wanted in zip(plan.datasets, expected.datasets, strict=True):
        if (
            actual.default_table_expiration_ms != wanted.default_table_expiration_ms
            or actual.default_partition_expiration_ms != wanted.default_partition_expiration_ms
        ):
            raise ProvisioningError("dataset expiry desired state mismatch")
        if tuple(map(_access_key, actual.access_entries)) != tuple(
            map(_access_key, wanted.access_entries)
        ):
            raise ProvisioningError("dataset access desired state mismatch")


def resolve_gcloud_executable(*, resolver: ExecutableResolver = shutil.which) -> str:
    for candidate in ("gcloud.cmd", "gcloud"):
        executable = resolver(candidate)
        if executable:
            return executable
    raise ProvisioningError("gcloud executable not found")


def _service_account_list_command(plan: ProvisionPlan, executable: str) -> list[str]:
    return [
        executable,
        "iam",
        "service-accounts",
        "list",
        "--project",
        plan.project,
        "--format=json(email)",
    ]


def _service_account_create_command(
    plan: ProvisionPlan, account: ServiceAccountSpec, executable: str
) -> list[str]:
    return [
        executable,
        "iam",
        "service-accounts",
        "create",
        account.account_id,
        "--project",
        plan.project,
        "--quiet",
    ]


def _iam_read_command(plan: ProvisionPlan, executable: str) -> list[str]:
    return [
        executable,
        "projects",
        "get-iam-policy",
        plan.project,
        "--flatten=bindings",
        f"--filter=bindings.role={plan.job_user_role}",
        "--format=json(bindings.role,bindings.members,bindings.condition)",
    ]


def _iam_add_command(plan: ProvisionPlan, email: str, executable: str) -> list[str]:
    return [
        executable,
        "projects",
        "add-iam-policy-binding",
        plan.project,
        "--member",
        f"serviceAccount:{email}",
        "--role",
        plan.job_user_role,
        "--condition=None",
        "--format=none",
        "--quiet",
    ]


def _dataset_ref(plan: ProvisionPlan, spec: DatasetSpec) -> str:
    return f"{plan.project}.{spec.dataset_id}"


def build_actions(
    plan: ProvisionPlan,
    *,
    resolver: ExecutableResolver = shutil.which,
) -> tuple[Action, ...]:
    _validate_plan(plan)
    executable = resolve_gcloud_executable(resolver=resolver)
    actions = [
        Action(
            "discover.service_accounts",
            "discover",
            "gcloud",
            "list_service_accounts",
            plan.project,
            tuple(_service_account_list_command(plan, executable)),
        ),
        Action(
            "discover.job_user_role",
            "discover",
            "gcloud",
            "read_job_user_role",
            plan.project,
            tuple(_iam_read_command(plan, executable)),
        ),
        Action(
            "discover.bigquery_client",
            "discover",
            "bigquery",
            "construct_client",
            plan.project,
        ),
    ]
    actions.extend(
        Action(
            f"discover.dataset.{spec.dataset_id}",
            "discover",
            "bigquery",
            "get_dataset",
            _dataset_ref(plan, spec),
        )
        for spec in plan.datasets
    )
    actions.extend(
        Action(
            f"mutate.service_account.{account.account_id}",
            "mutate",
            "gcloud",
            "create_service_account",
            account.email,
            tuple(_service_account_create_command(plan, account, executable)),
            "service account is missing",
        )
        for account in plan.service_accounts
    )
    actions.extend(
        Action(
            f"mutate.job_user_role.{account.account_id}",
            "mutate",
            "gcloud",
            "add_job_user_member",
            account.email,
            tuple(_iam_add_command(plan, account.email, executable)),
            "unconditional jobUser member is missing",
        )
        for account in plan.service_accounts
    )
    actions.extend(
        Action(
            f"mutate.dataset.{spec.dataset_id}",
            "mutate",
            "bigquery",
            "create_dataset",
            _dataset_ref(plan, spec),
            condition="dataset is missing",
        )
        for spec in plan.datasets
    )
    actions.extend(
        Action(
            f"mutate.access.{spec.dataset_id}",
            "mutate",
            "bigquery",
            "update_dataset_access",
            _dataset_ref(plan, spec),
            condition="approved access is missing",
        )
        for spec in plan.datasets
    )
    actions.extend(
        [
            Action(
                "readback.service_accounts",
                "readback",
                "gcloud",
                "list_service_accounts",
                plan.project,
                tuple(_service_account_list_command(plan, executable)),
            ),
            Action(
                "readback.job_user_role",
                "readback",
                "gcloud",
                "read_job_user_role",
                plan.project,
                tuple(_iam_read_command(plan, executable)),
            ),
        ]
    )
    actions.extend(
        Action(
            f"readback.dataset.{spec.dataset_id}",
            "readback",
            "bigquery",
            "get_dataset",
            _dataset_ref(plan, spec),
        )
        for spec in plan.datasets
    )
    return tuple(actions)


def _run_gcloud(command: list[str]) -> subprocess.CompletedProcess[str]:
    if not isinstance(command, list):
        raise ProvisioningError("gcloud command must use list arguments")
    return subprocess.run(
        command,
        shell=False,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


def _call_gcloud(
    runner: GcloudRunner, command: list[str], action: str
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(command)
    except ProvisioningError:
        raise
    except Exception as exc:
        raise ProvisioningError(f"gcloud {action} failed") from exc
    if not isinstance(getattr(result, "returncode", None), int):
        raise ProvisioningError(f"gcloud {action} returned no status")
    if result.returncode != 0:
        raise ProvisioningError(f"gcloud {action} failed")
    return result


def _json_payload(runner: GcloudRunner, command: list[str], action: str) -> object:
    result = _call_gcloud(runner, command, action)
    stdout = getattr(result, "stdout", None)
    if not isinstance(stdout, str):
        raise ProvisioningError(f"gcloud {action} returned no readback")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProvisioningError(f"gcloud {action} returned malformed JSON") from exc


def _list_service_accounts(
    plan: ProvisionPlan, runner: GcloudRunner, command: list[str]
) -> set[str]:
    payload = _json_payload(runner, command, "service account list")
    if not isinstance(payload, list):
        raise ProvisioningError("service account readback shape mismatch")
    emails: list[str] = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("email"), str):
            raise ProvisioningError("service account readback shape mismatch")
        emails.append(item["email"])
    if len(emails) != len(set(emails)):
        raise ProvisioningError("duplicate service account readback")
    return set(emails)


def _job_user_members(plan: ProvisionPlan, runner: GcloudRunner, command: list[str]) -> set[str]:
    payload = _json_payload(runner, command, "IAM readback")
    if not isinstance(payload, list):
        raise ProvisioningError("IAM readback shape mismatch")
    if len(payload) > 1:
        raise ProvisioningError("duplicate project role binding")
    if not payload:
        return set()
    item = payload[0]
    if not isinstance(item, dict) or not isinstance(item.get("bindings"), dict):
        raise ProvisioningError("IAM readback shape mismatch")
    binding = item["bindings"]
    if binding.get("role") != plan.job_user_role:
        raise ProvisioningError("IAM role readback mismatch")
    if binding.get("condition") not in (None, {}):
        raise ProvisioningError("IAM role binding is conditional")
    members = binding.get("members")
    if not isinstance(members, list) or not all(isinstance(member, str) for member in members):
        raise ProvisioningError("IAM member readback shape mismatch")
    if len(members) != len(set(members)):
        raise ProvisioningError("duplicate project role member")
    return set(members)


def _validate_mapping_identity(entity_type: str, entity_id: object) -> None:
    if not isinstance(entity_id, Mapping):
        raise ValueError("mapping identity required")
    if entity_type in _MAPPING_ENTITY_KEYS:
        required = _MAPPING_ENTITY_KEYS[entity_type]
        if frozenset(entity_id) != required or not all(
            isinstance(entity_id[key], str) and entity_id[key] for key in required
        ):
            raise ValueError("mapping identity shape mismatch")
        return
    if entity_type != "dataset" or frozenset(entity_id) != frozenset({"dataset", "targetTypes"}):
        raise ValueError("dataset identity shape mismatch")
    nested = entity_id["dataset"]
    target_types = entity_id["targetTypes"]
    if (
        not isinstance(nested, Mapping)
        or frozenset(nested) != frozenset({"projectId", "datasetId"})
        or not all(isinstance(nested[key], str) and nested[key] for key in nested)
        or target_types != ["VIEWS"]
    ):
        raise ValueError("dataset identity shape mismatch")


def _parse_access_snapshot(entry: bigquery.AccessEntry) -> AccessSnapshot:
    if not isinstance(entry, bigquery.AccessEntry):
        raise ValueError("access entry class mismatch")
    entity_type = entry.entity_type
    entity_id = entry.entity_id
    role = entry.role
    if not isinstance(entity_type, str) or not entity_type:
        raise ValueError("access entity type missing")
    if entity_type in {*_MAPPING_ENTITY_KEYS, "dataset"}:
        if role is not None:
            raise ValueError("mapping access role must be null")
        _validate_mapping_identity(entity_type, entity_id)
        string_id = None
    else:
        if not isinstance(entity_id, str) or not entity_id:
            raise ValueError("string access identity missing")
        if not isinstance(role, str) or not role:
            raise ValueError("string access role missing")
        string_id = entity_id
    receipt = json.dumps(
        entry.to_api_repr(),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return AccessSnapshot(role, entity_type, string_id, receipt)


# Every identity form a BigQuery access entry can carry. An entry names exactly
# one of these. More than one cannot be acted on, and none cannot be read.
_ACCESS_IDENTITY_KEYS = frozenset(
    {
        "userByEmail",
        "groupByEmail",
        "domain",
        "specialGroup",
        "iamMember",
        "view",
        "routine",
        "dataset",
    }
)
# Keys that qualify an entry rather than name its grantee.
_ACCESS_QUALIFIER_KEYS = frozenset({"role", "condition"})


def validate_raw_access_entries(raw: object) -> None:
    """Refuse an installed access list this provisioner cannot account for.

    google-cloud-bigquery raised on an entry carrying two identities until
    3.44, which accepts it and keeps whichever identity it resolves last. The
    other grantee stays in the payload and never reaches entity_type or
    entity_id, so anything reading only the parsed view would mutate a dataset
    whose ACL it had not actually read. This reads the raw payload instead,
    before the library collapses it, so the outcome is the same on every
    library version rather than borrowed from one.

    No message names a grantee. A readback error travels further than the
    dataset it came from.
    """
    if not isinstance(raw, (list, tuple)):
        raise ValueError("dataset access list shape mismatch")
    for entry in raw:
        if not isinstance(entry, dict) or not all(isinstance(key, str) for key in entry):
            raise ValueError("access entry shape mismatch")
        identities = set(entry) - _ACCESS_QUALIFIER_KEYS
        if identities - _ACCESS_IDENTITY_KEYS:
            raise ValueError("access entry identity unrecognized")
        if len(identities) > 1:
            raise ValueError("access entry identity ambiguous")
        if not identities:
            raise ValueError("access entry identity missing")


def parse_dataset_snapshot(dataset: bigquery.Dataset) -> DatasetSnapshot:
    """Convert an installed Dataset resource into one immutable safe snapshot."""
    try:
        if not isinstance(dataset, bigquery.Dataset):
            raise ValueError("dataset class mismatch")
        project = dataset.project
        dataset_id = dataset.dataset_id
        location = dataset.location
        table_expiry = dataset.default_table_expiration_ms
        partition_expiry = dataset.default_partition_expiration_ms
        if not isinstance(project, str) or not project:
            raise ValueError("dataset project missing")
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ValueError("dataset ID missing")
        if location is not None and not isinstance(location, str):
            raise ValueError("dataset location shape mismatch")
        for expiry in (table_expiry, partition_expiry):
            if expiry is not None and (not isinstance(expiry, int) or isinstance(expiry, bool)):
                raise ValueError("dataset expiry shape mismatch")
        # Read the raw list before the library parses it, so a two-identity
        # entry is refused on every version rather than only where the
        # vendor happens to raise.
        validate_raw_access_entries(dataset.to_api_repr().get("access", []))
        access_entries = tuple(_parse_access_snapshot(entry) for entry in dataset.access_entries)
        return DatasetSnapshot(
            project,
            dataset_id,
            location,
            table_expiry,
            partition_expiry,
            access_entries,
        )
    except ProvisioningError:
        raise
    except Exception:
        raise ProvisioningError("dataset readback malformed") from None


def _verify_dataset_metadata(
    plan: ProvisionPlan, spec: DatasetSpec, snapshot: DatasetSnapshot
) -> None:
    if (
        snapshot.project != plan.project
        or snapshot.dataset_id != spec.dataset_id
        or snapshot.location != plan.location
        or snapshot.default_table_expiration_ms != spec.default_table_expiration_ms
        or snapshot.default_partition_expiration_ms != spec.default_partition_expiration_ms
    ):
        raise ProvisioningError("dataset metadata readback mismatch")


def _desired_access_snapshots(spec: DatasetSpec) -> tuple[AccessSnapshot, ...]:
    try:
        return tuple(_parse_access_snapshot(entry) for entry in spec.access_entries)
    except ProvisioningError:
        raise
    except Exception:
        raise ProvisioningError("desired dataset access malformed") from None


def _verify_dedicated_access(
    spec: DatasetSpec,
    snapshot: DatasetSnapshot,
    *,
    require_all: bool,
) -> None:
    expected_entries = _desired_access_snapshots(spec)
    expected = {
        entry.entity_id: (entry.role, entry.entity_type, entry.entity_id)
        for entry in expected_entries
    }
    seen: Counter[str] = Counter()
    for entry in snapshot.access_entries:
        identity = entry.entity_id
        if identity not in _DEDICATED_IDENTITIES:
            continue
        if identity is None:
            raise ProvisioningError("dedicated identity resolution failed")
        if identity not in expected:
            raise ProvisioningError("cross-boundary dedicated access detected")
        if (entry.role, entry.entity_type, entry.entity_id) != expected[identity]:
            raise ProvisioningError("dedicated access shape mismatch")
        seen[identity] += 1
        if seen[identity] > 1:
            raise ProvisioningError("dedicated access duplicate detected")
    if require_all and set(seen) != set(expected):
        raise ProvisioningError("dedicated access readback missing")


def _unrelated_access_receipts(snapshot: DatasetSnapshot) -> Counter[str]:
    return Counter(
        entry.receipt
        for entry in snapshot.access_entries
        if entry.entity_id not in _DEDICATED_IDENTITIES
    )


def _read_dataset(
    client: Any, dataset_ref: str, *, missing_allowed: bool
) -> bigquery.Dataset | None:
    try:
        dataset = client.get_dataset(dataset_ref)
    except NotFound:
        if missing_allowed:
            return None
        raise ProvisioningError("dataset readback missing") from None
    except Exception:
        raise ProvisioningError("BigQuery dataset readback failed") from None
    if not isinstance(dataset, bigquery.Dataset):
        raise ProvisioningError("dataset readback malformed")
    return dataset


def _spec_for_target(plan: ProvisionPlan, target: str) -> DatasetSpec:
    for spec in plan.datasets:
        if _dataset_ref(plan, spec) == target:
            return spec
    raise ProvisioningError("action dataset target mismatch")


def _entries_from_snapshot(
    snapshot: DatasetSnapshot,
) -> list[bigquery.AccessEntry]:
    try:
        return [
            bigquery.AccessEntry.from_api_repr(json.loads(entry.receipt))
            for entry in snapshot.access_entries
        ]
    except Exception:
        raise ProvisioningError("dataset access reconstruction failed") from None


def _required_access_missing(
    spec: DatasetSpec, snapshot: DatasetSnapshot
) -> list[bigquery.AccessEntry]:
    current = {entry.receipt for entry in snapshot.access_entries}
    missing = []
    for entry, parsed in zip(spec.access_entries, _desired_access_snapshots(spec), strict=True):
        if parsed.receipt not in current:
            missing.append(entry)
    return missing


def _execute_action(action: Action, state: _ApplyState) -> str:
    plan = state.plan
    command = list(action.command) if action.command is not None else None
    if action.operation == "list_service_accounts":
        if command is None:
            raise ProvisioningError("provisioning action carries no command")
        accounts = _list_service_accounts(plan, state.runner, command)
        if action.phase == "discover":
            state.accounts = set(accounts)
            state.initial_accounts = set(accounts)
        else:
            wanted = {account.email for account in plan.service_accounts}
            if not state.initial_accounts.issubset(accounts):
                raise ProvisioningError("unrelated service account readback changed")
            if not wanted.issubset(accounts):
                raise ProvisioningError("service account readback missing")
            state.accounts = set(accounts)
        return "executed"

    if action.operation == "read_job_user_role":
        if command is None:
            raise ProvisioningError("provisioning action carries no command")
        members = _job_user_members(plan, state.runner, command)
        if action.phase == "discover":
            state.members = set(members)
            state.initial_members = set(members)
        else:
            wanted = {f"serviceAccount:{account.email}" for account in plan.service_accounts}
            if not state.initial_members.issubset(members):
                raise ProvisioningError("unrelated project IAM member changed")
            if not wanted.issubset(members):
                raise ProvisioningError("project IAM readback missing")
            state.members = set(members)
        return "executed"

    if action.operation == "construct_client":
        try:
            state.client = state.client_factory()
        except Exception:
            raise ProvisioningError("BigQuery client construction failed") from None
        if getattr(state.client, "project", None) != plan.project:
            raise ProvisioningError("BigQuery client project mismatch")
        return "executed"

    if action.operation == "get_dataset":
        if state.client is None:
            raise ProvisioningError("BigQuery client missing before dataset read")
        spec = _spec_for_target(plan, action.target)
        dataset = _read_dataset(
            state.client,
            action.target,
            missing_allowed=action.phase == "discover",
        )
        if dataset is None:
            state.datasets[spec.dataset_id] = None
            state.snapshots[spec.dataset_id] = None
            state.unrelated_access[spec.dataset_id] = Counter()
            return "executed"
        snapshot = parse_dataset_snapshot(dataset)
        _verify_dataset_metadata(plan, spec, snapshot)
        _verify_dedicated_access(spec, snapshot, require_all=action.phase == "readback")
        if action.phase == "discover":
            state.unrelated_access[spec.dataset_id] = _unrelated_access_receipts(snapshot)
        else:
            final_unrelated = _unrelated_access_receipts(snapshot)
            if state.unrelated_access[spec.dataset_id] - final_unrelated:
                raise ProvisioningError("unrelated access readback changed")
        state.datasets[spec.dataset_id] = dataset
        state.snapshots[spec.dataset_id] = snapshot
        return "executed"

    if action.operation == "create_service_account":
        if action.target in state.accounts:
            return "skipped"
        if command is None:
            raise ProvisioningError("provisioning action carries no command")
        _call_gcloud(state.runner, command, "service account create")
        state.accounts.add(action.target)
        return "executed"

    if action.operation == "add_job_user_member":
        member = f"serviceAccount:{action.target}"
        if member in state.members:
            return "skipped"
        if command is None:
            raise ProvisioningError("provisioning action carries no command")
        _call_gcloud(state.runner, command, "IAM binding add")
        state.members.add(member)
        return "executed"

    if action.operation == "create_dataset":
        if state.client is None:
            raise ProvisioningError("BigQuery client missing before dataset create")
        spec = _spec_for_target(plan, action.target)
        if state.snapshots[spec.dataset_id] is not None:
            return "skipped"
        dataset = bigquery.Dataset(action.target)
        dataset.location = plan.location
        dataset.default_table_expiration_ms = spec.default_table_expiration_ms
        dataset.default_partition_expiration_ms = spec.default_partition_expiration_ms
        parse_dataset_snapshot(dataset)
        try:
            created = state.client.create_dataset(dataset)
        except Exception:
            raise ProvisioningError("BigQuery dataset creation failed") from None
        snapshot = parse_dataset_snapshot(created)
        _verify_dataset_metadata(plan, spec, snapshot)
        _verify_dedicated_access(spec, snapshot, require_all=False)
        state.datasets[spec.dataset_id] = created
        state.snapshots[spec.dataset_id] = snapshot
        return "executed"

    if action.operation == "update_dataset_access":
        if state.client is None:
            raise ProvisioningError("BigQuery client missing before access update")
        spec = _spec_for_target(plan, action.target)
        dataset = state.datasets[spec.dataset_id]
        snapshot = state.snapshots[spec.dataset_id]
        if dataset is None or snapshot is None:
            raise ProvisioningError("dataset missing before access update")
        missing = _required_access_missing(spec, snapshot)
        if not missing:
            return "skipped"
        try:
            dataset.access_entries = [
                *_entries_from_snapshot(snapshot),
                *missing,
            ]
            updated = state.client.update_dataset(dataset, ["access_entries"])
        except ProvisioningError:
            raise
        except Exception:
            raise ProvisioningError("BigQuery dataset access update failed") from None
        updated_snapshot = parse_dataset_snapshot(updated)
        _verify_dataset_metadata(plan, spec, updated_snapshot)
        _verify_dedicated_access(spec, updated_snapshot, require_all=True)
        state.datasets[spec.dataset_id] = updated
        state.snapshots[spec.dataset_id] = updated_snapshot
        return "executed"

    raise ProvisioningError("unknown provisioning action")


def apply_plan(
    plan: ProvisionPlan,
    *,
    runner: GcloudRunner = _run_gcloud,
    client_factory: ClientFactory | None = None,
    resolver: ExecutableResolver = shutil.which,
    trace: list[ActionResult] | None = None,
) -> tuple[ActionResult, ...]:
    actions = build_actions(plan, resolver=resolver)
    if client_factory is None:

        def client_factory() -> bigquery.Client:
            return bigquery.Client(project=plan.project)

    state = _ApplyState(plan, runner, client_factory)
    results: list[ActionResult] = []
    for action in actions:
        try:
            status = _execute_action(action, state)
        except Exception:
            failed = ActionResult(action, "failed")
            results.append(failed)
            if trace is not None:
                trace.append(failed)
            raise
        result = ActionResult(action, status)
        results.append(result)
        if trace is not None:
            trace.append(result)
    return tuple(results)


def render_dry_run(
    plan: ProvisionPlan,
    *,
    resolver: ExecutableResolver = shutil.which,
) -> str:
    actions = build_actions(plan, resolver=resolver)
    lines = [
        "Open Intelligence staging infrastructure",
        "Mode: DRY RUN",
        f"Project: {plan.project}",
        f"Location: {plan.location}",
        "Service accounts:",
    ]
    lines.extend(f"  {account.email}" for account in plan.service_accounts)
    lines.append("Datasets:")
    for spec in plan.datasets:
        lines.append(
            f"  {plan.project}.{spec.dataset_id}, default table expiry null, "
            "default partition expiry null"
        )
        for entry in spec.access_entries:
            lines.append(f"    {entry.role} {entry.entity_type} {entry.entity_id}")
    lines.extend(
        [
            f"Project IAM: {plan.job_user_role} for all three service accounts",
            "Forward actions:",
        ]
    )
    lines.extend(f"ACTION {json.dumps(action.to_record(), sort_keys=True)}" for action in actions)
    lines.extend(
        [
            "Readback checks:",
            "  Exact service account emails and unconditional project jobUser grants.",
            "  Exact dataset project, IDs, US location, and null default expiries.",
            "  Required access, prohibited cross access, and unrelated entry retention.",
            "Rollback plan:",
            "  This script cannot execute rollback.",
            "  Dataset deletion, service account deletion, and role removal require "
            "separate destructive authorization.",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or apply approved Open Intelligence staging resources."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the approved additive provisioning plan.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: GcloudRunner = _run_gcloud,
    client_factory: ClientFactory | None = None,
    resolver: ExecutableResolver = shutil.which,
) -> int:
    args = _parser().parse_args(argv)
    plan = build_plan()
    if not args.apply:
        print(render_dry_run(plan, resolver=resolver))
        return 0
    apply_plan(
        plan,
        runner=runner,
        client_factory=client_factory,
        resolver=resolver,
    )
    print("Apply readback complete for the approved staging resources.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProvisioningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
