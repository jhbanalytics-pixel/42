"""Closed, read-only CLI for the staging Intelligence Brain runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from importlib import metadata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from google.cloud import bigquery
from src.analysis.open_intelligence import execution_approval, execution_generations
from src.analysis.open_intelligence.brain import _run_intelligence_brain_from_authority
from src.analysis.open_intelligence.brain_authority import (
    _issue_live_brain_evidence_snapshot,
    _issue_live_brain_runtime_request,
)
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.brain_live_reader import (
    RUNTIME_CONTRACT_VERSION,
    SOURCE_COPY_RECEIPT_FIELDS,
    TARGET_LOCATION,
    TARGET_PROJECT,
    LiveBrainReadRefusal,
    read_live_brain_authority,
)
from src.analysis.open_intelligence.execution_origins import select_origin
from src.analysis.open_intelligence.run_receipts import (
    RUN_RECEIPT_ROW_FIELDS,
    build_run_receipt,
)

# The Brain runtime contract the deployer stamps into the job annotations. Job,
# principal, repository and image namespace are not spelled here: they come from the
# brain_read binding of the active trusted generation's fresh origin.
_CONTRACT_SHA256 = "f62be04fd31f6236855c05e5907d87f87b5d89f644512c2391420d46da6b4962"
_BRAIN_SCRIPT = "scripts/staging/run_live_intelligence_brain.py"
_MANIFEST_VERSION_V2 = "open_intelligence_execution_manifest_v2"
_ANNOTATIONS = {
    "contract_sha256": "42.ogilvy/contract-sha256",
    "dependency_lock_sha256": "42.ogilvy/dependency-lock-sha256",
    "runtime_sbom_sha256": "42.ogilvy/runtime-sbom-sha256",
    "source_sha": "42.ogilvy/source-sha",
    "deployment_manifest_sha256": "42.ogilvy/deployment-manifest-sha256",
    "execution_approval_sha256": "42.ogilvy/execution-approval-sha256",
    "origin_registry_sha256": "42.ogilvy/origin-registry-sha256",
    "resource_manifest_sha256": "42.ogilvy/resource-manifest-sha256",
}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
_RUNTIME_LOCK_PATH = Path("/app/requirements.lock")
_RUNTIME_INVENTORY_PATH = Path("/app/runtime-distributions.json")
_RUNTIME_INVENTORY_DIGEST_PATH = Path("/app/runtime-distributions.sha256")
_BASE_IMAGE_SUPPLIED = {"pip": "26.2.1"}
_LOCAL_PROJECT = {"trends-engine-v2": "0.1.0"}

_ERROR_CODES = frozenset(
    {
        "invalid_arguments",
        "identity_refused",
        "receipt_refused",
        "readback_refused",
        "signal_refused",
        "future_data_refused",
        "semantic_refused",
        "side_effect_refused",
        "internal_refused",
    }
)
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}\Z")
_SIGNAL_ID = re.compile(r"sig_[0-9a-f]{64}\Z")
_DURABLE_ARTIFACT_CONTEXT = None


class _ArgumentRefusal(ValueError):
    def __init__(self, values):
        super().__init__("invalid arguments")
        self.values = values


class RuntimeIdentityRefusal(LiveBrainReadRefusal):
    def __init__(self, detail: str):
        super().__init__("identity_refused", detail)


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    source_sha: str
    dependency_lock_sha256: str
    runtime_sbom_sha256: str
    deployment_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class BrainRuntimeProfile:
    job_resource: str
    service_identity: str
    connected_repo: str
    image_repository: str
    image_uri_regex: str
    origin_registry_sha256: str
    resource_manifest_sha256: str
    manifest_version: str
    secret_versions: tuple[tuple[str, str], ...]


def _brain_profile(generation=None) -> BrainRuntimeProfile:
    """The one Brain profile of a trusted generation.

    Job, principal, repository and image namespace are the brain_read binding of the
    generation's fresh origin, admitted for consumption. The Brain job binds no secret,
    so the deployer's resolved secret versions are the empty tuple.
    """
    if generation is None:
        generation = execution_generations.active_generation()
    registry = generation.registry
    rows = [
        origin
        for origin in registry.values()
        if origin.manifest_version == _MANIFEST_VERSION_V2
        and "brain_read" in origin.operation_bindings
    ]
    if len(rows) != 1:
        raise RuntimeIdentityRefusal("no single fresh origin binds brain_read")
    try:
        origin = select_origin(
            manifest_version=rows[0].manifest_version,
            contract_sha256=rows[0].contract_sha256,
            mode="new_consume",
            registry=registry,
        )
    except Exception as error:
        raise RuntimeIdentityRefusal("fresh Brain origin is not admitted") from error
    binding = origin.operation_bindings["brain_read"]
    return BrainRuntimeProfile(
        job_resource=binding.job_resource,
        service_identity=binding.service_identity,
        connected_repo=origin.connected_repo,
        image_repository=origin.image_repository,
        image_uri_regex=origin.image_uri_regex,
        origin_registry_sha256=generation.origin_registry_sha256,
        resource_manifest_sha256=generation.resource_manifest_sha256,
        manifest_version=_MANIFEST_VERSION_V2,
        secret_versions=(),
    )


def _require_profile_authority(profile: BrainRuntimeProfile, authority: object) -> None:
    """The issued authority must be the profile's: same generation, job, principal, image."""
    generation = getattr(authority, "generation", None)
    manifest = getattr(authority, "manifest", None)
    approval = getattr(authority, "approval", None)
    pair = (
        getattr(generation, "origin_registry_sha256", None),
        getattr(generation, "resource_manifest_sha256", None),
    )
    image_uri = getattr(authority, "image_uri", None)
    if (
        type(approval) is not execution_approval.ExecutionApprovalV2
        or pair != (profile.origin_registry_sha256, profile.resource_manifest_sha256)
        or (approval.origin_registry_sha256, approval.resource_manifest_sha256) != pair
        or getattr(authority, "operation", None) != "brain_read"
        or getattr(authority, "job_resource", None) != profile.job_resource
        or getattr(manifest, "job_resource", None) != profile.job_resource
        or getattr(manifest, "service_identity", None) != profile.service_identity
        or not isinstance(image_uri, str)
        or re.fullmatch(profile.image_uri_regex, image_uri) is None
    ):
        raise RuntimeIdentityRefusal("durable Brain authority differs from the origin profile")


