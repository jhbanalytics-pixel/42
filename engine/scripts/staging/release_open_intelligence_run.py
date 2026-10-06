"""Release one named open intelligence run for display.

Approved by 42-noncanary-run-release-approval-request-2026-08-29.md section
3.4. Release is a separate human act with a record: a run can never release
itself, and this script is the only code allowed to flip either display flag,
in the database, against a verified receipt.

It takes one approved immutable run profile (run id, source policy digest,
cutoff, generation pair, canary namespace flag), selected on the command line by
profile name and defaulting to the retained R3 profile, verifies the receipt
exists and is completed, complete-partitioned, non-canary and still blocked,
then in one transaction enables the receipt's display release state, marks that
run's predictions display eligible, and writes a release record naming who
approved, under which document, when, and the receipt digest at the moment of
the decision. It refuses a second release of the same run and refuses a run
whose receipt changed between read and release. A live staging profile also
proves its source authority, its membership certification and the deterministic
prediction enrollment of every promoted signal before the display transaction.
The transaction is the release: no path replaces it with a ready flag update.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from google.cloud import bigquery
from src.analysis.open_intelligence import execution_approval, execution_generations, live_quality
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.capture_registry import validate_capture_entry
from src.analysis.open_intelligence.live_quality import (
    QUALITY_RELEASE_FIELDS,
    review_packet_digest_sql,
    run_receipt_digest_sql,
)
from src.analysis.open_intelligence.persistence import (
    candidate_projection_digest_sql,
    row_set_digest_sql,
)
from src.analysis.open_intelligence.predictions import (
    PREDICTION_ROW_FIELDS,
    PredictionRules,
    PromotedSignal,
    build_signal_prediction_rows,
)
from src.analysis.open_intelligence.readiness import (
    EXPLICIT_ORIGIN_POLICY,
    INDEPENDENCE_POLICIES,
    WAVE1_FAMILY_POLICY,
)
from src.analysis.open_intelligence.run_receipts import (
    QA_CLIENT_SCOPE_ID,
    RUN_RECEIPT_ROW_FIELDS,
    build_run_receipt,
    run_receipt_digest,
)

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
RECEIPT_TABLE = "open_intelligence_run_receipts_v1"
CANDIDATES_TABLE = "signal_candidates_v2"
EVIDENCE_TABLE = "signal_evidence_v2"
MEMBERSHIP_TABLE = "signal_membership_v2"
PREDICTIONS_TABLE = "signal_predictions_v2"
LINEAGE_TABLE = "signal_lineage_v2"
OUTCOMES_TABLE = "signal_outcomes_v2"
ANALYSIS_TABLE = "signal_analysis_v2"
RELEASE_RECORD_TABLE = "open_intelligence_quality_release_records_v2"
RELEASE_CONTRACT_VERSION = "open_intelligence_quality_release_v2"
R3_RUN_ID = "run_20260903_dynamic_apply_v2_r16"
APPROVAL_DATASET = "trends_v2_staging_approvals"
LOCATION = "US"
_RUN_ID = re.compile(r"[a-z0-9_][a-z0-9_-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
_PROFILE_NAME = re.compile(r"[a-z0-9_][a-z0-9_-]{0,63}\Z")
R3_PROFILE_NAME = "r3"
_PROMOTED_CANDIDATE_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
    "signal_id",
    "signal_date",
    "market",
    "discovery_mode",
    "evidence_state",
    "velocity_score",
    "breadth_score",
    "cluster_build_version",
)
PREDICTION_ACCURACY_NOTE = (
    "Prediction rows are deterministic enrollments issued before display release. They "
    "carry no measured accuracy claim; outcomes are evaluated separately at each "
    "prediction's evaluation date."
)
# The retained v1 apply job and principal, kept for historical inspection of the r16 chain
# only. A fresh release resolves the upstream apply binding from the v2 origin row.
_APPLY_JOB = "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-apply-staging"
_APPLY_IDENTITY = "trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com"
_MANIFEST_V1 = "open_intelligence_execution_manifest_v1"
_MANIFEST_V2 = "open_intelligence_execution_manifest_v2"
_HISTORICAL_MODES = ("historical_read", "historical_replay")
_GENERATION_FIELDS = ("origin_registry_sha256", "resource_manifest_sha256")
_EXECUTION_PROOF_FIELDS = {
    "execution_proof_contract_version",
    "r3_pre_execution_addendum_sha256",
    "execution_name",
    "job_resource",
    "image_digest",
    "source_sha",
    "service_identity",
    "command",
    "args",
    "config_digest",
    "run_id",
    "signal_date",
    "started_at",
    "completed_at",
    "status",
    "run_receipt_digest",
    "row_set_digest",
    "persisted_row_family_counts",
}
_DURABLE_ARTIFACT_CONTEXT = None


class ReleaseRefusal(ValueError):
    """The release cannot proceed honestly."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _live_code(profile: ReleaseRunProfile, code: str) -> str | None:
    """A refusal code for a live profile; the retained R3 replay keeps its bare refusal."""
    return None if profile.is_replay else code


@dataclass(frozen=True, slots=True)
class ReleaseRunProfile:
    """One approved immutable run profile the release binds to.

    A profile without a source policy digest is the retained historical replay: the
    packaged active generation is resolved at run time and its predictions were issued
    by the retained apply. A profile with a source policy digest is a live staging
    release, which must also prove source authority, membership certification and the
    prediction enrollment of every promoted signal before the display transaction.
    """

    profile_name: str
    run_id: str
    source_policy_digest: str | None
    cutoff: str
    generation_pair: tuple[str, str] | None
    canary_namespace: bool
    prediction_rules: PredictionRules | None = None
    # The readiness independence rule the run's chain evaluated under. The retained R3
    # replay names the legacy rule; every new staging profile names the explicit origin rule.
    independence_policy: str = WAVE1_FAMILY_POLICY

    def __post_init__(self) -> None:
        if (
            not isinstance(self.profile_name, str)
            or _PROFILE_NAME.fullmatch(self.profile_name) is None
        ):
            raise ReleaseRefusal("release profile name is invalid", code="release_profile_invalid")
        if self.independence_policy not in INDEPENDENCE_POLICIES:
            raise ReleaseRefusal(
                "release profile independence policy is invalid", code="release_profile_invalid"
            )
        if (
            self.profile_name != R3_PROFILE_NAME
            and self.independence_policy != EXPLICIT_ORIGIN_POLICY
        ):
            raise ReleaseRefusal(
                "a new staging profile must name the explicit origin independence policy",
                code="release_profile_invalid",
            )
        if not isinstance(self.run_id, str) or _RUN_ID.fullmatch(self.run_id) is None:
            raise ReleaseRefusal(
                "release profile run id is invalid", code="release_profile_invalid"
            )
        digest = self.source_policy_digest
        if digest is not None and (
            not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None
        ):
            raise ReleaseRefusal(
                "release profile source policy digest is invalid", code="release_profile_invalid"
            )
        try:
            cutoff = date.fromisoformat(self.cutoff)
        except (TypeError, ValueError) as error:
            raise ReleaseRefusal(
                "release profile cutoff is invalid", code="release_profile_invalid"
            ) from error
        if cutoff.isoformat() != self.cutoff:
            raise ReleaseRefusal(
                "release profile cutoff is invalid", code="release_profile_invalid"
            )
        pair = self.generation_pair
        if pair is not None:
            if (
                not isinstance(pair, (tuple, list))
                or len(pair) != 2
                or any(
                    not isinstance(item, str) or _DIGEST.fullmatch(item) is None for item in pair
                )
            ):
                raise ReleaseRefusal(
                    "release profile generation pair is invalid", code="release_profile_invalid"
                )
            object.__setattr__(self, "generation_pair", tuple(pair))
        if type(self.canary_namespace) is not bool:
            raise ReleaseRefusal(
                "release profile canary namespace flag is invalid", code="release_profile_invalid"
            )
        if self.prediction_rules is not None and not isinstance(
            self.prediction_rules, PredictionRules
        ):
            raise ReleaseRefusal(
                "release profile prediction rules are invalid", code="release_profile_invalid"
            )

    @property
    def is_replay(self) -> bool:
        """Only the retained R3 binding replays; every other profile is a live release."""
        return self == R3_PROFILE

    def binding(self) -> dict[str, object]:
        rules = self.prediction_rules
        return {
            "profile_name": self.profile_name,
            "run_id": self.run_id,
            "source_policy_digest": self.source_policy_digest,
            "cutoff": self.cutoff,
            "generation_pair": None if self.generation_pair is None else list(self.generation_pair),
            "canary_namespace": self.canary_namespace,
            "independence_policy": self.independence_policy,
            "prediction_rules": (
                None
                if rules is None
                else {"evaluation_days": rules.evaluation_days, "rule_version": rules.rule_version}
            ),
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.binding())


R3_PROFILE = ReleaseRunProfile(
    profile_name=R3_PROFILE_NAME,
    run_id=R3_RUN_ID,
    source_policy_digest=None,
    cutoff="2026-09-03",
    generation_pair=None,
    canary_namespace=False,
    independence_policy=WAVE1_FAMILY_POLICY,
)
# Approved immutable profiles selectable by name. Adding one is a reviewed change.
RELEASE_PROFILES: dict[str, ReleaseRunProfile] = {"r3": R3_PROFILE}


@dataclass(frozen=True, slots=True)
class ReleaseEvidence:
    blocked_run_receipt_digest: str
    candidate_projection_digest: str
    packet_digest: str
    quality_review_receipt_digest: str
    execution_proof_digest: str
    release_contract_digest: str
    source_sha: str
    profile_digest: str | None = None
    # The readiness independence policy the certified run was evaluated under, taken
    # from the chain record by the certify adapter; None on the retained R3 replay.
    independence_policy: str | None = None


@dataclass(frozen=True, slots=True)
class ReleaseInputs:
    receipt: object
    evidence: ReleaseEvidence
    artifacts: Mapping[str, bytes]


