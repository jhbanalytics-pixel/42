import hashlib
import importlib
import json
import os
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import KW_ONLY, InitVar, dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from types import MappingProxyType
from weakref import WeakKeyDictionary

from . import execution_generations
from .execution_origins import (
    _MODES,
    ExecutionOrigin,
    OriginRefusal,
    OriginRegistry,
    _policy_bytes_for_origin,
    resolve_origin,
    select_origin,
)

_PROJECT = "ogilvy-trends-v2"
_MANIFEST_VERSION = "open_intelligence_execution_manifest_v1"
_APPROVAL_VERSION = "open_intelligence_execution_approval_v1"
_CONSUMPTION_VERSION = "open_intelligence_execution_consumption_v1"
_RESULT_VERSION = "open_intelligence_execution_result_v1"
_APPROVAL_VERSION_V2 = "open_intelligence_execution_approval_v2"
_CONSUMPTION_VERSION_V2 = "open_intelligence_execution_consumption_v2"
_RESULT_VERSION_V2 = "open_intelligence_execution_result_v2"
# Reader routine version: the v3 read routine serves v2 manifest rows and the recurring
# grant kind; every row it returns still carries the v2 approval contract version.
_APPROVAL_VERSION_V3 = "open_intelligence_execution_approval_v3"
_RECURRING_GRANT_CONTRACT = "42_recurring_execution_grant_v1"
_RECURRING_GRANT_KIND = "recurring_grant_v1"
_RECURRING_GRANT_REVOCATION_KIND = "recurring_grant_revocation_v1"
_RECURRING_GRANT_ROW_FIELDS = ("revocation_state", "revoked_at")
_BUILD_PROVENANCE_VERSION = "open_intelligence_build_provenance_v1"
_BOOTSTRAP_CONTRACT_SHA256 = "527798d33d83382f77586fe9869f4456371fce5370a21ad4316d2e4821969545"
_SOURCE_SNAPSHOT_CONTRACT_SHA256 = (
    "5dcd9346af27fd7dac97efdd138fce19b56813e0d1eb6629aebcfa9b0233595f"
)
_APPROVED_BY = "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab"
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_BRAIN_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}")
_BRAIN_SIGNAL_ID = re.compile(r"sig_[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
_BUILD_RESOURCE = re.compile(r"projects/ogilvy-trends-v2/locations/(us-central1)/builds/([^/]+)")
_PROJECT_NUMBER = "590353929363"
_RAW_BUILD_RESOURCE = re.compile(
    r"projects/(ogilvy-trends-v2|590353929363)/locations/(us-central1)/builds/([^/]+)"
)
_EXECUTION_RESOURCE = re.compile(
    r"projects/ogilvy-trends-v2/locations/us-central1/jobs/([^/]+)/executions/([^/]+)"
)
_IMAGE_URI = re.compile(
    r"us-central1-docker\.pkg\.dev/ogilvy-trends-v2/pipeline/"
    r"trends-engine@sha256:[0-9a-f]{64}"
)
# The engine image is built from a connected repository revision with the build's dir
# set to engine (cloudbuild.publish.yaml), so its describe response names that subtree as
# source.connectedRepository.dir. The directory an origin row expects is pinned here by
# image repository and never read from the build; a row absent from this map expects none.
_CONNECTED_REPOSITORY_DIR = {
    "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine": "engine",
}
_LIMIT_FIELDS = (
    "max_bytes_billed",
    "max_credits",
    "max_model_calls",
    "max_rows_written",
)
_MANIFEST_FIELDS = (
    "manifest_version",
    "operation",
    "contract_sha256",
    "project",
    "datasets",
    "location",
    "job_resource",
    "service_identity",
    "source_sha",
    "image_uri",
    "build_resource",
    "command",
    "arguments",
    "environment",
    "secrets",
    "max_retries",
    "timeout_seconds",
    "input_artifacts",
    "limits",
    "expires_at",
)
_BUILD_RESPONSE_FIELDS = (
    "name",
    "id",
    "projectId",
    "status",
    "results",
    "finishTime",
)

_OPERATION_CONTRACTS = {
    "source_snapshot_capture": {
        "job": "trends-engine-oi-source-snapshot-staging",
        "identity": "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ("trends_v2_dev", "trends_v2_staging"),
        "secrets": (),
        "command": ("python",),
        "dynamic_arguments": "source_snapshot_capture",
        "limits": (1_000_000_000, 0, 0, 0),
        "artifacts": (
            "build_provenance",
            "capture_contract",
            "capture_plan",
            "recovery_context",
            "source_metadata",
            "storage_policy",
        ),
    },
    "bootstrap_migration_apply": {
        "job": "trends-engine-oi-approval-bootstrap-staging",
        "identity": "trends-engine-oi-bootstrap@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ("trends_v2_staging_approvals",),
        "environment": (
            ("BIGQUERY_DATASET", "trends_v2_staging_approvals"),
            ("GCP_PROJECT", _PROJECT),
            ("TRENDS_ENV", "staging"),
        ),
        "secrets": (),
        "command": ("python",),
        "arguments": (
            "scripts/migrations/create_open_intelligence_execution_approval_store.py",
            "apply",
        ),
        "limits": (0, 0, 0, 4),
        "artifacts": (
            "bootstrap_contract",
            "build_provenance",
            "iam_plan",
            "kms_public_key",
            "migration_dry_run",
            "migration_plan",
        ),
    },
    "migration_apply": {
        "job": "trends-engine-oi-migration-staging",
        "identity": "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ("trends_v2_staging",),
        "secrets": (),
        "command": ("python",),
        "arguments": ("scripts/migrations/create_open_intelligence_v2.py", "apply"),
        "limits": (0, 0, 0, 1),
        "artifacts": (
            "build_provenance",
            "cloud_build",
            "migration_contract",
            "migration_dry_run",
            "migration_plan",
        ),
    },
    "collection_exposure_issue": {
        "job": "trends-engine-oi-exposure-issuer-staging",
        "identity": "trends-engine-oi-exposure@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ("trends_v2_staging",),
        "secrets": (),
        "command": ("python",),
        "arguments": ("scripts/staging/issue_collection_exposure_receipts.py",),
        "limits": (100, 0, 0, 7),
        "artifacts": (
            "build_provenance",
            "config",
            "issuer_contract",
            "source_copy_receipt_set",
            "vendor_quota_receipt",
        ),
    },
    "r3_apply": {
        "job": "trends-engine-oi-apply-staging",
        "identity": "trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ("trends_v2_staging",),
        "secrets": (),
        "command": ("python",),
        "arguments": ("scripts/staging/replay_open_intelligence.py",),
        "limits": (100, 0, 0, 7),
        "artifacts": (
            "build_provenance",
            "config",
            "exposure_execution_proof",
            "exposure_receipt_readback",
            "inserted_natural_key_set",
            "quality_review_receipt",
            "r3_contract",
            "source_window_receipt_set",
        ),
    },
    "r3_proof_issue": {
        "job": "trends-engine-oi-r3-proof-staging",
        "identity": "trends-engine-oi-r3-proof@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ("trends_v2_staging",),
        "secrets": (),
        "command": ("python",),
        "argument_prefix": (
            "scripts/staging/issue_r3_execution_proof.py",
            "--r3-execution-name",
        ),
        "limits": (0, 0, 0, 0),
        "artifacts": (
            "blocked_run_receipt",
            "build_provenance",
            "proof_issuer_contract",
        ),
    },
    "r3_release": {
        "job": "trends-engine-oi-release-staging",
        "identity": "trends-engine-oi-r3-release@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ("trends_v2_staging",),
        "secrets": (),
        "command": ("python",),
        "arguments": ("scripts/staging/release_open_intelligence_run.py",),
        "limits": (0, 0, 0, 2),
        "artifacts": (
            "blocked_run_receipt",
            "build_provenance",
            "execution_proof",
            "quality_review_receipt",
            "release_contract",
        ),
    },
    "brain_read": {
        "job": "trends-engine-oi-brain-staging",
        "identity": "trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ("trends_v2_staging",),
        "secrets": (),
        "command": ("python",),
        "dynamic_arguments": "brain_read",
        "limits": (0, 0, 0, 0),
        "artifacts": (
            "brain_contract",
            "build_provenance",
            "run_receipt",
            "source_window_receipt_set",
        ),
    },
    "wave1_pilot": {
        "job": "trends-engine-open-intelligence-staging",
        "identity": "trends-engine-oi-wave1@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ("trends_v2_staging", "trends_v2_staging_funded"),
        "environment": (
            ("BIGQUERY_DATASET", "trends_v2_staging"),
            ("GCP_PROJECT", _PROJECT),
            ("SOCIALCRAWL_CREDENTIAL_LANE", "ogilvy_funded"),
            ("SOCIALCRAWL_FUNDED_STAGE_NAME", "stage_1_wave_1"),
            ("TRENDS_ENV", "staging"),
        ),
        "secrets": ("SOCIALCRAWL_OGILVY_API_KEY",),
        "command": ("python",),
        "arguments": ("scripts/run_rss_now.py",),
        "limits": (50_000_000_000, 63, 0, 100),
        "artifacts": (
            "build_provenance",
            "funded_preflight",
            "gdelt_dry_run_set",
            "r3_seed_manifest",
            "source_lab_snapshot",
            "wave1_contract",
        ),
    },
}
_DURABLE_ANNOTATIONS = frozenset({"42.ogilvy/execution-approval-sha256", "42.ogilvy/source-sha"})
_BRAIN_DURABLE_ANNOTATIONS = frozenset(
    {
        "42.ogilvy/contract-sha256",
        "42.ogilvy/dependency-lock-sha256",
        "42.ogilvy/runtime-sbom-sha256",
        "42.ogilvy/source-sha",
        "42.ogilvy/deployment-manifest-sha256",
        "42.ogilvy/execution-approval-sha256",
    }
)
_DEFAULT_ENVIRONMENT = (
    ("BIGQUERY_DATASET", "trends_v2_staging"),
    ("GCP_PROJECT", _PROJECT),
    ("TRENDS_ENV", "staging"),
)
_ANNOTATION_REGISTRY = "42.ogilvy/origin-registry-sha256"
_ANNOTATION_RESOURCE = "42.ogilvy/resource-manifest-sha256"
_GENERATION_ANNOTATIONS = frozenset({_ANNOTATION_REGISTRY, _ANNOTATION_RESOURCE})
_HISTORICAL_MODES = _MODES[:2]
_SECRET_VERSION = re.compile(r"[1-9][0-9]*")
_APPROVAL_ROW_FIELDS = (
    "approval_contract_version",
    "approval_id",
    "manifest_version",
    "operation",
    "contract_sha256",
    "manifest_sha256",
    "canonical_manifest_json",
    "approved_by",
    "approved_at",
    "expires_at",
    "approval_phrase_sha256",
)
_CONSUMPTION_ROW_FIELDS = (
    "consumption_contract_version",
    "consumption_id",
    "approval_id",
    "manifest_sha256",
    "operation",
    "execution_name",
    "job_resource",
    "source_sha",
    "image_uri",
    "consumed_at",
)
_RESULT_ROW_FIELDS = (
    "result_contract_version",
    "result_id",
    "consumption_id",
    "approval_id",
    "manifest_sha256",
    "operation",
    "execution_name",
    "result_reference",
    "canonical_result_json",
    "result_digest",
    "status",
    "completed_at",
)
_GENERATION_FIELDS = ("origin_registry_sha256", "resource_manifest_sha256")


class ApprovalRefusal(ValueError):
    pass


def _is_unresolved_result(error: BaseException) -> bool:
    return isinstance(error, ApprovalRefusal) and str(error) == "execution_result_unresolved"


@dataclass(frozen=True, slots=True)
class ExecutionManifest:
    manifest_version: str
    operation: str
    contract_sha256: str
    project: str
    datasets: tuple[str, ...]
    location: str
    job_resource: str
    service_identity: str
    source_sha: str
    image_uri: str
    build_resource: str
    command: tuple[str, ...]
    arguments: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    secrets: tuple[str, ...]
    max_retries: int
    timeout_seconds: int
    input_artifacts: tuple[tuple[str, str], ...]
    limits: Mapping[str, int]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class BuildProvenanceReceipt:
    build_provenance_contract_version: str
    build_resource: str
    project_id: str
    location: str
    build_id: str
    status: str
    resolved_source_sha: str
    image_uri: str
    operation_contract_sha256: str
    finished_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionApproval:
    approval_contract_version: str
    approval_id: str
    manifest_version: str
    operation: str
    contract_sha256: str
    manifest_sha256: str
    canonical_manifest_json: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    approval_phrase_sha256: str

    def __post_init__(self) -> None:
        code = "execution_approval_manifest_invalid"
        if not isinstance(self.approved_at, datetime) or not isinstance(
            self.expires_at,
            datetime,
        ):
            raise ApprovalRefusal("execution_approval_expired")
        try:
            horizon = self.expires_at - self.approved_at
        except TypeError as exc:
            raise ApprovalRefusal("execution_approval_expired") from exc
        if (
            self.approved_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or horizon <= timedelta(0)
            or horizon > timedelta(hours=24)
        ):
            raise ApprovalRefusal("execution_approval_expired")
        manifest_payload, canonical_bytes = _canonical_json_object(
            self.canonical_manifest_json,
            code,
        )
        manifest = _validate_execution_manifest_v1(manifest_payload)
        if (
            self.approval_contract_version != _APPROVAL_VERSION
            or not isinstance(self.approval_id, str)
            or re.fullmatch(r"exa_[0-9a-f]{64}", self.approval_id) is None
            or self.manifest_version != _MANIFEST_VERSION
            or self.operation != manifest.operation
            or self.contract_sha256 != manifest.contract_sha256
            or not isinstance(self.manifest_sha256, str)
            or _HEX_64.fullmatch(self.manifest_sha256) is None
            or hashlib.sha256(canonical_bytes).hexdigest() != self.manifest_sha256
            or self.approved_by != _APPROVED_BY
            or not isinstance(self.approval_phrase_sha256, str)
            or _HEX_64.fullmatch(self.approval_phrase_sha256) is None
            or _approval_phrase_sha256(self.operation, self.manifest_sha256)
            != self.approval_phrase_sha256
            or manifest.expires_at != self.expires_at
            or approval_id(self.manifest_sha256, self.approved_by, self.approved_at)
            != self.approval_id
        ):
            _refuse(code)


@dataclass(frozen=True, slots=True)
class ExecutionConsumption:
    consumption_contract_version: str
    consumption_id: str
    approval_id: str
    manifest_sha256: str
    operation: str
    execution_name: str
    job_resource: str
    source_sha: str
    image_uri: str
    consumed_at: datetime

    def __post_init__(self) -> None:
        code = "execution_approval_manifest_invalid"
        contract = (
            _OPERATION_CONTRACTS.get(self.operation) if _is_exact_string(self.operation) else None
        )
        execution_match = (
            _EXECUTION_RESOURCE.fullmatch(self.execution_name)
            if isinstance(self.execution_name, str)
            else None
        )
        expected_job = (
            f"projects/{_PROJECT}/locations/us-central1/jobs/{contract['job']}"
            if contract is not None
            else None
        )
        if (
            self.consumption_contract_version != _CONSUMPTION_VERSION
            or not isinstance(self.consumption_id, str)
            or re.fullmatch(r"exc_[0-9a-f]{64}", self.consumption_id) is None
            or not isinstance(self.approval_id, str)
            or re.fullmatch(r"exa_[0-9a-f]{64}", self.approval_id) is None
            or not isinstance(self.manifest_sha256, str)
            or _HEX_64.fullmatch(self.manifest_sha256) is None
            or contract is None
            or self.job_resource != expected_job
            or execution_match is None
            or execution_match.group(1) != contract["job"]
            or not isinstance(self.source_sha, str)
            or _GIT_SHA.fullmatch(self.source_sha) is None
            or not isinstance(self.image_uri, str)
            or _IMAGE_URI.fullmatch(self.image_uri) is None
            or consumption_id(self.approval_id, self.execution_name, self.consumed_at)
            != self.consumption_id
        ):
            _refuse(code)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    result_contract_version: str
    result_id: str
    consumption_id: str
    approval_id: str
    manifest_sha256: str
    operation: str
    execution_name: str
    result_reference: str
    canonical_result_json: str
    result_digest: str
    status: str
    completed_at: datetime

    def __post_init__(self) -> None:
        code = "execution_approval_manifest_invalid"
        contract = (
            _OPERATION_CONTRACTS.get(self.operation) if _is_exact_string(self.operation) else None
        )
        execution_match = (
            _EXECUTION_RESOURCE.fullmatch(self.execution_name)
            if isinstance(self.execution_name, str)
            else None
        )
        _, canonical_bytes = _canonical_json_object(self.canonical_result_json, code)
        if (
            self.result_contract_version != _RESULT_VERSION
            or not isinstance(self.result_id, str)
            or re.fullmatch(r"exr_[0-9a-f]{64}", self.result_id) is None
            or not isinstance(self.consumption_id, str)
            or re.fullmatch(r"exc_[0-9a-f]{64}", self.consumption_id) is None
            or not isinstance(self.approval_id, str)
            or re.fullmatch(r"exa_[0-9a-f]{64}", self.approval_id) is None
            or not isinstance(self.manifest_sha256, str)
            or _HEX_64.fullmatch(self.manifest_sha256) is None
            or contract is None
            or execution_match is None
            or execution_match.group(1) != contract["job"]
            or not _is_exact_string(self.result_reference)
            or not isinstance(self.result_digest, str)
            or _HEX_64.fullmatch(self.result_digest) is None
            or hashlib.sha256(canonical_bytes).hexdigest() != self.result_digest
            or not isinstance(self.status, str)
            or self.status not in {"succeeded", "failed"}
            or result_id(
                self.consumption_id,
                self.result_reference,
                self.result_digest,
                self.status,
                self.completed_at,
            )
            != self.result_id
        ):
            _refuse(code)


@dataclass(frozen=True, slots=True)
class ExecutionApprovalV2:
    approval_contract_version: str
    approval_id: str
    manifest_version: str
    operation: str
    contract_sha256: str
    manifest_sha256: str
    canonical_manifest_json: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    approval_phrase_sha256: str
    origin_registry_sha256: str
    resource_manifest_sha256: str
    _: KW_ONLY
    mode: InitVar[object]
    registry: InitVar[object]
    expected_resource_manifest_sha256: InitVar[object]

    def __post_init__(self, mode, registry, expected_resource_manifest_sha256) -> None:
        _validate_approval_v2(
            self,
            mode=mode,
            registry=registry,
            expected_resource_manifest_sha256=expected_resource_manifest_sha256,
        )


@dataclass(frozen=True, slots=True)
class RecurringGrantApproval:
    """A recurring execution grant approval row (kind recurring_grant_v1).

    The row lives in the v2 approval store: manifest_sha256 is the grant digest (the
    canonical terms without revocation_state), canonical_manifest_json is the grant
    document, contract_sha256 is the proposal file sha256 the approval phrase names,
    expires_at is the grant's valid_until, and the v3 read routine adds the revocation
    state it derives from the revocation row keyed by the same digest.
    """

    approval_contract_version: str
    approval_id: str
    manifest_version: str
    operation: str
    contract_sha256: str
    manifest_sha256: str
    canonical_manifest_json: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    approval_phrase_sha256: str
    origin_registry_sha256: str
    resource_manifest_sha256: str
    revocation_state: str
    revoked_at: datetime | None
    _: KW_ONLY
    mode: InitVar[object]
    registry: InitVar[object]
    expected_resource_manifest_sha256: InitVar[object]

    def __post_init__(self, mode, registry, expected_resource_manifest_sha256) -> None:
        _validate_recurring_grant_approval(
            self,
            mode=mode,
            registry=registry,
            expected_resource_manifest_sha256=expected_resource_manifest_sha256,
        )

    def grant(self) -> dict[str, object]:
        return json.loads(self.canonical_manifest_json)


@dataclass(frozen=True, slots=True)
class ExecutionConsumptionV2:
    consumption_contract_version: str
    consumption_id: str
    approval_id: str
    manifest_sha256: str
    operation: str
    execution_name: str
    job_resource: str
    source_sha: str
    image_uri: str
    consumed_at: datetime
    origin_registry_sha256: str
    resource_manifest_sha256: str
    _: KW_ONLY
    mode: InitVar[object]
    registry: InitVar[object]
    expected_resource_manifest_sha256: InitVar[object]

    def __post_init__(self, mode, registry, expected_resource_manifest_sha256) -> None:
        _validate_consumption_v2(
            self,
            mode=mode,
            registry=registry,
            expected_resource_manifest_sha256=expected_resource_manifest_sha256,
        )


@dataclass(frozen=True, slots=True)
class ExecutionResultV2:
    result_contract_version: str
    result_id: str
    consumption_id: str
    approval_id: str
    manifest_sha256: str
    operation: str
    execution_name: str
    result_reference: str
    canonical_result_json: str
    result_digest: str
    status: str
    completed_at: datetime
    origin_registry_sha256: str
    resource_manifest_sha256: str
    _: KW_ONLY
    mode: InitVar[object]
    registry: InitVar[object]
    expected_resource_manifest_sha256: InitVar[object]

    def __post_init__(self, mode, registry, expected_resource_manifest_sha256) -> None:
        _validate_result_v2(
            self,
            mode=mode,
            registry=registry,
            expected_resource_manifest_sha256=expected_resource_manifest_sha256,
        )


def _refuse(code: str) -> None:
    raise ApprovalRefusal(code)


def _approval_phrase_sha256(operation: str, manifest_digest: str) -> str:
    if operation == "bootstrap_migration_apply":
        phrase = (
            "I approve one staging bootstrap migration for manifest SHA256 "
            f"{manifest_digest}. Production remains unchanged."
        )
    else:
        phrase = (
            f"I approve one staging execution of {operation} for manifest SHA256 "
            f"{manifest_digest}. Production remains unchanged."
        )
    return hashlib.sha256(phrase.encode("utf-8")).hexdigest()


def _is_nfc(value: object) -> bool:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value) == value
    if isinstance(value, list | tuple):
        return all(_is_nfc(item) for item in value)
    if isinstance(value, dict):
        return all(_is_nfc(key) and _is_nfc(item) for key, item in value.items())
    return True