@dataclass(frozen=True, slots=True)
class BrainControlInputs:
    receipt: object
    artifacts: Mapping[str, bytes]


@dataclass(slots=True)
class _BrainArtifactProvider:
    client: object
    run_id: str
    signal_id: str
    research_depth: str
    decision_question: str | None
    inputs: BrainControlInputs | None = None

    def read(self, name: str) -> bytes:
        if self.inputs is None:
            self.inputs = _read_brain_control_artifacts(
                client=self.client,
                run_id=self.run_id,
                signal_id=self.signal_id,
                research_depth=self.research_depth,
                decision_question=self.decision_question,
            )
        if name not in self.inputs.artifacts:
            raise RuntimeIdentityRefusal("durable Brain artifact is unavailable")
        return self.inputs.artifacts[name]


def _execution_approval_artifact_bytes(name: str) -> bytes:
    if not isinstance(_DURABLE_ARTIFACT_CONTEXT, _BrainArtifactProvider):
        raise RuntimeIdentityRefusal("durable Brain artifact is unavailable")
    return _DURABLE_ARTIFACT_CONTEXT.read(name)


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _lock_versions(lock_bytes: bytes) -> dict[str, str]:
    try:
        text = lock_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeIdentityRefusal("runtime dependency lock is not UTF-8") from error
    if "\r" in text:
        raise RuntimeIdentityRefusal("runtime dependency lock line endings differ")
    versions: dict[str, str] = {}
    current_has_hash: bool | None = None
    for line in text.splitlines():
        match = re.match(r"^([a-z0-9][a-z0-9._-]*)==([^ ;\\]+)", line)
        if match:
            if current_has_hash is False:
                raise RuntimeIdentityRefusal("runtime dependency lock entry has no hash")
            name = _normalized_name(match.group(1))
            if name in versions:
                raise RuntimeIdentityRefusal("runtime dependency lock contains duplicate names")
            versions[name] = match.group(2)
            current_has_hash = "--hash=sha256:" in line
            continue
        if current_has_hash is not None and "--hash=" in line:
            if re.search(r"--hash=sha256:[0-9a-f]{64}(?:\s|\\|\Z)", line) is None:
                raise RuntimeIdentityRefusal("runtime dependency lock hash is invalid")
            current_has_hash = True
    if current_has_hash is False:
        raise RuntimeIdentityRefusal("runtime dependency lock entry has no hash")
    if not versions or "--hash=sha256:" not in text or " @ " in text or "-e " in text:
        raise RuntimeIdentityRefusal("runtime dependency lock is not closed")
    return versions


