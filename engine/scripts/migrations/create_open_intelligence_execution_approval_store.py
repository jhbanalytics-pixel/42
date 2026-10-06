"""Render and approval-gate the isolated execution approval store bootstrap."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import types
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.open_intelligence import execution_approval, execution_origins

SCHEMA_DIR = ROOT / "infra" / "bigquery_schemas"
ROUTINE_DIR = ROOT / "infra" / "bigquery_routines"

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging_approvals"
LOCATION = "US"
APPROVAL_CONTRACT_VERSION = "open_intelligence_execution_approval_v1"
APPROVED_BOOTSTRAP_CONTRACT_SHA256: str | None = (
    "527798d33d83382f77586fe9869f4456371fce5370a21ad4316d2e4821969545"
)
KMS_KEY_VERSION = (
    "projects/ogilvy-trends-v2/locations/global/keyRings/open-intelligence-staging/"
    "cryptoKeys/execution-bootstrap/cryptoKeyVersions/1"
)
BOOTSTRAP_IDENTITY = "trends-engine-oi-bootstrap@ogilvy-trends-v2.iam.gserviceaccount.com"
BOOTSTRAP_JOB = (
    "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
    "trends-engine-oi-approval-bootstrap-staging"
)
BOOTSTRAP_LOCK_OBJECT = (
    "gs://ogilvy-trends-v2-execution-approvals-staging/bootstrap/locks/"
    "open-intelligence-execution-approval-v1.lock"
)
_LAST_ERROR = ""
_SUBMITTED_JOB: object | None = None
_OUTPUT_RESERVATION: object | None = None
_HEX_40 = re.compile(r"[0-9a-f]{40}")
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_MANIFEST_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
_BUILD_RESOURCE = re.compile(r"projects/ogilvy-trends-v2/locations/us-central1/builds/[^/]+")
_IMAGE_URI = re.compile(
    r"us-central1-docker\.pkg\.dev/ogilvy-trends-v2/pipeline/"
    r"trends-engine@sha256:[0-9a-f]{64}"
)

TABLE_NAMES = (
    "open_intelligence_execution_approvals_v1",
    "open_intelligence_execution_consumptions_v1",
    "open_intelligence_execution_results_v1",
    "open_intelligence_execution_approval_lock_v1",
)
ROUTINE_SPECS = (
    (
        "sp_approve_open_intelligence_execution_v1",
        "roles/bigquery.routineDataEditor",
        (
            ("canonical_manifest_json", "STRING"),
            ("manifest_sha256", "STRING"),
            ("approval_phrase", "STRING"),
        ),
    ),
    (
        "sp_read_open_intelligence_execution_approval_v1",
        "roles/bigquery.routineDataViewer",
        (("manifest_sha256", "STRING"),),
    ),
    (
        "sp_read_open_intelligence_execution_result_v1",
        "roles/bigquery.routineDataViewer",
        (("p_consumption_id", "STRING"),),
    ),
    (
        "sp_consume_open_intelligence_execution_v1",
        "roles/bigquery.routineDataEditor",
        (
            ("manifest_sha256", "STRING"),
            ("execution_name", "STRING"),
            ("job_resource", "STRING"),
            ("source_sha", "STRING"),
            ("image_uri", "STRING"),
        ),
    ),
    (
        "sp_record_open_intelligence_execution_result_v1",
        "roles/bigquery.routineDataEditor",
        (
            ("consumption_id", "STRING"),
            ("result_reference", "STRING"),
            ("canonical_result_json", "STRING"),
            ("result_digest", "STRING"),
            ("status", "STRING"),
        ),
    ),
    (
        "sp_read_open_intelligence_execution_result_chain_v1",
        "roles/bigquery.routineDataViewer",
        (
            ("p_source_operation", "STRING"),
            ("p_run_id", "STRING"),
        ),
    ),
    (
        "sp_import_open_intelligence_execution_bootstrap_v1",
        "roles/bigquery.routineDataEditor",
        (
            ("canonical_manifest_json", "STRING"),
            ("manifest_sha256", "STRING"),
            ("manifest_object", "STRING"),
            ("manifest_generation", "INT64"),
            ("manifest_created_at", "TIMESTAMP"),
            ("signature_object", "STRING"),
            ("signature_generation", "INT64"),
            ("signature_sha256", "STRING"),
            ("kms_key_version", "STRING"),
            ("signature_created_at", "TIMESTAMP"),
            ("bootstrap_lock_object", "STRING"),
            ("bootstrap_lock_generation", "INT64"),
            ("bootstrap_lock_created_at", "TIMESTAMP"),
            ("execution_name", "STRING"),
            ("job_resource", "STRING"),
            ("source_sha", "STRING"),
            ("image_uri", "STRING"),
            ("migration_result_reference", "STRING"),
            ("migration_result_digest", "STRING"),
        ),
    ),
    (
        "sp_disable_open_intelligence_execution_approval_v1",
        "roles/bigquery.routineDataEditor",
        (("approval_phrase", "STRING"),),
    ),
    (
        "sp_consume_open_intelligence_source_snapshot_v1",
        "roles/bigquery.routineDataEditor",
        tuple(
            (name, "STRING")
            for name in (
                "manifest_sha256",
                "execution_name",
                "job_resource",
                "source_sha",
                "image_uri",
                "capture_plan_json",
                "recovery_context_json",
                "storage_policy_json",
            )
        ),
    ),
)

OPERATION_TARGETS = {
    "source_snapshot_capture": (
        "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
        "trends-engine-oi-source-snapshot-staging",
    ),
    "bootstrap_migration_apply": (
        BOOTSTRAP_IDENTITY,
        "trends-engine-oi-approval-bootstrap-staging",
    ),
    "migration_apply": (
        "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
        "trends-engine-oi-migration-staging",
    ),
    "collection_exposure_issue": (
        "trends-engine-oi-exposure@ogilvy-trends-v2.iam.gserviceaccount.com",
        "trends-engine-oi-exposure-issuer-staging",
    ),
    "r3_apply": (
        "trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com",
        "trends-engine-oi-apply-staging",
    ),
    "r3_proof_issue": (
        "trends-engine-oi-r3-proof@ogilvy-trends-v2.iam.gserviceaccount.com",
        "trends-engine-oi-r3-proof-staging",
    ),
    "r3_release": (
        "trends-engine-oi-r3-release@ogilvy-trends-v2.iam.gserviceaccount.com",
        "trends-engine-oi-release-staging",
    ),
    "brain_read": (
        "trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com",
        "trends-engine-oi-brain-staging",
    ),
    "wave1_pilot": (
        "trends-engine-oi-wave1@ogilvy-trends-v2.iam.gserviceaccount.com",
        "trends-engine-open-intelligence-staging",
    ),
}


# Versioned (v2) execution store. Everything below is additive: the v1 names,
# specs, seed and plan above stay byte for byte as they were.
V2_APPROVAL_CONTRACT_VERSION = "open_intelligence_execution_approval_v2"
V2_MANIFEST_VERSION = "open_intelligence_execution_manifest_v2"
V2_TABLE_NAMES = (
    "open_intelligence_execution_approvals_v2",
    "open_intelligence_execution_consumptions_v2",
    "open_intelligence_execution_results_v2",
    "open_intelligence_execution_approval_lock_v2",
    "open_intelligence_execution_origin_registries_v1",
    "open_intelligence_execution_resource_manifests_v1",
    "open_intelligence_execution_origin_policies_v1",
    "open_intelligence_execution_active_generation_v1",
    "open_intelligence_execution_derivations_v1",
    "open_intelligence_execution_derivation_tombstones_v1",
)
_V2_PAIR_PARAMETERS = (
    ("origin_registry_sha256", "STRING"),
    ("resource_manifest_sha256", "STRING"),
)
CANONICAL_JSON_UDF = "fn_is_canonical_execution_json_v1"
V2_ROUTINE_SPECS = (
    (
        CANONICAL_JSON_UDF,
        "roles/bigquery.routineDataViewer",
        (("raw", "STRING"),),
    ),
    (
        "sp_approve_open_intelligence_execution_v2",
        "roles/bigquery.routineDataEditor",
        (
            ("canonical_manifest_json", "STRING"),
            ("manifest_sha256", "STRING"),
            ("approval_phrase", "STRING"),
            *_V2_PAIR_PARAMETERS,
        ),
    ),
    (
        "sp_approve_open_intelligence_execution_v3",
        "roles/bigquery.routineDataEditor",
        (
            ("canonical_manifest_json", "STRING"),
            ("manifest_sha256", "STRING"),
            ("approval_phrase", "STRING"),
            *_V2_PAIR_PARAMETERS,
        ),
    ),
    (
        "sp_read_open_intelligence_execution_approval_v2",
        "roles/bigquery.routineDataViewer",
        (("manifest_sha256", "STRING"),),
    ),
    (
        "sp_read_open_intelligence_execution_approval_v3",
        "roles/bigquery.routineDataViewer",
        (("manifest_sha256", "STRING"),),
    ),
    (
        "sp_consume_open_intelligence_execution_v2",
        "roles/bigquery.routineDataEditor",
        (
            ("manifest_sha256", "STRING"),
            ("execution_name", "STRING"),
            ("job_resource", "STRING"),
            ("source_sha", "STRING"),
            ("image_uri", "STRING"),
            *_V2_PAIR_PARAMETERS,
        ),
    ),
    (
        "sp_record_open_intelligence_execution_result_v2",
        "roles/bigquery.routineDataEditor",
        (
            ("consumption_id", "STRING"),
            ("result_reference", "STRING"),
            ("canonical_result_json", "STRING"),
            ("result_digest", "STRING"),
            ("status", "STRING"),
            *_V2_PAIR_PARAMETERS,
        ),
    ),
    (
        "sp_read_open_intelligence_execution_result_v2",
        "roles/bigquery.routineDataViewer",
        (("p_consumption_id", "STRING"),),
    ),
    (
        "sp_read_open_intelligence_execution_result_chain_v2",
        "roles/bigquery.routineDataViewer",
        (
            ("p_source_operation", "STRING"),
            ("p_run_id", "STRING"),
        ),
    ),
    (
        "sp_disable_open_intelligence_execution_approval_v2",
        "roles/bigquery.routineDataEditor",
        (("approval_phrase", "STRING"),),
    ),
    (
        "sp_consume_open_intelligence_source_snapshot_v2",
        "roles/bigquery.routineDataEditor",
        tuple(
            (name, "STRING")
            for name in (
                "manifest_sha256",
                "execution_name",
                "job_resource",
                "source_sha",
                "image_uri",
                "capture_plan_json",
                "recovery_context_json",
                "storage_policy_json",
                "origin_registry_sha256",
                "resource_manifest_sha256",
            )
        ),
    ),
    # Amendment d: the three recurring grant v2 routines (operator invoked) and the
    # seven daily derivation, read and record routines (daily job invoked).
    (
        "sp_approve_open_intelligence_recurring_grant_v2",
        "roles/bigquery.routineDataEditor",
        (
            ("canonical_grant_json", "STRING"),
            ("grant_sha256", "STRING"),
            ("approval_phrase", "STRING"),
            *_V2_PAIR_PARAMETERS,
        ),
    ),
    (
        "sp_read_open_intelligence_recurring_grant_v2",
        "roles/bigquery.routineDataViewer",
        (("grant_sha256", "STRING"),),
    ),
    (
        "sp_disable_open_intelligence_recurring_grant_v2",
        "roles/bigquery.routineDataEditor",
        (
            ("grant_sha256", "STRING"),
            ("disable_phrase", "STRING"),
            ("reason_code", "STRING"),
            *_V2_PAIR_PARAMETERS,
        ),
    ),
    (
        "sp_derive_open_intelligence_daily_execution_v1",
        "roles/bigquery.routineDataEditor",
        (
            ("canonical_manifest_json", "STRING"),
            ("manifest_sha256", "STRING"),
            ("canonical_operation_context_json", "STRING"),
            ("operation_context_sha256", "STRING"),
            ("canonical_operation_artifact_set_json", "STRING"),
            ("authorizing_grant_digest", "STRING"),
        ),
    ),
    (
        "sp_select_open_intelligence_daily_derivation_v1",
        "roles/bigquery.routineDataViewer",
        (
            ("child_job_resource", "STRING"),
            ("child_image_uri", "STRING"),
            ("authorizing_grant_digest", "STRING"),
            ("child_job_policy_digest", "STRING"),
        ),
    ),
    (
        "sp_consume_open_intelligence_daily_derivation_v1",
        "roles/bigquery.routineDataEditor",
        (
            ("derivation_id", "STRING"),
            ("execution_name", "STRING"),
            ("canonical_operation_context_json", "STRING"),
            ("canonical_operation_artifact_set_json", "STRING"),
            ("canonical_execution_observation_json", "STRING"),
            ("execution_observation_sha256", "STRING"),
        ),
    ),
    (
        "sp_cancel_open_intelligence_daily_derivation_v1",
        "roles/bigquery.routineDataEditor",
        (
            ("derivation_id", "STRING"),
            ("reason_code", "STRING"),
            ("canonical_reconciliation_json", "STRING"),
            ("reconciliation_digest", "STRING"),
        ),
    ),
    (
        "sp_read_open_intelligence_daily_derivation_v1",
        "roles/bigquery.routineDataViewer",
        (("derivation_id", "STRING"),),
    ),
    (
        "sp_read_open_intelligence_daily_chain_v1",
        "roles/bigquery.routineDataViewer",
        (
            ("derivation_id", "STRING"),
            ("canonical_execution_observation_json", "STRING"),
            ("execution_observation_sha256", "STRING"),
        ),
    ),
    (
        "sp_record_open_intelligence_daily_result_v1",
        "roles/bigquery.routineDataEditor",
        (
            ("derivation_id", "STRING"),
            ("consumption_id", "STRING"),
            ("canonical_payload_json", "STRING"),
            ("payload_digest", "STRING"),
            ("execution_observation_sha256", "STRING"),
            ("canonical_stage_metering_json", "STRING"),
            ("result_reference", "STRING"),
            ("terminal_state", "STRING"),
            ("effect_state", "STRING"),
            ("spend_state", "STRING"),
        ),
    ),
    # Amendment e: the owner reconciliation of a daily consumption that ended without a
    # result. The owner invokes it under the approver identity; the amendment e delta
    # grants that, so no service account is bound to it here.
    (
        "sp_reconcile_open_intelligence_daily_consumption_v1",
        "roles/bigquery.routineDataEditor",
        (
            ("derivation_id", "STRING"),
            ("consumption_id", "STRING"),
            ("execution_name", "STRING"),
            ("execution_terminal_state", "STRING"),
            ("canonical_reconciliation_json", "STRING"),
            ("reconciliation_digest", "STRING"),
        ),
    ),
    # Amendment e: the v3 source snapshot routine (bridge stack). It is installed beside
    # the retained v2 routine, which the active capture binding still consumes; the
    # amendment e delta grants orchestration on it, so no principal is bound here.
    (
        "sp_consume_open_intelligence_source_snapshot_v3",
        "roles/bigquery.routineDataEditor",
        (
            ("manifest_sha256", "STRING"),
            ("execution_name", "STRING"),
            ("job_resource", "STRING"),
            ("source_sha", "STRING"),
            ("image_uri", "STRING"),
            ("capture_plan_json", "STRING"),
            ("recovery_context_json", "STRING"),
            ("storage_policy_json", "STRING"),
            *_V2_PAIR_PARAMETERS,
        ),
    ),
)
# The grant bound capture consumes through its own routine, as the v1 capture did: the
# capture binding swaps the generic consume routine for the source snapshot routine.
V2_SOURCE_SNAPSHOT_OPERATION = "source_snapshot_capture"
V2_SOURCE_SNAPSHOT_ROUTINE = "sp_consume_open_intelligence_source_snapshot_v2"
V2_RUNTIME_ROUTINES = (
    "sp_read_open_intelligence_execution_approval_v2",
    "sp_read_open_intelligence_execution_approval_v3",
    "sp_consume_open_intelligence_execution_v2",
    "sp_record_open_intelligence_execution_result_v2",
    "sp_read_open_intelligence_execution_result_v2",
    CANONICAL_JSON_UDF,
)
V2_CHAIN_READER_OPERATIONS = ("r3_apply", "r3_proof_issue", "r3_release")
# The daily job derives, selects, consumes, cancels, reads and records through these
# under the identity the registry binds to intelligence-42-daily-staging
# (daily_execution_authority.py); no other principal calls them.
V2_DAILY_JOB = f"projects/{PROJECT}/locations/us-central1/jobs/intelligence-42-daily-staging"
V2_DAILY_ROUTINES = (
    "sp_derive_open_intelligence_daily_execution_v1",
    "sp_select_open_intelligence_daily_derivation_v1",
    "sp_consume_open_intelligence_daily_derivation_v1",
    "sp_cancel_open_intelligence_daily_derivation_v1",
    "sp_read_open_intelligence_daily_derivation_v1",
    "sp_read_open_intelligence_daily_chain_v1",
    "sp_record_open_intelligence_daily_result_v1",
)
V2_OPERATOR_ROUTINES = (
    "sp_approve_open_intelligence_execution_v2",
    "sp_approve_open_intelligence_execution_v3",
    "sp_disable_open_intelligence_execution_approval_v2",
    CANONICAL_JSON_UDF,
    "sp_approve_open_intelligence_recurring_grant_v2",
    "sp_read_open_intelligence_recurring_grant_v2",
    "sp_disable_open_intelligence_recurring_grant_v2",
)
# The generation cutover is not a persistent routine (the reviewed resource manifest
# carries no row for it). It is an operator-only multi statement script that the
# migration runs once, after the v1 disable and the residual evidence checks, with
# its five values supplied as STRING query parameters.
V2_CUTOVER_SCRIPT_PATH = (
    ROUTINE_DIR / "sp_activate_open_intelligence_execution_generation_v2_cutover_script.sql"
)
V2_CUTOVER_SCRIPT = V2_CUTOVER_SCRIPT_PATH.read_text(encoding="utf-8")
# The generation rotation is the second cutover shape: the same operator-only multi
# statement script discipline, moving the single active row from the pair it holds to
# the pair the amended registry produces, with its six values as STRING query parameters.
V2_ROTATION_SCRIPT_PATH = (
    ROUTINE_DIR / "sp_rotate_open_intelligence_execution_generation_v2_script.sql"
)
V2_ROTATION_SCRIPT = V2_ROTATION_SCRIPT_PATH.read_text(encoding="utf-8")
V2_ROTATION_PARAMETERS = (
    "retire_origin_registry_sha256",
    "retire_resource_manifest_sha256",
    "activate_origin_registry_sha256",
    "activate_resource_manifest_sha256",
    "expected_v2_lock_version",
    "activation_phrase",
)
V2_CUTOVER_PARAMETERS = (
    "origin_registry_sha256",
    "resource_manifest_sha256",
    "residual_consumption_ids_json",
    "residual_set_sha256",
    "activation_phrase",
)
# Fixed package-relative registration inputs. No request or CLI argument selects them.
REGISTRY_PATH = ROOT / "configs" / "open_intelligence" / "execution_origins_v1.json"
RESOURCE_MANIFEST_PATH = ROOT / "configs" / "open_intelligence" / "resource_manifest_v1.json"
POLICY_FILES = (
    "successor-brain-read.json",
    "successor-migration-exposure.json",
    "successor-source-snapshot-capture.json",
    "successor-wave1-pilot.json",
)
POLICY_CONTRACT_DIR = ROOT / "configs" / "open_intelligence" / "origin_contracts"
# The active bridge generation, registered beside amendment e so the rotation to it finds
# its registry, its manifest and every policy its registry names. The bridge registry
# names its capture policy at the candidate_contracts path, so it is registered from there.
BRIDGE_REGISTRY_PATH = ROOT / "configs" / "open_intelligence" / "execution_origins_bridge_v3.json"
BRIDGE_RESOURCE_MANIFEST_PATH = (
    ROOT / "configs" / "open_intelligence" / "resource_manifest_bridge_v3.json"
)
BRIDGE_POLICY_FILES = ("source-bridge-capture-v3.json",)
BRIDGE_POLICY_CONTRACT_DIR = ROOT / "configs" / "open_intelligence" / "candidate_contracts"
# Exact reviewed digests. A file that hashes to anything else refuses registration.
REGISTRATION_DIGESTS = {
    "execution_origins_v1.json": (
        "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179"
    ),
    "resource_manifest.json": ("33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09"),
    "successor-brain-read.json": (
        "596920b6dd5d3d431120349af7b108ab3182337e008c449ebee1557ea69f2de4"
    ),
    "successor-migration-exposure.json": (
        "0fd42bbc35e9db6e935f6fb353f7956fb6d8c433999c563285a35d96c96a9a65"
    ),
    "successor-source-snapshot-capture.json": (
        "66e1a11f30c6dd8a7ca9db2110ef0af91d9a3aeda6b48d0b6323797839a06546"
    ),
    "successor-wave1-pilot.json": (
        "edc805068e0297085926004b86fb1d7d30cc2158e07761439cdb6a6ed074c2d3"
    ),
    # The daily contract revision registered three daily operations and bounded
    # exposure, so the policy, the registry linking it and the bridge manifest naming
    # that registry all moved (025c1f4b, 573deeea and ab7802f4 before). apply-v2
    # registers the new bytes. Tightening the date regex to ASCII digits then moved the
    # policy, registry and manifest again (95dcb79c, e6b95e35 and b7553041 before).
    "execution_origins_bridge_v3.json": (
        "f23001ed7c5ae2d108dda69ab412e79d2c6b951ce147e92b3b53559cfc67b9e2"
    ),
    # Amendment g added the route A capture's artifact bucket to the bridge manifest, so
    # its digest moved from 1643e4ce. apply-v2 registers the new bytes, and a row already
    # registered for 1643e4ce stays as it is.
    "resource_manifest_bridge_v3.json": (
        "592ed45ffdc1a0a10371d197bc4cc2057372b12f845b7ea9c0280bfd68b37efe"
    ),
    "source-bridge-capture-v3.json": (
        "b007a53067e2e4e841421c146eb96bedc3f269b413b253db0d21c0ebd16ce226"
    ),
}


class MigrationRefusal(RuntimeError):
    """Deterministic refusal before or during bootstrap planning."""


@dataclass(frozen=True)
class SourcePlan:
    name: str
    path: Path
    sha256: str
    sql: str


@dataclass(frozen=True)
class TablePlan(SourcePlan):
    pass


@dataclass(frozen=True)
class RoutinePlan(SourcePlan):
    authorized_role: str
    parameters: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class IamBinding:
    principal: str
    resource: str
    role: str
    purpose: str


@dataclass(frozen=True)
class RoutineAuthorization:
    routine: str
    dataset: str
    role: str


@dataclass(frozen=True)
class IamPlan:
    proposed_job_user_grants: tuple[IamBinding, ...]
    required_existing_job_user_bindings: tuple[IamBinding, ...]
    routine_authorizations: tuple[RoutineAuthorization, ...]
    principal_routine_bindings: tuple[IamBinding, ...]
    run_viewer_bindings: tuple[IamBinding, ...]
    cloudbuild_viewer_bindings: tuple[IamBinding, ...]
    required_existing_dataset_writer: IamBinding
    temporary_bootstrap_bindings: tuple[IamBinding, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ApprovalStoreMigrationPlan:
    project: str
    dataset: str
    location: str
    default_table_expiration: None
    time_travel_hours: int
    tables: tuple[TablePlan, ...]
    routines: tuple[RoutinePlan, ...]
    lock_seed: dict[str, object]
    iam_plan: IamPlan


@dataclass(frozen=True)
class BootstrapInputs:
    manifest_bytes: bytes
    signature_bytes: bytes
    public_key_pem: bytes
    object_generations: Mapping[str, int]
    build_payload: Mapping[str, object]


@dataclass(frozen=True)
class BootstrapAuthority:
    manifest: Mapping[str, object]
    manifest_bytes: bytes
    manifest_sha256: str
    object_generations: Mapping[str, int]


@dataclass(frozen=True)
class _BootstrapImportContext:
    manifest_object: str
    manifest_created_at: datetime
    signature_object: str
    signature_created_at: datetime
    signature_sha256: str
    lock_generation: int
    lock_created_at: datetime
    execution_name: str
    migration_result_reference: str
    migration_result_digest: str


@dataclass(frozen=True)
class ApprovalStoreMigrationReceipt:
    manifest_sha256: str
    approval_id: str
    consumption_id: str
    result_id: str
    migration_result_reference: str
    migration_result_digest: str
    bootstrap_proof_digest: str

    def to_dict(self) -> dict[str, str]:
        return {
            "contract_version": "open_intelligence_execution_bootstrap_apply_v1",
            "manifest_sha256": self.manifest_sha256,
            "approval_id": self.approval_id,
            "consumption_id": self.consumption_id,
            "result_id": self.result_id,
            "migration_result_reference": self.migration_result_reference,
            "migration_result_digest": self.migration_result_digest,
            "bootstrap_proof_digest": self.bootstrap_proof_digest,
        }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binding(principal: str, resource: str, role: str, purpose: str) -> IamBinding:
    return IamBinding(principal, resource, role, purpose)


def _routine_resource(name: str) -> str:
    return f"projects/{PROJECT}/datasets/{DATASET}/routines/{name}"


def _project_resource() -> str:
    return f"projects/{PROJECT}"


def _service_principal(identity: str) -> str:
    return f"serviceAccount:{identity}"


def _build_iam_plan(routines: tuple[RoutinePlan, ...]) -> IamPlan:
    human = "user:albert.meintjes@ogilvy.co.za"
    migration = _service_principal(OPERATION_TARGETS["migration_apply"][0])
    code_principals = tuple(
        _service_principal(OPERATION_TARGETS[operation][0])
        for operation in OPERATION_TARGETS
        if OPERATION_TARGETS[operation][0] != OPERATION_TARGETS["migration_apply"][0]
    )
    proposed_job_users = tuple(
        _binding(principal, _project_resource(), "roles/bigquery.jobUser", "query submission")
        for principal in (human, *code_principals)
    )
    existing_job_user = (
        _binding(
            migration,
            _project_resource(),
            "roles/bigquery.jobUser",
            "required existing migration query submission",
        ),
    )
    authorizations = tuple(
        RoutineAuthorization(
            _routine_resource(item.name), f"{PROJECT}.{DATASET}", item.authorized_role
        )
        for item in routines
    )
    routine_by_short_name = {item.name: _routine_resource(item.name) for item in routines}
    access = {
        "source_snapshot_capture": ("sp_consume_open_intelligence_source_snapshot_v1",),
        "bootstrap_migration_apply": (
            "sp_import_open_intelligence_execution_bootstrap_v1",
            "sp_read_open_intelligence_execution_result_v1",
        ),
        "migration_apply": (
            "sp_read_open_intelligence_execution_approval_v1",
            "sp_consume_open_intelligence_execution_v1",
            "sp_record_open_intelligence_execution_result_v1",
            "sp_read_open_intelligence_execution_result_v1",
        ),
        "collection_exposure_issue": (
            "sp_read_open_intelligence_execution_approval_v1",
            "sp_consume_open_intelligence_execution_v1",
            "sp_record_open_intelligence_execution_result_v1",
            "sp_read_open_intelligence_execution_result_v1",
        ),
        "r3_apply": (
            "sp_read_open_intelligence_execution_approval_v1",
            "sp_consume_open_intelligence_execution_v1",
            "sp_record_open_intelligence_execution_result_v1",
            "sp_read_open_intelligence_execution_result_chain_v1",
            "sp_read_open_intelligence_execution_result_v1",
        ),
        "r3_proof_issue": (
            "sp_read_open_intelligence_execution_approval_v1",
            "sp_consume_open_intelligence_execution_v1",
            "sp_record_open_intelligence_execution_result_v1",
            "sp_read_open_intelligence_execution_result_chain_v1",
            "sp_read_open_intelligence_execution_result_v1",
        ),
        "r3_release": (
            "sp_read_open_intelligence_execution_approval_v1",
            "sp_consume_open_intelligence_execution_v1",
            "sp_record_open_intelligence_execution_result_v1",
            "sp_read_open_intelligence_execution_result_chain_v1",
            "sp_read_open_intelligence_execution_result_v1",
        ),
        "brain_read": (
            "sp_read_open_intelligence_execution_approval_v1",
            "sp_consume_open_intelligence_execution_v1",
            "sp_record_open_intelligence_execution_result_v1",
            "sp_read_open_intelligence_execution_result_v1",
        ),
        "wave1_pilot": (
            "sp_read_open_intelligence_execution_approval_v1",
            "sp_consume_open_intelligence_execution_v1",
            "sp_record_open_intelligence_execution_result_v1",
            "sp_read_open_intelligence_execution_result_v1",
        ),
    }
    principal_routines = [
        _binding(
            human,
            routine_by_short_name[name],
            "roles/bigquery.dataViewer",
            "routine metadata and invocation",
        )
        for name in (
            "sp_approve_open_intelligence_execution_v1",
            "sp_disable_open_intelligence_execution_approval_v1",
        )
    ]
    for operation, routine_names in access.items():
        principal = _service_principal(OPERATION_TARGETS[operation][0])
        principal_routines.extend(
            _binding(
                principal,
                routine_by_short_name[name],
                "roles/bigquery.dataViewer",
                f"{operation} routine metadata and invocation",
            )
            for name in routine_names
        )
    run_viewers = tuple(
        _binding(
            _service_principal(identity),
            f"projects/{PROJECT}/locations/us-central1/jobs/{job}",
            "roles/run.viewer",
            f"{operation} immutable execution readback",
        )
        for operation, (identity, job) in OPERATION_TARGETS.items()
    )
    cloudbuild_viewers = tuple(
        _binding(
            _service_principal(identity),
            _project_resource(),
            "roles/cloudbuild.builds.viewer",
            f"{operation} build provenance readback",
        )
        for operation, (identity, _job) in OPERATION_TARGETS.items()
        if operation != "source_snapshot_capture"
    )
    bootstrap = _service_principal(BOOTSTRAP_IDENTITY)
    temporary = (
        _binding(
            bootstrap, _project_resource(), "roles/bigquery.user", "temporary dataset creation"
        ),
        _binding(
            bootstrap, KMS_KEY_VERSION, "roles/cloudkms.publicKeyViewer", "KMS public key read"
        ),
        _binding(
            bootstrap,
            "gs://ogilvy-trends-v2-execution-approvals-staging/bootstrap/manifests/"
            "{manifest_sha256}.json",
            "roles/storage.objectViewer",
            "generation-pinned manifest read",
        ),
        _binding(
            bootstrap,
            "gs://ogilvy-trends-v2-execution-approvals-staging/bootstrap/signatures/"
            "{manifest_sha256}.sig",
            "roles/storage.objectViewer",
            "generation-pinned signature read",
        ),
        _binding(
            bootstrap,
            BOOTSTRAP_LOCK_OBJECT,
            "roles/storage.objectCreator",
            "create-only singleton bootstrap lock",
        ),
    )
    return IamPlan(
        proposed_job_user_grants=proposed_job_users,
        required_existing_job_user_bindings=existing_job_user,
        routine_authorizations=authorizations,
        principal_routine_bindings=tuple(principal_routines),
        run_viewer_bindings=run_viewers,
        cloudbuild_viewer_bindings=cloudbuild_viewers,
        required_existing_dataset_writer=_binding(
            migration,
            f"projects/{PROJECT}/datasets/trends_v2_staging",
            "roles/bigquery.dataEditor",
            "required existing migration DDL authority",
        ),
        temporary_bootstrap_bindings=temporary,
    )


def build_plan() -> ApprovalStoreMigrationPlan:
    tables = tuple(
        TablePlan(
            name=name,
            path=SCHEMA_DIR / f"{name}.sql",
            sha256=_sha256(SCHEMA_DIR / f"{name}.sql"),
            sql=(SCHEMA_DIR / f"{name}.sql").read_text(encoding="utf-8"),
        )
        for name in TABLE_NAMES
    )
    routines = tuple(
        RoutinePlan(
            name=name,
            path=ROUTINE_DIR / f"{name}.sql",
            sha256=_sha256(ROUTINE_DIR / f"{name}.sql"),
            sql=(ROUTINE_DIR / f"{name}.sql").read_text(encoding="utf-8"),
            authorized_role=role,
            parameters=parameters,
        )
        for name, role, parameters in ROUTINE_SPECS
    )
    return ApprovalStoreMigrationPlan(
        project=PROJECT,
        dataset=DATASET,
        location=LOCATION,
        default_table_expiration=None,
        time_travel_hours=168,
        tables=tables,
        routines=routines,
        lock_seed={
            "lock_name": APPROVAL_CONTRACT_VERSION,
            "approval_contract_version": APPROVAL_CONTRACT_VERSION,
            "lock_version": 0,
            "state": "ready",
            "last_approval_id": None,
        },
        iam_plan=_build_iam_plan(routines),
    )


def _source_payload(item: SourcePlan) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": item.name,
        "path": item.path.relative_to(ROOT).as_posix(),
        "sha256": item.sha256,
    }
    if isinstance(item, RoutinePlan):
        payload["authorized_role"] = item.authorized_role
        payload["parameters"] = [list(parameter) for parameter in item.parameters]
    return payload


def render_plan(plan: ApprovalStoreMigrationPlan) -> str:
    payload = {
        "dataset": {
            "default_table_expiration": plan.default_table_expiration,
            "dataset": plan.dataset,
            "location": plan.location,
            "project": plan.project,
            "time_travel_hours": plan.time_travel_hours,
        },
        "iam_plan": plan.iam_plan.to_dict(),
        "lock_seed": plan.lock_seed,
        "proof_kind": "code_only_structural_proof",
        "routines": [_source_payload(item) for item in plan.routines],
        "tables": [_source_payload(item) for item in plan.tables],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MigrationRefusal("execution_approval_manifest_invalid")
        result[key] = value
    return result


def _parse_manifest(manifest_bytes: bytes) -> Mapping[str, object]:
    try:
        text = manifest_bytes.decode("utf-8")
        manifest = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                MigrationRefusal("execution_approval_manifest_invalid")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationRefusal("execution_approval_manifest_invalid") from exc
    if not isinstance(manifest, dict):
        raise MigrationRefusal("execution_approval_manifest_invalid")
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if canonical != manifest_bytes:
        raise MigrationRefusal("execution_approval_manifest_invalid")
    return manifest


def _is_nfc(value: object) -> bool:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value) == value
    if isinstance(value, list):
        return all(_is_nfc(item) for item in value)
    if isinstance(value, dict):
        return all(_is_nfc(key) and _is_nfc(item) for key, item in value.items())
    return True


def _validate_bootstrap_manifest(manifest: Mapping[str, object]) -> None:
    fields = {
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
    }
    if set(manifest) != fields or not _is_nfc(manifest):
        raise MigrationRefusal("execution_approval_manifest_invalid")
    expected = {
        "manifest_version": "open_intelligence_execution_manifest_v1",
        "operation": "bootstrap_migration_apply",
        "contract_sha256": APPROVED_BOOTSTRAP_CONTRACT_SHA256,
        "project": PROJECT,
        "datasets": [DATASET],
        "location": LOCATION,
        "job_resource": BOOTSTRAP_JOB,
        "service_identity": BOOTSTRAP_IDENTITY,
        "command": ["python"],
        "arguments": [
            "scripts/migrations/create_open_intelligence_execution_approval_store.py",
            "apply",
        ],
        "environment": [
            {"name": "BIGQUERY_DATASET", "value": DATASET},
            {"name": "GCP_PROJECT", "value": PROJECT},
            {"name": "TRENDS_ENV", "value": "staging"},
        ],
        "secrets": [],
        "max_retries": 0,
        "timeout_seconds": 900,
        "limits": {
            "max_bytes_billed": 0,
            "max_credits": 0,
            "max_model_calls": 0,
            "max_rows_written": 4,
        },
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise MigrationRefusal("execution_approval_manifest_mismatch")
    if (
        type(manifest["max_retries"]) is not int
        or type(manifest["timeout_seconds"]) is not int
        or not isinstance(manifest["limits"], dict)
        or any(type(value) is not int for value in manifest["limits"].values())
    ):
        raise MigrationRefusal("execution_approval_manifest_invalid")
    expires_at = manifest.get("expires_at")
    if not isinstance(expires_at, str) or _MANIFEST_TIMESTAMP.fullmatch(expires_at) is None:
        raise MigrationRefusal("execution_approval_manifest_invalid")
    expected_artifacts = (
        "bootstrap_contract",
        "build_provenance",
        "iam_plan",
        "kms_public_key",
        "migration_dry_run",
        "migration_plan",
    )
    artifacts = manifest.get("input_artifacts")
    if (
        not isinstance(artifacts, list)
        or len(artifacts) != len(expected_artifacts)
        or any(not isinstance(item, dict) for item in artifacts)
        or tuple(item["name"] for item in artifacts) != expected_artifacts
    ):
        raise MigrationRefusal("execution_approval_artifact_mismatch")
    if any(
        set(item) != {"name", "sha256"}
        or not isinstance(item["sha256"], str)
        or _HEX_64.fullmatch(item["sha256"]) is None
        for item in artifacts
    ):
        raise MigrationRefusal("execution_approval_artifact_mismatch")
    source_sha = manifest.get("source_sha")
    image_uri = manifest.get("image_uri")
    build_resource = manifest.get("build_resource")
    if not isinstance(source_sha, str) or _HEX_40.fullmatch(source_sha) is None:
        raise MigrationRefusal("execution_approval_manifest_invalid")
    if not isinstance(image_uri, str) or _IMAGE_URI.fullmatch(image_uri) is None:
        raise MigrationRefusal("execution_approval_manifest_invalid")
    if not isinstance(build_resource, str) or _BUILD_RESOURCE.fullmatch(build_resource) is None:
        raise MigrationRefusal("execution_approval_manifest_invalid")


def _verify_bootstrap_authority(
    manifest_bytes: bytes,
    signature_bytes: bytes,
    public_key_pem: bytes,
    object_generations: Mapping[str, int],
    build_payload: Mapping[str, object],
) -> BootstrapAuthority:
    if APPROVED_BOOTSTRAP_CONTRACT_SHA256 is None:
        raise MigrationRefusal("execution_approval_bootstrap_unapproved")
    manifest = _parse_manifest(manifest_bytes)
    _validate_bootstrap_manifest(manifest)
    if set(object_generations) != {"manifest", "signature"} or any(
        type(value) is not int or value <= 0 for value in object_generations.values()
    ):
        raise MigrationRefusal("execution_approval_bootstrap_unapproved")
    try:
        expires_at = datetime.strptime(
            str(manifest["expires_at"]), "%Y-%m-%dT%H:%M:%S.%fZ"
        ).replace(tzinfo=UTC)
    except ValueError as exc:
        raise MigrationRefusal("execution_approval_manifest_invalid") from exc
    if expires_at <= datetime.now(UTC):
        raise MigrationRefusal("execution_approval_expired")
    if build_payload != {
        "build_resource": manifest["build_resource"],
        "status": "SUCCESS",
        "resolved_source_sha": manifest["source_sha"],
        "image_uri": manifest["image_uri"],
    }:
        raise MigrationRefusal("execution_approval_artifact_mismatch")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils

        public_key = serialization.load_pem_public_key(public_key_pem)
        if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
            public_key.curve, ec.SECP256R1
        ):
            raise MigrationRefusal("execution_approval_bootstrap_unapproved")
        public_key.verify(
            signature_bytes,
            hashlib.sha256(manifest_bytes).digest(),
            ec.ECDSA(utils.Prehashed(hashes.SHA256())),
        )
    except InvalidSignature as exc:
        raise MigrationRefusal("execution_approval_manifest_mismatch") from exc
    except (TypeError, ValueError) as exc:
        raise MigrationRefusal("execution_approval_bootstrap_unapproved") from exc
    return BootstrapAuthority(
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        object_generations=dict(object_generations),
    )


def _load_credentials() -> object:
    import google.auth

    credentials, project = google.auth.default(
        scopes=("https://www.googleapis.com/auth/cloud-platform",)
    )
    if project != PROJECT:
        raise MigrationRefusal("execution_approval_target_invalid")
    return credentials


def _authorized_session(credentials: object):
    from google.auth.transport.requests import AuthorizedSession

    return AuthorizedSession(credentials)


def _bigquery_client(credentials: object):
    from google.cloud import bigquery

    return bigquery.Client(project=PROJECT, credentials=credentials, location=LOCATION)


def _storage_client(credentials: object):
    from google.cloud import storage

    return storage.Client(project=PROJECT, credentials=credentials)


def _response_json(response: object) -> Mapping[str, object]:
    try:
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise MigrationRefusal("execution_approval_bootstrap_unapproved") from exc
    if not isinstance(payload, dict):
        raise MigrationRefusal("execution_approval_bootstrap_unapproved")
    return payload


def _storage_read(
    session: object,
    object_name: str,
    generation: int,
) -> tuple[bytes, Mapping[str, object]]:
    encoded_name = quote(object_name, safe="")
    url = (
        "https://storage.googleapis.com/storage/v1/b/"
        f"ogilvy-trends-v2-execution-approvals-staging/o/{encoded_name}"
    )
    metadata = _response_json(session.get(url, params={"generation": generation}))
    if metadata.get("generation") != str(generation):
        raise MigrationRefusal("execution_approval_bootstrap_unapproved")
    try:
        response = session.get(url, params={"generation": generation, "alt": "media"})
        response.raise_for_status()
        content = bytes(response.content)
    except Exception as exc:
        raise MigrationRefusal("execution_approval_bootstrap_unapproved") from exc
    if not content:
        raise MigrationRefusal("execution_approval_bootstrap_unapproved")
    return content, metadata


def _resolved_build_source(source_provenance: object) -> str:
    source_fields = {
        "resolvedRepoSource": "commitSha",
        "resolvedGitSource": "revision",
        "resolvedConnectedRepository": "revision",
    }
    if not isinstance(source_provenance, dict):
        raise MigrationRefusal("execution_approval_artifact_mismatch")
    present = [name for name in source_fields if name in source_provenance]
    if (
        len(present) != 1
        or not set(source_provenance).issubset({present[0], "fileHashes"})
        or (
            "fileHashes" in source_provenance
            and not isinstance(source_provenance["fileHashes"], dict)
        )
    ):
        raise MigrationRefusal("execution_approval_artifact_mismatch")
    source = source_provenance[present[0]]
    field = source_fields[present[0]]
    if (
        not isinstance(source, dict)
        or field not in source
        or not isinstance(source[field], str)
        or _HEX_40.fullmatch(source[field]) is None
    ):
        raise MigrationRefusal("execution_approval_artifact_mismatch")
    return source[field]


def _bootstrap_runtime_selector() -> tuple[str, str]:
    required_selector = os.getenv("OPEN_INTELLIGENCE_BOOTSTRAP_MANIFEST_SHA256")
    execution_id = os.getenv("CLOUD_RUN_EXECUTION")
    job_id = os.getenv("CLOUD_RUN_JOB")
    if (
        not isinstance(required_selector, str)
        or _HEX_64.fullmatch(required_selector) is None
        or job_id != "trends-engine-oi-approval-bootstrap-staging"
        or not isinstance(execution_id, str)
        or not execution_id
        or "/" in execution_id
    ):
        raise MigrationRefusal("execution_approval_bootstrap_unapproved")
    return required_selector, execution_id


def _bootstrap_execution_annotations(
    session: object,
    execution_name: str,
    required_selector: str,
) -> Mapping[str, object]:
    execution = _response_json(session.get(f"https://run.googleapis.com/v2/{execution_name}"))
    job = _response_json(session.get(f"https://run.googleapis.com/v2/{BOOTSTRAP_JOB}"))
    expected_annotation_names = {
        "42.ogilvy/bootstrap-manifest-sha256",
        "42.ogilvy/bootstrap-manifest-generation",
        "42.ogilvy/bootstrap-signature-generation",
        "42.ogilvy/execution-approval-sha256",
        "42.ogilvy/source-sha",
    }
    execution_annotations = execution.get("annotations")
    job_template = job.get("template")
    job_annotations = job_template.get("annotations") if isinstance(job_template, dict) else None
    try:
        execution_job = execution_approval.canonical_execution_job(
            execution.get("job"), BOOTSTRAP_JOB
        )
    except execution_approval.ApprovalRefusal as exc:
        raise MigrationRefusal("execution_approval_bootstrap_unapproved") from exc
    if (
        execution.get("name") != execution_name
        or execution_job != BOOTSTRAP_JOB
        or not isinstance(execution_annotations, dict)
        or not isinstance(job_annotations, dict)
        or set(execution_annotations) != expected_annotation_names
        or execution_annotations != job_annotations
        or execution_annotations["42.ogilvy/bootstrap-manifest-sha256"] != required_selector
        or execution_annotations["42.ogilvy/execution-approval-sha256"] != required_selector
    ):
        raise MigrationRefusal("execution_approval_bootstrap_unapproved")
    return execution_annotations


def _bootstrap_object_generations(
    execution_annotations: Mapping[str, object],
) -> tuple[int, int]:
    try:
        manifest_generation = int(execution_annotations["42.ogilvy/bootstrap-manifest-generation"])
        signature_generation = int(
            execution_annotations["42.ogilvy/bootstrap-signature-generation"]
        )
    except (TypeError, ValueError) as exc:
        raise MigrationRefusal("execution_approval_bootstrap_unapproved") from exc
    if manifest_generation <= 0 or signature_generation <= 0:
        raise MigrationRefusal("execution_approval_bootstrap_unapproved")
    return manifest_generation, signature_generation


def _bootstrap_public_key_pem(session: object) -> bytes:
    public_key = _response_json(
        session.get(f"https://cloudkms.googleapis.com/v1/{KMS_KEY_VERSION}/publicKey")
    )
    if (
        public_key.get("name") != KMS_KEY_VERSION
        or public_key.get("algorithm") != "EC_SIGN_P256_SHA256"
        or not isinstance(public_key.get("pem"), str)
    ):
        raise MigrationRefusal("execution_approval_bootstrap_unapproved")
    return public_key["pem"].encode()


def _bootstrap_origin(manifest: Mapping[str, object]) -> execution_origins.ExecutionOrigin:
    """Select the retained bootstrap origin row, historical read only, never a v2 path."""

    try:
        registry = execution_origins.load_origin_registry(
            REGISTRY_PATH,
            expected_sha256=REGISTRATION_DIGESTS["execution_origins_v1.json"],
            contract_root=ROOT,
        )
        return execution_origins.resolve_origin(manifest, "historical_read", registry)
    except execution_origins.OriginRefusal as exc:
        raise MigrationRefusal("execution_approval_artifact_mismatch") from exc


def _bootstrap_build_payload(
    session: object,
    manifest: Mapping[str, object],
) -> Mapping[str, object]:
    build_resource = manifest["build_resource"]
    origin = _bootstrap_origin(manifest)
    build = _response_json(session.get(f"https://cloudbuild.googleapis.com/v1/{build_resource}"))
    source_provenance = build.get("sourceProvenance")
    results = build.get("results")
    if isinstance(source_provenance, dict) and source_provenance:
        resolved_source = _resolved_build_source(source_provenance)
    else:
        # Second-generation connected-repository builds return an empty sourceProvenance
        # and carry the resolved revision under source.connectedRepository. The shared
        # normalizer binds the exact approved repository before the revision is trusted.
        try:
            resolved_source = execution_approval._connected_repository_sha(
                build.get("source"), origin=origin
            )
        except execution_approval.ApprovalRefusal as exc:
            raise MigrationRefusal("execution_approval_artifact_mismatch") from exc
        if resolved_source is None:
            raise MigrationRefusal("execution_approval_artifact_mismatch")
    try:
        canonical_name = execution_approval._canonical_build_resource(build.get("name"))
    except execution_approval.ApprovalRefusal as exc:
        raise MigrationRefusal("execution_approval_artifact_mismatch") from exc
    images = results.get("images") if isinstance(results, dict) else None
    image = images[0] if isinstance(images, list) and len(images) == 1 else None
    image_name = image.get("name") if isinstance(image, dict) else None
    image_digest = image.get("digest") if isinstance(image, dict) else None
    if (
        canonical_name != build_resource
        or build.get("projectId") != PROJECT
        or build.get("status") != "SUCCESS"
        or resolved_source != manifest["source_sha"]
        or not isinstance(image_name, str)
        or re.fullmatch(origin.image_name, image_name) is None
        or not isinstance(image_digest, str)
        or f"{origin.image_repository}@{image_digest}" != manifest["image_uri"]
    ):
        raise MigrationRefusal("execution_approval_artifact_mismatch")
    return {
        "build_resource": build_resource,
        "status": "SUCCESS",
        "resolved_source_sha": manifest["source_sha"],
        "image_uri": manifest["image_uri"],
    }


def _load_bootstrap_inputs(credentials: object) -> BootstrapInputs:
    required_selector, execution_id = _bootstrap_runtime_selector()
    session = _authorized_session(credentials)
    execution_name = f"{BOOTSTRAP_JOB}/executions/{execution_id}"
    execution_annotations = _bootstrap_execution_annotations(
        session,
        execution_name,
        required_selector,
    )
    manifest_generation, signature_generation = _bootstrap_object_generations(execution_annotations)

    manifest_bytes, _manifest_metadata = _storage_read(
        session,
        f"bootstrap/manifests/{required_selector}.json",
        manifest_generation,
    )
    signature_bytes, _signature_metadata = _storage_read(
        session,
        f"bootstrap/signatures/{required_selector}.sig",
        signature_generation,
    )
    if hashlib.sha256(manifest_bytes).hexdigest() != required_selector:
        raise MigrationRefusal("execution_approval_manifest_mismatch")
    manifest = _parse_manifest(manifest_bytes)
    _validate_bootstrap_manifest(manifest)
    if execution_annotations["42.ogilvy/source-sha"] != manifest["source_sha"]:
        raise MigrationRefusal("execution_approval_artifact_mismatch")

    public_key_pem = _bootstrap_public_key_pem(session)
    build_payload = _bootstrap_build_payload(session, manifest)
    return BootstrapInputs(
        manifest_bytes=manifest_bytes,
        signature_bytes=signature_bytes,
        public_key_pem=public_key_pem,
        object_generations={
            "manifest": manifest_generation,
            "signature": signature_generation,
        },
        build_payload=build_payload,
    )


def _read_preflight(
    plan: ApprovalStoreMigrationPlan,
    credentials: object,
    authority: BootstrapAuthority,
) -> None:
    if (
        plan.project != PROJECT
        or plan.dataset != DATASET
        or plan.location != LOCATION
        or tuple(item.name for item in plan.tables) != TABLE_NAMES
        or tuple(item.name for item in plan.routines) != tuple(item[0] for item in ROUTINE_SPECS)
        or authority.manifest_sha256 != hashlib.sha256(authority.manifest_bytes).hexdigest()
        or authority.manifest.get("operation") != "bootstrap_migration_apply"
    ):
        raise MigrationRefusal("execution_approval_target_invalid")
    client = _bigquery_client(credentials)
    try:
        client.get_dataset(f"{PROJECT}.{DATASET}")
    except Exception as exc:
        code = getattr(exc, "code", None)
        code = code() if callable(code) else code
        if code == 404 or exc.__class__.__name__ == "NotFound":
            return
        raise MigrationRefusal("execution_approval_internal_refusal") from exc
    raise MigrationRefusal("execution_approval_schema_mismatch")


def _create_bootstrap_lock(credentials: object, authority: BootstrapAuthority) -> None:
    client = _storage_client(credentials)
    blob = client.bucket("ogilvy-trends-v2-execution-approvals-staging").blob(
        "bootstrap/locks/open-intelligence-execution-approval-v1.lock"
    )
    content = authority.manifest_sha256.encode()
    try:
        blob.upload_from_string(content, if_generation_match=0)
        blob.reload()
        generation = blob.generation
        created_at = blob.time_created
        readback = blob.download_as_bytes(if_generation_match=generation)
    except Exception as exc:
        raise MigrationRefusal("execution_approval_bootstrap_used") from exc
    if (
        type(generation) is not int
        or generation <= 0
        or not isinstance(created_at, datetime)
        or created_at.tzinfo is None
        or readback != content
    ):
        raise MigrationRefusal("execution_approval_bootstrap_unapproved")


def _apply_ddl(
    plan: ApprovalStoreMigrationPlan,
    credentials: object,
    authority: BootstrapAuthority,
) -> None:
    if authority.manifest_sha256 != hashlib.sha256(authority.manifest_bytes).hexdigest():
        raise MigrationRefusal("execution_approval_manifest_mismatch")
    try:
        from google.cloud import bigquery

        client = _bigquery_client(credentials)
        dataset = bigquery.Dataset(f"{PROJECT}.{DATASET}")
        dataset.location = LOCATION
        dataset.default_table_expiration_ms = plan.default_table_expiration
        dataset.default_partition_expiration_ms = None
        dataset.max_time_travel_hours = plan.time_travel_hours
        client.create_dataset(dataset)
        for source in (*plan.tables, *plan.routines):
            sql = source.sql.replace("{project}", PROJECT).replace("{dataset}", DATASET)
            client.query(sql).result()
        seed_sql = f"""