def _is_exact_string(value: object, *, nonempty: bool = True) -> bool:
    return isinstance(value, str) and (not nonempty or bool(value)) and _is_nfc(value)


def _is_exact_int(value: object) -> bool:
    return type(value) is int


def _parse_timestamp(value: object, code: str) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        _refuse(code)
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ApprovalRefusal(code) from exc


def _format_timestamp(value: datetime, code: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _refuse(code)
    normalized = value.astimezone(UTC)
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canonical_bytes(payload: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ApprovalRefusal("execution_approval_manifest_invalid") from exc


def _canonical_json_object(value: object, code: str) -> tuple[dict[str, object], bytes]:
    if not _is_exact_string(value):
        _refuse(code)
    try:
        payload = json.loads(value)
        if not isinstance(payload, dict) or not _is_nfc(payload):
            _refuse(code)
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApprovalRefusal(code) from exc
    if canonical.decode("utf-8") != value:
        _refuse(code)
    return payload, canonical


def _canonical_json_object_v2(value: object, code: str) -> tuple[dict[str, object], bytes]:
    if not isinstance(value, str) or not value:
        _refuse(code)
    try:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            _refuse(code)
        stack = [(payload, 1)]
        while stack:
            current, depth = stack.pop()
            if depth > 500:
                _refuse(code)
            items = current.items() if isinstance(current, dict) else enumerate(current)
            for key, item in items:
                if isinstance(current, dict) and unicodedata.normalize("NFC", key) != key:
                    _refuse(code)
                if isinstance(item, str):
                    if unicodedata.normalize("NFC", item) != item:
                        _refuse(code)
                elif isinstance(item, dict | list):
                    stack.append((item, depth + 1))
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except ApprovalRefusal:
        raise
    except (
        json.JSONDecodeError,
        UnicodeError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
        MemoryError,
    ) as exc:
        raise ApprovalRefusal(code) from exc
    if canonical.decode("utf-8") != value:
        _refuse(code)
    return payload, canonical


def _string_array(value: object, *, nonempty: bool) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or any(not _is_exact_string(item) for item in value)
    ):
        _refuse("execution_approval_manifest_invalid")
    return tuple(value)


def _named_pairs(value: object, value_field: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or not value:
        _refuse("execution_approval_manifest_invalid")
    pairs = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", value_field}:
            _refuse("execution_approval_manifest_invalid")
        name = item["name"]
        pair_value = item[value_field]
        if not _is_exact_string(name) or not _is_exact_string(pair_value):
            _refuse("execution_approval_manifest_invalid")
        pairs.append((name, pair_value))
    if pairs != sorted(pairs) or len({name for name, _ in pairs}) != len(pairs):
        _refuse("execution_approval_manifest_invalid")
    return tuple(pairs)


def _validate_limits(operation: str, value: object) -> Mapping[str, int]:
    if not isinstance(value, dict) or set(value) != set(_LIMIT_FIELDS):
        _refuse("execution_approval_manifest_invalid")
    if any(not _is_exact_int(item) or item < 0 for item in value.values()):
        _refuse("execution_approval_manifest_invalid")

    ordered = {field: value[field] for field in _LIMIT_FIELDS}
    max_bytes, max_credits, max_model_calls, max_rows = ordered.values()
    if operation == "source_snapshot_capture":
        valid = max_bytes <= 1_000_000_000 and max_credits == max_model_calls == max_rows == 0
    elif operation == "bootstrap_migration_apply":
        valid = (max_bytes, max_credits, max_model_calls, max_rows) == (0, 0, 0, 4)
    elif operation == "migration_apply":
        valid = max_bytes == max_credits == max_model_calls == 0 and max_rows <= 1
    elif operation == "collection_exposure_issue":
        valid = max_credits == max_model_calls == 0
    elif operation == "r3_apply":
        valid = max_credits == max_model_calls == 0 and max_rows == 7
    elif operation in {"r3_proof_issue", "brain_read"}:
        valid = max_bytes == max_credits == max_model_calls == max_rows == 0
    elif operation == "r3_release":
        valid = max_bytes == max_credits == max_model_calls == 0 and max_rows <= 2
    else:
        valid = max_credits <= 63 and max_model_calls == 0
    if not valid:
        _refuse("execution_approval_manifest_invalid")
    return MappingProxyType(ordered)


def _validate_manifest_envelope(payload: object) -> dict[str, object]:
    if (
        not isinstance(payload, dict)
        or (tuple(payload) != _MANIFEST_FIELDS and set(payload) != set(_MANIFEST_FIELDS))
        or not _is_nfc(payload)
    ):
        _refuse("execution_approval_manifest_invalid")
    return payload


def _validate_manifest_contract(
    payload: Mapping[str, object],
) -> tuple[str, Mapping[str, object]]:
    operation = payload["operation"]
    if not isinstance(operation, str) or operation not in _OPERATION_CONTRACTS:
        _refuse("execution_approval_manifest_invalid")
    contract = _OPERATION_CONTRACTS[operation]

    if payload["manifest_version"] != _MANIFEST_VERSION:
        _refuse("execution_approval_manifest_invalid")
    if (
        not isinstance(payload["contract_sha256"], str)
        or _HEX_64.fullmatch(payload["contract_sha256"]) is None
        or (
            operation == "bootstrap_migration_apply"
            and payload["contract_sha256"] != _BOOTSTRAP_CONTRACT_SHA256
        )
        or (
            operation == "source_snapshot_capture"
            and payload["contract_sha256"] != _SOURCE_SNAPSHOT_CONTRACT_SHA256
        )
    ):
        _refuse("execution_approval_manifest_invalid")
    return operation, contract


def _validate_manifest_target(
    payload: Mapping[str, object],
    contract: Mapping[str, object],
) -> tuple[tuple[str, ...], str]:
    if payload["project"] != _PROJECT or payload["location"] != "US":
        _refuse("execution_approval_target_invalid")

    datasets = _string_array(payload["datasets"], nonempty=True)
    if datasets != tuple(sorted(datasets)):
        _refuse("execution_approval_manifest_invalid")
    if datasets != contract["datasets"]:
        _refuse("execution_approval_target_invalid")
    expected_job = f"projects/{_PROJECT}/locations/us-central1/jobs/{contract['job']}"
    if payload["job_resource"] != expected_job:
        _refuse("execution_approval_target_invalid")
    if payload["service_identity"] != contract["identity"]:
        _refuse("execution_approval_identity_invalid")
    if (
        not isinstance(payload["source_sha"], str)
        or _GIT_SHA.fullmatch(payload["source_sha"]) is None
    ):
        _refuse("execution_approval_manifest_invalid")
    if (
        not isinstance(payload["image_uri"], str)
        or _IMAGE_URI.fullmatch(payload["image_uri"]) is None
    ):
        _refuse("execution_approval_target_invalid")
    if (
        not isinstance(payload["build_resource"], str)
        or _BUILD_RESOURCE.fullmatch(payload["build_resource"]) is None
    ):
        _refuse("execution_approval_target_invalid")
    return datasets, expected_job


def _validate_manifest_invocation(
    payload: Mapping[str, object],
    operation: str,
    contract: Mapping[str, object],
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[tuple[str, str], ...],
    tuple[str, ...],
]:
    command = _string_array(payload["command"], nonempty=True)
    arguments = _string_array(payload["arguments"], nonempty=False)
    arguments_valid = arguments == contract.get("arguments")
    if contract.get("dynamic_arguments") == "source_snapshot_capture":
        arguments_valid = (
            len(arguments) == 5
            and arguments[0] == "scripts/staging/capture_protected_production_snapshot.py"
            and arguments[1] == "--cutoff-date"
            and arguments[2]
            in importlib.import_module(".protected_context_registry", __package__).allowed_cutoffs()
            and arguments[3] == "--mode"
            and arguments[4] in {"initial", "recover"}
        )
    if contract.get("dynamic_arguments") == "brain_read":
        decision_question = arguments[10] if len(arguments) == 11 else None
        arguments_valid = (
            len(arguments) in {9, 11}
            and arguments[0] == "scripts/staging/run_live_intelligence_brain.py"
            and arguments[1:4] == ("--target", "staging", "--run-id")
            and _BRAIN_RUN_ID.fullmatch(arguments[4]) is not None
            and arguments[5] == "--signal-id"
            and _BRAIN_SIGNAL_ID.fullmatch(arguments[6]) is not None
            and arguments[7] == "--research-depth"
            and arguments[8] in {"briefing", "scan", "investigation"}
            and (arguments[8] == "briefing" or decision_question is not None)
            and (
                decision_question is None
                or (
                    arguments[9] == "--decision-question"
                    and len(decision_question) <= 2000
                    and not any(
                        unicodedata.category(character) == "Cc" for character in decision_question
                    )
                )
            )
        )
    if "argument_prefix" in contract:
        execution_match = (
            _EXECUTION_RESOURCE.fullmatch(arguments[2]) if len(arguments) == 3 else None
        )
        arguments_valid = (
            len(arguments) == 3
            and arguments[:2] == contract["argument_prefix"]
            and execution_match is not None
            and execution_match.group(1) == "trends-engine-oi-apply-staging"
        )
    if command != contract["command"] or not arguments_valid:
        _refuse("execution_approval_manifest_invalid")

    environment = _named_pairs(payload["environment"], "value")
    expected_environment = contract.get("environment", _DEFAULT_ENVIRONMENT)
    if environment != expected_environment:
        _refuse("execution_approval_manifest_invalid")
    secrets = _string_array(payload["secrets"], nonempty=False)
    if secrets != tuple(sorted(secrets)) or secrets != contract["secrets"]:
        _refuse("execution_approval_manifest_invalid")
    if payload["max_retries"] != 0 or type(payload["max_retries"]) is not int:
        _refuse("execution_approval_manifest_invalid")
    if (
        not _is_exact_int(payload["timeout_seconds"])
        or not 0 < payload["timeout_seconds"] <= 10800
        or (operation == "bootstrap_migration_apply" and payload["timeout_seconds"] != 900)
        or (operation == "source_snapshot_capture" and payload["timeout_seconds"] > 600)
    ):
        _refuse("execution_approval_manifest_invalid")
    return command, arguments, environment, secrets


def _validate_manifest_inputs(
    payload: Mapping[str, object],
    operation: str,
    contract: Mapping[str, object],
) -> tuple[tuple[tuple[str, str], ...], Mapping[str, int], datetime]:
    input_artifacts = _named_pairs(payload["input_artifacts"], "sha256")
    if tuple(name for name, _ in input_artifacts) != contract["artifacts"] or any(
        _HEX_64.fullmatch(digest) is None for _, digest in input_artifacts
    ):
        _refuse("execution_approval_manifest_invalid")
    limits = _validate_limits(operation, payload["limits"])
    if (
        operation == "source_snapshot_capture"
        and dict(input_artifacts)["capture_contract"] != _SOURCE_SNAPSHOT_CONTRACT_SHA256
    ):
        _refuse("execution_approval_artifact_mismatch")
    expires_at = _parse_timestamp(payload["expires_at"], "execution_approval_manifest_invalid")
    return input_artifacts, limits, expires_at


def _validate_execution_manifest_v1(payload: object) -> ExecutionManifest:
    payload = _validate_manifest_envelope(payload)
    operation, contract = _validate_manifest_contract(payload)
    datasets, expected_job = _validate_manifest_target(payload, contract)
    command, arguments, environment, secrets = _validate_manifest_invocation(
        payload,
        operation,
        contract,
    )
    input_artifacts, limits, expires_at = _validate_manifest_inputs(
        payload,
        operation,
        contract,
    )

    return ExecutionManifest(
        manifest_version=_MANIFEST_VERSION,
        operation=operation,
        contract_sha256=payload["contract_sha256"],
        project=_PROJECT,
        datasets=datasets,
        location="US",
        job_resource=expected_job,
        service_identity=contract["identity"],
        source_sha=payload["source_sha"],
        image_uri=payload["image_uri"],
        build_resource=payload["build_resource"],
        command=command,
        arguments=arguments,
        environment=environment,
        secrets=secrets,
        max_retries=0,
        timeout_seconds=payload["timeout_seconds"],
        input_artifacts=input_artifacts,
        limits=limits,
        expires_at=expires_at,
    )


def _policy_limit_matches(value: int, constraint: Mapping[str, int]) -> bool:
    return (
        ("exact" not in constraint or value == constraint["exact"])
        and ("minimum" not in constraint or value >= constraint["minimum"])
        and ("maximum" not in constraint or value <= constraint["maximum"])
    )


@dataclass(frozen=True, slots=True)
class SourceSnapshotCaptureArguments:
    """The three facts the capture routine reads back from the seven element vector."""

    cutoff_date: date
    mode: str
    grant_id: str


def _capture_vector_fields(
    arguments: tuple[str, ...],
    rule: Mapping[str, object],
) -> SourceSnapshotCaptureArguments | None:
    """Parse a source_snapshot_capture_v2 vector, or None when the routine would refuse it.

    Mirrors the routine's source_snapshot_arguments_invalid assertion: seven elements, the
    fixed prefix, a cutoff that parses as a calendar date, the mode switch and one of the
    policy's modes, then the grant switch and a grant id in the policy's shape.
    """
    prefix = tuple(rule["prefix"])
    if (
        rule.get("kind") != "source_snapshot_capture_v2"
        or len(arguments) != rule["length"]
        or arguments[: len(prefix)] != prefix
        or any(not isinstance(item, str) for item in arguments)
        or re.fullmatch(rule["cutoff_date_regex"], arguments[2]) is None
        or arguments[3] != rule["mode_switch"]
        or arguments[4] not in set(rule["modes"])
        or arguments[5] != rule["grant_switch"]
        or re.fullmatch(rule["grant_id_regex"], arguments[6]) is None
    ):
        return None
    try:
        cutoff = date.fromisoformat(arguments[2])
    except ValueError:
        return None
    return SourceSnapshotCaptureArguments(
        cutoff_date=cutoff, mode=arguments[4], grant_id=arguments[6]
    )


def read_source_snapshot_capture_arguments_v2(
    arguments: object,
    *,
    rule: Mapping[str, object],
) -> SourceSnapshotCaptureArguments:
    """The consume path: read the cutoff, mode and grant id back from an admitted vector."""
    if not isinstance(arguments, tuple | list):
        _refuse("source_snapshot_arguments_invalid")
    fields = _capture_vector_fields(tuple(arguments), rule)
    if fields is None:
        _refuse("source_snapshot_arguments_invalid")
    return fields


def build_source_snapshot_capture_arguments_v2(
    plan: object,
    *,
    grant: object,
    mode: object,
    now: datetime,
    rule: Mapping[str, object],
    source_metadata=None,
    storage_policy=None,
    bridge_artifacts=None,
) -> tuple[str, ...]:
    """The approval path: derive the seven element vector from a v2 plan the routine admits.

    The plan is validated against the v2 plan contract under the grant, which refuses with
    the routine's own codes (source_snapshot_plan_invalid, source_snapshot_cutoff_not_permitted,
    source_snapshot_estate_mismatch), so the cutoff is one of the grant's allowed cutoffs and
    the grant id and source estate digest are the grant's. A mode outside the policy refuses
    with source_snapshot_arguments_invalid, as the routine would.
    """
    from .production_snapshot_tables import validate_capture_plan_v2

    if (
        rule.get("kind") != "source_snapshot_capture_v2"
        or not isinstance(mode, str)
        or mode not in tuple(rule["modes"])
    ):
        _refuse("source_snapshot_arguments_invalid")
    if (
        not isinstance(plan, Mapping)
        or plan.get("contract_version") != rule["plan_contract_version"]
        or not isinstance(plan.get("cutoff_date"), str)
    ):
        _refuse("source_snapshot_plan_invalid")
    try:
        cutoff = date.fromisoformat(plan["cutoff_date"])
    except ValueError as error:
        raise ApprovalRefusal("source_snapshot_plan_invalid") from error
    try:
        if rule["plan_contract_version"] == "open_intelligence_protected_capture_plan_v2":
            validate_capture_plan_v2(plan, cutoff=cutoff, grant=grant, now=now)
        elif rule["plan_contract_version"] == "open_intelligence_protected_capture_plan_v3":
            _prepare_bridge_plan(
                plan,
                grant=grant,
                source_metadata=source_metadata,
                storage_policy=storage_policy,
                bridge_artifacts=bridge_artifacts,
                now=now,
            )
        else:
            _refuse("source_snapshot_plan_invalid")
    except ApprovalRefusal:
        raise
    except (KeyError, TypeError) as error:
        raise ApprovalRefusal("source_snapshot_plan_invalid") from error
    except ValueError as error:
        # The plan contract raises the routine's codes as plain ValueError; the builder
        # speaks one refusal family so a caller catching ApprovalRefusal sees every code.
        raise ApprovalRefusal(str(error)) from error
    vector = (
        *rule["prefix"],
        cutoff.isoformat(),
        rule["mode_switch"],
        mode,
        rule["grant_switch"],
        plan["snapshot_plan"]["grant_id"],
    )
    if _capture_vector_fields(vector, rule) is None:
        _refuse("source_snapshot_arguments_invalid")
    return vector


_BRIDGE_ARTIFACT_PINS = {
    "bridge_policy": "bridge_policy_digest",
    "temporal_rules": "temporal_rules_digest",
    "collection_receipt_set": "collection_receipt_set_digest",
    "history_completion_set": "history_completion_set_digest",
}


def _prepare_bridge_plan(plan, *, grant, source_metadata, storage_policy, bridge_artifacts, now):
    from .brain_contract import canonical_digest
    from .source_estate_bridge import validate_bridge_profile
    from .source_estate_bridge_evidence import parse_bridge_artifacts
    from .source_estate_bridge_plan import validate_bridge_plan

    if not isinstance(storage_policy, dict) or storage_policy.get("grant") != grant:
        _refuse("source_snapshot_storage_policy_invalid")
    parsed = parse_bridge_artifacts(bridge_artifacts)
    profile = plan["snapshot_plan"]
    if any(
        profile[field] != canonical_digest(parsed[name])
        for name, field in _BRIDGE_ARTIFACT_PINS.items()
    ):
        _refuse("execution_approval_artifact_mismatch")
    # The estate is the one the storage policy grant carries, never a value of the plan.
    estate = storage_policy["grant"].get("source_estate_digest")
    validate_bridge_profile(
        profile,
        cutoff_date=plan["cutoff_date"],
        observed_at=now,
        artifacts=parsed,
        source_estate_digest=estate,
        collection_completed_at=[
            _parse_timestamp(item["collection_completed_at"], "source_snapshot_plan_invalid")
            for item in parsed["collection_receipt_set"]["receipts"]
        ],
    )
    history_time = _parse_timestamp(
        profile["history_snapshot_as_of"], "source_snapshot_plan_invalid"
    )
    if any(
        item["state"] != "unavailable"
        and _parse_timestamp(item["available_at"], "source_snapshot_plan_invalid") > history_time
        for item in parsed["history_completion_set"]["entries"]
    ):
        _refuse("source_snapshot_plan_invalid")
    return validate_bridge_plan(
        plan,
        profile=profile,
        source_metadata=source_metadata,
        storage_policy=storage_policy,
        cutoff_date=plan["cutoff_date"],
        client_scope_id=plan["client_scope_id"],
        now=now,
        artifacts=parsed,
    )


def prepare_source_snapshot_capture_v3(expected, *, artifact_reader, rule, now):
    """Regenerate pinned v3 proposal bytes without issuing native evidence authority."""
    from .daily_execution_contracts import canonical_json_object

    if rule.get("plan_contract_version") != "open_intelligence_protected_capture_plan_v3":
        _refuse("source_snapshot_plan_invalid")
    parsed = {}
    for name in ("capture_plan", "source_metadata", "storage_policy", *_BRIDGE_ARTIFACT_PINS):
        try:
            raw = artifact_reader(name)
            if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != expected.get(name):
                _refuse("execution_approval_artifact_mismatch")
            value, _ = canonical_json_object(raw, "execution_approval_artifact_mismatch")
            parsed[name] = value
        except (KeyError, TypeError, UnicodeError, ValueError) as exc:
            raise ApprovalRefusal("execution_approval_artifact_mismatch") from exc
    try:
        return _prepare_bridge_plan(
            parsed["capture_plan"],
            grant=parsed["storage_policy"]["grant"],
            source_metadata=parsed["source_metadata"],
            storage_policy=parsed["storage_policy"],
            bridge_artifacts={name: parsed[name] for name in _BRIDGE_ARTIFACT_PINS},
            now=now,
        )
    except (KeyError, TypeError) as exc:
        raise ApprovalRefusal("source_snapshot_plan_invalid") from exc
    except ValueError as exc:
        raise ApprovalRefusal(str(exc)) from exc


def _validate_policy_arguments(
    arguments: tuple[str, ...],
    rule: Mapping[str, object],
) -> bool:
    kind = rule["kind"]
    if kind == "exact":
        return arguments == tuple(rule["value"])
    if kind == "r3_execution_reference":
        prefix = tuple(rule["prefix"])
        return (
            len(arguments) == len(prefix) + 1
            and arguments[: len(prefix)] == prefix
            and re.fullmatch(rule["execution_resource_regex"], arguments[-1]) is not None
        )
    if kind == "source_snapshot_capture_v2":
        return _capture_vector_fields(arguments, rule) is not None
    if kind == "prefix_pattern_v1":
        prefix = tuple(rule["prefix"])
        pattern = tuple(rule["pattern"])
        return (
            len(arguments) == len(prefix) + len(pattern)
            and arguments[: len(prefix)] == prefix
            and all(
                value == item["literal"]
                if "literal" in item
                else re.fullmatch(item["regex"], value) is not None
                for value, item in zip(arguments[len(prefix) :], pattern, strict=True)
            )
        )
    if kind != "brain_read_v1" or len(arguments) not in set(rule["lengths"]):
        return False
    prefix = tuple(rule["prefix"])
    decision_question = arguments[10] if len(arguments) == 11 else None
    if (
        arguments[: len(prefix)] != prefix
        or re.fullmatch(rule["run_id_regex"], arguments[4]) is None
        or arguments[5] != rule["signal_switch"]
        or re.fullmatch(rule["signal_id_regex"], arguments[6]) is None
        or arguments[7] != rule["depth_switch"]
        or arguments[8] not in set(rule["depths"])
        or (arguments[8] in set(rule["decision_required_for"]) and decision_question is None)
    ):
        return False
    return decision_question is None or (
        arguments[9] == rule["decision_switch"]
        and len(decision_question) <= rule["decision_max_characters"]
        and (
            not rule["decision_control_characters_forbidden"]
            or not any(unicodedata.category(character) == "Cc" for character in decision_question)
        )
    )


def _validate_execution_manifest_v2(
    payload: object,
    *,
    origin: ExecutionOrigin,
    registry,
) -> ExecutionManifest:
    payload = _validate_manifest_envelope(payload)
    policy = json.loads(_policy_bytes_for_origin(registry=registry, origin=origin))
    common = policy["common_manifest_validation"]
    operation = payload["operation"]
    rule = policy["operation_validation"].get(operation) if isinstance(operation, str) else None
    if rule is None or operation not in origin.operation_bindings:
        _refuse("execution_approval_manifest_invalid")
    if (
        payload["manifest_version"] != origin.manifest_version
        or payload["contract_sha256"] != origin.contract_sha256
    ):
        _refuse("execution_approval_manifest_invalid")
    if payload["project"] != common["project"] or payload["location"] != common["location"]:
        _refuse("execution_approval_target_invalid")

    datasets = _string_array(payload["datasets"], nonempty=True)
    if datasets != tuple(sorted(datasets)) or len(datasets) != len(set(datasets)):
        _refuse("execution_approval_manifest_invalid")
    if datasets != tuple(rule["datasets"]) or payload["job_resource"] != rule["job_resource"]:
        _refuse("execution_approval_target_invalid")
    if payload["service_identity"] != rule["service_identity"]:
        _refuse("execution_approval_identity_invalid")
    if (
        not isinstance(payload["source_sha"], str)
        or re.fullmatch(common["source_sha_regex"], payload["source_sha"]) is None
    ):
        _refuse("execution_approval_manifest_invalid")
    if (
        not isinstance(payload["image_uri"], str)
        or re.fullmatch(origin.image_uri_regex, payload["image_uri"]) is None
        or not isinstance(payload["build_resource"], str)
        or re.fullmatch(common["build_resource_regex"], payload["build_resource"]) is None
    ):
        _refuse("execution_approval_target_invalid")

    command = _string_array(payload["command"], nonempty=True)
    arguments = _string_array(payload["arguments"], nonempty=False)
    if command != tuple(rule["command"]) or not _validate_policy_arguments(
        arguments,
        rule["arguments"],
    ):
        _refuse("execution_approval_manifest_invalid")
    environment = _named_pairs(payload["environment"], "value")
    expected_environment = tuple((item["name"], item["value"]) for item in rule["environment"])
    secrets = _string_array(payload["secrets"], nonempty=False)
    if (
        environment != expected_environment
        or secrets != tuple(sorted(secrets))
        or secrets != tuple(rule["secrets"])
    ):
        _refuse("execution_approval_manifest_invalid")
    if (
        not _is_exact_int(payload["max_retries"])
        or payload["max_retries"] != common["max_retries"]["exact_integer"]
        or not _is_exact_int(payload["timeout_seconds"])
        or not common["timeout_seconds"]["integer_minimum"]
        <= payload["timeout_seconds"]
        <= common["timeout_seconds"]["integer_maximum"]
    ):
        _refuse("execution_approval_manifest_invalid")

    input_artifacts = _named_pairs(payload["input_artifacts"], "sha256")
    if tuple(name for name, _ in input_artifacts) != tuple(rule["input_artifact_names"]) or any(
        re.fullmatch(rule["input_artifact_digest_regex"], digest) is None
        for _, digest in input_artifacts
    ):
        _refuse("execution_approval_manifest_invalid")
    limits_payload = payload["limits"]
    if (
        not isinstance(limits_payload, dict)
        or set(limits_payload) != set(_LIMIT_FIELDS)
        or any(not _is_exact_int(value) for value in limits_payload.values())
    ):
        _refuse("execution_approval_manifest_invalid")
    limits = {field: limits_payload[field] for field in _LIMIT_FIELDS}
    if any(
        not _policy_limit_matches(limits[field], rule["limits"][field]) for field in _LIMIT_FIELDS
    ):
        _refuse("execution_approval_manifest_invalid")
    if (
        not isinstance(payload["expires_at"], str)
        or re.fullmatch(common["expires_at_regex"], payload["expires_at"]) is None
    ):
        _refuse("execution_approval_manifest_invalid")
    expires_at = _parse_timestamp(payload["expires_at"], "execution_approval_manifest_invalid")

    return ExecutionManifest(
        manifest_version=origin.manifest_version,
        operation=operation,
        contract_sha256=origin.contract_sha256,
        project=common["project"],
        datasets=datasets,
        location=common["location"],
        job_resource=rule["job_resource"],
        service_identity=rule["service_identity"],
        source_sha=payload["source_sha"],
        image_uri=payload["image_uri"],
        build_resource=payload["build_resource"],
        command=command,
        arguments=arguments,
        environment=environment,
        secrets=secrets,
        max_retries=payload["max_retries"],
        timeout_seconds=payload["timeout_seconds"],
        input_artifacts=input_artifacts,
        limits=MappingProxyType(limits),
        expires_at=expires_at,
    )


def validate_execution_manifest(payload: object, *, mode, registry) -> ExecutionManifest:
    selector = payload if isinstance(payload, Mapping) else {}
    origin = select_origin(
        manifest_version=selector.get("manifest_version"),
        contract_sha256=selector.get("contract_sha256"),
        mode=mode,
        registry=registry,
    )
    if origin.manifest_version == _MANIFEST_VERSION:
        resolve_origin(payload, mode, registry)
        return _validate_execution_manifest_v1(payload)
    return _validate_execution_manifest_v2(payload, origin=origin, registry=registry)


def _manifest_to_mapping(manifest: ExecutionManifest) -> dict[str, object]:
    return {
        "manifest_version": manifest.manifest_version,
        "operation": manifest.operation,
        "contract_sha256": manifest.contract_sha256,
        "project": manifest.project,
        "datasets": list(manifest.datasets),
        "location": manifest.location,
        "job_resource": manifest.job_resource,
        "service_identity": manifest.service_identity,
        "source_sha": manifest.source_sha,
        "image_uri": manifest.image_uri,
        "build_resource": manifest.build_resource,
        "command": list(manifest.command),
        "arguments": list(manifest.arguments),
        "environment": [{"name": name, "value": value} for name, value in manifest.environment],
        "secrets": list(manifest.secrets),
        "max_retries": manifest.max_retries,
        "timeout_seconds": manifest.timeout_seconds,
        "input_artifacts": [
            {"name": name, "sha256": digest} for name, digest in manifest.input_artifacts
        ],
        "limits": dict(manifest.limits),
        "expires_at": _format_timestamp(manifest.expires_at, "execution_approval_manifest_invalid"),
    }


def canonical_manifest_bytes(payload: object, *, mode, registry) -> bytes:
    return _canonical_bytes(
        _manifest_to_mapping(validate_execution_manifest(payload, mode=mode, registry=registry))
    )


def manifest_sha256(payload: object, *, mode, registry) -> str:
    return hashlib.sha256(
        canonical_manifest_bytes(payload, mode=mode, registry=registry)
    ).hexdigest()


def _resolved_source_sha(source_provenance: object) -> str:
    if not isinstance(source_provenance, dict):
        _refuse("execution_approval_build_provenance_invalid")
    source_fields = {
        "resolvedRepoSource": "commitSha",
        "resolvedGitSource": "revision",
        "resolvedConnectedRepository": "revision",
    }
    present = [name for name in source_fields if name in source_provenance]
    if len(present) != 1 or set(source_provenance) != set(present):
        _refuse("execution_approval_build_provenance_invalid")
    source = source_provenance[present[0]]
    revision_field = source_fields[present[0]]
    if not isinstance(source, dict) or set(source) != {revision_field}:
        _refuse("execution_approval_build_provenance_invalid")
    revision = source[revision_field]
    if not isinstance(revision, str) or _GIT_SHA.fullmatch(revision) is None:
        _refuse("execution_approval_build_provenance_invalid")
    return revision


def canonical_execution_job(value: object, job_resource: str) -> str:
    """Bind an execution's parent job to one exact job resource.

    Cloud Run v2 returns ``execution.job`` as the short job id on live readback (observed
    2 September 2026) while the job resource itself is fully qualified. Either exact form
    binds; anything else refuses.
    """
    if (
        not isinstance(value, str)
        or not isinstance(job_resource, str)
        or "/jobs/" not in job_resource
    ):
        _refuse("execution_approval_execution_mismatch")
    short = job_resource.rsplit("/", 1)[1]
    if value != job_resource and value != short:
        _refuse("execution_approval_execution_mismatch")
    return job_resource


def _connected_repository_sha(source: object, *, origin) -> str | None:
    code = "execution_approval_build_provenance_invalid"
    if type(origin) is not ExecutionOrigin:
        _refuse(code)
    if source is None:
        return None
    if not isinstance(source, dict):
        _refuse(code)
    if not source:
        return None
    if set(source) != {"connectedRepository"}:
        _refuse(code)
    connected = source["connectedRepository"]
    if not isinstance(connected, dict):
        _refuse(code)
    expected_dir = _CONNECTED_REPOSITORY_DIR.get(origin.image_repository)
    if "dir" in connected:
        if expected_dir is None or connected["dir"] != expected_dir:
            _refuse(code)
        if set(connected) != {"repository", "revision", "dir"}:
            _refuse(code)
    elif set(connected) != {"repository", "revision"}:
        _refuse(code)
    if connected["repository"] != origin.connected_repo:
        _refuse(code)
    revision = connected["revision"]
    if not isinstance(revision, str) or _GIT_SHA.fullmatch(revision) is None:
        _refuse(code)
    return revision


def _canonical_build_resource(name: object) -> str:
    match = _RAW_BUILD_RESOURCE.fullmatch(name) if isinstance(name, str) else None
    if match is None:
        _refuse("execution_approval_build_provenance_invalid")
    _project, location, build_id = match.groups()
    return f"projects/{_PROJECT}/locations/{location}/builds/{build_id}"


def _canonical_image_results(results: object) -> dict[str, list[dict[str, object]]]:
    code = "execution_approval_build_provenance_invalid"
    if not isinstance(results, dict) or not isinstance(results.get("images"), list):
        _refuse(code)
    images = []
    for image in results["images"]:
        if not isinstance(image, dict) or not {"name", "digest"} <= set(image):
            _refuse(code)
        images.append({"name": image["name"], "digest": image["digest"]})
    return {"images": images}


def normalize_cloud_build_response(payload: object, *, origin) -> dict[str, object]:
    code = "execution_approval_build_provenance_invalid"
    if type(origin) is not ExecutionOrigin or not isinstance(payload, dict) or not _is_nfc(payload):
        _refuse(code)
    required = ("name", "id", "projectId", "status", "results", "finishTime")
    if any(field not in payload for field in required):
        _refuse(code)
    legacy_present = "sourceProvenance" in payload
    source_present = "source" in payload
    legacy = payload.get("sourceProvenance") if legacy_present else {}
    if legacy_present and not isinstance(legacy, dict):
        _refuse(code)
    legacy_sha = _resolved_source_sha(legacy) if legacy else None
    connected_sha = (
        _connected_repository_sha(payload["source"], origin=origin) if source_present else None
    )
    if origin.manifest_version.endswith("_v2") and connected_sha is None:
        _refuse(code)
    if origin.manifest_version.endswith("_v1") and legacy_sha is None and connected_sha is None:
        _refuse(code)
    if legacy_sha is not None and connected_sha is not None and legacy_sha != connected_sha:
        _refuse(code)
    normalized = {
        "name": _canonical_build_resource(payload["name"]),
        "id": payload["id"],
        "projectId": payload["projectId"],
        "status": payload["status"],
        "results": _canonical_image_results(payload["results"]),
        "finishTime": payload["finishTime"],
    }
    if connected_sha is not None:
        connected = payload["source"]["connectedRepository"]
        normalized["source"] = {"connectedRepository": dict(connected)}
    if legacy_present:
        normalized["sourceProvenance"] = dict(legacy)
    return normalized


def build_provenance_from_response(
    payload: object,
    operation_contract_sha256: str,
    *,
    manifest_version,
    mode,
    registry,
) -> BuildProvenanceReceipt:
    code = "execution_approval_build_provenance_invalid"
    origin = select_origin(
        manifest_version=manifest_version,
        contract_sha256=operation_contract_sha256,
        mode=mode,
        registry=registry,
    )
    payload = normalize_cloud_build_response(payload, origin=origin)
    if (
        not isinstance(operation_contract_sha256, str)
        or _HEX_64.fullmatch(operation_contract_sha256) is None
    ):
        _refuse(code)
    match = _BUILD_RESOURCE.fullmatch(payload["name"]) if isinstance(payload["name"], str) else None
    if match is None:
        _refuse(code)
    location, build_id = match.groups()
    if (
        payload["id"] != build_id
        or payload["projectId"] != _PROJECT
        or payload["status"] != "SUCCESS"
    ):
        _refuse(code)

    legacy = payload.get("sourceProvenance", {})
    legacy_sha = _resolved_source_sha(legacy) if legacy else None
    connected_sha = (
        _connected_repository_sha(payload["source"], origin=origin) if "source" in payload else None
    )
    source_sha = connected_sha or legacy_sha
    if source_sha is None:
        _refuse(code)
    results = payload["results"]
    if not isinstance(results, dict) or set(results) != {"images"}:
        _refuse(code)
    images = results["images"]
    if not isinstance(images, list) or len(images) != 1:
        _refuse(code)
    image = images[0]
    if not isinstance(image, dict) or set(image) != {"name", "digest"}:
        _refuse(code)
    if (
        not isinstance(image["name"], str)
        or re.fullmatch(origin.image_name, image["name"]) is None
        or not isinstance(image["digest"], str)
    ):
        _refuse(code)
    image_uri = f"{origin.image_repository}@{image['digest']}"
    if re.fullmatch(origin.image_uri_regex, image_uri) is None:
        _refuse(code)
    finished_at = _parse_timestamp(payload["finishTime"], code)

    return BuildProvenanceReceipt(
        build_provenance_contract_version=_BUILD_PROVENANCE_VERSION,
        build_resource=payload["name"],
        project_id=_PROJECT,
        location=location,
        build_id=build_id,
        status="SUCCESS",
        resolved_source_sha=source_sha,
        image_uri=image_uri,
        operation_contract_sha256=operation_contract_sha256,
        finished_at=finished_at,
    )


def canonical_build_provenance_bytes(receipt: BuildProvenanceReceipt, *, origin) -> bytes:
    code = "execution_approval_build_provenance_invalid"
    if type(receipt) is not BuildProvenanceReceipt or type(origin) is not ExecutionOrigin:
        _refuse(code)
    mapping = {
        "build_provenance_contract_version": receipt.build_provenance_contract_version,
        "build_resource": receipt.build_resource,
        "project_id": receipt.project_id,
        "location": receipt.location,
        "build_id": receipt.build_id,
        "status": receipt.status,
        "resolved_source_sha": receipt.resolved_source_sha,
        "image_uri": receipt.image_uri,
        "operation_contract_sha256": receipt.operation_contract_sha256,
        "finished_at": _format_timestamp(receipt.finished_at, code),
    }
    if (
        receipt.build_provenance_contract_version != _BUILD_PROVENANCE_VERSION
        or not all(
            isinstance(value, str)
            for value in (
                receipt.build_provenance_contract_version,
                receipt.build_resource,
                receipt.project_id,
                receipt.location,
                receipt.build_id,
                receipt.status,
                receipt.resolved_source_sha,
                receipt.image_uri,
                receipt.operation_contract_sha256,
            )
        )
        or receipt.project_id != _PROJECT
        or receipt.location != "us-central1"
        or receipt.status != "SUCCESS"
        or _BUILD_RESOURCE.fullmatch(receipt.build_resource) is None
        or not receipt.build_resource.endswith(f"/builds/{receipt.build_id}")
        or _GIT_SHA.fullmatch(receipt.resolved_source_sha) is None
        or re.fullmatch(origin.image_uri_regex, receipt.image_uri) is None
        or receipt.operation_contract_sha256 != origin.contract_sha256
    ):
        _refuse(code)
    return _canonical_bytes(mapping)


def _identifier(prefix: str, fields: Mapping[str, object]) -> str:
    return prefix + hashlib.sha256(_canonical_bytes(fields)).hexdigest()


def approval_id(
    manifest_sha256: str,
    approved_by: str,
    approved_at: datetime,
) -> str:
    if (
        not isinstance(manifest_sha256, str)
        or _HEX_64.fullmatch(manifest_sha256) is None
        or approved_by != _APPROVED_BY
    ):
        _refuse("execution_approval_manifest_invalid")
    return _identifier(
        "exa_",
        {
            "approval_contract_version": _APPROVAL_VERSION,
            "manifest_sha256": manifest_sha256,
            "approved_by": approved_by,
            "approved_at": _format_timestamp(approved_at, "execution_approval_manifest_invalid"),
        },
    )


def consumption_id(
    approval_id: str,
    execution_name: str,
    consumed_at: datetime,
) -> str:
    if (
        not isinstance(approval_id, str)
        or re.fullmatch(r"exa_[0-9a-f]{64}", approval_id) is None
        or not _is_exact_string(execution_name)
    ):
        _refuse("execution_approval_manifest_invalid")
    return _identifier(
        "exc_",
        {
            "approval_id": approval_id,
            "execution_name": execution_name,
            "consumed_at": _format_timestamp(consumed_at, "execution_approval_manifest_invalid"),
        },
    )


def result_id(
    consumption_id: str,
    result_reference: str,
    result_digest: str,
    status: str,
    completed_at: datetime,
) -> str:
    if (
        not isinstance(consumption_id, str)
        or re.fullmatch(r"exc_[0-9a-f]{64}", consumption_id) is None
        or not _is_exact_string(result_reference)
        or not isinstance(result_digest, str)
        or _HEX_64.fullmatch(result_digest) is None
        or not isinstance(status, str)
        or status not in {"succeeded", "failed"}
    ):
        _refuse("execution_approval_manifest_invalid")
    return _identifier(
        "exr_",
        {
            "consumption_id": consumption_id,
            "result_reference": result_reference,
            "result_digest": result_digest,
            "status": status,
            "completed_at": _format_timestamp(completed_at, "execution_approval_manifest_invalid"),
        },
    )


def _validate_v2_generation(
    origin_registry_sha256: object,
    resource_manifest_sha256: object,
    *,
    registry,
    expected_resource_manifest_sha256,
) -> None:
    if (
        type(registry) is not OriginRegistry
        or not isinstance(origin_registry_sha256, str)
        or _HEX_64.fullmatch(origin_registry_sha256) is None
        or origin_registry_sha256 != registry.sha256
        or not isinstance(resource_manifest_sha256, str)
        or _HEX_64.fullmatch(resource_manifest_sha256) is None
        or not isinstance(expected_resource_manifest_sha256, str)
        or _HEX_64.fullmatch(expected_resource_manifest_sha256) is None
        or resource_manifest_sha256 != expected_resource_manifest_sha256
    ):
        _refuse("execution_approval_manifest_invalid")


def _v2_origin_for_operation(operation: object, *, mode, registry) -> ExecutionOrigin:
    if type(registry) is not OriginRegistry or not _is_exact_string(operation):
        _refuse("execution_approval_manifest_invalid")
    matches = [
        origin
        for origin in registry.values()
        if origin.manifest_version == "open_intelligence_execution_manifest_v2"
        and operation in origin.operation_bindings
    ]
    if len(matches) != 1:
        _refuse("execution_approval_manifest_invalid")
    origin = matches[0]
    return select_origin(
        manifest_version=origin.manifest_version,
        contract_sha256=origin.contract_sha256,
        mode=mode,
        registry=registry,
    )


def _approval_phrase_sha256_v2(
    operation: str,
    manifest_sha256: str,
    origin_registry_sha256: str,
    resource_manifest_sha256: str,
) -> str:
    phrase = (
        "I approve one 42 staging execution of "
        f"{operation} for manifest SHA256 {manifest_sha256}, "
        f"origin registry SHA256 {origin_registry_sha256} and "
        f"resource manifest SHA256 {resource_manifest_sha256}. "
        "Production remains unchanged."
    )
    return hashlib.sha256(phrase.encode("utf-8")).hexdigest()


def approval_id_v2(
    manifest_sha256,
    approved_by,
    approved_at,
    *,
    origin_registry_sha256,
    resource_manifest_sha256,
):
    if (
        not isinstance(manifest_sha256, str)
        or _HEX_64.fullmatch(manifest_sha256) is None
        or approved_by != _APPROVED_BY
        or not isinstance(origin_registry_sha256, str)
        or _HEX_64.fullmatch(origin_registry_sha256) is None
        or not isinstance(resource_manifest_sha256, str)
        or _HEX_64.fullmatch(resource_manifest_sha256) is None
    ):
        _refuse("execution_approval_manifest_invalid")
    return _identifier(
        "exa_",
        {
            "approval_contract_version": _APPROVAL_VERSION_V2,
            "approved_at": _format_timestamp(approved_at, "execution_approval_manifest_invalid"),
            "approved_by": approved_by,
            "manifest_sha256": manifest_sha256,
            "origin_registry_sha256": origin_registry_sha256,
            "resource_manifest_sha256": resource_manifest_sha256,
        },
    )


def consumption_id_v2(
    approval_id,
    execution_name,
    consumed_at,
    *,
    origin_registry_sha256,
    resource_manifest_sha256,
):
    if (
        not isinstance(approval_id, str)
        or re.fullmatch(r"exa_[0-9a-f]{64}", approval_id) is None
        or not _is_exact_string(execution_name)
        or not isinstance(origin_registry_sha256, str)
        or _HEX_64.fullmatch(origin_registry_sha256) is None
        or not isinstance(resource_manifest_sha256, str)
        or _HEX_64.fullmatch(resource_manifest_sha256) is None
    ):
        _refuse("execution_approval_manifest_invalid")
    return _identifier(
        "exc_",
        {
            "approval_id": approval_id,
            "consumed_at": _format_timestamp(consumed_at, "execution_approval_manifest_invalid"),
            "consumption_contract_version": _CONSUMPTION_VERSION_V2,
            "execution_name": execution_name,
            "origin_registry_sha256": origin_registry_sha256,
            "resource_manifest_sha256": resource_manifest_sha256,
        },
    )


def result_id_v2(
    consumption_id,
    result_reference,
    result_digest,
    status,
    completed_at,
    *,
    origin_registry_sha256,
    resource_manifest_sha256,
):
    if (
        not isinstance(consumption_id, str)
        or re.fullmatch(r"exc_[0-9a-f]{64}", consumption_id) is None
        or not _is_exact_string(result_reference)
        or not isinstance(result_digest, str)
        or _HEX_64.fullmatch(result_digest) is None
        or not isinstance(status, str)
        or status not in {"succeeded", "failed"}
        or not isinstance(origin_registry_sha256, str)
        or _HEX_64.fullmatch(origin_registry_sha256) is None
        or not isinstance(resource_manifest_sha256, str)
        or _HEX_64.fullmatch(resource_manifest_sha256) is None
    ):
        _refuse("execution_approval_manifest_invalid")
    return _identifier(
        "exr_",
        {
            "completed_at": _format_timestamp(completed_at, "execution_approval_manifest_invalid"),
            "consumption_id": consumption_id,
            "origin_registry_sha256": origin_registry_sha256,
            "resource_manifest_sha256": resource_manifest_sha256,
            "result_contract_version": _RESULT_VERSION_V2,
            "result_digest": result_digest,
            "result_reference": result_reference,
            "status": status,
        },
    )


def _validate_approval_v2(
    record: ExecutionApprovalV2,
    *,
    mode,
    registry,
    expected_resource_manifest_sha256,
) -> ExecutionManifest:
    _validate_v2_generation(
        record.origin_registry_sha256,
        record.resource_manifest_sha256,
        registry=registry,
        expected_resource_manifest_sha256=expected_resource_manifest_sha256,
    )
    if not isinstance(record.approved_at, datetime) or not isinstance(record.expires_at, datetime):
        _refuse("execution_approval_expired")
    try:
        horizon = record.expires_at - record.approved_at
    except TypeError as exc:
        raise ApprovalRefusal("execution_approval_expired") from exc
    if (
        record.approved_at.tzinfo is None
        or record.expires_at.tzinfo is None
        or horizon <= timedelta(0)
        or horizon > timedelta(hours=24)
    ):
        _refuse("execution_approval_expired")
    manifest_payload, canonical_bytes = _canonical_json_object_v2(
        record.canonical_manifest_json,
        "execution_approval_manifest_invalid",
    )
    admitted = validate_execution_manifest(manifest_payload, mode=mode, registry=registry)
    if (
        record.approval_contract_version != _APPROVAL_VERSION_V2
        or record.manifest_version != "open_intelligence_execution_manifest_v2"
        or record.operation != admitted.operation
        or record.contract_sha256 != admitted.contract_sha256
        or not isinstance(record.manifest_sha256, str)
        or _HEX_64.fullmatch(record.manifest_sha256) is None
        or hashlib.sha256(canonical_bytes).hexdigest() != record.manifest_sha256
        or record.approved_by != _APPROVED_BY
        or record.expires_at != admitted.expires_at
        or record.approval_phrase_sha256
        != _approval_phrase_sha256_v2(
            record.operation,
            record.manifest_sha256,
            record.origin_registry_sha256,
            record.resource_manifest_sha256,
        )
        or record.approval_id
        != approval_id_v2(
            record.manifest_sha256,
            record.approved_by,
            record.approved_at,
            origin_registry_sha256=record.origin_registry_sha256,
            resource_manifest_sha256=record.resource_manifest_sha256,
        )
    ):
        _refuse("execution_approval_manifest_invalid")
    return admitted


def _validate_recurring_grant_approval(
    record: RecurringGrantApproval,
    *,
    mode,
    registry,
    expected_resource_manifest_sha256,
) -> None:
    """Mirror of the v3 read routine's grant branch; refuses execution_approval_manifest_invalid."""
    from . import recurring_grant_phrases

    _require_mode(mode, _MODES)
    _validate_v2_generation(
        record.origin_registry_sha256,
        record.resource_manifest_sha256,
        registry=registry,
        expected_resource_manifest_sha256=expected_resource_manifest_sha256,
    )
    code = "execution_approval_manifest_invalid"
    if not isinstance(record.approved_at, datetime) or not isinstance(record.expires_at, datetime):
        _refuse(code)
    if record.approved_at.tzinfo is None or record.expires_at.tzinfo is None:
        _refuse(code)
    grant, canonical_bytes = _canonical_json_object_v2(record.canonical_manifest_json, code)
    try:
        digest = recurring_grant_phrases.grant_digest(grant)
        if recurring_grant_phrases.canonical_grant_bytes(grant) != canonical_bytes:
            _refuse(code)
        valid_until = datetime.fromisoformat(grant["valid_until"])
        phrase_sha256 = recurring_grant_phrases.approval_phrase_sha256(record.contract_sha256)
    except (ValueError, TypeError, KeyError) as exc:
        raise ApprovalRefusal(code) from exc
    if (
        record.approval_contract_version != _APPROVAL_VERSION_V2
        or record.manifest_version != _RECURRING_GRANT_CONTRACT
        or record.operation != _RECURRING_GRANT_KIND
        or record.manifest_sha256 != digest
        or record.approved_by != _APPROVED_BY
        or grant["issuing_principal"] != _APPROVED_BY
        or grant["revocation_state"] != "active"
        or grant["resource_manifest_digest"] != record.resource_manifest_sha256
        or valid_until.tzinfo is None
        or record.expires_at != valid_until
        or record.expires_at <= record.approved_at
        or record.approval_phrase_sha256 != phrase_sha256
        or record.approval_id
        != approval_id_v2(
            record.manifest_sha256,
            record.approved_by,
            record.approved_at,
            origin_registry_sha256=record.origin_registry_sha256,
            resource_manifest_sha256=record.resource_manifest_sha256,
        )
        or record.revocation_state not in recurring_grant_phrases.REVOCATION_STATES
        or (record.revocation_state == "active") != (record.revoked_at is None)
        or (
            record.revoked_at is not None
            and (
                not isinstance(record.revoked_at, datetime)
                or record.revoked_at.tzinfo is None
                or record.revoked_at <= record.approved_at
            )
        )
    ):
        _refuse(code)


def _validate_consumption_v2(
    record: ExecutionConsumptionV2,
    *,
    mode,
    registry,
    expected_resource_manifest_sha256,
) -> ExecutionOrigin:
    _validate_v2_generation(
        record.origin_registry_sha256,
        record.resource_manifest_sha256,
        registry=registry,
        expected_resource_manifest_sha256=expected_resource_manifest_sha256,
    )
    origin = _v2_origin_for_operation(record.operation, mode=mode, registry=registry)
    binding = origin.operation_bindings[record.operation]
    if (
        record.consumption_contract_version != _CONSUMPTION_VERSION_V2
        or not isinstance(record.consumption_id, str)
        or re.fullmatch(r"exc_[0-9a-f]{64}", record.consumption_id) is None
        or not isinstance(record.approval_id, str)
        or re.fullmatch(r"exa_[0-9a-f]{64}", record.approval_id) is None
        or not isinstance(record.manifest_sha256, str)
        or _HEX_64.fullmatch(record.manifest_sha256) is None
        or record.job_resource != binding.job_resource
        or not isinstance(record.execution_name, str)
        or re.fullmatch(
            re.escape(binding.job_resource) + r"/executions/[^/]+", record.execution_name
        )
        is None
        or not isinstance(record.source_sha, str)
        or _GIT_SHA.fullmatch(record.source_sha) is None
        or not isinstance(record.image_uri, str)
        or re.fullmatch(origin.image_uri_regex, record.image_uri) is None
        or record.consumption_id
        != consumption_id_v2(
            record.approval_id,
            record.execution_name,
            record.consumed_at,
            origin_registry_sha256=record.origin_registry_sha256,
            resource_manifest_sha256=record.resource_manifest_sha256,
        )
    ):
        _refuse("execution_approval_manifest_invalid")
    return origin


def _validate_result_v2(
    record: ExecutionResultV2,
    *,
    mode,
    registry,
    expected_resource_manifest_sha256,
) -> ExecutionOrigin:
    _validate_v2_generation(
        record.origin_registry_sha256,
        record.resource_manifest_sha256,
        registry=registry,
        expected_resource_manifest_sha256=expected_resource_manifest_sha256,
    )
    origin = _v2_origin_for_operation(record.operation, mode=mode, registry=registry)
    binding = origin.operation_bindings[record.operation]
    _, canonical_bytes = _canonical_json_object_v2(
        record.canonical_result_json,
        "execution_approval_manifest_invalid",
    )
    if (
        record.result_contract_version != _RESULT_VERSION_V2
        or not isinstance(record.result_id, str)
        or re.fullmatch(r"exr_[0-9a-f]{64}", record.result_id) is None
        or not isinstance(record.consumption_id, str)
        or re.fullmatch(r"exc_[0-9a-f]{64}", record.consumption_id) is None
        or not isinstance(record.approval_id, str)
        or re.fullmatch(r"exa_[0-9a-f]{64}", record.approval_id) is None
        or not isinstance(record.manifest_sha256, str)
        or _HEX_64.fullmatch(record.manifest_sha256) is None
        or not isinstance(record.execution_name, str)
        or re.fullmatch(
            re.escape(binding.job_resource) + r"/executions/[^/]+", record.execution_name
        )
        is None
        or not _is_exact_string(record.result_reference)
        or not isinstance(record.result_digest, str)
        or _HEX_64.fullmatch(record.result_digest) is None
        or hashlib.sha256(canonical_bytes).hexdigest() != record.result_digest
        or not isinstance(record.status, str)
        or record.status not in {"succeeded", "failed"}
        or record.result_id
        != result_id_v2(
            record.consumption_id,
            record.result_reference,
            record.result_digest,
            record.status,
            record.completed_at,
            origin_registry_sha256=record.origin_registry_sha256,
            resource_manifest_sha256=record.resource_manifest_sha256,
        )
    ):
        _refuse("execution_approval_manifest_invalid")
    return origin


def canonical_approval_v2_bytes(
    record,
    *,
    mode,
    registry,
    expected_resource_manifest_sha256,
):
    if type(record) is not ExecutionApprovalV2:
        _refuse("execution_approval_manifest_invalid")
    _validate_approval_v2(
        record,
        mode=mode,
        registry=registry,
        expected_resource_manifest_sha256=expected_resource_manifest_sha256,
    )
    return _canonical_bytes(
        {
            "approval_contract_version": record.approval_contract_version,
            "approval_id": record.approval_id,
            "manifest_version": record.manifest_version,
            "operation": record.operation,
            "contract_sha256": record.contract_sha256,
            "manifest_sha256": record.manifest_sha256,
            "canonical_manifest_json": record.canonical_manifest_json,
            "approved_by": record.approved_by,
            "approved_at": _format_timestamp(
                record.approved_at, "execution_approval_manifest_invalid"
            ),
            "expires_at": _format_timestamp(
                record.expires_at, "execution_approval_manifest_invalid"
            ),
            "approval_phrase_sha256": record.approval_phrase_sha256,
            "origin_registry_sha256": record.origin_registry_sha256,
            "resource_manifest_sha256": record.resource_manifest_sha256,
        }
    )


def canonical_consumption_v2_bytes(
    record,
    *,
    mode,
    registry,
    expected_resource_manifest_sha256,
):
    if type(record) is not ExecutionConsumptionV2:
        _refuse("execution_approval_manifest_invalid")
    _validate_consumption_v2(
        record,
        mode=mode,
        registry=registry,
        expected_resource_manifest_sha256=expected_resource_manifest_sha256,
    )
    return _canonical_bytes(
        {
            "consumption_contract_version": record.consumption_contract_version,
            "consumption_id": record.consumption_id,
            "approval_id": record.approval_id,
            "manifest_sha256": record.manifest_sha256,
            "operation": record.operation,
            "execution_name": record.execution_name,
            "job_resource": record.job_resource,
            "source_sha": record.source_sha,
            "image_uri": record.image_uri,
            "consumed_at": _format_timestamp(
                record.consumed_at, "execution_approval_manifest_invalid"
            ),
            "origin_registry_sha256": record.origin_registry_sha256,
            "resource_manifest_sha256": record.resource_manifest_sha256,
        }
    )


def canonical_result_v2_bytes(
    record,
    *,
    mode,
    registry,
    expected_resource_manifest_sha256,
):
    if type(record) is not ExecutionResultV2:
        _refuse("execution_approval_manifest_invalid")
    _validate_result_v2(
        record,
        mode=mode,
        registry=registry,
        expected_resource_manifest_sha256=expected_resource_manifest_sha256,
    )
    return _canonical_bytes(
        {
            "result_contract_version": record.result_contract_version,
            "result_id": record.result_id,
            "consumption_id": record.consumption_id,
            "approval_id": record.approval_id,
            "manifest_sha256": record.manifest_sha256,
            "operation": record.operation,
            "execution_name": record.execution_name,
            "result_reference": record.result_reference,
            "canonical_result_json": record.canonical_result_json,
            "result_digest": record.result_digest,
            "status": record.status,
            "completed_at": _format_timestamp(
                record.completed_at, "execution_approval_manifest_invalid"
            ),
            "origin_registry_sha256": record.origin_registry_sha256,
            "resource_manifest_sha256": record.resource_manifest_sha256,
        }
    )


def validate_execution_chain_v2(
    approval,
    consumption,
    result,
    *,
    mode,
    registry,
    expected_resource_manifest_sha256,
) -> None:
    if (
        type(approval) is not ExecutionApprovalV2
        or type(consumption) is not ExecutionConsumptionV2
        or type(result) is not ExecutionResultV2
    ):
        _refuse("execution_approval_manifest_invalid")
    manifest = _validate_approval_v2(
        approval,
        mode=mode,
        registry=registry,
        expected_resource_manifest_sha256=expected_resource_manifest_sha256,
    )
    _validate_consumption_v2(
        consumption,
        mode=mode,
        registry=registry,
        expected_resource_manifest_sha256=expected_resource_manifest_sha256,
    )
    _validate_result_v2(
        result,
        mode=mode,
        registry=registry,
        expected_resource_manifest_sha256=expected_resource_manifest_sha256,
    )
    if (
        consumption.approval_id != approval.approval_id
        or consumption.manifest_sha256 != approval.manifest_sha256
        or consumption.operation != approval.operation
        or consumption.job_resource != manifest.job_resource
        or consumption.source_sha != manifest.source_sha
        or consumption.image_uri != manifest.image_uri
        or result.consumption_id != consumption.consumption_id
        or result.approval_id != approval.approval_id
        or result.manifest_sha256 != approval.manifest_sha256
        or result.operation != approval.operation
        or result.execution_name != consumption.execution_name
        or consumption.origin_registry_sha256 != approval.origin_registry_sha256
        or result.origin_registry_sha256 != approval.origin_registry_sha256
        or consumption.resource_manifest_sha256 != approval.resource_manifest_sha256
        or result.resource_manifest_sha256 != approval.resource_manifest_sha256
        or not approval.approved_at <= consumption.consumed_at < approval.expires_at
        or result.completed_at < consumption.consumed_at
    ):
        _refuse("execution_approval_manifest_invalid")


def _build_authority_registry():
    secret = object()

    class IssuedExecutionAuthority:
        __slots__ = (
            "__weakref__",
            "_digest",
            "approval",
            "execution_name",
            "generation",
            "image_uri",
            "job_resource",
            "manifest",
            "operation",
            "source_sha",
        )

        def __init__(
            self,
            supplied_secret: object,
            operation: str,
            manifest: ExecutionManifest,
            approval: ExecutionApprovalV2,
            execution_name: str,
            job_resource: str,
            source_sha: str,
            image_uri: str,
            digest: str,
            generation: execution_generations.TrustedGeneration,
        ) -> None:
            if supplied_secret is not secret:
                _refuse("execution_approval_identity_invalid")
            self.operation = operation
            self.manifest = manifest
            self.approval = approval
            self.execution_name = execution_name
            self.job_resource = job_resource
            self.source_sha = source_sha
            self.image_uri = image_uri
            self._digest = digest
            self.generation = generation

    registry: WeakKeyDictionary[IssuedExecutionAuthority, tuple[str, str]] = WeakKeyDictionary()
    consumptions: WeakKeyDictionary[IssuedExecutionAuthority, ExecutionConsumptionV2] = (
        WeakKeyDictionary()
    )
    result_attempts: WeakKeyDictionary[IssuedExecutionAuthority, tuple[tuple[str, object], ...]] = (
        WeakKeyDictionary()
    )

    def issue(
        operation: str,
        manifest: ExecutionManifest,
        approval: ExecutionApprovalV2,
        execution_name: str,
        job_resource: str,
        source_sha: str,
        image_uri: str,
        digest: str,
        generation: execution_generations.TrustedGeneration,
    ) -> object:
        authority = IssuedExecutionAuthority(
            secret,
            operation,
            manifest,
            approval,
            execution_name,
            job_resource,
            source_sha,
            image_uri,
            digest,
            generation,
        )
        registry[authority] = (digest, "issued")
        return authority

    def validate(authority: object, expected_state: str) -> object:
        if type(authority) is not IssuedExecutionAuthority:
            _refuse("execution_approval_identity_invalid")
        registration = registry.get(authority)
        if registration is None:
            _refuse("execution_approval_identity_invalid")
        current_digest = _runtime_digest(
            authority.manifest,
            authority.approval,
            authority.execution_name,
            authority.job_resource,
        )
        if registration[0] != current_digest or authority._digest != current_digest:
            _refuse("execution_approval_identity_invalid")
        if registration[1] != expected_state:
            if expected_state == "issued":
                _refuse("execution_approval_consumed")
            _refuse("execution_approval_identity_invalid")
        return authority

    def transition(authority: object, expected_state: str, new_state: str) -> object:
        issued = validate(authority, expected_state)
        registry[issued] = (issued._digest, new_state)
        return issued

    def bind_consumption(authority: object, consumption: ExecutionConsumptionV2) -> None:
        consumptions[authority] = consumption

    def bound_consumption(authority: object) -> ExecutionConsumptionV2 | None:
        return consumptions.get(authority)

    def begin_result_attempt(
        authority: object,
        request: Mapping[str, object],
    ) -> object:
        if type(authority) is not IssuedExecutionAuthority:
            _refuse("execution_approval_identity_invalid")
        registration = registry.get(authority)
        if registration is None:
            _refuse("execution_approval_identity_invalid")
        current_digest = _runtime_digest(
            authority.manifest,
            authority.approval,
            authority.execution_name,
            authority.job_resource,
        )
        if registration[0] != current_digest or authority._digest != current_digest:
            _refuse("execution_approval_identity_invalid")
        attempted = tuple(request.items())
        if registration[1] == "consumed":
            result_attempts[authority] = attempted
            registry[authority] = (authority._digest, "result_attempted")
        elif registration[1] != "result_attempted":
            _refuse("execution_approval_identity_invalid")
        elif result_attempts.get(authority) != attempted:
            _refuse("execution_result_conflict")
        return authority

    return (
        issue,
        validate,
        transition,
        bind_consumption,
        bound_consumption,
        begin_result_attempt,
    )


(
    _issue_runtime_authority,
    _validate_issued_authority,
    _transition_authority,
    _bind_authority_consumption,
    _authority_consumption,
    _begin_authority_result_attempt,
) = _build_authority_registry()
del _build_authority_registry


def _runtime_digest(
    manifest: ExecutionManifest,
    approval: ExecutionApproval,
    execution_name: str,
    job_resource: str,
) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            {
                "approval_id": approval.approval_id,
                "execution_name": execution_name,
                "job_resource": job_resource,
                "manifest_sha256": approval.manifest_sha256,
                "operation": manifest.operation,
                "source_sha": manifest.source_sha,
                "image_uri": manifest.image_uri,
            }
        )
    ).hexdigest()