def _installed_distributions() -> tuple[tuple[str, str], ...]:
    return tuple((item.metadata["Name"], item.version) for item in metadata.distributions())


def _runtime_inventory_payload(*, distributions, locked_versions) -> bytes:
    observed: dict[str, str] = {}
    for raw_name, version in distributions:
        name = _normalized_name(raw_name)
        if name in observed or not isinstance(version, str) or not version:
            raise RuntimeIdentityRefusal("installed distribution inventory is ambiguous")
        observed[name] = version
    expected = {**locked_versions, **_BASE_IMAGE_SUPPLIED, **_LOCAL_PROJECT}
    if observed != expected:
        unexpected = set(observed) - set(expected)
        if unexpected:
            raise RuntimeIdentityRefusal("base image supplied distribution differs")
        raise RuntimeIdentityRefusal("installed distribution inventory differs from lock")
    rows = []
    for name in sorted(observed):
        if name in _BASE_IMAGE_SUPPLIED:
            source = "base_image_supplied"
        elif name in _LOCAL_PROJECT:
            source = "local_project"
        else:
            source = "locked"
        rows.append({"name": name, "source": source, "version": observed[name]})
    return json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_only(path: Path) -> bool:
    return path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0


def _verify_runtime_files(
    *,
    lock_path: Path,
    inventory_path: Path,
    inventory_digest_path: Path,
    dependency_lock_sha256: str,
    runtime_sbom_sha256: str,
    distributions,
) -> dict[str, str]:
    try:
        lock_bytes = lock_path.read_bytes()
        inventory_bytes = inventory_path.read_bytes()
        inventory_digest_text = inventory_digest_path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as error:
        raise RuntimeIdentityRefusal("runtime identity files are unavailable") from error
    lock_digest = hashlib.sha256(lock_bytes).hexdigest()
    if lock_digest != dependency_lock_sha256 or _DIGEST.fullmatch(dependency_lock_sha256) is None:
        raise RuntimeIdentityRefusal("runtime dependency lock digest differs")
    locked_versions = _lock_versions(lock_bytes)
    recomputed_inventory = _runtime_inventory_payload(
        distributions=distributions,
        locked_versions=locked_versions,
    )
    if recomputed_inventory != inventory_bytes:
        raise RuntimeIdentityRefusal("runtime distribution inventory differs")
    inventory_digest = hashlib.sha256(inventory_bytes).hexdigest()
    if (
        inventory_digest_text != inventory_digest + "\n"
        or inventory_digest != runtime_sbom_sha256
        or _DIGEST.fullmatch(runtime_sbom_sha256) is None
    ):
        raise RuntimeIdentityRefusal("runtime inventory digest differs")
    if not _read_only(inventory_path) or not _read_only(inventory_digest_path):
        raise RuntimeIdentityRefusal("runtime inventory files are writable")
    return {
        "dependency_lock_sha256": lock_digest,
        "runtime_sbom_sha256": inventory_digest,
    }


def _write_runtime_inventory() -> None:
    lock_bytes = _RUNTIME_LOCK_PATH.read_bytes()
    payload = _runtime_inventory_payload(
        distributions=_installed_distributions(),
        locked_versions=_lock_versions(lock_bytes),
    )
    _RUNTIME_INVENTORY_PATH.write_bytes(payload)
    _RUNTIME_INVENTORY_DIGEST_PATH.write_text(
        hashlib.sha256(payload).hexdigest() + "\n",
        encoding="ascii",
        newline="\n",
    )


