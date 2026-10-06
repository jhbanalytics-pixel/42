"""Plan or apply the immutable Open Intelligence staging job definition."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.analysis.open_intelligence import execution_approval
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.execution_origins import (
    ExecutionOrigin,
    OriginRefusal,
    OriginRegistry,
    select_origin,
)

PROJECT = "ogilvy-trends-v2"
PROJECT_NUMBER = "590353929363"
REGION = "us-central1"
JOB = "trends-engine-open-intelligence-staging"
SERVICE_ACCOUNT = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
IMAGE_PATTERN = re.compile(
    r"us-central1-docker\.pkg\.dev/ogilvy-trends-v2/pipeline/"
    r"trends-engine@sha256:[0-9a-f]{64}\Z"
)
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
ENVIRONMENT = (
    ("GCP_PROJECT", PROJECT),
    ("TRENDS_ENV", "staging"),
    ("BIGQUERY_DATASET", "trends_v2"),
    ("GEMINI_MODEL", "gemini-3.5-flash"),
    ("GEMINI_LOCATION", "global"),
    ("MAILER_V2_ENABLED", "false"),
    ("RECONCILE_ENABLED", "false"),
)
READBACK_CHECKS = (
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
)
ROLLBACK_PLAN = (
    "Deletion or previous immutable image restoration requires separate "
    "authorization. This script has no rollback execution path."
)
FORBIDDEN_TEMPLATE_ANNOTATIONS = frozenset(
    {
        "run.googleapis.com/secrets",
        "run.googleapis.com/cloudsql-instances",
        "run.googleapis.com/vpc-access-connector",
        "run.googleapis.com/vpc-access-egress",
        "run.googleapis.com/network-interfaces",
    }
)
BRAIN_JOB = "trends-engine-oi-brain-staging"
BRAIN_SERVICE_ACCOUNT = "trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com"
BRAIN_CONTRACT_SHA256 = "f62be04fd31f6236855c05e5907d87f87b5d89f644512c2391420d46da6b4962"
# The Brain job carries the same environment as every durable job, which the
# approval contract binds; the Brain's own verifier binds the same set.
BRAIN_ENVIRONMENT = execution_approval._DEFAULT_ENVIRONMENT
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
BRAIN_ANNOTATION_KEYS = (
    "42.ogilvy/contract-sha256",
    "42.ogilvy/dependency-lock-sha256",
    "42.ogilvy/runtime-sbom-sha256",
    "42.ogilvy/source-sha",
    "42.ogilvy/deployment-manifest-sha256",
    "42.ogilvy/execution-approval-sha256",
)
DEPLOYER_CONTRACT_SHA256 = "47750bbf7652d0e4d9d6501333859c423820456586969d5323da2fbebec9ba73"
OPERATION_JOBS: Mapping[str, tuple[str, str]] = {
    operation: (contract["job"], contract["identity"])
    for operation, contract in execution_approval._OPERATION_CONTRACTS.items()
}
_OPERATION_BY_JOB: Mapping[str, str] = {
    job: operation for operation, (job, _) in OPERATION_JOBS.items()
}
if len(_OPERATION_BY_JOB) != len(OPERATION_JOBS):
    raise RuntimeError("execution matrix job names must be unique per operation")
_PRODUCTION_JOBS = frozenset(
    {
        "trends-engine-pipeline",
        "trends-engine-phase2",
        "trends-engine-regen",
        "trends-engine-resend",
    }
)


def _operation_contract_sha256(operation: str) -> str:
    return BRAIN_CONTRACT_SHA256 if operation == "brain_read" else DEPLOYER_CONTRACT_SHA256


DURABLE_ANNOTATION_KEYS = ("42.ogilvy/execution-approval-sha256", "42.ogilvy/source-sha")
BOOTSTRAP_ANNOTATION_KEYS = (
    "42.ogilvy/bootstrap-manifest-sha256",
    "42.ogilvy/bootstrap-manifest-generation",
    "42.ogilvy/bootstrap-signature-generation",
)
_UNOBSERVABLE_MANIFEST_FIELDS = ("contract_sha256", "dependency_lock_sha256", "runtime_sbom_sha256")
MANIFEST_V1 = "open_intelligence_execution_manifest_v1"
MANIFEST_V2 = "open_intelligence_execution_manifest_v2"
# The exact manifest version literal selects the admission mode: retained v1 bytes are
# inspected under historical_read, a v2 manifest is admitted for deployment under
# new_approval. Nothing else (operation name, environment, registry state) selects one.
_MODE_BY_VERSION = {MANIFEST_V1: "historical_read", MANIFEST_V2: "new_approval"}
V2_ANNOTATION_KEYS = ("42.ogilvy/origin-registry-sha256", "42.ogilvy/resource-manifest-sha256")
_SECRET_VERSION_RESOURCE = re.compile(
    r"//secretmanager\.googleapis\.com/projects/([^/]+)/secrets/([^/]+)/versions/([^/]+)\Z"
)
_NUMERIC_SECRET_VERSION = re.compile(r"[1-9][0-9]*\Z")


def operation_annotation_keys(
    operation: str, *, manifest_version: str = MANIFEST_V1
) -> tuple[str, ...]:
    """Exact job annotation set the runtime authority reader requires for one operation."""
    if operation == "brain_read":
        keys = BRAIN_ANNOTATION_KEYS
    elif operation == "bootstrap_migration_apply":
        keys = (*DURABLE_ANNOTATION_KEYS, *BOOTSTRAP_ANNOTATION_KEYS)
    else:
        keys = DURABLE_ANNOTATION_KEYS
    if manifest_version == MANIFEST_V2:
        return (*keys, *V2_ANNOTATION_KEYS)
    return keys


def _observable_manifest(manifest: Mapping[str, object], operation: str) -> dict[str, object]:
    if operation == "brain_read":
        return dict(manifest)
    return {
        key: value for key, value in manifest.items() if key not in _UNOBSERVABLE_MANIFEST_FIELDS
    }


def _bootstrap_generation(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.isdecimal() or int(value) <= 0:
        raise DeploymentError(f"{field} must be a positive decimal object generation")
    return value


_BRAIN_LOCK_PATH = Path(__file__).resolve().parents[2] / "requirements.lock"
_CLOUD_BUILD_RESOURCE = re.compile(
    r"projects/ogilvy-trends-v2/locations/([a-z][a-z0-9-]{0,62})/"
    r"builds/([a-z0-9][a-z0-9-]{0,127})\Z"
)
_CLOUD_BUILD_SOURCE_FIELDS = (
    ("resolvedRepoSource", "commitSha"),
    ("resolvedConnectedRepository", "revision"),
    ("resolvedGitSource", "revision"),
)

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
Resolver = Callable[[str], str | None]


class DeploymentError(RuntimeError):
    """Raised when deployment state or readback cannot be proven."""


class CurrentJobUnparseable(DeploymentError):
    """The pre-mutation job exists but does not parse as a contract-shaped job.

    Live legacy jobs carry no ``42.ogilvy/`` annotations and omit fields the strict parser
    requires. That is drift to replace. The post-mutation readback never uses this class.
    """


@dataclass(frozen=True)
class DesiredJob:
    project: str
    region: str
    job: str
    service_account: str
    image: str
    source_sha: str
    command: tuple[str, ...]
    args: tuple[str, ...]
    timeout_seconds: int
    tasks: int
    parallelism: int
    max_retries: int
    cpu: str
    memory: str
    environment: tuple[tuple[str, str], ...]
    labels: tuple[tuple[str, str], ...]

    def to_record(self) -> dict[str, object]:
        return {
            "project": self.project,
            "region": self.region,
            "job": self.job,
            "service_account": self.service_account,
            "image": self.image,
            "source_sha": self.source_sha,
            "command": list(self.command),
            "args": list(self.args),
            "timeout_seconds": self.timeout_seconds,
            "tasks": self.tasks,
            "parallelism": self.parallelism,
            "max_retries": self.max_retries,
            "cpu": self.cpu,
            "memory": self.memory,
            "environment": dict(self.environment),
            "labels": dict(self.labels),
        }


@dataclass(frozen=True)
class BrainDesiredJob:
    project: str
    region: str
    job: str
    service_account: str
    image: str
    source_sha: str
    dependency_lock_sha256: str
    runtime_sbom_sha256: str
    command: tuple[str, ...]
    args: tuple[str, ...]
    timeout_seconds: int
    max_retries: int
    memory: str
    environment: tuple[tuple[str, str], ...]
    secret_names: tuple[str, ...]
    writable_code_mounts: tuple[str, ...]
    operation: str
    annotations: tuple[tuple[str, str], ...]
    manifest_version: str = MANIFEST_V1
    secret_versions: tuple[tuple[str, str], ...] = ()

    def to_record(self) -> dict[str, object]:
        return {
            "project": self.project,
            "region": self.region,
            "job": self.job,
            "service_account": self.service_account,
            "image": self.image,
            "source_sha": self.source_sha,
            "dependency_lock_sha256": self.dependency_lock_sha256,
            "runtime_sbom_sha256": self.runtime_sbom_sha256,
            "command": list(self.command),
            "args": list(self.args),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "memory": self.memory,
            "environment": dict(self.environment),
            "secret_names": list(self.secret_names),
            "writable_code_mounts": list(self.writable_code_mounts),
            "operation": self.operation,
            "annotations": dict(self.annotations),
            **({"cpu": "2"} if self.operation == "source_snapshot_capture" else {}),
            **(
                {
                    "manifest_version": self.manifest_version,
                    "secret_versions": dict(self.secret_versions),
                }
                if self.manifest_version == MANIFEST_V2
                else {}
            ),
        }


@dataclass(frozen=True)
class BrainJobSnapshot:
    manifest: Mapping[str, object]
    annotations: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class BrainDependencyAuthority:
    dependency_lock_sha256: str
    runtime_sbom_sha256: str


@dataclass(frozen=True)
class CloudBuildAuthority:
    build_resource: str
    build_id: str
    project_id: str
    location: str
    status: str
    resolved_source_kind: str
    resolved_source_sha: str
    result_image_name: str
    result_image_digest: str
    finish_time: str
    build_provenance_sha256: str | None = None
    origin_registry_sha256: str | None = None
    resource_manifest_sha256: str | None = None
    origin_mode: str | None = None


@dataclass(frozen=True)
class BrainDeploymentReceipt:
    receipt_contract_version: str
    source_sha: str
    image_digest: str
    dependency_lock_sha256: str
    runtime_sbom_sha256: str
    service_identity: str
    command: str
    arguments: tuple[str, ...]
    deployment_manifest_sha256: str
    build_identity: dict[str, str]


@dataclass(frozen=True)
class JobSnapshot:
    project: str
    region: str
    job: str
    service_account: str
    image: str
    command: tuple[str, ...]
    args: tuple[str, ...]
    timeout_seconds: int
    tasks: int
    parallelism: int
    max_retries: int
    cpu: str
    memory: str
    environment: tuple[tuple[str, str], ...]
    resource_labels: tuple[tuple[str, str], ...]
    template_labels: tuple[tuple[str, str], ...]
    has_secret_refs: bool
    has_volumes: bool
    has_cloudsql: bool
    has_vpc: bool
    secret_names: tuple[str, ...] = ()
    secret_versions: tuple[tuple[str, str], ...] = ()


def desired_job(image: str, source_sha: str) -> DesiredJob:
    if not isinstance(image, str) or IMAGE_PATTERN.fullmatch(image) is None:
        raise DeploymentError("image must be the approved immutable image digest")
    if not isinstance(source_sha, str) or SHA_PATTERN.fullmatch(source_sha) is None:
        raise DeploymentError("source SHA must be a full lowercase source SHA")
    return DesiredJob(
        PROJECT,
        REGION,
        JOB,
        SERVICE_ACCOUNT,
        image,
        source_sha,
        ("python",),
        ("scripts/run_rss_now.py",),
        10800,
        1,
        1,
        0,
        "2",
        "8Gi",
        ENVIRONMENT,
        (("environment", "staging"), ("source-sha", source_sha)),
    )


def _brain_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or DIGEST_PATTERN.fullmatch(value) is None:
        raise DeploymentError(f"{field} must be a lowercase SHA256")
    return value


def brain_deployment_manifest(desired: BrainDesiredJob) -> dict[str, object]:
    return {
        "contract_sha256": _operation_contract_sha256(desired.operation),
        "dependency_lock_sha256": desired.dependency_lock_sha256,
        "runtime_sbom_sha256": desired.runtime_sbom_sha256,
        "source_sha": desired.source_sha,
        "job_resource": f"projects/{desired.project}/locations/{desired.region}/jobs/{desired.job}",
        "region": desired.region,
        "image_uri_with_digest": desired.image,
        "service_identity": desired.service_account,
        "command": f"{desired.command[0]} {desired.args[0]}",
        "arguments": desired.args[1:],
        "environment_names": tuple(sorted(name for name, _value in desired.environment)),
        "secret_names": desired.secret_names,
        "memory": desired.memory,
        "timeout_seconds": desired.timeout_seconds,
        "max_retries": desired.max_retries,
        "writable_code_mounts": desired.writable_code_mounts,
        "operation": desired.operation,
        **({"cpu": "2"} if desired.operation == "source_snapshot_capture" else {}),
        **(
            {"manifest_version": MANIFEST_V2, "secret_versions": desired.secret_versions}
            if desired.manifest_version == MANIFEST_V2
            else {}
        ),
    }


def _active_generation():
    """Load the active trusted generation from packaged source.

    The generation module belongs to the authority adapter. An import failure refuses;
    no other registry path or digest is ever consulted.
    """
    try:
        from src.analysis.open_intelligence.execution_generations import active_generation
    except ImportError as error:
        raise DeploymentError("trusted generation module is unavailable") from error
    try:
        return active_generation()
    except DeploymentError:
        raise
    except ValueError as error:
        # A refusal from the generation loader keeps its code text and reaches the
        # script boundary as a deployment refusal, never as a traceback.
        raise DeploymentError(str(error)) from error
    except Exception as error:
        raise DeploymentError("trusted generation is unavailable") from error


def _require_generation(generation):
    registry = getattr(generation, "registry", None)
    origin_registry_sha256 = getattr(generation, "origin_registry_sha256", None)
    resource_manifest_sha256 = getattr(generation, "resource_manifest_sha256", None)
    resource_manifest = getattr(generation, "resource_manifest", None)
    if (
        type(registry) is not OriginRegistry
        or origin_registry_sha256 != registry.sha256
        or not isinstance(resource_manifest_sha256, str)
        or DIGEST_PATTERN.fullmatch(resource_manifest_sha256) is None
        or not isinstance(resource_manifest, Mapping)
        or resource_manifest.get("origin_registry_sha256") != origin_registry_sha256
    ):
        raise DeploymentError("trusted generation is invalid")
    return generation


def _require_v2_origin(origin) -> ExecutionOrigin:
    if (
        type(origin) is not ExecutionOrigin
        or origin.manifest_version != MANIFEST_V2
        or "new_approval" not in origin.allowed_execution_modes
    ):
        raise DeploymentError("resolved origin is not a fresh v2 origin")
    return origin


def retained_v1_origin(operation: str, *, registry) -> ExecutionOrigin:
    """Select the retained v1 origin for one operation under explicit historical read."""
    if type(registry) is not OriginRegistry:
        raise DeploymentError("retained v1 origin needs the packaged registry")
    rows = [
        origin
        for origin in registry.values()
        if origin.manifest_version == MANIFEST_V1 and operation in origin.operation_bindings
    ]
    if len(rows) != 1:
        raise DeploymentError("retained v1 origin is unavailable or ambiguous")
    try:
        return select_origin(
            manifest_version=MANIFEST_V1,
            contract_sha256=rows[0].contract_sha256,
            mode="historical_read",
            registry=registry,
        )
    except OriginRefusal as error:
        raise DeploymentError("retained v1 origin refused historical read") from error


def origin_for_manifest(manifest: object, *, generation) -> ExecutionOrigin:
    """Resolve the one fresh v2 origin an admitted v2 manifest names."""
    if getattr(manifest, "manifest_version", None) != MANIFEST_V2:
        raise DeploymentError("only a v2 execution manifest resolves a fresh origin")
    registry = _require_generation(generation).registry
    try:
        origin = select_origin(
            manifest_version=MANIFEST_V2,
            contract_sha256=getattr(manifest, "contract_sha256", None),
            mode="new_approval",
            registry=registry,
        )
    except OriginRefusal as error:
        raise DeploymentError("Brain execution manifest origin is invalid") from error
    binding = origin.operation_bindings.get(getattr(manifest, "operation", None))
    if (
        binding is None
        or getattr(manifest, "job_resource", None) != binding.job_resource
        or getattr(manifest, "service_identity", None) != binding.service_identity
    ):
        raise DeploymentError("Brain execution manifest origin is invalid")
    return origin


def _generation_sequence(value: object) -> tuple[object, ...] | None:
    """Accept any real sequence from frozen generation data, never a string or mapping."""
    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(value, Sequence):
        return None
    return tuple(value)


def resolve_secret_versions(
    secrets: Sequence[str], *, resource_manifest: object, project: str = PROJECT
) -> tuple[tuple[str, str], ...]:
    """Bind each secret name to exactly one approved numeric Secret Manager version.

    Selection is by exact project and secret name inside the selected resource manifest,
    with the read action. A missing secret, a latest version, a non-numeric version or
    more than one approved version refuses.
    """
    names = tuple(secrets)
    if not all(isinstance(name, str) and name for name in names) or len(set(names)) != len(names):
        raise DeploymentError("secret names are invalid")
    resources = _generation_sequence(
        resource_manifest.get("resources") if isinstance(resource_manifest, Mapping) else None
    )
    if resources is None:
        raise DeploymentError("resource manifest secret resources are unavailable")
    approved: dict[str, list[str]] = {}
    for item in resources:
        if not isinstance(item, Mapping):
            raise DeploymentError("resource manifest entry is invalid")
        name = item.get("name")
        actions = _generation_sequence(item.get("actions"))
        match = _SECRET_VERSION_RESOURCE.fullmatch(name) if isinstance(name, str) else None
        if match is None:
            continue
        resource_project, secret_name, version = match.groups()
        if resource_project != project or actions is None or "read" not in actions:
            continue
        approved.setdefault(secret_name, []).append(version)
    bindings = []
    for name in names:
        versions = approved.get(name, [])
        if not versions:
            raise DeploymentError(f"secret {name} has no approved read version")
        if len(versions) != 1:
            raise DeploymentError(f"secret {name} has ambiguous approved versions")
        if _NUMERIC_SECRET_VERSION.fullmatch(versions[0]) is None:
            raise DeploymentError(f"secret {name} must bind one numeric version")
        bindings.append((name, versions[0]))
    return tuple(sorted(bindings))


def desired_operation_job(
    operation: str,
    image: str,
    source_sha: str,
    dependency_lock_sha256: str,
    runtime_sbom_sha256: str,
    *,
    arguments: Sequence[str],
    environment: Sequence[tuple[str, str]],
    secrets: Sequence[str],
    execution_approval_sha256: str,
    timeout_seconds: int = 900,
    bootstrap_generations: tuple[str, str] | None = None,
    origin=None,
    generation=None,
) -> BrainDesiredJob:
    if operation not in OPERATION_JOBS:
        raise DeploymentError("operation is not in the execution matrix")
    if (origin is None) != (generation is None):
        raise DeploymentError("v2 route needs one resolved origin and one trusted generation")
    if origin is not None:
        _require_v2_origin(origin)
        _require_generation(generation)
        if bootstrap_generations is not None:
            raise DeploymentError("bootstrap object generations are v1 only")
        binding = origin.operation_bindings.get(operation)
        if binding is None:
            raise DeploymentError("operation is not bound by the resolved origin")
        job = binding.job_resource.rsplit("/", 1)[1]
        service_account = binding.service_identity
    else:
        job, service_account = OPERATION_JOBS[operation]
    if job in _PRODUCTION_JOBS:
        raise DeploymentError("operation job must not be a production job")
    if (operation == "bootstrap_migration_apply") != (bootstrap_generations is not None):
        raise DeploymentError(
            "bootstrap object generations are required only for the bootstrap job"
        )
    if bootstrap_generations is not None:
        if not isinstance(bootstrap_generations, tuple) or len(bootstrap_generations) != 2:
            raise DeploymentError("bootstrap object generations are invalid")
        bootstrap_generations = (
            _bootstrap_generation(bootstrap_generations[0], "bootstrap manifest generation"),
            _bootstrap_generation(bootstrap_generations[1], "bootstrap signature generation"),
        )
    if origin is not None:
        if not isinstance(image, str) or re.fullmatch(origin.image_uri_regex, image) is None:
            raise DeploymentError("image must be the approved immutable image digest")
    elif not isinstance(image, str) or IMAGE_PATTERN.fullmatch(image) is None:
        raise DeploymentError("image must be the approved immutable image digest")
    if not isinstance(source_sha, str) or SHA_PATTERN.fullmatch(source_sha) is None:
        raise DeploymentError("source SHA must be a full lowercase source SHA")
    dependency_lock_sha256 = _brain_digest(dependency_lock_sha256, "dependency lock digest")
    runtime_sbom_sha256 = _brain_digest(runtime_sbom_sha256, "runtime SBOM digest")
    execution_approval_sha256 = _brain_digest(
        execution_approval_sha256, "execution approval digest"
    )
    arguments = tuple(arguments)
    if not arguments or not all(isinstance(value, str) and value for value in arguments):
        raise DeploymentError("Brain invocation arguments are required")
    environment = tuple((str(name), str(value)) for name, value in environment)
    secrets = tuple(secrets)
    if not all(isinstance(name, str) and name for name in secrets) or len(set(secrets)) != len(
        secrets
    ):
        raise DeploymentError("secret names are invalid")
    if set(secrets) & {name for name, _value in environment}:
        raise DeploymentError("secret names collide with environment names")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 0 < timeout_seconds <= 10_800
    ):
        raise DeploymentError("Brain timeout is invalid")
    if operation == "source_snapshot_capture" and timeout_seconds != 600:
        raise DeploymentError("source snapshot timeout must be exactly 600 seconds")
    secret_versions = ()
    if origin is not None:
        secret_versions = resolve_secret_versions(
            secrets, resource_manifest=generation.resource_manifest
        )
    initial = BrainDesiredJob(
        PROJECT,
        REGION,
        job,
        service_account,
        image,
        source_sha,
        dependency_lock_sha256,
        runtime_sbom_sha256,
        ("python",),
        arguments,
        timeout_seconds,
        0,
        "8Gi" if operation == "source_snapshot_capture" else "4Gi",
        environment,
        tuple(sorted(secrets)),
        (),
        operation,
        (),
        MANIFEST_V1 if origin is None else MANIFEST_V2,
        secret_versions,
    )
    manifest_digest = canonical_digest(brain_deployment_manifest(initial))
    if operation == "brain_read":
        annotations = (
            ("42.ogilvy/contract-sha256", _operation_contract_sha256(operation)),
            ("42.ogilvy/dependency-lock-sha256", dependency_lock_sha256),
            ("42.ogilvy/runtime-sbom-sha256", runtime_sbom_sha256),
            ("42.ogilvy/source-sha", source_sha),
            ("42.ogilvy/deployment-manifest-sha256", manifest_digest),
            ("42.ogilvy/execution-approval-sha256", execution_approval_sha256),
        )
    else:
        annotations = (
            ("42.ogilvy/execution-approval-sha256", execution_approval_sha256),
            ("42.ogilvy/source-sha", source_sha),
        )
        if bootstrap_generations is not None:
            annotations = (
                *annotations,
                ("42.ogilvy/bootstrap-manifest-sha256", execution_approval_sha256),
                ("42.ogilvy/bootstrap-manifest-generation", bootstrap_generations[0]),
                ("42.ogilvy/bootstrap-signature-generation", bootstrap_generations[1]),
            )
    if origin is not None:
        annotations = (
            *annotations,
            ("42.ogilvy/origin-registry-sha256", generation.origin_registry_sha256),
            ("42.ogilvy/resource-manifest-sha256", generation.resource_manifest_sha256),
        )
    return replace(initial, annotations=annotations)


def desired_brain_job(
    image: str,
    source_sha: str,
    dependency_lock_sha256: str,
    runtime_sbom_sha256: str,
    *,
    arguments: Sequence[str],
    execution_approval_sha256: str,
    timeout_seconds: int = 900,
) -> BrainDesiredJob:
    return desired_operation_job(
        "brain_read",
        image,
        source_sha,
        dependency_lock_sha256,
        runtime_sbom_sha256,
        arguments=("scripts/staging/run_live_intelligence_brain.py", *tuple(arguments)),
        environment=BRAIN_ENVIRONMENT,
        secrets=(),
        execution_approval_sha256=execution_approval_sha256,
        timeout_seconds=timeout_seconds,
    )


def desired_operation_job_from_execution_manifest(
    manifest: object,
    *,
    operation: str,
    execution_approval_sha256: str,
    dependency_lock_sha256: str,
    runtime_sbom_sha256: str,
    bootstrap_generations: tuple[str, str] | None = None,
    origin=None,
    generation=None,
) -> BrainDesiredJob:
    if operation not in OPERATION_JOBS:
        raise DeploymentError("Brain execution manifest authority is invalid")
    version = getattr(manifest, "manifest_version", None)
    if version not in _MODE_BY_VERSION:
        raise DeploymentError("Brain execution manifest authority is invalid")
    if version == MANIFEST_V2:
        if origin is None or generation is None:
            raise DeploymentError(
                "v2 execution manifest needs one resolved origin and one trusted generation"
            )
        binding = _require_v2_origin(origin).operation_bindings.get(operation)
        if binding is None:
            raise DeploymentError("operation is not bound by the resolved origin")
        expected_job_resource = binding.job_resource
        service_account = binding.service_identity
    else:
        if origin is not None or generation is not None:
            raise DeploymentError("retained v1 execution manifest takes no origin or generation")
        job, service_account = OPERATION_JOBS[operation]
        expected_job_resource = f"projects/{PROJECT}/locations/{REGION}/jobs/{job}"
    if (
        getattr(manifest, "operation", None) != operation
        or getattr(manifest, "job_resource", None) != expected_job_resource
        or getattr(manifest, "service_identity", None) != service_account
    ):
        raise DeploymentError("Brain execution manifest authority is invalid")
    command = tuple(getattr(manifest, "command", ()))
    arguments = tuple(getattr(manifest, "arguments", ()))
    environment = tuple(getattr(manifest, "environment", ()))
    secrets = tuple(getattr(manifest, "secrets", ()))
    if command != ("python",) or not arguments:
        raise DeploymentError("Brain execution manifest invocation is invalid")
    desired = desired_operation_job(
        operation,
        getattr(manifest, "image_uri", None),
        getattr(manifest, "source_sha", None),
        dependency_lock_sha256,
        runtime_sbom_sha256,
        arguments=arguments,
        environment=environment,
        secrets=secrets,
        execution_approval_sha256=execution_approval_sha256,
        timeout_seconds=getattr(manifest, "timeout_seconds", None),
        bootstrap_generations=bootstrap_generations,
        origin=origin,
        generation=generation,
    )
    if (
        desired.command != command
        or desired.args != arguments
        or getattr(manifest, "max_retries", None) != desired.max_retries
        or getattr(manifest, "timeout_seconds", None) != desired.timeout_seconds
    ):
        raise DeploymentError("Brain execution manifest deployment policy is invalid")
    return desired


def desired_brain_job_from_execution_manifest(
    manifest: object,
    *,
    execution_approval_sha256: str,
    dependency_lock_sha256: str,
    runtime_sbom_sha256: str,
) -> BrainDesiredJob:
    return desired_operation_job_from_execution_manifest(
        manifest,
        operation="brain_read",
        execution_approval_sha256=execution_approval_sha256,
        dependency_lock_sha256=dependency_lock_sha256,
        runtime_sbom_sha256=runtime_sbom_sha256,
    )


def load_operation_execution_manifest(
    path: Path, digest: str, operation: str, *, generation=None
) -> object:
    if operation not in OPERATION_JOBS:
        raise DeploymentError("Brain execution manifest operation is invalid")
    if not isinstance(path, Path) or not path.is_absolute():
        raise DeploymentError("Brain execution manifest path is invalid")
    digest = _brain_digest(digest, "execution manifest digest")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeploymentError("Brain execution manifest is invalid") from error
    version = payload.get("manifest_version") if isinstance(payload, Mapping) else None
    mode = _MODE_BY_VERSION.get(version)
    if mode is None:
        raise DeploymentError("Brain execution manifest is invalid")
    registry = _require_generation(
        _active_generation() if generation is None else generation
    ).registry
    try:
        canonical = execution_approval.canonical_manifest_bytes(
            payload, mode=mode, registry=registry
        )
        manifest = execution_approval.validate_execution_manifest(
            payload, mode=mode, registry=registry
        )
    except (execution_approval.ApprovalRefusal, OriginRefusal) as error:
        raise DeploymentError("Brain execution manifest is invalid") from error
    if raw != canonical or hashlib.sha256(canonical).hexdigest() != digest:
        raise DeploymentError("Brain execution manifest digest differs")
    if manifest.operation != operation or manifest.manifest_version != version:
        raise DeploymentError("Brain execution manifest operation is invalid")
    return manifest


def load_brain_execution_manifest(path: Path, digest: str, *, generation=None) -> object:
    return load_operation_execution_manifest(path, digest, "brain_read", generation=generation)


def brain_snapshot_from_desired(
    desired: BrainDesiredJob, *, runtime_sbom_sha256: str | None = None
) -> BrainJobSnapshot:
    manifest = _observable_manifest(brain_deployment_manifest(desired), desired.operation)
    annotations = dict(desired.annotations)
    if runtime_sbom_sha256 is not None:
        manifest["runtime_sbom_sha256"] = runtime_sbom_sha256
        annotations["42.ogilvy/runtime-sbom-sha256"] = runtime_sbom_sha256
    return BrainJobSnapshot(manifest, tuple(annotations.items()))


def brain_snapshot_mismatches(
    snapshot: BrainJobSnapshot, desired: BrainDesiredJob
) -> tuple[str, ...]:
    expected_manifest = _observable_manifest(brain_deployment_manifest(desired), desired.operation)
    mismatches = [
        field for field, value in expected_manifest.items() if snapshot.manifest.get(field) != value
    ]
    if dict(snapshot.annotations) != dict(desired.annotations):
        for annotation in operation_annotation_keys(
            desired.operation, manifest_version=desired.manifest_version
        ):
            if dict(snapshot.annotations).get(annotation) != dict(desired.annotations).get(
                annotation
            ):
                field = annotation.rsplit("/", 1)[-1].replace("-", "_")
                if field not in mismatches:
                    mismatches.append(field)
        if (
            set(snapshot.annotations) != set(desired.annotations)
            and "annotations" not in mismatches
        ):
            mismatches.append("annotations")
    return tuple(mismatches)


def derive_brain_dependency_authority(
    *,
    lock_path: Path = _BRAIN_LOCK_PATH,
    supplied_dependency_lock_sha256: str,
    supplied_runtime_sbom_sha256: str,
) -> BrainDependencyAuthority:
    from scripts.staging.run_live_intelligence_brain import (
        RuntimeIdentityRefusal,
        _lock_versions,
        _runtime_inventory_payload,
    )

    try:
        lock_bytes = lock_path.read_bytes()
    except OSError as error:
        raise DeploymentError("Brain dependency lock is unavailable") from error
    lock_digest = hashlib.sha256(lock_bytes).hexdigest()
    if lock_digest != supplied_dependency_lock_sha256:
        raise DeploymentError("Brain dependency lock digest differs from repository bytes")
    try:
        locked = _lock_versions(lock_bytes)
    except RuntimeIdentityRefusal as error:
        raise DeploymentError("Brain dependency lock is invalid") from error
    reserved = {"pip", "trends-engine-v2"}
    if set(locked) & reserved:
        raise DeploymentError("Brain runtime inventory authority classes overlap")
    distributions = (
        *locked.items(),
        ("pip", "26.2.1"),
        ("trends-engine-v2", "0.1.0"),
    )
    try:
        inventory = _runtime_inventory_payload(
            distributions=distributions,
            locked_versions=locked,
        )
    except RuntimeIdentityRefusal as error:
        raise DeploymentError("Brain runtime inventory authority is invalid") from error
    runtime_sbom_sha256 = hashlib.sha256(inventory).hexdigest()
    if runtime_sbom_sha256 != supplied_runtime_sbom_sha256:
        raise DeploymentError("Brain runtime SBOM digest differs from derived inventory")
    return BrainDependencyAuthority(lock_digest, runtime_sbom_sha256)


def validate_cloud_build_resource(value: object) -> str:
    if not isinstance(value, str) or _CLOUD_BUILD_RESOURCE.fullmatch(value) is None:
        raise DeploymentError("Cloud Build resource is invalid")
    return value


def _utc_finish_time(value: object) -> str:
    if not isinstance(value, str):
        raise DeploymentError("Cloud Build finish time is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DeploymentError("Cloud Build finish time is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeploymentError("Cloud Build finish time is invalid")
    return value


def _image_repository(name: object) -> str | None:
    if not isinstance(name, str) or "@" in name:
        return None
    slash = name.rfind("/")
    colon = name.rfind(":")
    if colon <= slash or colon == len(name) - 1:
        return None
    return name[:colon]


def validate_cloud_build_authority(
    payload: object,
    *,
    build_resource: str,
    desired: BrainDesiredJob,
    origin,
) -> CloudBuildAuthority:
    """Inspect a retained v1 build under one explicit historical v1 origin."""
    build_resource = validate_cloud_build_resource(build_resource)
    if (
        type(origin) is not ExecutionOrigin
        or origin.manifest_version != MANIFEST_V1
        or getattr(desired, "manifest_version", None) != MANIFEST_V1
        or getattr(desired, "operation", None) not in origin.operation_bindings
    ):
        raise DeploymentError("retained v1 build inspection needs one historical v1 origin")
    if not isinstance(payload, Mapping):
        raise DeploymentError("Cloud Build response is invalid")
    try:
        payload = execution_approval.normalize_cloud_build_response(dict(payload), origin=origin)
    except (execution_approval.ApprovalRefusal, OriginRefusal) as error:
        raise DeploymentError("Cloud Build source provenance is invalid") from error
    match = _CLOUD_BUILD_RESOURCE.fullmatch(build_resource)
    assert match is not None
    location, build_id = match.groups()
    if (
        payload.get("name") != build_resource
        or payload.get("id") != build_id
        or payload.get("projectId") != PROJECT
        or payload.get("status") != "SUCCESS"
    ):
        raise DeploymentError("Cloud Build identity or status differs")
    finish_time = _utc_finish_time(payload.get("finishTime"))
    provenance = payload.get("sourceProvenance")
    native = payload.get("source")
    if not isinstance(provenance, Mapping) and not isinstance(native, Mapping):
        raise DeploymentError("Cloud Build source provenance is unavailable")
    resolved = []
    if isinstance(provenance, Mapping):
        for source_kind, revision_field in _CLOUD_BUILD_SOURCE_FIELDS:
            source = provenance.get(source_kind)
            if isinstance(source, Mapping) and source.get(revision_field) is not None:
                resolved.append((source_kind, source.get(revision_field)))
    if isinstance(native, Mapping):
        # The shared normalizer already validated this envelope against the origin's
        # connected repository and required agreement with any legacy provenance. The
        # receipt keeps the retained kind label for a connected repository resolution.
        connected = native.get("connectedRepository")
        if isinstance(connected, Mapping) and connected.get("revision") is not None:
            resolved.append(("resolvedConnectedRepository", connected.get("revision")))
    if not resolved or len({sha for _kind, sha in resolved}) != 1:
        raise DeploymentError("Cloud Build resolved source is unavailable or ambiguous")
    source_kind, source_sha = resolved[0]
    if not isinstance(source_sha, str) or SHA_PATTERN.fullmatch(source_sha) is None:
        raise DeploymentError("Cloud Build resolved source SHA is invalid")
    if source_sha != desired.source_sha:
        raise DeploymentError("Cloud Build resolved source SHA differs from local source")
    results = payload.get("results")
    images = results.get("images") if isinstance(results, Mapping) else None
    if not isinstance(images, list):
        raise DeploymentError("Cloud Build image results are unavailable")
    if len(images) != 1:
        raise DeploymentError("Cloud Build must produce exactly one result image")
    repository, image_digest = desired.image.rsplit("@", 1)
    matches = [
        item
        for item in images
        if isinstance(item, Mapping)
        and item.get("digest") == image_digest
        and _image_repository(item.get("name")) == repository
    ]
    if len(matches) != 1:
        raise DeploymentError("Cloud Build result image authority is unavailable or ambiguous")
    image_name = matches[0].get("name")
    if not isinstance(image_name, str):
        raise DeploymentError("Cloud Build result image name is invalid")
    return CloudBuildAuthority(
        build_resource=build_resource,
        build_id=build_id,
        project_id=PROJECT,
        location=location,
        status="SUCCESS",
        resolved_source_kind=source_kind,
        resolved_source_sha=source_sha,
        result_image_name=image_name,
        result_image_digest=image_digest,
        finish_time=finish_time,
    )


def validate_cloud_build_authority_v2(
    payload: object,
    *,
    build_resource: str,
    desired: BrainDesiredJob,
    origin,
    generation,
) -> CloudBuildAuthority:
    """Admit a fresh v2 build through the shared provenance factory under one origin."""
    build_resource = validate_cloud_build_resource(build_resource)
    _require_v2_origin(origin)
    _require_generation(generation)
    if (
        getattr(desired, "manifest_version", None) != MANIFEST_V2
        or desired.operation not in origin.operation_bindings
    ):
        raise DeploymentError("v2 build authority needs a v2 job bound by the resolved origin")
    if not isinstance(payload, Mapping):
        raise DeploymentError("Cloud Build response is invalid")
    try:
        receipt = execution_approval.build_provenance_from_response(
            dict(payload),
            origin.contract_sha256,
            manifest_version=origin.manifest_version,
            mode="new_approval",
            registry=generation.registry,
        )
        normalized = execution_approval.normalize_cloud_build_response(dict(payload), origin=origin)
        provenance = execution_approval.canonical_build_provenance_bytes(receipt, origin=origin)
    except (execution_approval.ApprovalRefusal, OriginRefusal) as error:
        raise DeploymentError("Cloud Build source provenance is invalid") from error
    match = _CLOUD_BUILD_RESOURCE.fullmatch(build_resource)
    assert match is not None
    location, build_id = match.groups()
    if (
        receipt.build_resource != build_resource
        or receipt.build_id != build_id
        or receipt.project_id != PROJECT
        or receipt.status != "SUCCESS"
    ):
        raise DeploymentError("Cloud Build identity or status differs")
    if receipt.resolved_source_sha != desired.source_sha:
        raise DeploymentError("Cloud Build resolved source SHA differs from local source")
    if receipt.image_uri != desired.image:
        raise DeploymentError("Cloud Build result image differs from the desired image")
    finish_time = _utc_finish_time(normalized.get("finishTime"))
    images = normalized["results"]["images"]
    image_name = images[0].get("name") if len(images) == 1 else None
    if not isinstance(image_name, str):
        raise DeploymentError("Cloud Build result image name is invalid")
    return CloudBuildAuthority(
        build_resource=build_resource,
        build_id=build_id,
        project_id=PROJECT,
        location=location,
        status="SUCCESS",
        resolved_source_kind="resolvedConnectedRepository",
        resolved_source_sha=receipt.resolved_source_sha,
        result_image_name=image_name,
        result_image_digest=desired.image.rsplit("@", 1)[1],
        finish_time=finish_time,
        build_provenance_sha256=hashlib.sha256(provenance).hexdigest(),
        origin_registry_sha256=generation.origin_registry_sha256,
        resource_manifest_sha256=generation.resource_manifest_sha256,
        origin_mode="new_approval",
    )


def cloud_build_identity_payload(authority: CloudBuildAuthority) -> dict[str, str]:
    if not isinstance(authority, CloudBuildAuthority):
        raise DeploymentError("Cloud Build authority is invalid")
    payload = {
        "build_resource": authority.build_resource,
        "build_id": authority.build_id,
        "project_id": authority.project_id,
        "location": authority.location,
        "status": authority.status,
        "resolved_source_kind": authority.resolved_source_kind,
        "resolved_source_sha": authority.resolved_source_sha,
        "result_image_name": authority.result_image_name,
        "result_image_digest": authority.result_image_digest,
        "finish_time": authority.finish_time,
    }
    for field in (
        "build_provenance_sha256",
        "origin_registry_sha256",
        "resource_manifest_sha256",
        "origin_mode",
    ):
        value = getattr(authority, field)
        if value is not None:
            payload[field] = value
    return payload


def build_brain_deployment_receipt(
    *,
    desired: BrainDesiredJob,
    snapshot: BrainJobSnapshot,
    build_authority: CloudBuildAuthority,
    dependency_lock_sha256: str,
    runtime_sbom_sha256: str,
) -> BrainDeploymentReceipt:
    mismatches = brain_snapshot_mismatches(snapshot, desired)
    if mismatches:
        raise DeploymentError("Brain job readback mismatch: " + ", ".join(mismatches))
    manifest = snapshot.manifest
    manifest_digest = canonical_digest(manifest)
    # Ordinary operations carry no digest annotations, so their readback manifest omits
    # the three unobservable fields; the desired job, already validated against the
    # repository bytes, is the authority for them. The Brain readback must match exactly.
    observed_lock = manifest.get("dependency_lock_sha256", desired.dependency_lock_sha256)
    observed_sbom = manifest.get("runtime_sbom_sha256", desired.runtime_sbom_sha256)
    if (
        manifest.get("source_sha") != build_authority.resolved_source_sha
        or manifest.get("image_uri_with_digest") != desired.image
        or desired.image.rsplit("@", 1)[1] != build_authority.result_image_digest
        or observed_lock != dependency_lock_sha256
        or observed_sbom != runtime_sbom_sha256
        or desired.dependency_lock_sha256 != dependency_lock_sha256
        or desired.runtime_sbom_sha256 != runtime_sbom_sha256
    ):
        raise DeploymentError("Brain deployment receipt authority differs from readback")
    return BrainDeploymentReceipt(
        receipt_contract_version="brain_deployment_receipt_v1",
        source_sha=build_authority.resolved_source_sha,
        image_digest=build_authority.result_image_digest,
        dependency_lock_sha256=dependency_lock_sha256,
        runtime_sbom_sha256=runtime_sbom_sha256,
        service_identity=str(manifest["service_identity"]),
        command=str(manifest["command"]),
        arguments=tuple(manifest["arguments"]),
        deployment_manifest_sha256=manifest_digest,
        build_identity=cloud_build_identity_payload(build_authority),
    )


def render_brain_deployment_receipt(receipt: BrainDeploymentReceipt) -> str:
    if not isinstance(receipt, BrainDeploymentReceipt):
        raise DeploymentError("Brain deployment receipt is invalid")
    return canonical_bytes(receipt).decode("utf-8") + "\n"


def _default_cloud_build_reader(build_resource: str) -> Mapping[str, object]:
    from google.auth import default
    from google.auth.transport.requests import AuthorizedSession

    credentials, _project = default()
    response = AuthorizedSession(credentials).get(
        f"https://cloudbuild.googleapis.com/v1/{build_resource}"
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise DeploymentError("Cloud Build response is invalid")
    return payload


def _brain_describe_command(desired: BrainDesiredJob, executable: str) -> list[str]:
    return [
        executable,
        "run",
        "jobs",
        "describe",
        desired.job,
        f"--project={desired.project}",
        f"--region={desired.region}",
        "--quiet",
        "--format=json",
    ]


def _secret_key(desired: BrainDesiredJob, name: str) -> str:
    if desired.manifest_version != MANIFEST_V2:
        return "latest"
    versions = dict(desired.secret_versions)
    if name not in versions:
        raise DeploymentError(f"secret {name} is missing its exact numeric version")
    return versions[name]


def brain_job_resource(desired: BrainDesiredJob) -> dict[str, object]:
    return {
        "apiVersion": "run.googleapis.com/v1",
        "kind": "Job",
        "metadata": {
            "name": desired.job,
            "namespace": PROJECT_NUMBER,
        },
        "spec": {
            "template": {
                "metadata": {"annotations": dict(desired.annotations), "labels": {}},
                "spec": {
                    "taskCount": 1,
                    "parallelism": 1,
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "image": desired.image,
                                    "command": list(desired.command),
                                    "args": list(desired.args),
                                    "env": [
                                        *(
                                            {"name": key, "value": value}
                                            for key, value in desired.environment
                                        ),
                                        *(
                                            {
                                                "name": name,
                                                "valueFrom": {
                                                    "secretKeyRef": {
                                                        "name": name,
                                                        "key": _secret_key(desired, name),
                                                    }
                                                },
                                            }
                                            for name in desired.secret_names
                                        ),
                                    ],
                                    "resources": {
                                        "limits": {
                                            "cpu": "2"
                                            if desired.operation == "source_snapshot_capture"
                                            else "1",
                                            "memory": desired.memory,
                                        }
                                    },
                                    "volumeMounts": [],
                                }
                            ],
                            "serviceAccountName": desired.service_account,
                            "maxRetries": desired.max_retries,
                            "timeoutSeconds": str(desired.timeout_seconds),
                            "volumes": [],
                        }
                    },
                },
            }
        },
    }


def _brain_replace_command(
    desired: BrainDesiredJob, executable: str, resource_path: str
) -> list[str]:
    return [
        executable,
        "run",
        "jobs",
        "replace",
        resource_path,
        f"--project={desired.project}",
        f"--region={desired.region}",
        "--quiet",
        "--format=none",
    ]


def render_brain_dry_run(desired: BrainDesiredJob) -> str:
    executable = "gcloud"
    describe = _brain_describe_command(desired, executable)
    return json.dumps(
        {
            "mode": "dry-run",
            "desired_snapshot": desired.to_record(),
            "desired_resource": brain_job_resource(desired),
            "actions": {
                "describe": describe,
                "replace": _brain_replace_command(desired, executable, "<absolute-brain-job-yaml>"),
                "readback": list(describe),
            },
            "rollback_plan": ROLLBACK_PLAN,
        },
        indent=2,
    )


def _reconcile_brain_job_readback(desired, *, read_current, mutate):
    try:
        current = read_current()
    except CurrentJobUnparseable:
        # An existing job outside the contract shape is drift; replace it and read back.
        current = BrainJobSnapshot({}, ())
    if current is not None and not brain_snapshot_mismatches(current, desired):
        return "unchanged", current
    operation = "create" if current is None else "update"
    mutate(operation, desired)
    readback = read_current()
    if readback is None:
        raise DeploymentError("Brain job readback mismatch: job is missing")
    mismatches = brain_snapshot_mismatches(readback, desired)
    if mismatches:
        raise DeploymentError("Brain job readback mismatch: " + ", ".join(mismatches))
    return ("created" if operation == "create" else "updated"), readback


def reconcile_brain_job(desired, *, read_current, mutate) -> str:
    result, _readback = _reconcile_brain_job_readback(
        desired, read_current=read_current, mutate=mutate
    )
    return result


def parse_brain_job_snapshot(payload: object, *, origin=None) -> BrainJobSnapshot:
    base = parse_job_snapshot(payload)
    root = _mapping(payload, "root")
    spec = _mapping(root.get("spec"), "spec")
    execution_template = _mapping(spec.get("template"), "spec.template")
    template_metadata = _mapping(execution_template.get("metadata"), "spec.template.metadata")
    annotation_values = _mapping(
        template_metadata.get("annotations"), "spec.template.metadata.annotations"
    )
    if origin is None:
        manifest_version = MANIFEST_V1
        operation = _OPERATION_BY_JOB.get(base.job)
        if operation is None:
            raise DeploymentError("job readback names a job outside the execution matrix")
        if any(version != "latest" for _name, version in base.secret_versions):
            raise DeploymentError("job readback secret binding is not the exact latest binding")
    else:
        manifest_version = MANIFEST_V2
        jobs = {
            binding.job_resource.rsplit("/", 1)[1]: name
            for name, binding in _require_v2_origin(origin).operation_bindings.items()
        }
        operation = jobs.get(base.job)
        if operation is None:
            raise DeploymentError("job readback names a job outside the resolved origin")
        if any(
            _NUMERIC_SECRET_VERSION.fullmatch(version) is None
            for _name, version in base.secret_versions
        ):
            raise DeploymentError("job readback secret binding is not an exact numeric version")
    expected_keys = operation_annotation_keys(operation, manifest_version=manifest_version)
    ours = {key for key in annotation_values if key.startswith("42.ogilvy/")}
    if ours != set(expected_keys):
        raise DeploymentError("job readback annotation set differs from the operation contract")
    annotations = tuple(
        (key, _string(annotation_values.get(key), f"annotation {key}")) for key in expected_keys
    )
    values = dict(annotations)
    if operation == "bootstrap_migration_apply" and (
        values["42.ogilvy/bootstrap-manifest-sha256"]
        != values["42.ogilvy/execution-approval-sha256"]
    ):
        raise DeploymentError("bootstrap job annotations name two different manifests")
    command = (
        f"{base.command[0]} {base.args[0]}" if base.command == ("python",) and base.args else ""
    )
    manifest = {
        "contract_sha256": values.get("42.ogilvy/contract-sha256"),
        "dependency_lock_sha256": values.get("42.ogilvy/dependency-lock-sha256"),
        "runtime_sbom_sha256": values.get("42.ogilvy/runtime-sbom-sha256"),
        "source_sha": values["42.ogilvy/source-sha"],
        "job_resource": f"projects/{base.project}/locations/{base.region}/jobs/{base.job}",
        "region": base.region,
        "image_uri_with_digest": base.image,
        "service_identity": base.service_account,
        "command": command,
        "arguments": base.args[1:],
        "environment_names": tuple(sorted(name for name, _value in base.environment)),
        "secret_names": base.secret_names,
        "memory": base.memory,
        "timeout_seconds": base.timeout_seconds,
        "max_retries": base.max_retries,
        "writable_code_mounts": ("configured",) if base.has_volumes else (),
        "operation": operation,
        **(
            {"cpu": "2" if base.cpu in {"2", "2000m"} else base.cpu}
            if operation == "source_snapshot_capture"
            else {}
        ),
        **(
            {"manifest_version": MANIFEST_V2, "secret_versions": base.secret_versions}
            if manifest_version == MANIFEST_V2
            else {}
        ),
    }
    if operation == "brain_read":
        if values["42.ogilvy/deployment-manifest-sha256"] != canonical_digest(manifest):
            raise DeploymentError("Brain job deployment manifest annotation differs")
        return BrainJobSnapshot(manifest, annotations)
    return BrainJobSnapshot(_observable_manifest(manifest, operation), annotations)


def _is_exact_job_not_found(result: subprocess.CompletedProcess[str], job: str) -> bool:
    if result.returncode != 1 or not isinstance(result.stderr, str):
        return False
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    return len(lines) == 1 and lines[0] in (
        f"ERROR: (gcloud.run.jobs.describe) Cannot find job [{job}].",
        f"ERROR: (gcloud.run.jobs.describe) NOT_FOUND: Job '{job}' was not found.",
    )


def _is_exact_brain_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    return _is_exact_job_not_found(result, BRAIN_JOB)


def _describe_brain(
    desired: BrainDesiredJob, executable: str, runner: Runner, *, origin=None
) -> BrainJobSnapshot | None:
    result = _call(
        runner,
        _brain_describe_command(desired, executable),
        "gcloud Brain describe",
    )
    if result.returncode != 0:
        if _is_exact_job_not_found(result, desired.job):
            return None
        raise DeploymentError("gcloud Brain describe failed")
    if not isinstance(result.stdout, str):
        raise DeploymentError("gcloud Brain describe returned no JSON")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DeploymentError("gcloud Brain describe returned malformed JSON") from exc
    try:
        snapshot = parse_brain_job_snapshot(payload, origin=origin)
    except DeploymentError as exc:
        raise CurrentJobUnparseable(str(exc)) from exc
    if snapshot.manifest.get("job_resource") != (
        f"projects/{desired.project}/locations/{desired.region}/jobs/{desired.job}"
    ):
        raise DeploymentError("gcloud Brain describe returned wrong job identity")
    return snapshot


def apply_brain_job(
    desired: BrainDesiredJob,
    *,
    runner: Runner | None = None,
    resolver: Resolver = shutil.which,
    environment: Mapping[str, str] | None = None,
    build_reader=None,
    origin=None,
    generation=None,
) -> BrainDeploymentReceipt:
    if getattr(desired, "manifest_version", None) != MANIFEST_V2:
        raise DeploymentError(
            "retained v1 jobs are inspection only; apply needs a v2 execution manifest"
        )
    if origin is None or generation is None:
        raise DeploymentError("v2 apply needs one resolved origin and one trusted generation")
    _require_v2_origin(origin)
    _require_generation(generation)
    annotations = dict(desired.annotations)
    if (
        annotations.get("42.ogilvy/origin-registry-sha256") != generation.origin_registry_sha256
        or annotations.get("42.ogilvy/resource-manifest-sha256")
        != generation.resource_manifest_sha256
        or desired.operation not in origin.operation_bindings
    ):
        raise DeploymentError("desired job does not name the selected generation")
    runner = _run_subprocess if runner is None else runner
    _check_local_git(desired.source_sha, runner)
    dependency_authority = derive_brain_dependency_authority(
        supplied_dependency_lock_sha256=desired.dependency_lock_sha256,
        supplied_runtime_sbom_sha256=desired.runtime_sbom_sha256,
    )
    environment = os.environ if environment is None else environment
    build_resource = validate_cloud_build_resource(environment.get("OI_BRAIN_CLOUD_BUILD_RESOURCE"))
    build_reader = _default_cloud_build_reader if build_reader is None else build_reader
    build_payload = build_reader(build_resource)
    build_authority = validate_cloud_build_authority_v2(
        build_payload,
        build_resource=build_resource,
        desired=desired,
        origin=origin,
        generation=generation,
    )
    executable = resolve_gcloud_executable(resolver=resolver)

    def read_current():
        return _describe_brain(desired, executable, runner, origin=origin)

    def mutate(operation, expected):
        import yaml

        with tempfile.TemporaryDirectory(prefix="brain-job-") as directory:
            resource_path = Path(directory) / "job.yaml"
            resource_path.write_text(
                yaml.safe_dump(brain_job_resource(expected), sort_keys=False),
                encoding="utf-8",
                newline="\n",
            )
            result = _call(
                runner,
                _brain_replace_command(expected, executable, str(resource_path.resolve())),
                f"gcloud Brain {operation}",
            )
        if result.returncode != 0:
            raise DeploymentError(f"gcloud Brain {operation} failed")

    _result, readback = _reconcile_brain_job_readback(
        desired=desired,
        read_current=read_current,
        mutate=mutate,
    )
    return build_brain_deployment_receipt(
        desired=desired,
        snapshot=readback,
        build_authority=build_authority,
        dependency_lock_sha256=dependency_authority.dependency_lock_sha256,
        runtime_sbom_sha256=dependency_authority.runtime_sbom_sha256,
    )


def _env_argument(desired: DesiredJob) -> str:
    return ",".join(f"{key}={value}" for key, value in desired.environment)


def _label_argument(desired: DesiredJob) -> str:
    return ",".join(f"{key}={value}" for key, value in desired.labels)


def _describe_command(desired: DesiredJob, executable: str) -> list[str]:
    return [
        executable,
        "run",
        "jobs",
        "describe",
        desired.job,
        f"--project={desired.project}",
        f"--region={desired.region}",
        "--quiet",
        "--format=json",
    ]


def _mutation_command(desired: DesiredJob, executable: str, operation: str) -> list[str]:
    if operation not in {"create", "update"}:
        raise DeploymentError("unsupported mutation operation")
    command = [
        executable,
        "run",
        "jobs",
        operation,
        desired.job,
        f"--project={desired.project}",
        f"--region={desired.region}",
        f"--image={desired.image}",
        f"--service-account={desired.service_account}",
        f"--command={','.join(desired.command)}",
        f"--args={','.join(desired.args)}",
        f"--task-timeout={desired.timeout_seconds}s",
        f"--tasks={desired.tasks}",
        f"--parallelism={desired.parallelism}",
        f"--max-retries={desired.max_retries}",
        f"--cpu={desired.cpu}",
        f"--memory={desired.memory}",
        f"--set-env-vars={_env_argument(desired)}",
    ]
    if operation == "update":
        command.extend(
            [
                "--clear-secrets",
                "--clear-volume-mounts",
                "--clear-volumes",
                "--clear-cloudsql-instances",
                "--vpc-egress=private-ranges-only",
                "--clear-vpc-connector",
                "--clear-network",
                f"--update-labels={_label_argument(desired)}",
            ]
        )
    else:
        command.append(f"--labels={_label_argument(desired)}")
    command.extend(["--quiet", "--format=none"])
    return command


def render_dry_run(desired: DesiredJob) -> str:
    executable = "gcloud"
    describe = _describe_command(desired, executable)
    payload = {
        "mode": "dry-run",
        "desired_snapshot": desired.to_record(),
        "actions": {
            "describe": describe,
            "create_if_missing": _mutation_command(desired, executable, "create"),
            "update_if_present": _mutation_command(desired, executable, "update"),
            "readback": list(describe),
        },
        "readback_checks": list(READBACK_CHECKS),
        "rollback_plan": ROLLBACK_PLAN,
    }
    return json.dumps(payload, indent=2)


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DeploymentError(f"job readback malformed at {field}")
    return value


def _sequence(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise DeploymentError(f"job readback malformed at {field}")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise DeploymentError(f"job readback malformed at {field}")
    return value


def _integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DeploymentError(f"job readback malformed at {field}")
    return value


def _duration_seconds(value: object, field: str) -> int:
    if not isinstance(value, str) or re.fullmatch(r"(?:0|[1-9][0-9]*)", value) is None:
        raise DeploymentError(f"job readback malformed at {field}")
    return int(value)


def _configured_forbidden_annotations(
    annotations: Mapping[str, Any],
) -> frozenset[str]:
    configured: set[str] = set()
    for key in FORBIDDEN_TEMPLATE_ANNOTATIONS:
        if key not in annotations:
            continue
        value = annotations[key]
        if not isinstance(value, str):
            raise DeploymentError("job readback malformed at annotations")
        if value != "":
            configured.add(key)
    return frozenset(configured)


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    items = _sequence(value, field)
    if not all(isinstance(item, str) for item in items):
        raise DeploymentError(f"job readback malformed at {field}")
    return tuple(items)


def _project_from_metadata(metadata: Mapping[str, Any], job: str) -> str:
    namespace = metadata.get("namespace")
    if namespace != PROJECT_NUMBER:
        raise DeploymentError("job readback malformed at project identity")
    self_link = _string(metadata.get("selfLink"), "metadata.selfLink")
    expected_link = f"/apis/run.googleapis.com/v1/namespaces/{PROJECT_NUMBER}/jobs/{job}"
    if self_link != expected_link:
        if self_link.endswith(f"/jobs/{job}"):
            raise DeploymentError("job readback malformed at project identity")
        raise DeploymentError("job readback malformed at job identity")
    return PROJECT


def _parse_environment(
    value: object,
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    items = _sequence(value, "container.env")
    environment: list[tuple[str, str]] = []
    secret_bindings: list[tuple[str, str]] = []
    names: set[str] = set()
    for item in items:
        entry = _mapping(item, "container.env entry")
        name = _string(entry.get("name"), "container.env.name")
        if name in names:
            raise DeploymentError("job readback ambiguous duplicate environment name")
        names.add(name)
        if "valueFrom" in entry:
            value_from = _mapping(entry["valueFrom"], "container.env.valueFrom")
            secret_ref = _mapping(value_from.get("secretKeyRef"), "container.env.secretKeyRef")
            secret_name = _string(secret_ref.get("name"), "container.env.secretKeyRef.name")
            key = secret_ref.get("key")
            if (
                secret_name != name
                or not isinstance(key, str)
                or (key != "latest" and _NUMERIC_SECRET_VERSION.fullmatch(key) is None)
            ):
                raise DeploymentError("job readback secret binding is not the exact latest binding")
            secret_bindings.append((name, key))
            continue
        value_string = _string(entry.get("value"), "container.env.value")
        environment.append((name, value_string))
    return tuple(sorted(environment)), tuple(sorted(secret_bindings))


def parse_job_snapshot(payload: object) -> JobSnapshot:
    """Parse the current nested Cloud Run v1 job JSON into one safe snapshot."""
    root = _mapping(payload, "root")
    if root.get("apiVersion") != "run.googleapis.com/v1":
        raise DeploymentError("job readback malformed at apiVersion")
    if root.get("kind") != "Job":
        raise DeploymentError("job readback malformed at kind")
    metadata = _mapping(root.get("metadata"), "metadata")
    job = _string(metadata.get("name"), "metadata.name")
    project = _project_from_metadata(metadata, job)
    resource_labels = _mapping(metadata.get("labels"), "metadata.labels")
    region = _string(
        resource_labels.get("cloud.googleapis.com/location"),
        "metadata.labels.cloud.googleapis.com/location",
    )

    spec = _mapping(root.get("spec"), "spec")
    execution_template = _mapping(spec.get("template"), "spec.template")
    template_metadata = _mapping(execution_template.get("metadata"), "spec.template.metadata")
    template_labels = _mapping(template_metadata.get("labels", {}), "spec.template.metadata.labels")
    annotations_value = template_metadata.get("annotations", {})
    annotations = _mapping(annotations_value, "spec.template.metadata.annotations")
    execution_spec = _mapping(execution_template.get("spec"), "spec.template.spec")
    tasks = _integer(execution_spec.get("taskCount"), "taskCount")
    parallelism = _integer(execution_spec.get("parallelism"), "parallelism")
    task_template = _mapping(execution_spec.get("template"), "spec.template.spec.template")
    pod_spec = _mapping(task_template.get("spec"), "spec.template.spec.template.spec")
    service_account = _string(pod_spec.get("serviceAccountName"), "serviceAccountName")
    max_retries = _integer(pod_spec.get("maxRetries"), "maxRetries")
    timeout_seconds = _duration_seconds(pod_spec.get("timeoutSeconds"), "timeoutSeconds")
    containers = _sequence(pod_spec.get("containers"), "containers")
    if len(containers) != 1:
        raise DeploymentError("job readback must contain exactly one container")
    container = _mapping(containers[0], "container")
    image = _string(container.get("image"), "container.image")
    command = _string_tuple(container.get("command"), "container.command")
    args = _string_tuple(container.get("args"), "container.args")
    environment, secret_bindings = _parse_environment(container.get("env"))
    has_secret_refs = bool(secret_bindings)
    resources = _mapping(container.get("resources"), "container.resources")
    limits = _mapping(resources.get("limits"), "container.resources.limits")
    cpu = _string(limits.get("cpu"), "container.resources.limits.cpu")
    memory = _string(limits.get("memory"), "container.resources.limits.memory")

    volumes = pod_spec.get("volumes", [])
    volume_mounts = container.get("volumeMounts", [])
    has_volumes = bool(
        _sequence(volumes, "volumes") or _sequence(volume_mounts, "container.volumeMounts")
    )
    forbidden_annotations = _configured_forbidden_annotations(annotations)
    has_cloudsql = "run.googleapis.com/cloudsql-instances" in forbidden_annotations
    has_secret_refs = has_secret_refs or "run.googleapis.com/secrets" in forbidden_annotations
    has_vpc = bool(
        forbidden_annotations
        - {
            "run.googleapis.com/secrets",
            "run.googleapis.com/cloudsql-instances",
        }
    )

    def selected_labels(labels: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
        values: list[tuple[str, str]] = []
        for key in ("environment", "source-sha"):
            value = labels.get(key)
            if value is not None and not isinstance(value, str):
                raise DeploymentError("job readback malformed at labels")
            if isinstance(value, str):
                values.append((key, value))
        return tuple(values)

    return JobSnapshot(
        project,
        region,
        job,
        service_account,
        image,
        command,
        args,
        timeout_seconds,
        tasks,
        parallelism,
        max_retries,
        cpu,
        memory,
        environment,
        selected_labels(resource_labels),
        selected_labels(template_labels),
        has_secret_refs,
        has_volumes,
        has_cloudsql,
        has_vpc,
        tuple(name for name, _version in secret_bindings),
        secret_bindings,
    )


def _snapshot_mismatches(snapshot: JobSnapshot, desired: DesiredJob) -> tuple[str, ...]:
    expected_environment = tuple(sorted(desired.environment))
    expected_labels = desired.labels
    checks = {
        "project": snapshot.project == desired.project,
        "region": snapshot.region == desired.region,
        "job": snapshot.job == desired.job,
        "service_account": snapshot.service_account == desired.service_account,
        "image": snapshot.image == desired.image,
        "command": snapshot.command == desired.command,
        "args": snapshot.args == desired.args,
        "timeout": snapshot.timeout_seconds == desired.timeout_seconds,
        "tasks": snapshot.tasks == desired.tasks,
        "parallelism": snapshot.parallelism == desired.parallelism,
        "retries": snapshot.max_retries == desired.max_retries,
        "cpu": snapshot.cpu == desired.cpu,
        "memory": snapshot.memory == desired.memory,
        "environment": snapshot.environment == expected_environment,
        "resource_labels": snapshot.resource_labels == expected_labels,
        "template_labels": snapshot.template_labels == expected_labels,
        "secret_refs": not snapshot.has_secret_refs,
        "volumes": not snapshot.has_volumes,
        "cloudsql": not snapshot.has_cloudsql,
        "vpc": not snapshot.has_vpc,
    }
    return tuple(name for name, matches in checks.items() if not matches)


def _run_subprocess(command: list[str]) -> subprocess.CompletedProcess[str]:
    if not isinstance(command, list):
        raise DeploymentError("subprocess command must use list arguments")
    return subprocess.run(
        command,
        shell=False,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


def _call(runner: Runner, command: list[str], action: str) -> subprocess.CompletedProcess[str]:
    if not isinstance(command, list):
        raise DeploymentError("subprocess command must use list arguments")
    try:
        result = runner(command)
    except DeploymentError:
        raise
    except Exception as exc:
        raise DeploymentError(f"{action} failed") from exc
    if not isinstance(getattr(result, "returncode", None), int):
        raise DeploymentError(f"{action} returned no status")
    return result


def _check_local_git(source_sha: str, runner: Runner) -> None:
    status = _call(
        runner,
        ["git", "status", "--porcelain", "--untracked-files=no"],
        "git status",
    )
    if status.returncode != 0 or not isinstance(status.stdout, str):
        raise DeploymentError("git status failed")
    if status.stdout.strip():
        raise DeploymentError("tracked tree is dirty")
    head = _call(runner, ["git", "rev-parse", "HEAD"], "git HEAD")
    if head.returncode != 0 or not isinstance(head.stdout, str):
        raise DeploymentError("git HEAD failed")
    if head.stdout.strip() != source_sha:
        raise DeploymentError("local HEAD does not equal source SHA")


def resolve_gcloud_executable(*, resolver: Resolver = shutil.which) -> str:
    for candidate in ("gcloud.cmd", "gcloud"):
        executable = resolver(candidate)
        if executable:
            return executable
    raise DeploymentError("gcloud executable not found")


def _is_exact_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode != 1:
        return False
    stderr = getattr(result, "stderr", None)
    if not isinstance(stderr, str):
        return False
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if len(lines) != 1:
        return False
    return lines[0] in (
        f"ERROR: (gcloud.run.jobs.describe) Cannot find job [{JOB}].",
        f"ERROR: (gcloud.run.jobs.describe) NOT_FOUND: Job '{JOB}' was not found.",
    )


def _describe(desired: DesiredJob, executable: str, runner: Runner) -> JobSnapshot | None:
    result = _call(
        runner,
        _describe_command(desired, executable),
        "gcloud describe",
    )
    if result.returncode != 0:
        if _is_exact_not_found(result):
            return None
        raise DeploymentError("gcloud describe failed")
    if not isinstance(result.stdout, str):
        raise DeploymentError("gcloud describe returned no JSON")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DeploymentError("gcloud describe returned malformed JSON") from exc
    snapshot = parse_job_snapshot(payload)
    if (
        snapshot.job != desired.job
        or snapshot.project != desired.project
        or snapshot.region != desired.region
    ):
        raise DeploymentError("gcloud describe returned wrong job identity")
    return snapshot


def _mutate(
    desired: DesiredJob,
    executable: str,
    runner: Runner,
    operation: str,
) -> None:
    result = _call(
        runner,
        _mutation_command(desired, executable, operation),
        f"gcloud {operation}",
    )
    if result.returncode != 0:
        raise DeploymentError(f"gcloud {operation} failed")


def apply_job(
    image: str,
    source_sha: str,
    *,
    runner: Runner = _run_subprocess,
    resolver: Resolver = shutil.which,
) -> str:
    desired = desired_job(image, source_sha)
    _check_local_git(source_sha, runner)
    executable = resolve_gcloud_executable(resolver=resolver)
    current = _describe(desired, executable, runner)
    if current is not None and not _snapshot_mismatches(current, desired):
        return "unchanged"
    operation = "create" if current is None else "update"
    _mutate(desired, executable, runner, operation)
    try:
        readback = _describe(desired, executable, runner)
    except DeploymentError as exc:
        raise DeploymentError("job readback mismatch") from exc
    if readback is None:
        raise DeploymentError("job readback mismatch: job is missing")
    mismatches = _snapshot_mismatches(readback, desired)
    if mismatches:
        raise DeploymentError("job readback mismatch: " + ", ".join(mismatches))
    return "created" if operation == "create" else "updated"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or apply the immutable Open Intelligence staging job."
    )
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--brain", action="store_true")
    parser.add_argument("--dependency-lock-sha256")
    parser.add_argument("--runtime-sbom-sha256")
    parser.add_argument("--brain-argument", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    return parser


def _parse_operation_cli(argv: tuple[str, ...]) -> tuple[str, Path, str, str, str, bool]:
    if argv[:1] == ("--brain",):
        operation = "brain_read"
        rest = argv[1:]
    elif argv[:1] == ("--operation",) and len(argv) >= 2:
        operation = argv[1]
        rest = argv[2:]
    else:
        raise DeploymentError("Brain deployment CLI is invalid")
    if (
        operation not in OPERATION_JOBS
        or len(rest) not in {8, 9}
        or rest[0] != "--execution-manifest-file"
        or rest[2] != "--execution-manifest-sha256"
        or rest[4] != "--dependency-lock-sha256"
        or rest[6] != "--runtime-sbom-sha256"
        or (len(rest) == 9 and rest[8] != "--apply")
    ):
        raise DeploymentError("Brain deployment CLI is invalid")
    manifest_path = Path(rest[1])
    if not manifest_path.is_absolute():
        raise DeploymentError("Brain deployment CLI is invalid")
    try:
        manifest_sha256 = _brain_digest(rest[3], "execution manifest digest")
        dependency_lock_sha256 = _brain_digest(rest[5], "dependency lock digest")
        runtime_sbom_sha256 = _brain_digest(rest[7], "runtime SBOM digest")
    except DeploymentError as error:
        raise DeploymentError("Brain deployment CLI is invalid") from error
    return (
        operation,
        manifest_path,
        manifest_sha256,
        dependency_lock_sha256,
        runtime_sbom_sha256,
        len(rest) == 9,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner = _run_subprocess,
    resolver: Resolver = shutil.which,
) -> int:
    values = tuple(sys.argv[1:] if argv is None else argv)
    if "--brain" in values or "--operation" in values:
        (
            operation,
            manifest_path,
            manifest_sha256,
            dependency_lock_sha256,
            runtime_sbom_sha256,
            apply,
        ) = _parse_operation_cli(values)
        generation = _active_generation()
        origin = None
        if values[0] == "--brain":
            manifest = load_brain_execution_manifest(
                manifest_path, manifest_sha256, generation=generation
            )
        else:
            manifest = load_operation_execution_manifest(
                manifest_path, manifest_sha256, operation, generation=generation
            )
        if getattr(manifest, "manifest_version", None) == MANIFEST_V2:
            origin = origin_for_manifest(manifest, generation=generation)
            desired_brain = desired_operation_job_from_execution_manifest(
                manifest,
                operation=operation,
                execution_approval_sha256=manifest_sha256,
                dependency_lock_sha256=dependency_lock_sha256,
                runtime_sbom_sha256=runtime_sbom_sha256,
                origin=origin,
                generation=generation,
            )
        elif values[0] == "--brain":
            desired_brain = desired_brain_job_from_execution_manifest(
                manifest,
                execution_approval_sha256=manifest_sha256,
                dependency_lock_sha256=dependency_lock_sha256,
                runtime_sbom_sha256=runtime_sbom_sha256,
            )
        else:
            bootstrap_generations = None
            if operation == "bootstrap_migration_apply":
                bootstrap_generations = (
                    os.environ.get("OPEN_INTELLIGENCE_BOOTSTRAP_MANIFEST_GENERATION", ""),
                    os.environ.get("OPEN_INTELLIGENCE_BOOTSTRAP_SIGNATURE_GENERATION", ""),
                )
            desired_brain = desired_operation_job_from_execution_manifest(
                manifest,
                operation=operation,
                bootstrap_generations=bootstrap_generations,
                execution_approval_sha256=manifest_sha256,
                dependency_lock_sha256=dependency_lock_sha256,
                runtime_sbom_sha256=runtime_sbom_sha256,
            )
        if not apply:
            print(render_brain_dry_run(desired_brain))
            return 0
        receipt = apply_brain_job(
            desired_brain,
            runner=runner,
            resolver=resolver,
            origin=origin,
            generation=None if origin is None else generation,
        )
        sys.stdout.write(render_brain_deployment_receipt(receipt))
        return 0
    args = _parser().parse_args(values)
    desired = desired_job(args.image_digest, args.source_sha)
    if not args.apply:
        print(render_dry_run(desired))
        return 0
    result = apply_job(
        desired.image,
        desired.source_sha,
        runner=runner,
        resolver=resolver,
    )
    print(json.dumps({"mode": "apply", "result": result}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DeploymentError, OriginRefusal) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