@dataclass(slots=True)
class _ReleaseArtifactProvider:
    client: object
    generation: object = None
    apply_binding: object = None
    inputs: ReleaseInputs | None = None
    profile: ReleaseRunProfile = R3_PROFILE

    def read(self, name: str) -> bytes:
        if self.inputs is None:
            self.inputs = _read_release_inputs(
                self.client,
                version=execution_approval._RESULT_VERSION_V2,
                mode="historical_read",
                generation=self.generation,
                apply_binding=self.apply_binding,
                profile=self.profile,
            )
        if name not in self.inputs.artifacts:
            raise ReleaseRefusal("r3 release execution artifact is unavailable")
        return self.inputs.artifacts[name]


def _parse_cli(argv: Sequence[str]) -> tuple[str, ...]:
    """No argument selects the retained R3 profile; `--profile NAME` selects a registered one."""
    values = tuple(argv)
    if not values:
        return ()
    if len(values) == 2 and values[0] == "--profile" and isinstance(values[1], str) and values[1]:
        return (values[1],)
    raise ReleaseRefusal("r3 release CLI grammar is invalid")


def _select_profile(selection: tuple[str, ...]) -> ReleaseRunProfile:
    if not selection:
        return R3_PROFILE
    profile = RELEASE_PROFILES.get(selection[0])
    if profile is None:
        raise ReleaseRefusal(
            f"release profile {selection[0]} is not registered", code="release_profile_unknown"
        )
    return profile


def _require_profile_generation(profile: ReleaseRunProfile, generation) -> tuple[str, str] | None:
    """A live profile names the generation pair it was approved under; R3 resolves the active one."""
    if profile.generation_pair is None:
        return None
    pair = _generation_pair(generation)
    if pair != profile.generation_pair:
        raise ReleaseRefusal(
            "release profile generation pair differs from the active pair",
            code="release_generation_pair_differs",
        )
    return pair


def _execution_approval_artifact_bytes(name: str) -> bytes:
    if not isinstance(_DURABLE_ARTIFACT_CONTEXT, _ReleaseArtifactProvider):
        raise ReleaseRefusal("r3 release execution artifact is unavailable")
    return _DURABLE_ARTIFACT_CONTEXT.read(name)


def _operation_binding(operation, *, manifest_version, mode, registry):
    """One origin row and its exact binding for the operation, under an explicit mode."""
    from src.analysis.open_intelligence.execution_origins import OriginRegistry, select_origin

    if type(registry) is not OriginRegistry or not isinstance(operation, str):
        raise ReleaseRefusal("execution origin registry is invalid")
    matches = [
        origin
        for origin in registry.values()
        if origin.manifest_version == manifest_version and operation in origin.operation_bindings
    ]
    if len(matches) != 1:
        raise ReleaseRefusal(f"{operation} has no single execution origin")
    origin = select_origin(
        manifest_version=matches[0].manifest_version,
        contract_sha256=matches[0].contract_sha256,
        mode=mode,
        registry=registry,
    )
    return origin, origin.operation_bindings[operation]


def _generation_pair(value):
    pair = tuple(getattr(value, name, None) for name in _GENERATION_FIELDS)
    if any(not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None for digest in pair):
        return None
    return pair


def _require_runtime_profile(authority, operation, *, generation, binding):
    """Bind the issued authority to the v2 origin row its generation names."""
    from src.analysis.open_intelligence.execution_generations import TrustedGeneration

    issued = getattr(authority, "generation", None)
    if (
        type(issued) is not TrustedGeneration
        or _generation_pair(issued) != _generation_pair(generation)
        or getattr(authority, "operation", None) != operation
    ):
        raise ReleaseRefusal(f"{operation} runtime generation differs from the active pair")
    origin, resolved = _operation_binding(
        operation, manifest_version=_MANIFEST_V2, mode="new_consume", registry=issued.registry
    )
    manifest = getattr(authority, "manifest", None)
    execution_name = getattr(authority, "execution_name", None)
    image_uri = getattr(authority, "image_uri", None)
    if (
        resolved != binding
        or getattr(authority, "job_resource", None) != resolved.job_resource
        or getattr(manifest, "job_resource", None) != resolved.job_resource
        or getattr(manifest, "service_identity", None) != resolved.service_identity
        or not isinstance(execution_name, str)
        or not execution_name.startswith(resolved.job_resource + "/executions/")
        or not isinstance(image_uri, str)
        or re.fullmatch(origin.image_uri_regex, image_uri) is None
        or getattr(manifest, "image_uri", None) != image_uri
    ):
        raise ReleaseRefusal(f"{operation} runtime identity differs from its execution origin")
    return origin, resolved


def _same_generation(authority, consumption):
    pair = _generation_pair(getattr(authority, "generation", None))
    return pair is not None and pair == _generation_pair(consumption)


def _admit_chain_row(row, operation, *, mode, generation):
    """Admit one v2 result chain row through the trusted catalogue and the same pair."""
    from src.analysis.open_intelligence.execution_generations import load_trusted_generation
    from src.analysis.open_intelligence.execution_origins import OriginRefusal

    if mode not in _HISTORICAL_MODES:
        raise ReleaseRefusal(f"{operation} result chain mode is not historical")
    pair = _generation_pair(SimpleNamespace(**{name: row.get(name) for name in _GENERATION_FIELDS}))
    if pair is None or pair != _generation_pair(generation):
        raise ReleaseRefusal(f"{operation} result chain generation differs")
    try:
        admitted = load_trusted_generation(*pair)
    except OriginRefusal as error:
        raise ReleaseRefusal(f"{operation} result chain generation is untrusted") from error
    _origin, binding = _operation_binding(
        operation, manifest_version=_MANIFEST_V2, mode=mode, registry=admitted.registry
    )
    return admitted.registry, binding


def _query(client: object, sql: str, *, parameters=(), max_results=None):
    config = bigquery.QueryJobConfig(
        use_legacy_sql=False,
        query_parameters=list(parameters),
    )
    job = client.query(
        sql,
        job_config=config,
        location=LOCATION,
        retry=None,
        job_retry=None,
    )
    return tuple(
        job.result(
            max_results=max_results,
            retry=None,
            job_retry=None,
        )
    )


def _receipt_from_row(row: Mapping[str, object]):
    fields = {name: row.get(name) for name in RUN_RECEIPT_ROW_FIELDS}
    if isinstance(fields["market_scope"], list):
        fields["market_scope"] = tuple(fields["market_scope"])
    try:
        return build_run_receipt(**fields)
    except ValueError as error:
        raise ReleaseRefusal("r3 blocked receipt is invalid") from error


def _read_blocked_receipt(client: object, *, profile: ReleaseRunProfile = R3_PROFILE):
    columns = ", ".join(f"`{field}`" for field in RUN_RECEIPT_ROW_FIELDS)
    rows = _query(
        client,
        f"SELECT {columns} FROM `{PROJECT}.{DATASET}.{RECEIPT_TABLE}` WHERE run_id = @run_id",
        parameters=(bigquery.ScalarQueryParameter("run_id", "STRING", profile.run_id),),
        max_results=2,
    )
    if len(rows) != 1:
        raise ReleaseRefusal("r3 blocked receipt cardinality differs")
    receipt = _receipt_from_row(dict(rows[0]))
    if (
        receipt.run_id != profile.run_id
        or receipt.status != "completed"
        or receipt.complete_partitions is not True
        or receipt.display_release_state != "blocked"
    ):
        raise ReleaseRefusal("r3 blocked receipt authority differs")
    return receipt


def _read_operation_result_chain(
    client: object,
    operation: str,
    *,
    version,
    mode,
    generation,
    profile: ReleaseRunProfile = R3_PROFILE,
) -> dict[str, object]:
    if mode not in _HISTORICAL_MODES:
        raise ReleaseRefusal(f"{operation} result chain mode is not historical")
    if version == execution_approval._RESULT_VERSION:
        # The retained v1 chain under a historical mode against the packaged retained registry.
        rows = _query(
            client,
            f"CALL `{PROJECT}.{APPROVAL_DATASET}."
            "sp_read_open_intelligence_execution_result_chain_v1`(@source_operation, @run_id)",
            parameters=(
                bigquery.ScalarQueryParameter("source_operation", "STRING", operation),
                bigquery.ScalarQueryParameter("run_id", "STRING", profile.run_id),
            ),
            max_results=2,
        )
    elif version == execution_approval._RESULT_VERSION_V2:
        rows = _query(
            client,
            f"CALL `{PROJECT}.{APPROVAL_DATASET}."
            "sp_read_open_intelligence_execution_result_chain_v2`(@p_source_operation, @p_run_id)",
            parameters=(
                bigquery.ScalarQueryParameter("p_source_operation", "STRING", operation),
                bigquery.ScalarQueryParameter("p_run_id", "STRING", profile.run_id),
            ),
            max_results=2,
        )
    else:
        raise ReleaseRefusal(f"{operation} result chain version is unknown")
    if len(rows) != 1:
        raise ReleaseRefusal(f"{operation} result authority is unavailable")
    row = dict(rows[0])
    if version == execution_approval._RESULT_VERSION_V2:
        registry, binding = _admit_chain_row(row, operation, mode=mode, generation=generation)
        manifest_version = _MANIFEST_V2
    else:
        registry = generation.registry
        _origin, binding = _operation_binding(
            operation, manifest_version=_MANIFEST_V1, mode=mode, registry=registry
        )
        manifest_version = _MANIFEST_V1
    manifest_json = row.get("canonical_manifest_json")
    canonical_result_json = row.get("canonical_result_json")
    result_digest = row.get("result_digest")
    if (
        row.get("status") != "succeeded"
        or not isinstance(manifest_json, str)
        or not isinstance(canonical_result_json, str)
        or not isinstance(result_digest, str)
        or _DIGEST.fullmatch(result_digest) is None
        or hashlib.sha256(canonical_result_json.encode()).hexdigest() != result_digest
    ):
        raise ReleaseRefusal(f"{operation} result authority differs")
    try:
        manifest_payload = json.loads(manifest_json)
        manifest = execution_approval.validate_execution_manifest(
            manifest_payload, mode=mode, registry=registry
        )
        manifest_sha256 = execution_approval.manifest_sha256(
            manifest_payload, mode=mode, registry=registry
        )
        payload = json.loads(canonical_result_json)
    except (json.JSONDecodeError, ValueError, execution_approval.ApprovalRefusal) as error:
        raise ReleaseRefusal(f"{operation} result authority is invalid") from error
    execution_name = row.get("execution_name")
    if (
        manifest.manifest_version != manifest_version
        or manifest.operation != operation
        or row.get("manifest_sha256") != manifest_sha256
        or not isinstance(execution_name, str)
        or not execution_name.startswith(binding.job_resource + "/executions/")
        or row.get("job_resource") != manifest.job_resource
        or manifest.job_resource != binding.job_resource
        or manifest.service_identity != binding.service_identity
        or row.get("source_sha") != manifest.source_sha
        or row.get("image_uri") != manifest.image_uri
        or not isinstance(payload, dict)
        or payload.get("run_id") != profile.run_id
        or canonical_bytes(payload).decode() != canonical_result_json
    ):
        raise ReleaseRefusal(f"{operation} result authority is invalid")
    return {**row, "payload": payload, "registry": registry, "binding": binding}