def _mapping_value(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeIdentityRefusal(f"control-plane {field} is invalid")
    return value


def _task_authority(resource: Mapping[str, object], *, execution: bool) -> dict[str, object]:
    template = _mapping_value(resource.get("template"), "template")
    if not execution:
        template = _mapping_value(template.get("template"), "task template")
    containers = template.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise RuntimeIdentityRefusal("control-plane container authority is ambiguous")
    container = _mapping_value(containers[0], "container")
    environment = container.get("env", [])
    if not isinstance(environment, list):
        raise RuntimeIdentityRefusal("control-plane environment is invalid")
    environment_values = {}
    secret_names = []
    for item in environment:
        entry = _mapping_value(item, "environment entry")
        name = entry.get("name")
        if not isinstance(name, str) or name in environment_values:
            raise RuntimeIdentityRefusal("control-plane environment is ambiguous")
        if "valueSource" in entry or "valueFrom" in entry:
            secret_names.append(name)
        elif not isinstance(entry.get("value"), str):
            raise RuntimeIdentityRefusal("control-plane environment value is invalid")
        else:
            environment_values[name] = entry["value"]
    resources = _mapping_value(container.get("resources"), "resources")
    limits = _mapping_value(resources.get("limits"), "resource limits")
    return {
        "image": container.get("image"),
        "container_command": tuple(container.get("command", ())),
        "container_args": tuple(container.get("args", ())),
        "service_identity": template.get("serviceAccount"),
        "max_retries": template.get("maxRetries"),
        "environment": environment_values,
        "secret_names": tuple(sorted(secret_names)),
        "memory": limits.get("memory"),
        "timeout": template.get("timeout"),
        "writable_code_mounts": tuple(container.get("volumeMounts", ())),
        "volumes": tuple(template.get("volumes", ())),
    }


def _deployment_manifest(
    *,
    annotations: Mapping[str, object],
    task: Mapping[str, object],
    invocation_arguments,
    profile: BrainRuntimeProfile,
) -> dict[str, object]:
    # Mirrors the deployer's brain_deployment_manifest for a v2 job: the v1 keys plus
    # manifest_version and the resolved secret versions.
    return {
        "contract_sha256": annotations[_ANNOTATIONS["contract_sha256"]],
        "dependency_lock_sha256": annotations[_ANNOTATIONS["dependency_lock_sha256"]],
        "runtime_sbom_sha256": annotations[_ANNOTATIONS["runtime_sbom_sha256"]],
        "source_sha": annotations[_ANNOTATIONS["source_sha"]],
        "job_resource": profile.job_resource,
        "region": "us-central1",
        "image_uri_with_digest": task["image"],
        "service_identity": task["service_identity"],
        "command": f"{task['container_command'][0]} {_BRAIN_SCRIPT}",
        "arguments": tuple(invocation_arguments),
        "environment_names": tuple(sorted(task["environment"])),
        "secret_names": task["secret_names"],
        "memory": task["memory"],
        "timeout_seconds": 900,
        "max_retries": task["max_retries"],
        "writable_code_mounts": task["writable_code_mounts"],
        "operation": "brain_read",
        "manifest_version": profile.manifest_version,
        "secret_versions": profile.secret_versions,
    }


def _verify_runtime_identity(
    *, payload, invocation_arguments, distributions=None, profile=None
) -> RuntimeIdentity:
    if profile is None:
        profile = _brain_profile()
    root = _mapping_value(payload, "readback")
    execution = _mapping_value(root.get("execution"), "execution")
    job = _mapping_value(root.get("job"), "job")
    execution_name = execution.get("name")
    if not isinstance(execution_name, str) or "/locations/us-central1/" not in execution_name:
        raise RuntimeIdentityRefusal("control-plane execution identity differs")
    try:
        execution_job = execution_approval.canonical_execution_job(
            execution.get("job"), profile.job_resource
        )
    except execution_approval.ApprovalRefusal as exc:
        raise RuntimeIdentityRefusal("control-plane parent job differs") from exc
    if execution_job != profile.job_resource or job.get("name") != profile.job_resource:
        raise RuntimeIdentityRefusal("control-plane parent job differs")
    execution_annotations = _mapping_value(execution.get("annotations"), "annotations")
    job_template = _mapping_value(job.get("template"), "job template")
    job_annotations = _mapping_value(job_template.get("annotations"), "job annotations")
    if execution_annotations != job_annotations:
        raise RuntimeIdentityRefusal("control-plane annotation drift")
    annotations = execution_annotations
    required = tuple(_ANNOTATIONS.values())
    if set(annotations) != set(required):
        raise RuntimeIdentityRefusal("control-plane annotations are incomplete")
    if annotations[_ANNOTATIONS["contract_sha256"]] != _CONTRACT_SHA256:
        raise RuntimeIdentityRefusal("control-plane contract annotation differs")
    if (
        annotations[_ANNOTATIONS["origin_registry_sha256"]],
        annotations[_ANNOTATIONS["resource_manifest_sha256"]],
    ) != (profile.origin_registry_sha256, profile.resource_manifest_sha256):
        raise RuntimeIdentityRefusal("control-plane generation annotations differ")
    if _SOURCE_SHA.fullmatch(str(annotations[_ANNOTATIONS["source_sha"]])) is None:
        raise RuntimeIdentityRefusal("control-plane source SHA is invalid")
    for field in ("dependency_lock_sha256", "runtime_sbom_sha256", "deployment_manifest_sha256"):
        if _DIGEST.fullmatch(str(annotations[_ANNOTATIONS[field]])) is None:
            raise RuntimeIdentityRefusal("control-plane digest annotation is invalid")
    execution_task = _task_authority(execution, execution=True)
    job_task = _task_authority(job, execution=False)
    if execution_task != job_task:
        raise RuntimeIdentityRefusal("current Job and Execution authority differ")
    expected_args = (_BRAIN_SCRIPT, *tuple(invocation_arguments))
    if (
        execution_task["container_command"] != ("python",)
        or execution_task["container_args"] != expected_args
        or re.fullmatch(profile.image_uri_regex, str(execution_task["image"])) is None
        or execution_task["service_identity"] != profile.service_identity
        or execution_task["max_retries"] != 0
        or execution_task["environment"] != dict(execution_approval._DEFAULT_ENVIRONMENT)
        or execution_task["secret_names"]
        or execution_task["memory"] != "4Gi"
        or execution_task["timeout"] != "900s"
        or execution_task["writable_code_mounts"]
        or execution_task["volumes"]
    ):
        raise RuntimeIdentityRefusal("control-plane runtime configuration differs")
    manifest = _deployment_manifest(
        annotations=annotations,
        task=execution_task,
        invocation_arguments=invocation_arguments,
        profile=profile,
    )
    manifest_digest = canonical_digest(manifest)
    if manifest_digest != annotations[_ANNOTATIONS["deployment_manifest_sha256"]]:
        raise RuntimeIdentityRefusal("control-plane deployment manifest differs")
    _verify_runtime_files(
        lock_path=_RUNTIME_LOCK_PATH,
        inventory_path=_RUNTIME_INVENTORY_PATH,
        inventory_digest_path=_RUNTIME_INVENTORY_DIGEST_PATH,
        dependency_lock_sha256=annotations[_ANNOTATIONS["dependency_lock_sha256"]],
        runtime_sbom_sha256=annotations[_ANNOTATIONS["runtime_sbom_sha256"]],
        distributions=_installed_distributions() if distributions is None else distributions,
    )
    return RuntimeIdentity(
        source_sha=annotations[_ANNOTATIONS["source_sha"]],
        dependency_lock_sha256=annotations[_ANNOTATIONS["dependency_lock_sha256"]],
        runtime_sbom_sha256=annotations[_ANNOTATIONS["runtime_sbom_sha256"]],
        deployment_manifest_sha256=manifest_digest,
    )


def _cloud_run_control_plane_reader(execution_name: str, expected_job: str):
    from google.auth import default
    from google.auth.transport.requests import AuthorizedSession

    credentials, _project = default()
    session = AuthorizedSession(credentials)
    execution_response = session.get(f"https://run.googleapis.com/v2/{execution_name}")
    execution_response.raise_for_status()
    execution = execution_response.json()
    try:
        job_resource = execution_approval.canonical_execution_job(
            execution.get("job"), expected_job
        )
    except execution_approval.ApprovalRefusal as exc:
        raise RuntimeIdentityRefusal("control-plane parent job is unavailable") from exc
    job_response = session.get(f"https://run.googleapis.com/v2/{job_resource}")
    job_response.raise_for_status()
    return {"execution": execution, "job": job_response.json()}


def _default_runtime_identity_verifier(invocation_arguments):
    # Cloud Run hands the job and execution over as bare names; the control
    # plane answers only for the full execution resource under the Brain job.
    profile = _brain_profile()
    job_name = os.environ.get("CLOUD_RUN_JOB")
    execution_name = os.environ.get("CLOUD_RUN_EXECUTION")
    if not job_name or not execution_name or "/" in job_name or "/" in execution_name:
        raise RuntimeIdentityRefusal("Cloud Run execution identity is unavailable")
    if profile.job_resource.rsplit("/", 1)[1] != job_name:
        raise RuntimeIdentityRefusal("Cloud Run job identity differs")
    return _verify_runtime_identity(
        payload=_cloud_run_control_plane_reader(
            f"{profile.job_resource}/executions/{execution_name}", profile.job_resource
        ),
        invocation_arguments=invocation_arguments,
        profile=profile,
    )


def _plain(value):
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _parse(argv):
    values = {
        "target": None,
        "run_id": None,
        "signal_id": None,
        "research_depth": None,
        "decision_question": None,
    }
    options = {
        "--target": "target",
        "--run-id": "run_id",
        "--signal-id": "signal_id",
        "--research-depth": "research_depth",
        "--decision-question": "decision_question",
    }
    index = 0
    while index < len(argv):
        field = options.get(argv[index])
        if field is None or index + 1 >= len(argv) or values[field] is not None:
            raise _ArgumentRefusal(values)
        values[field] = argv[index + 1]
        index += 2
    if (
        values["target"] != "staging"
        or not isinstance(values["run_id"], str)
        or _RUN_ID.fullmatch(values["run_id"]) is None
        or not isinstance(values["signal_id"], str)
        or _SIGNAL_ID.fullmatch(values["signal_id"]) is None
        or values["research_depth"] not in {"briefing", "scan", "investigation"}
        or (values["research_depth"] != "briefing" and not values["decision_question"])
    ):
        raise _ArgumentRefusal(values)
    return values


def _control_query(client: object, sql: str, *, parameters=(), max_results: int):
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            use_legacy_sql=False,
            query_parameters=list(parameters),
        ),
        location=TARGET_LOCATION,
        retry=None,
        job_retry=None,
    )
    if getattr(job, "statement_type", "SELECT") != "SELECT":
        raise LiveBrainReadRefusal("side_effect_refused", "only SELECT jobs are permitted")
    return tuple(job.result(max_results=max_results, retry=None, job_retry=None))