def _task_template(resource: object, *, execution: bool) -> Mapping[str, object]:
    if not isinstance(resource, Mapping):
        _refuse("execution_approval_execution_mismatch")
    outer = resource.get("template")
    if not isinstance(outer, Mapping):
        _refuse("execution_approval_execution_mismatch")
    task = outer if execution else outer.get("template")
    if not isinstance(task, Mapping):
        _refuse("execution_approval_execution_mismatch")
    return task


def _validate_runtime_task(
    task: Mapping[str, object],
    manifest: ExecutionManifest,
    *,
    secret_versions: Mapping[str, str],
) -> None:
    containers = task.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        _refuse("execution_approval_execution_mismatch")
    container = containers[0]
    if not isinstance(container, Mapping):
        _refuse("execution_approval_execution_mismatch")
    expected_environment = [{"name": name, "value": value} for name, value in manifest.environment]
    expected_environment.extend(
        {
            "name": name,
            "valueSource": {
                "secretKeyRef": {
                    "secret": name,
                    "version": secret_versions[name],
                }
            },
        }
        for name in manifest.secrets
    )
    if (
        task.get("serviceAccount") != manifest.service_identity
        or task.get("maxRetries") != manifest.max_retries
        or task.get("timeout") != f"{manifest.timeout_seconds}s"
        or container.get("image") != manifest.image_uri
        or container.get("command") != list(manifest.command)
        or container.get("args") != list(manifest.arguments)
        or container.get("env", []) != expected_environment
    ):
        _refuse("execution_approval_execution_mismatch")
    if manifest.operation == "source_snapshot_capture":
        resources = container.get("resources")
        limits = resources.get("limits") if isinstance(resources, Mapping) else None
        memory = limits.get("memory") if isinstance(limits, Mapping) else None
        memory_match = (
            re.fullmatch(r"([1-9][0-9]*)(Mi|Gi)", memory) if isinstance(memory, str) else None
        )
        if (
            not isinstance(limits, Mapping)
            or type(task.get("maxRetries")) is not int
            or limits.get("cpu") not in {"1", "2", "1000m", "2000m"}
            or memory_match is None
            or int(memory_match[1]) * (1024 if memory_match[2] == "Gi" else 1) > 8192
        ):
            _refuse("execution_approval_execution_mismatch")