INSERT INTO `{PROJECT}.{DATASET}.open_intelligence_execution_approval_lock_v1`
  (lock_name, approval_contract_version, lock_version, state, last_approval_id, created_at, updated_at)
SELECT
  '{APPROVAL_CONTRACT_VERSION}', '{APPROVAL_CONTRACT_VERSION}', 0, 'ready', NULL,
  CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP()
FROM (SELECT 1)
WHERE NOT EXISTS (
  SELECT 1 FROM `{PROJECT}.{DATASET}.open_intelligence_execution_approval_lock_v1`
)
""".strip()
        client.query(seed_sql).result()
    except MigrationRefusal:
        raise
    except Exception as exc:
        raise MigrationRefusal("execution_approval_internal_refusal") from exc


_API_TYPE_ALIASES = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}


def _schema_from_sql(sql: str) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (
            match.group("name"),
            match.group("type"),
            "REQUIRED" if match.group("required") else "NULLABLE",
            match.group("description"),
        )
        for match in re.finditer(
            r"^  (?P<name>[a-z0-9_]+) (?P<type>STRING|INT64|TIMESTAMP)"
            r"(?P<required> NOT NULL)? OPTIONS\(description = '(?P<description>[^']+)'\),?$",
            sql,
            re.MULTILINE,
        )
    )


def _table_description(sql: str) -> str:
    matches = re.findall(r"description = '([^']+)'", sql)
    if not matches:
        raise MigrationRefusal("execution_approval_schema_mismatch")
    return matches[-1]


_ROUTINE_BODY_MARKER = re.compile(r"\n(?P<label>[a-z_]+: )?BEGIN\n")


def _routine_body(sql: str) -> str:
    # The body starts at the first BEGIN on its own line after the header. A block
    # label before it (main: BEGIN) is kept as part of the definition, although BigQuery
    # rejects a labelled procedure body, so no deployed routine carries one.
    match = _ROUTINE_BODY_MARKER.search(sql)
    if match is None:
        raise MigrationRefusal("execution_approval_schema_mismatch")
    label = match.group("label") or ""
    return f"{label}BEGIN\n" + sql[match.end() :].rstrip().removesuffix(";")


def _row_value(row: object, field: str) -> object:
    if isinstance(row, Mapping):
        return row.get(field)
    try:
        return row[field]
    except (KeyError, TypeError):
        return getattr(row, field, None)


def _readback_store(
    plan: ApprovalStoreMigrationPlan,
    credentials: object,
    authority: BootstrapAuthority,
) -> None:
    if authority.manifest_sha256 != hashlib.sha256(authority.manifest_bytes).hexdigest():
        raise MigrationRefusal("execution_approval_manifest_mismatch")
    try:
        client = _bigquery_client(credentials)
        dataset = client.get_dataset(f"{PROJECT}.{DATASET}")
        # The API reports max_time_travel_hours as a decimal string on readback.
        time_travel = dataset.max_time_travel_hours
        if isinstance(time_travel, str) and time_travel.isdecimal():
            time_travel = int(time_travel)
        if (
            dataset.location != LOCATION
            or dataset.default_table_expiration_ms is not None
            or dataset.default_partition_expiration_ms is not None
            or time_travel != plan.time_travel_hours
        ):
            raise MigrationRefusal("execution_approval_schema_mismatch")
        for table_plan in plan.tables:
            table = client.get_table(f"{PROJECT}.{DATASET}.{table_plan.name}")
            # The API reports legacy type names; INT64 columns read back as INTEGER.
            actual_schema = tuple(
                (
                    field.name,
                    _API_TYPE_ALIASES.get(field.field_type, field.field_type),
                    field.mode,
                    field.description,
                )
                for field in table.schema
            )
            if (
                table.table_type != "TABLE"
                or table.expires is not None
                or table.description != _table_description(table_plan.sql)
                or actual_schema != _schema_from_sql(table_plan.sql)
            ):
                raise MigrationRefusal("execution_approval_schema_mismatch")
        parameter_rows = tuple(
            client.query(
                f"SELECT specific_name, ordinal_position, parameter_name, data_type, parameter_mode "
                f"FROM `{PROJECT}.{DATASET}.INFORMATION_SCHEMA.PARAMETERS` "
                f"ORDER BY specific_name, ordinal_position"
            ).result()
        )
        actual_parameters: dict[str, list[tuple[int, str, str, str]]] = {}
        for row in parameter_rows:
            name = _row_value(row, "specific_name")
            # INFORMATION_SCHEMA.PARAMETERS reports NULL parameter_mode for procedure inputs.
            mode = _row_value(row, "parameter_mode")
            actual_parameters.setdefault(name, []).append(
                (
                    _row_value(row, "ordinal_position"),
                    _row_value(row, "parameter_name"),
                    _row_value(row, "data_type"),
                    "IN" if mode is None else mode,
                )
            )
        expected_parameters = {
            item.name: [
                (position, name, data_type, "IN")
                for position, (name, data_type) in enumerate(item.parameters, start=1)
            ]
            for item in plan.routines
        }
        if actual_parameters != expected_parameters:
            raise MigrationRefusal("execution_approval_schema_mismatch")
        routine_rows = tuple(
            client.query(
                f"SELECT routine_name, routine_definition FROM `{PROJECT}.{DATASET}.INFORMATION_SCHEMA.ROUTINES`"
            ).result()
        )
        actual_routines = {
            _row_value(row, "routine_name"): _row_value(row, "routine_definition")
            for row in routine_rows
        }
        expected_routines = {
            item.name: _routine_body(item.sql)
            .replace("{project}", PROJECT)
            .replace("{dataset}", DATASET)
            for item in plan.routines
        }
        if actual_routines != expected_routines:
            raise MigrationRefusal("execution_approval_schema_mismatch")
        lock_rows = tuple(
            client.query(
                f"SELECT * FROM `{PROJECT}.{DATASET}.open_intelligence_execution_approval_lock_v1`"
            ).result()
        )
        if len(lock_rows) != 1:
            raise MigrationRefusal("execution_approval_schema_mismatch")
        lock = lock_rows[0]
        created_at = _row_value(lock, "created_at")
        updated_at = _row_value(lock, "updated_at")
        if (
            _row_value(lock, "lock_name") != APPROVAL_CONTRACT_VERSION
            or _row_value(lock, "approval_contract_version") != APPROVAL_CONTRACT_VERSION
            or _row_value(lock, "lock_version") != 0
            or _row_value(lock, "state") != "ready"
            or _row_value(lock, "last_approval_id") is not None
            or not isinstance(created_at, datetime)
            or created_at.tzinfo is None
            or updated_at != created_at
        ):
            raise MigrationRefusal("execution_approval_schema_mismatch")
    except MigrationRefusal:
        raise
    except Exception as exc:
        raise MigrationRefusal("execution_approval_internal_refusal") from exc


class _GoogleIamAdapter:
    def __init__(self, credentials: object):
        self._credentials = credentials
        self._session = _authorized_session(credentials)

    def _policy_endpoint(self, resource: str) -> tuple[str, str, str]:
        if resource == f"projects/{PROJECT}":
            base = f"https://cloudresourcemanager.googleapis.com/v1/{resource}"
            return base + ":getIamPolicy", base + ":setIamPolicy", "post"
        if "/locations/us-central1/jobs/" in resource:
            base = f"https://run.googleapis.com/v2/{resource}"
            return base + ":getIamPolicy", base + ":setIamPolicy", "get"
        if f"projects/{PROJECT}/datasets/{DATASET}/routines/" in resource:
            base = f"https://bigquery.googleapis.com/bigquery/v2/{resource}"
            return base + ":getIamPolicy", base + ":setIamPolicy", "post"
        if resource.startswith("projects/") and "/cryptoKeys/" in resource:
            # Cloud KMS serves getIamPolicy on GET only, and conditional bindings are
            # returned only when policy version 3 is requested (live probe, 2 Sep 2026).
            key = resource.split("/cryptoKeyVersions/", 1)[0]
            base = f"https://cloudkms.googleapis.com/v1/{key}"
            return (
                base + ":getIamPolicy?options.requestedPolicyVersion=3",
                base + ":setIamPolicy",
                "get",
            )
        raise MigrationRefusal("execution_approval_target_invalid")

    def _get_policy(self, resource: str) -> dict[str, object]:
        get_url, _set_url, method = self._policy_endpoint(resource)
        response = (
            self._session.get(get_url)
            if method == "get"
            else self._session.post(get_url, json={"options": {"requestedPolicyVersion": 3}})
        )
        return dict(_response_json(response))

    def _set_policy(self, resource: str, policy: Mapping[str, object]) -> None:
        _get_url, set_url, _method = self._policy_endpoint(resource)
        response = self._session.post(set_url, json={"policy": dict(policy)})
        _response_json(response)

    @staticmethod
    def _storage_condition(resource: str) -> dict[str, str]:
        object_name = resource.removeprefix("gs://ogilvy-trends-v2-execution-approvals-staging/")
        return {
            "title": "42-bootstrap-" + hashlib.sha256(object_name.encode()).hexdigest()[:16],
            "description": "Bounded 42 staging bootstrap object access.",
            "expression": (
                "resource.name == "
                f"'projects/_/buckets/ogilvy-trends-v2-execution-approvals-staging/objects/{object_name}'"
            ),
        }

    @staticmethod
    def _policy_condition(binding: IamBinding) -> dict[str, str] | None:
        if "/cryptoKeyVersions/" not in binding.resource:
            return None
        return {
            "title": "42-bootstrap-kms-version",
            "description": "Bounded 42 staging bootstrap KMS public key access.",
            "expression": f"resource.name == '{binding.resource}'",
        }

    def _storage_policy(self):
        bucket = _storage_client(self._credentials).bucket(
            "ogilvy-trends-v2-execution-approvals-staging"
        )
        return bucket, bucket.get_iam_policy(requested_policy_version=3)

    def _dataset_has_writer(self, binding: IamBinding) -> bool:
        dataset = _bigquery_client(self._credentials).get_dataset(f"{PROJECT}.trends_v2_staging")
        principal = binding.principal.removeprefix("serviceAccount:")
        return any(
            entry.role == "WRITER"
            and entry.entity_type == "userByEmail"
            and entry.entity_id == principal
            for entry in dataset.access_entries
        )

    def has_binding(self, binding: IamBinding) -> bool:
        if binding.resource == f"projects/{PROJECT}/datasets/trends_v2_staging":
            return self._dataset_has_writer(binding)
        if binding.resource.startswith("gs://"):
            _bucket, policy = self._storage_policy()
            expected_condition = self._storage_condition(binding.resource)
            return any(
                item.get("role") == binding.role
                and binding.principal in item.get("members", ())
                and item.get("condition") == expected_condition
                for item in policy.bindings
            )
        policy = self._get_policy(binding.resource)
        expected_condition = self._policy_condition(binding)
        return any(
            item.get("role") == binding.role
            and binding.principal in item.get("members", ())
            and item.get("condition") == expected_condition
            for item in policy.get("bindings", ())
        )

    def add_binding(self, binding: IamBinding) -> None:
        if binding.resource.startswith("gs://"):
            bucket, policy = self._storage_policy()
            policy.bindings.append(
                {
                    "role": binding.role,
                    "members": {binding.principal},
                    "condition": self._storage_condition(binding.resource),
                }
            )
            bucket.set_iam_policy(policy)
            return
        policy = self._get_policy(binding.resource)
        bindings = list(policy.get("bindings", ()))
        condition = self._policy_condition(binding)
        match = next(
            (
                item
                for item in bindings
                if item.get("role") == binding.role and item.get("condition") == condition
            ),
            None,
        )
        if match is None:
            item = {"role": binding.role, "members": [binding.principal]}
            if condition is not None:
                item["condition"] = condition
            bindings.append(item)
        else:
            members = set(match.get("members", ()))
            members.add(binding.principal)
            match["members"] = sorted(members)
        policy["bindings"] = bindings
        self._set_policy(binding.resource, policy)

    def remove_binding(self, binding: IamBinding) -> None:
        if binding.resource.startswith("gs://"):
            bucket, policy = self._storage_policy()
            condition = self._storage_condition(binding.resource)
            policy.bindings = [
                item
                for item in policy.bindings
                if not (
                    item.get("role") == binding.role
                    and binding.principal in item.get("members", ())
                    and item.get("condition") == condition
                )
            ]
            bucket.set_iam_policy(policy)
            return
        policy = self._get_policy(binding.resource)
        condition = self._policy_condition(binding)
        revised = []
        for item in policy.get("bindings", ()):
            if item.get("role") != binding.role or item.get("condition") != condition:
                revised.append(item)
                continue
            members = set(item.get("members", ()))
            members.discard(binding.principal)
            if members:
                item["members"] = sorted(members)
                revised.append(item)
        policy["bindings"] = revised
        self._set_policy(binding.resource, policy)

    @staticmethod
    def _authorization_entry(authorization: RoutineAuthorization) -> dict[str, object]:
        routine_name = authorization.routine.rsplit("/", 1)[1]
        return {
            "role": authorization.role,
            "routine": {
                "projectId": PROJECT,
                "datasetId": DATASET,
                "routineId": routine_name,
            },
        }

    def _dataset_access(self) -> tuple[str, dict[str, object]]:
        url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/datasets/{DATASET}"
        return url, dict(_response_json(self._session.get(url)))

    def has_authorization(self, authorization: RoutineAuthorization) -> bool:
        _url, dataset = self._dataset_access()
        expected = self._authorization_entry(authorization)
        return expected in dataset.get("access", ())

    def add_authorizations(self, authorizations: Sequence[RoutineAuthorization]) -> None:
        # One dataset patch for every entry: BigQuery rate-limits dataset metadata
        # updates, and the live bootstrap hit 403 on the sixth single-entry patch.
        entries = [self._authorization_entry(item) for item in authorizations]
        if not entries:
            return
        url, dataset = self._dataset_access()
        access = list(dataset.get("access", ()))
        access.extend(entry for entry in entries if entry not in access)
        response = self._session.patch(url, params={"fields": "access"}, json={"access": access})
        _response_json(response)

    def remove_authorizations(self, authorizations: Sequence[RoutineAuthorization]) -> None:
        entries = [self._authorization_entry(item) for item in authorizations]
        if not entries:
            return
        url, dataset = self._dataset_access()
        access = [item for item in dataset.get("access", ()) if item not in entries]
        response = self._session.patch(url, params={"fields": "access"}, json={"access": access})
        _response_json(response)

    def add_authorization(self, authorization: RoutineAuthorization) -> None:
        self.add_authorizations((authorization,))

    def remove_authorization(self, authorization: RoutineAuthorization) -> None:
        self.remove_authorizations((authorization,))

    def remove_direct_bootstrap_access(self) -> None:
        client = _bigquery_client(self._credentials)
        dataset = client.get_dataset(f"{PROJECT}.{DATASET}")
        bootstrap_email = BOOTSTRAP_IDENTITY
        revised = [
            entry
            for entry in dataset.access_entries
            if not (
                entry.entity_type in {"userByEmail", "specialGroup"}
                and entry.entity_id == bootstrap_email
            )
        ]
        if revised != list(dataset.access_entries):
            dataset.access_entries = revised
            client.update_dataset(dataset, ["access_entries"])

    def has_direct_bootstrap_access(self) -> bool:
        dataset = _bigquery_client(self._credentials).get_dataset(f"{PROJECT}.{DATASET}")
        return any(
            entry.entity_type in {"userByEmail", "specialGroup"}
            and entry.entity_id == BOOTSTRAP_IDENTITY
            for entry in dataset.access_entries
        )


def _iam_adapter(credentials: object):
    return _GoogleIamAdapter(credentials)


def _exact_bootstrap_binding(
    binding: IamBinding,
    authority: BootstrapAuthority,
) -> IamBinding:
    return IamBinding(
        principal=binding.principal,
        resource=binding.resource.replace("{manifest_sha256}", authority.manifest_sha256),
        role=binding.role,
        purpose=binding.purpose,
    )


def _apply_iam_plan(
    plan: ApprovalStoreMigrationPlan,
    credentials: object,
    authority: BootstrapAuthority,
) -> tuple[IamBinding | RoutineAuthorization, ...]:
    if authority.manifest_sha256 != hashlib.sha256(authority.manifest_bytes).hexdigest():
        raise MigrationRefusal("execution_approval_manifest_mismatch")
    adapter = _iam_adapter(credentials)
    temporary_bindings = tuple(
        _exact_bootstrap_binding(binding, authority)
        for binding in plan.iam_plan.temporary_bootstrap_bindings
    )
    required_bindings = (
        *plan.iam_plan.required_existing_job_user_bindings,
        plan.iam_plan.required_existing_dataset_writer,
        *temporary_bindings,
    )
    if any(not adapter.has_binding(binding) for binding in required_bindings):
        raise MigrationRefusal("execution_approval_identity_invalid")
    created: list[IamBinding | RoutineAuthorization] = []
    try:
        missing_authorizations = [
            authorization
            for authorization in plan.iam_plan.routine_authorizations
            if not adapter.has_authorization(authorization)
        ]
        if missing_authorizations:
            adapter.add_authorizations(missing_authorizations)
            created.extend(missing_authorizations)
        bindings = (
            *plan.iam_plan.proposed_job_user_grants,
            *plan.iam_plan.principal_routine_bindings,
            *plan.iam_plan.run_viewer_bindings,
            *plan.iam_plan.cloudbuild_viewer_bindings,
        )
        for binding in bindings:
            if not adapter.has_binding(binding):
                adapter.add_binding(binding)
                created.append(binding)
        if any(
            not adapter.has_authorization(item)
            if isinstance(item, RoutineAuthorization)
            else not adapter.has_binding(item)
            for item in created
        ):
            raise MigrationRefusal("execution_approval_identity_invalid")
    except Exception:
        _rollback_attempt_grants(plan, credentials, authority, tuple(created))
        raise
    return tuple(created)


def _rollback_attempt_grants(
    _plan: ApprovalStoreMigrationPlan,
    credentials: object,
    _authority: BootstrapAuthority,
    created: tuple[IamBinding | RoutineAuthorization, ...],
) -> None:
    adapter = _iam_adapter(credentials)
    for item in reversed(created):
        if not isinstance(item, RoutineAuthorization):
            adapter.remove_binding(item)
    adapter.remove_authorizations(
        [item for item in created if isinstance(item, RoutineAuthorization)]
    )


def _parse_server_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise MigrationRefusal("execution_approval_bootstrap_unapproved")
    match = re.fullmatch(
        r"(?P<whole>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(?P<fraction>\d{1,9}))?Z",
        value,
    )
    if match is None:
        raise MigrationRefusal("execution_approval_bootstrap_unapproved")
    fraction = ((match.group("fraction") or "") + "000000")[:6]
    try:
        return datetime.strptime(
            f"{match.group('whole')}.{fraction}Z",
            "%Y-%m-%dT%H:%M:%S.%fZ",
        ).replace(tzinfo=UTC)
    except ValueError as exc:
        raise MigrationRefusal("execution_approval_bootstrap_unapproved") from exc


def _format_server_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise MigrationRefusal("execution_approval_schema_mismatch")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _deterministic_id(prefix: str, payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return prefix + hashlib.sha256(canonical).hexdigest()


def _migration_readback_bytes(
    plan: ApprovalStoreMigrationPlan,
    authority: BootstrapAuthority,
) -> bytes:
    payload = {
        "contract_version": "open_intelligence_execution_approval_store_readback_v1",
        "dataset": f"{PROJECT}.{DATASET}",
        "manifest_sha256": authority.manifest_sha256,
        "plan_sha256": hashlib.sha256(render_plan(plan).encode()).hexdigest(),
        "routine_sha256": {item.name: item.sha256 for item in plan.routines},
        "table_sha256": {item.name: item.sha256 for item in plan.tables},
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _bootstrap_import_context(
    plan: ApprovalStoreMigrationPlan,
    credentials: object,
    authority: BootstrapAuthority,
) -> _BootstrapImportContext:
    manifest_object = f"bootstrap/manifests/{authority.manifest_sha256}.json"
    signature_object = f"bootstrap/signatures/{authority.manifest_sha256}.sig"
    session = _authorized_session(credentials)
    manifest_bytes, manifest_metadata = _storage_read(
        session,
        manifest_object,
        authority.object_generations["manifest"],
    )
    signature_bytes, signature_metadata = _storage_read(
        session,
        signature_object,
        authority.object_generations["signature"],
    )
    if manifest_bytes != authority.manifest_bytes:
        raise MigrationRefusal("execution_approval_manifest_mismatch")
    manifest_created_at = _parse_server_time(manifest_metadata.get("timeCreated"))
    signature_created_at = _parse_server_time(signature_metadata.get("timeCreated"))

    lock_blob = (
        _storage_client(credentials)
        .bucket("ogilvy-trends-v2-execution-approvals-staging")
        .blob("bootstrap/locks/open-intelligence-execution-approval-v1.lock")
    )
    lock_blob.reload()
    lock_generation = lock_blob.generation
    lock_created_at = lock_blob.time_created
    if (
        type(lock_generation) is not int
        or lock_generation <= 0
        or not isinstance(lock_created_at, datetime)
        or lock_created_at.tzinfo is None
        or lock_blob.download_as_bytes(if_generation_match=lock_generation)
        != authority.manifest_sha256.encode()
    ):
        raise MigrationRefusal("execution_approval_bootstrap_unapproved")

    execution_id = os.getenv("CLOUD_RUN_EXECUTION")
    if not isinstance(execution_id, str) or not execution_id or "/" in execution_id:
        raise MigrationRefusal("execution_approval_execution_mismatch")
    execution_name = f"{BOOTSTRAP_JOB}/executions/{execution_id}"
    return _BootstrapImportContext(
        manifest_object=manifest_object,
        manifest_created_at=manifest_created_at,
        signature_object=signature_object,
        signature_created_at=signature_created_at,
        signature_sha256=hashlib.sha256(signature_bytes).hexdigest(),
        lock_generation=lock_generation,
        lock_created_at=lock_created_at,
        execution_name=execution_name,
        migration_result_reference=execution_name + "#approval-store-readback",
        migration_result_digest=hashlib.sha256(
            _migration_readback_bytes(plan, authority)
        ).hexdigest(),
    )


def _bootstrap_import_parameters(
    bigquery: object,
    authority: BootstrapAuthority,
    context: _BootstrapImportContext,
) -> list[object]:
    return [
        bigquery.ScalarQueryParameter(
            "canonical_manifest_json", "STRING", authority.manifest_bytes.decode()
        ),
        bigquery.ScalarQueryParameter("manifest_sha256", "STRING", authority.manifest_sha256),
        bigquery.ScalarQueryParameter(
            "manifest_object",
            "STRING",
            "gs://ogilvy-trends-v2-execution-approvals-staging/" + context.manifest_object,
        ),
        bigquery.ScalarQueryParameter(
            "manifest_generation", "INT64", authority.object_generations["manifest"]
        ),
        bigquery.ScalarQueryParameter(
            "manifest_created_at", "TIMESTAMP", context.manifest_created_at
        ),
        bigquery.ScalarQueryParameter(
            "signature_object",
            "STRING",
            "gs://ogilvy-trends-v2-execution-approvals-staging/" + context.signature_object,
        ),
        bigquery.ScalarQueryParameter(
            "signature_generation", "INT64", authority.object_generations["signature"]
        ),
        bigquery.ScalarQueryParameter("signature_sha256", "STRING", context.signature_sha256),
        bigquery.ScalarQueryParameter("kms_key_version", "STRING", KMS_KEY_VERSION),
        bigquery.ScalarQueryParameter(
            "signature_created_at", "TIMESTAMP", context.signature_created_at
        ),
        bigquery.ScalarQueryParameter("bootstrap_lock_object", "STRING", BOOTSTRAP_LOCK_OBJECT),
        bigquery.ScalarQueryParameter(
            "bootstrap_lock_generation", "INT64", context.lock_generation
        ),
        bigquery.ScalarQueryParameter(
            "bootstrap_lock_created_at", "TIMESTAMP", context.lock_created_at
        ),
        bigquery.ScalarQueryParameter("execution_name", "STRING", context.execution_name),
        bigquery.ScalarQueryParameter("job_resource", "STRING", BOOTSTRAP_JOB),
        bigquery.ScalarQueryParameter("source_sha", "STRING", authority.manifest["source_sha"]),
        bigquery.ScalarQueryParameter("image_uri", "STRING", authority.manifest["image_uri"]),
        bigquery.ScalarQueryParameter(
            "migration_result_reference", "STRING", context.migration_result_reference
        ),
        bigquery.ScalarQueryParameter(
            "migration_result_digest", "STRING", context.migration_result_digest
        ),
    ]


def _bootstrap_expected_ids(
    authority: BootstrapAuthority,
    context: _BootstrapImportContext,
) -> tuple[str, str]:
    approval_id = _deterministic_id(
        "exa_",
        {
            "approval_contract_version": APPROVAL_CONTRACT_VERSION,
            "approved_at": _format_server_time(context.signature_created_at),
            "approved_by": "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab",
            "manifest_sha256": authority.manifest_sha256,
        },
    )
    consumption_id = _deterministic_id(
        "exc_",
        {
            "approval_id": approval_id,
            "consumed_at": _format_server_time(context.lock_created_at),
            "execution_name": context.execution_name,
        },
    )
    return approval_id, consumption_id


def _bootstrap_receipt_from_row(row: object) -> ApprovalStoreMigrationReceipt:
    return ApprovalStoreMigrationReceipt(
        manifest_sha256=_row_value(row, "manifest_sha256"),
        approval_id=_row_value(row, "approval_id"),
        consumption_id=_row_value(row, "consumption_id"),
        result_id=_row_value(row, "result_id"),
        migration_result_reference=_row_value(row, "migration_result_reference"),
        migration_result_digest=_row_value(row, "migration_result_digest"),
        bootstrap_proof_digest=_row_value(row, "bootstrap_proof_digest"),
    )


def _validate_bootstrap_receipt(
    receipt: ApprovalStoreMigrationReceipt,
    authority: BootstrapAuthority,
    context: _BootstrapImportContext,
    expected_approval_id: str,
    expected_consumption_id: str,
) -> None:
    if (
        receipt.manifest_sha256 != authority.manifest_sha256
        or receipt.approval_id != expected_approval_id
        or receipt.consumption_id != expected_consumption_id
        or receipt.migration_result_reference != context.migration_result_reference
        or receipt.migration_result_digest != context.migration_result_digest
        or re.fullmatch(r"exr_[0-9a-f]{64}", receipt.result_id) is None
        or _HEX_64.fullmatch(receipt.bootstrap_proof_digest) is None
    ):
        raise MigrationRefusal("execution_approval_schema_mismatch")


def _validate_bootstrap_readback(
    readback: object,
    receipt: ApprovalStoreMigrationReceipt,
    context: _BootstrapImportContext,
    expected_approval_id: str,
    expected_consumption_id: str,
) -> None:
    canonical_result_json = _row_value(readback, "canonical_result_json")
    completed_at = _row_value(readback, "completed_at")
    if not isinstance(canonical_result_json, str) or not isinstance(completed_at, datetime):
        raise MigrationRefusal("execution_approval_schema_mismatch")
    expected_result_id = _deterministic_id(
        "exr_",
        {
            "completed_at": _format_server_time(completed_at),
            "consumption_id": expected_consumption_id,
            "result_digest": receipt.bootstrap_proof_digest,
            "result_reference": context.migration_result_reference,
            "status": "succeeded",
        },
    )
    if (
        hashlib.sha256(canonical_result_json.encode()).hexdigest() != receipt.bootstrap_proof_digest
        or expected_result_id != receipt.result_id
        or _row_value(readback, "approval_id") != expected_approval_id
        or _row_value(readback, "consumption_id") != expected_consumption_id
        or _row_value(readback, "result_reference") != context.migration_result_reference
        or _row_value(readback, "result_digest") != receipt.bootstrap_proof_digest
        or _row_value(readback, "status") != "succeeded"
    ):
        raise MigrationRefusal("execution_approval_schema_mismatch")


def _import_bootstrap(
    plan: ApprovalStoreMigrationPlan,
    credentials: object,
    authority: BootstrapAuthority,
) -> ApprovalStoreMigrationReceipt:
    try:
        from google.cloud import bigquery

        context = _bootstrap_import_context(plan, credentials, authority)
        query_parameters = _bootstrap_import_parameters(bigquery, authority, context)
        call_sql = (
            f"CALL `{PROJECT}.{DATASET}.sp_import_open_intelligence_execution_bootstrap_v1`("
            + ",".join(f"@{parameter.name}" for parameter in query_parameters)
            + ")"
        )
        client = _bigquery_client(credentials)
        rows = tuple(
            client.query(
                call_sql,
                job_config=bigquery.QueryJobConfig(query_parameters=query_parameters),
            ).result()
        )
        if len(rows) != 1:
            raise MigrationRefusal("execution_approval_schema_mismatch")
        receipt = _bootstrap_receipt_from_row(rows[0])
        expected_approval_id, expected_consumption_id = _bootstrap_expected_ids(
            authority,
            context,
        )
        _validate_bootstrap_receipt(
            receipt,
            authority,
            context,
            expected_approval_id,
            expected_consumption_id,
        )
        readback_rows = tuple(
            client.query(
                f"SELECT r.result_id, r.result_reference, r.canonical_result_json, r.result_digest, "
                f"r.status, r.completed_at, c.consumption_id, a.approval_id "
                f"FROM `{PROJECT}.{DATASET}.open_intelligence_execution_results_v1` r "
                f"JOIN `{PROJECT}.{DATASET}.open_intelligence_execution_consumptions_v1` c USING (consumption_id) "
                f"JOIN `{PROJECT}.{DATASET}.open_intelligence_execution_approvals_v1` a "
                f"ON a.approval_id = c.approval_id "
                f"WHERE r.result_id = '{receipt.result_id}'"
            ).result()
        )
        if len(readback_rows) != 1:
            raise MigrationRefusal("execution_approval_schema_mismatch")
        _validate_bootstrap_readback(
            readback_rows[0],
            receipt,
            context,
            expected_approval_id,
            expected_consumption_id,
        )
        return receipt
    except MigrationRefusal:
        raise
    except Exception as exc:
        raise MigrationRefusal("execution_approval_internal_refusal") from exc


def _remove_bootstrap_access(
    plan: ApprovalStoreMigrationPlan,
    credentials: object,
    authority: BootstrapAuthority,
) -> None:
    if authority.manifest_sha256 != hashlib.sha256(authority.manifest_bytes).hexdigest():
        raise MigrationRefusal("execution_approval_manifest_mismatch")
    adapter = _iam_adapter(credentials)
    bootstrap_principal = _service_principal(BOOTSTRAP_IDENTITY)
    bootstrap_routine_bindings = tuple(
        binding
        for binding in plan.iam_plan.principal_routine_bindings
        if binding.principal == bootstrap_principal
    )
    temporary_bindings = tuple(
        _exact_bootstrap_binding(binding, authority)
        for binding in plan.iam_plan.temporary_bootstrap_bindings
    )
    for binding in (*bootstrap_routine_bindings, *temporary_bindings):
        if adapter.has_binding(binding):
            adapter.remove_binding(binding)
    adapter.remove_direct_bootstrap_access()
    if (
        any(
            adapter.has_binding(binding)
            for binding in (
                *bootstrap_routine_bindings,
                *temporary_bindings,
            )
        )
        or adapter.has_direct_bootstrap_access()
    ):
        raise MigrationRefusal("execution_approval_identity_invalid")


def _apply_plan(plan: ApprovalStoreMigrationPlan) -> ApprovalStoreMigrationReceipt:
    credentials = _load_credentials()
    inputs = _load_bootstrap_inputs(credentials)
    authority = _verify_bootstrap_authority(
        inputs.manifest_bytes,
        inputs.signature_bytes,
        inputs.public_key_pem,
        inputs.object_generations,
        inputs.build_payload,
    )
    _read_preflight(plan, credentials, authority)
    _create_bootstrap_lock(credentials, authority)
    _apply_ddl(plan, credentials, authority)
    _readback_store(plan, credentials, authority)
    try:
        created = _apply_iam_plan(plan, credentials, authority)
    except Exception:
        _remove_bootstrap_access(plan, credentials, authority)
        raise
    try:
        receipt = _import_bootstrap(plan, credentials, authority)
    except Exception:
        _rollback_attempt_grants(plan, credentials, authority, created)
        _remove_bootstrap_access(plan, credentials, authority)
        raise
    _remove_bootstrap_access(plan, credentials, authority)
    return receipt


# Versioned (v2) execution store installation and configuration registration.
# Additive to the v1 bootstrap above: nothing here reads, seeds or resets the
# v1 lock, and no v1 routine or table definition is re-applied.


@dataclass(frozen=True)
class RegistrationInput:
    name: str
    path: Path
    sha256: str
    canonical_json: str


@dataclass(frozen=True)
class VersionedStoreMigrationPlan:
    project: str
    dataset: str
    location: str
    tables: tuple[TablePlan, ...]
    routines: tuple[RoutinePlan, ...]
    lock_seed: dict[str, object]
    registrations: tuple[RegistrationInput, ...]
    active_pair: tuple[str, str]
    iam_plan: IamPlan


def _canonical_registration_text(raw: bytes) -> str:
    parsed = _parse_manifest(raw)
    if not _is_nfc(parsed):
        raise MigrationRefusal("execution_approval_manifest_invalid")
    return raw.decode("utf-8")


def _registration_input(name: str, path: Path) -> RegistrationInput:
    if not path.is_file():
        code = (
            "execution_resource_manifest_missing"
            if name.startswith("resource_manifest")
            else "execution_origin_registry_invalid"
        )
        raise MigrationRefusal(code)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != REGISTRATION_DIGESTS[name]:
        raise MigrationRefusal("execution_origin_registry_digest_mismatch")
    return RegistrationInput(
        name=name,
        path=path,
        sha256=digest,
        canonical_json=_canonical_registration_text(raw),
    )


def _registration_generation(
    registry_name: str, registry_path: Path, resource_name: str, resource_path: Path
) -> tuple[RegistrationInput, RegistrationInput, frozenset[str]]:
    """One registry and its manifest, and the policy digests the registry verifies."""

    registry = _registration_input(registry_name, registry_path)
    try:
        loaded = execution_origins.load_origin_registry(
            registry_path,
            expected_sha256=registry.sha256,
            contract_root=ROOT,
        )
    except execution_origins.OriginRefusal as exc:
        raise MigrationRefusal(str(exc)) from exc
    resource = _registration_input(resource_name, resource_path)
    resource_payload = json.loads(resource.canonical_json)
    if (
        not isinstance(resource_payload, dict)
        or resource_payload.get("origin_registry_sha256") != registry.sha256
    ):
        raise MigrationRefusal("execution_origin_registry_digest_mismatch")
    return registry, resource, frozenset(loaded.verified_successor_policy_digests.values())


# Registrations are laid out as the two (registry, manifest) pairs, amendment e first and
# the active bridge generation second, then every policy either registry names.
_REGISTRATION_PAIR_COUNT = 2


def _build_registrations() -> tuple[RegistrationInput, ...]:
    registry, resource, verified = _registration_generation(
        "execution_origins_v1.json",
        REGISTRY_PATH,
        "resource_manifest.json",
        RESOURCE_MANIFEST_PATH,
    )
    bridge_registry, bridge_resource, bridge_verified = _registration_generation(
        "execution_origins_bridge_v3.json",
        BRIDGE_REGISTRY_PATH,
        "resource_manifest_bridge_v3.json",
        BRIDGE_RESOURCE_MANIFEST_PATH,
    )
    policies = (
        *(_registration_input(name, POLICY_CONTRACT_DIR / name) for name in POLICY_FILES),
        *(
            _registration_input(name, BRIDGE_POLICY_CONTRACT_DIR / name)
            for name in BRIDGE_POLICY_FILES
        ),
    )
    digests = [policy.sha256 for policy in policies]
    if len(set(digests)) != len(digests) or set(digests) != verified | bridge_verified:
        raise MigrationRefusal("execution_origin_registry_invalid")
    return (registry, resource, bridge_registry, bridge_resource, *policies)


def _registration_pairs(
    registrations: Sequence[RegistrationInput],
) -> tuple[tuple[tuple[RegistrationInput, RegistrationInput], ...], tuple[RegistrationInput, ...]]:
    """Split the fixed layout into its (registry, manifest) pairs and its policies."""

    pairs = tuple(
        (registrations[2 * index], registrations[2 * index + 1])
        for index in range(_REGISTRATION_PAIR_COUNT)
    )
    return pairs, tuple(registrations[2 * _REGISTRATION_PAIR_COUNT :])


def _v2_origin_bindings() -> tuple[tuple[str, str, str], ...]:
    """Return (operation, service identity, job resource) for every v2 registry binding."""

    registry = execution_origins.load_origin_registry(
        REGISTRY_PATH,
        expected_sha256=REGISTRATION_DIGESTS["execution_origins_v1.json"],
        contract_root=ROOT,
    )
    bindings = []
    for (manifest_version, _contract), origin in sorted(registry.items()):
        if manifest_version != V2_MANIFEST_VERSION:
            continue
        for operation, binding in sorted(origin.operation_bindings.items()):
            bindings.append((operation, binding.service_identity, binding.job_resource))
    return tuple(bindings)


def _distinct_bindings(rows: Iterable[IamBinding]) -> tuple[IamBinding, ...]:
    seen: set[tuple[str, str, str]] = set()
    kept = []
    for row in rows:
        key = (row.principal, row.resource, row.role)
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
    return tuple(kept)


def _build_v2_iam_plan(routines: tuple[RoutinePlan, ...]) -> IamPlan:
    human = "user:albert.meintjes@ogilvy.co.za"
    routine_by_short_name = {item.name: _routine_resource(item.name) for item in routines}
    authorizations = tuple(
        RoutineAuthorization(
            _routine_resource(item.name), f"{PROJECT}.{DATASET}", item.authorized_role
        )
        for item in routines
    )
    principal_routines = [
        _binding(
            human,
            routine_by_short_name[name],
            "roles/bigquery.dataViewer",
            "operator routine metadata and invocation",
        )
        for name in V2_OPERATOR_ROUTINES
    ]
    bindings = _v2_origin_bindings()
    required_job_users = [
        _binding(
            human,
            _project_resource(),
            "roles/bigquery.jobUser",
            "required existing operator query submission",
        )
    ]
    for operation, identity, job in bindings:
        principal = _service_principal(identity)
        names = list(V2_RUNTIME_ROUTINES)
        if operation in V2_CHAIN_READER_OPERATIONS:
            names.append("sp_read_open_intelligence_execution_result_chain_v2")
        if operation == V2_SOURCE_SNAPSHOT_OPERATION:
            names = [
                V2_SOURCE_SNAPSHOT_ROUTINE
                if name == "sp_consume_open_intelligence_execution_v2"
                else name
                for name in names
            ]
        if job == V2_DAILY_JOB:
            names.extend(V2_DAILY_ROUTINES)
        principal_routines.extend(
            _binding(
                principal,
                routine_by_short_name[name],
                "roles/bigquery.dataViewer",
                f"{operation} routine metadata and invocation",
            )
            for name in names
        )
        required_job_users.append(
            _binding(
                principal,
                _project_resource(),
                "roles/bigquery.jobUser",
                f"required existing {operation} query submission from the deployment delta",
            )
        )
    run_viewers = tuple(
        _binding(
            _service_principal(identity),
            job,
            "roles/run.viewer",
            f"{operation} immutable execution readback",
        )
        for operation, identity, job in bindings
    )
    cloudbuild_viewers = tuple(
        _binding(
            _service_principal(identity),
            _project_resource(),
            "roles/cloudbuild.builds.viewer",
            f"{operation} build provenance readback",
        )
        for operation, identity, _job in bindings
    )
    # Several operations share one identity and one job since amendment d, so every
    # group is deduplicated on principal, resource and role; the first row's purpose
    # stands for the rest.
    return IamPlan(
        proposed_job_user_grants=(),
        required_existing_job_user_bindings=_distinct_bindings(required_job_users),
        routine_authorizations=authorizations,
        principal_routine_bindings=_distinct_bindings(principal_routines),
        run_viewer_bindings=_distinct_bindings(run_viewers),
        cloudbuild_viewer_bindings=_distinct_bindings(cloudbuild_viewers),
        required_existing_dataset_writer=_binding(
            human,
            f"projects/{PROJECT}/datasets/{DATASET}",
            "roles/bigquery.dataEditor",
            "required existing operator DDL and registration authority",
        ),
        temporary_bootstrap_bindings=(),
    )


def build_v2_plan() -> VersionedStoreMigrationPlan:
    tables = tuple(
        TablePlan(
            name=name,
            path=SCHEMA_DIR / f"{name}.sql",
            sha256=_sha256(SCHEMA_DIR / f"{name}.sql"),
            sql=(SCHEMA_DIR / f"{name}.sql").read_text(encoding="utf-8"),
        )
        for name in V2_TABLE_NAMES
    )
    routines = tuple(
        RoutinePlan(
            name=name,
            path=ROUTINE_DIR / f"{name}.sql",
            sha256=_sha256(ROUTINE_DIR / f"{name}.sql"),
            sql=(ROUTINE_DIR / f"{name}.sql").read_text(encoding="utf-8"),
            authorized_role=role,
            parameters=parameters,
        )
        for name, role, parameters in V2_ROUTINE_SPECS
    )
    registrations = _build_registrations()
    return VersionedStoreMigrationPlan(
        project=PROJECT,
        dataset=DATASET,
        location=LOCATION,
        tables=tables,
        routines=routines,
        lock_seed={
            "lock_name": V2_APPROVAL_CONTRACT_VERSION,
            "approval_contract_version": V2_APPROVAL_CONTRACT_VERSION,
            "lock_version": 0,
            "state": "prepared",
            "last_approval_id": None,
        },
        registrations=registrations,
        # The active pair is the bridge generation, the code's active generation.
        active_pair=(registrations[2].sha256, registrations[3].sha256),
        iam_plan=_build_v2_iam_plan(routines),
    )


def _registration_path(item: RegistrationInput) -> str:
    if item.path.is_relative_to(ROOT):
        return item.path.relative_to(ROOT).as_posix()
    return item.path.as_posix()


def render_v2_plan(plan: VersionedStoreMigrationPlan) -> str:
    payload = {
        "active_pair": {
            "origin_registry_sha256": plan.active_pair[0],
            "resource_manifest_sha256": plan.active_pair[1],
        },
        "dataset": {
            "dataset": plan.dataset,
            "location": plan.location,
            "project": plan.project,
        },
        "lock_seed": plan.lock_seed,
        "proof_kind": "code_only_structural_proof",
        "registrations": [
            {"name": item.name, "path": _registration_path(item), "sha256": item.sha256}
            for item in plan.registrations
        ],
        "routines": [_source_payload(item) for item in plan.routines],
        "tables": [_source_payload(item) for item in plan.tables],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def v2_lock_seed_sql() -> str:
    """Seed the v2 singleton as prepared, once, only when its table is empty."""

    return f"""