def _read_quality_review_receipt(
    client: object,
    *,
    packet: Mapping[str, object],
    source_window_digest: str,
    candidate_projection_digest: str,
    profile: ReleaseRunProfile = R3_PROFILE,
) -> dict[str, object]:
    rows = _query(
        client,
        f"CALL `{PROJECT}.{DATASET}.sp_read_open_intelligence_quality_review_receipt_v1`(@run_id)",
        parameters=(bigquery.ScalarQueryParameter("run_id", "STRING", profile.run_id),),
        max_results=2,
    )
    if len(rows) != 1:
        raise ReleaseRefusal("r3 quality review receipt authority is unavailable")
    row = dict(rows[0])
    expected_row_fields = {
        "review_store_contract_version",
        "run_id",
        "canonical_review_receipt_json",
        "review_receipt_digest",
        "artifact_sha256",
        "source_window_digest",
        "candidate_projection_digest",
        "packet_digest",
        "registered_by",
        "registered_at",
    }
    canonical_json = row.get("canonical_review_receipt_json")
    if set(row) != expected_row_fields or not isinstance(canonical_json, str):
        raise ReleaseRefusal("r3 quality review receipt authority differs")
    try:
        raw_receipt = json.loads(canonical_json)
    except json.JSONDecodeError as error:
        raise ReleaseRefusal("r3 quality review receipt authority is invalid") from error
    if (
        not isinstance(raw_receipt, dict)
        or canonical_bytes(raw_receipt).decode() != canonical_json
        or row.get("review_store_contract_version") != "open_intelligence_quality_review_store_v1"
        or row.get("run_id") != profile.run_id
        or row.get("artifact_sha256") != hashlib.sha256(canonical_json.encode()).hexdigest()
        or row.get("review_receipt_digest") != raw_receipt.get("receipt_digest")
        or row.get("source_window_digest") != source_window_digest
        or row.get("candidate_projection_digest") != candidate_projection_digest
        or row.get("packet_digest") != packet.get("packet_digest")
        or row.get("registered_by")
        != "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab"
    ):
        raise ReleaseRefusal("r3 quality review receipt authority differs")
    try:
        receipt = {field: raw_receipt[field] for field in live_quality.REVIEW_RECEIPT_FIELDS}
        for field in (
            "reviewed_evidence_ids",
            "foreign_market_evidence_ids",
            "factual_conflict_evidence_ids",
            "uncertain_evidence_ids",
        ):
            receipt[field] = tuple(receipt[field])
        receipt["reviewed_at"] = datetime.fromisoformat(
            receipt["reviewed_at"].replace("Z", "+00:00")
        )
        live_quality.validate_review_receipt(
            receipt,
            packet=packet,
            source_window_digest=source_window_digest,
            candidate_projection_digest=candidate_projection_digest,
        )
    except (KeyError, TypeError, ValueError, live_quality.QualityRefusal) as error:
        raise ReleaseRefusal("r3 quality review receipt authority is invalid") from error
    return receipt


def _proof_ledger_binding(
    proof_result: Mapping[str, object],
    apply_result: Mapping[str, object],
    *,
    mode,
    registry,
    profile: ReleaseRunProfile = R3_PROFILE,
) -> tuple[bytes, str]:
    manifest_json = proof_result.get("canonical_manifest_json")
    manifest_digest = proof_result.get("manifest_sha256")
    proof_json = proof_result.get("canonical_result_json")
    proof = proof_result.get("payload")
    apply_digest = apply_result.get("result_digest")
    apply_reference = apply_result.get("result_reference")
    if (
        not isinstance(manifest_json, str)
        or not isinstance(manifest_digest, str)
        or not isinstance(proof_json, str)
        or not isinstance(proof, Mapping)
        or not isinstance(apply_digest, str)
        or _DIGEST.fullmatch(apply_digest) is None
        or not isinstance(apply_reference, str)
        or not apply_reference
    ):
        raise ReleaseRefusal("r3 proof ledger binding is invalid")
    try:
        manifest_payload = json.loads(manifest_json)
        manifest = execution_approval.validate_execution_manifest(
            manifest_payload, mode=mode, registry=registry
        )
        manifest_sha256 = execution_approval.manifest_sha256(
            manifest_payload, mode=mode, registry=registry
        )
    except (json.JSONDecodeError, ValueError, execution_approval.ApprovalRefusal) as error:
        raise ReleaseRefusal("r3 proof ledger binding is invalid") from error
    issuer_contract = canonical_bytes(
        {
            "contract_version": "r3-proof-issuer-durable-v1",
            "run_id": profile.run_id,
            "target_result_digest": apply_digest,
            "target_result_reference": apply_reference,
        }
    )
    if (
        manifest.operation != "r3_proof_issue"
        or manifest_digest != manifest_sha256
        or dict(manifest.input_artifacts).get("proof_issuer_contract")
        != hashlib.sha256(issuer_contract).hexdigest()
        or proof.get("r3_pre_execution_addendum_sha256") != manifest_digest
        or canonical_bytes(proof).decode() != proof_json
    ):
        raise ReleaseRefusal("r3 proof ledger binding differs")
    return proof_json.encode(), manifest_digest


def _control_digests(client: object, *, profile: ReleaseRunProfile = R3_PROFILE) -> tuple[str, str]:
    rows = _query(
        client,
        (
            f"SELECT {candidate_projection_digest_sql(profile.run_id)} AS candidate_projection_digest, "
            f"{review_packet_digest_sql(profile.run_id, sql_expression=False)} AS packet_digest"
        ),
        max_results=2,
    )
    if len(rows) != 1:
        raise ReleaseRefusal("r3 release control digest cardinality differs")
    row = dict(rows[0])
    values = (row.get("candidate_projection_digest"), row.get("packet_digest"))
    if any(not isinstance(value, str) or _DIGEST.fullmatch(value) is None for value in values):
        raise ReleaseRefusal("r3 release control digest differs")
    return values


def _read_review_packet(
    client: object, *, profile: ReleaseRunProfile = R3_PROFILE
) -> dict[str, object]:
    fields = ", ".join(f"`{field}`" for field in live_quality.REVIEW_ITEM_FIELDS)
    rows = _query(
        client,
        (
            f"SELECT {fields} FROM `{PROJECT}.{DATASET}.{EVIDENCE_TABLE}` "
            "WHERE run_id = @run_id AND client_scope_id != 'qa_canary' "
            "ORDER BY market, signal_id, evidence_id"
        ),
        parameters=(bigquery.ScalarQueryParameter("run_id", "STRING", profile.run_id),),
        max_results=50_001,
    )
    if not rows or len(rows) > 50_000:
        raise ReleaseRefusal("r3 review packet authority is unavailable")
    items = tuple(live_quality.review_item(dict(row)) for row in rows)
    if any(set(item) != set(live_quality.REVIEW_ITEM_FIELDS) for item in items):
        raise ReleaseRefusal("r3 review packet authority is invalid")
    preimage = {
        "review_contract_version": live_quality.REVIEW_CONTRACT_VERSION,
        "run_id": profile.run_id,
        "review_items": items,
    }
    return {**preimage, "packet_digest": live_quality.canonical_digest(preimage)}