def _runtime_read_pair(payload: object) -> tuple[Mapping[str, object], Mapping[str, object]]:
    if not isinstance(payload, Mapping) or set(payload) != {"execution", "job"}:
        _refuse("execution_approval_execution_mismatch")
    execution = payload["execution"]
    job = payload["job"]
    if not isinstance(execution, Mapping) or not isinstance(job, Mapping):
        _refuse("execution_approval_execution_mismatch")
    return execution, job


def _revalidate_runtime_approval(value: object) -> ExecutionApproval:
    if not isinstance(value, ExecutionApproval):
        _refuse("execution_approval_unavailable")
    try:
        return ExecutionApproval(
            approval_contract_version=value.approval_contract_version,
            approval_id=value.approval_id,
            manifest_version=value.manifest_version,
            operation=value.operation,
            contract_sha256=value.contract_sha256,
            manifest_sha256=value.manifest_sha256,
            canonical_manifest_json=value.canonical_manifest_json,
            approved_by=value.approved_by,
            approved_at=value.approved_at,
            expires_at=value.expires_at,
            approval_phrase_sha256=value.approval_phrase_sha256,
        )
    except (TypeError, ApprovalRefusal) as exc:
        raise ApprovalRefusal("execution_approval_schema_mismatch") from exc


def _row_fields(row: object, names: tuple[str, ...]) -> dict[str, object]:
    return {name: _runtime_row_value(row, name) for name in names}