INSERT INTO `{PROJECT}.{DATASET}.open_intelligence_execution_approval_lock_v2`
  (lock_name, approval_contract_version, lock_version, state, last_approval_id, created_at, updated_at)
SELECT
  '{V2_APPROVAL_CONTRACT_VERSION}', '{V2_APPROVAL_CONTRACT_VERSION}', 0, 'prepared', NULL,
  CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP()
FROM (SELECT 1)
WHERE NOT EXISTS (
  SELECT 1 FROM `{PROJECT}.{DATASET}.open_intelligence_execution_approval_lock_v2`
)
""".strip()


def _registration_statements(
    table: str,
    key_column: str,
    key_parameter: str,
    json_column: str,
    json_parameter: str,
    conflict_code: str,
    extra_columns: str = "",
    extra_values: str = "",
    extra_predicate: str = "",
) -> str:
    qualified = f"`{{project}}.{{dataset}}.{table}`"
    udf = f"`{{project}}.{{dataset}}.{CANONICAL_JSON_UDF}`"
    return f"""
ASSERT @{key_parameter} = LOWER(TO_HEX(SHA256(@{json_parameter}))) AS 'execution_origin_registry_digest_mismatch';
ASSERT {udf}(@{json_parameter}) AND SAFE.PARSE_JSON(@{json_parameter}, wide_number_mode => 'exact') IS NOT NULL{extra_predicate} AS 'execution_origin_registry_invalid';
ASSERT (SELECT COUNT(*) FROM {qualified} WHERE {key_column} = @{key_parameter} AND {json_column} != @{json_parameter}) = 0 AS '{conflict_code}';
ASSERT (SELECT COUNT(*) FROM {qualified} WHERE {json_column} = @{json_parameter} AND {key_column} != @{key_parameter}) = 0 AS '{conflict_code}';
INSERT INTO {qualified} ({key_column}, {extra_columns}{json_column}, registered_at, registered_by)
SELECT @{key_parameter}, {extra_values}@{json_parameter}, CURRENT_TIMESTAMP(), SESSION_USER()
FROM (SELECT 1)
WHERE NOT EXISTS (SELECT 1 FROM {qualified} WHERE {key_column} = @{key_parameter});
ASSERT (SELECT COUNT(*) FROM {qualified} WHERE {key_column} = @{key_parameter} AND {json_column} = @{json_parameter}) = 1 AS '{conflict_code}';
""".strip()


def v2_registration_script(
    plan: VersionedStoreMigrationPlan,
) -> tuple[str, tuple[tuple[str, str, str], ...]]:
    """Render the operator-only registration transaction and its STRING parameters.

    Identical rows are idempotent (the INSERT is guarded by NOT EXISTS), a
    conflicting row or duplicate digest refuses, and nothing here touches either
    lock or the active generation row.
    """

    pairs, policies = _registration_pairs(plan.registrations)
    parameters: list[tuple[str, str, str]] = []
    statements = ["BEGIN TRANSACTION;"]
    for pair_index, (registry, resource) in enumerate(pairs):
        # The first pair keeps its original parameter names; later pairs take a suffix.
        suffix = f"_{pair_index}" if pair_index else ""
        parameters.extend(
            (
                (f"origin_registry_sha256{suffix}", "STRING", registry.sha256),
                (f"registry_json{suffix}", "STRING", registry.canonical_json),
                (f"resource_manifest_sha256{suffix}", "STRING", resource.sha256),
                (f"resource_json{suffix}", "STRING", resource.canonical_json),
            )
        )
        statements.append(
            _registration_statements(
                "open_intelligence_execution_origin_registries_v1",
                "origin_registry_sha256",
                f"origin_registry_sha256{suffix}",
                "canonical_registry_json",
                f"registry_json{suffix}",
                "execution_origin_registry_conflict",
                extra_predicate=(
                    f" AND JSON_VALUE(@registry_json{suffix}, '$.contract_version') = "
                    "'open_intelligence_execution_origin_registry_v1'"
                ),
            )
        )
        statements.append(
            _registration_statements(
                "open_intelligence_execution_resource_manifests_v1",
                "resource_manifest_sha256",
                f"resource_manifest_sha256{suffix}",
                "canonical_resource_manifest_json",
                f"resource_json{suffix}",
                "execution_resource_manifest_conflict",
                extra_columns="origin_registry_sha256, ",
                extra_values=f"@origin_registry_sha256{suffix}, ",
                extra_predicate=(
                    f" AND JSON_VALUE(@resource_json{suffix}, '$.origin_registry_sha256') = "
                    f"@origin_registry_sha256{suffix}"
                ),
            )
        )
    for index, policy in enumerate(policies):
        parameters.append((f"policy_sha256_{index}", "STRING", policy.sha256))
        parameters.append((f"policy_json_{index}", "STRING", policy.canonical_json))
        statements.append(
            _registration_statements(
                "open_intelligence_execution_origin_policies_v1",
                "contract_sha256",
                f"policy_sha256_{index}",
                "canonical_policy_json",
                f"policy_json_{index}",
                "execution_origin_policy_conflict",
                extra_predicate=(
                    f" AND JSON_VALUE(@policy_json_{index}, '$.contract_version') = "
                    "'open_intelligence_execution_origin_policy_v2'"
                ),
            )
        )
    statements.append("COMMIT TRANSACTION;")
    return "\n".join(statements), tuple(parameters)


def _render_sql(sql: str) -> str:
    return sql.replace("{project}", PROJECT).replace("{dataset}", DATASET)


def _apply_v2_installation(plan: VersionedStoreMigrationPlan, credentials: object) -> None:
    """Create the v2 tables and routines, seed the prepared v2 lock once, register the generation."""

    try:
        from google.cloud import bigquery

        client = _bigquery_client(credentials)
        for source in (*plan.tables, *plan.routines):
            client.query(_render_sql(source.sql)).result()
        client.query(v2_lock_seed_sql()).result()
        sql, parameters = v2_registration_script(plan)
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(name, kind, value) for name, kind, value in parameters
            ]
        )
        client.query(_render_sql(sql), job_config=job_config).result()
    except MigrationRefusal:
        raise
    except Exception as exc:
        raise MigrationRefusal("execution_approval_internal_refusal") from exc


def _v2_routine_definition(item: RoutinePlan) -> str:
    if item.name == CANONICAL_JSON_UDF:
        # INFORMATION_SCHEMA returns the JavaScript body exactly as written between the
        # raw string quotes, leading and trailing newline included (native readback on
        # 13 September 2026), so the expected body keeps them too.
        match = re.search(r'AS r"""(?P<body>.*)""";\s*$', item.sql, re.DOTALL)
        if match is None:
            raise MigrationRefusal("execution_approval_schema_mismatch")
        return match.group("body")
    return _render_sql(_routine_body(item.sql))


_LOCK_FIELDS = (
    "lock_name",
    "approval_contract_version",
    "lock_version",
    "state",
    "last_approval_id",
    "created_at",
    "updated_at",
)


def _read_v1_lock_row(client: object) -> tuple[object, ...]:
    rows = tuple(
        client.query(
            f"SELECT {', '.join(_LOCK_FIELDS)} "
            f"FROM `{PROJECT}.{DATASET}.open_intelligence_execution_approval_lock_v1`"
        ).result()
    )
    if len(rows) != 1:
        raise MigrationRefusal("execution_approval_lock_invalid")
    return tuple(_row_value(rows[0], field) for field in _LOCK_FIELDS)


def _readback_v2_store(
    plan: VersionedStoreMigrationPlan,
    credentials: object,
    v1_lock_before: tuple[object, ...],
) -> None:
    """Prove the v2 install landed and that the v1 lock and routine bodies are untouched."""

    try:
        client = _bigquery_client(credentials)
        if _read_v1_lock_row(client) != v1_lock_before:
            raise MigrationRefusal("execution_approval_lock_invalid")
        for table_plan in plan.tables:
            table = client.get_table(f"{PROJECT}.{DATASET}.{table_plan.name}")
            actual_schema = tuple(
                (
                    field.name,
                    _API_TYPE_ALIASES.get(field.field_type, field.field_type),
                    field.mode,
                    field.description,
                )
                for field in table.schema
            )
            if (
                table.table_type != "TABLE"
                or table.expires is not None
                or actual_schema != _schema_from_sql(table_plan.sql)
            ):
                raise MigrationRefusal("execution_approval_schema_mismatch")
        routine_rows = tuple(
            client.query(
                f"SELECT routine_name, routine_definition "
                f"FROM `{PROJECT}.{DATASET}.INFORMATION_SCHEMA.ROUTINES`"
            ).result()
        )
        actual_routines = {
            _row_value(row, "routine_name"): _row_value(row, "routine_definition")
            for row in routine_rows
        }
        expected_v1 = {
            item.name: _render_sql(_routine_body(item.sql)) for item in build_plan().routines
        }
        expected_v2 = {item.name: _v2_routine_definition(item) for item in plan.routines}
        for name, definition in (*expected_v1.items(), *expected_v2.items()):
            if actual_routines.get(name) != definition:
                raise MigrationRefusal("execution_approval_schema_mismatch")
        lock_rows = tuple(
            client.query(
                f"SELECT lock_name, approval_contract_version, lock_version, state "
                f"FROM `{PROJECT}.{DATASET}.open_intelligence_execution_approval_lock_v2`"
            ).result()
        )
        if len(lock_rows) != 1:
            raise MigrationRefusal("execution_approval_lock_invalid")
        lock = lock_rows[0]
        if (
            _row_value(lock, "lock_name") != V2_APPROVAL_CONTRACT_VERSION
            or _row_value(lock, "approval_contract_version") != V2_APPROVAL_CONTRACT_VERSION
            or not isinstance(_row_value(lock, "lock_version"), int)
            or _row_value(lock, "lock_version") < 0
            or _row_value(lock, "state")
            not in {"prepared", "ready", "approving", "consuming", "recording_result", "disabled"}
        ):
            raise MigrationRefusal("execution_approval_lock_invalid")
        pairs, policies = _registration_pairs(plan.registrations)
        checks = (
            *(
                check
                for registry, resource in pairs
                for check in (
                    (
                        "open_intelligence_execution_origin_registries_v1",
                        "origin_registry_sha256",
                        "canonical_registry_json",
                        registry,
                    ),
                    (
                        "open_intelligence_execution_resource_manifests_v1",
                        "resource_manifest_sha256",
                        "canonical_resource_manifest_json",
                        resource,
                    ),
                )
            ),
            *(
                (
                    "open_intelligence_execution_origin_policies_v1",
                    "contract_sha256",
                    "canonical_policy_json",
                    policy,
                )
                for policy in policies
            ),
        )
        for table, key_column, json_column, item in checks:
            rows = tuple(
                client.query(
                    f"SELECT {json_column} AS canonical_json FROM `{PROJECT}.{DATASET}.{table}` "
                    f"WHERE {key_column} = '{item.sha256}'"
                ).result()
            )
            if len(rows) != 1 or _row_value(rows[0], "canonical_json") != item.canonical_json:
                raise MigrationRefusal("execution_approval_schema_mismatch")
    except MigrationRefusal:
        raise
    except Exception as exc:
        raise MigrationRefusal("execution_approval_internal_refusal") from exc


def v2_cutover_parameters(
    plan: VersionedStoreMigrationPlan,
    *,
    residual_consumption_ids: Sequence[str],
    activation_phrase: str,
) -> tuple[tuple[str, str, str], ...]:
    """Render the five STRING parameters of the cutover script from reviewed inputs."""

    ids = tuple(residual_consumption_ids)
    if any(
        not isinstance(item, str) or re.fullmatch(r"exc_[0-9a-f]{64}", item) is None for item in ids
    ) or len(set(ids)) != len(ids):
        raise MigrationRefusal("execution_approval_residual_invalid")
    residual_json = json.dumps(sorted(ids), separators=(",", ":"))
    return (
        ("origin_registry_sha256", "STRING", plan.active_pair[0]),
        ("resource_manifest_sha256", "STRING", plan.active_pair[1]),
        ("residual_consumption_ids_json", "STRING", residual_json),
        (
            "residual_set_sha256",
            "STRING",
            hashlib.sha256(residual_json.encode("utf-8")).hexdigest(),
        ),
        ("activation_phrase", "STRING", activation_phrase),
    )


def _apply_v2_cutover(
    plan: VersionedStoreMigrationPlan,
    credentials: object,
    *,
    residual_consumption_ids: Sequence[str],
    activation_phrase: str,
) -> dict[str, object]:
    """Run the prepared to ready cutover once. No CLI word invokes this yet."""

    parameters = v2_cutover_parameters(
        plan,
        residual_consumption_ids=residual_consumption_ids,
        activation_phrase=activation_phrase,
    )
    try:
        from google.cloud import bigquery

        client = _bigquery_client(credentials)
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(name, kind, value) for name, kind, value in parameters
            ]
        )
        rows = tuple(client.query(_render_sql(V2_CUTOVER_SCRIPT), job_config=job_config).result())
    except MigrationRefusal:
        raise
    except Exception as exc:
        raise MigrationRefusal("execution_approval_internal_refusal") from exc
    if len(rows) != 1:
        raise MigrationRefusal("execution_approval_lock_invalid")
    receipt = rows[0]
    return {
        field: _row_value(receipt, field)
        for field in (
            "contract_version",
            "state",
            "activated_at",
            "activated_by",
            "lock_version",
            "origin_registry_sha256",
            "resource_manifest_sha256",
        )
    }


ROTATION_PHRASE_FORMAT = (
    "I rotate the 42 staging execution generation from origin registry SHA256 %s and resource "
    "manifest SHA256 %s to origin registry SHA256 %s and resource manifest SHA256 %s. "
    "Production remains unchanged."
)
_ROTATION_DIGEST = re.compile(r"[0-9a-f]{64}")
_ROTATION_RECEIPT_FIELDS = (
    "contract_version",
    "state",
    "rotated_at",
    "rotated_by",
    "lock_version",
    "retired_origin_registry_sha256",
    "retired_resource_manifest_sha256",
    "origin_registry_sha256",
    "resource_manifest_sha256",
)


def _require_code_active_pair(activate_pair: tuple[str, ...]) -> None:
    """A rotation happens once, so it may activate only the pair this code admits.

    The pair must equal the code's active generation pair and load from the trusted
    catalogue with its reviewed registry and resource manifest bytes.
    """

    from src.analysis.open_intelligence import execution_generations

    code = "execution_rotation_activate_pair_inactive"
    if activate_pair != tuple(execution_generations.ACTIVE_GENERATION_PAIR):
        raise MigrationRefusal(code)
    try:
        execution_generations.require_active_generation(*activate_pair)
    except (execution_origins.OriginRefusal, OSError, ValueError) as exc:
        raise MigrationRefusal(code) from exc


def v2_rotation_parameters(
    *,
    retire_pair: tuple[str, str],
    activate_pair: tuple[str, str],
    expected_v2_lock_version: int,
    activation_phrase: str,
) -> tuple[tuple[str, str, str], ...]:
    """Render the six STRING parameters of the rotation script from reviewed inputs."""

    digests = (*retire_pair, *activate_pair)
    if any(
        not isinstance(item, str) or _ROTATION_DIGEST.fullmatch(item) is None for item in digests
    ):
        raise MigrationRefusal("execution_approval_generation_invalid")
    if tuple(retire_pair) == tuple(activate_pair):
        raise MigrationRefusal("execution_approval_generation_invalid")
    _require_code_active_pair(tuple(activate_pair))
    if (
        not isinstance(expected_v2_lock_version, int)
        or isinstance(expected_v2_lock_version, bool)
        or expected_v2_lock_version < 0
    ):
        raise MigrationRefusal("execution_approval_lock_invalid")
    if activation_phrase != ROTATION_PHRASE_FORMAT % digests:
        raise MigrationRefusal("execution_approval_manifest_mismatch")
    return (
        ("retire_origin_registry_sha256", "STRING", retire_pair[0]),
        ("retire_resource_manifest_sha256", "STRING", retire_pair[1]),
        ("activate_origin_registry_sha256", "STRING", activate_pair[0]),
        ("activate_resource_manifest_sha256", "STRING", activate_pair[1]),
        ("expected_v2_lock_version", "STRING", str(expected_v2_lock_version)),
        ("activation_phrase", "STRING", activation_phrase),
    )


def _apply_v2_rotation(
    credentials: object,
    *,
    retire_pair: tuple[str, str],
    activate_pair: tuple[str, str],
    expected_v2_lock_version: int,
    activation_phrase: str,
) -> dict[str, object]:
    """Run the ready to ready rotation once, through the client path the first cutover uses."""

    parameters = v2_rotation_parameters(
        retire_pair=retire_pair,
        activate_pair=activate_pair,
        expected_v2_lock_version=expected_v2_lock_version,
        activation_phrase=activation_phrase,
    )
    try:
        from google.cloud import bigquery

        client = _bigquery_client(credentials)
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(name, kind, value) for name, kind, value in parameters
            ]
        )
        rows = tuple(_submit_rotation_script(client, job_config).result())
    except MigrationRefusal:
        raise
    except Exception as exc:
        raise MigrationRefusal("execution_approval_internal_refusal") from exc
    if len(rows) != 1:
        raise MigrationRefusal("execution_approval_lock_invalid")
    receipt = rows[0]
    return {field: _row_value(receipt, field) for field in _ROTATION_RECEIPT_FIELDS}


def _submit_rotation_script(client: object, job_config: object) -> object:
    """Submit the rotation script under an id generated here, registered before the call.

    A run that ends anywhere after the insert request leaves the socket keeps the job id in its
    reservation, so the operator reads the job's state from BigQuery instead of guessing
    whether it committed. The explicit id also stops the client library resubmitting the
    script under a fresh id after an ambiguous error.
    """

    global _SUBMITTED_JOB
    job_id = "rotation-" + uuid.uuid4().hex
    _SUBMITTED_JOB = types.SimpleNamespace(
        job_id=job_id,
        location=getattr(client, "location", None),
        project=getattr(client, "project", None),
    )
    if _OUTPUT_RESERVATION is not None:
        # The id is on disk for the whole wait, so a kill that runs no finally still leaves it.
        with contextlib.suppress(OSError):
            _write_submitted_marker(_OUTPUT_RESERVATION, _SUBMITTED_JOB)
            _OUTPUT_RESERVATION.flush()
    job = client.query(_render_sql(V2_ROTATION_SCRIPT), job_config=job_config, job_id=job_id)
    _SUBMITTED_JOB = job
    return job


ROTATION_WORDS = ("rotate-v2-preflight", "rotate-v2-dry-run", "rotate-v2-apply")
ROTATION_OPTIONS = (
    "--retire-origin-registry-sha256",
    "--retire-resource-manifest-sha256",
    "--activate-origin-registry-sha256",
    "--activate-resource-manifest-sha256",
    "--expected-v2-lock-version",
    "--output",
    "--activation-phrase-file",
)
CUTOVER_WORDS = ("cutover-v2-preflight", "cutover-v2-dry-run", "cutover-v2-apply")
CUTOVER_OPTIONS = (
    "--residuals",
    "--v1-disable-receipt",
    "--inactivity-receipt",
    "--expected-v1-lock-version",
    "--output",
    "--activation-phrase-file",
)
CUTOVER_PHASES = ("pre-disable", "post-disable")
CUTOVER_PREFLIGHT_OPTIONS = (*CUTOVER_OPTIONS, "--phase")
_CUTOVER_PRE_DISABLE_REFUSAL = "execution_approval_v1_lock_not_disabled"
_CUTOVER_DML_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "MERGE")
_CUTOVER_VARIABLE = re.compile(r"\bv_[a-z0-9_]+\b")
_CUTOVER_LOCK_VERSION = re.compile(r"0|[1-9][0-9]*")


@dataclass(frozen=True)
class CutoverOptions:
    word: str
    residuals: Path
    v1_disable_receipt: Path
    inactivity_receipt: Path
    expected_v1_lock_version: int
    output: Path
    activation_phrase_file: Path
    phase: str | None


@dataclass(frozen=True)
class RotationOptions:
    word: str
    retire_origin_registry_sha256: str
    retire_resource_manifest_sha256: str
    activate_origin_registry_sha256: str
    activate_resource_manifest_sha256: str
    expected_v2_lock_version: int
    output: Path
    activation_phrase_file: Path


@dataclass(frozen=True)
class CutoverCheck:
    """One script assert rendered as a read only SELECT over the same predicate."""

    name: str
    refusal: str
    sql: str
    sql_sha256: str
    parameters: tuple[str, ...]


@dataclass(frozen=True)
class CutoverPostMutationAssert:
    """A script assert that reads the transaction's own writes. It has no read only form."""

    name: str
    refusal: str


