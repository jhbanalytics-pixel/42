"""Validate one legacy released run without issuing evidence authority."""

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from scripts.staging.replay_open_intelligence import partition_completeness_proven

from src.analysis.open_intelligence import execution_approval, live_quality
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.brain_live_reader import _validate_complete_run_rows
from src.analysis.open_intelligence.general_question_planning import validate_stored_question_plan
from src.analysis.open_intelligence.general_question_release_material import (
    _APPROVAL_FIELDS,
    _CONSUMPTION_FIELDS,
    _RESULT_FIELDS,
    _REVIEW_FIELDS,
    reconstruct_release_contract,
)
from src.analysis.open_intelligence.general_question_request import validate_question_request
from src.analysis.open_intelligence.production_snapshot_tables import retained_origin_registry
from src.analysis.open_intelligence.run_receipts import build_run_receipt, run_receipt_digest

_RUN_ID = "run_20260903_dynamic_apply_v2_r16"
_SOURCE_SHA = "0f738215cfff3f9c8e4fa3bc1f6f8a38fe1e30c6"
_IMAGE_URI = (
    "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:"
    "aae0e3f1f9a840e4afc0cae87e4f02a589a982b2ce0401bf57d838e1a30e80b3"
)
_CONTRACT_SHA = "5eb924575097ffc893b6d93fcd54ac8bca4ae766adb1cd31d4c03ffa92350054"
_PROFILE = "legacy_r16_v2"
_REGISTERED_BY = "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab"
_OPERATIONS = ("r3_apply", "r3_proof_issue", "r3_release")
_SOURCE_FIELDS = {
    "signal_candidates_v2",
    "signal_evidence_v2",
    "signal_membership_v2",
    "signal_lineage_v2",
    "signal_analysis_v2",
    "signal_predictions_v2",
    "signal_outcomes_v2",
}


@dataclass(frozen=True, slots=True)
class ReleasedRunAdmission:
    run_id: str
    producer_profile_id: str
    receipt: object
    batch: object
    release: dict
    review_receipt: dict
    execution_chains: dict
    release_contract_digest: str
    evidence_authority: bool
    remaining_checks: tuple[str, ...]


def _invalid(error=None):
    raise ValueError("release_admission_invalid") from error