def _trusted_generation_for_row(fields: Mapping[str, object], code: str):
    pair = tuple(fields[name] for name in _GENERATION_FIELDS)
    if any(not isinstance(digest, str) or _HEX_64.fullmatch(digest) is None for digest in pair):
        _refuse(code)
    return execution_generations.load_trusted_generation(*pair)


def _v2_context(mode: object, generation) -> dict[str, object]:
    return {
        "mode": mode,
        "registry": generation.registry,
        "expected_resource_manifest_sha256": generation.resource_manifest_sha256,
    }


def _revalidate_runtime_approval_v2(value: object, *, generation) -> ExecutionApprovalV2:
    if type(value) is not ExecutionApprovalV2:
        _refuse("execution_approval_unavailable")
    try:
        return ExecutionApprovalV2(
            **_row_fields(value, _APPROVAL_ROW_FIELDS + _GENERATION_FIELDS),
            **_v2_context("new_consume", generation),
        )
    except (TypeError, ApprovalRefusal) as exc:
        raise ApprovalRefusal("execution_approval_schema_mismatch") from exc


def _approval_from_row_v2(row: object, code: str, *, mode) -> ExecutionApprovalV2:
    fields = _row_fields(row, _APPROVAL_ROW_FIELDS + _GENERATION_FIELDS)
    generation = _trusted_generation_for_row(fields, code)
    try:
        return ExecutionApprovalV2(**fields, **_v2_context(mode, generation))
    except (TypeError, ApprovalRefusal) as exc:
        raise ApprovalRefusal(code) from exc