def _parse_word_values(
    arguments: Sequence[str], options: Sequence[str], version_option: str
) -> dict[str, str] | None:
    """The shared grammar after the word: every option exactly once, no empty value."""

    if len(arguments) != 1 + 2 * len(options):
        return None
    values: dict[str, str] = {}
    for name, value in zip(arguments[1::2], arguments[2::2], strict=True):
        if name not in options or name in values or not value:
            return None
        values[name] = value
    if _CUTOVER_LOCK_VERSION.fullmatch(values[version_option]) is None:
        return None
    return values


def _parse_rotation_arguments(arguments: Sequence[str]) -> RotationOptions | None:
    """Exact grammar: one rotation word, then its seven options once each as name value pairs."""

    values = _parse_word_values(arguments, ROTATION_OPTIONS, "--expected-v2-lock-version")
    if values is None:
        return None
    return RotationOptions(
        word=arguments[0],
        retire_origin_registry_sha256=values["--retire-origin-registry-sha256"],
        retire_resource_manifest_sha256=values["--retire-resource-manifest-sha256"],
        activate_origin_registry_sha256=values["--activate-origin-registry-sha256"],
        activate_resource_manifest_sha256=values["--activate-resource-manifest-sha256"],
        expected_v2_lock_version=int(values["--expected-v2-lock-version"]),
        output=Path(values["--output"]),
        activation_phrase_file=Path(values["--activation-phrase-file"]),
    )


def _parse_cutover_arguments(
    arguments: Sequence[str],
) -> CutoverOptions | RotationOptions | None:
    """Exact grammar: one cutover or rotation word, then its options once each as name value pairs."""

    if not arguments:
        return None
    if arguments[0] in ROTATION_WORDS:
        return _parse_rotation_arguments(arguments)
    if arguments[0] not in CUTOVER_WORDS:
        return None
    word = arguments[0]
    options = CUTOVER_PREFLIGHT_OPTIONS if word == "cutover-v2-preflight" else CUTOVER_OPTIONS
    values = _parse_word_values(arguments, options, "--expected-v1-lock-version")
    if values is None:
        return None
    version = values["--expected-v1-lock-version"]
    phase = values.get("--phase")
    if word == "cutover-v2-preflight" and phase not in CUTOVER_PHASES:
        return None
    return CutoverOptions(
        word=word,
        residuals=Path(values["--residuals"]),
        v1_disable_receipt=Path(values["--v1-disable-receipt"]),
        inactivity_receipt=Path(values["--inactivity-receipt"]),
        expected_v1_lock_version=int(version),
        output=Path(values["--output"]),
        activation_phrase_file=Path(values["--activation-phrase-file"]),
        phase=phase,
    )


def _cutover_script_statements(script: str = V2_CUTOVER_SCRIPT) -> tuple[str, ...]:
    """Split a cutover shaped script on top level semicolons, leaving quoted text intact."""

    statements: list[str] = []
    current: list[str] = []
    quoted = False
    for char in script:
        if char == "'":
            quoted = not quoted
        if char == ";" and not quoted:
            statements.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    cleaned = []
    for statement in statements:
        if statement.startswith("BEGIN") and not statement.startswith("BEGIN TRANSACTION"):
            statement = statement[len("BEGIN") :].strip()
        cleaned.append(statement)
    return tuple(cleaned)


def _cutover_bindings(statements: Sequence[str]) -> dict[str, str]:
    """Map every script variable to the parameter or expression the script binds it to."""

    bindings: dict[str, str] = {}
    for statement in statements:
        declared = re.fullmatch(r"DECLARE (v_\w+) \w+ DEFAULT (@\w+)", statement)
        assigned = re.fullmatch(r"SET (v_\w+) = (.+)", statement, re.DOTALL)
        match = declared or assigned
        if match is None:
            continue
        name = match.group(1)
        if name in bindings:
            raise MigrationRefusal("execution_approval_internal_refusal")
        bindings[name] = match.group(2) if declared else f"({match.group(2)})"
    return bindings


def _bind_cutover_predicate(predicate: str, bindings: Mapping[str, str]) -> str:
    text = predicate
    for _ in range(len(bindings) + 1):
        replaced = _CUTOVER_VARIABLE.sub(
            lambda found: bindings.get(found.group(0), found.group(0)), text
        )
        if replaced == text:
            break
        text = replaced
    if _CUTOVER_VARIABLE.search(text) is not None:
        raise MigrationRefusal("execution_approval_internal_refusal")
    return text


def _cutover_asserts(script: str = V2_CUTOVER_SCRIPT) -> tuple[tuple[int, str, str, bool], ...]:
    """Return (ordinal, raw predicate, refusal code, after first write) per ASSERT in order."""

    asserts: list[tuple[int, str, str, bool]] = []
    after_dml = False
    for statement in _cutover_script_statements(script):
        keyword = statement.split(None, 1)[0] if statement else ""
        if keyword in _CUTOVER_DML_KEYWORDS:
            after_dml = True
        match = re.fullmatch(
            r"ASSERT\s+(?P<predicate>.+?)\s+AS\s+'(?P<code>[a-z0-9_]+)'", statement, re.DOTALL
        )
        if match is None:
            continue
        asserts.append((len(asserts) + 1, match.group("predicate"), match.group("code"), after_dml))
    return tuple(asserts)


def cutover_preflight_checks(
    script: str = V2_CUTOVER_SCRIPT, script_parameters: Sequence[str] = V2_CUTOVER_PARAMETERS
) -> tuple[CutoverCheck, ...]:
    """Every assert before the script's first write, each as one read only SELECT."""

    bindings = _cutover_bindings(_cutover_script_statements(script))
    checks = []
    for ordinal, predicate, code, after_dml in _cutover_asserts(script):
        if after_dml:
            continue
        sql = _render_sql(f"SELECT ({_bind_cutover_predicate(predicate, bindings)}) AS ok")
        parameters = tuple(
            name for name in script_parameters if re.search(rf"(?<!@)@{name}\b", sql)
        )
        checks.append(
            CutoverCheck(
                name=f"assert_{ordinal:02d}_{code}",
                refusal=code,
                sql=sql,
                sql_sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                parameters=parameters,
            )
        )
    return tuple(checks)


def cutover_post_mutation_asserts(
    script: str = V2_CUTOVER_SCRIPT,
) -> tuple[CutoverPostMutationAssert, ...]:
    """The asserts after the script's first write. The transaction alone can evaluate them."""

    return tuple(
        CutoverPostMutationAssert(name=f"assert_{ordinal:02d}_{code}", refusal=code)
        for ordinal, _predicate, code, after_dml in _cutover_asserts(script)
        if after_dml
    )


def rotation_preflight_checks() -> tuple[CutoverCheck, ...]:
    """Every rotation assert before its first UPDATE, each as one read only SELECT."""

    return cutover_preflight_checks(V2_ROTATION_SCRIPT, V2_ROTATION_PARAMETERS)


def rotation_post_mutation_asserts() -> tuple[CutoverPostMutationAssert, ...]:
    """The rotation asserts after its first UPDATE. The transaction alone can evaluate them."""

    return cutover_post_mutation_asserts(V2_ROTATION_SCRIPT)


def _redacted_cutover_parameters(
    parameters: Sequence[tuple[str, str, str]],
) -> dict[str, object]:
    """The rendered parameters with the residual list shown as count and digest only."""

    view: dict[str, object] = {}
    for name, _kind, value in parameters:
        if name == "residual_consumption_ids_json":
            view[name] = {
                "count": len(json.loads(value)),
                "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            }
        else:
            view[name] = value
    return view


def _cutover_script_path() -> str:
    return V2_CUTOVER_SCRIPT_PATH.relative_to(ROOT).as_posix()


def _redacted_rotation_parameters(
    parameters: Sequence[tuple[str, str, str]],
) -> dict[str, object]:
    """The rendered parameters with the activation phrase shown as its digest only."""

    view: dict[str, object] = {}
    for name, _kind, value in parameters:
        if name == "activation_phrase":
            view[name] = {"sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()}
        else:
            view[name] = value
    return view


def _rotation_script_path() -> str:
    return V2_ROTATION_SCRIPT_PATH.relative_to(ROOT).as_posix()


_ROTATION_LOCK_SQL = (
    f"SELECT {', '.join(_LOCK_FIELDS)} "
    f"FROM `{PROJECT}.{DATASET}.open_intelligence_execution_approval_lock_v2`"
)
_ROTATION_ACTIVE_GENERATION_SQL = (
    "SELECT origin_registry_sha256, resource_manifest_sha256 "
    f"FROM `{PROJECT}.{DATASET}.open_intelligence_execution_active_generation_v1`"
)
# The two tables the rotation script UPDATEs with the caller's own rights.
_ROTATION_UPDATE_TABLES = (
    "open_intelligence_execution_active_generation_v1",
    "open_intelligence_execution_approval_lock_v2",
)
_ROTATION_UPDATE_PERMISSION = "bigquery.tables.updateData"
_ROTATION_RECORD_TABLES = (
    ("approvals_v2", "open_intelligence_execution_approvals_v2"),
    ("consumptions_v2", "open_intelligence_execution_consumptions_v2"),
    ("results_v2", "open_intelligence_execution_results_v2"),
)
_ROTATION_LIVE_RECORDS_SQL = "SELECT " + ", ".join(
    f"(SELECT COUNT(*) FROM `{PROJECT}.{DATASET}.{table}` "
    "WHERE origin_registry_sha256 = @retire_origin_registry_sha256 "
    f"OR resource_manifest_sha256 = @retire_resource_manifest_sha256) AS {label}"
    for label, table in _ROTATION_RECORD_TABLES
)


def _read_rotation_lock_row(client: object) -> tuple[object, ...]:
    rows = tuple(client.query(_ROTATION_LOCK_SQL).result())
    if len(rows) != 1:
        raise MigrationRefusal("execution_approval_lock_invalid")
    return tuple(_row_value(rows[0], field) for field in _LOCK_FIELDS)


_QUERY_ERROR_REASON = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_QUERY_ERROR_MESSAGE_LIMIT = 300


def _query_error_detail(exc: BaseException, values: Mapping[str, str]) -> dict[str, str]:
    """Name why a read only query failed, without echoing what was bound into it.

    The reason is BigQuery's error reason (or the exception class) from a closed spelling. The
    message has every bound digest and the phrase replaced by the parameter name, and every
    64 hex run masked, before it is cut to printable ASCII without a backslash and bounded in
    length. The lock version is too short to replace safely (a bare "1" would rewrite the
    routine name) and is already in the receipt as expected_v2_lock_version.
    """

    errors = getattr(exc, "errors", None)
    first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
    reason = first.get("reason") if first else type(exc).__name__
    if not isinstance(reason, str) or _QUERY_ERROR_REASON.fullmatch(reason) is None:
        reason = "unknown"
    message = getattr(exc, "message", None)
    if not isinstance(message, str):
        message = first.get("message") if isinstance(first.get("message"), str) else str(exc)
    for name, value in sorted(values.items(), key=lambda item: -len(item[1])):
        if len(value) >= 16:
            message = message.replace(value, "@" + name)
    message = re.sub(r"[0-9A-Fa-f]{64}", "<sha256>", message)
    message = "".join(
        " " if char.isspace() else char if " " <= char <= "~" and char != "\\" else "?"
        for char in message
    )
    return {"error_reason": reason, "error_message": message[:_QUERY_ERROR_MESSAGE_LIMIT]}


def _rotation_update_guards(client: object, values: Mapping[str, str]) -> list[dict[str, object]]:
    """Ask BigQuery, read only, whether the caller may update each table the script UPDATEs.

    The script's two UPDATEs run with the caller's own rights, which no read only SELECT
    proves. tables.testIamPermissions answers for the credentials this preflight runs
    with, so it must run as the approver who will apply. A missing permission, an answer
    of any other shape or a failed call fails the guard, and the refusal names the table.
    """

    guards: list[dict[str, object]] = []
    for table in _ROTATION_UPDATE_TABLES:
        guard: dict[str, object] = {
            "name": f"update_data_{table}",
            "parameters": [],
            "permission": _ROTATION_UPDATE_PERMISSION,
            "refusal": f"execution_rotation_update_data_missing_{table}",
            "table": table,
        }
        granted = None
        try:
            response = client.test_iam_permissions(
                f"{PROJECT}.{DATASET}.{table}", [_ROTATION_UPDATE_PERMISSION]
            )
            if isinstance(response, Mapping):
                granted = response.get("permissions")
        except Exception as exc:
            guard["error"] = "query_failed"
            guard.update(_query_error_detail(exc, values))
        guard["result"] = (
            "pass"
            if isinstance(granted, list) and _ROTATION_UPDATE_PERMISSION in granted
            else "fail"
        )
        guards.append(guard)
    return guards


def _run_rotation_preflight(
    credentials: object,
    *,
    retire_pair: tuple[str, str],
    activate_pair: tuple[str, str],
    expected_v2_lock_version: int,
    activation_phrase: str,
) -> dict[str, object]:
    """Evaluate every pre-write assert read only, then observe the lock, the active row and the counts."""

    parameters = v2_rotation_parameters(
        retire_pair=retire_pair,
        activate_pair=activate_pair,
        expected_v2_lock_version=expected_v2_lock_version,
        activation_phrase=activation_phrase,
    )
    values = {name: value for name, _kind, value in parameters}
    checks = rotation_preflight_checks()
    try:
        from google.cloud import bigquery

        client = _bigquery_client(credentials)
        rows = tuple(client.query("SELECT SESSION_USER() AS session_user").result())
    except MigrationRefusal:
        raise
    except Exception as exc:
        raise MigrationRefusal("execution_approval_internal_refusal") from exc
    session_user = _row_value(rows[0], "session_user") if len(rows) == 1 else None
    if not isinstance(session_user, str) or not session_user:
        raise MigrationRefusal("execution_approval_identity_invalid")
    results: list[dict[str, object]] = []
    for check in checks:
        record: dict[str, object] = {
            "name": check.name,
            "parameters": list(check.parameters),
            "refusal": check.refusal,
            "sql_sha256": check.sql_sha256,
        }
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(name, "STRING", values[name])
                for name in check.parameters
            ]
        )
        try:
            rows = tuple(client.query(check.sql, job_config=job_config).result())
            passed = len(rows) == 1 and _row_value(rows[0], "ok") is True
        except Exception as exc:
            passed = False
            record["error"] = "query_failed"
            record.update(_query_error_detail(exc, values))
        record["result"] = "pass" if passed else "fail"
        results.append(record)
    lock_guard: dict[str, object] = {
        "name": "v2_lock_expected",
        "parameters": [],
        "refusal": "execution_rotation_lock_not_ready",
    }
    observed_lock: dict[str, object] | None = None
    try:
        lock = _read_rotation_lock_row(client)
        observed_lock = {"lock_version": lock[2], "state": lock[3]}
    except MigrationRefusal:
        observed_lock = None
    except Exception as exc:
        lock_guard["error"] = "query_failed"
        lock_guard.update(_query_error_detail(exc, values))
    lock_version = None if observed_lock is None else observed_lock["lock_version"]
    lock_guard["result"] = (
        "pass"
        if observed_lock is not None
        and isinstance(lock_version, int)
        and not isinstance(lock_version, bool)
        and lock_version == expected_v2_lock_version
        and lock[0] == "open_intelligence_execution_approval_v2"
        and lock[1] == "open_intelligence_execution_approval_v2"
        and observed_lock["state"] == "ready"
        else "fail"
    )
    results.append(lock_guard)
    active_guard: dict[str, object] = {
        "name": "active_generation_expected",
        "parameters": [],
        "refusal": "execution_rotation_active_generation_mismatch",
    }
    observed_active: list[dict[str, object]] | None = None
    try:
        observed_active = [
            {
                "origin_registry_sha256": _row_value(row, "origin_registry_sha256"),
                "resource_manifest_sha256": _row_value(row, "resource_manifest_sha256"),
            }
            for row in client.query(_ROTATION_ACTIVE_GENERATION_SQL).result()
        ]
    except Exception as exc:
        active_guard["error"] = "query_failed"
        active_guard.update(_query_error_detail(exc, values))
    active_guard["result"] = (
        "pass"
        if observed_active is not None
        and len(observed_active) == 1
        and observed_active[0]["origin_registry_sha256"] == retire_pair[0]
        and observed_active[0]["resource_manifest_sha256"] == retire_pair[1]
        else "fail"
    )
    results.append(active_guard)
    records_guard: dict[str, object] = {
        "name": "live_records_expected",
        "parameters": ["retire_origin_registry_sha256", "retire_resource_manifest_sha256"],
        "refusal": "execution_rotation_live_records",
    }
    observed_records: dict[str, object] | None = None
    try:
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(name, "STRING", values[name])
                for name in records_guard["parameters"]
            ]
        )
        rows = tuple(client.query(_ROTATION_LIVE_RECORDS_SQL, job_config=job_config).result())
        if len(rows) == 1:
            observed_records = {
                label: _row_value(rows[0], label) for label, _table in _ROTATION_RECORD_TABLES
            }
    except Exception as exc:
        records_guard["error"] = "query_failed"
        records_guard.update(_query_error_detail(exc, values))
    records_guard["result"] = (
        "pass"
        if observed_records is not None
        and all(
            isinstance(count, int) and not isinstance(count, bool) and count == 0
            for count in observed_records.values()
        )
        else "fail"
    )
    results.append(records_guard)
    results.extend(_rotation_update_guards(client, values))
    failed = [item for item in results if item["result"] == "fail"]
    return {
        "checks": results,
        "contract_version": "execution_rotation_preflight_v1",
        "expected_v2_lock": {"lock_version": expected_v2_lock_version, "state": "ready"},
        "expected_v2_lock_version": expected_v2_lock_version,
        "failed_checks": [item["name"] for item in failed],
        "observed_active_generation": observed_active,
        "observed_live_records": observed_records,
        "observed_v2_lock": observed_lock,
        "parameters": _redacted_rotation_parameters(parameters),
        "post_mutation_asserts": [
            {"evaluated": False, "name": item.name, "refusal": item.refusal}
            for item in rotation_post_mutation_asserts()
        ],
        "refusal": str(failed[0]["refusal"]) if failed else None,
        "script_path": _rotation_script_path(),
        "script_sha256": _sha256(V2_ROTATION_SCRIPT_PATH),
        "session_user": session_user,
        "state": "refused" if failed else "ready",
    }


def _run_rotation_apply(
    credentials: object,
    *,
    retire_pair: tuple[str, str],
    activate_pair: tuple[str, str],
    expected_v2_lock_version: int,
    activation_phrase: str,
) -> dict[str, object]:
    """Run the rotation only behind a ready preflight produced in this process and this run."""

    preflight = _run_rotation_preflight(
        credentials,
        retire_pair=retire_pair,
        activate_pair=activate_pair,
        expected_v2_lock_version=expected_v2_lock_version,
        activation_phrase=activation_phrase,
    )
    receipt: dict[str, object] = {
        "contract_version": "execution_rotation_apply_v1",
        "expected_v2_lock_version": expected_v2_lock_version,
        "parameters": preflight["parameters"],
        "preflight": preflight,
        "refusal": preflight["refusal"],
        "rotation": None,
        "script_sha256": preflight["script_sha256"],
        "state": "refused",
    }
    if preflight["state"] != "ready":
        receipt["refusal"] = preflight.get("refusal") or "execution_rotation_lock_not_ready"
        return receipt
    try:
        rotation = _apply_v2_rotation(
            credentials,
            retire_pair=retire_pair,
            activate_pair=activate_pair,
            expected_v2_lock_version=expected_v2_lock_version,
            activation_phrase=activation_phrase,
        )
    except MigrationRefusal as exc:
        if _SUBMITTED_JOB is not None:
            # The transaction may have committed before the refusal; the marker records the job.
            raise
        receipt["refusal"] = str(exc)
        return receipt
    observed = {
        key: _format_server_time(value) if isinstance(value, datetime) else value
        for key, value in rotation.items()
    }
    mismatched = _rotation_receipt_mismatch(
        observed,
        retire_pair=retire_pair,
        activate_pair=activate_pair,
        expected_v2_lock_version=expected_v2_lock_version,
    )
    if mismatched is not None:
        # The script job ran and may have committed, so this is no refusal without a write:
        # the receipt keeps the job id and the row as observed for the operator to read back.
        receipt["job"] = _submitted_identity(_SUBMITTED_JOB)
        receipt["mismatched_field"] = mismatched
        receipt["observed_rotation"] = observed
        receipt["refusal"] = _ROTATION_UNVERIFIED
        receipt["state"] = "unverified"
        return receipt
    receipt["rotation"] = observed
    receipt["refusal"] = None
    receipt["state"] = "rotated"
    return receipt


_ROTATION_UNVERIFIED = "execution_rotation_receipt_unverified"


def _rotation_receipt_mismatch(
    rotation: Mapping[str, object],
    *,
    retire_pair: tuple[str, str],
    activate_pair: tuple[str, str],
    expected_v2_lock_version: int,
) -> str | None:
    """Name the first receipt field that disagrees with the request, or None.

    Every expected value comes from the operator's reviewed inputs, never from the row: the
    contract version and lock state the script writes, the pair it moved from and to, and the
    lock version one past the version the preflight required.
    """

    expected = (
        ("contract_version", "open_intelligence_execution_rotation_receipt_v2"),
        ("state", "ready"),
        ("lock_version", expected_v2_lock_version + 1),
        ("origin_registry_sha256", activate_pair[0]),
        ("resource_manifest_sha256", activate_pair[1]),
        ("retired_origin_registry_sha256", retire_pair[0]),
        ("retired_resource_manifest_sha256", retire_pair[1]),
    )
    for field, value in expected:
        # An exact type check, so a bool or a text lock version never equals an int.
        observed = rotation.get(field)
        if type(observed) is not type(value) or observed != value:
            return field
    return None


def _rotation_receipt(options: RotationOptions) -> dict[str, object]:
    activation_phrase = _load_activation_phrase(options.activation_phrase_file)
    retire_pair = (options.retire_origin_registry_sha256, options.retire_resource_manifest_sha256)
    activate_pair = (
        options.activate_origin_registry_sha256,
        options.activate_resource_manifest_sha256,
    )
    parameters = v2_rotation_parameters(
        retire_pair=retire_pair,
        activate_pair=activate_pair,
        expected_v2_lock_version=options.expected_v2_lock_version,
        activation_phrase=activation_phrase,
    )
    if options.word == "rotate-v2-dry-run":
        return {
            "contract_version": "execution_rotation_dry_run_v1",
            "expected_v2_lock_version": options.expected_v2_lock_version,
            "parameters": _redacted_rotation_parameters(parameters),
            "script_path": _rotation_script_path(),
            "script_sha256": _sha256(V2_ROTATION_SCRIPT_PATH),
        }
    credentials = _load_credentials()
    run = _run_rotation_preflight if options.word == "rotate-v2-preflight" else _run_rotation_apply
    return run(
        credentials,
        retire_pair=retire_pair,
        activate_pair=activate_pair,
        expected_v2_lock_version=options.expected_v2_lock_version,
        activation_phrase=activation_phrase,
    )