def _build_release_inputs(
    *,
    receipt,
    execution_proof_bytes: bytes,
    execution_proof_manifest_sha256: str,
    quality_review_receipt: Mapping[str, object],
    review_packet: Mapping[str, object],
    candidate_projection_digest: str,
    apply_binding,
    profile: ReleaseRunProfile = R3_PROFILE,
    run_independence_policy: str | None = None,
) -> ReleaseInputs:
    # The caller names the upstream apply binding: the fresh path passes the v2 row it
    # resolved, a historical caller passes the retained v1 row it selected.
    try:
        proof = json.loads(execution_proof_bytes)
    except (TypeError, json.JSONDecodeError) as error:
        raise ReleaseRefusal("r3 execution proof authority is invalid") from error
    blocked_digest = run_receipt_digest(receipt)
    expected_counts = [
        ["candidates", receipt.candidate_count],
        ["evidence", receipt.evidence_count],
        ["membership", receipt.membership_count],
        ["lineage", receipt.lineage_count],
        ["predictions", receipt.prediction_count],
        ["outcomes", 0],
        ["analysis", receipt.analysis_count],
    ]
    try:
        started_at = datetime.fromisoformat(proof["started_at"].replace("Z", "+00:00"))
        completed_at = datetime.fromisoformat(proof["completed_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError) as error:
        raise ReleaseRefusal("r3 execution proof authority differs") from error
    if (
        not isinstance(proof, dict)
        or set(proof) != _EXECUTION_PROOF_FIELDS
        or execution_proof_bytes != canonical_bytes(proof)
        or proof.get("execution_proof_contract_version") != "r3-execution-proof-v1"
        or proof.get("run_id") != profile.run_id
        or proof.get("signal_date") != profile.cutoff
        or proof.get("status") != "succeeded"
        or not isinstance(proof.get("r3_pre_execution_addendum_sha256"), str)
        or _DIGEST.fullmatch(proof["r3_pre_execution_addendum_sha256"]) is None
        or proof.get("r3_pre_execution_addendum_sha256") != execution_proof_manifest_sha256
        or not isinstance(proof.get("execution_name"), str)
        or not proof["execution_name"].startswith(apply_binding.job_resource + "/executions/")
        or re.fullmatch(r"[^/]+", proof["execution_name"].rsplit("/executions/", 1)[1]) is None
        or proof.get("job_resource") != apply_binding.job_resource
        or not isinstance(proof.get("image_digest"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", proof["image_digest"]) is None
        or proof.get("service_identity") != apply_binding.service_identity
        or proof.get("command") != "python"
        or not isinstance(proof.get("args"), list)
        or not proof["args"]
        or proof["args"][0] != "scripts/staging/replay_open_intelligence.py"
        or not isinstance(proof.get("config_digest"), str)
        or _DIGEST.fullmatch(proof["config_digest"]) is None
        or started_at.tzinfo is None
        or completed_at.tzinfo is None
        or completed_at < started_at
        or proof.get("run_receipt_digest") != blocked_digest
        or proof.get("row_set_digest") != receipt.row_set_digest
        or proof.get("persisted_row_family_counts") != expected_counts
        or proof.get("source_sha") != receipt.source_sha
        or not isinstance(proof.get("source_sha"), str)
        or _SOURCE_SHA.fullmatch(proof["source_sha"]) is None
        or not isinstance(execution_proof_manifest_sha256, str)
        or _DIGEST.fullmatch(execution_proof_manifest_sha256) is None
    ):
        raise ReleaseRefusal("r3 execution proof authority differs")
    try:
        review_authority = live_quality.validate_review_receipt(
            quality_review_receipt,
            packet=review_packet,
            source_window_digest=receipt.source_window_digest,
            candidate_projection_digest=candidate_projection_digest,
        )
    except (TypeError, ValueError, live_quality.QualityRefusal) as error:
        raise ReleaseRefusal("r3 quality review receipt authority differs") from error
    packet_digest = review_packet.get("packet_digest")
    if (
        review_authority.run_id != profile.run_id
        or not isinstance(packet_digest, str)
        or _DIGEST.fullmatch(packet_digest) is None
        or _DIGEST.fullmatch(candidate_projection_digest) is None
    ):
        raise ReleaseRefusal("r3 quality review receipt authority differs")
    review_bytes = canonical_bytes(quality_review_receipt)
    review_artifact_sha256 = hashlib.sha256(review_bytes).hexdigest()
    proof_digest = hashlib.sha256(execution_proof_bytes).hexdigest()
    release_contract = {
        "contract_version": "r3-release-contract-v1",
        "run_id": profile.run_id,
        "source_sha": receipt.source_sha,
        "blocked_run_receipt_digest": blocked_digest,
        "candidate_projection_digest": candidate_projection_digest,
        "packet_digest": packet_digest,
        "quality_review_receipt_digest": review_authority.receipt_digest,
        "quality_review_receipt_artifact_sha256": review_artifact_sha256,
        "execution_proof_digest": proof_digest,
        "execution_proof_manifest_sha256": execution_proof_manifest_sha256,
    }
    if not profile.is_replay:
        release_contract.update(
            contract_version="release-profile-contract-v1",
            profile_name=profile.profile_name,
            profile_digest=profile.digest,
            source_policy_digest=profile.source_policy_digest,
            cutoff=profile.cutoff,
        )
    artifacts = {
        "blocked_run_receipt": canonical_bytes(receipt),
        "execution_proof": execution_proof_bytes,
        "quality_review_receipt": review_bytes,
        "release_contract": canonical_bytes(release_contract),
    }
    evidence = ReleaseEvidence(
        blocked_run_receipt_digest=blocked_digest,
        candidate_projection_digest=candidate_projection_digest,
        packet_digest=packet_digest,
        quality_review_receipt_digest=review_authority.receipt_digest,
        execution_proof_digest=proof_digest,
        release_contract_digest=hashlib.sha256(artifacts["release_contract"]).hexdigest(),
        source_sha=receipt.source_sha,
        profile_digest=None if profile.is_replay else profile.digest,
        independence_policy=None if profile.is_replay else run_independence_policy,
    )
    return ReleaseInputs(receipt=receipt, evidence=evidence, artifacts=artifacts)


def _read_release_inputs(
    client: object,
    *,
    version,
    mode,
    generation,
    apply_binding,
    profile: ReleaseRunProfile = R3_PROFILE,
) -> ReleaseInputs:
    receipt = _read_blocked_receipt(client, profile=profile)
    apply_result = _read_operation_result_chain(
        client, "r3_apply", version=version, mode=mode, generation=generation, profile=profile
    )
    if apply_result["binding"] != apply_binding:
        raise ReleaseRefusal("r3_apply result authority upstream identity differs")
    proof_result = _read_operation_result_chain(
        client, "r3_proof_issue", version=version, mode=mode, generation=generation, profile=profile
    )
    proof_bytes, proof_manifest_sha256 = _proof_ledger_binding(
        proof_result, apply_result, mode=mode, registry=proof_result["registry"], profile=profile
    )
    candidate_digest, packet_digest = _control_digests(client, profile=profile)
    review_packet = _read_review_packet(client, profile=profile)
    if review_packet.get("packet_digest") != packet_digest:
        raise ReleaseRefusal("r3 review packet digest differs")
    review_receipt = _read_quality_review_receipt(
        client,
        packet=review_packet,
        source_window_digest=receipt.source_window_digest,
        candidate_projection_digest=candidate_digest,
        profile=profile,
    )
    return _build_release_inputs(
        receipt=receipt,
        execution_proof_bytes=proof_bytes,
        execution_proof_manifest_sha256=proof_manifest_sha256,
        quality_review_receipt=review_receipt,
        review_packet=review_packet,
        candidate_projection_digest=candidate_digest,
        apply_binding=apply_binding,
        profile=profile,
    )


@dataclass(frozen=True)
class ReleaseReport:
    run_id: str
    blocked_receipt_digest: str
    released_receipt_digest: str
    run_receipt_digest: str
    source_window_digest: str
    candidate_projection_digest: str
    packet_digest: str
    review_receipt_digest: str
    approval_addendum_sha256: str
    released_at: datetime
    release_contract_version: str
    approved_by: str
    approval_document: str


@dataclass(frozen=True)
class ProfileReleaseReport(ReleaseReport):
    """The R3 report plus the live profile binding and the prediction enrollment proof."""

    profile_name: str
    profile_digest: str
    source_policy_digest: str
    cutoff: str
    transaction_readback: dict[str, int]
    prediction_readback: dict[str, int]
    prediction_output_digest: str
    prediction_enrollment: dict[str, object]
    prediction_accuracy_note: str


@dataclass(frozen=True)
class RollbackSelection:
    """A display selection of an older compatible release; evidence is never edited."""

    display_run_id: str
    source_policy_digest: str
    released_at: datetime
    retained_run_ids: tuple[str, ...]
    retained_predictions: object
    retained_results: object
    evidence_edits: tuple[()] = ()
    # A selection moves nothing. The served run is read as the newest enabled receipt
    # (``app/src/api/bq.py`` ``fetch_dynamic_run_receipt``) and the release write refuses
    # a run that is already released, so no existing write can serve an older release.
    served: bool = False


def select_rollback_release(
    *,
    current_run_id: str,
    released_records: Sequence[Mapping[str, object]],
    compatible_source_policy_digests: Sequence[str],
    newer_predictions: object = (),
    newer_results: object = (),
    compatible_source_shas: Sequence[str] | None = None,
    compatible_run_contract_versions: Sequence[str] | None = None,
) -> RollbackSelection:
    """Pick the newest earlier release under a compatible source policy.

    Every newer result and prediction row is retained as it is: the selection moves
    display to the older run and returns the newer rows untouched.

    When ``compatible_source_shas`` is given, a record is compatible only when its
    recorded ``source_sha`` (the image that produced the run) is in it, and when
    ``compatible_run_contract_versions`` is given, only when its recorded
    ``run_contract_version`` is. A record that does not carry the field, or carries a
    source policy digest of None, is unknown and never compatible. Compatibility is
    read, never made: no record is edited so that an older image accepts it.
    """
    policy_set = _rollback_compatible_set(
        compatible_source_policy_digests, _DIGEST, "source policy digests"
    )
    sha_set = (
        None
        if compatible_source_shas is None
        else _rollback_compatible_set(compatible_source_shas, _SOURCE_SHA, "source shas")
    )
    contract_set = (
        None
        if compatible_run_contract_versions is None
        else _rollback_compatible_set(
            compatible_run_contract_versions, _RUN_CONTRACT, "run contract versions"
        )
    )
    records = []
    for record in released_records:
        if not isinstance(record, Mapping):
            raise ReleaseRefusal("release record is invalid", code="rollback_record_invalid")
        run_id = record.get("run_id")
        released_at = record.get("released_at")
        policy = record.get("source_policy_digest")
        cutoff = record.get("cutoff")
        if (
            not isinstance(run_id, str)
            or _RUN_ID.fullmatch(run_id) is None
            or not isinstance(released_at, datetime)
            or released_at.tzinfo is None
            or (
                policy is not None
                and (not isinstance(policy, str) or _DIGEST.fullmatch(policy) is None)
            )
            or not isinstance(cutoff, str)
        ):
            raise ReleaseRefusal("release record is invalid", code="rollback_record_invalid")
        try:
            date.fromisoformat(cutoff)
        except ValueError as error:
            raise ReleaseRefusal(
                "release record is invalid", code="rollback_record_invalid"
            ) from error
        records.append(
            {
                "run_id": run_id,
                "released_at": released_at.astimezone(UTC),
                "source_policy_digest": policy,
                "cutoff": cutoff,
                "source_sha": record.get("source_sha"),
                "run_contract_version": record.get("run_contract_version"),
            }
        )
    current = next((record for record in records if record["run_id"] == current_run_id), None)
    if current is None:
        raise ReleaseRefusal(
            "the current release is not a released run", code="rollback_current_release_unknown"
        )
    compatible = [
        record
        for record in records
        if record["run_id"] != current_run_id
        and record["released_at"] < current["released_at"]
        and record["source_policy_digest"] in policy_set
        and record["cutoff"] <= current["cutoff"]
        and (sha_set is None or record["source_sha"] in sha_set)
        and (contract_set is None or record["run_contract_version"] in contract_set)
    ]
    if not compatible:
        raise ReleaseRefusal(
            "no earlier release is compatible with the source policy",
            code="rollback_no_compatible_release",
        )
    target = max(compatible, key=lambda record: (record["released_at"], record["run_id"]))
    # Every release at or after the target that is not the target is retained, so a
    # release tied with the target on released_at is never dropped.
    retained = tuple(
        sorted(
            record["run_id"]
            for record in records
            if record["released_at"] >= target["released_at"]
            and record["run_id"] != target["run_id"]
        )
    )
    return RollbackSelection(
        display_run_id=target["run_id"],
        source_policy_digest=target["source_policy_digest"],
        released_at=target["released_at"],
        retained_run_ids=retained,
        retained_predictions=newer_predictions,
        retained_results=newer_results,
    )


_RUN_CONTRACT = re.compile(r"[a-z0-9_][a-z0-9_.-]{0,127}\Z")
# Statements a rollback may never issue. Rollback is a read: it selects, it does not write.
# Keywords are matched outside quoted literals and identifiers, and a keyword joined to a
# letter, digit, hyphen or underscore is part of a name, so a run id such as
# ``r4-update-fix`` is data and never reads as a write.
_ROLLBACK_WRITE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|CALL|BEGIN|COMMIT|EXECUTE)"
    r"(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_ROLLBACK_QUOTED = re.compile(r"'(?:[^'\\]|\\.)*'|`[^`]*`")
# Quoting and comment forms the rollback SQL never uses. Any of them left outside a single
# quoted literal or a backtick identifier could hide a statement from the keyword check.
_ROLLBACK_FOREIGN_SYNTAX = ('"', "--", "#", "/*", "*/", "'")


def _rollback_compatible_set(values: object, pattern: re.Pattern[str], name: str) -> frozenset[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ReleaseRefusal(
            f"compatible {name} are invalid", code="rollback_compatibility_invalid"
        )
    for value in values:
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise ReleaseRefusal(
                f"compatible {name} are invalid", code="rollback_compatibility_invalid"
            )
    return frozenset(values)


def _rollback_reads_only(query_runner):
    """Wrap a query runner so the rollback path can issue a single SELECT and nothing else.

    The statement separator is refused on the raw text, before any stripping, so no
    quote, comment or string form can hide a second statement; ``_escape`` never
    produces ``;`` and a value containing one simply refuses. A triple quote is refused
    on the raw text too. Single quoted literals and backtick identifiers are then
    removed, and what is left must start with SELECT, carry no double quote, comment
    marker or stray single quote, and name no write keyword.
    """

    def run(sql: str):
        text = sql.strip()
        bare = _ROLLBACK_QUOTED.sub(" ", text)
        if (
            ";" in text
            or "'''" in text
            or not bare.upper().startswith("SELECT ")
            or any(marker in bare for marker in _ROLLBACK_FOREIGN_SYNTAX)
            or _ROLLBACK_WRITE.search(bare) is not None
        ):
            raise ReleaseRefusal("rollback issues reads only", code="rollback_write_refused")
        return list(query_runner(text))

    return run


def _quoted_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{_escape(value)}'" for value in values)


def read_rollback_selection(
    *,
    current_run_id: str,
    query_runner,
    compatible_source_policy_digests: Sequence[str],
    compatible_source_shas: Sequence[str],
    compatible_run_contract_versions: Sequence[str],
    profiles: Mapping[str, ReleaseRunProfile] | None = None,
) -> RollbackSelection:
    """Select the rollback target from the durable release records, reading only.

    Reads every quality release record under the current release contract, the run
    receipt of each released run and the approved profile registry, then selects the
    newest earlier release whose image (``source_sha``), run contract and source policy
    digest the target image accepts. The source policy digest is not a column of the
    release record: it is read from the approved immutable profile that names the run,
    so a run no registered profile names, the retained R3 replay among them, carries an
    unknown policy and is never selected. A released run whose receipt is not enabled is
    never selected either, and a release record with no receipt refuses as invalid.

    Every release at or after the target other than the target itself is retained. For
    those runs the identity of each prediction (``run_id``, ``prediction_id``) and of each
    outcome scored against those predictions (``run_id``, the evaluation run,
    ``prediction_id`` and ``outcome_id``) is read back and returned as it is; no other
    column is read.
    Every statement passes through a guard that refuses anything but one SELECT, so the
    path can neither edit evidence nor write a release. The returned selection is not
    served: serving an older release is not expressible by the existing release write.
    """
    reader = _rollback_reads_only(query_runner)
    registry = RELEASE_PROFILES if profiles is None else profiles
    policies: dict[str, str | None] = {}
    for profile in registry.values():
        if not isinstance(profile, ReleaseRunProfile):
            raise ReleaseRefusal("release profile is invalid", code="release_profile_invalid")
        policies[profile.run_id] = profile.source_policy_digest
    released = reader(
        "SELECT run_id, released_at, release_contract_version FROM "
        f"`{PROJECT}.{DATASET}.{RELEASE_RECORD_TABLE}` "
        f"WHERE release_contract_version = '{RELEASE_CONTRACT_VERSION}'"
    )
    run_ids = []
    for row in released:
        run_id = row.get("run_id") if isinstance(row, Mapping) else None
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise ReleaseRefusal("release record is invalid", code="rollback_record_invalid")
        run_ids.append(run_id)
    if len(set(run_ids)) != len(run_ids):
        raise ReleaseRefusal("release record is invalid", code="rollback_record_invalid")
    receipts: dict[str, Mapping[str, object]] = {}
    if run_ids:
        for row in reader(
            "SELECT run_id, run_contract_version, source_sha, observation_end, "
            f"display_release_state FROM `{PROJECT}.{DATASET}.{RECEIPT_TABLE}` "
            f"WHERE run_id IN ({_quoted_list(sorted(run_ids))})"
        ):
            if not isinstance(row, Mapping) or row.get("run_id") in receipts:
                raise ReleaseRefusal("release record is invalid", code="rollback_record_invalid")
            receipts[row.get("run_id")] = row
    records = []
    for row in released:
        run_id = row["run_id"]
        receipt = receipts.get(run_id)
        if receipt is None:
            raise ReleaseRefusal("release record is invalid", code="rollback_record_invalid")
        servable = receipt.get("display_release_state") == "enabled"
        observation_end = receipt.get("observation_end")
        records.append(
            {
                "run_id": run_id,
                "released_at": row.get("released_at"),
                "source_policy_digest": policies.get(run_id),
                "cutoff": (
                    observation_end.isoformat()
                    if isinstance(observation_end, date)
                    else observation_end
                ),
                "source_sha": receipt.get("source_sha") if servable else None,
                "run_contract_version": (receipt.get("run_contract_version") if servable else None),
            }
        )
    selection = select_rollback_release(
        current_run_id=current_run_id,
        released_records=records,
        compatible_source_policy_digests=compatible_source_policy_digests,
        compatible_source_shas=compatible_source_shas,
        compatible_run_contract_versions=compatible_run_contract_versions,
    )
    predictions: tuple[dict[str, object], ...] = ()
    results: tuple[dict[str, object], ...] = ()
    if selection.retained_run_ids:
        retained = _quoted_list(selection.retained_run_ids)
        predictions = tuple(
            dict(row)
            for row in reader(
                "SELECT run_id, prediction_id FROM "
                f"`{PROJECT}.{DATASET}.{PREDICTIONS_TABLE}` WHERE run_id IN ({retained})"
            )
        )
        # An outcome's run_id is its evaluation run, not the discovery run, so the
        # outcomes are read by the prediction they score.
        prediction_ids = sorted(
            {
                row["prediction_id"]
                for row in predictions
                if isinstance(row.get("prediction_id"), str)
            }
        )
        if prediction_ids:
            results = tuple(
                dict(row)
                for row in reader(
                    "SELECT run_id, prediction_id, outcome_id FROM "
                    f"`{PROJECT}.{DATASET}.{OUTCOMES_TABLE}` "
                    f"WHERE prediction_id IN ({_quoted_list(prediction_ids)})"
                )
            )
    return replace(selection, retained_predictions=predictions, retained_results=results)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _read_receipt(run_id: str, query_runner) -> object:
    columns = ", ".join(RUN_RECEIPT_ROW_FIELDS)
    rows = list(
        query_runner(
            f"SELECT {columns} FROM `{PROJECT}.{DATASET}.{RECEIPT_TABLE}` "
            f"WHERE run_id = '{_escape(run_id)}'"
        )
    )
    if len(rows) != 1:
        raise ReleaseRefusal(f"expected exactly one receipt for run {run_id}, found {len(rows)}")
    fields = {name: rows[0].get(name) for name in RUN_RECEIPT_ROW_FIELDS}
    market_scope = fields.get("market_scope")
    if isinstance(market_scope, list):
        fields["market_scope"] = tuple(market_scope)
    return build_run_receipt(**fields)


def _release_admission_parts(run_id: str) -> tuple[str, str]:
    escaped = _escape(run_id)
    ctes = (
        f"candidates AS ("
        f"SELECT client_scope_id, run_id, signal_date, market, signal_id, label, "
        f"label_member_identity, "
        f"evidence_state FROM `{PROJECT}.{DATASET}.{CANDIDATES_TABLE}` "
        f"WHERE run_id = '{escaped}'), "
        f"evidence AS ("
        f"SELECT client_scope_id, run_id, signal_date, market, signal_id, availability "
        f"FROM `{PROJECT}.{DATASET}.{EVIDENCE_TABLE}` WHERE run_id = '{escaped}'), "
        f"memberships AS ("
        f"SELECT client_scope_id, run_id, signal_date, market, signal_id, "
        f"member_identity, canonical_value, candidate_type, qualifies_evidence FROM "
        f"`{PROJECT}.{DATASET}.{MEMBERSHIP_TABLE}` WHERE run_id = '{escaped}'), "
        f"predictions AS ("
        f"SELECT client_scope_id, run_id, signal_date, market, signal_id, prediction_id, "
        f"evidence_state, source_families, display_eligible FROM "
        f"`{PROJECT}.{DATASET}.{PREDICTIONS_TABLE}` WHERE run_id = '{escaped}'), "
        f"lineage AS (SELECT run_id FROM `{PROJECT}.{DATASET}.{LINEAGE_TABLE}` "
        f"WHERE run_id = '{escaped}'), "
        f"outcomes AS (SELECT run_id FROM `{PROJECT}.{DATASET}.{OUTCOMES_TABLE}` "
        f"WHERE run_id = '{escaped}'), "
        f"analysis AS (SELECT run_id FROM `{PROJECT}.{DATASET}.{ANALYSIS_TABLE}` "
        f"WHERE run_id = '{escaped}')"
    )
    qualifying = (
        f"SELECT p.prediction_id FROM predictions p JOIN candidates c "
        f"ON c.client_scope_id = p.client_scope_id AND c.run_id = p.run_id "
        f"AND c.signal_date = p.signal_date AND c.market = p.market "
        f"AND c.signal_id = p.signal_id "
        f"WHERE c.evidence_state = 'ready' AND p.evidence_state = 'ready' "
        f"AND NOT p.display_eligible AND ARRAY_LENGTH(p.source_families) >= 2 "
        f"AND (SELECT COUNT(*) FROM predictions px "
        f"WHERE px.client_scope_id = p.client_scope_id AND px.run_id = p.run_id "
        f"AND px.signal_date = p.signal_date AND px.market = p.market "
        f"AND px.signal_id = p.signal_id) = 1 "
        f"AND EXISTS (SELECT 1 FROM evidence e "
        f"WHERE e.client_scope_id = p.client_scope_id AND e.run_id = p.run_id "
        f"AND e.signal_date = p.signal_date AND e.market = p.market "
        f"AND e.signal_id = p.signal_id AND e.availability = 'available') "
        f"AND (SELECT COUNT(*) FROM memberships m "
        f"WHERE m.client_scope_id = p.client_scope_id AND m.run_id = p.run_id "
        f"AND m.signal_date = p.signal_date AND m.market = p.market "
        f"AND m.signal_id = p.signal_id AND m.member_identity = c.label_member_identity "
        f"AND m.canonical_value = c.label AND m.qualifies_evidence) = 1"
    )
    return ctes, qualifying


def _release_admission_sql(run_id: str) -> str:
    ctes, qualifying = _release_admission_parts(run_id)
    return (
        f"WITH {ctes} SELECT "
        f"(SELECT COUNT(*) FROM candidates) AS candidate_rows, "
        f"(SELECT COUNT(*) FROM evidence) AS evidence_rows, "
        f"(SELECT COUNT(*) FROM memberships) AS membership_rows, "
        f"(SELECT COUNT(*) FROM lineage) AS lineage_rows, "
        f"(SELECT COUNT(*) FROM predictions) AS prediction_rows, "
        f"(SELECT COUNT(*) FROM outcomes) AS outcome_rows, "
        f"(SELECT COUNT(*) FROM analysis) AS analysis_rows, "
        f"(SELECT COUNT(*) FROM ({qualifying})) AS qualifying_prediction_rows"
    )


def _read_release_admission(run_id: str, query_runner) -> dict[str, int]:
    rows = list(query_runner(_release_admission_sql(run_id)))
    if len(rows) != 1:
        raise ReleaseRefusal("release admission readback did not return exactly one row")
    fields = (
        "candidate_rows",
        "evidence_rows",
        "membership_rows",
        "lineage_rows",
        "prediction_rows",
        "outcome_rows",
        "analysis_rows",
        "qualifying_prediction_rows",
    )
    try:
        return {field: int(rows[0][field]) for field in fields}
    except (KeyError, TypeError, ValueError) as error:
        raise ReleaseRefusal("release admission readback is invalid") from error


def _window_closes_on(end: datetime, cutoff: date) -> bool:
    end = end.astimezone(UTC)
    if end.date() == cutoff:
        return True
    return end.date() == cutoff + timedelta(days=1) and end.time() == time(0)


def _require_source_authority(
    profile: ReleaseRunProfile, receipt, source_authority: Sequence[Mapping[str, object]]
) -> None:
    """Every market of the run is covered by a succeeded capture under the profile policy."""
    if profile.source_policy_digest is None:
        raise ReleaseRefusal(
            "release profile binds no source policy", code="release_source_authority_incomplete"
        )
    try:
        entries = [validate_capture_entry(entry) for entry in source_authority]
    except (TypeError, ValueError) as error:
        raise ReleaseRefusal(
            "source authority entry is invalid", code="release_source_authority_incomplete"
        ) from error
    cutoff = date.fromisoformat(profile.cutoff)
    covered: set[str] = set()
    for entry in entries:
        if (
            entry["completion_state"] != "succeeded"
            or entry["policy_digest"] != profile.source_policy_digest
            or not _window_closes_on(entry["observation_window_end"], cutoff)
        ):
            continue
        covered.update(entry["markets"])
    if set(receipt.market_scope) - covered:
        raise ReleaseRefusal(
            "source authority does not cover every market at the profile cutoff",
            code="release_source_authority_incomplete",
        )


def _expected_admission(receipt) -> dict[str, int]:
    return {
        "candidate_rows": receipt.candidate_count,
        "evidence_rows": receipt.evidence_count,
        "membership_rows": receipt.membership_count,
        "lineage_rows": receipt.lineage_count,
        "prediction_rows": receipt.prediction_count,
        "outcome_rows": 0,
        "analysis_rows": receipt.analysis_count,
        "qualifying_prediction_rows": receipt.prediction_count,
    }


def _require_membership_certification(run_id: str, receipt, query_runner) -> dict[str, int]:
    """The certified row set, membership included, is what the warehouse still holds."""
    admission = _read_release_admission(run_id, query_runner)
    if admission != _expected_admission(receipt):
        raise ReleaseRefusal(
            "membership or row counts changed after certification",
            code="release_membership_changed_after_certification",
        )
    digest_rows = list(query_runner(f"SELECT {row_set_digest_sql(run_id)} AS row_set_digest"))
    if digest_rows != [{"row_set_digest": receipt.row_set_digest}]:
        raise ReleaseRefusal(
            "row set digest changed after certification",
            code="release_membership_changed_after_certification",
        )
    return admission


def _enrollment_refusal(message: str) -> ReleaseRefusal:
    return ReleaseRefusal(message, code="prediction_enrollment_differs")


def _enroll_predictions(profile: ReleaseRunProfile, receipt, query_runner) -> dict[str, object]:
    """Rebuild the prediction row of every promoted signal and prove the persisted rows match."""
    rules = profile.prediction_rules
    if rules is None:
        raise ReleaseRefusal(
            "release profile declares no prediction rules", code="prediction_enrollment_incomplete"
        )
    run_id = profile.run_id
    candidates = [
        dict(row)
        for row in query_runner(
            f"SELECT {', '.join(_PROMOTED_CANDIDATE_FIELDS)} FROM "
            f"`{PROJECT}.{DATASET}.{CANDIDATES_TABLE}` WHERE run_id = '{_escape(run_id)}' "
            f"AND evidence_state = 'ready' AND client_scope_id != '{QA_CLIENT_SCOPE_ID}' "
            f"ORDER BY market, signal_id"
        )
    ]
    persisted = [
        dict(row)
        for row in query_runner(
            f"SELECT {', '.join(PREDICTION_ROW_FIELDS)} FROM "
            f"`{PROJECT}.{DATASET}.{PREDICTIONS_TABLE}` WHERE run_id = '{_escape(run_id)}' "
            f"ORDER BY market, signal_date, signal_id"
        )
    ]
    by_signal: dict[tuple[object, object], dict[str, object]] = {}
    for row in persisted:
        key = (row.get("market"), row.get("signal_id"))
        if key in by_signal:
            raise _enrollment_refusal("a promoted signal carries two prediction rows")
        if row.get("display_eligible") is not False:
            raise _enrollment_refusal("a prediction row is display eligible before release")
        by_signal[key] = row
    if not candidates:
        raise ReleaseRefusal(
            "the run has no promoted signal to enroll", code="prediction_enrollment_incomplete"
        )
    promoted = []
    predicted_at: set[object] = set()
    for candidate in candidates:
        if candidate.get("evidence_state") != "ready":
            raise _enrollment_refusal("the promoted set carries an unready candidate")
        row = by_signal.get((candidate.get("market"), candidate.get("signal_id")))
        if row is None:
            raise ReleaseRefusal(
                f"promoted signal {candidate.get('signal_id')} has no prediction row",
                code="prediction_enrollment_incomplete",
            )
        try:
            promoted.append(
                PromotedSignal(candidate, row.get("source_families"), row.get("first_seen_at"))
            )
        except ValueError as error:
            raise _enrollment_refusal(
                "a prediction row does not bind its promoted signal"
            ) from error
        predicted_at.add(row.get("predicted_at"))
    if len(persisted) != len(candidates):
        raise _enrollment_refusal("a prediction row has no promoted signal")
    if len(predicted_at) != 1:
        raise _enrollment_refusal("the run's predictions were not issued at one instant")
    try:
        rebuilt = build_signal_prediction_rows(
            promoted_signals=promoted, predicted_at=next(iter(predicted_at)), rules=rules
        )
    except ValueError as error:
        raise _enrollment_refusal(
            "prediction rows cannot be rebuilt from the promoted set"
        ) from error
    for row in rebuilt:
        stored = by_signal[(row["market"], row["signal_id"])]
        if canonical_bytes(stored) != canonical_bytes(row):
            raise _enrollment_refusal(
                f"prediction row for {row['signal_id']} differs from its deterministic rebuild"
            )
    if len(rebuilt) != receipt.prediction_count:
        raise _enrollment_refusal("prediction count differs from the receipt")
    return {
        "output_digest": canonical_digest(list(rebuilt)),
        "readback": {
            "prediction_rows": len(persisted),
            "eligible_rows": sum(bool(row["display_eligible"]) for row in persisted),
            "promoted_signals": len(candidates),
        },
        "enrollment": {
            "horizon_days": rules.evaluation_days,
            "predicted_at": sorted(predicted_at),
            "evaluation_dates": sorted({row["evaluation_date"] for row in rebuilt}),
            "baseline_fields": sorted({key for row in rebuilt for key in row["baseline"]}),
            "source_families": [list(row["source_families"]) for row in rebuilt],
        },
    }


def _release_open_intelligence_run(
    *,
    run_id: str,
    released_at: datetime,
    query_runner,
    evidence: ReleaseEvidence,
    profile: ReleaseRunProfile = R3_PROFILE,
    source_authority: Sequence[Mapping[str, object]] = (),
) -> ReleaseReport:
    if not isinstance(profile, ReleaseRunProfile):
        raise ReleaseRefusal("release profile is invalid", code="release_profile_invalid")
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ReleaseRefusal("run_id is invalid")
    if not isinstance(released_at, datetime) or released_at.tzinfo is None:
        raise ReleaseRefusal("released_at must be timezone aware")
    released_at = released_at.astimezone(UTC)
    if not isinstance(evidence, ReleaseEvidence) or any(
        _DIGEST.fullmatch(value) is None
        for value in (
            evidence.blocked_run_receipt_digest,
            evidence.candidate_projection_digest,
            evidence.packet_digest,
            evidence.quality_review_receipt_digest,
            evidence.execution_proof_digest,
            evidence.release_contract_digest,
        )
    ):
        raise ReleaseRefusal("release evidence authority is invalid")
    if _SOURCE_SHA.fullmatch(evidence.source_sha) is None:
        raise ReleaseRefusal("release source authority is invalid")
    if run_id != profile.run_id:
        if profile.is_replay:
            raise ReleaseRefusal("release identity differs from approved R3 run")
        raise ReleaseRefusal(
            "release identity differs from the approved profile run",
            code="release_profile_identity_differs",
        )
    if not profile.is_replay:
        if profile.canary_namespace:
            raise ReleaseRefusal(
                "a canary namespace profile is never released for display",
                code="release_canary_namespace",
            )
        if evidence.profile_digest != profile.digest:
            raise ReleaseRefusal(
                "release evidence binds a different profile digest",
                code="release_profile_digest_differs",
            )
        if evidence.independence_policy != profile.independence_policy:
            raise ReleaseRefusal(
                "the run's readiness independence policy differs from the profile policy",
                code="release_independence_policy_differs",
            )

    receipt = _read_receipt(run_id, query_runner)
    if not profile.is_replay:
        if receipt.signal_date.isoformat() != profile.cutoff:
            raise ReleaseRefusal(
                "receipt cutoff differs from the approved profile cutoff",
                code="release_profile_cutoff_differs",
            )
        _require_source_authority(profile, receipt, source_authority)
    if receipt.status != "completed":
        raise ReleaseRefusal(
            f"run {run_id} has status {receipt.status}, not completed",
            code=_live_code(profile, "release_run_not_completed"),
        )
    if receipt.complete_partitions is not True:
        raise ReleaseRefusal(
            f"run {run_id} does not prove complete partitions",
            code=_live_code(profile, "release_partitions_incomplete"),
        )
    if receipt.client_scope_id == QA_CLIENT_SCOPE_ID:
        raise ReleaseRefusal(
            "a canary run is never released for display",
            code=_live_code(profile, "release_canary_namespace"),
        )
    if receipt.display_release_state != "blocked":
        raise ReleaseRefusal(
            f"run {run_id} is already released",
            code=_live_code(profile, "release_already_released"),
        )
    if receipt.candidate_count < 1:
        raise ReleaseRefusal(
            f"run {run_id} has no candidate", code=_live_code(profile, "release_run_empty")
        )
    if receipt.evidence_count < 1:
        raise ReleaseRefusal(
            f"run {run_id} has no evidence", code=_live_code(profile, "release_run_empty")
        )
    if receipt.membership_count < 1:
        raise ReleaseRefusal(
            f"run {run_id} has no membership", code=_live_code(profile, "release_run_empty")
        )
    if receipt.prediction_count < 1:
        raise ReleaseRefusal(
            f"run {run_id} has no displayable prediction",
            code=_live_code(profile, "release_run_empty"),
        )
    blocked_receipt_digest = run_receipt_digest(receipt)
    if (
        blocked_receipt_digest != evidence.blocked_run_receipt_digest
        or receipt.source_sha != evidence.source_sha
    ):
        raise ReleaseRefusal(
            "blocked run receipt differs from durable release evidence",
            code=_live_code(profile, "release_evidence_differs"),
        )
    existing = list(
        query_runner(
            f"SELECT COUNT(*) AS existing FROM "
            f"`{PROJECT}.{DATASET}.{RELEASE_RECORD_TABLE}` "
            f"WHERE run_id = '{_escape(run_id)}'"
        )
    )
    if existing and int(existing[0].get("existing", 0)) > 0:
        raise ReleaseRefusal(
            f"a release record already exists for run {run_id}",
            code=_live_code(profile, "release_duplicate"),
        )
    enrollment = None
    if not profile.is_replay:
        # A live profile proves the certified membership and the prediction enrollment of
        # every promoted signal before the display transaction; the transaction below then
        # asserts the same counts and digests again under its own guards.
        _require_membership_certification(run_id, receipt, query_runner)
        enrollment = _enroll_predictions(profile, receipt, query_runner)

    # The digest of the receipt exactly as it was read and approved: the
    # blocked state the decision was taken against.
    released_receipt_digest = run_receipt_digest(replace(receipt, display_release_state="enabled"))

    # One transaction. The receipt update is guarded by every field the
    # decision rested on, so a receipt that changed since the read updates
    # zero rows and the readback below refuses.
    stamp = released_at.strftime("%Y-%m-%d %H:%M:%S.%f")
    admission_sql = _release_admission_sql(run_id)
    projection_digest_sql = candidate_projection_digest_sql(run_id)
    packet_digest_sql = review_packet_digest_sql(run_id, sql_expression=False)
    released_receipt_digest_sql = run_receipt_digest_sql("released_receipt")
    blocked_receipt_digest_sql = run_receipt_digest_sql("blocked_receipt")
    transaction_row_set_digest_sql = row_set_digest_sql(run_id)
    query_runner(
        # DDL cannot live inside a BigQuery transaction; the record table was
        # created idempotently above.
        f"BEGIN TRANSACTION;\n"
        f"ASSERT (SELECT {blocked_receipt_digest_sql} FROM "
        f"`{PROJECT}.{DATASET}.{RECEIPT_TABLE}` blocked_receipt "
        f"WHERE run_id = '{_escape(run_id)}' AND display_release_state = 'blocked') "
        f"= '{evidence.blocked_run_receipt_digest}' AS 'blocked_receipt_digest changed';\n"
        f"ASSERT (SELECT candidate_rows = {receipt.candidate_count} "
        f"AND evidence_rows = {receipt.evidence_count} "
        f"AND membership_rows = {receipt.membership_count} "
        f"AND lineage_rows = {receipt.lineage_count} "
        f"AND prediction_rows = {receipt.prediction_count} "
        f"AND outcome_rows = 0 "
        f"AND analysis_rows = {receipt.analysis_count} "
        f"AND qualifying_prediction_rows = {receipt.prediction_count} "
        f"FROM ({admission_sql})) AS 'release admission changed';\n"
        f"ASSERT {transaction_row_set_digest_sql} = '{receipt.row_set_digest}' "
        f"AS 'row_set_digest changed';\n"
        f"ASSERT {projection_digest_sql} = '{evidence.candidate_projection_digest}' "
        f"AS 'candidate projection digest changed';\n"
        f"ASSERT {packet_digest_sql} = '{evidence.packet_digest}' "
        f"AS 'review packet digest changed';\n"
        f"UPDATE `{PROJECT}.{DATASET}.{RECEIPT_TABLE}` "
        f"SET display_release_state = 'enabled' "
        f"WHERE run_id = '{_escape(run_id)}' "
        f"AND display_release_state = 'blocked' "
        f"AND status = 'completed' "
        f"AND complete_partitions "
        f"AND row_set_digest = '{_escape(receipt.row_set_digest)}';\n"
        f"ASSERT @@row_count = 1 AS 'receipt update count changed';\n"
        f"ASSERT (SELECT {released_receipt_digest_sql} FROM "
        f"`{PROJECT}.{DATASET}.{RECEIPT_TABLE}` released_receipt "
        f"WHERE run_id = '{_escape(run_id)}') = '{released_receipt_digest}' "
        f"AS 'released receipt digest changed';\n"
        f"INSERT INTO `{PROJECT}.{DATASET}.{RELEASE_RECORD_TABLE}` "
        f"({', '.join(QUALITY_RELEASE_FIELDS)}) "
        f"SELECT '{_escape(run_id)}', '{released_receipt_digest}', "
        f"'{receipt.source_window_digest}', '{evidence.candidate_projection_digest}', "
        f"'{evidence.packet_digest}', '{evidence.quality_review_receipt_digest}', "
        f"'{evidence.release_contract_digest}', TIMESTAMP '{stamp}+00', "
        f"'{RELEASE_CONTRACT_VERSION}' "
        f"FROM (SELECT 1) WHERE NOT EXISTS ("
        f"SELECT 1 FROM `{PROJECT}.{DATASET}.{RELEASE_RECORD_TABLE}` "
        f"WHERE run_id = '{_escape(run_id)}');\n"
        f"ASSERT @@row_count = 1 AS 'release record insert count changed';\n"
        f"COMMIT TRANSACTION;"
    )

    # Readback: the flip must actually have happened. A guarded update that
    # matched zero rows means the receipt changed between read and release.
    after = _read_receipt(run_id, query_runner)
    if after.display_release_state != "enabled":
        raise ReleaseRefusal(
            f"run {run_id} did not release: the receipt changed between read and release"
        )
    if (
        after.row_set_digest != receipt.row_set_digest
        or run_receipt_digest(after) != released_receipt_digest
    ):
        raise ReleaseRefusal("released receipt digest or row set changed")
    post_admission = _read_release_admission(run_id, query_runner)
    expected_admission = _expected_admission(receipt)
    if post_admission != expected_admission:
        raise ReleaseRefusal("post-commit release admission changed")
    digest_rows = list(query_runner(f"SELECT {row_set_digest_sql(run_id)} AS row_set_digest"))
    if digest_rows != [{"row_set_digest": receipt.row_set_digest}]:
        raise ReleaseRefusal("post-commit row_set_digest changed")
    prediction_readback = list(
        query_runner(
            f"SELECT COUNT(*) AS prediction_rows, COUNTIF(display_eligible) AS eligible_rows "
            f"FROM `{PROJECT}.{DATASET}.{PREDICTIONS_TABLE}` WHERE run_id = '{run_id}'"
        )
    )
    if prediction_readback != [{"prediction_rows": receipt.prediction_count, "eligible_rows": 0}]:
        raise ReleaseRefusal("predictions changed during release")
    expected_release_record = {
        "run_id": run_id,
        "run_receipt_digest": released_receipt_digest,
        "source_window_digest": receipt.source_window_digest,
        "candidate_projection_digest": evidence.candidate_projection_digest,
        "packet_digest": evidence.packet_digest,
        "review_receipt_digest": evidence.quality_review_receipt_digest,
        "approval_addendum_sha256": evidence.release_contract_digest,
        "released_at": released_at,
        "release_contract_version": RELEASE_CONTRACT_VERSION,
    }
    recorded = list(
        query_runner(
            f"SELECT {', '.join(QUALITY_RELEASE_FIELDS)} FROM "
            f"`{PROJECT}.{DATASET}.{RELEASE_RECORD_TABLE}` "
            f"WHERE run_id = '{_escape(run_id)}'"
        )
    )
    if len(recorded) != 1 or dict(recorded[0]) != expected_release_record:
        raise ReleaseRefusal("release record readback differs")
    report_fields = {
        "run_id": run_id,
        "blocked_receipt_digest": blocked_receipt_digest,
        "released_receipt_digest": released_receipt_digest,
        "run_receipt_digest": released_receipt_digest,
        "source_window_digest": receipt.source_window_digest,
        "candidate_projection_digest": evidence.candidate_projection_digest,
        "packet_digest": evidence.packet_digest,
        "review_receipt_digest": evidence.quality_review_receipt_digest,
        "approval_addendum_sha256": evidence.release_contract_digest,
        "released_at": released_at,
        "release_contract_version": RELEASE_CONTRACT_VERSION,
        "approved_by": "durable_execution_approval",
    }
    if enrollment is None:
        return ReleaseReport(**report_fields, approval_document="r3-release-contract-v1")
    return ProfileReleaseReport(
        **report_fields,
        approval_document="release-profile-contract-v1",
        profile_name=profile.profile_name,
        profile_digest=profile.digest,
        source_policy_digest=profile.source_policy_digest,
        cutoff=profile.cutoff,
        transaction_readback=post_admission,
        prediction_readback=enrollment["readback"],
        prediction_output_digest=enrollment["output_digest"],
        prediction_enrollment=enrollment["enrollment"],
        prediction_accuracy_note=PREDICTION_ACCURACY_NOTE,
    )


def release_live_profile(
    profile: ReleaseRunProfile,
    *,
    adapter: object,
    released_at: datetime,
    telemetry: object | None = None,
) -> ProfileReleaseReport:
    """Release a live profile through its adapter: compose, certify and release entry points.

    The adapter is the seam the daily stage handlers provide. ``compose(profile)`` returns
    the durable source authority (the admitted capture entries covering the run),
    ``certify(profile)`` returns the certified ``ReleaseEvidence`` read back from the
    certification record, and ``release(profile)`` returns the warehouse query runner the
    display transaction runs against. The authority to call this path is the caller's:
    the daily kernel dispatches it only under an issued release receipt, and the command
    line keeps refusing a live profile without an adapter.
    """
    if not isinstance(profile, ReleaseRunProfile) or profile.is_replay:
        raise ReleaseRefusal(
            "the retained R3 replay does not release through an adapter",
            code="release_profile_invalid",
        )
    if adapter is None or any(
        not callable(getattr(adapter, name, None)) for name in ("compose", "certify", "release")
    ):
        raise ReleaseRefusal(
            f"release profile {profile.profile_name} has no release adapter",
            code="release_profile_adapter_unavailable",
        )
    source_authority = adapter.compose(profile)
    evidence = adapter.certify(profile)
    query_runner = adapter.release(profile)
    report = _release_open_intelligence_run(
        run_id=profile.run_id,
        released_at=released_at,
        query_runner=query_runner,
        evidence=evidence,
        profile=profile,
        source_authority=source_authority,
    )
    if not isinstance(report, ProfileReleaseReport):
        raise ReleaseRefusal("live profile release did not return a profile report")
    if telemetry is not None:
        # The release decides one run. Its readback counts released signals in
        # their own unit; observation level telemetry is not part of the
        # decision record here and is recorded as unknown, not derived.
        telemetry.count(
            "released_signals",
            int(report.transaction_readback["candidate_rows"]),
            operation_id=report.run_id,
            observed_at=released_at,
        )
        telemetry.boundary_unavailable(
            "release", operation_id=report.run_id, reason_code="release_decision_is_run_scoped"
        )
    return report


def _main_impl(
    argv: Sequence[str] | None = None, *, _terminal_context: list[object], adapter: object = None
) -> int:
    profile = _select_profile(_parse_cli(tuple(sys.argv[1:] if argv is None else argv)))
    # Own binding and upstream apply binding are resolved separately from the packaged
    # active pair before any read; the issued authority must carry the same pair.
    fresh_generation = execution_generations.active_generation()
    _require_profile_generation(profile, fresh_generation)
    if not profile.is_replay:
        # A live profile releases only through the adapter its stage handlers provide;
        # without one the command refuses rather than releasing on partial authority.
        if adapter is None:
            raise ReleaseRefusal(
                f"release profile {profile.profile_name} has no release adapter yet",
                code="release_profile_adapter_unavailable",
            )
        report = release_live_profile(profile, adapter=adapter, released_at=datetime.now(UTC))
        payload = json.loads(canonical_bytes(report).decode())
        sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    _own_origin, own_binding = _operation_binding(
        "r3_release",
        manifest_version=_MANIFEST_V2,
        mode="new_consume",
        registry=fresh_generation.registry,
    )
    _apply_origin, apply_binding = _operation_binding(
        "r3_apply",
        manifest_version=_MANIFEST_V2,
        mode="historical_read",
        registry=fresh_generation.registry,
    )
    client = bigquery.Client(project=PROJECT, location=LOCATION)
    provider = _ReleaseArtifactProvider(
        client, generation=fresh_generation, apply_binding=apply_binding, profile=profile
    )
    global _DURABLE_ARTIFACT_CONTEXT
    if _DURABLE_ARTIFACT_CONTEXT is not None:
        raise ReleaseRefusal("r3 release execution artifact context is already active")
    _DURABLE_ARTIFACT_CONTEXT = provider
    try:
        authority = execution_approval._load_execution_authority(
            "r3_release", mode="new_consume", artifact_reader=_execution_approval_artifact_bytes
        )
        _require_runtime_profile(
            authority, "r3_release", generation=fresh_generation, binding=own_binding
        )
        inputs = provider.inputs
        if inputs is None:
            raise ReleaseRefusal("r3 release execution artifacts were not validated")
        consumption = execution_approval._consume_execution_authority(authority)
        _terminal_context.clear()
        _terminal_context.extend((authority, consumption))
    finally:
        _DURABLE_ARTIFACT_CONTEXT = None

    def query_runner(sql: str):
        return [dict(row) for row in _query(client, sql)]

    report = _release_open_intelligence_run(
        run_id=profile.run_id,
        released_at=datetime.now(UTC),
        query_runner=query_runner,
        evidence=inputs.evidence,
        profile=profile,
    )
    canonical_result_json = canonical_bytes(report).decode()
    result_digest = hashlib.sha256(canonical_result_json.encode()).hexdigest()
    result = execution_approval._record_execution_result(
        authority,
        consumption,
        f"bq://{PROJECT}.{DATASET}.{RELEASE_RECORD_TABLE}#{profile.run_id}",
        canonical_result_json,
        result_digest,
        "succeeded",
    )
    _terminal_context.clear()
    payload = json.loads(canonical_result_json)
    payload["execution_approval"] = {
        "manifest_sha256": authority.approval.manifest_sha256,
        "approval_id": authority.approval.approval_id,
        "consumption_id": consumption.consumption_id,
        "result_id": result.result_id,
    }
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


def _selected_run_id(argv: Sequence[str] | None) -> str:
    try:
        return _select_profile(_parse_cli(tuple(sys.argv[1:] if argv is None else argv))).run_id
    except ReleaseRefusal:
        return R3_RUN_ID


def main(argv: Sequence[str] | None = None) -> int:
    terminal_context: list[object] = []
    run_id = _selected_run_id(argv)
    try:
        return _main_impl(argv, _terminal_context=terminal_context)
    except Exception as error:
        if execution_approval._is_unresolved_result(error):
            terminal_context.clear()
            raise
        if len(terminal_context) == 2 and _same_generation(*terminal_context):
            authority, consumption = terminal_context
            failure_payload = {
                "error_code": "r3_release_failed",
                "run_id": run_id,
                "status": "failed",
            }
            failure_json = canonical_bytes(failure_payload).decode()
            failure_digest = hashlib.sha256(failure_json.encode()).hexdigest()
            execution_approval._record_execution_result(
                authority,
                consumption,
                f"bq://{PROJECT}.{DATASET}.{RELEASE_RECORD_TABLE}#{run_id}:failed",
                failure_json,
                failure_digest,
                "failed",
            )
        raise


def release_with_adapter(argv: Sequence[str], *, adapter: object) -> int:
    """The command with a live profile adapter injected; ``main`` keeps the refusal.

    The daily stage handlers are the only caller. A live profile takes no execution
    approval terminal context here: the kernel's issued release receipt is the authority,
    and the retained R3 replay still refuses an adapter inside the live path.
    """
    if adapter is None:
        raise ReleaseRefusal(
            "a live profile release requires its adapter",
            code="release_profile_adapter_unavailable",
        )
    return _main_impl(tuple(argv), _terminal_context=[], adapter=adapter)


def run_executable(argv=None, *, runner=None, stderr=None) -> int:
    values = tuple(sys.argv[1:] if argv is None else argv)
    boundary = main if runner is None else runner
    error_output = sys.stderr if stderr is None else stderr
    try:
        return boundary(values)
    except ReleaseRefusal as error:
        payload = {"error": error.code or "r3_release_authority_refused"}
    except Exception:
        payload = {"error": "r3_release_internal_refusal"}
    error_output.write(canonical_bytes(payload).decode() + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(run_executable(sys.argv[1:]))