def _approval_from_row_v3(
    row: object, code: str, *, mode
) -> ExecutionApprovalV2 | RecurringGrantApproval:
    """Rows from the v3 read routine: the grant kind by its operation, else a v2 manifest row."""
    if _runtime_row_value(row, "operation") != _RECURRING_GRANT_KIND:
        return _approval_from_row_v2(row, code, mode=mode)
    fields = _row_fields(
        row, _APPROVAL_ROW_FIELDS + _GENERATION_FIELDS + _RECURRING_GRANT_ROW_FIELDS
    )
    generation = _trusted_generation_for_row(fields, code)
    try:
        return RecurringGrantApproval(**fields, **_v2_context(mode, generation))
    except (TypeError, ApprovalRefusal) as exc:
        raise ApprovalRefusal(code) from exc


def _result_from_value_v2(value: object, code: str, *, mode) -> ExecutionResultV2:
    fields = _row_fields(value, _RESULT_ROW_FIELDS + _GENERATION_FIELDS)
    generation = _trusted_generation_for_row(fields, code)
    try:
        return ExecutionResultV2(**fields, **_v2_context(mode, generation))
    except (TypeError, ApprovalRefusal) as exc:
        raise ApprovalRefusal(code) from exc


def _require_mode(mode: object, allowed: tuple[str, ...]) -> None:
    if not isinstance(mode, str) or mode not in allowed:
        raise OriginRefusal("execution_origin_mode_forbidden")


def _runtime_credentials() -> object:
    import google.auth

    credentials, project = google.auth.default(
        scopes=("https://www.googleapis.com/auth/cloud-platform",)
    )
    if project != _PROJECT:
        _refuse("execution_approval_target_invalid")
    return credentials


def _runtime_session(credentials: object):
    from google.auth.transport.requests import AuthorizedSession

    return AuthorizedSession(credentials)


def _default_execution_reader() -> Mapping[str, object]:
    job = os.getenv("CLOUD_RUN_JOB")
    execution = os.getenv("CLOUD_RUN_EXECUTION")
    if (
        not isinstance(job, str)
        or not job
        or "/" in job
        or not isinstance(execution, str)
        or not execution
        or "/" in execution
    ):
        _refuse("execution_approval_execution_mismatch")
    job_resource = f"projects/{_PROJECT}/locations/us-central1/jobs/{job}"
    execution_resource = f"{job_resource}/executions/{execution}"
    try:
        session = _runtime_session(_runtime_credentials())
        execution_response = session.get(f"https://run.googleapis.com/v2/{execution_resource}")
        execution_response.raise_for_status()
        job_response = session.get(f"https://run.googleapis.com/v2/{job_resource}")
        job_response.raise_for_status()
        return {
            "execution": execution_response.json(),
            "job": job_response.json(),
        }
    except ApprovalRefusal:
        raise
    except Exception as exc:
        raise ApprovalRefusal("execution_approval_unavailable") from exc


def _default_build_reader(build_resource: str) -> object:
    if not isinstance(build_resource, str) or _BUILD_RESOURCE.fullmatch(build_resource) is None:
        _refuse("execution_approval_target_invalid")
    try:
        response = _runtime_session(_runtime_credentials()).get(
            f"https://cloudbuild.googleapis.com/v1/{build_resource}"
        )
        response.raise_for_status()
        return response.json()
    except ApprovalRefusal:
        raise
    except Exception as exc:
        raise ApprovalRefusal("execution_approval_build_provenance_invalid") from exc


def _runtime_row_value(row: object, field: str) -> object:
    if isinstance(row, Mapping):
        return row.get(field)
    try:
        return row[field]
    except (KeyError, TypeError):
        return getattr(row, field, None)


def _source_control_query():
    from threading import Lock

    lock = Lock()
    submissions = 0
    remaining_bytes = 1_250_000_000

    def query(procedure, parameters, *, allow_zero=False):
        nonlocal submissions, remaining_bytes
        with lock:
            if submissions >= 10:
                _refuse("execution_approval_internal_refusal")
            if remaining_bytes <= 0:
                _refuse("execution_approval_query_budget_exceeded")
            requested_bytes = {
                "sp_consume_open_intelligence_source_snapshot_v1": 300_000_000,
                "sp_record_open_intelligence_execution_result_v1": 150_000_000,
            }.get(procedure, 125_000_000)
            reserved_bytes = min(requested_bytes, remaining_bytes)
            submissions += 1
            remaining_bytes -= reserved_bytes
        return _runtime_query(
            procedure,
            parameters,
            allow_zero=allow_zero,
            _bounded_control=True,
            _source_byte_limit=reserved_bytes,
        )

    return query


def _runtime_query(
    procedure: str,
    parameters: list[object],
    *,
    allow_zero: bool = False,
    _bounded_control: bool = False,
    _source_byte_limit: int = 125_000_000,
) -> object:
    if _bounded_control and (
        type(_source_byte_limit) is not int or not 0 < _source_byte_limit <= 300_000_000
    ):
        _refuse("execution_approval_query_budget_exceeded")
    try:
        from google.cloud import bigquery

        client = bigquery.Client(
            project=_PROJECT,
            credentials=_runtime_credentials(),
            location="US",
        )
        sql = (
            f"CALL `{_PROJECT}.trends_v2_staging_approvals.{procedure}`("
            + ",".join(f"@{item.name}" for item in parameters)
            + ")"
        )
        if _bounded_control:
            original_request = client._http.request

            def bounded_request(*args, **kwargs):
                timeout = kwargs.get("timeout")
                if isinstance(timeout, tuple):
                    timeout = tuple(30 if value is None else min(value, 30) for value in timeout)
                else:
                    timeout = 30 if timeout is None else min(timeout, 30)
                kwargs["timeout"] = timeout
                return original_request(*args, **kwargs)

            client._http.request = bounded_request
            try:
                rows = tuple(
                    client.query(
                        sql,
                        job_config=bigquery.QueryJobConfig(
                            query_parameters=parameters, maximum_bytes_billed=_source_byte_limit
                        ),
                        retry=None,
                        job_retry=None,
                        timeout=30,
                    ).result(retry=None, job_retry=None, timeout=30, max_results=2)
                )
            finally:
                client.close()
        else:
            rows = tuple(
                client.query(
                    sql,
                    job_config=bigquery.QueryJobConfig(query_parameters=parameters),
                    retry=None,
                    job_retry=None,
                ).result(retry=None, job_retry=None)
            )
    except ApprovalRefusal:
        raise
    except Exception as exc:
        message = str(exc)
        if _bounded_control and (
            "Query exceeded limit for bytes billed" in message
            or any(
                isinstance(error, Mapping) and error.get("reason") == "bytesBilledLimitExceeded"
                for error in (getattr(exc, "errors", None) or ())
            )
        ):
            raise ApprovalRefusal("execution_approval_query_budget_exceeded") from exc
        for code in (
            "execution_approval_unavailable",
            "execution_approval_consumed",
            "execution_approval_expired",
            "execution_approval_concurrent_conflict",
            "execution_approval_identity_invalid",
            "execution_approval_execution_mismatch",
            "execution_approval_schema_mismatch",
            "execution_result_conflict",
        ):
            if code in message:
                raise ApprovalRefusal(code) from exc
        raise ApprovalRefusal("execution_approval_internal_refusal") from exc
    if allow_zero:
        if len(rows) > 1:
            _refuse("execution_result_conflict")
        return rows
    if len(rows) != 1:
        _refuse("execution_approval_schema_mismatch")
    return rows[0]