def _run_cutover_preflight(
    plan: VersionedStoreMigrationPlan,
    credentials: object,
    *,
    residual_consumption_ids: Sequence[str],
    activation_phrase: str,
    expected_v1_lock_version: int,
    phase: str,
) -> dict[str, object]:
    """Evaluate every pre-write assert of the script read only, then the v1 lock guard by phase."""

    if phase not in CUTOVER_PHASES:
        raise MigrationRefusal("execution_approval_manifest_invalid")
    expected_state = "ready" if phase == "pre-disable" else "disabled"
    parameters = v2_cutover_parameters(
        plan,
        residual_consumption_ids=residual_consumption_ids,
        activation_phrase=activation_phrase,
    )
    values = {name: value for name, _kind, value in parameters}
    checks = cutover_preflight_checks()
    try:
        from google.cloud import bigquery

        client = _bigquery_client(credentials)
        rows = tuple(client.query("SELECT SESSION_USER() AS session_user").result())
    except MigrationRefusal:
        raise
    except Exception as exc:
        raise MigrationRefusal("execution_approval_internal_refusal") from exc
    session_user = _row_value(rows[0], "session_user") if len(rows) == 1 else None
    if not isinstance(session_user, str) or not session_user:
        raise MigrationRefusal("execution_approval_identity_invalid")
    results: list[dict[str, object]] = []
    for check in checks:
        record: dict[str, object] = {
            "name": check.name,
            "parameters": list(check.parameters),
            "refusal": check.refusal,
            "sql_sha256": check.sql_sha256,
        }
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(name, "STRING", values[name])
                for name in check.parameters
            ]
        )
        try:
            rows = tuple(client.query(check.sql, job_config=job_config).result())
            passed = len(rows) == 1 and _row_value(rows[0], "ok") is True
        except Exception:
            passed = False
            record["error"] = "query_failed"
        record["result"] = "pass" if passed else "fail"
        results.append(record)
    observed: dict[str, object] | None = None
    guard: dict[str, object] = {
        "name": "v1_lock_expected",
        "parameters": [],
        "refusal": "execution_approval_lock_invalid",
    }
    try:
        lock = _read_v1_lock_row(client)
        observed = {"lock_version": lock[2], "state": lock[3]}
    except MigrationRefusal:
        observed = None
    except Exception:
        guard["error"] = "query_failed"
    lock_version = None if observed is None else observed["lock_version"]
    guard["result"] = (
        "pass"
        if isinstance(lock_version, int)
        and not isinstance(lock_version, bool)
        and lock_version == expected_v1_lock_version
        and observed is not None
        and observed["state"] == expected_state
        else "fail"
    )
    results.append(guard)
    failed = [item for item in results if item["result"] == "fail"]
    state, refusal = _cutover_preflight_state(phase, checks, failed)
    return {
        "checks": results,
        "contract_version": "execution_cutover_preflight_v1",
        "expected_v1_lock": {"lock_version": expected_v1_lock_version, "state": expected_state},
        "expected_v1_lock_version": expected_v1_lock_version,
        "failed_checks": [item["name"] for item in failed],
        "observed_v1_lock": observed,
        "parameters": _redacted_cutover_parameters(parameters),
        "post_mutation_asserts": [
            {"evaluated": False, "name": item.name, "refusal": item.refusal}
            for item in cutover_post_mutation_asserts()
        ],
        "phase": phase,
        "refusal": refusal,
        "script_path": _cutover_script_path(),
        "script_sha256": _sha256(V2_CUTOVER_SCRIPT_PATH),
        "session_user": session_user,
        "state": state,
    }


def _cutover_preflight_state(
    phase: str, checks: Sequence[CutoverCheck], failed: Sequence[Mapping[str, object]]
) -> tuple[str, str | None]:
    """Pre-disable allows exactly the v1 not disabled assert to fail; post-disable allows none."""

    if phase == "post-disable":
        return ("refused", str(failed[0]["refusal"])) if failed else ("ready", None)
    allowed = [check.name for check in checks if check.refusal == _CUTOVER_PRE_DISABLE_REFUSAL]
    unexpected = [item for item in failed if item["name"] not in allowed]
    if len(allowed) == 1 and not unexpected and [item["name"] for item in failed] == allowed:
        return ("ready_for_disable", None)
    if unexpected:
        return ("refused", str(unexpected[0]["refusal"]))
    return ("refused", "execution_approval_lock_invalid")


def _load_cutover_json_object(path: Path, code: str) -> dict[str, object]:
    def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise MigrationRefusal(code)
            result[key] = value
        return result

    try:
        payload = json.loads(
            path.read_bytes().decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(MigrationRefusal(code)),
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise MigrationRefusal(code) from exc
    if not isinstance(payload, dict):
        raise MigrationRefusal(code)
    return payload


def _load_cutover_residuals(path: Path) -> tuple[str, ...]:
    payload = _load_cutover_json_object(path, "execution_approval_residual_invalid")
    residuals = payload.get("residuals")
    if not isinstance(residuals, list):
        raise MigrationRefusal("execution_approval_residual_invalid")
    ids: list[str] = []
    for entry in residuals:
        if not isinstance(entry, dict) or not isinstance(entry.get("consumption_id"), str):
            raise MigrationRefusal("execution_approval_residual_invalid")
        ids.append(entry["consumption_id"])
    return tuple(ids)


def _load_activation_phrase(path: Path) -> str:
    try:
        phrase = path.read_bytes().decode("utf-8").rstrip("\r\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise MigrationRefusal("execution_approval_manifest_mismatch") from exc
    if not phrase or "\n" in phrase or "\r" in phrase:
        raise MigrationRefusal("execution_approval_manifest_mismatch")
    return phrase


def _load_cutover_receipt(path: Path, *, expected_lock_version: int | None) -> dict[str, object]:
    """Summarise an operator receipt file: contract version, digest and the guarded lock version."""

    payload = _load_cutover_json_object(path, "execution_approval_manifest_invalid")
    version = payload.get("contract_version")
    if not isinstance(version, str) or not version:
        raise MigrationRefusal("execution_approval_manifest_invalid")
    summary: dict[str, object] = {"contract_version": version, "sha256": _sha256(path)}
    if expected_lock_version is not None:
        lock_version = payload.get("lock_version")
        if (
            not isinstance(lock_version, int)
            or isinstance(lock_version, bool)
            or lock_version != expected_lock_version
        ):
            raise MigrationRefusal("execution_approval_lock_invalid")
        summary["lock_version"] = lock_version
    return summary


def _run_cutover_apply(
    plan: VersionedStoreMigrationPlan,
    credentials: object,
    *,
    residual_consumption_ids: Sequence[str],
    activation_phrase: str,
    expected_v1_lock_version: int,
    v1_disable_receipt: Mapping[str, object],
    inactivity_receipt: Mapping[str, object],
) -> dict[str, object]:
    """Run the cutover only behind a ready preflight produced in this process and this run."""

    preflight = _run_cutover_preflight(
        plan,
        credentials,
        residual_consumption_ids=residual_consumption_ids,
        activation_phrase=activation_phrase,
        expected_v1_lock_version=expected_v1_lock_version,
        phase="post-disable",
    )
    receipt: dict[str, object] = {
        "activation": None,
        "contract_version": "execution_cutover_apply_v1",
        "expected_v1_lock_version": expected_v1_lock_version,
        "inactivity_receipt": dict(inactivity_receipt),
        "parameters": preflight["parameters"],
        "preflight": preflight,
        "refusal": preflight["refusal"],
        "script_sha256": preflight["script_sha256"],
        "state": "refused",
        "v1_disable_receipt": dict(v1_disable_receipt),
    }
    if preflight.get("phase") != "post-disable" or preflight["state"] != "ready":
        receipt["refusal"] = preflight.get("refusal") or "execution_approval_lock_invalid"
        return receipt
    try:
        activation = _apply_v2_cutover(
            plan,
            credentials,
            residual_consumption_ids=residual_consumption_ids,
            activation_phrase=activation_phrase,
        )
    except MigrationRefusal as exc:
        receipt["refusal"] = str(exc)
        return receipt
    receipt["activation"] = {
        key: _format_server_time(value) if isinstance(value, datetime) else value
        for key, value in activation.items()
    }
    receipt["refusal"] = None
    receipt["state"] = "activated"
    return receipt


def _cutover_receipt(options: CutoverOptions) -> dict[str, object]:
    residual_ids = _load_cutover_residuals(options.residuals)
    activation_phrase = _load_activation_phrase(options.activation_phrase_file)
    if options.word == "cutover-v2-apply":
        disable = _load_cutover_receipt(
            options.v1_disable_receipt, expected_lock_version=options.expected_v1_lock_version
        )
        inactivity = _load_cutover_receipt(options.inactivity_receipt, expected_lock_version=None)
    plan = build_v2_plan()
    if options.word == "cutover-v2-dry-run":
        parameters = v2_cutover_parameters(
            plan, residual_consumption_ids=residual_ids, activation_phrase=activation_phrase
        )
        return {
            "contract_version": "execution_cutover_dry_run_v1",
            "expected_v1_lock_version": options.expected_v1_lock_version,
            "parameters": _redacted_cutover_parameters(parameters),
            "script_path": _cutover_script_path(),
            "script_sha256": _sha256(V2_CUTOVER_SCRIPT_PATH),
        }
    credentials = _load_credentials()
    if options.word == "cutover-v2-preflight":
        return _run_cutover_preflight(
            plan,
            credentials,
            residual_consumption_ids=residual_ids,
            activation_phrase=activation_phrase,
            expected_v1_lock_version=options.expected_v1_lock_version,
            phase=options.phase,
        )
    return _run_cutover_apply(
        plan,
        credentials,
        residual_consumption_ids=residual_ids,
        activation_phrase=activation_phrase,
        expected_v1_lock_version=options.expected_v1_lock_version,
        v1_disable_receipt=disable,
        inactivity_receipt=inactivity,
    )


def _discard_output_reservation(stream: object, path: Path) -> None:
    """Drop the empty file reserved for a receipt that was never produced.

    A handle that fails to close is reported and the removal still runs. A reservation that
    cannot be removed stays put and is reported, because the next run refuses it as
    output_exists.
    """

    try:
        stream.close()
    except OSError:
        _emit({"error": "output_reservation_unclosed"}, error=True)
    try:
        path.unlink()
    except OSError:
        _emit({"error": "output_reservation_retained"}, error=True)


def _submitted_identity(job: object) -> dict[str, object]:
    return {
        "job_id": getattr(job, "job_id", None),
        "location": getattr(job, "location", None),
        "project": getattr(job, "project", None),
    }


def _submitted_marker_line(job: object) -> str:
    marker = {
        "contract_version": "execution_rotation_submitted_v1",
        "state": "submitted",
        **_submitted_identity(job),
    }
    return json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_submitted_marker(stream: object, job: object) -> None:
    """Make the reservation hold exactly the marker line, whatever it held before."""

    stream.seek(0)
    stream.truncate()
    stream.write((_submitted_marker_line(job) + "\n").encode("utf-8"))


def _retain_output_reservation(stream: object, job: object) -> None:
    """Keep the reservation for a run that ended before reading its submitted job's result.

    Stderr names the job before any write is attempted, so the id survives a disk and a
    stdout failing together. The marker names the job so the operator can read its state
    from BigQuery; a marker that cannot land on disk reaches stdout when stdout is alive, and
    the retained file refuses the next run as output_exists either way.
    """

    _emit({"error": "execution_rotation_result_unread", **_submitted_identity(job)}, error=True)
    try:
        with stream:
            _write_submitted_marker(stream, job)
    except OSError:
        # Stderr already carries the id; a dead stdout must not replace the original exit.
        with contextlib.suppress(OSError):
            _write_exact_line(sys.stdout, _submitted_marker_line(job))


def _refuse_existing_output(path: Path) -> None:
    """Refuse an output path that is taken, naming the job first when it holds a marker."""

    try:
        with open(path, "rb") as handle:
            payload = json.loads(handle.readline().decode("utf-8"))
    except (OSError, ValueError):
        payload = None
    job = None
    if isinstance(payload, dict):
        if payload.get("contract_version") == "execution_rotation_submitted_v1":
            job = payload
        elif (
            payload.get("contract_version") == "execution_rotation_apply_v1"
            and payload.get("state") == "unverified"
            and isinstance(payload.get("job"), dict)
        ):
            job = payload["job"]
    if job is not None:
        identity = {key: job.get(key) for key in ("job_id", "location", "project")}
        _emit({"error": "execution_rotation_result_unread", **identity}, error=True)
    _emit({"error": "output_exists"}, error=True)


def _run_cutover_word(options: CutoverOptions | RotationOptions) -> int:
    """Reserve the output file before any client call, fill it before stdout, exit by state."""

    global _SUBMITTED_JOB, _OUTPUT_RESERVATION
    _SUBMITTED_JOB = None
    _OUTPUT_RESERVATION = None
    if options.output.exists():
        _refuse_existing_output(options.output)
        return 1
    try:
        # The handle is held open across the client call: it is the proof the receipt can land.
        stream = open(options.output, "xb")  # noqa: SIM115
    except FileExistsError:
        _refuse_existing_output(options.output)
        return 1
    except OSError:
        _emit({"error": "output_unwritable"}, error=True)
        return 1
    _OUTPUT_RESERVATION = stream
    build = _rotation_receipt if isinstance(options, RotationOptions) else _cutover_receipt
    built = False
    try:
        receipt = build(options)
        built = True
    except MigrationRefusal as exc:
        refusal = str(exc)
    except Exception:
        refusal = "execution_approval_internal_refusal"
    finally:
        # Every exit without a receipt, an interrupt included, gives the reservation back,
        # unless the script job was submitted: then the reservation keeps the job id.
        if not built and _SUBMITTED_JOB is not None:
            _retain_output_reservation(stream, _SUBMITTED_JOB)
        elif not built:
            _discard_output_reservation(stream, options.output)
    if not built:
        _emit({"error": refusal}, error=True)
        return 1
    line = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        with stream:
            stream.seek(0)
            stream.truncate()
            stream.write((line + "\n").encode("utf-8"))
    except OSError:
        # The receipt may record a committed apply, so it reaches stdout before the refusal.
        _write_exact_line(sys.stdout, line)
        # An apply keeps its reservation as the alarm; a read only word gives it back.
        if not options.word.endswith("-apply"):
            _discard_output_reservation(stream, options.output)
        _emit({"error": "execution_approval_internal_refusal"}, error=True)
        return 1
    _write_exact_line(sys.stdout, line)
    if receipt.get("state") == "unverified":
        _emit(
            {
                "error": str(receipt["refusal"]),
                "mismatched_field": receipt["mismatched_field"],
                **receipt["job"],
            },
            error=True,
        )
        return 1
    if receipt.get("state") == "refused":
        _emit({"error": str(receipt["refusal"])}, error=True)
        return 1
    return 0


# Routine IAM for the v2 store. The routines are created with an empty routine IAM
# policy and no dataset authorization, and CREATE OR REPLACE drops routine level IAM
# again, so the v2 words grant the approved provisioning delta's routine
# authorizations and routine level bindings here. The delta is the authority: the
# code derived map (_build_v2_iam_plan) is only compared with it and every
# difference is reported. Writes are add only, etag guarded and read back in full;
# nothing is ever removed and project level grants are reported, never made.
# ops/deploy/iam_delta_v1.json as approved in amendment d (approved 2026-09-20). That file
# now carries amendment e, so these words read the approved amendment d bytes retained
# beside the amendment d manifest; the amendment e rows have their own words below.
V2_IAM_DELTA_PATH = ROOT / "configs" / "open_intelligence" / "iam_delta_amendment_d.json"
V2_IAM_DELTA_SHA256 = "1d2e8ead5a7c17c51b53ca640ed4c295294aef204ce4abeee9f03c7e822d5e3e"
_V2_IAM_WORDS = {"iam-v2-plan": "plan", "iam-v2-dry-run": "dry_run", "iam-v2-apply": "apply"}
_GOOGLE_BIGQUERY = "//bigquery.googleapis.com/"
_V2_IAM_DATASET = f"{_GOOGLE_BIGQUERY}projects/{PROJECT}/datasets/{DATASET}"
_V2_IAM_PROJECT = f"//cloudresourcemanager.googleapis.com/projects/{PROJECT}"
_V2_IAM_ROUTINE_BINDING_ROLE = "roles/bigquery.dataViewer"
_V2_IAM_AUTHORIZATION_ROLES = frozenset(
    {"roles/bigquery.routineDataViewer", "roles/bigquery.routineDataEditor"}
)
_V2_IAM_SERVICE_SUFFIX = f"@{PROJECT}.iam.gserviceaccount.com"


@dataclass(frozen=True)
class V2IamTargets:
    delta_sha256: str
    authorizations: tuple[RoutineAuthorization, ...]
    bindings: tuple[IamBinding, ...]
    project_bindings: tuple[IamBinding, ...]
    disagreement: dict[str, object]


def _v2_iam_delta_refusal() -> MigrationRefusal:
    return MigrationRefusal("execution_approval_v2_iam_delta_invalid")


def _load_v2_iam_delta() -> tuple[Mapping[str, object], str]:
    try:
        raw = V2_IAM_DELTA_PATH.read_bytes()
    except OSError as exc:
        raise _v2_iam_delta_refusal() from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != V2_IAM_DELTA_SHA256:
        raise _v2_iam_delta_refusal()
    try:
        delta = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, ValueError, MigrationRefusal) as exc:
        raise _v2_iam_delta_refusal() from exc
    approval = delta.get("approval") if isinstance(delta, dict) else None
    if (
        not isinstance(approval, dict)
        or approval.get("state") != "approved"
        or delta.get("project") != PROJECT
        or not isinstance(delta.get("routine_authorizations"), list)
        or not isinstance(delta.get("bindings"), list)
    ):
        raise _v2_iam_delta_refusal()
    return delta, digest


def _v2_iam_service_identities(delta: Mapping[str, object]) -> frozenset[str]:
    identities = set()
    for group in ("create", "existing"):
        rows = delta.get(group)
        if not isinstance(rows, list):
            raise _v2_iam_delta_refusal()
        for row in rows:
            resource = row.get("resource") if isinstance(row, dict) else None
            if not isinstance(resource, str):
                raise _v2_iam_delta_refusal()
            if "/serviceAccounts/" in resource:
                email = resource.rsplit("/", 1)[1]
                if email.endswith(_V2_IAM_SERVICE_SUFFIX):
                    identities.add(_service_principal(email))
    return frozenset(identities)


def _v2_iam_routine_name(resource: object, routine_names: frozenset[str]) -> str:
    prefix = _V2_IAM_DATASET + "/routines/"
    if not isinstance(resource, str) or not resource.startswith(prefix):
        raise _v2_iam_delta_refusal()
    name = resource.removeprefix(prefix)
    # The name reaches a URL only after it matches a routine this plan installs.
    if name not in routine_names:
        raise _v2_iam_delta_refusal()
    return name


def _v2_iam_row_keys(row: object, keys: set[str]) -> Mapping[str, object]:
    if not isinstance(row, dict) or set(row) != keys:
        raise _v2_iam_delta_refusal()
    return row


def _v2_iam_guard_member(member: object, identities: frozenset[str]) -> str:
    if not isinstance(member, str) or member not in identities:
        raise MigrationRefusal("execution_approval_v2_iam_member_refused")
    if not member.startswith("serviceAccount:") or not member.endswith(_V2_IAM_SERVICE_SUFFIX):
        raise MigrationRefusal("execution_approval_v2_iam_member_refused")
    return member


def v2_iam_targets(plan: VersionedStoreMigrationPlan) -> V2IamTargets:
    """Return the delta's v2 routine authorizations and bindings, checked against the plan."""

    delta, digest = _load_v2_iam_delta()
    routine_names = frozenset(item.name for item in plan.routines)
    identities = _v2_iam_service_identities(delta)
    authorizations: list[RoutineAuthorization] = []
    for row in delta["routine_authorizations"]:
        row = _v2_iam_row_keys(row, {"dataset", "role", "routine"})
        if row["dataset"] != _V2_IAM_DATASET or row["role"] not in _V2_IAM_AUTHORIZATION_ROLES:
            raise _v2_iam_delta_refusal()
        name = _v2_iam_routine_name(row["routine"], routine_names)
        authorizations.append(
            RoutineAuthorization(_routine_resource(name), f"{PROJECT}.{DATASET}", str(row["role"]))
        )
    bindings: list[IamBinding] = []
    project_bindings: list[IamBinding] = []
    for row in delta["bindings"]:
        row = _v2_iam_row_keys(row, {"condition", "member", "purpose", "resource", "role"})
        resource = row["resource"]
        if resource == _V2_IAM_PROJECT:
            project_bindings.append(
                _binding(str(row["member"]), _project_resource(), str(row["role"]), "report only")
            )
            continue
        if not isinstance(resource, str) or not resource.startswith(_V2_IAM_DATASET + "/"):
            continue
        name = _v2_iam_routine_name(resource, routine_names)
        member = _v2_iam_guard_member(row["member"], identities)
        if row["role"] != _V2_IAM_ROUTINE_BINDING_ROLE or row["condition"] is not None:
            raise _v2_iam_delta_refusal()
        bindings.append(
            _binding(
                member, _routine_resource(name), _V2_IAM_ROUTINE_BINDING_ROLE, str(row["purpose"])
            )
        )
    auth_keys = [(item.routine, item.role) for item in authorizations]
    binding_keys = [(item.principal, item.resource, item.role) for item in bindings]
    if len(set(auth_keys)) != len(auth_keys) or len(set(binding_keys)) != len(binding_keys):
        raise _v2_iam_delta_refusal()
    if len({item.routine for item in authorizations}) != len(authorizations):
        raise _v2_iam_delta_refusal()
    return V2IamTargets(
        delta_sha256=digest,
        authorizations=tuple(authorizations),
        bindings=tuple(bindings),
        project_bindings=tuple(project_bindings),
        disagreement=_v2_iam_disagreement(plan, authorizations, bindings),
    )


def _v2_iam_disagreement(
    plan: VersionedStoreMigrationPlan,
    authorizations: Sequence[RoutineAuthorization],
    bindings: Sequence[IamBinding],
) -> dict[str, object]:
    def auth_rows(keys):
        return [{"routine": r.rsplit("/", 1)[1], "role": role} for r, role in sorted(keys)]

    def binding_rows(keys):
        return [
            {"member": m, "routine": r.rsplit("/", 1)[1], "role": role}
            for m, r, role in sorted(keys)
        ]

    delta_auth = {(item.routine, item.role) for item in authorizations}
    plan_auth = {(item.routine, item.role) for item in plan.iam_plan.routine_authorizations}
    delta_bind = {(item.principal, item.resource, item.role) for item in bindings}
    plan_bind = {
        (item.principal, item.resource, item.role)
        for item in plan.iam_plan.principal_routine_bindings
    }
    return {
        "authorizations_plan_only": auth_rows(plan_auth - delta_auth),
        "authorizations_delta_only": auth_rows(delta_auth - plan_auth),
        "routine_bindings_plan_only": binding_rows(plan_bind - delta_bind),
        "routine_bindings_delta_only": binding_rows(delta_bind - plan_bind),
    }


class _V2IamAdapter:
    """Etag guarded reads and writes of the approvals dataset and its routine policies."""

    def __init__(self, session: object):
        self._session = session

    @staticmethod
    def _dataset_url() -> str:
        return f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/datasets/{DATASET}"

    @staticmethod
    def _routine_get_url(resource: str) -> str:
        prefix = f"projects/{PROJECT}/datasets/{DATASET}/routines/"
        name = resource.removeprefix(prefix)
        if not resource.startswith(prefix) or not re.fullmatch(r"[a-z0-9_]+", name):
            raise MigrationRefusal("execution_approval_target_invalid")
        return f"https://bigquery.googleapis.com/bigquery/v2/{prefix}{name}"

    @classmethod
    def _routine_url(cls, resource: str, verb: str) -> str:
        return f"{cls._routine_get_url(resource)}:{verb}"

    @staticmethod
    def _policy(payload: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(payload.get("etag"), str) or not isinstance(
            payload.get("bindings", []), list
        ):
            raise MigrationRefusal("execution_approval_v2_iam_readback_mismatch")
        return dict(payload)

    def dataset_access(self) -> tuple[str, list[dict[str, object]]]:
        etag, access, _modified = self.dataset_state()
        return etag, access

    def dataset_state(self) -> tuple[str, list[dict[str, object]], int | None]:
        """The etag, the access list and lastModifiedTime, all from one read."""

        payload = _response_json(self._session.get(self._dataset_url()))
        etag, access = payload.get("etag"), payload.get("access")
        if not isinstance(etag, str) or not etag or not isinstance(access, list):
            raise MigrationRefusal("execution_approval_v2_iam_readback_mismatch")
        return etag, [dict(item) for item in access], _epoch_ms(payload.get("lastModifiedTime"))

    def patch_dataset_access(self, etag: str, access: Sequence[Mapping[str, object]]) -> None:
        response = self._session.patch(
            self._dataset_url(),
            params={"fields": "access"},
            json={"access": [dict(item) for item in access]},
            headers={"If-Match": etag},
        )
        _response_json(response)

    def routine_policy(self, resource: str) -> dict[str, object]:
        response = self._session.post(
            self._routine_url(resource, "getIamPolicy"),
            json={"options": {"requestedPolicyVersion": 3}},
        )
        return self._policy(_response_json(response))

    def routine_if_exists(self, resource: str) -> dict[str, object] | None:
        """The routines.get resource as read, or None when the routine is not there."""

        response = self._session.get(self._routine_get_url(resource))
        if getattr(response, "status_code", None) == 404:
            return None
        return dict(_response_json(response))

    def routine(self, resource: str) -> dict[str, object]:
        payload = self.routine_if_exists(resource)
        if payload is None:
            raise MigrationRefusal("execution_approval_v2_iam_routine_missing")
        return payload

    def routine_type_if_exists(self, resource: str) -> str | None:
        payload = self.routine_if_exists(resource)
        return None if payload is None else str(payload.get("routineType") or "")

    def routine_type(self, resource: str) -> str:
        return str(self.routine(resource).get("routineType") or "")

    def routine_policy_if_exists(self, resource: str) -> dict[str, object] | None:
        response = self._session.post(
            self._routine_url(resource, "getIamPolicy"),
            json={"options": {"requestedPolicyVersion": 3}},
        )
        if getattr(response, "status_code", None) == 404:
            return None
        return self._policy(_response_json(response))

    def set_routine_policy(self, resource: str, policy: Mapping[str, object]) -> None:
        response = self._session.post(
            self._routine_url(resource, "setIamPolicy"), json={"policy": dict(policy)}
        )
        _response_json(response)

    def project_policy(self) -> dict[str, object]:
        url = f"https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT}:getIamPolicy"
        response = self._session.post(url, json={"options": {"requestedPolicyVersion": 3}})
        return self._policy(_response_json(response))


def _v2_iam_adapter(credentials: object) -> _V2IamAdapter:
    return _V2IamAdapter(_authorized_session(credentials))


class _V2IamWriteRefusal(MigrationRefusal):
    """A refusal after the write phase began; applied says what already landed."""

    def __init__(self, code: str, applied: Mapping[str, object]):
        super().__init__(code)
        self.applied = dict(applied)


def _policy_pairs(policy: Mapping[str, object]) -> set[tuple[str, str, str]]:
    """Flatten a policy to (role, member, condition expression) rows."""

    rows = set()
    for item in policy.get("bindings", ()):
        if not isinstance(item, dict) or not isinstance(item.get("members", []), list):
            raise MigrationRefusal("execution_approval_v2_iam_readback_mismatch")
        condition = item.get("condition")
        expression = json.dumps(condition, sort_keys=True) if condition is not None else ""
        for member in item.get("members", ()):
            rows.add((str(item.get("role")), str(member), expression))
    return rows


def _policy_has(policy: Mapping[str, object], binding: IamBinding) -> bool:
    return (binding.role, binding.principal, "") in _policy_pairs(policy)


def _access_routine_ref(entry: Mapping[str, object]) -> tuple[str, str, str] | None:
    routine = entry.get("routine")
    if routine is None:
        return None
    if not isinstance(routine, dict):
        raise MigrationRefusal("execution_approval_v2_iam_readback_mismatch")
    return (
        str(routine.get("projectId")),
        str(routine.get("datasetId")),
        str(routine.get("routineId")),
    )


def _access_key(entry: Mapping[str, object]) -> tuple[str, object]:
    # BigQuery need not echo the role of an authorized routine entry on read, so a
    # routine entry is identified by its routine reference alone.
    ref = _access_routine_ref(entry)
    if ref is not None:
        return ("routine", ref)
    return ("entry", json.dumps(entry, sort_keys=True))


def _authorization_ref(authorization: RoutineAuthorization) -> tuple[str, str, str]:
    return (PROJECT, DATASET, authorization.routine.rsplit("/", 1)[1])


# BigQuery refused a role on a scalar function's authorized routine entry ("Role is
# not supported for routine", live 23 Sept 2026), so that entry is written without
# one. Nothing was observed for a table valued function and the delta names none, so
# one is refused rather than guessed at. The type is read live from routines.get,
# never taken from the name.
_V2_FUNCTION_TYPES = frozenset({"SCALAR_FUNCTION"})
_V2_UNSUPPORTED_TYPES = frozenset({"TABLE_VALUED_FUNCTION"})
_V2_PROCEDURE_TYPE = "PROCEDURE"


def _is_function_type(routine_type: str) -> bool:
    if routine_type in _V2_FUNCTION_TYPES:
        return True
    if routine_type == _V2_PROCEDURE_TYPE:
        return False
    if routine_type in _V2_UNSUPPORTED_TYPES:
        raise MigrationRefusal("execution_approval_v2_iam_routine_type_unsupported")
    raise MigrationRefusal("execution_approval_v2_iam_routine_type_invalid")


def _written_role(authorization: RoutineAuthorization, functions: frozenset[str]) -> str | None:
    return None if authorization.routine in functions else authorization.role


def _v2_authorization_entry(
    authorization: RoutineAuthorization, functions: frozenset[str]
) -> dict[str, object]:
    entry = _GoogleIamAdapter._authorization_entry(authorization)
    if authorization.routine in functions:
        del entry["role"]
    return entry


def _authorization_present(
    access: Sequence[Mapping[str, object]],
    authorization: RoutineAuthorization,
    functions: frozenset[str],
) -> bool:
    ref = _authorization_ref(authorization)
    expected = _written_role(authorization, functions)
    found = [entry for entry in access if _access_routine_ref(entry) == ref]
    # A function's entry must hold no role and a procedure's exactly the delta role; a
    # roleless procedure entry proves no role and is refused, not counted present.
    for entry in found:
        if entry.get("role") != expected:
            raise MigrationRefusal("execution_approval_v2_iam_authorization_role_mismatch")
    return bool(found)


def _routine_entry_shape(access: Sequence[Mapping[str, object]]) -> dict[str, int]:
    routines = [entry for entry in access if _access_routine_ref(entry) is not None]
    with_role = sum(1 for entry in routines if entry.get("role") is not None)
    return {
        "count": len(routines),
        "with_role": with_role,
        "without_role": len(routines) - with_role,
    }


@dataclass
class _V2IamState:
    etag: str
    access: list[dict[str, object]]
    policies: dict[str, dict[str, object]]
    project: dict[str, object]
    functions: frozenset[str]
    # lastModifiedTime of the dataset and of each delta routine, in epoch milliseconds.
    dataset_modified: int | None = None
    routine_modified: dict[str, int | None] = dataclasses.field(default_factory=dict)


def _read_v2_iam_state(
    adapter: _V2IamAdapter, targets: V2IamTargets, preserved: Sequence[IamBinding] = ()
) -> _V2IamState:
    etag, access, dataset_modified = adapter.dataset_state()
    resources = {item.resource for item in targets.bindings} | {item.resource for item in preserved}
    # Every delta routine must exist live (and be of a known type) before its policy
    # is read; a missing one is named as missing, not as a failed policy read.
    routines = {item.routine: adapter.routine(item.routine) for item in targets.authorizations}
    functions = frozenset(
        item.routine
        for item in targets.authorizations
        if _is_function_type(str(routines[item.routine].get("routineType") or ""))
    )
    policies = {resource: adapter.routine_policy(resource) for resource in sorted(resources)}
    return _V2IamState(
        etag,
        access,
        policies,
        adapter.project_policy(),
        functions,
        dataset_modified,
        {name: _epoch_ms(payload.get("lastModifiedTime")) for name, payload in routines.items()},
    )


# A dataset entry that authorizes a routine expires when the routine is replaced
# (CREATE OR REPLACE or routines.update), yet BigQuery keeps listing it for up to 24
# hours (https://docs.cloud.google.com/bigquery/docs/authorized-routines). BigQuery
# records no time an entry was added, so presence alone proves nothing. The verdicts:
# stale when the routine was modified after the dataset's own lastModifiedTime (every
# entry was added at or before that time, so before the routine changed); present only
# when a recorded authorisation receipt holds the routine's current lastModifiedTime
# (the routine was read at that time before its entry was added and has not changed
# since); possibly stale otherwise. A later dataset change of any kind moves the
# dataset time past the routine's, so without a receipt that case can never be read as
# present. BigQuery's own removal of a stale entry reads as missing.
AUTHORISATION_PRESENT = "present"
AUTHORISATION_STALE = "authorisation_stale"
AUTHORISATION_POSSIBLY_STALE = "authorisation_possibly_stale"
AUTHORISATION_MISSING = "missing"
# Receipts live in one folder beside the checkout, as the grant claims do, so a clean
# checkout never holds them. Only an add this module made and read back writes one.
V2_AUTHORISATION_RECEIPTS_DIR = ROOT.parent.resolve().parent / "42-routine-authorisation-receipts"
_V2_AUTHORISATION_RECEIPT_CONTRACT = "open_intelligence_v2_routine_authorisation_receipt_v1"


def _epoch_ms(value: object) -> int | None:
    """BigQuery's int64 millisecond times arrive as decimal strings."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def _ms_text(value: int | None) -> str | None:
    if value is None:
        return None
    moment = datetime.fromtimestamp(value // 1000, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{value % 1000:03d}Z"


def _v2_authorisation_receipts() -> dict[str, frozenset[int]]:
    """Per routine name, every lastModifiedTime a recorded add read before adding its entry.

    A receipt that cannot be read or is not one of ours proves nothing and is skipped,
    so the verdict falls back to possibly stale.
    """

    folder = Path(V2_AUTHORISATION_RECEIPTS_DIR)
    try:
        paths = sorted(folder.glob("*.json")) if folder.is_dir() else []
    except OSError:
        return {}
    recorded: dict[str, set[int]] = {}
    for path in paths:
        try:
            payload = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("contract_version") != _V2_AUTHORISATION_RECEIPT_CONTRACT
            or payload.get("project") != PROJECT
            or payload.get("dataset") != DATASET
            or not isinstance(payload.get("authorisations"), list)
        ):
            continue
        for row in payload["authorisations"]:
            if not isinstance(row, dict) or not isinstance(row.get("routine"), str):
                continue
            modified = _epoch_ms(row.get("routine_last_modified_time"))
            if modified is not None:
                recorded.setdefault(row["routine"], set()).add(modified)
    return {name: frozenset(times) for name, times in recorded.items()}


def _staleness(
    routine_modified: int | None, dataset_modified: int | None, recorded: frozenset[int]
) -> str:
    """The verdict on an entry that is listed; see the rule above."""

    if routine_modified is None:
        return AUTHORISATION_POSSIBLY_STALE
    if dataset_modified is not None and routine_modified > dataset_modified:
        return AUTHORISATION_STALE
    if routine_modified in recorded:
        return AUTHORISATION_PRESENT
    return AUTHORISATION_POSSIBLY_STALE


def _authorisation_verdict(
    state: _V2IamState, item: RoutineAuthorization, receipts: Mapping[str, frozenset[int]]
) -> str:
    if not _authorization_present(state.access, item, state.functions):
        return AUTHORISATION_MISSING
    return _staleness(
        state.routine_modified.get(item.routine),
        state.dataset_modified,
        receipts.get(item.routine.rsplit("/", 1)[1], frozenset()),
    )


def _authorisation_row(state: _V2IamState, item: RoutineAuthorization) -> dict[str, object]:
    return {
        "routine": item.routine.rsplit("/", 1)[1],
        "role": item.role,
        "written_role": _written_role(item, state.functions),
        "routine_last_modified_time": _ms_text(state.routine_modified.get(item.routine)),
    }


def _authorisation_staleness(
    targets: V2IamTargets, state: _V2IamState, receipts: Mapping[str, frozenset[int]]
) -> dict[str, object]:
    """Every listed entry that cannot be proven fresh, named, with the dataset time."""

    verdicts = [
        (item, _authorisation_verdict(state, item, receipts)) for item in targets.authorizations
    ]
    return {
        "dataset_last_modified_time": _ms_text(state.dataset_modified),
        "present": sum(1 for _item, value in verdicts if value == AUTHORISATION_PRESENT),
        "stale": [
            _authorisation_row(state, item)
            for item, value in verdicts
            if value == AUTHORISATION_STALE
        ],
        "possibly_stale": [
            _authorisation_row(state, item)
            for item, value in verdicts
            if value == AUTHORISATION_POSSIBLY_STALE
        ],
        "missing": sum(1 for _item, value in verdicts if value == AUTHORISATION_MISSING),
    }


def _record_v2_authorisations(
    added: Sequence[RoutineAuthorization], before: _V2IamState, after: _V2IamState
) -> dict[str, object]:
    """Record each entry this run added and read back, with the routine time read before it.

    A routine whose lastModifiedTime moved between the read before the add and the
    readback is left out: its entry may predate the change. A receipt that cannot be
    written leaves those entries possibly stale; the writes themselves stand.
    """

    rows = [
        {
            "routine": item.routine.rsplit("/", 1)[1],
            "written_role": _written_role(item, before.functions),
            "routine_last_modified_time": str(before.routine_modified[item.routine]),
        }
        for item in added
        if before.routine_modified.get(item.routine) is not None
        and before.routine_modified.get(item.routine) == after.routine_modified.get(item.routine)
    ]
    left_out = sorted(
        item.routine.rsplit("/", 1)[1]
        for item in added
        if item.routine.rsplit("/", 1)[1] not in {row["routine"] for row in rows}
    )
    if not rows:
        return {"state": "nothing_recorded", "recorded": 0, "left_out": left_out}
    payload = {
        "contract_version": _V2_AUTHORISATION_RECEIPT_CONTRACT,
        "project": PROJECT,
        "dataset": DATASET,
        "dataset_last_modified_time": None
        if after.dataset_modified is None
        else str(after.dataset_modified),
        "authorisations": sorted(rows, key=lambda row: row["routine"]),
    }
    name = f"{after.dataset_modified or 0}-{_digest(payload)[:16]}.json"
    try:
        folder = Path(V2_AUTHORISATION_RECEIPTS_DIR)
        folder.mkdir(exist_ok=True)
        with open(folder / name, "xb") as handle:
            handle.write((json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    except FileExistsError:
        pass
    except OSError:
        return {"state": "unwritten", "recorded": 0, "left_out": left_out}
    return {"state": "recorded", "file": name, "recorded": len(rows), "left_out": left_out}


def _v2_iam_split(
    targets: V2IamTargets, state: _V2IamState, preserved: Sequence[IamBinding] = ()
) -> tuple[list[RoutineAuthorization], list[IamBinding], list[IamBinding]]:
    auth_missing = [
        item
        for item in targets.authorizations
        if not _authorization_present(state.access, item, state.functions)
    ]
    binding_missing = [
        item for item in targets.bindings if not _policy_has(state.policies[item.resource], item)
    ]
    delta_keys = {(item.principal, item.resource, item.role) for item in targets.bindings}
    restore_missing = [
        item
        for item in preserved
        if (item.principal, item.resource, item.role) not in delta_keys
        and not _policy_has(state.policies[item.resource], item)
    ]
    return auth_missing, binding_missing, restore_missing


def _snapshot_v2_iam(
    plan: VersionedStoreMigrationPlan, credentials: object
) -> tuple[IamBinding, ...]:
    """Read every v2 routine policy before CREATE OR REPLACE drops it.

    Returns the unconditional (role, member) rows to restore. A routine that does not
    exist yet has nothing to restore. A policy this step cannot restore exactly (a
    conditional binding) is refused before anything is replaced.
    """

    adapter = _v2_iam_adapter(credentials)
    rows = []
    for item in plan.routines:
        resource = _routine_resource(item.name)
        policy = adapter.routine_policy_if_exists(resource)
        if policy is None:
            continue
        for role, member, condition in sorted(_policy_pairs(policy)):
            if condition:
                raise MigrationRefusal("execution_approval_v2_iam_snapshot_unsupported")
            rows.append(_binding(member, resource, role, "restored after CREATE OR REPLACE"))
    return tuple(rows)


def _v2_iam_new_policy(policy: Mapping[str, object], missing: Sequence[IamBinding]) -> dict:
    revised = json.loads(json.dumps(policy))
    bindings = list(revised.get("bindings", ()))
    for binding in missing:
        match = next(
            (
                item
                for item in bindings
                if item.get("role") == binding.role and item.get("condition") is None
            ),
            None,
        )
        if match is None:
            bindings.append({"role": binding.role, "members": [binding.principal]})
        else:
            match["members"] = sorted({*match.get("members", ()), binding.principal})
    revised["bindings"] = bindings
    if not _policy_pairs(policy) <= _policy_pairs(revised):
        raise MigrationRefusal("execution_approval_v2_iam_write_refused")
    return revised


def _v2_iam_writes(targets, state, auth_missing, additions, preserved):
    """Return the exact add only payloads: (new access list or None, {resource: policy})."""

    allowed = {(item.principal, item.resource, item.role) for item in targets.bindings} | {
        (item.principal, item.resource, item.role) for item in preserved
    }
    for item in additions:
        if (item.principal, item.resource, item.role) not in allowed:
            raise MigrationRefusal("execution_approval_v2_iam_member_refused")
    access = None
    if auth_missing:
        access = [
            *state.access,
            *(_v2_authorization_entry(item, state.functions) for item in auth_missing),
        ]
    by_resource: dict[str, list[IamBinding]] = {}
    for item in additions:
        by_resource.setdefault(item.resource, []).append(item)
    policies = {
        resource: _v2_iam_new_policy(state.policies[resource], rows)
        for resource, rows in sorted(by_resource.items())
    }
    return access, policies


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _without_etag(policy: Mapping[str, object]) -> list[list[str]]:
    return sorted(list(row) for row in _policy_pairs(policy))


def _policy_view(policy: Mapping[str, object]) -> list[dict[str, object]]:
    """Roles and members only: what Albert reads in the dry run."""

    members: dict[str, set[str]] = {}
    for role, member, _condition in _policy_pairs(policy):
        members.setdefault(role, set()).add(member)
    return [{"role": role, "members": sorted(names)} for role, names in sorted(members.items())]


def _v2_iam_target_payload(targets: V2IamTargets) -> dict[str, object]:
    return {
        "authorizations": sorted([item.routine, item.role] for item in targets.authorizations),
        "bindings": sorted([item.principal, item.resource, item.role] for item in targets.bindings),
    }


def _v2_iam_readback_digest(targets: V2IamTargets, state: _V2IamState) -> str:
    return _digest(
        {
            # The live role of each matched entry, as read back, not the expected one.
            "authorizations": sorted(
                [item.routine, str(entry.get("role") or "")]
                for item in targets.authorizations
                if _authorization_present(state.access, item, state.functions)
                for entry in state.access
                if _access_routine_ref(entry) == _authorization_ref(item)
            ),
            "policies": {
                resource: _without_etag(policy) for resource, policy in state.policies.items()
            },
        }
    )


def _v2_iam_project_report(targets: V2IamTargets, state: _V2IamState) -> dict[str, object]:
    missing = [
        {"member": item.principal, "role": item.role}
        for item in targets.project_bindings
        if not _policy_has(state.project, item)
    ]
    return {
        "target": len(targets.project_bindings),
        "present": len(targets.project_bindings) - len(missing),
        "missing": missing,
        "granted_here": False,
    }


def _v2_iam_disagreement_summary(targets: V2IamTargets) -> dict[str, object]:
    summary: dict[str, object] = {}
    for key, rows in targets.disagreement.items():
        summary[key] = rows
        summary[key + "_count"] = len(rows)
    summary["authority"] = "delta"
    return summary


def _binding_row(item: IamBinding) -> dict[str, str]:
    return {"member": item.principal, "routine": item.resource.rsplit("/", 1)[1], "role": item.role}


def _run_v2_iam(
    plan: VersionedStoreMigrationPlan,
    credentials: object,
    mode: str,
    preserved: Sequence[IamBinding] = (),
    check_missing_routines: bool = False,
) -> dict[str, object]:
    if mode not in {"plan", "dry_run", "apply"}:
        raise MigrationRefusal("execution_approval_manifest_invalid")
    targets = v2_iam_targets(plan)
    adapter = _v2_iam_adapter(credentials)
    before = _read_v2_iam_state(adapter, targets, preserved)
    auth_missing, binding_missing, restore_missing = _v2_iam_split(targets, before, preserved)
    if check_missing_routines and auth_missing:
        differences = _v2_missing_routine_differences(plan, adapter, auth_missing)
        if differences:
            raise _ApplyV2RoutineRefusal(
                "execution_approval_v2_reauthorise_routine_not_current",
                {"routines": differences},
            )
    access, policies = _v2_iam_writes(
        targets, before, auth_missing, [*binding_missing, *restore_missing], preserved
    )
    receipt: dict[str, object] = {
        "contract_version": "open_intelligence_execution_store_v2_iam_v1",
        "mode": mode,
        "delta_sha256": targets.delta_sha256,
        "target_sha256": _digest(_v2_iam_target_payload(targets)),
        "dataset_routine_entries": _routine_entry_shape(before.access),
        # The delta names a role for every authorization; BigQuery takes none on a
        # function's entry, so these are written roleless and grant no more.
        "role_dropped_for_function": sorted(
            (
                {"routine": item.routine.rsplit("/", 1)[1], "delta_role": item.role}
                for item in targets.authorizations
                if item.routine in before.functions
            ),
            key=lambda row: row["routine"],
        ),
        "project_level": _v2_iam_project_report(targets, before),
        "disagreement": _v2_iam_disagreement_summary(targets),
    }
    receipts = _v2_authorisation_receipts()
    if mode == "plan":
        # A listed entry counts as present only when it is proven fresh; a stale or
        # possibly stale one is named as such and never counted present.
        staleness = _authorisation_staleness(targets, before, receipts)
        receipt["authorizations"] = {
            "target": len(targets.authorizations),
            "present": staleness["present"],
            "missing": [
                {
                    "routine": item.routine.rsplit("/", 1)[1],
                    "role": item.role,
                    "written_role": _written_role(item, before.functions),
                }
                for item in auth_missing
            ],
            "stale": staleness["stale"],
            "possibly_stale": staleness["possibly_stale"],
            "dataset_last_modified_time": staleness["dataset_last_modified_time"],
        }
        receipt["routine_bindings"] = {
            "target": len(targets.bindings),
            "present": len(targets.bindings) - len(binding_missing),
            "missing": [_binding_row(item) for item in binding_missing],
        }
        receipt["readback_sha256"] = _v2_iam_readback_digest(targets, before)
        return receipt
    receipt["writes_planned"] = {
        "dataset_access_patches": 0 if access is None else 1,
        "routine_policy_sets": len(policies),
    }
    receipt["policies_to_write"] = {
        resource.rsplit("/", 1)[1]: _policy_view(policy) for resource, policy in policies.items()
    }
    receipt["planned_payload_sha256"] = _digest(
        {
            "access": access,
            "policies": {resource: _without_etag(item) for resource, item in policies.items()},
        }
    )
    added_auth = added_bindings = 0
    restored: list[IamBinding] = []
    after = before
    if mode == "apply":
        applied: dict[str, object] = {
            "dataset_access_patched": False,
            "authorizations_added": 0,
            "routine_policies_set": [],
            "failed_at": None,
        }
        step = "dataset_access"
        try:
            if access is not None:
                adapter.patch_dataset_access(before.etag, access)
                applied["dataset_access_patched"] = True
                applied["authorizations_added"] = len(auth_missing)
            for resource, policy in policies.items():
                step = resource.rsplit("/", 1)[1]
                adapter.set_routine_policy(resource, policy)
                applied["routine_policies_set"].append(step)
        except Exception as exc:
            applied["failed_at"] = step
            raise _V2IamWriteRefusal("execution_approval_v2_iam_write_refused", applied) from exc
        added_auth = len(auth_missing)
        added_bindings = len(binding_missing)
        restored = list(restore_missing)
        applied["failed_at"] = "readback"
        try:
            after = _read_v2_iam_state(adapter, targets, preserved)
            still_auth, still_bindings, still_restore = _v2_iam_split(targets, after, preserved)
            after_keys = {_access_key(item) for item in after.access}
            kept_access = all(_access_key(item) in after_keys for item in before.access)
            kept_policies = all(
                _policy_pairs(before.policies[resource]) <= _policy_pairs(after.policies[resource])
                for resource in before.policies
            )
        except Exception as exc:
            raise _V2IamWriteRefusal(
                "execution_approval_v2_iam_readback_mismatch", applied
            ) from exc
        if still_auth or still_bindings or still_restore or not kept_access or not kept_policies:
            raise _V2IamWriteRefusal("execution_approval_v2_iam_readback_mismatch", applied)
        if auth_missing:
            receipt["authorisation_record"] = _record_v2_authorisations(auth_missing, before, after)
            receipts = _v2_authorisation_receipts()
    else:
        restored = []
    final_auth, final_bindings, _final_restore = _v2_iam_split(targets, after, preserved)
    receipt["authorizations"] = {
        "target": len(targets.authorizations),
        "present_before": len(targets.authorizations) - len(auth_missing),
        "added": added_auth,
        "present_after": len(targets.authorizations) - len(final_auth),
    }
    receipt["routine_bindings"] = {
        "target": len(targets.bindings),
        "present_before": len(targets.bindings) - len(binding_missing),
        "added": added_bindings,
        "present_after": len(targets.bindings) - len(final_bindings),
    }
    receipt["restored"] = [_binding_row(item) for item in restored]
    # present_before and present_after count listed entries; this names every listed
    # entry that cannot be proven fresh.
    receipt["authorisation_staleness"] = _authorisation_staleness(targets, after, receipts)
    receipt["readback_sha256"] = _v2_iam_readback_digest(targets, after)
    return receipt


class _ApplyV2Refusal(MigrationRefusal):
    """A refusal after the routine IAM snapshot; it carries the rows to restore by hand."""

    def __init__(self, cause: MigrationRefusal, preserved: Sequence[IamBinding]):
        super().__init__(str(cause))
        self.applied = cause.applied if isinstance(cause, _V2IamWriteRefusal) else None
        self.preserved = [_binding_row(item) for item in preserved]


def _v2_iam_preflight(plan: VersionedStoreMigrationPlan, credentials: object) -> None:
    targets = v2_iam_targets(plan)
    adapter = _v2_iam_adapter(credentials)
    _etag, access = adapter.dataset_access()
    for item in targets.authorizations:
        routine_type = adapter.routine_type_if_exists(item.routine)
        if routine_type is None:
            continue
        functions = frozenset({item.routine}) if _is_function_type(routine_type) else frozenset()
        _authorization_present(access, item, functions)


class _ApplyV2RoutineRefusal(MigrationRefusal):
    """A refusal before any write that names the routines it refused on."""

    def __init__(self, code: str, detail: Mapping[str, object]):
        super().__init__(code)
        self.detail = dict(detail)


# What apply-v2 compares before it writes a routine: the routines.get fields the
# rendered CREATE statement sets, and nothing it leaves at a default. A procedure's
# argument without a mode is IN and any argument without a kind is FIXED_TYPE; a
# routine without a language is SQL (the routines API defaults). The body is the one
# _readback_v2_store already proves against INFORMATION_SCHEMA.ROUTINES.
_V2_ROUTINE_HEADER = re.compile(
    r"CREATE OR REPLACE (?P<kind>PROCEDURE|FUNCTION) "
    r"`\{project\}\.\{dataset\}\.(?P<name>[a-z0-9_]+)`\((?P<arguments>[^()]*)\)"
    r"(?:\s+RETURNS (?P<returns>[A-Z0-9]+))?"
    r"(?:\s+LANGUAGE (?P<language>[a-z]+))?"
    r"(?:\s+OPTIONS \(description = '(?P<description>[^'\\]*)'\))?\s*"
)
_V2_ROUTINE_ARGUMENT = re.compile(r"([a-z_][a-z0-9_]*) ([A-Z0-9]+)")
_V2_ROUTINE_KINDS = {"PROCEDURE": "PROCEDURE", "FUNCTION": "SCALAR_FUNCTION"}
_V2_ROUTINE_LANGUAGES = {None: "SQL", "js": "JAVASCRIPT"}
_V2_ROUTINE_ARGUMENT_KEYS = frozenset({"name", "dataType", "mode", "argumentKind"})


def _v2_routine_expected(item: RoutinePlan) -> dict[str, object]:
    """The routines.get view of the routine the rendered SQL creates."""

    if item.name == CANONICAL_JSON_UDF:
        end = item.sql.find('AS r"""')
    else:
        marker = _ROUTINE_BODY_MARKER.search(item.sql)
        end = -1 if marker is None else marker.start()
    header = _V2_ROUTINE_HEADER.fullmatch(item.sql[:end]) if end >= 0 else None
    if header is None or header.group("name") != item.name:
        raise MigrationRefusal("execution_approval_schema_mismatch")
    procedure = header.group("kind") == "PROCEDURE"
    arguments = []
    listed = header.group("arguments").strip()
    for text in listed.split(",") if listed else ():
        match = _V2_ROUTINE_ARGUMENT.fullmatch(text.strip())
        if match is None:
            raise MigrationRefusal("execution_approval_schema_mismatch")
        arguments.append(
            {
                "name": match.group(1),
                "dataType": {"typeKind": match.group(2)},
                "mode": "IN" if procedure else None,
                "argumentKind": "FIXED_TYPE",
            }
        )
    language = header.group("language")
    if language not in _V2_ROUTINE_LANGUAGES or procedure == bool(header.group("returns")):
        raise MigrationRefusal("execution_approval_schema_mismatch")
    returns = header.group("returns")
    return {
        "routineType": _V2_ROUTINE_KINDS[header.group("kind")],
        "language": _V2_ROUTINE_LANGUAGES[language],
        "arguments": arguments,
        "returnType": None if returns is None else {"typeKind": returns},
        "description": header.group("description"),
        "definitionBody": _v2_routine_definition(item),
    }


def _v2_routine_live(payload: Mapping[str, object], procedure: bool) -> dict[str, object]:
    """The same view of a routines.get resource; anything unexpected is kept as read."""

    arguments = payload.get("arguments", [])
    if isinstance(arguments, list):
        arguments = [
            {
                "name": argument.get("name"),
                "dataType": argument.get("dataType"),
                "mode": argument.get("mode", "IN" if procedure else None),
                "argumentKind": argument.get("argumentKind", "FIXED_TYPE"),
            }
            if isinstance(argument, dict) and set(argument) <= _V2_ROUTINE_ARGUMENT_KEYS
            else argument
            for argument in arguments
        ]
    return {
        "routineType": payload.get("routineType"),
        "language": payload.get("language", "SQL"),
        "arguments": arguments,
        "returnType": payload.get("returnType"),
        "description": payload.get("description"),
        "definitionBody": payload.get("definitionBody"),
    }


def _v2_routine_differences(item: RoutinePlan, payload: Mapping[str, object]) -> list[str]:
    """The compared fields where the live routine differs from what apply-v2 would create."""

    expected = _v2_routine_expected(item)
    live = _v2_routine_live(payload, expected["routineType"] == "PROCEDURE")
    return sorted(key for key in expected if expected[key] != live[key])


def _v2_missing_routine_differences(
    plan: VersionedStoreMigrationPlan,
    adapter: _V2IamAdapter,
    missing: Sequence[RoutineAuthorization],
) -> list[dict[str, object]]:
    routines = {item.name: item for item in plan.routines}
    differences = []
    for authorization in missing:
        name = authorization.routine.rsplit("/", 1)[1]
        fields = _v2_routine_differences(
            routines[name], adapter.routine(authorization.routine)
        )
        if fields:
            differences.append({"routine": name, "differs": fields})
    return differences


def _v2_routine_changes(
    plan: VersionedStoreMigrationPlan, credentials: object
) -> dict[str, list[str]]:
    """Every v2 routine apply-v2 must write, with why, read before anything is written.

    A routine that holds an entry in the approvals dataset's access list is refused, not
    replaced: the replace voids its authorization and only deleting the entry and adding
    it again would restore it, and no entry is ever deleted here.
    """

    adapter = _v2_iam_adapter(credentials)
    _etag, access = adapter.dataset_access()
    held = {
        ref[2]
        for ref in (_access_routine_ref(entry) for entry in access)
        if ref is not None and ref[:2] == (PROJECT, DATASET)
    }
    changes: dict[str, list[str]] = {}
    for item in plan.routines:
        payload = adapter.routine_if_exists(_routine_resource(item.name))
        differences = ["absent"] if payload is None else _v2_routine_differences(item, payload)
        if differences:
            changes[item.name] = differences
    voided = [
        {"routine": name, "differs": differences}
        for name, differences in sorted(changes.items())
        if name in held
    ]
    if voided:
        raise _ApplyV2RoutineRefusal(
            "execution_approval_v2_routine_replace_voids_authorization", {"routines": voided}
        )
    return changes


def _apply_v2_plan(plan: VersionedStoreMigrationPlan) -> dict[str, object]:
    credentials = _load_credentials()
    v1_lock_before = _read_v1_lock_row(_bigquery_client(credentials))
    # Everything the IAM step would refuse on its own reads (the delta pin, a v2 routine
    # entry held with another role) is checked before anything is replaced.
    _v2_iam_preflight(plan, credentials)
    # A routine whose live definition equals the rendered one is never written again, so
    # its dataset authorization is not voided; a changed one that holds an authorization
    # is refused here, before any write.
    changes = _v2_routine_changes(plan, credentials)
    # CREATE OR REPLACE drops routine level IAM, so every routine policy is read first
    # and whatever it held (a hand applied operator binding included) is restored.
    preserved = _snapshot_v2_iam(plan, credentials)
    install = replace(plan, routines=tuple(item for item in plan.routines if item.name in changes))
    try:
        _apply_v2_installation(install, credentials)
        _readback_v2_store(plan, credentials, v1_lock_before)
        # The install is finished only once the delta's routine authorizations and
        # bindings and the snapshot rows are back and read back.
        iam_receipt = _run_v2_iam(plan, credentials, "apply", preserved)
    except MigrationRefusal as exc:
        raise _ApplyV2Refusal(exc, preserved) from exc
    except Exception as exc:
        internal = MigrationRefusal("execution_approval_internal_refusal")
        raise _ApplyV2Refusal(internal, preserved) from exc
    return {
        "contract_version": "open_intelligence_execution_store_v2_install_v1",
        "origin_registry_sha256": plan.active_pair[0],
        "resource_manifest_sha256": plan.active_pair[1],
        "registrations": [item.sha256 for item in plan.registrations],
        "v1_lock_state": v1_lock_before[3],
        "v1_lock_version": v1_lock_before[2],
        "routines": {
            "created": sorted(name for name, why in changes.items() if why == ["absent"]),
            "replaced": sorted(name for name, why in changes.items() if why != ["absent"]),
            "unchanged": sorted(item.name for item in plan.routines if item.name not in changes),
        },
        "iam": iam_receipt,
    }


# The read only authorisation state and the add only re-authorisation. Once BigQuery has
# dropped an entry a replace voided (the plan then lists it missing), the re-authorisation
# adds the amendment d entries back through the iam-v2 add path (_v2_iam_writes and
# patch_dataset_access, If-Match on the etag the state was read with) and records a
# receipt. It sends no routine policy and never removes or rewrites an entry; while a
# target entry is still listed and proven stale it refuses before any write. The amendment
# e rows over amendment d go back through iam-e-apply, under its own approval.
_V2_AUTHORISATION_STATE_WORD = "iam-v2-authorisation-state"
_V2_REAUTHORISE_WORDS = {
    "iam-v2-reauthorise-dry-run": "dry_run",
    "iam-v2-reauthorise-apply": "apply",
}


def v2_authorisation_state(
    plan: VersionedStoreMigrationPlan, credentials: object
) -> dict[str, object]:
    """Per authorised v2 routine: the entry, its role, the routine time and the verdict."""

    targets = v2_iam_targets(plan)
    adapter = _v2_iam_adapter(credentials)
    _etag, access, dataset_modified = adapter.dataset_state()
    delta_roles = {item.routine.rsplit("/", 1)[1]: item.role for item in targets.authorizations}
    installed = {item.name for item in plan.routines}
    listed = {
        ref[2]
        for ref in (_access_routine_ref(entry) for entry in access)
        if ref is not None and ref[:2] == (PROJECT, DATASET) and ref[2] in installed
    }
    routines = {item.name: item for item in plan.routines}
    receipts = _v2_authorisation_receipts()
    rows = []
    for name in sorted(set(delta_roles) | listed):
        entries = [
            entry for entry in access if _access_routine_ref(entry) == (PROJECT, DATASET, name)
        ]
        payload = adapter.routine_if_exists(_routine_resource(name))
        modified = None if payload is None else _epoch_ms(payload.get("lastModifiedTime"))
        routine_type = None if payload is None else str(payload.get("routineType") or "")
        if not entries:
            verdict = AUTHORISATION_MISSING
        elif payload is None:
            verdict = "routine_missing"
        else:
            verdict = _staleness(modified, dataset_modified, receipts.get(name, frozenset()))
        rows.append(
            {
                "routine": name,
                "entry_present": bool(entries),
                "entry_count": len(entries),
                "entry_role": sorted(
                    {str(entry.get("role")) for entry in entries if entry.get("role")}
                ),
                "delta_role": delta_roles.get(name),
                "routine_type": routine_type,
                "routine_last_modified_time": _ms_text(modified),
                "recorded_routine_times": [
                    _ms_text(value) for value in sorted(receipts.get(name, ()))
                ],
                "differs": (
                    ["absent"]
                    if payload is None
                    else _v2_routine_differences(routines[name], payload)
                ),
                "verdict": verdict,
            }
        )
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row["verdict"])] = counts.get(str(row["verdict"]), 0) + 1
    return {
        "contract_version": "open_intelligence_execution_store_v2_authorisation_state_v1",
        "mode": "state",
        "delta_sha256": targets.delta_sha256,
        "dataset_last_modified_time": _ms_text(dataset_modified),
        "counts": dict(sorted(counts.items())),
        "routines": rows,
    }