def _read_brain_control_artifacts(
    *,
    client: object,
    run_id: str,
    signal_id: str,
    research_depth: str,
    decision_question: str | None,
) -> BrainControlInputs:
    receipt_fields = ", ".join(f"`{field}`" for field in RUN_RECEIPT_ROW_FIELDS)
    rows = _control_query(
        client,
        (
            f"SELECT {receipt_fields} FROM `{TARGET_PROJECT}.trends_v2_staging."
            "open_intelligence_run_receipts_v1` WHERE run_id = @run_id"
        ),
        parameters=(bigquery.ScalarQueryParameter("run_id", "STRING", run_id),),
        max_results=2,
    )
    if len(rows) != 1:
        raise LiveBrainReadRefusal("receipt_refused", "one run receipt is required")
    values = {field: dict(rows[0]).get(field) for field in RUN_RECEIPT_ROW_FIELDS}
    if isinstance(values["market_scope"], list):
        values["market_scope"] = tuple(values["market_scope"])
    try:
        receipt = build_run_receipt(**values)
    except ValueError as error:
        raise LiveBrainReadRefusal("receipt_refused", "run receipt is invalid") from error
    if (
        receipt.run_id != run_id
        or receipt.status != "completed"
        or receipt.complete_partitions is not True
    ):
        raise LiveBrainReadRefusal("receipt_refused", "completed partitions are required")

    copy_fields = ", ".join(f"`{field}`" for field in SOURCE_COPY_RECEIPT_FIELDS)
    copy_rows = _control_query(
        client,
        (
            f"SELECT {copy_fields} FROM `{TARGET_PROJECT}.trends_v2_staging."
            "open_intelligence_source_copy_receipts_v1` "
            "WHERE window_start = @window_start AND window_end = @window_end "
            "ORDER BY source_table"
        ),
        parameters=(
            bigquery.ScalarQueryParameter("window_start", "DATE", receipt.observation_start),
            bigquery.ScalarQueryParameter("window_end", "DATE", receipt.observation_end),
        ),
        max_results=10_001,
    )
    source_receipts = tuple(
        {field: dict(row).get(field) for field in SOURCE_COPY_RECEIPT_FIELDS} for row in copy_rows
    )
    if (
        not source_receipts
        or len(source_receipts) > 10_000
        or len({row["copy_run_id"] for row in source_receipts}) != 1
    ):
        raise LiveBrainReadRefusal("readback_refused", "source window authority unavailable")
    contract = {
        "contract_version": "brain-read-durable-v1",
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "run_id": run_id,
        "signal_id": signal_id,
        "research_depth": research_depth,
        "decision_question": decision_question,
    }
    return BrainControlInputs(
        receipt=receipt,
        artifacts={
            "brain_contract": canonical_bytes(contract),
            "run_receipt": canonical_bytes(receipt),
            "source_window_receipt_set": canonical_bytes(source_receipts),
        },
    )