def _default_approval_reader(
    manifest_digest: str,
    *,
    version,
    mode,
    query=None,
) -> ExecutionApproval | ExecutionApprovalV2 | RecurringGrantApproval:
    from google.cloud import bigquery

    _require_mode(mode, _MODES)
    if version == _APPROVAL_VERSION:
        _require_mode(mode, _HISTORICAL_MODES)
        procedure = "sp_read_open_intelligence_execution_approval_v1"
    elif version == _APPROVAL_VERSION_V2:
        procedure = "sp_read_open_intelligence_execution_approval_v2"
    elif version == _APPROVAL_VERSION_V3:
        procedure = "sp_read_open_intelligence_execution_approval_v3"
    else:
        _refuse("execution_approval_schema_mismatch")
    row = (_runtime_query if query is None else query)(
        procedure,
        [bigquery.ScalarQueryParameter("manifest_sha256", "STRING", manifest_digest)],
    )
    if version == _APPROVAL_VERSION_V3:
        approval = _approval_from_row_v3(row, "execution_approval_schema_mismatch", mode=mode)
        if type(approval) is RecurringGrantApproval and approval.revocation_state != "active":
            _refuse("execution_approval_revoked")
        return approval
    if version == _APPROVAL_VERSION_V2:
        return _approval_from_row_v2(row, "execution_approval_schema_mismatch", mode=mode)
    try:
        approval = ExecutionApproval(**_row_fields(row, _APPROVAL_ROW_FIELDS))
        manifest_payload = json.loads(approval.canonical_manifest_json)
    except (TypeError, ValueError, ApprovalRefusal) as exc:
        raise ApprovalRefusal("execution_approval_schema_mismatch") from exc
    validate_execution_manifest(
        manifest_payload,
        mode=mode,
        registry=execution_generations.active_generation().registry,
    )
    return approval


def _default_artifact_reader(operation: str, name: str) -> bytes:
    modules = {
        "source_snapshot_capture": "scripts.staging.capture_protected_production_snapshot",
        "migration_apply": "scripts.migrations.create_open_intelligence_v2",
        "collection_exposure_issue": "scripts.staging.issue_collection_exposure_receipts",
        "r3_apply": "scripts.staging.replay_open_intelligence",
        "r3_proof_issue": "scripts.staging.issue_r3_execution_proof",
        "r3_release": "scripts.staging.release_open_intelligence_run",
        "brain_read": "scripts.staging.run_live_intelligence_brain",
        "wave1_pilot": "scripts.run_rss_now",
    }
    module_name = modules.get(operation)
    if module_name is None:
        _refuse("execution_approval_artifact_mismatch")
    try:
        reader = importlib.import_module(module_name).__dict__.get(
            "_execution_approval_artifact_bytes"
        )
        if not callable(reader):
            _refuse("execution_approval_artifact_mismatch")
        content = reader(name)
    except Exception as exc:
        raise ApprovalRefusal("execution_approval_artifact_mismatch") from exc
    if not isinstance(content, bytes):
        _refuse("execution_approval_artifact_mismatch")
    return content


def _require_fresh_v2_write(version: object, expected_version: str, mode: object) -> None:
    # Fresh v1 writes are frozen; only an explicit v2 new_consume request may mutate.
    if version == expected_version:
        _require_mode(mode, ("new_consume",))
    elif version == expected_version.replace("_v2", "_v1"):
        raise OriginRefusal("execution_origin_mode_forbidden")
    else:
        _refuse("execution_approval_schema_mismatch")


def _default_consumption_writer(
    request: Mapping[str, object],
    *,
    version,
    mode,
    query=None,
) -> Mapping[str, object]:
    from google.cloud import bigquery

    _require_fresh_v2_write(version, _CONSUMPTION_VERSION_V2, mode)
    row = (_runtime_query if query is None else query)(
        "sp_consume_open_intelligence_execution_v2",
        [
            bigquery.ScalarQueryParameter("manifest_sha256", "STRING", request["manifest_sha256"]),
            bigquery.ScalarQueryParameter("execution_name", "STRING", request["execution_name"]),
            bigquery.ScalarQueryParameter("job_resource", "STRING", request["job_resource"]),
            bigquery.ScalarQueryParameter("source_sha", "STRING", request["source_sha"]),
            bigquery.ScalarQueryParameter("image_uri", "STRING", request["image_uri"]),
            bigquery.ScalarQueryParameter(
                "origin_registry_sha256", "STRING", request["origin_registry_sha256"]
            ),
            bigquery.ScalarQueryParameter(
                "resource_manifest_sha256", "STRING", request["resource_manifest_sha256"]
            ),
        ],
    )
    return _row_fields(row, _CONSUMPTION_ROW_FIELDS + _GENERATION_FIELDS)


def _default_result_writer(
    request: Mapping[str, object],
    *,
    version,
    mode,
    query=None,
) -> Mapping[str, object]:
    from google.cloud import bigquery

    _require_fresh_v2_write(version, _RESULT_VERSION_V2, mode)
    row = (_runtime_query if query is None else query)(
        "sp_record_open_intelligence_execution_result_v2",
        [
            bigquery.ScalarQueryParameter("consumption_id", "STRING", request["consumption_id"]),
            bigquery.ScalarQueryParameter(
                "result_reference", "STRING", request["result_reference"]
            ),
            bigquery.ScalarQueryParameter(
                "canonical_result_json", "STRING", request["canonical_result_json"]
            ),
            bigquery.ScalarQueryParameter("result_digest", "STRING", request["result_digest"]),
            bigquery.ScalarQueryParameter("status", "STRING", request["status"]),
            bigquery.ScalarQueryParameter(
                "origin_registry_sha256", "STRING", request["origin_registry_sha256"]
            ),
            bigquery.ScalarQueryParameter(
                "resource_manifest_sha256", "STRING", request["resource_manifest_sha256"]
            ),
        ],
    )
    return _row_fields(row, _RESULT_ROW_FIELDS + _GENERATION_FIELDS)


def _default_result_reader(consumption_id: str, *, version, mode, query=None) -> object:
    from google.cloud import bigquery

    _require_mode(mode, _MODES)
    if version == _RESULT_VERSION:
        _require_mode(mode, _HISTORICAL_MODES)
        procedure = "sp_read_open_intelligence_execution_result_v1"
    elif version == _RESULT_VERSION_V2:
        procedure = "sp_read_open_intelligence_execution_result_v2"
    else:
        _refuse("execution_approval_schema_mismatch")
    return (_runtime_query if query is None else query)(
        procedure,
        [bigquery.ScalarQueryParameter("p_consumption_id", "STRING", consumption_id)],
        allow_zero=True,
    )


def _result_from_value(value: object, code: str) -> ExecutionResult:
    try:
        return ExecutionResult(
            result_contract_version=_runtime_row_value(value, "result_contract_version"),
            result_id=_runtime_row_value(value, "result_id"),
            consumption_id=_runtime_row_value(value, "consumption_id"),
            approval_id=_runtime_row_value(value, "approval_id"),
            manifest_sha256=_runtime_row_value(value, "manifest_sha256"),
            operation=_runtime_row_value(value, "operation"),
            execution_name=_runtime_row_value(value, "execution_name"),
            result_reference=_runtime_row_value(value, "result_reference"),
            canonical_result_json=_runtime_row_value(value, "canonical_result_json"),
            result_digest=_runtime_row_value(value, "result_digest"),
            status=_runtime_row_value(value, "status"),
            completed_at=_runtime_row_value(value, "completed_at"),
        )
    except (TypeError, ApprovalRefusal) as exc:
        raise ApprovalRefusal(code) from exc


def _exact_reconciled_result(
    rows: object,
    request: Mapping[str, object],
) -> ExecutionResult | None:
    if not isinstance(rows, list | tuple) or len(rows) > 1:
        _refuse("execution_result_conflict")
    if not rows:
        return None
    result = _result_from_value(rows[0], "execution_result_conflict")
    if any(getattr(result, name) != expected for name, expected in request.items()):
        _refuse("execution_result_conflict")
    return result


def _exact_reconciled_result_v2(
    rows: object,
    request: Mapping[str, object],
    *,
    generation,
) -> ExecutionResultV2 | None:
    if not isinstance(rows, list | tuple) or len(rows) > 1:
        _refuse("execution_result_conflict")
    if not rows:
        return None
    result = _result_from_row_v2(rows[0], "execution_result_conflict", generation=generation)
    if any(getattr(result, name) != expected for name, expected in request.items()):
        _refuse("execution_result_conflict")
    return result


def _result_from_row_v2(value: object, code: str, *, generation) -> ExecutionResultV2:
    # Runtime rows are admitted under the generation the execution annotations selected,
    # so a row carrying any other pair refuses here rather than being re-read elsewhere.
    try:
        return ExecutionResultV2(
            **_row_fields(value, _RESULT_ROW_FIELDS + _GENERATION_FIELDS),
            **_v2_context("new_consume", generation),
        )
    except (TypeError, ApprovalRefusal) as exc:
        raise ApprovalRefusal(code) from exc


def _v2_secret_versions(manifest: ExecutionManifest, generation) -> Mapping[str, str]:
    versions = {}
    for name in manifest.secrets:
        prefix = (
            f"//secretmanager.googleapis.com/projects/{manifest.project}/secrets/{name}/versions/"
        )
        # Exact project and secret name; only the numeric version is discovered, and a
        # second version or a non-numeric one such as latest makes the binding ambiguous.
        matches = [
            row["name"][len(prefix) :]
            for row in generation.resource_manifest["resources"]
            if row["name"].startswith(prefix) and "read" in row["actions"]
        ]
        if len(matches) != 1 or _SECRET_VERSION.fullmatch(matches[0]) is None:
            _refuse("execution_approval_target_invalid")
        versions[name] = matches[0]
    return MappingProxyType(versions)


def _validate_generation_resources(
    manifest: ExecutionManifest,
    generation,
    secret_versions: Mapping[str, str],
) -> None:
    resources = [(f"//run.googleapis.com/{manifest.job_resource}", "invoke")]
    resources.extend(
        (f"//bigquery.googleapis.com/projects/{manifest.project}/datasets/{dataset}", "read")
        for dataset in manifest.datasets
    )
    resources.extend(
        (
            f"//secretmanager.googleapis.com/projects/{manifest.project}/secrets/{name}"
            f"/versions/{version}",
            "read",
        )
        for name, version in secret_versions.items()
    )
    for resource, action in resources:
        try:
            execution_generations.assert_resource_allowed(resource, action, generation=generation)
        except OriginRefusal as exc:
            raise ApprovalRefusal("execution_approval_target_invalid") from exc
    identity = (
        f"//iam.googleapis.com/projects/{manifest.project}/serviceAccounts/"
        f"{manifest.service_identity}"
    )
    if (
        generation.resource_manifest["identities"].get(f"execution_{manifest.operation}")
        != identity
    ):
        _refuse("execution_approval_identity_invalid")


# The closed pairing of a capture plan contract with the only routine that consumes it.
# The retained v2 routine keeps its deployed bytes; the v3 plan has its own routine.
_SOURCE_SNAPSHOT_ROUTINES = MappingProxyType(
    {
        "open_intelligence_protected_capture_plan_v2": (
            "sp_consume_open_intelligence_source_snapshot_v2"
        ),
        "open_intelligence_protected_capture_plan_v3": (
            "sp_consume_open_intelligence_source_snapshot_v3"
        ),
    }
)
_ROUTINE_DIR = Path(__file__).resolve().parents[3] / "infra" / "bigquery_routines"


def _source_snapshot_routine(rule: Mapping[str, object]) -> str:
    """The consume routine the policy names, admitted only when it is the routine paired
    with the policy's plan contract and its packaged bytes hash to the policy's digest."""
    version, routine = rule.get("plan_contract_version"), rule.get("consume_routine")
    if (
        not isinstance(version, str)
        or not isinstance(routine, str)
        or _SOURCE_SNAPSHOT_ROUTINES.get(version) != routine
    ):
        _refuse("execution_approval_manifest_invalid")
    try:
        packaged = hashlib.sha256((_ROUTINE_DIR / f"{routine}.sql").read_bytes()).hexdigest()
    except OSError as exc:
        raise ApprovalRefusal("execution_approval_manifest_invalid") from exc
    if packaged != rule.get("consume_routine_sha256"):
        _refuse("execution_approval_manifest_invalid")
    return routine


def _dedicated_consume_routine(operation: str, *, origin, registry) -> str | None:
    """The consume routine a policy binds to the operation's vector kind, or None when the
    generic v2 consume routine reads the vector."""
    policy = json.loads(_policy_bytes_for_origin(registry=registry, origin=origin))
    rule = policy["operation_validation"][operation]["arguments"]
    if rule["kind"] != "source_snapshot_capture_v2":
        return None
    return _source_snapshot_routine(rule)


def _source_snapshot_artifact_bytes(manifest, names):
    from google.cloud import storage

    from .production_snapshot_storage import BUCKET_NAME, SourceCaptureObjects

    expected = dict(manifest.input_artifacts)
    client = storage.Client(project=_PROJECT, credentials=_runtime_credentials())
    try:
        objects = SourceCaptureObjects(client.bucket(BUCKET_NAME))
        return {name: objects.read_input(name, expected[name], timeout=30) for name in names}
    finally:
        client.close()


