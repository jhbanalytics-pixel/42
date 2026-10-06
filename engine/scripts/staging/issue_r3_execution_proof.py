"""Durably approved post-completion issuer for one R3 execution proof."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.cloud import bigquery
from scripts.staging import replay_open_intelligence as replay
from src.analysis.open_intelligence import execution_approval
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.persistence import row_set_digest_sql
from src.analysis.open_intelligence.run_receipts import (
    RUN_RECEIPT_ROW_FIELDS,
    build_run_receipt,
)

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
APPROVAL_DATASET = "trends_v2_staging_approvals"
LOCATION = "US"
RUN_ID = "run_20260903_dynamic_apply_v2_r16"
# The retained v1 apply job, kept for historical inspection of the r16 chain only. A fresh
# run takes the upstream apply job and principal from the v2 origin row instead.
R3_APPLY_JOB = "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-apply-staging"
_DIGEST = re.compile(r"[0-9a-f]{64}")
_EXECUTION = re.compile(
    r"projects/ogilvy-trends-v2/locations/us-central1/jobs/[a-z0-9-]+/executions/[^/]+"
)
_MANIFEST_V1 = "open_intelligence_execution_manifest_v1"
_MANIFEST_V2 = "open_intelligence_execution_manifest_v2"
_HISTORICAL_MODES = ("historical_read", "historical_replay")
_GENERATION_FIELDS = ("origin_registry_sha256", "resource_manifest_sha256")
_DURABLE_ARTIFACT_CONTEXT = None


class ProofIssuerRefusal(ValueError):
    pass


@dataclass(slots=True)
class _ProofArtifactProvider:
    client: object
    execution_name: str
    generation: object = None
    apply_binding: object = None
    blocked_receipt: object | None = None
    target_result: Mapping[str, object] | None = None
    artifacts: dict[str, bytes] | None = None

    def read(self, name: str) -> bytes:
        if self.artifacts is None:
            self.blocked_receipt = _blocked_receipt(self.client)
            self.target_result = _target_result(
                self.client,
                self.execution_name,
                version=execution_approval._RESULT_VERSION_V2,
                mode="historical_read",
                generation=self.generation,
                apply_binding=self.apply_binding,
            )
            self.artifacts = _build_artifacts(self.blocked_receipt, self.target_result)
        if name not in self.artifacts:
            raise ProofIssuerRefusal("r3 proof execution artifact is unavailable")
        return self.artifacts[name]


def _parse_cli(argv: Sequence[str]) -> str:
    values = tuple(argv)
    if len(values) != 2 or values[0] != "--r3-execution-name":
        raise ProofIssuerRefusal("r3 proof CLI grammar is invalid")
    execution_name = values[1]
    if not isinstance(execution_name, str) or _EXECUTION.fullmatch(execution_name) is None:
        raise ProofIssuerRefusal("r3 proof target Execution is invalid")
    return execution_name


def _execution_approval_artifact_bytes(name: str) -> bytes:
    if not isinstance(_DURABLE_ARTIFACT_CONTEXT, _ProofArtifactProvider):
        raise ProofIssuerRefusal("r3 proof execution artifact is unavailable")
    return _DURABLE_ARTIFACT_CONTEXT.read(name)


def _operation_binding(operation, *, manifest_version, mode, registry):
    """One origin row and its exact binding for the operation, under an explicit mode."""
    from src.analysis.open_intelligence.execution_origins import OriginRegistry, select_origin

    if type(registry) is not OriginRegistry or not isinstance(operation, str):
        raise ProofIssuerRefusal("execution origin registry is invalid")
    matches = [
        origin
        for origin in registry.values()
        if origin.manifest_version == manifest_version and operation in origin.operation_bindings
    ]
    if len(matches) != 1:
        raise ProofIssuerRefusal(f"{operation} has no single execution origin")
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
        raise ProofIssuerRefusal(f"{operation} runtime generation differs from the active pair")
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
        raise ProofIssuerRefusal(f"{operation} runtime identity differs from its execution origin")
    return origin, resolved


def _same_generation(authority, consumption):
    pair = _generation_pair(getattr(authority, "generation", None))
    return pair is not None and pair == _generation_pair(consumption)


def _admit_chain_row(row, operation, *, mode, generation):
    """Admit one v2 result chain row through the trusted catalogue and the same pair."""
    from src.analysis.open_intelligence.execution_generations import load_trusted_generation
    from src.analysis.open_intelligence.execution_origins import OriginRefusal

    if mode not in _HISTORICAL_MODES:
        raise ProofIssuerRefusal(f"{operation} result chain mode is not historical")
    pair = _generation_pair(SimpleNamespace(**{name: row.get(name) for name in _GENERATION_FIELDS}))
    if pair is None or pair != _generation_pair(generation):
        raise ProofIssuerRefusal(f"{operation} result chain generation differs")
    try:
        admitted = load_trusted_generation(*pair)
    except OriginRefusal as error:
        raise ProofIssuerRefusal(f"{operation} result chain generation is untrusted") from error
    manifest_json = row.get("canonical_manifest_json")
    try:
        manifest_payload = json.loads(manifest_json) if isinstance(manifest_json, str) else None
        manifest = execution_approval.validate_execution_manifest(
            manifest_payload, mode=mode, registry=admitted.registry
        )
        manifest_sha256 = execution_approval.manifest_sha256(
            manifest_payload, mode=mode, registry=admitted.registry
        )
    except (TypeError, ValueError, execution_approval.ApprovalRefusal) as error:
        raise ProofIssuerRefusal(f"{operation} result chain manifest is invalid") from error
    _origin, binding = _operation_binding(
        operation, manifest_version=_MANIFEST_V2, mode=mode, registry=admitted.registry
    )
    execution_name = row.get("execution_name")
    if (
        manifest.manifest_version != _MANIFEST_V2
        or manifest.operation != operation
        or row.get("manifest_sha256") != manifest_sha256
        or manifest.job_resource != binding.job_resource
        or manifest.service_identity != binding.service_identity
        or row.get("job_resource") != binding.job_resource
        or row.get("source_sha") != manifest.source_sha
        or row.get("image_uri") != manifest.image_uri
        or not isinstance(execution_name, str)
        or not execution_name.startswith(binding.job_resource + "/executions/")
    ):
        raise ProofIssuerRefusal(f"{operation} result chain binding differs")
    return manifest, binding


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
    rows = tuple(
        job.result(
            max_results=max_results,
            retry=None,
            job_retry=None,
        )
    )
    return rows


def _blocked_receipt(client: object):
    fields = ", ".join(f"`{field}`" for field in RUN_RECEIPT_ROW_FIELDS)
    rows = _query(
        client,
        (
            f"SELECT {fields} FROM `{PROJECT}.{DATASET}.open_intelligence_run_receipts_v1` "
            "WHERE run_id = @run_id"
        ),
        parameters=(bigquery.ScalarQueryParameter("run_id", "STRING", RUN_ID),),
        max_results=2,
    )
    if len(rows) != 1:
        raise ProofIssuerRefusal("r3 blocked receipt cardinality differs")
    values = {field: dict(rows[0]).get(field) for field in RUN_RECEIPT_ROW_FIELDS}
    if isinstance(values["market_scope"], list):
        values["market_scope"] = tuple(values["market_scope"])
    try:
        receipt = build_run_receipt(**values)
    except ValueError as error:
        raise ProofIssuerRefusal("r3 blocked receipt is invalid") from error
    if (
        receipt.run_id != RUN_ID
        or receipt.status != "completed"
        or receipt.complete_partitions is not True
        or receipt.display_release_state != "blocked"
    ):
        raise ProofIssuerRefusal("r3 blocked receipt authority differs")
    return receipt


def _target_result(
    client: object,
    execution_name: str,
    *,
    version,
    mode,
    generation,
    apply_binding,
) -> Mapping[str, object]:
    if mode not in _HISTORICAL_MODES:
        raise ProofIssuerRefusal("r3 target result chain mode is not historical")
    if version == execution_approval._RESULT_VERSION:
        # The retained v1 chain under a historical mode; the r16 apply ran on the old job.
        rows = _query(
            client,
            f"CALL `{PROJECT}.{APPROVAL_DATASET}."
            "sp_read_open_intelligence_execution_result_chain_v1`(@source_operation, @run_id)",
            parameters=(
                bigquery.ScalarQueryParameter("source_operation", "STRING", "r3_apply"),
                bigquery.ScalarQueryParameter("run_id", "STRING", RUN_ID),
            ),
            max_results=2,
        )
    elif version == execution_approval._RESULT_VERSION_V2:
        rows = _query(
            client,
            f"CALL `{PROJECT}.{APPROVAL_DATASET}."
            "sp_read_open_intelligence_execution_result_chain_v2`(@p_source_operation, @p_run_id)",
            parameters=(
                bigquery.ScalarQueryParameter("p_source_operation", "STRING", "r3_apply"),
                bigquery.ScalarQueryParameter("p_run_id", "STRING", RUN_ID),
            ),
            max_results=2,
        )
    else:
        raise ProofIssuerRefusal("r3 target result chain version is unknown")
    if len(rows) != 1:
        raise ProofIssuerRefusal("r3 target result cardinality differs")
    row = dict(rows[0])
    if version == execution_approval._RESULT_VERSION_V2:
        _manifest, binding = _admit_chain_row(row, "r3_apply", mode=mode, generation=generation)
    else:
        _origin, binding = _operation_binding(
            "r3_apply", manifest_version=_MANIFEST_V1, mode=mode, registry=generation.registry
        )
    if binding != apply_binding or not execution_name.startswith(
        binding.job_resource + "/executions/"
    ):
        raise ProofIssuerRefusal("r3 target result upstream apply identity differs")
    canonical_result_json = row.get("canonical_result_json")
    digest = row.get("result_digest")
    if (
        row.get("execution_name") != execution_name
        or row.get("status") != "succeeded"
        or not isinstance(canonical_result_json, str)
        or not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
        or hashlib.sha256(canonical_result_json.encode()).hexdigest() != digest
    ):
        raise ProofIssuerRefusal("r3 target result authority differs")
    try:
        payload = json.loads(canonical_result_json)
    except json.JSONDecodeError as error:
        raise ProofIssuerRefusal("r3 target result is invalid") from error
    if not isinstance(payload, dict):
        raise ProofIssuerRefusal("r3 target result is invalid")
    return {
        "canonical_result_json": canonical_result_json,
        "result_digest": digest,
        "result_reference": row.get("result_reference"),
        "proof": payload,
    }


def _build_artifacts(receipt, target_result: Mapping[str, object]) -> dict[str, bytes]:
    return {
        "proof_issuer_contract": canonical_bytes(
            {
                "contract_version": "r3-proof-issuer-durable-v1",
                "run_id": RUN_ID,
                "target_result_digest": target_result["result_digest"],
                "target_result_reference": target_result["result_reference"],
            }
        ),
        "blocked_run_receipt": canonical_bytes(receipt),
    }


def _snapshot_sql() -> str:
    digest_sql = row_set_digest_sql(RUN_ID).replace(f"'{RUN_ID}'", "@run_id")
    receipt_fields = ", ".join(f"r.`{field}`" for field in RUN_RECEIPT_ROW_FIELDS)
    counts = (
        ("candidate_rows", "signal_candidates_v2"),
        ("evidence_rows", "signal_evidence_v2"),
        ("membership_rows", "signal_membership_v2"),
        ("lineage_rows", "signal_lineage_v2"),
        ("prediction_rows", "signal_predictions_v2"),
        ("outcome_rows", "signal_outcomes_v2"),
        ("analysis_rows", "signal_analysis_v2"),
    )
    projections = ", ".join(
        f"(SELECT COUNT(*) FROM `{PROJECT}.{DATASET}.{table}` WHERE run_id = @run_id) AS {alias}"
        for alias, table in counts
    )
    return (
        f"SELECT {receipt_fields}, {projections}, ({digest_sql}) AS recomputed_row_set_digest "
        f"FROM `{PROJECT}.{DATASET}.open_intelligence_run_receipts_v1` r "
        "WHERE r.run_id = @run_id"
    )


def _snapshot(client: object):
    rows = _query(
        client,
        _snapshot_sql(),
        parameters=(bigquery.ScalarQueryParameter("run_id", "STRING", RUN_ID),),
        max_results=2,
    )
    if len(rows) != 1:
        raise ProofIssuerRefusal("r3 proof snapshot cardinality differs")
    row = dict(rows[0])
    fields = {field: row.get(field) for field in RUN_RECEIPT_ROW_FIELDS}
    if isinstance(fields["market_scope"], list):
        fields["market_scope"] = tuple(fields["market_scope"])
    try:
        receipt = build_run_receipt(**fields)
    except ValueError as error:
        raise ProofIssuerRefusal("r3 proof snapshot receipt is invalid") from error
    counts = {
        "candidates": row.get("candidate_rows"),
        "evidence": row.get("evidence_rows"),
        "membership": row.get("membership_rows"),
        "lineage": row.get("lineage_rows"),
        "predictions": row.get("prediction_rows"),
        "outcomes": row.get("outcome_rows"),
        "analysis": row.get("analysis_rows"),
    }
    expected = {
        "candidates": receipt.candidate_count,
        "evidence": receipt.evidence_count,
        "membership": receipt.membership_count,
        "lineage": receipt.lineage_count,
        "predictions": receipt.prediction_count,
        "outcomes": 0,
        "analysis": receipt.analysis_count,
    }
    if counts != expected or row.get("recomputed_row_set_digest") != receipt.row_set_digest:
        raise ProofIssuerRefusal("r3 snapshot counts or row-set digest differ")
    return receipt, counts


def _target_proof_authority(
    target_result: Mapping[str, object], *, apply_binding
) -> Mapping[str, object]:
    proof = target_result["proof"]
    required = {
        "execution_name",
        "job_resource",
        "image_digest",
        "source_sha",
        "service_identity",
        "command",
        "args",
        "config_digest",
        "started_at",
        "completed_at",
        "status",
    }
    if not isinstance(proof, dict) or not required.issubset(proof):
        raise ProofIssuerRefusal("r3 target proof authority is incomplete")
    values = {field: proof[field] for field in required}
    try:
        values["args"] = tuple(values["args"])
        for field in ("started_at", "completed_at"):
            if isinstance(values[field], str):
                values[field] = datetime.fromisoformat(values[field].replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ProofIssuerRefusal("r3 target proof authority is invalid") from error
    if (
        values["job_resource"] != apply_binding.job_resource
        or values["service_identity"] != apply_binding.service_identity
        or not isinstance(values["execution_name"], str)
        or not values["execution_name"].startswith(apply_binding.job_resource + "/executions/")
        or values["status"] != "succeeded"
    ):
        raise ProofIssuerRefusal("r3 target proof authority differs")
    return values


def _main_impl(argv: Sequence[str] | None = None, *, _terminal_context: list[object]) -> int:
    from src.analysis.open_intelligence.execution_generations import active_generation

    execution_name = _parse_cli(tuple(sys.argv[1:] if argv is None else argv))
    # Own binding and upstream apply binding are resolved separately from the packaged
    # active pair before any read; the issued authority must carry the same pair.
    fresh_generation = active_generation()
    _own_origin, own_binding = _operation_binding(
        "r3_proof_issue",
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
    if not execution_name.startswith(apply_binding.job_resource + "/executions/"):
        raise ProofIssuerRefusal("r3 proof target Execution is outside the apply binding")
    client = bigquery.Client(project=PROJECT, location=LOCATION)
    provider = _ProofArtifactProvider(
        client, execution_name, generation=fresh_generation, apply_binding=apply_binding
    )
    global _DURABLE_ARTIFACT_CONTEXT
    if _DURABLE_ARTIFACT_CONTEXT is not None:
        raise ProofIssuerRefusal("r3 proof execution artifact context is already active")
    _DURABLE_ARTIFACT_CONTEXT = provider
    try:
        authority = execution_approval._load_execution_authority(
            "r3_proof_issue", mode="new_consume", artifact_reader=_execution_approval_artifact_bytes
        )
        _require_runtime_profile(
            authority, "r3_proof_issue", generation=fresh_generation, binding=own_binding
        )
        blocked_receipt = provider.blocked_receipt
        target_result = provider.target_result
        if blocked_receipt is None or target_result is None:
            raise ProofIssuerRefusal("r3 proof execution artifacts were not validated")
        consumption = execution_approval._consume_execution_authority(authority)
        _terminal_context.clear()
        _terminal_context.extend((authority, consumption, execution_name))
    finally:
        _DURABLE_ARTIFACT_CONTEXT = None
    receipt, counts = _snapshot(client)
    proof = replay.build_r3_execution_proof(
        pre_execution_addendum_sha256=authority.approval.manifest_sha256,
        authority=_target_proof_authority(target_result, apply_binding=apply_binding),
        applied_receipt=blocked_receipt,
        readback_receipt=receipt,
        persisted_counts=counts,
        readback_counts=counts,
    )
    canonical_result_json = replay.render_r3_execution_proof(proof).rstrip("\n")
    result_digest = hashlib.sha256(canonical_result_json.encode()).hexdigest()
    result = execution_approval._record_execution_result(
        authority,
        consumption,
        execution_name + "#r3-execution-proof",
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


def main(argv: Sequence[str] | None = None) -> int:
    terminal_context: list[object] = []
    try:
        return _main_impl(argv, _terminal_context=terminal_context)
    except Exception as error:
        if execution_approval._is_unresolved_result(error):
            terminal_context.clear()
            raise
        if len(terminal_context) == 3 and _same_generation(*terminal_context[:2]):
            authority, consumption, execution_name = terminal_context
            failure_payload = {
                "error_code": "r3_proof_issue_failed",
                "run_id": RUN_ID,
                "status": "failed",
            }
            failure_json = canonical_bytes(failure_payload).decode("utf-8")
            failure_digest = hashlib.sha256(failure_json.encode()).hexdigest()
            execution_approval._record_execution_result(
                authority,
                consumption,
                str(execution_name) + "#r3-execution-proof:failed",
                failure_json,
                failure_digest,
                "failed",
            )
        raise


def run_executable(argv=None, *, runner=None, stderr=None) -> int:
    values = tuple(sys.argv[1:] if argv is None else argv)
    boundary = main if runner is None else runner
    error_output = sys.stderr if stderr is None else stderr
    try:
        return boundary(values)
    except ProofIssuerRefusal:
        payload = {"error": "r3_proof_authority_refused"}
    except Exception:
        payload = {"error": "r3_proof_internal_refusal"}
    error_output.write(canonical_bytes(payload).decode("utf-8") + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(run_executable(sys.argv[1:]))