def _guard_reauthorisation(
    before: Sequence[Mapping[str, object]],
    after: Sequence[Mapping[str, object]],
    added: Sequence[RoutineAuthorization],
    functions: frozenset[str],
) -> None:
    """The payload is every entry read, unchanged and in order, then only the new entries."""

    expected = [*before, *(_v2_authorization_entry(item, functions) for item in added)]
    if [_iam_e_canonical(item) for item in after] != [_iam_e_canonical(item) for item in expected]:
        raise MigrationRefusal("execution_approval_v2_iam_write_refused")
    if any(_access_routine_ref(entry) is None for entry in after[len(before) :]):
        raise MigrationRefusal("execution_approval_v2_iam_write_refused")


def _run_v2_reauthorise(
    plan: VersionedStoreMigrationPlan, credentials: object, mode: str
) -> dict[str, object]:
    if mode not in {"dry_run", "apply"}:
        raise MigrationRefusal("execution_approval_manifest_invalid")
    targets = v2_iam_targets(plan)
    adapter = _v2_iam_adapter(credentials)
    before = _read_v2_iam_state(adapter, targets)
    receipts = _v2_authorisation_receipts()
    verdicts = {
        item.routine: _authorisation_verdict(before, item, receipts)
        for item in targets.authorizations
    }
    stale = [
        item for item in targets.authorizations if verdicts[item.routine] == AUTHORISATION_STALE
    ]
    if stale:
        # Adding cannot mend a listed stale entry and nothing here deletes one; BigQuery
        # removes it within 24 hours, and then this word adds it back.
        raise _ApplyV2RoutineRefusal(
            "execution_approval_v2_iam_authorisation_still_stale",
            {"stale": [_authorisation_row(before, item) for item in stale]},
        )
    missing = [
        item for item in targets.authorizations if verdicts[item.routine] == AUTHORISATION_MISSING
    ]
    differences = _v2_missing_routine_differences(plan, adapter, missing)
    if differences:
        raise _ApplyV2RoutineRefusal(
            "execution_approval_v2_reauthorise_routine_not_current",
            {"routines": differences},
        )
    access, policies = _v2_iam_writes(targets, before, missing, [], ())
    if policies:
        raise MigrationRefusal("execution_approval_v2_iam_write_refused")
    if access is not None:
        _guard_reauthorisation(before.access, access, missing, before.functions)
    receipt: dict[str, object] = {
        "contract_version": "open_intelligence_execution_store_v2_reauthorisation_v1",
        "mode": mode,
        "delta_sha256": targets.delta_sha256,
        "target_sha256": _digest(_v2_iam_target_payload(targets)),
        "writes_planned": {
            "dataset_access_patches": 0 if access is None else 1,
            "routine_policy_sets": 0,
        },
        "entries_to_add": [_authorisation_row(before, item) for item in missing],
        "planned_payload_sha256": _digest({"access": access}),
    }
    after = before
    if mode == "apply" and access is not None:
        applied: dict[str, object] = {
            "dataset_access_patched": False,
            "authorizations_added": 0,
            "failed_at": "dataset_access",
        }
        try:
            adapter.patch_dataset_access(before.etag, access)
        except Exception as exc:
            raise _V2IamWriteRefusal("execution_approval_v2_iam_write_refused", applied) from exc
        applied.update(
            {
                "dataset_access_patched": True,
                "authorizations_added": len(missing),
                "failed_at": "readback",
            }
        )
        try:
            after = _read_v2_iam_state(adapter, targets)
            after_keys = {_access_key(item) for item in after.access}
            kept = all(_access_key(item) in after_keys for item in before.access)
            landed = all(
                _authorization_present(after.access, item, after.functions) for item in missing
            )
        except Exception as exc:
            raise _V2IamWriteRefusal(
                "execution_approval_v2_iam_readback_mismatch", applied
            ) from exc
        if not kept or not landed:
            raise _V2IamWriteRefusal("execution_approval_v2_iam_readback_mismatch", applied)
        receipt["authorisation_record"] = _record_v2_authorisations(missing, before, after)
        receipts = _v2_authorisation_receipts()
    receipt["authorizations"] = {
        "target": len(targets.authorizations),
        "missing_before": len(missing),
        "added": len(missing) if mode == "apply" and access is not None else 0,
    }
    receipt["authorisation_staleness"] = _authorisation_staleness(targets, after, receipts)
    return receipt