def _load_execution_authority_impl(
    operation: str,
    *,
    mode,
    _issuer: Callable[..., object],
    _source_snapshot: bool = False,
    execution_reader: Callable[[], object] = _default_execution_reader,
    approval_reader: Callable[[str], object] | None = None,
    build_reader: Callable[[str], object] = _default_build_reader,
    artifact_reader: Callable[[str], bytes] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> object:
    _require_mode(mode, ("new_consume",))
    if not _is_exact_string(operation):
        _refuse("execution_approval_manifest_invalid")
    default_capture_artifacts = _source_snapshot and artifact_reader is None
    if artifact_reader is None:

        def read_artifact(name: str) -> bytes:
            return _default_artifact_reader(operation, name)

        artifact_reader = read_artifact
    if approval_reader is None:
        approval_reader = partial(_default_approval_reader, version=_APPROVAL_VERSION_V2, mode=mode)
    execution, job = _runtime_read_pair(execution_reader())
    annotations = execution.get("annotations")
    job_template = job.get("template") if isinstance(job, Mapping) else None
    job_annotations = job_template.get("annotations") if isinstance(job_template, Mapping) else None
    expected_annotation_names = (
        _BRAIN_DURABLE_ANNOTATIONS if operation == "brain_read" else _DURABLE_ANNOTATIONS
    ) | _GENERATION_ANNOTATIONS
    if (
        not isinstance(annotations, Mapping)
        or not isinstance(job_annotations, Mapping)
        or annotations != job_annotations
        or set(annotations) != expected_annotation_names
        or any(
            not isinstance(value, str) or _HEX_64.fullmatch(value) is None
            for name, value in annotations.items()
            if name != "42.ogilvy/source-sha"
        )
        or not isinstance(annotations.get("42.ogilvy/source-sha"), str)
        or _GIT_SHA.fullmatch(annotations["42.ogilvy/source-sha"]) is None
    ):
        _refuse("execution_approval_execution_mismatch")
    pair = (annotations[_ANNOTATION_REGISTRY], annotations[_ANNOTATION_RESOURCE])
    generation = execution_generations.require_active_generation(*pair)
    origin = _v2_origin_for_operation(operation, mode=mode, registry=generation.registry)
    dedicated = _dedicated_consume_routine(operation, origin=origin, registry=generation.registry)
    if _source_snapshot:
        if operation != "source_snapshot_capture" or dedicated is None:
            _refuse("execution_approval_manifest_invalid")
    elif dedicated:
        # A vector kind bound to its own consume routine is never consumed by the generic
        # loader: capture authority is issued only through that routine's caller.
        _refuse("execution_approval_manifest_invalid")
    manifest_digest = annotations["42.ogilvy/execution-approval-sha256"]
    approval = _revalidate_runtime_approval_v2(
        approval_reader(manifest_digest), generation=generation
    )
    if (approval.origin_registry_sha256, approval.resource_manifest_sha256) != pair:
        _refuse("execution_approval_execution_mismatch")
    try:
        manifest_payload = json.loads(approval.canonical_manifest_json)
    except json.JSONDecodeError as exc:
        raise ApprovalRefusal("execution_approval_manifest_invalid") from exc
    manifest = validate_execution_manifest(
        manifest_payload, mode=mode, registry=generation.registry
    )
    execution_name = execution.get("name")
    current_time = now()
    if (
        approval.operation != operation
        or manifest.operation != operation
        or manifest.contract_sha256 != origin.contract_sha256
        or approval.manifest_sha256 != manifest_digest
        or not isinstance(execution_name, str)
        or canonical_execution_job(execution.get("job"), manifest.job_resource)
        != manifest.job_resource
        or job.get("name") != manifest.job_resource
        or _EXECUTION_RESOURCE.fullmatch(execution_name) is None
        or not execution_name.startswith(manifest.job_resource + "/executions/")
        or annotations["42.ogilvy/source-sha"] != manifest.source_sha
        or not isinstance(current_time, datetime)
        or current_time.tzinfo is None
        or current_time >= approval.expires_at
    ):
        _refuse("execution_approval_execution_mismatch")
    execution_task = _task_template(execution, execution=True)
    job_task = _task_template(job, execution=False)
    if execution_task != job_task:
        _refuse("execution_approval_execution_mismatch")
    secret_versions = _v2_secret_versions(manifest, generation)
    _validate_generation_resources(manifest, generation, secret_versions)
    _validate_runtime_task(job_task, manifest, secret_versions=secret_versions)
    build_receipt = build_provenance_from_response(
        build_reader(manifest.build_resource),
        manifest.contract_sha256,
        manifest_version=manifest.manifest_version,
        mode=mode,
        registry=generation.registry,
    )
    artifacts = dict(manifest.input_artifacts)
    if _source_snapshot:
        if (
            build_receipt.build_resource != manifest.build_resource
            or build_receipt.resolved_source_sha != manifest.source_sha
            or build_receipt.image_uri != manifest.image_uri
        ):
            _refuse("execution_approval_artifact_mismatch")
    elif (
        hashlib.sha256(canonical_build_provenance_bytes(build_receipt, origin=origin)).hexdigest()
        != artifacts["build_provenance"]
        # The pinned digest proves which build was approved; the image the task runs and
        # the source it names must be that build's, not only values the manifest states.
        or build_receipt.build_resource != manifest.build_resource
        or build_receipt.resolved_source_sha != manifest.source_sha
        or build_receipt.image_uri != manifest.image_uri
    ):
        _refuse("execution_approval_artifact_mismatch")
    if default_capture_artifacts:
        artifact_reader = _source_snapshot_artifact_bytes(manifest, tuple(artifacts)).__getitem__
    for name, digest in manifest.input_artifacts:
        if name == "build_provenance":
            continue
        try:
            content = artifact_reader(name)
        except Exception as exc:
            raise ApprovalRefusal("execution_approval_artifact_mismatch") from exc
        if not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != digest:
            _refuse("execution_approval_artifact_mismatch")
    digest = _runtime_digest(manifest, approval, execution_name, manifest.job_resource)
    return _issuer(
        operation,
        manifest,
        approval,
        execution_name,
        manifest.job_resource,
        manifest.source_sha,
        manifest.image_uri,
        digest,
        generation,
    )


def _build_runtime_loader(issue: Callable[..., object]):
    def load_execution_authority(
        operation: str,
        *,
        mode,
        execution_reader: Callable[[], object] = _default_execution_reader,
        approval_reader: Callable[[str], object] | None = None,
        build_reader: Callable[[str], object] = _default_build_reader,
        artifact_reader: Callable[[str], bytes] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> object:
        return _load_execution_authority_impl(
            operation,
            mode=mode,
            _issuer=issue,
            execution_reader=execution_reader,
            approval_reader=approval_reader,
            build_reader=build_reader,
            artifact_reader=artifact_reader,
            now=now,
        )

    def load_source_snapshot_authority(*, mode, **kwargs):
        return _load_execution_authority_impl(
            "source_snapshot_capture", mode=mode, _issuer=issue, _source_snapshot=True, **kwargs
        )

    return load_execution_authority, load_source_snapshot_authority


_load_execution_authority, _load_source_snapshot_authority = _build_runtime_loader(
    _issue_runtime_authority
)
del _build_runtime_loader
del _issue_runtime_authority


def _consume_execution_authority_impl(
    authority: object,
    *,
    _transition: Callable[[object, str, str], object],
    _bind: Callable[[object, ExecutionConsumptionV2], None],
    consumption_writer: Callable[[Mapping[str, object]], object] | None = None,
) -> ExecutionConsumptionV2:
    issued = _transition(authority, "issued", "consumption_attempted")
    generation = issued.generation
    if consumption_writer is None:
        consumption_writer = partial(
            _default_consumption_writer,
            version=_CONSUMPTION_VERSION_V2,
            mode="new_consume",
        )
    request = {
        "approval_id": issued.approval.approval_id,
        "manifest_sha256": issued.approval.manifest_sha256,
        "operation": issued.operation,
        "execution_name": issued.execution_name,
        "job_resource": issued.job_resource,
        "source_sha": issued.source_sha,
        "image_uri": issued.image_uri,
        "origin_registry_sha256": generation.origin_registry_sha256,
        "resource_manifest_sha256": generation.resource_manifest_sha256,
    }
    try:
        value = consumption_writer(request)
    except Exception as exc:
        raise ApprovalRefusal("execution_approval_concurrent_conflict") from exc
    if not isinstance(value, Mapping):
        _refuse("execution_approval_schema_mismatch")
    try:
        fields = _row_fields(value, _CONSUMPTION_ROW_FIELDS + _GENERATION_FIELDS)
        for name in ("job_resource", "source_sha", "image_uri"):
            if fields[name] is not None and fields[name] != request[name]:
                _refuse("execution_approval_schema_mismatch")
            fields[name] = request[name]
        consumption = ExecutionConsumptionV2(
            **fields,
            **_v2_context("new_consume", generation),
        )
    except (TypeError, ApprovalRefusal) as exc:
        raise ApprovalRefusal("execution_approval_schema_mismatch") from exc
    if any(getattr(consumption, name) != expected for name, expected in request.items()):
        _refuse("execution_approval_schema_mismatch")
    _transition(issued, "consumption_attempted", "consumed")
    _bind(issued, consumption)
    return consumption


def _record_execution_result_impl(
    authority: object,
    consumption: ExecutionConsumptionV2,
    result_reference: str,
    canonical_result_json: str,
    result_digest: str,
    status: str,
    *,
    _begin_attempt: Callable[[object, Mapping[str, object]], object],
    _transition: Callable[[object, str, str], object],
    _bound: Callable[[object], ExecutionConsumptionV2 | None],
    result_writer: Callable[[Mapping[str, object]], object] | None = None,
    result_reader: Callable[[str], object] | None = None,
) -> ExecutionResultV2:
    if _bound(authority) is not consumption:
        _refuse("execution_approval_identity_invalid")
    if result_writer is None:
        result_writer = partial(
            _default_result_writer, version=_RESULT_VERSION_V2, mode="new_consume"
        )
    if result_reader is None:
        result_reader = partial(
            _default_result_reader, version=_RESULT_VERSION_V2, mode="new_consume"
        )
    try:
        payload = json.loads(canonical_result_json)
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApprovalRefusal("execution_approval_manifest_invalid") from exc
    if (
        not isinstance(payload, dict)
        or canonical != canonical_result_json
        or hashlib.sha256(canonical_result_json.encode()).hexdigest() != result_digest
        or status not in {"succeeded", "failed"}
    ):
        _refuse("execution_approval_manifest_invalid")
    request = {
        "consumption_id": consumption.consumption_id,
        "approval_id": consumption.approval_id,
        "manifest_sha256": consumption.manifest_sha256,
        "operation": consumption.operation,
        "execution_name": consumption.execution_name,
        "result_reference": result_reference,
        "canonical_result_json": canonical_result_json,
        "result_digest": result_digest,
        "status": status,
        "origin_registry_sha256": consumption.origin_registry_sha256,
        "resource_manifest_sha256": consumption.resource_manifest_sha256,
    }
    issued = _begin_attempt(authority, request)
    generation = issued.generation
    attempted_request = MappingProxyType(request)

    def read_written_result() -> ExecutionResultV2:
        try:
            result = _exact_reconciled_result_v2(
                result_reader(consumption.consumption_id),
                attempted_request,
                generation=generation,
            )
        except ApprovalRefusal as exc:
            if str(exc) == "execution_result_conflict":
                raise
            raise ApprovalRefusal("execution_result_unresolved") from exc
        except Exception as exc:
            raise ApprovalRefusal("execution_result_unresolved") from exc
        if result is None:
            _refuse("execution_result_unresolved")
        return result

    try:
        result_writer(attempted_request)
    except Exception:
        try:
            result = _exact_reconciled_result_v2(
                result_reader(consumption.consumption_id),
                attempted_request,
                generation=generation,
            )
        except ApprovalRefusal as exc:
            if str(exc) == "execution_result_conflict":
                raise
            raise ApprovalRefusal("execution_result_unresolved") from exc
        except Exception as exc:
            raise ApprovalRefusal("execution_result_unresolved") from exc
        if result is None:
            try:
                result_writer(attempted_request)
            except Exception:
                try:
                    result = _exact_reconciled_result_v2(
                        result_reader(consumption.consumption_id),
                        attempted_request,
                        generation=generation,
                    )
                except ApprovalRefusal as exc:
                    if str(exc) == "execution_result_conflict":
                        raise
                    raise ApprovalRefusal("execution_result_unresolved") from exc
                except Exception as exc:
                    raise ApprovalRefusal("execution_result_unresolved") from exc
                if result is None:
                    _refuse("execution_result_unresolved")
            else:
                result = read_written_result()
    else:
        result = read_written_result()
    if any(getattr(result, name) != expected for name, expected in attempted_request.items()):
        _refuse("execution_approval_schema_mismatch")
    _transition(issued, "result_attempted", "complete")
    return result


def _build_runtime_actions(
    validate: Callable[[object, str], object],
    transition: Callable[[object, str, str], object],
    bind: Callable[[object, ExecutionConsumptionV2], None],
    bound: Callable[[object], ExecutionConsumptionV2 | None],
    begin_attempt: Callable[[object, Mapping[str, object]], object],
):
    def consume_execution_authority(
        authority: object,
        *,
        consumption_writer: Callable[[Mapping[str, object]], object] | None = None,
    ) -> ExecutionConsumptionV2:
        return _consume_execution_authority_impl(
            authority,
            _transition=transition,
            _bind=bind,
            consumption_writer=consumption_writer,
        )

    def record_execution_result(
        authority: object,
        consumption: ExecutionConsumptionV2,
        result_reference: str,
        canonical_result_json: str,
        result_digest: str,
        status: str,
        *,
        result_writer: Callable[[Mapping[str, object]], object] | None = None,
        result_reader: Callable[[str], object] | None = None,
    ) -> ExecutionResultV2:
        return _record_execution_result_impl(
            authority,
            consumption,
            result_reference,
            canonical_result_json,
            result_digest,
            status,
            _begin_attempt=begin_attempt,
            _transition=transition,
            _bound=bound,
            result_writer=result_writer,
            result_reader=result_reader,
        )

    def consume_source_snapshot_authority(
        authority, *, artifact_reader=None, query=None, bridge_evidence_readers=None
    ):
        from google.cloud import bigquery

        issued = validate(authority, "issued")
        manifest = issued.manifest
        generation = execution_generations.require_active_generation(
            issued.approval.origin_registry_sha256, issued.approval.resource_manifest_sha256
        )
        if (
            manifest.operation != "source_snapshot_capture"
            or any(
                getattr(issued, name) != getattr(manifest, name)
                for name in ("operation", "source_sha", "image_uri", "job_resource")
            )
            or issued.generation != generation
        ):
            _refuse("execution_approval_identity_invalid")
        origin = _v2_origin_for_operation(
            issued.operation, mode="new_consume", registry=issued.generation.registry
        )
        routine = _dedicated_consume_routine(
            issued.operation, origin=origin, registry=issued.generation.registry
        )
        if routine is None:
            _refuse("execution_approval_manifest_invalid")
        policy = json.loads(
            _policy_bytes_for_origin(registry=issued.generation.registry, origin=origin)
        )
        rule = policy["operation_validation"][issued.operation]["arguments"]
        if rule["plan_contract_version"] not in (
            "open_intelligence_protected_capture_plan_v2",
            "open_intelligence_protected_capture_plan_v3",
        ):
            _refuse("source_snapshot_plan_invalid")
        names = ("capture_plan", "recovery_context", "storage_policy")
        bridge = rule["plan_contract_version"] == "open_intelligence_protected_capture_plan_v3"
        if bridge:
            names += ("source_metadata", *_BRIDGE_ARTIFACT_PINS)
        reader = artifact_reader
        if reader is None:
            reader = _source_snapshot_artifact_bytes(issued.manifest, names).__getitem__
        expected = dict(issued.manifest.input_artifacts)
        artifacts = {}
        pinned = {}
        for name in names:
            try:
                content = reader(name)
            except Exception as exc:
                raise ApprovalRefusal("execution_approval_artifact_mismatch") from exc
            if not isinstance(content, bytes) or hashlib.sha256(
                content
            ).hexdigest() != expected.get(name):
                _refuse("execution_approval_artifact_mismatch")
            pinned[name] = content
            if name in ("capture_plan", "recovery_context", "storage_policy"):
                artifacts[name + "_json"] = content.decode("utf-8")

        if bridge:
            plan = prepare_source_snapshot_capture_v3(
                expected, artifact_reader=pinned.__getitem__, rule=rule, now=datetime.now(UTC)
            )
            try:
                from .source_estate_bridge_evidence import resolve_capture_bridge_evidence
            except ImportError as exc:
                raise ApprovalRefusal("bridge_payload_contract_unavailable") from exc
            if bridge_evidence_readers is None:
                _refuse("bridge_evidence_readers_unavailable")
            try:
                resolve_capture_bridge_evidence(
                    {name: pinned[name] for name in _BRIDGE_ARTIFACT_PINS},
                    profile=plan["snapshot_plan"],
                    read_chain=bridge_evidence_readers.read_chain,
                    read_funded_execution=bridge_evidence_readers.read_funded_execution,
                    generation_loader=getattr(bridge_evidence_readers, "generation_loader", None),
                )
            except (AttributeError, ValueError) as exc:
                raise ApprovalRefusal("bridge_evidence_unavailable") from exc

        def write(request):
            if any(
                request[name] != getattr(manifest, name)
                for name in ("operation", "job_resource", "source_sha", "image_uri")
            ):
                _refuse("execution_approval_identity_invalid")
            values = {
                name: request[name]
                for name in (
                    "manifest_sha256",
                    "execution_name",
                    "job_resource",
                    "source_sha",
                    "image_uri",
                )
            }
            values.update(artifacts)
            values.update({name: request[name] for name in _GENERATION_FIELDS})
            row = (_runtime_query if query is None else query)(
                routine,
                [
                    bigquery.ScalarQueryParameter(name, "STRING", value)
                    for name, value in values.items()
                ],
            )
            omitted = ("job_resource", "source_sha", "image_uri")
            # The routine checks these parameters against the ledger before its final projection.
            return {
                **{name: getattr(manifest, name) for name in omitted},
                **_row_fields(
                    row,
                    tuple(
                        name
                        for name in _CONSUMPTION_ROW_FIELDS + _GENERATION_FIELDS
                        if name not in omitted
                    ),
                ),
            }

        return consume_execution_authority(issued, consumption_writer=write)

    def require_consumed_execution(
        authority: object,
        consumption: ExecutionConsumptionV2,
        operation: str,
    ) -> ExecutionConsumptionV2:
        issued = validate(authority, "consumed")
        if issued.operation != operation or bound(issued) is not consumption:
            _refuse("execution_approval_identity_invalid")
        return consumption

    return (
        consume_execution_authority,
        record_execution_result,
        require_consumed_execution,
        consume_source_snapshot_authority,
    )


(
    _consume_execution_authority,
    _record_execution_result,
    _require_consumed_execution,
    _consume_source_snapshot_authority,
) = _build_runtime_actions(
    _validate_issued_authority,
    _transition_authority,
    _bind_authority_consumption,
    _authority_consumption,
    _begin_authority_result_attempt,
)
del _build_runtime_actions
del _validate_issued_authority
del _transition_authority
del _bind_authority_consumption
del _authority_consumption
del _begin_authority_result_attempt


__all__ = [
    "ApprovalRefusal",
    "BuildProvenanceReceipt",
    "ExecutionApproval",
    "ExecutionApprovalV2",
    "ExecutionConsumption",
    "ExecutionConsumptionV2",
    "ExecutionManifest",
    "ExecutionResult",
    "ExecutionResultV2",
    "approval_id",
    "approval_id_v2",
    "build_provenance_from_response",
    "canonical_approval_v2_bytes",
    "canonical_build_provenance_bytes",
    "canonical_consumption_v2_bytes",
    "canonical_execution_job",
    "canonical_manifest_bytes",
    "canonical_result_v2_bytes",
    "consumption_id",
    "consumption_id_v2",
    "manifest_sha256",
    "normalize_cloud_build_response",
    "result_id",
    "result_id_v2",
    "validate_execution_chain_v2",
    "validate_execution_manifest",
]