def _execute(
    *,
    run_id,
    signal_id,
    research_depth,
    decision_question=None,
    invocation_arguments=(),
    runtime_identity_verifier=_default_runtime_identity_verifier,
    bigquery_factory=None,
    **_unused,
):
    identity = runtime_identity_verifier(tuple(invocation_arguments))
    client = (
        bigquery.Client(project=TARGET_PROJECT, location=TARGET_LOCATION)
        if bigquery_factory is None
        else bigquery_factory()
    )
    provider = _BrainArtifactProvider(
        client,
        run_id,
        signal_id,
        research_depth,
        decision_question,
    )
    global _DURABLE_ARTIFACT_CONTEXT
    if _DURABLE_ARTIFACT_CONTEXT is not None:
        raise RuntimeIdentityRefusal("durable Brain artifact context is already active")
    _DURABLE_ARTIFACT_CONTEXT = provider
    try:
        authority = execution_approval._load_execution_authority(
            "brain_read", mode="new_consume", artifact_reader=_execution_approval_artifact_bytes
        )
        _require_profile_authority(_brain_profile(), authority)
        control_inputs = provider.inputs
        if control_inputs is None:
            raise RuntimeIdentityRefusal("durable Brain artifacts were not validated")
        if (
            control_inputs.receipt.source_sha != identity.source_sha
            or authority.source_sha != identity.source_sha
        ):
            raise RuntimeIdentityRefusal("run receipt source SHA differs")
        consumption = execution_approval._consume_execution_authority(authority)
    except execution_approval.ApprovalRefusal as error:
        raise RuntimeIdentityRefusal("durable Brain execution approval refused") from error
    finally:
        _DURABLE_ARTIFACT_CONTEXT = None
    try:
        read = read_live_brain_authority(client=client, run_id=run_id, signal_id=signal_id)
        if read.receipt.source_sha != identity.source_sha:
            raise LiveBrainReadRefusal("identity_refused", "run receipt source SHA differs")
        runtime = _issue_live_brain_runtime_request(
            read,
            research_depth=research_depth,
            decision_question=decision_question,
        )
        snapshot = _issue_live_brain_evidence_snapshot(runtime, read)
        result = _run_intelligence_brain_from_authority(runtime, snapshot)
        payload = {
            "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
            "run_id": run_id,
            "signal_id": signal_id,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_digest": snapshot.snapshot_digest,
            "brain_result": _plain(result),
            "brain_result_digest": result.result_digest,
            "read_receipt": _plain(read.read_receipt),
            "model_calls": 0,
            "persisted": False,
        }
        canonical_result_json = canonical_bytes(payload).decode("utf-8")
        result_digest = hashlib.sha256(canonical_result_json.encode()).hexdigest()
    except Exception as error:
        # The authority is consumed, so the run leaves a terminal row under the same
        # consumption; the row carries the refusal code and never the detail.
        _record_brain_failure(
            authority,
            consumption,
            run_id=run_id,
            signal_id=signal_id,
            research_depth=research_depth,
            decision_question=decision_question,
            error=error,
        )
        raise
    stored = execution_approval._record_execution_result(
        authority,
        consumption,
        _result_reference(
            run_id, signal_id, snapshot.snapshot_id, research_depth, decision_question
        ),
        canonical_result_json,
        result_digest,
        "succeeded",
    )
    payload["execution_approval"] = {
        "manifest_sha256": authority.approval.manifest_sha256,
        "approval_id": authority.approval.approval_id,
        "consumption_id": consumption.consumption_id,
        "result_id": stored.result_id,
    }
    return payload