# Amendment e and the rounds after it. ops/deploy/iam_delta_v1.json adds, over the
# retained amendment d delta, rows the iam-v2 words cannot grant. The iam-e words grant
# exactly the rows the delta adds over the pinned amendment d bytes, whatever the round:
# routine authorizations, and bindings on routines, datasets, tables, buckets, Cloud Run
# jobs and the project, plus any custom role the delta defines with an exact permission
# set. The delta is bound by the digest of the bytes read at run time: the plan and the
# dry run report it, and the apply takes the digest Albert approved as its one argument
# and refuses any other bytes. The manifest is bound by the digest the delta names. Writes
# are add only (a custom role is created, or updated to exactly its defined permission
# set), guarded by the etag each resource was read with and checked where the request is
# built, so the dry run and the apply share one send path. No member is ever removed.
# Every dataset, table, bucket and Cloud Run job a row names is read itself before its
# policy, since a policy read may answer a resource that does not exist with an empty
# policy (Cloud Run does); what is not there is reported as resource_missing by the plan
# and the dry run, and the apply refuses before its first write while anything is.
# An amendment proposed after an approval stands in the delta's amendments list under
# its own approval block, so the stamp beneath it keeps covering exactly its bytes. Its
# rows are served with the rest, and the delta counts as approved only when the root
# block and every amendment block are approved, each only over approved earlier ones.
IAM_E_DELTA_PATH = ROOT.parent / "ops" / "deploy" / "iam_delta_v1.json"
IAM_E_BASE_PATH = V2_IAM_DELTA_PATH
IAM_E_BASE_SHA256 = V2_IAM_DELTA_SHA256
_IAM_E_WORDS = {"iam-e-plan": "plan", "iam-e-dry-run": "dry_run", "iam-e-apply": "apply"}
_IAM_E_SHA256 = re.compile(r"[0-9a-f]{64}")
_IAM_E_AMENDMENT_KEYS = frozenset({"amendment", "approval", "bindings"})
_IAM_E_AMENDMENT_NAME = re.compile(r"[a-z]")
_IAM_E_OPERATOR = "user:albert.meintjes@ogilvy.co.za"
_IAM_E_OPERATOR_ROUTINES = frozenset(
    {
        "sp_approve_open_intelligence_execution_v2",
        "sp_approve_open_intelligence_execution_v3",
        "sp_disable_open_intelligence_execution_approval_v2",
        CANONICAL_JSON_UDF,
        "sp_approve_open_intelligence_recurring_grant_v2",
        "sp_read_open_intelligence_recurring_grant_v2",
        "sp_disable_open_intelligence_recurring_grant_v2",
        "sp_reconcile_open_intelligence_daily_consumption_v1",
    }
)
# The predefined roles each kind admits. A custom role is admitted on any kind but a
# routine only when the delta defines it, so the words hold it to its permission set.
_IAM_E_KIND_ROLES = {
    "routine": frozenset({_V2_IAM_ROUTINE_BINDING_ROLE}),
    "dataset": frozenset({"roles/bigquery.dataViewer", "roles/bigquery.dataEditor"}),
    "table": frozenset({"roles/bigquery.dataViewer", "roles/bigquery.dataEditor"}),
    "bucket": frozenset(
        {"roles/storage.objectViewer", "roles/storage.objectCreator", "roles/storage.objectUser"}
    ),
    "runtime_job": frozenset(
        {
            "roles/run.viewer",
            "roles/run.invoker",
            "roles/run.jobsExecutor",
            "roles/run.jobsExecutorWithOverrides",
        }
    ),
    "project": frozenset({"roles/cloudbuild.builds.viewer"}),
}
# Roles amendment g binds that the words never define, each on its one kind: two live
# custom roles the serving identity already holds, QuestionVertexPredict on the project
# and QuestionControlReplace on a bucket, bound again with a later time bound, the same
# QuestionVertexPredict for the daily account (the daily compose's model calls), and the
# predefined bucket reader the route A capture's preflight needs (storage.buckets.get).
_IAM_E_BOUND_ONLY_ROLES = {
    "project": frozenset({f"projects/{PROJECT}/roles/QuestionVertexPredict"}),
    "bucket": frozenset(
        {f"projects/{PROJECT}/roles/QuestionControlReplace", "roles/storage.bucketViewer"}
    ),
}
# Predefined roles a kind admits for one named member only: project jobUser for the
# ingest account, amendment d's row 57, which the ingest route needs to run a BigQuery
# job (bigquery.jobs.create is a project permission). No other member takes it here.
_IAM_E_MEMBER_ROLES = {
    "project": {
        "roles/bigquery.jobUser": frozenset(
            {f"serviceAccount:intelligence-42-ingest@{PROJECT}.iam.gserviceaccount.com"}
        )
    },
}
# The one condition a project row may carry: amendment g's time bound on the serving
# identity's Vertex role. Every other project row is unconditioned.
_IAM_E_PROJECT_CONDITIONS = frozenset({"request.time < timestamp('2026-12-31T23:59:59Z')"})
# Amendment d rows approved on 20 September that no tool wrote: iam-v2 writes routine
# rows only, runtime_jobs.py only the rows on the jobs it manages, and these words only
# the rows over the amendment d bytes. Row 54, the funded account's run.viewer on its own
# pilot job, is needed by the funded run (the runtime guard reads its own execution and
# job); rows 56 and 57, the ingest account's dataEditor on intelligence_42_sources_staging
# and its jobUser on the project, by the ingest route. The one final apply, the apply of
# a delta carrying amendment g, serves all three, add only and read back, while the delta
# carries each exactly as amendment d approved it; an earlier file serves none.
_IAM_E_UNAPPLIED_BASE_ROWS = {
    "g": frozenset(
        {
            (
                None,
                f"serviceAccount:intelligence-42-funded@{PROJECT}.iam.gserviceaccount.com",
                f"//run.googleapis.com/projects/{PROJECT}/locations/us-central1/jobs/"
                "intelligence-42-funded-pilot-staging",
                "roles/run.viewer",
            ),
            (
                None,
                f"serviceAccount:intelligence-42-ingest@{PROJECT}.iam.gserviceaccount.com",
                f"//bigquery.googleapis.com/projects/{PROJECT}/datasets/"
                "intelligence_42_sources_staging",
                "roles/bigquery.dataEditor",
            ),
            (
                None,
                f"serviceAccount:intelligence-42-ingest@{PROJECT}.iam.gserviceaccount.com",
                f"//cloudresourcemanager.googleapis.com/projects/{PROJECT}",
                "roles/bigquery.jobUser",
            ),
        }
    )
}
# Resources an amendment may name beyond the manifest the delta binds, for that
# amendment's rows alone: amendment g's route A capture artifact bucket, a row of the
# active bridge generation manifest (resource_manifest_bridge_v3.json) that the reviewed
# amendment e manifest does not carry. Tests hold it equal to the bridge manifest row.
_IAM_E_AMENDMENT_RESOURCES = {
    "g": frozenset(
        {"//storage.googleapis.com/projects/_/buckets/ogilvy-trends-v2-oi-source-artifacts-staging"}
    )
}
_IAM_E_CUSTOM_ROLE = re.compile(
    rf"projects/{re.escape(PROJECT)}/roles/([A-Za-z][A-Za-z0-9_.]{{2,63}})"
)
_IAM_E_PERMISSION = re.compile(r"[a-z][a-z0-9]*\.[a-zA-Z]+\.[a-zA-Z]+")
# A custom role may not carry a permission that grants IAM itself.
_IAM_E_ESCALATING_SERVICES = ("iam.", "resourcemanager.", "orgpolicy.", "serviceusage.")
# The permission lists the delta validator pins for the custom roles it admits
# (CUSTOM_ROLE_PERMISSIONS in ops/deploy/iam_delta.py; tests/repository holds this copy
# equal to it). A custom role a row binds is held to its pin unless the delta itself
# defines the role under custom_roles, and a delta definition must equal the pin.
_IAM_E_PINNED_CUSTOM_ROLES = {
    f"projects/{PROJECT}/roles/SourceSnapshotCreate": (
        "bigquery.datasets.get",
        "bigquery.tables.create",
        "bigquery.tables.createSnapshot",
        "bigquery.tables.get",
        "bigquery.tables.getData",
    ),
    f"projects/{PROJECT}/roles/CaptureJobRead": ("bigquery.jobs.get",),
}
_IAM_E_PINNED_DESCRIPTION = "Holds exactly the permissions the 42 iam delta validator pins."
_IAM_E_LEGACY = {"roles/bigquery.dataViewer": "READER", "roles/bigquery.dataEditor": "WRITER"}
_IAM_E_FROM_LEGACY = {
    "READER": "roles/bigquery.dataViewer",
    "WRITER": "roles/bigquery.dataEditor",
    "OWNER": "roles/bigquery.dataOwner",
}
_IAM_E_P = re.escape(PROJECT)
_IAM_E_BQ = rf"//bigquery\.googleapis\.com/projects/{_IAM_E_P}/datasets/"
_IAM_E_GRAMMARS = (
    ("routine", re.compile(rf"{_IAM_E_BQ}{DATASET}/routines/([a-z][a-z0-9_]{{0,255}})")),
    (
        "table",
        re.compile(
            rf"{_IAM_E_BQ}([a-z][a-z0-9_]{{0,1023}}/tables/[A-Za-z_][A-Za-z0-9_]{{0,1023}})"
        ),
    ),
    ("dataset", re.compile(rf"{_IAM_E_BQ}([a-z][a-z0-9_]{{0,1023}})")),
    (
        "bucket",
        re.compile(
            r"//storage\.googleapis\.com/projects/_/buckets/([a-z0-9][a-z0-9_-]{1,61}[a-z0-9])"
        ),
    ),
    (
        "runtime_job",
        re.compile(
            rf"//run\.googleapis\.com/projects/{_IAM_E_P}/locations/us-central1/jobs/"
            r"([a-z][a-z0-9-]{0,61}[a-z0-9])"
        ),
    ),
    ("project", re.compile(rf"//cloudresourcemanager\.googleapis\.com/projects/({_IAM_E_P})")),
)
_IAM_E_BUCKET_REFERENCE = re.compile(r"projects/_/buckets/([^/'\"]*)")
_IAM_E_SERVICE_ACCOUNT = re.compile(
    rf"serviceAccount:[a-z][a-z0-9-]{{4,28}}[a-z0-9]@{_IAM_E_P}\.iam\.gserviceaccount\.com"
)
# Custom roles first, since a binding may name one; the project policy last.
_IAM_E_KIND_ORDER = {
    "custom_role": 0,
    "authorization": 1,
    "routine": 2,
    "dataset": 3,
    "table": 4,
    "bucket": 5,
    "runtime_job": 6,
    "project": 7,
}
_IAM_E_POLICY_KINDS = ("routine", "table", "bucket", "runtime_job", "project")


def _iam_e_refusal(name: str) -> MigrationRefusal:
    return MigrationRefusal(f"execution_approval_iam_e_{name}")


class _IamEDetailRefusal(MigrationRefusal):
    """A refusal that names what it refused on."""

    def __init__(self, code: str, detail: Mapping[str, object]):
        super().__init__(code)
        self.detail = dict(detail)


@dataclass(frozen=True)
class IamEAuthorization:
    routine: str
    role: str | None
    routine_type: str | None


@dataclass(frozen=True)
class IamERow:
    kind: str
    ident: str
    member: str
    resource: str
    role: str
    condition: str | None


@dataclass(frozen=True)
class IamECustomRole:
    role: str
    role_id: str
    title: str
    description: str
    permissions: tuple[str, ...]


@dataclass(frozen=True)
class IamETargets:
    delta_sha256: str
    manifest_sha256: str
    approval_state: str
    custom_roles: tuple[IamECustomRole, ...]
    authorizations: tuple[IamEAuthorization, ...]
    bindings: tuple[IamERow, ...]
    # The last amendment, when it alone is proposed over approved blocks: the one a
    # grant may apply.
    pending_amendment: str | None = None


def _iam_e_read(path: Path, code: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise _iam_e_refusal(code) from exc


def _iam_e_parse(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, ValueError, MigrationRefusal) as exc:
        raise _iam_e_refusal("delta_invalid") from exc
    if not isinstance(value, dict):
        raise _iam_e_refusal("delta_invalid")
    return value


def _iam_e_canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _iam_e_approval_state(delta: Mapping[str, object]) -> str:
    approval = delta.get("approval")
    if not isinstance(approval, dict) or set(approval) != {
        "applied",
        "approved_at",
        "approved_by",
        "state",
    }:
        raise _iam_e_refusal("delta_invalid")
    if approval["applied"] is not False:
        raise _iam_e_refusal("delta_invalid")
    recorded = all(
        isinstance(approval[key], str) and approval[key] for key in ("approved_at", "approved_by")
    )
    unrecorded = approval["approved_at"] is None and approval["approved_by"] is None
    if approval["state"] == "approved" and recorded:
        return "approved"
    if approval["state"] == "proposed" and unrecorded:
        return "proposed"
    raise _iam_e_refusal("delta_invalid")


def _iam_e_kind(resource: object) -> tuple[str, str]:
    if not isinstance(resource, str) or not resource.isascii():
        raise _iam_e_refusal("resource_refused")
    for kind, grammar in _IAM_E_GRAMMARS:
        match = grammar.fullmatch(resource)
        if match is not None:
            return kind, match.group(1)
    raise _iam_e_refusal("resource_refused")


def _iam_e_staging(kind: str, ident: str) -> None:
    # Production is never a target: every named dataset, table, bucket and job is a
    # staging one, and nothing named for production passes whatever the manifest says.
    # A routine's grammar pins the staging approvals dataset, and a project row admits
    # only the builds viewer or a custom role the delta defines.
    if kind in {"project", "routine"}:
        return
    name = ident.split("/tables/", 1)[0] if kind == "table" else ident
    tokens = re.split(r"[_.-]", name)
    if "staging" not in tokens or any(token.startswith("prod") for token in tokens):
        raise _iam_e_refusal("resource_refused")


def _iam_e_role(
    kind: str,
    role: object,
    custom: Mapping[str, IamECustomRole],
    *,
    member: str | None = None,
) -> str:
    if not isinstance(role, str):
        raise _iam_e_refusal("role_refused")
    if role in _IAM_E_KIND_ROLES[kind] or role in _IAM_E_BOUND_ONLY_ROLES.get(kind, ()):
        return role
    if member is not None and member in _IAM_E_MEMBER_ROLES.get(kind, {}).get(role, ()):
        return role
    if kind != "routine" and role in custom:
        return role
    raise _iam_e_refusal("role_refused")


def _iam_e_condition(kind: str, ident: str, condition: object) -> str | None:
    if kind != "bucket":
        if kind == "project" and condition in _IAM_E_PROJECT_CONDITIONS:
            return condition
        if condition is not None:
            raise _iam_e_refusal("condition_refused")
        return None
    # A bucket row is conditioned as the delta states it, and the condition may name
    # this bucket's objects only.
    if not isinstance(condition, str) or not condition or condition != condition.strip():
        raise _iam_e_refusal("condition_refused")
    buckets = _IAM_E_BUCKET_REFERENCE.findall(condition)
    if not buckets or any(name != ident for name in buckets):
        raise _iam_e_refusal("condition_refused")
    return condition


def _iam_e_member(kind: str, ident: str, member: object, identities: frozenset[str]) -> str:
    if not isinstance(member, str):
        raise _iam_e_refusal("member_refused")
    if member == _IAM_E_OPERATOR and kind == "routine" and ident in _IAM_E_OPERATOR_ROUTINES:
        return member
    if _IAM_E_SERVICE_ACCOUNT.fullmatch(member) and member in identities:
        return member
    raise _iam_e_refusal("member_refused")


def _iam_e_manifest(digest: object) -> tuple[frozenset[str], frozenset[str]]:
    raw = _iam_e_read(RESOURCE_MANIFEST_PATH, "manifest_digest_mismatch")
    # The manifest is bound by the digest the delta names, recomputed from its bytes.
    if not isinstance(digest, str) or hashlib.sha256(raw).hexdigest() != digest:
        raise _iam_e_refusal("manifest_digest_mismatch")
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        names = frozenset(str(row["name"]) for row in manifest["resources"])
        identities = frozenset(
            "serviceAccount:" + str(name).rsplit("/", 1)[1]
            for name in manifest["identities"].values()
        )
    except (UnicodeError, ValueError, KeyError, TypeError, AttributeError, MigrationRefusal) as exc:
        raise _iam_e_refusal("manifest_digest_mismatch") from exc
    return names, identities


def _iam_e_custom_role(row: object) -> IamECustomRole:
    if not isinstance(row, dict) or set(row) != {"description", "permissions", "role", "title"}:
        raise _iam_e_refusal("delta_invalid")
    match = _IAM_E_CUSTOM_ROLE.fullmatch(str(row["role"]))
    title, description, permissions = row["title"], row["description"], row["permissions"]
    if (
        match is None
        or not isinstance(title, str)
        or not 0 < len(title) <= 100
        or not isinstance(description, str)
        or len(description) > 256
        or not isinstance(permissions, list)
        or not permissions
        or permissions != sorted(set(permissions))
    ):
        raise _iam_e_refusal("delta_invalid")
    for permission in permissions:
        if not isinstance(permission, str) or not _IAM_E_PERMISSION.fullmatch(permission):
            raise _iam_e_refusal("delta_invalid")
        if permission.startswith(_IAM_E_ESCALATING_SERVICES) or permission.endswith(
            ".setIamPolicy"
        ):
            raise _iam_e_refusal("role_refused")
    return IamECustomRole(row["role"], match.group(1), title, description, tuple(permissions))


def _iam_e_authorization(row: object, names: frozenset[str]) -> IamEAuthorization:
    if not isinstance(row, dict) or set(row) not in (
        {"dataset", "role", "routine"},
        {"dataset", "role", "routine", "routine_type"},
    ):
        raise _iam_e_refusal("delta_invalid")
    if row["dataset"] != _V2_IAM_DATASET:
        raise _iam_e_refusal("delta_invalid")
    kind, name = _iam_e_kind(row["routine"])
    if kind != "routine" or row["routine"] not in names:
        raise _iam_e_refusal("resource_refused")
    role, routine_type = row["role"], row.get("routine_type")
    if role is None:
        # A function takes no role on its dataset entry; the row records that it is one.
        if routine_type not in _V2_FUNCTION_TYPES:
            raise _iam_e_refusal("delta_invalid")
    elif role not in _V2_IAM_AUTHORIZATION_ROLES or "routine_type" in row:
        raise _iam_e_refusal("delta_invalid")
    return IamEAuthorization(name, role, routine_type)


def _iam_e_binding(
    row: object,
    names: frozenset[str],
    identities: frozenset[str],
    forbidden: frozenset[str],
    custom: Mapping[str, IamECustomRole],
) -> IamERow:
    if not isinstance(row, dict) or set(row) != {
        "condition",
        "member",
        "purpose",
        "resource",
        "role",
    }:
        raise _iam_e_refusal("delta_invalid")
    resource = row["resource"]
    if resource in forbidden:
        raise _iam_e_refusal("delta_invalid")
    kind, ident = _iam_e_kind(resource)
    if resource not in names:
        raise _iam_e_refusal("resource_refused")
    _iam_e_staging(kind, ident)
    member = _iam_e_member(kind, ident, row["member"], identities)
    role = _iam_e_role(kind, row["role"], custom, member=member)
    condition = _iam_e_condition(kind, ident, row["condition"])
    return IamERow(kind, ident, member, resource, role, condition)


def _iam_e_amendments(
    delta: Mapping[str, object], state: str
) -> tuple[str, list[object], str | None]:
    """The delta's state over every approval block, the rows its amendments carry, and
    the last amendment when it alone is proposed over approved blocks."""

    if "amendments" not in delta:
        return state, [], None
    entries = delta["amendments"]
    if not isinstance(entries, list) or not entries:
        raise _iam_e_refusal("delta_invalid")
    names: list[str] = []
    rows: list[object] = []
    pending: str | None = None
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _IAM_E_AMENDMENT_KEYS:
            raise _iam_e_refusal("delta_invalid")
        name = entry["amendment"]
        if not isinstance(name, str) or not _IAM_E_AMENDMENT_NAME.fullmatch(name):
            raise _iam_e_refusal("delta_invalid")
        if names and name <= names[-1]:
            raise _iam_e_refusal("delta_invalid")
        names.append(name)
        current = _iam_e_approval_state(entry)
        # An amendment is approved only over an approved file and approved earlier ones.
        if current == "approved" and state != "approved":
            raise _iam_e_refusal("delta_invalid")
        pending = name if current == "proposed" and state == "approved" else None
        state = current
        if not isinstance(entry["bindings"], list) or not entry["bindings"]:
            raise _iam_e_refusal("delta_invalid")
        rows.extend(entry["bindings"])
    return state, rows, pending


def _iam_e_amendment_scope(
    delta: Mapping[str, object], names: frozenset[str]
) -> tuple[dict[str, frozenset[str]], frozenset[str]]:
    """The resource names each amendment row may name (the manifest's, plus those pinned
    for its own amendment), and the amendment d grants the delta's amendments serve
    again. Called only after the amendments list is checked."""

    scoped: dict[str, frozenset[str]] = {}
    served: set[str] = set()
    for entry in delta.get("amendments", ()):
        for row in _IAM_E_UNAPPLIED_BASE_ROWS.get(entry["amendment"], ()):
            served.add(
                _iam_e_canonical(dict(zip(("condition", "member", "resource", "role"), row)))
            )
        extra = _IAM_E_AMENDMENT_RESOURCES.get(entry["amendment"])
        if extra is None:
            continue
        for row in entry["bindings"]:
            scoped[_iam_e_canonical(row)] = names | extra
    return scoped, frozenset(served)


def iam_e_targets() -> IamETargets:
    """Return exactly the rows the delta read now adds over the pinned amendment d delta."""

    raw = _iam_e_read(IAM_E_DELTA_PATH, "delta_invalid")
    base_raw = _iam_e_read(IAM_E_BASE_PATH, "delta_digest_mismatch")
    # The base is history and stays pinned; the delta is bound at apply by its digest.
    if hashlib.sha256(base_raw).hexdigest() != IAM_E_BASE_SHA256:
        raise _iam_e_refusal("delta_digest_mismatch")
    delta, base = _iam_e_parse(raw), _iam_e_parse(base_raw)
    manifest_sha256 = delta.get("resource_manifest_sha256")
    names, identities = _iam_e_manifest(manifest_sha256)
    state = _iam_e_approval_state(delta)
    if (delta.get("project"), delta.get("contract_version")) != (PROJECT, "42_iam_delta_v1"):
        raise _iam_e_refusal("delta_invalid")
    state, amended_rows, pending = _iam_e_amendments(delta, state)
    for source in (delta, base):
        for group in ("bindings", "routine_authorizations", "must_not_grant"):
            if not isinstance(source.get(group), list):
                raise _iam_e_refusal("delta_invalid")
    roles = delta.get("custom_roles", [])
    if not isinstance(roles, list):
        raise _iam_e_refusal("delta_invalid")
    custom_roles = [_iam_e_custom_role(row) for row in roles]
    custom = {item.role: item for item in custom_roles}
    if len(custom) != len(custom_roles):
        raise _iam_e_refusal("delta_invalid")
    for item in custom_roles:
        pinned = _IAM_E_PINNED_CUSTOM_ROLES.get(item.role)
        if pinned is not None and pinned != item.permissions:
            raise _iam_e_refusal("delta_invalid")
    forbidden = frozenset(
        str(item.get("resource")) for item in delta["must_not_grant"] if isinstance(item, dict)
    )
    rows = [*delta["bindings"], *amended_rows]
    keys = [_iam_e_canonical(row) for row in rows]
    base_keys = {_iam_e_canonical(row) for row in base["bindings"]}
    bound = {
        row.get("role")
        for row, key in zip(rows, keys)
        if isinstance(row, dict) and key not in base_keys
    }
    for role in sorted(_IAM_E_PINNED_CUSTOM_ROLES):
        if role in bound and role not in custom:
            item = _iam_e_custom_role(
                {
                    "role": role,
                    "title": role.rsplit("/", 1)[1],
                    "description": _IAM_E_PINNED_DESCRIPTION,
                    "permissions": list(_IAM_E_PINNED_CUSTOM_ROLES[role]),
                }
            )
            custom_roles.append(item)
            custom[role] = item
    # The words only ever add: an amendment d binding the delta no longer carries would
    # have to be removed, so such a delta is refused whole.
    if not base_keys <= set(keys):
        raise _iam_e_refusal("delta_invalid")
    grants = [
        _iam_e_canonical({k: row.get(k) for k in ("condition", "member", "resource", "role")})
        if isinstance(row, dict)
        else _iam_e_canonical(row)
        for row in rows
    ]
    if len(set(grants)) != len(grants):
        raise _iam_e_refusal("delta_invalid")
    scoped, served = _iam_e_amendment_scope(delta, names)
    bindings = tuple(
        _iam_e_binding(row, scoped.get(key, names), identities, forbidden, custom)
        for row, key, grant in zip(rows, keys, grants)
        if key not in base_keys or grant in served
    )
    rows = delta["routine_authorizations"]
    keys = [_iam_e_canonical(row) for row in rows]
    base_keys = {_iam_e_canonical(row) for row in base["routine_authorizations"]}
    authorizations = tuple(
        _iam_e_authorization(row, names) for row, key in zip(rows, keys) if key not in base_keys
    )
    routines = [item.routine for item in authorizations]
    if len(set(routines)) != len(routines):
        raise _iam_e_refusal("delta_invalid")
    # An amendment d authorization may only leave the delta restated for its routine.
    for row in base["routine_authorizations"]:
        if _iam_e_canonical(row) not in set(keys):
            routine = row.get("routine") if isinstance(row, dict) else None
            if not isinstance(routine, str) or routine.rsplit("/", 1)[-1] not in routines:
                raise _iam_e_refusal("delta_invalid")
    return IamETargets(
        hashlib.sha256(raw).hexdigest(),
        str(manifest_sha256),
        state,
        tuple(custom_roles),
        authorizations,
        bindings,
        pending,
    )


_IAM_E_PREFIXES = {
    "dataset": f"//bigquery.googleapis.com/projects/{PROJECT}/datasets/",
    "table": f"//bigquery.googleapis.com/projects/{PROJECT}/datasets/",
    "bucket": "//storage.googleapis.com/projects/_/buckets/",
    "runtime_job": f"//run.googleapis.com/projects/{PROJECT}/locations/us-central1/jobs/",
}


class _IamEAdapter(_V2IamAdapter):
    """Etag guarded reads and writes of every kind the iam-e words serve."""

    _BIGQUERY = f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/datasets/"
    _STORAGE = "https://storage.googleapis.com/storage/v1/b/"
    _RUN = f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/us-central1/jobs/"
    _ROLES = f"https://iam.googleapis.com/v1/projects/{PROJECT}/roles"
    _PROJECT = f"https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT}"

    @classmethod
    def _url(cls, kind: str, ident: str) -> str:
        # Every identifier reaches a URL only after it matched its own grammar again.
        if kind == "custom_role":
            if not _IAM_E_CUSTOM_ROLE.fullmatch(f"projects/{PROJECT}/roles/{ident}"):
                raise MigrationRefusal("execution_approval_target_invalid")
            return f"{cls._ROLES}/{ident}"
        grammar = dict(_IAM_E_GRAMMARS)[kind]
        if not grammar.fullmatch(_IAM_E_PREFIXES[kind] + ident):
            raise MigrationRefusal("execution_approval_target_invalid")
        return {
            "dataset": cls._BIGQUERY + ident,
            "table": cls._BIGQUERY + ident,
            "bucket": cls._STORAGE + ident + "/iam",
            "runtime_job": cls._RUN + ident,
        }[kind]

    def dataset(self, ident: str) -> dict[str, object] | None:
        if ident == DATASET:
            etag, access = self.dataset_access()
            return {"etag": etag, "access": access}
        response = self._session.get(self._url("dataset", ident), params={"accessPolicyVersion": 3})
        if getattr(response, "status_code", None) == 404:
            return None
        payload = _response_json(response)
        etag, access = payload.get("etag"), payload.get("access")
        if not isinstance(etag, str) or not etag or not isinstance(access, list):
            raise _iam_e_refusal("readback_mismatch")
        return {"etag": etag, "access": [dict(item) for item in access]}

    def patch_dataset(self, ident: str, etag: str, access: Sequence[Mapping[str, object]]) -> None:
        if ident == DATASET:
            self.patch_dataset_access(etag, access)
            return
        response = self._session.patch(
            self._url("dataset", ident),
            params={"fields": "access", "accessPolicyVersion": 3},
            json={"access": [dict(item) for item in access]},
            headers={"If-Match": etag},
        )
        _response_json(response)

    def exists(self, kind: str, ident: str) -> bool:
        """Whether the resource itself is there, read from the resource and not its policy."""

        if kind not in {"table", "bucket", "runtime_job"}:
            raise _iam_e_refusal("resource_refused")
        url = self._url(kind, ident)
        response = self._session.get(url.removesuffix("/iam") if kind == "bucket" else url)
        if getattr(response, "status_code", None) == 404:
            return False
        _response_json(response)
        return True

    def policy_if_exists(self, kind: str, ident: str) -> dict[str, object] | None:
        if kind == "routine":
            return self.routine_policy_if_exists(_routine_resource(ident))
        if kind == "project":
            return self.project_policy()
        # A policy read may answer a resource that does not exist with an empty policy.
        if not self.exists(kind, ident):
            return None
        if kind == "bucket":
            response = self._session.get(
                self._url(kind, ident), params={"optionsRequestedPolicyVersion": 3}
            )
        elif kind == "runtime_job":
            response = self._session.get(
                self._url(kind, ident) + ":getIamPolicy",
                params={"options.requestedPolicyVersion": 3},
            )
        elif kind == "table":
            response = self._session.post(
                self._url(kind, ident) + ":getIamPolicy",
                json={"options": {"requestedPolicyVersion": 3}},
            )
        else:
            raise _iam_e_refusal("resource_refused")
        if getattr(response, "status_code", None) == 404:
            return None
        return self._policy(_response_json(response))

    def set_policy(self, kind: str, ident: str, policy: Mapping[str, object]) -> None:
        if kind == "routine":
            self.set_routine_policy(_routine_resource(ident), policy)
            return
        if kind == "bucket":
            _response_json(self._session.put(self._url(kind, ident), json=dict(policy)))
            return
        if kind == "project":
            url = self._PROJECT + ":setIamPolicy"
        elif kind in {"table", "runtime_job"}:
            url = self._url(kind, ident) + ":setIamPolicy"
        else:
            raise _iam_e_refusal("write_refused")
        _response_json(self._session.post(url, json={"policy": dict(policy)}))

    def custom_role_if_exists(self, role_id: str) -> dict[str, object] | None:
        response = self._session.get(self._url("custom_role", role_id))
        if getattr(response, "status_code", None) == 404:
            return None
        payload = _response_json(response)
        if not isinstance(payload.get("etag"), str) or not isinstance(
            payload.get("includedPermissions", []), list
        ):
            raise _iam_e_refusal("readback_mismatch")
        return dict(payload)

    def create_custom_role(self, role_id: str, body: Mapping[str, object]) -> None:
        self._url("custom_role", role_id)
        _response_json(self._session.post(self._ROLES, json=dict(body)))

    def update_custom_role(self, role_id: str, body: Mapping[str, object]) -> None:
        response = self._session.patch(
            self._url("custom_role", role_id),
            params={"updateMask": "includedPermissions"},
            json=dict(body),
        )
        _response_json(response)


def _iam_e_adapter(credentials: object) -> _IamEAdapter:
    return _IamEAdapter(_authorized_session(credentials))


@dataclass
class _IamEState:
    custom_roles: dict[str, dict[str, object] | None]
    routine_types: dict[str, str | None]
    datasets: dict[str, dict[str, object] | None]
    policies: dict[tuple[str, str], dict[str, object] | None]


def _iam_e_routines(targets: IamETargets) -> list[str]:
    names = {item.routine for item in targets.authorizations}
    names |= {row.ident for row in targets.bindings if row.kind == "routine"}
    return sorted(names)


def _iam_e_read_state(adapter: _IamEAdapter, targets: IamETargets) -> _IamEState:
    custom_roles = {
        item.role: adapter.custom_role_if_exists(item.role_id) for item in targets.custom_roles
    }
    routine_types = {
        name: adapter.routine_type_if_exists(_routine_resource(name))
        for name in _iam_e_routines(targets)
    }
    datasets = {}
    if targets.authorizations:
        datasets[DATASET] = adapter.dataset(DATASET)
    for ident in sorted({row.ident for row in targets.bindings if row.kind == "dataset"}):
        datasets[ident] = adapter.dataset(ident)
    policies: dict[tuple[str, str], dict[str, object] | None] = {}
    for kind, ident in sorted({(row.kind, row.ident) for row in targets.bindings}):
        if kind == "dataset":
            continue
        if kind == "routine" and routine_types[ident] is None:
            policies[(kind, ident)] = None
            continue
        policies[(kind, ident)] = adapter.policy_if_exists(kind, ident)
    return _IamEState(custom_roles, routine_types, datasets, policies)


def _iam_e_entry_member(entry: Mapping[str, object]) -> str:
    if "userByEmail" in entry:
        email = str(entry["userByEmail"])
        return ("serviceAccount:" if email.endswith(".gserviceaccount.com") else "user:") + email
    if "iamMember" in entry:
        return str(entry["iamMember"])
    return "entry:" + _iam_e_canonical(entry)


def _iam_e_access_grants(access: Sequence[Mapping[str, object]]) -> set[tuple[str, str, str]]:
    rows = set()
    for entry in access:
        if not isinstance(entry, dict):
            raise _iam_e_refusal("readback_mismatch")
        if "routine" in entry:
            continue
        role = str(entry.get("role"))
        condition = entry.get("condition")
        expression = _iam_e_canonical(condition) if condition is not None else ""
        rows.add((_IAM_E_FROM_LEGACY.get(role, role), _iam_e_entry_member(entry), expression))
    return rows


def _iam_e_policy_grants(policy: Mapping[str, object]) -> set[tuple[str, str, str]]:
    """(role, member, condition expression) for every grant a policy holds."""

    rows = set()
    for item in policy.get("bindings", ()):
        if not isinstance(item, dict) or not isinstance(item.get("members", []), list):
            raise _iam_e_refusal("readback_mismatch")
        condition = item.get("condition")
        if condition is not None and (
            not isinstance(condition, dict) or not isinstance(condition.get("expression"), str)
        ):
            raise _iam_e_refusal("readback_mismatch")
        expression = "" if condition is None else condition["expression"]
        for member in item.get("members", ()):
            rows.add((str(item.get("role")), str(member), expression))
    return rows


def _iam_e_custom_role_state(state: _IamEState, item: IamECustomRole) -> str:
    live = state.custom_roles[item.role]
    if live is None:
        return "missing"
    if live.get("name") != item.role:
        raise _iam_e_refusal("readback_mismatch")
    # A deleted or disabled role is never brought back here.
    if live.get("deleted", False) is not False or live.get("stage") == "DISABLED":
        raise _iam_e_refusal("custom_role_unusable")
    permissions = live.get("includedPermissions", [])
    if sorted(permissions) == list(item.permissions) and len(set(permissions)) == len(permissions):
        return "present"
    return "differs"


def _iam_e_authorization_state(state: _IamEState, item: IamEAuthorization) -> str:
    routine_type = state.routine_types[item.routine]
    if routine_type is None:
        return "routine_missing"
    # The routine type is read live, never taken from the row or the name.
    is_function = _is_function_type(routine_type)
    if item.role is None and routine_type != item.routine_type:
        raise _iam_e_refusal("routine_type_mismatch")
    if item.role is not None and is_function:
        raise _iam_e_refusal("routine_type_mismatch")
    ref = (PROJECT, DATASET, item.routine)
    found = [
        entry for entry in state.datasets[DATASET]["access"] if _access_routine_ref(entry) == ref
    ]
    for entry in found:
        if entry.get("role") != item.role:
            raise _iam_e_refusal("authorization_role_mismatch")
    return "present" if found else "missing"


def _iam_e_row_state(state: _IamEState, row: IamERow) -> str:
    if row.kind == "routine" and state.routine_types[row.ident] is None:
        return "routine_missing"
    grant = (row.role, row.member, row.condition or "")
    if row.kind == "dataset":
        dataset = state.datasets[row.ident]
        if dataset is None:
            return "resource_missing"
        held = _iam_e_access_grants(dataset["access"])
    else:
        policy = state.policies[(row.kind, row.ident)]
        if policy is None:
            return "resource_missing"
        held = _iam_e_policy_grants(policy)
    return "present" if grant in held else "missing"


def _iam_e_condition_json(expression: str) -> dict[str, str]:
    # IAM needs a title on a conditional binding and the delta carries none, so it is
    # derived from the expression the way the iam-delta words derive it.
    title = "42 iam delta v1 " + hashlib.sha256(expression.encode("utf-8")).hexdigest()[:16]
    return {"title": title, "expression": expression}


def _iam_e_revised_policy(
    policy: Mapping[str, object], additions: Sequence[IamERow]
) -> dict[str, object]:
    revised = json.loads(json.dumps(policy))
    bindings = list(revised.get("bindings", ()))
    for row in additions:
        condition = None if row.condition is None else _iam_e_condition_json(row.condition)
        match = next(
            (
                item
                for item in bindings
                if item.get("role") == row.role
                and (item.get("condition") or {}).get("expression") == (row.condition or None)
                and (condition is None) == (item.get("condition") is None)
            ),
            None,
        )
        if match is None:
            item = {"role": row.role, "members": [row.member]}
            if condition is not None:
                item["condition"] = condition
            bindings.append(item)
        else:
            match["members"] = sorted({*match.get("members", ()), row.member})
    revised["bindings"] = bindings
    if any(item.get("condition") is not None for item in bindings):
        revised["version"] = 3
    return revised


def _iam_e_revised_access(
    access: Sequence[Mapping[str, object]], entries: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    return [*(dict(item) for item in access), *(dict(item) for item in entries)]


def _iam_e_access_entry(row: IamERow) -> dict[str, object]:
    email = row.member.removeprefix("serviceAccount:")
    return {"role": _IAM_E_LEGACY.get(row.role, row.role), "userByEmail": email}


def _iam_e_authorization_entry(item: IamEAuthorization) -> dict[str, object]:
    entry: dict[str, object] = {
        "routine": {"projectId": PROJECT, "datasetId": DATASET, "routineId": item.routine}
    }
    if item.role is not None:
        entry = {"role": item.role, **entry}
    return entry


def _iam_e_guard_policy(
    kind: str,
    before: Mapping[str, object],
    after: Mapping[str, object],
    additions: Sequence[IamERow],
    permitted: frozenset[IamERow],
) -> None:
    """The send path check for one policy: exactly these rows are added, nothing else moves."""

    changed = _iam_e_refusal("project_binding_changed" if kind == "project" else "write_refused")
    for row in additions:
        if row not in permitted:
            raise _iam_e_refusal("write_refused")
    old = list(before.get("bindings", ()))
    new = list(after.get("bindings", ()))
    if len(new) < len(old):
        raise changed
    # Every key but the bindings and the version is kept as read, the etag among them.
    for key in set(before) | set(after):
        if key in {"bindings", "version"}:
            continue
        if before.get(key) != after.get(key):
            raise changed
    # The version is kept as read, or raised to 3 for a conditional binding; never lowered.
    version_before, version_after = before.get("version", 1), after.get("version", 1)
    if type(version_after) is not int or version_after not in {version_before, 3}:
        raise changed
    for kept, now in zip(old, new):
        if not isinstance(now, dict) or {k: v for k, v in kept.items() if k != "members"} != {
            k: v for k, v in now.items() if k != "members"
        }:
            raise changed
        if not set(kept.get("members", ())) <= set(now.get("members", ())):
            raise changed
    if not _iam_e_policy_grants(before) <= _iam_e_policy_grants(after):
        raise changed
    added = _iam_e_policy_grants(after) - _iam_e_policy_grants(before)
    if added != {(row.role, row.member, row.condition or "") for row in additions}:
        raise _iam_e_refusal("write_refused")
    if any(item.get("condition") is not None for item in new) and version_after != 3:
        raise _iam_e_refusal("write_refused")


def _iam_e_guard_access(
    before: Sequence[Mapping[str, object]],
    after: Sequence[Mapping[str, object]],
    expected: Sequence[Mapping[str, object]],
) -> None:
    if [_iam_e_canonical(item) for item in after] != [
        *(_iam_e_canonical(item) for item in before),
        *(_iam_e_canonical(item) for item in expected),
    ]:
        raise _iam_e_refusal("write_refused")


def _iam_e_role_body(item: IamECustomRole, live: Mapping[str, object] | None) -> dict:
    permissions = list(item.permissions)
    if live is None:
        return {
            "roleId": item.role_id,
            "role": {
                "title": item.title,
                "description": item.description,
                "includedPermissions": permissions,
                "stage": "GA",
            },
        }
    # Only the permission list changes, under the etag the role was read with.
    return {"includedPermissions": permissions, "etag": live["etag"]}


def _iam_e_guard_role(
    item: IamECustomRole, live: Mapping[str, object] | None, body: Mapping[str, object]
) -> None:
    written = body["role"]["includedPermissions"] if live is None else body["includedPermissions"]
    if list(written) != list(item.permissions):
        raise _iam_e_refusal("write_refused")
    if live is None and (
        set(body) != {"roleId", "role"}
        or body["roleId"] != item.role_id
        or body["role"]
        != {
            "title": item.title,
            "description": item.description,
            "includedPermissions": list(item.permissions),
            "stage": "GA",
        }
    ):
        raise _iam_e_refusal("write_refused")
    if live is not None and (
        set(body) != {"includedPermissions", "etag"} or body["etag"] != live["etag"]
    ):
        raise _iam_e_refusal("write_refused")


@dataclass(frozen=True)
class _IamEWrite:
    kind: str
    ident: str
    etag: str
    payload: dict[str, object]
    added: int


def _iam_e_writes(targets: IamETargets, state: _IamEState) -> list[_IamEWrite]:
    """Every write the apply would send, built and checked on the one send path."""

    permitted = frozenset(targets.bindings)
    writes: list[_IamEWrite] = []
    for item in targets.custom_roles:
        if _iam_e_custom_role_state(state, item) == "present":
            continue
        live = state.custom_roles[item.role]
        body = _iam_e_role_body(item, live)
        _iam_e_guard_role(item, live, body)
        etag = "" if live is None else str(live["etag"])
        writes.append(_IamEWrite("custom_role", item.role_id, etag, body, 1))
    by_dataset: dict[str, list[dict[str, object]]] = {}
    for auth in targets.authorizations:
        if _iam_e_authorization_state(state, auth) == "missing":
            by_dataset.setdefault(DATASET, []).append(_iam_e_authorization_entry(auth))
    by_policy: dict[tuple[str, str], list[IamERow]] = {}
    for row in targets.bindings:
        if _iam_e_row_state(state, row) != "missing":
            continue
        if row.kind == "dataset":
            by_dataset.setdefault(row.ident, []).append(_iam_e_access_entry(row))
        else:
            by_policy.setdefault((row.kind, row.ident), []).append(row)
    for ident, entries in by_dataset.items():
        before = state.datasets[ident]["access"]
        after = _iam_e_revised_access(before, entries)
        _iam_e_guard_access(before, after, entries)
        kind = "authorization" if ident == DATASET else "dataset"
        writes.append(
            _IamEWrite(
                kind,
                ident,
                str(state.datasets[ident]["etag"]),
                {"access": after},
                len(entries),
            )
        )
    for (kind, ident), rows in by_policy.items():
        before = state.policies[(kind, ident)]
        after = _iam_e_revised_policy(before, rows)
        _iam_e_guard_policy(kind, before, after, rows, permitted)
        writes.append(_IamEWrite(kind, ident, str(before["etag"]), after, len(rows)))
    return sorted(writes, key=lambda item: (_IAM_E_KIND_ORDER[item.kind], item.ident))


def _iam_e_send(adapter: _IamEAdapter, write: _IamEWrite) -> None:
    if write.kind == "custom_role":
        if write.etag:
            adapter.update_custom_role(write.ident, write.payload)
        else:
            adapter.create_custom_role(write.ident, write.payload)
    elif write.kind in {"authorization", "dataset"}:
        adapter.patch_dataset(write.ident, write.etag, write.payload["access"])
    elif write.kind in _IAM_E_POLICY_KINDS:
        adapter.set_policy(write.kind, write.ident, write.payload)
    else:
        raise _iam_e_refusal("write_refused")


def _iam_e_view(write: _IamEWrite) -> dict[str, object]:
    """The full policy, access list or role to write, without its etag: what Albert reads."""

    payload = {key: value for key, value in write.payload.items() if key != "etag"}
    if "bindings" in payload:
        payload["bindings"] = [
            {**item, "members": sorted(item.get("members", ()))} for item in payload["bindings"]
        ]
    return {"kind": write.kind, "resource": write.ident, "policy": payload}


def _iam_e_state_digest(state: _IamEState) -> str:
    return _digest(
        {
            "custom_roles": {
                role: None if live is None else sorted(live.get("includedPermissions", []))
                for role, live in state.custom_roles.items()
            },
            "routine_types": state.routine_types,
            "datasets": {
                ident: None
                if value is None
                else sorted(_iam_e_canonical(entry) for entry in value["access"])
                for ident, value in state.datasets.items()
            },
            "policies": {
                f"{kind}:{ident}": None
                if policy is None
                else sorted(list(row) for row in _iam_e_policy_grants(policy))
                for (kind, ident), policy in state.policies.items()
            },
        }
    )


def _iam_e_row_view(row: IamERow, state: str) -> dict[str, object]:
    return {
        "kind": row.kind,
        "member": row.member,
        "resource": row.resource,
        "role": row.role,
        "condition": row.condition,
        "state": state,
    }


def _iam_e_states(targets: IamETargets, state: _IamEState):
    roles = [(item, _iam_e_custom_role_state(state, item)) for item in targets.custom_roles]
    auth = [(item, _iam_e_authorization_state(state, item)) for item in targets.authorizations]
    rows = [(row, _iam_e_row_state(state, row)) for row in targets.bindings]
    return roles, auth, rows


_IAM_E_STATES = ("present", "missing", "differs", "routine_missing", "resource_missing")


def _iam_e_counts(roles, auth, rows) -> dict[str, dict[str, int]]:
    kinds = ("custom_role", "authorization", "routine", "dataset", "table", "bucket")
    counts = {
        kind: dict.fromkeys(("target", *_IAM_E_STATES), 0)
        for kind in (*kinds, "runtime_job", "project")
    }
    entries = (
        [("custom_role", value) for _item, value in roles]
        + [("authorization", value) for _item, value in auth]
        + [(row.kind, value) for row, value in rows]
    )
    for kind, value in entries:
        counts[kind]["target"] += 1
        counts[kind][value] += 1
    return {kind: item for kind, item in counts.items() if item["target"]}


def _iam_e_missing(state: _IamEState, rows) -> tuple[list[str], list[str]]:
    routines = sorted(name for name, kind in state.routine_types.items() if kind is None)
    resources = sorted({row.resource for row, value in rows if value == "resource_missing"})
    return routines, resources


IAM_E_REPO = ROOT.parent
_IAM_E_DELTA_IN_REPO = "ops/deploy/iam_delta_v1.json"
# A grant never lives where the checkout must stay clean: the delta, this tool and the
# engine source it imports.
_IAM_E_GUARDED = ("ops/deploy", "engine/scripts/migrations", "engine/src")
# Claims and receipts are keyed by the grant's digest in one folder beside the checkout,
# so no copy, link or move of a grant file can run it again.
_IAM_E_CLAIMS_NAME = "42-iam-grant-claims"
# Where section 4 moves a spent grant's claim, receipt and lock. The proof a grant is
# spent is its marker in the claims folder, which is never moved; a record file whose
# name holds the first 12 characters of the grant's digest is a second guard.
_IAM_E_RECORD = "docs/operations/iam-grants"
_IAM_E_GRANT_WORD = "iam-e-apply-granted"
_IAM_E_GRANT_LIMIT = 64 * 1024
_IAM_E_GRANT_KEYS = frozenset(
    {
        "amendment",
        "approved_by",
        "contract_version",
        "delta_sha256",
        "expires_at",
        "granted_at",
        "grantor",
        "head_commit",
        "purpose",
        "resource_manifest_sha256",
    }
)
_IAM_E_GRANT_CONTRACT = "42_iam_amendment_grant_v1"
_IAM_E_GRANT_PURPOSE = "iam_amendment_apply"
_IAM_E_GRANTOR = "Albert Meintjes"
_IAM_E_GRANT_GO = re.compile(
    r"Albert Meintjes, go in (session_[A-Za-z0-9]{8,64}) at "
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)",
    re.ASCII,
)
_IAM_E_GRANT_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_IAM_E_GRANT_WINDOW = 8 * 60 * 60
_IAM_E_HEAD = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class _IamEGrant:
    """A grant that passed every check, with where its claim and receipt go."""

    amendment: str
    approved_by: str
    grant_sha256: str
    head_commit: str
    claim_path: Path
    receipt_path: Path

    def approval(self) -> dict[str, str]:
        return {
            "path": "grant",
            "amendment": self.amendment,
            "grant_sha256": self.grant_sha256,
            "head_commit": self.head_commit,
            "approved_by": self.approved_by,
        }


@dataclass(frozen=True)
class _IamECheckout:
    head_commit: str
    delta_sha256: str | None
    worktree_sha256: str | None
    dirty: bool


def _iam_e_now() -> datetime:
    return datetime.now(UTC)


def _iam_e_grant_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _iam_e_grant_location(path: Path) -> None:
    resolved = Path(path).resolve()
    for folder in _IAM_E_GUARDED:
        if resolved.is_relative_to((Path(IAM_E_REPO) / folder).resolve()):
            raise _iam_e_refusal("grant_invalid")


def _iam_e_claims() -> Path:
    return Path(IAM_E_REPO).resolve().parent / _IAM_E_CLAIMS_NAME


def _iam_e_spent_path(grant_sha256: str) -> Path:
    return _iam_e_claims() / f"{grant_sha256}.spent.json"


def _iam_e_grant_paths(grant_sha256: str) -> tuple[Path, Path]:
    """The claim and receipt for these grant bytes; refused once the grant is spent."""

    claims = _iam_e_claims()
    claim = claims / f"{grant_sha256}.claim.json"
    receipt = claims / f"{grant_sha256}.receipt.json"
    if claim.exists() or receipt.exists() or _iam_e_spent_path(grant_sha256).exists():
        raise _iam_e_refusal("grant_consumed")
    record = Path(IAM_E_REPO) / _IAM_E_RECORD
    try:
        recorded = next(record.glob(f"*{grant_sha256[:12]}*"), None) is not None
    except OSError as exc:
        raise _iam_e_refusal("grant_consumed") from exc
    if recorded:
        raise _iam_e_refusal("grant_consumed")
    return claim, receipt


def _iam_e_unwritten(receipt_path: Path) -> bool:
    """Whether a receipt shows its run sent no write at all."""

    try:
        receipt = json.loads(receipt_path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    applied = receipt.get("applied") if isinstance(receipt, dict) else None
    return (
        isinstance(applied, dict)
        and applied.get("writes") == []
        and "failed_at" in applied
        and applied["failed_at"] is None
    )


def _iam_e_lock_path(amendment: str, delta_sha256: str) -> Path:
    return _iam_e_claims() / f"{amendment}.{delta_sha256}.lock.json"


def _iam_e_check_claimed(amendment: str, delta_sha256: str) -> None:
    """Refuse a new grant for an amendment and delta an earlier grant may have written."""

    # A lock blocks as a claim does, whatever else the folder holds; only the executor,
    # with Albert's new go, moves it.
    if _iam_e_lock_path(amendment, delta_sha256).exists():
        raise _iam_e_refusal("grant_amendment_claimed")
    claims = _iam_e_claims()
    if not claims.exists():
        return
    try:
        paths = sorted(claims.glob("*.claim.json"))
    except OSError as exc:
        raise _iam_e_refusal("grant_amendment_claimed") from exc
    for path in paths:
        try:
            claim = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise _iam_e_refusal("grant_amendment_claimed") from exc
        if not isinstance(claim, dict):
            raise _iam_e_refusal("grant_amendment_claimed")
        if (claim.get("amendment"), claim.get("delta_sha256")) != (amendment, delta_sha256):
            continue
        receipt = path.with_name(path.name.removesuffix(".claim.json") + ".receipt.json")
        if not _iam_e_unwritten(receipt):
            raise _iam_e_refusal("grant_amendment_claimed")
    # A receipt left alone blocks as its claim would, unless it shows no write attempted.
    try:
        receipts = sorted(claims.glob("*.receipt.json"))
    except OSError as exc:
        raise _iam_e_refusal("grant_amendment_claimed") from exc
    for path in receipts:
        try:
            receipt = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise _iam_e_refusal("grant_amendment_claimed") from exc
        if not isinstance(receipt, dict):
            raise _iam_e_refusal("grant_amendment_claimed")
        approval = receipt.get("approval")
        named = approval.get("amendment") if isinstance(approval, dict) else None
        if (named, receipt.get("delta_sha256")) != (amendment, delta_sha256):
            continue
        if not _iam_e_unwritten(path):
            raise _iam_e_refusal("grant_amendment_claimed")


def _iam_e_load_grant(path: Path) -> tuple[bytes, dict[str, str]]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_IAM_E_GRANT_LIMIT + 1)
    except OSError as exc:
        raise _iam_e_refusal("grant_invalid") from exc
    if len(raw) > _IAM_E_GRANT_LIMIT:
        raise _iam_e_refusal("grant_invalid")
    try:
        grant = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, ValueError, MigrationRefusal) as exc:
        raise _iam_e_refusal("grant_invalid") from exc
    if (
        not isinstance(grant, dict)
        or set(grant) != _IAM_E_GRANT_KEYS
        or not all(isinstance(value, str) for value in grant.values())
        or raw != _iam_e_grant_bytes(grant)
    ):
        raise _iam_e_refusal("grant_invalid")
    return raw, grant