def _time(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        _invalid()
    if result.tzinfo is None:
        _invalid()
    return result


def _json(value, fields):
    if type(value) is not str:
        _invalid()

    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                _invalid()
            result[key] = item
        return result

    try:
        result = json.loads(value, object_pairs_hook=unique)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        _invalid(error)
    if (
        type(result) is not dict
        or set(result) != set(fields)
        or canonical_bytes(result).decode() != value
    ):
        _invalid()
    return result


def _typed(value, fields, cls, time_fields):
    result = _json(value, fields)
    for field in time_fields:
        result[field] = _time(result[field])
    try:
        return cls(**result)
    except (TypeError, ValueError) as error:
        _invalid(error)


def _review(row, *, receipt, batch):
    if type(row) is not dict or set(row) != set(_REVIEW_FIELDS):
        _invalid()
    raw = _json(row["canonical_review_receipt_json"], live_quality.REVIEW_RECEIPT_FIELDS)
    review = {field: raw[field] for field in live_quality.REVIEW_RECEIPT_FIELDS}
    for field in (
        "reviewed_evidence_ids",
        "foreign_market_evidence_ids",
        "factual_conflict_evidence_ids",
        "uncertain_evidence_ids",
    ):
        review[field] = tuple(review[field])
    review["reviewed_at"] = _time(review["reviewed_at"])
    packet = live_quality.build_review_packet(receipt.run_id, batch)
    projection = live_quality.candidate_projection_digest(receipt.run_id, batch)
    try:
        authority = live_quality.validate_review_receipt(
            review,
            packet=packet,
            source_window_digest=receipt.source_window_digest,
            candidate_projection_digest=projection,
        )
    except ValueError as error:
        _invalid(error)
    registered = _time(row["registered_at"])
    if (
        row["review_store_contract_version"] != "open_intelligence_quality_review_store_v1"
        or row["run_id"] != receipt.run_id
        or row["artifact_sha256"]
        != hashlib.sha256(row["canonical_review_receipt_json"].encode()).hexdigest()
        or row["review_receipt_digest"] != authority.receipt_digest
        or row["source_window_digest"] != receipt.source_window_digest
        or row["candidate_projection_digest"] != projection
        or row["packet_digest"] != packet["packet_digest"]
        or row["registered_by"] != _REGISTERED_BY
        or not authority.reviewed_at <= registered
    ):
        _invalid()
    return review, packet, projection, registered


def _chains(value, *, receipt, as_of):
    if type(value) is not list or len(value) != 3:
        _invalid()
    # Retained legacy release chains are v1 records read under historical_read against the
    # packaged retained registry. A 42 (v2) chain is refused here; its reader is separate.
    registry = retained_origin_registry()
    output = {}
    for entry in value:
        if type(entry) is not dict or set(entry) != {"operation", "chain_count", "rows"}:
            _invalid()
        operation = entry["operation"]
        if operation not in _OPERATIONS or operation in output:
            _invalid()
        if entry["chain_count"] != 1 or type(entry["rows"]) is not list or len(entry["rows"]) != 1:
            _invalid()
        row = entry["rows"][0]
        if (
            type(row) is not dict
            or set(row)
            != {
                "operation",
                "approval_json",
                "consumption_json",
                "result_json",
                "approval_identity_count",
                "consumption_identity_count",
                "result_identity_count",
            }
            or row["operation"] != operation
            or any(
                type(row[field]) is not int or row[field] != 1
                for field in (
                    "approval_identity_count",
                    "consumption_identity_count",
                    "result_identity_count",
                )
            )
        ):
            _invalid()
        approval = _typed(
            row["approval_json"],
            _APPROVAL_FIELDS,
            execution_approval.ExecutionApproval,
            ("approved_at", "expires_at"),
        )
        consumption = _typed(
            row["consumption_json"],
            _CONSUMPTION_FIELDS,
            execution_approval.ExecutionConsumption,
            ("consumed_at",),
        )
        result = _typed(
            row["result_json"],
            _RESULT_FIELDS,
            execution_approval.ExecutionResult,
            ("completed_at",),
        )
        manifest_payload = json.loads(approval.canonical_manifest_json)
        manifest = execution_approval.validate_execution_manifest(
            manifest_payload, mode="historical_read", registry=registry
        )
        payload = json.loads(result.canonical_result_json)
        if (
            approval.operation != operation
            or manifest.operation != operation
            or manifest.contract_sha256 != _CONTRACT_SHA
            or manifest.source_sha != _SOURCE_SHA
            or manifest.image_uri != _IMAGE_URI
            or consumption.approval_id != approval.approval_id
            or consumption.manifest_sha256 != approval.manifest_sha256
            or consumption.operation != operation
            or consumption.job_resource != manifest.job_resource
            or consumption.source_sha != manifest.source_sha
            or consumption.image_uri != manifest.image_uri
            or result.consumption_id != consumption.consumption_id
            or result.approval_id != approval.approval_id
            or result.manifest_sha256 != approval.manifest_sha256
            or result.operation != operation
            or result.execution_name != consumption.execution_name
            or result.status != "succeeded"
            or type(payload) is not dict
            or payload.get("run_id") != receipt.run_id
            or not approval.approved_at <= consumption.consumed_at < approval.expires_at
            or not consumption.consumed_at <= result.completed_at <= as_of
        ):
            _invalid()
        output[operation] = {
            "approval": approval,
            "consumption": consumption,
            "result": result,
            "manifest": manifest,
            "payload": payload,
        }
    if set(output) != set(_OPERATIONS):
        _invalid()
    return output


def _release_result(chain, release, blocked_digest):
    payload = chain["payload"]
    expected = {
        "run_id": release["run_id"],
        "blocked_receipt_digest": blocked_digest,
        "released_receipt_digest": release["run_receipt_digest"],
        "run_receipt_digest": release["run_receipt_digest"],
        "source_window_digest": release["source_window_digest"],
        "candidate_projection_digest": release["candidate_projection_digest"],
        "packet_digest": release["packet_digest"],
        "review_receipt_digest": release["review_receipt_digest"],
        "approval_addendum_sha256": release["approval_addendum_sha256"],
        "released_at": release["released_at"],
        "release_contract_version": release["release_contract_version"],
        "approved_by": "durable_execution_approval",
        "approval_document": "r3-release-contract-v1",
    }
    for field, expected_value in expected.items():
        actual = _time(payload[field]) if field == "released_at" else payload[field]
        if actual != expected_value:
            _invalid()


def validate_released_run_admission(
    request,
    plan,
    intake,
    *,
    receipt,
    run_rows,
    release_material,
    producer_profile_id,
):
    try:
        if type(request) is not dict:
            _invalid()
        scope = {
            key: request[key]
            for key in (
                "client_scope_id",
                "market_scope",
                "brand_config_id",
                "audience_lens_ids",
                "theme_id",
            )
        }
        request = validate_question_request(
            request, scope=scope, policy_digest=request["policy_digest"]
        )
        plan = validate_stored_question_plan(plan, request=request, intake=intake)
        receipt = build_run_receipt(**asdict(receipt))
        if type(run_rows) is not dict or set(run_rows) != _SOURCE_FIELDS:
            _invalid()
        if (
            producer_profile_id != _PROFILE
            or receipt.run_id != _RUN_ID
            or receipt.source_sha != _SOURCE_SHA
            or receipt.cluster_build_version != "hybrid_graph_v1"
            or receipt.status != "completed"
            or not receipt.complete_partitions
            or receipt.display_release_state != "enabled"
            or receipt.client_scope_id != request["client_scope_id"]
            or not set(plan["markets"]) <= set(receipt.market_scope)
        ):
            _invalid()
        as_of = _time(request["as_of"])
        window = plan["window"]
        if window is None or not (
            receipt.observation_start.isoformat() <= window["end"]
            and receipt.observation_end.isoformat() >= window["start"]
        ):
            _invalid()
        batch = _validate_complete_run_rows(receipt, run_rows)
        if (
            type(release_material) is not dict
            or set(release_material)
            != {
                "run_id",
                "source_copy_run_count",
                "source_copy_completeness",
                "release_count",
                "release_rows",
                "review_count",
                "review_rows",
                "candidate_projection_digest",
                "packet_digest",
                "execution_chains",
            }
            or release_material["run_id"] != receipt.run_id
            or release_material["source_copy_run_count"] != 1
            or not partition_completeness_proven(release_material["source_copy_completeness"])
            or release_material["release_count"] != 1
            or release_material["review_count"] != 1
            or type(release_material["release_rows"]) is not list
            or len(release_material["release_rows"]) != 1
            or type(release_material["review_rows"]) is not list
            or len(release_material["review_rows"]) != 1
        ):
            _invalid()
        review, packet, projection, registered = _review(
            release_material["review_rows"][0], receipt=receipt, batch=batch
        )
        release = copy.deepcopy(release_material["release_rows"][0])
        if type(release) is not dict or set(release) != set(live_quality.QUALITY_RELEASE_FIELDS):
            _invalid()
        release["released_at"] = _time(release["released_at"])
        if (
            release["run_id"] != receipt.run_id
            or release["run_receipt_digest"] != run_receipt_digest(receipt)
            or release["source_window_digest"] != receipt.source_window_digest
            or release["candidate_projection_digest"] != projection
            or release["packet_digest"] != packet["packet_digest"]
            or release["review_receipt_digest"] != review["receipt_digest"]
            or release["release_contract_version"] != "open_intelligence_quality_release_v2"
            or release_material["candidate_projection_digest"] != projection
            or release_material["packet_digest"] != packet["packet_digest"]
            or not receipt.completed_at <= registered <= release["released_at"] <= as_of
        ):
            _invalid()
        chains = _chains(release_material["execution_chains"], receipt=receipt, as_of=as_of)
        apply_result = chains["r3_apply"]["result"]
        proof = chains["r3_proof_issue"]
        issuer_contract = canonical_bytes(
            {
                "contract_version": "r3-proof-issuer-durable-v1",
                "run_id": receipt.run_id,
                "target_result_digest": apply_result.result_digest,
                "target_result_reference": apply_result.result_reference,
            }
        )
        if (
            dict(proof["manifest"].input_artifacts).get("proof_issuer_contract")
            != hashlib.sha256(issuer_contract).hexdigest()
        ):
            _invalid()
        proof_bytes = proof["result"].canonical_result_json.encode()
        reconstructed = reconstruct_release_contract(
            receipt=receipt,
            batch=batch,
            execution_proof_bytes=proof_bytes,
            execution_proof_manifest_sha256=proof["approval"].manifest_sha256,
            review_receipt=review,
        )
        release_manifest = chains["r3_release"]["manifest"]
        release_artifacts = dict(release_manifest.input_artifacts)
        blocked_receipt_bytes = canonical_bytes(replace(receipt, display_release_state="blocked"))
        if (
            reconstructed["contract_digest"] != release["approval_addendum_sha256"]
            or release_artifacts.get("release_contract") != reconstructed["contract_digest"]
            or release_artifacts.get("blocked_run_receipt")
            != hashlib.sha256(blocked_receipt_bytes).hexdigest()
            or release_artifacts.get("execution_proof") != hashlib.sha256(proof_bytes).hexdigest()
            or release_artifacts.get("quality_review_receipt")
            != hashlib.sha256(canonical_bytes(review)).hexdigest()
        ):
            _invalid()
        blocked_digest = run_receipt_digest(replace(receipt, display_release_state="blocked"))
        _release_result(chains["r3_release"], release, blocked_digest)
        release_chain = chains["r3_release"]
        if (
            release_chain["result"].result_reference
            != (
                "bq://ogilvy-trends-v2.trends_v2_staging."
                f"open_intelligence_quality_release_records_v2#{receipt.run_id}"
            )
            or not release["released_at"] <= release_chain["result"].completed_at
        ):
            _invalid()
        return ReleasedRunAdmission(
            run_id=receipt.run_id,
            producer_profile_id=_PROFILE,
            receipt=receipt,
            batch=batch,
            release=copy.deepcopy(release),
            review_receipt=copy.deepcopy(review),
            execution_chains={
                operation: {
                    "approval_id": chain["approval"].approval_id,
                    "consumption_id": chain["consumption"].consumption_id,
                    "result_id": chain["result"].result_id,
                    "manifest_sha256": chain["approval"].manifest_sha256,
                    "source_sha": chain["manifest"].source_sha,
                    "image_uri": chain["manifest"].image_uri,
                }
                for operation, chain in chains.items()
            },
            release_contract_digest=reconstructed["contract_digest"],
            evidence_authority=False,
            remaining_checks=(
                "current_source_copy_validation",
                "physical_schema_validation",
                "query_readback_and_sql_parity",
                "selected_plan_coverage",
                "runtime_authorization",
            ),
        )
    except ValueError as error:
        if str(error) == "release_admission_invalid":
            raise
        _invalid(error)
    except (TypeError, KeyError, AttributeError, OverflowError) as error:
        _invalid(error)


__all__ = ["ReleasedRunAdmission", "validate_released_run_admission"]