def _record_brain_failure(
    authority, consumption, *, run_id, signal_id, research_depth, decision_question, error
):
    if isinstance(error, LiveBrainReadRefusal) and error.code in _ERROR_CODES:
        code = error.code
    elif isinstance(error, ValueError):
        code = "semantic_refused"
    else:
        code = "internal_refused"
    payload = {
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "run_id": run_id,
        "signal_id": signal_id,
        "error_code": code,
        "status": "failed",
        "model_calls": 0,
        "persisted": False,
    }
    canonical_result_json = canonical_bytes(payload).decode("utf-8")
    execution_approval._record_execution_result(
        authority,
        consumption,
        _result_reference(run_id, signal_id, "failed", research_depth, decision_question),
        canonical_result_json,
        hashlib.sha256(canonical_result_json.encode()).hexdigest(),
        "failed",
    )


def _result_reference(run_id, signal_id, snapshot_id, research_depth, decision_question):
    # The result store keeps result_reference unique, so every distinct request on one
    # snapshot needs its own reference or the second one dies on
    # execution_approval_conflict after its authority is consumed.
    request_digest = hashlib.sha256(
        canonical_bytes({"decision_question": decision_question, "research_depth": research_depth})
    ).hexdigest()[:16]
    return f"brain://{run_id}/{signal_id}/{snapshot_id}/{research_depth}/{request_digest}"