def _iam_e_grant_time(value: str) -> datetime:
    if not _IAM_E_GRANT_TIME.fullmatch(value):
        raise _iam_e_refusal("grant_invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise _iam_e_refusal("grant_invalid") from exc


def _iam_e_check_grant(grant: Mapping[str, str], targets: IamETargets) -> None:
    """Every check on the grant's own fields, in order, against the delta read now."""

    if (grant["contract_version"], grant["purpose"]) != (
        _IAM_E_GRANT_CONTRACT,
        _IAM_E_GRANT_PURPOSE,
    ):
        raise _iam_e_refusal("grant_invalid")
    if grant["grantor"] != _IAM_E_GRANTOR:
        raise _iam_e_refusal("grant_grantor_refused")
    go = _IAM_E_GRANT_GO.fullmatch(grant["approved_by"])
    if go is None:
        raise _iam_e_refusal("grant_invalid")
    granted_at = _iam_e_grant_time(grant["granted_at"])
    expires_at = _iam_e_grant_time(grant["expires_at"])
    if grant["granted_at"] != go.group(2) or _iam_e_grant_time(go.group(2)) != granted_at:
        raise _iam_e_refusal("grant_invalid")
    if not granted_at < expires_at:
        raise _iam_e_refusal("grant_invalid")
    if (expires_at - granted_at).total_seconds() > _IAM_E_GRANT_WINDOW:
        raise _iam_e_refusal("grant_window_too_long")
    now = _iam_e_now()
    if now < granted_at:
        raise _iam_e_refusal("grant_not_yet_valid")
    if now >= expires_at:
        raise _iam_e_refusal("grant_expired")
    if grant["amendment"] != targets.pending_amendment:
        raise _iam_e_refusal("grant_amendment_mismatch")
    if grant["delta_sha256"] != targets.delta_sha256:
        raise _iam_e_refusal("grant_delta_mismatch")
    if grant["resource_manifest_sha256"] != targets.manifest_sha256:
        raise _iam_e_refusal("grant_manifest_mismatch")


# Git long options, spelled from their parts.
_IAM_E_GIT_LONG = "-" * 2


def _iam_e_git(*args: str) -> bytes | None:
    # Git obeys GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE and the rest over -C, so none of
    # them reaches the git that proves the checkout; neither does system or global
    # config, a replace ref, an fsmonitor hook or the untracked cache.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    try:
        completed = subprocess.run(
            [
                "git",
                f"{_IAM_E_GIT_LONG}no-replace-objects",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(IAM_E_REPO),
                *args,
            ],
            check=False,
            capture_output=True,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _iam_e_refusal("grant_head_unreadable") from exc
    return completed.stdout if completed.returncode == 0 else None


def _iam_e_checkout() -> _IamECheckout:
    """HEAD, the digest of the delta HEAD holds, and whether the guarded folders changed."""

    top_level = _iam_e_git("rev-parse", f"{_IAM_E_GIT_LONG}show-toplevel")
    try:
        top = None if top_level is None else top_level.decode("utf-8").strip()
    except UnicodeDecodeError:
        top = None
    # The repository is the checkout itself, never one that holds it in a subfolder.
    if not top or Path(top).resolve() != Path(IAM_E_REPO).resolve():
        raise _iam_e_refusal("grant_head_unreadable")
    head = _iam_e_git("rev-parse", f"{_IAM_E_GIT_LONG}verify", "HEAD^{commit}")
    try:
        head_commit = "" if head is None else head.decode("ascii").strip()
    except UnicodeDecodeError:
        head_commit = ""
    if not _IAM_E_HEAD.fullmatch(head_commit):
        raise _iam_e_refusal("grant_head_unreadable")
    blob = _iam_e_git("cat-file", "blob", f"{head_commit}:{_IAM_E_DELTA_IN_REPO}")
    # Short status names every changed, staged or untracked path; empty means clean.
    status = _iam_e_git("status", "-s", "-uall", *_IAM_E_GUARDED)
    # Status skips an entry marked skip worktree (tag S) or assume unchanged (a lower
    # case tag), so any such entry in the guarded folders is dirty in itself.
    entries = _iam_e_git("ls-files", "-v", "-z", *_IAM_E_GUARDED)
    if status is None or entries is None:
        raise _iam_e_refusal("grant_head_unreadable")
    hidden = any(
        entry[:1] == b"S" or entry[:1].islower() for entry in entries.split(b"\0") if entry
    )
    # The file hashed is the file the checkout holds, byte for byte.
    try:
        worktree = (Path(IAM_E_REPO) / _IAM_E_DELTA_IN_REPO).read_bytes()
    except OSError:
        worktree = None
    return _IamECheckout(
        head_commit,
        None if blob is None else hashlib.sha256(blob).hexdigest(),
        None if worktree is None else hashlib.sha256(worktree).hexdigest(),
        bool(status.strip()) or hidden,
    )


def _iam_e_write_exclusive(path: Path, value: object) -> None:
    with open(path, "xb") as handle:
        handle.write(_iam_e_grant_bytes(value))


def _iam_e_granted(approved_sha256: str, grant_path: Path) -> tuple[IamETargets, _IamEGrant]:
    """Every check the granted apply makes before credentials load or anything is read."""

    targets = iam_e_targets()
    if targets.delta_sha256 != approved_sha256:
        raise _iam_e_refusal("delta_digest_mismatch")
    if targets.approval_state == "approved":
        raise _iam_e_refusal("grant_not_needed")
    if targets.pending_amendment is None:
        raise _iam_e_refusal("grant_prior_unapproved")
    _iam_e_grant_location(grant_path)
    raw, grant = _iam_e_load_grant(grant_path)
    claim_path, receipt_path = _iam_e_grant_paths(hashlib.sha256(raw).hexdigest())
    _iam_e_check_claimed(targets.pending_amendment, targets.delta_sha256)
    _iam_e_check_grant(grant, targets)
    checkout = _iam_e_checkout()
    if checkout.head_commit != grant["head_commit"]:
        raise _iam_e_refusal("grant_head_mismatch")
    if not checkout.delta_sha256 == checkout.worktree_sha256 == grant["delta_sha256"]:
        raise _iam_e_refusal("grant_delta_uncommitted")
    if checkout.dirty:
        raise _iam_e_refusal("grant_checkout_dirty")
    return targets, _IamEGrant(
        grant["amendment"],
        grant["approved_by"],
        hashlib.sha256(raw).hexdigest(),
        grant["head_commit"],
        claim_path,
        receipt_path,
    )


def _run_iam_e_granted(
    credentials_loader, approved_sha256: str, grant_path: Path
) -> tuple[dict, Path | None]:
    """Apply the proposed last amendment under Albert's grant; the receipt and where it goes."""

    targets, grant = _iam_e_granted(approved_sha256, grant_path)
    receipt = _iam_e_execute(credentials_loader, "apply", targets, grant)
    return receipt, grant.receipt_path if grant.claim_path.exists() else None


def _run_iam_e(credentials_loader, mode: str, approved_sha256: str | None = None) -> dict:
    if mode not in {"plan", "dry_run", "apply"}:
        raise _iam_e_refusal("delta_invalid")
    targets = iam_e_targets()
    if mode == "apply":
        # The apply is bound to the exact bytes Albert approved, by the digest he was shown.
        if approved_sha256 is None or targets.delta_sha256 != approved_sha256:
            raise _iam_e_refusal("delta_digest_mismatch")
        if targets.approval_state != "approved":
            raise _iam_e_refusal("delta_unapproved")
    return _iam_e_execute(credentials_loader, mode, targets)


def _iam_e_execute(
    credentials_loader, mode: str, targets: IamETargets, grant: _IamEGrant | None = None
) -> dict:
    adapter = _iam_e_adapter(credentials_loader())
    before = _iam_e_read_state(adapter, targets)
    roles, auth, rows = _iam_e_states(targets, before)
    missing_routines, missing_resources = _iam_e_missing(before, rows)
    writes = _iam_e_writes(targets, before)
    receipt: dict[str, object] = {
        "contract_version": "open_intelligence_iam_amendment_e_v1",
        "mode": mode,
        "delta_sha256": targets.delta_sha256,
        "base_delta_sha256": IAM_E_BASE_SHA256,
        "resource_manifest_sha256": targets.manifest_sha256,
        "approval_state": targets.approval_state,
        "routine_missing": missing_routines,
        "resource_missing": missing_resources,
        "apply_ready": targets.approval_state == "approved"
        and not missing_routines
        and not missing_resources,
    }
    if grant is not None:
        receipt["approval"] = grant.approval()
    if mode == "plan":
        receipt["rows"] = (
            [
                {
                    "kind": "custom_role",
                    "role": item.role,
                    "permissions": list(item.permissions),
                    "live_permissions": None
                    if before.custom_roles[item.role] is None
                    else sorted(before.custom_roles[item.role].get("includedPermissions", [])),
                    "state": value,
                }
                for item, value in roles
            ]
            + [
                {
                    "kind": "authorization",
                    "routine": item.routine,
                    "role": item.role,
                    "routine_type": item.routine_type,
                    "written_role": item.role,
                    "state": value,
                }
                for item, value in auth
            ]
            + [_iam_e_row_view(row, value) for row, value in rows]
        )
        receipt["counts"] = _iam_e_counts(roles, auth, rows)
        receipt["readback_sha256"] = _iam_e_state_digest(before)
        return receipt
    planned = dict.fromkeys(
        (
            "custom_role_writes",
            "dataset_access_patches",
            "routine_policy_sets",
            "table_policy_sets",
            "bucket_policy_sets",
            "run_job_policy_sets",
            "project_policy_sets",
        ),
        0,
    )
    names = {
        "custom_role": "custom_role_writes",
        "authorization": "dataset_access_patches",
        "dataset": "dataset_access_patches",
        "routine": "routine_policy_sets",
        "table": "table_policy_sets",
        "bucket": "bucket_policy_sets",
        "runtime_job": "run_job_policy_sets",
        "project": "project_policy_sets",
    }
    for item in writes:
        planned[names[item.kind]] += 1
    receipt["writes_planned"] = planned
    receipt["planned_payload_sha256"] = _digest([_iam_e_view(item) for item in writes])
    if mode == "dry_run":
        receipt["policies_to_write"] = [_iam_e_view(item) for item in writes]
        receipt["counts"] = _iam_e_counts(roles, auth, rows)
        receipt["readback_sha256"] = _iam_e_state_digest(before)
        return receipt
    if missing_routines or missing_resources:
        # Nothing is written until every routine and resource the rows name exists live.
        code = "routine_missing" if missing_routines else "resource_missing"
        raise _IamEDetailRefusal(
            f"execution_approval_iam_e_{code}",
            {"routine_missing": missing_routines, "resource_missing": missing_resources},
        )
    if grant is not None and writes:
        # The first write spends the grant: the claim is created once and never again.
        try:
            grant.claim_path.parent.mkdir(exist_ok=True)
            _iam_e_write_exclusive(
                grant.claim_path,
                {
                    "amendment": grant.amendment,
                    "delta_sha256": targets.delta_sha256,
                    "grant_sha256": grant.grant_sha256,
                    "planned_payload_sha256": receipt["planned_payload_sha256"],
                },
            )
            # The marker stays in the claims folder whatever section 4 moves, so the
            # grant stays spent; it never blocks the amendment and delta.
            _iam_e_write_exclusive(
                _iam_e_spent_path(grant.grant_sha256), {"grant_sha256": grant.grant_sha256}
            )
        except FileExistsError as exc:
            raise _iam_e_refusal("grant_consumed") from exc
        except OSError as exc:
            raise _iam_e_refusal("grant_claim_unwritten") from exc
        # Two grants from one go pass every earlier check at once; after its own claim
        # each takes the lock on the amendment and delta, and only the one that takes it
        # writes. The other is spent, and its receipt says it wrote nothing.
        lock = _iam_e_lock_path(str(targets.pending_amendment), targets.delta_sha256)
        try:
            _iam_e_write_exclusive(
                lock,
                {
                    "amendment": targets.pending_amendment,
                    "delta_sha256": targets.delta_sha256,
                    "grant_sha256": grant.grant_sha256,
                },
            )
        except OSError as exc:
            code = (
                "grant_amendment_claimed"
                if isinstance(exc, FileExistsError)
                else "grant_claim_unwritten"
            )
            _iam_e_record_refusal(
                grant,
                receipt,
                {"writes": [], "failed_at": None},
                f"execution_approval_iam_e_{code}",
            )
        try:
            return _iam_e_send_all(adapter, targets, before, writes, roles, auth, rows, receipt)
        except _V2IamWriteRefusal as exc:
            _iam_e_record_refusal(grant, receipt, exc.applied, str(exc), exc)
    return _iam_e_send_all(adapter, targets, before, writes, roles, auth, rows, receipt)


def _iam_e_record_refusal(
    grant: _IamEGrant,
    receipt: Mapping[str, object],
    applied: Mapping[str, object],
    code: str,
    cause: BaseException | None = None,
) -> None:
    """Write the receipt of a refusal after the claim, then raise it with that receipt."""

    failed = {**receipt, "applied": dict(applied)}
    try:
        _iam_e_write_exclusive(grant.receipt_path, {**failed, "error": code})
    except OSError as unwritten:
        raise _IamEDetailRefusal(
            "execution_approval_iam_e_grant_receipt_unwritten",
            {"receipt": {**failed, "error": code}},
        ) from unwritten
    raise _IamEDetailRefusal(code, failed) from cause


def _iam_e_send_all(adapter, targets, before, writes, roles, auth, rows, receipt) -> dict:
    """Send every write in order, stop at the first failure, then prove each row by a read."""

    applied: dict[str, object] = {"writes": [], "failed_at": None}
    for write in writes:
        try:
            _iam_e_send(adapter, write)
        except Exception as exc:
            applied["failed_at"] = {"kind": write.kind, "resource": write.ident}
            raise _V2IamWriteRefusal("execution_approval_iam_e_write_refused", applied) from exc
        applied["writes"].append({"kind": write.kind, "resource": write.ident})
    applied["failed_at"] = "readback"
    # The proof is a fresh read of every target, never the answer to a write.
    try:
        after = _iam_e_read_state(adapter, targets)
        roles_after, auth_after, rows_after = _iam_e_states(targets, after)
        kept = all(
            {_iam_e_canonical(entry) for entry in value["access"]}
            <= {_iam_e_canonical(entry) for entry in after.datasets[ident]["access"]}
            for ident, value in before.datasets.items()
        ) and all(
            policy is None
            or (
                after.policies[key] is not None
                and _iam_e_policy_grants(policy) <= _iam_e_policy_grants(after.policies[key])
            )
            for key, policy in before.policies.items()
        )
    except Exception as exc:
        raise _V2IamWriteRefusal("execution_approval_iam_e_readback_mismatch", applied) from exc
    unproven = [
        value for _item, value in [*roles_after, *auth_after, *rows_after] if value != "present"
    ]
    if unproven or not kept:
        raise _V2IamWriteRefusal("execution_approval_iam_e_readback_mismatch", applied)
    applied["failed_at"] = None
    before_counts = _iam_e_counts(roles, auth, rows)
    after_counts = _iam_e_counts(roles_after, auth_after, rows_after)
    receipt["applied"] = applied
    receipt["counts"] = {
        kind: {
            "target": value["target"],
            "present_before": value.get("present", 0),
            "added": value.get("missing", 0) + value.get("differs", 0),
            "present_after": after_counts[kind].get("present", 0),
        }
        for kind, value in before_counts.items()
    }
    receipt["readback_sha256"] = _iam_e_state_digest(after)
    return receipt


def _refusal_payload(exc: MigrationRefusal) -> dict[str, object]:
    payload: dict[str, object] = {"error": str(exc)}
    if isinstance(exc, _V2IamWriteRefusal):
        payload["applied"] = exc.applied
    if isinstance(exc, _ApplyV2Refusal):
        if exc.applied is not None:
            payload["applied"] = exc.applied
        payload["preserved_bindings"] = exc.preserved
    if isinstance(exc, (_IamEDetailRefusal, _ApplyV2RoutineRefusal)):
        payload.update(exc.detail)
    return payload


def _emit(payload: Mapping[str, object], *, error: bool = False) -> None:
    global _LAST_ERROR
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if error:
        _LAST_ERROR = line
        _write_exact_line(sys.stderr, line)
    else:
        _write_exact_line(sys.stdout, line)


def _write_exact_line(stream: object, line: str) -> None:
    encoded = (line + "\n").encode("utf-8")
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(encoded)
        return
    stream.write(line + "\n")


def _run_iam_e_granted_word(rest: Sequence[str]) -> int:
    # The granted apply takes the digest of the delta read now and the grant file's path.
    if len(rest) != 2 or not _IAM_E_SHA256.fullmatch(rest[0]) or Path(rest[1]).suffix != ".json":
        _emit({"error": "execution_approval_iam_e_grant_required"}, error=True)
        return 2
    try:
        receipt, receipt_path = _run_iam_e_granted(_load_credentials, rest[0], Path(rest[1]))
    except MigrationRefusal as exc:
        _emit(_refusal_payload(exc), error=True)
        return 1
    except Exception:
        _emit({"error": "execution_approval_internal_refusal"}, error=True)
        return 1
    _emit(receipt)
    if receipt_path is not None:
        try:
            _iam_e_write_exclusive(receipt_path, receipt)
        except OSError:
            _emit(
                {"error": "execution_approval_iam_e_grant_receipt_unwritten", "receipt": receipt},
                error=True,
            )
            return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["plan"]:
        _write_exact_line(sys.stdout, render_plan(build_plan()).removesuffix("\n"))
        return 0
    if arguments == ["plan-v2"]:
        try:
            _write_exact_line(sys.stdout, render_v2_plan(build_v2_plan()).removesuffix("\n"))
        except MigrationRefusal as exc:
            _emit({"error": str(exc)}, error=True)
            return 1
        return 0
    if arguments == ["apply-v2"]:
        try:
            receipt = _apply_v2_plan(build_v2_plan())
        except MigrationRefusal as exc:
            _emit(_refusal_payload(exc), error=True)
            return 1
        except Exception:
            _emit({"error": "execution_approval_internal_refusal"}, error=True)
            return 1
        _emit(receipt)
        return 0
    if len(arguments) == 1 and (
        arguments[0] == _V2_AUTHORISATION_STATE_WORD or arguments[0] in _V2_REAUTHORISE_WORDS
    ):
        try:
            if arguments[0] == _V2_AUTHORISATION_STATE_WORD:
                receipt = v2_authorisation_state(build_v2_plan(), _load_credentials())
            else:
                receipt = _run_v2_reauthorise(
                    build_v2_plan(), _load_credentials(), _V2_REAUTHORISE_WORDS[arguments[0]]
                )
        except MigrationRefusal as exc:
            _emit(_refusal_payload(exc), error=True)
            return 1
        except Exception:
            _emit({"error": "execution_approval_internal_refusal"}, error=True)
            return 1
        _emit(receipt)
        return 0
    if len(arguments) == 1 and arguments[0] in _V2_IAM_WORDS:
        try:
            receipt = _run_v2_iam(
                build_v2_plan(),
                _load_credentials(),
                _V2_IAM_WORDS[arguments[0]],
                check_missing_routines=arguments[0] == "iam-v2-apply",
            )
        except MigrationRefusal as exc:
            _emit(_refusal_payload(exc), error=True)
            return 1
        except Exception:
            _emit({"error": "execution_approval_internal_refusal"}, error=True)
            return 1
        _emit(receipt)
        return 0
    if arguments and arguments[0] == _IAM_E_GRANT_WORD:
        return _run_iam_e_granted_word(arguments[1:])
    if arguments and arguments[0] in _IAM_E_WORDS:
        mode, rest = _IAM_E_WORDS[arguments[0]], arguments[1:]
        # The apply takes the approved delta digest as its one argument; plan and dry run
        # take none.
        wanted = 1 if mode == "apply" else 0
        if len(rest) != wanted or not all(_IAM_E_SHA256.fullmatch(item) for item in rest):
            _emit({"error": "execution_approval_iam_e_digest_required"}, error=True)
            return 2
        try:
            receipt = _run_iam_e(_load_credentials, mode, *rest)
        except MigrationRefusal as exc:
            _emit(_refusal_payload(exc), error=True)
            return 1
        except Exception:
            _emit({"error": "execution_approval_internal_refusal"}, error=True)
            return 1
        _emit(receipt)
        return 0
    options = _parse_cutover_arguments(arguments)
    if options is not None:
        return _run_cutover_word(options)
    if arguments != ["apply"]:
        _emit({"error": "execution_approval_manifest_invalid"}, error=True)
        return 2
    if APPROVED_BOOTSTRAP_CONTRACT_SHA256 is None:
        _emit({"error": "execution_approval_bootstrap_unapproved"}, error=True)
        return 1
    try:
        receipt = _apply_plan(build_plan())
    except MigrationRefusal as exc:
        _emit({"error": str(exc)}, error=True)
        return 1
    except Exception:
        _emit({"error": "execution_approval_internal_refusal"}, error=True)
        return 1
    _emit(receipt.to_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