def _refusal(error_code, run_id, signal_id):
    return {
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "error_code": error_code if error_code in _ERROR_CODES else "internal_refused",
        "run_id": run_id,
        "signal_id": signal_id,
        "model_calls": 0,
        "persisted": False,
    }


def _emit(payload):
    print(json.dumps(_plain(payload), sort_keys=True, separators=(",", ":")))


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    run_id = None
    signal_id = None
    try:
        values = _parse(raw)
        run_id = values["run_id"]
        signal_id = values["signal_id"]
        _emit(_execute(**values, invocation_arguments=tuple(raw)))
        return 0
    except _ArgumentRefusal as error:
        candidate_run_id = error.values.get("run_id")
        candidate_signal_id = error.values.get("signal_id")
        run_id = (
            candidate_run_id
            if isinstance(candidate_run_id, str) and _RUN_ID.fullmatch(candidate_run_id)
            else None
        )
        signal_id = (
            candidate_signal_id
            if isinstance(candidate_signal_id, str) and _SIGNAL_ID.fullmatch(candidate_signal_id)
            else None
        )
        _emit(_refusal("invalid_arguments", run_id, signal_id))
        return 2
    except LiveBrainReadRefusal as error:
        _emit(_refusal(error.code, run_id, signal_id))
        return 2
    except ValueError:
        _emit(
            _refusal(
                "invalid_arguments" if run_id is None else "semantic_refused", run_id, signal_id
            )
        )
        return 2
    except Exception:
        _emit(_refusal("internal_refused", run_id, signal_id))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
