import copy
import hashlib
import importlib
import json
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta

import pytest
from src.analysis.open_intelligence import execution_approval, live_quality
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.persistence import OpenIntelligenceRowBatch
from src.analysis.open_intelligence.predictions import PredictionRules
from src.analysis.open_intelligence.production_snapshot_tables import retained_origin_registry
from src.analysis.open_intelligence.run_receipts import build_run_receipt, run_receipt_digest

from tests.unit import test_dynamic_quality_review as review_fixture
from tests.unit import test_general_question_planning as planning_fixture
from tests.unit import test_open_intelligence_execution_approval_contract as approval_fixture
from tests.unit import test_open_intelligence_release as release_fixture

PROFILE_SOURCE = "0f738215cfff3f9c8e4fa3bc1f6f8a38fe1e30c6"
PROFILE_IMAGE = (
    "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:"
    "aae0e3f1f9a840e4afc0cae87e4f02a589a982b2ce0401bf57d838e1a30e80b3"
)
PROFILE_CONTRACT = "5eb924575097ffc893b6d93fcd54ac8bca4ae766adb1cd31d4c03ffa92350054"
APPROVED_BY = approval_fixture.APPROVED_BY
REGISTERED_BY = "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab"
RETAINED = {"mode": "historical_read", "registry": retained_origin_registry()}


def module():
    return importlib.import_module(
        "src.analysis.open_intelligence.general_question_release_admission"
    )


def canonical(value):
    return canonical_bytes(value).decode()


def source_inputs(version="hybrid_graph_v1"):
    original = review_fixture._batch()
    signal_date = date(2026, 9, 3)

    def dated(rows):
        return tuple({**row, "signal_date": signal_date} for row in rows)

    batch = OpenIntelligenceRowBatch(
        dated(original.candidates),
        dated(original.evidence),
        dated(original.membership),
        (),
        tuple(
            {
                **row,
                "signal_date": signal_date,
                "rule_version": PredictionRules().rule_version,
            }
            for row in original.predictions
        ),
        (),
        cluster_build_version=version,
    )
    candidate = batch.candidates[0]
    completed = datetime(2026, 9, 4, 10, tzinfo=UTC)
    receipt = build_run_receipt(
        **{
            **release_fixture._receipt_fields(),
            "signal_date": candidate["signal_date"],
            "observation_start": candidate["signal_date"],
            "observation_end": candidate["signal_date"],
            "cluster_build_version": version,
            "source_family_map_version": batch.predictions[0]["source_family_map_version"],
            "rule_version": "rules_v1",
            "candidate_count": len(batch.candidates),
            "evidence_count": len(batch.evidence),
            "membership_count": len(batch.membership),
            "lineage_count": len(batch.lineage),
            "analysis_count": 0,
            "prediction_count": len(batch.predictions),
            "source_sha": PROFILE_SOURCE,
            "completed_at": completed,
            "display_release_state": "enabled",
        }
    )
    run_rows = {
        "signal_candidates_v2": batch.candidates,
        "signal_evidence_v2": batch.evidence,
        "signal_membership_v2": batch.membership,
        "signal_lineage_v2": batch.lineage,
        "signal_analysis_v2": (),
        "signal_predictions_v2": batch.predictions,
        "signal_outcomes_v2": batch.outcomes,
    }
    return receipt, run_rows, batch


def manifest(operation, *, artifact_updates=None, source_sha=PROFILE_SOURCE):
    value = approval_fixture.valid_manifest(operation)
    value.update(
        contract_sha256=PROFILE_CONTRACT,
        source_sha=source_sha,
        image_uri=PROFILE_IMAGE,
        expires_at="2026-09-05T00:00:00.000000Z",
    )
    if artifact_updates:
        for artifact in value["input_artifacts"]:
            if artifact["name"] in artifact_updates:
                artifact["sha256"] = artifact_updates[artifact["name"]]
    execution_approval.validate_execution_manifest(value, **RETAINED)
    return value


def chain(operation, manifest_value, payload, *, minute, result_reference=None):
    approved_at = datetime(2026, 9, 4, 12, minute, tzinfo=UTC)
    expires_at = datetime(2026, 9, 5, tzinfo=UTC)
    manifest_json = canonical(manifest_value)
    manifest_digest = execution_approval.manifest_sha256(manifest_value, **RETAINED)
    approval_id = execution_approval.approval_id(manifest_digest, APPROVED_BY, approved_at)
    job_resource = manifest_value["job_resource"]
    execution_name = job_resource + "/executions/synthetic"
    consumed_at = approved_at + timedelta(minutes=1)
    consumption_id = execution_approval.consumption_id(approval_id, execution_name, consumed_at)
    result_json = canonical(payload)
    result_digest = hashlib.sha256(result_json.encode()).hexdigest()
    completed_at = consumed_at + timedelta(minutes=1)
    result_reference = result_reference or f"bq://synthetic/{operation}"
    result_id = execution_approval.result_id(
        consumption_id, result_reference, result_digest, "succeeded", completed_at
    )
    phrase = (
        f"I approve one staging execution of {operation} for manifest SHA256 "
        f"{manifest_digest}. Production remains unchanged."
    )
    approval = {
        "approval_contract_version": "open_intelligence_execution_approval_v1",
        "approval_id": approval_id,
        "manifest_version": "open_intelligence_execution_manifest_v1",
        "operation": operation,
        "contract_sha256": PROFILE_CONTRACT,
        "manifest_sha256": manifest_digest,
        "canonical_manifest_json": manifest_json,
        "approved_by": APPROVED_BY,
        "approved_at": approved_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "approval_phrase_sha256": hashlib.sha256(phrase.encode()).hexdigest(),
    }
    consumption = {
        "consumption_contract_version": "open_intelligence_execution_consumption_v1",
        "consumption_id": consumption_id,
        "approval_id": approval_id,
        "manifest_sha256": manifest_digest,
        "operation": operation,
        "execution_name": execution_name,
        "job_resource": job_resource,
        "source_sha": PROFILE_SOURCE,
        "image_uri": PROFILE_IMAGE,
        "consumed_at": consumed_at.isoformat(),
    }
    result = {
        "result_contract_version": "open_intelligence_execution_result_v1",
        "result_id": result_id,
        "consumption_id": consumption_id,
        "approval_id": approval_id,
        "manifest_sha256": manifest_digest,
        "operation": operation,
        "execution_name": execution_name,
        "result_reference": result_reference,
        "canonical_result_json": result_json,
        "result_digest": result_digest,
        "status": "succeeded",
        "completed_at": completed_at.isoformat(),
    }
    return {
        "operation": operation,
        "approval_json": canonical(approval),
        "consumption_json": canonical(consumption),
        "result_json": canonical(result),
        "approval_identity_count": 1,
        "consumption_identity_count": 1,
        "result_identity_count": 1,
    }


def inputs(version="hybrid_graph_v1"):
    receipt, run_rows, batch = source_inputs(version)
    packet = live_quality.build_review_packet(receipt.run_id, batch)
    review = review_fixture._receipt(
        packet,
        source_window_digest=receipt.source_window_digest,
        candidate_projection_digest=live_quality.candidate_projection_digest(receipt.run_id, batch),
    )
    blocked_digest = run_receipt_digest(replace(receipt, display_release_state="blocked"))
    apply_payload = {"run_id": receipt.run_id, "status": "succeeded"}
    apply_manifest = manifest("r3_apply")
    apply_chain = chain("r3_apply", apply_manifest, apply_payload, minute=0)
    apply_row = json.loads(apply_chain["result_json"])
    issuer = canonical_bytes(
        {
            "contract_version": "r3-proof-issuer-durable-v1",
            "run_id": receipt.run_id,
            "target_result_digest": apply_row["result_digest"],
            "target_result_reference": apply_row["result_reference"],
        }
    )
    proof_manifest = manifest(
        "r3_proof_issue",
        artifact_updates={"proof_issuer_contract": hashlib.sha256(issuer).hexdigest()},
    )
    proof_manifest_digest = execution_approval.manifest_sha256(proof_manifest, **RETAINED)
    proof_payload = {
        "execution_proof_contract_version": "r3-execution-proof-v1",
        "r3_pre_execution_addendum_sha256": proof_manifest_digest,
        "execution_name": (
            "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
            "trends-engine-oi-apply-staging/executions/synthetic"
        ),
        "job_resource": (
            "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-apply-staging"
        ),
        "image_digest": PROFILE_IMAGE.rsplit("@", 1)[1],
        "source_sha": PROFILE_SOURCE,
        "service_identity": ("trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com"),
        "command": "python",
        "args": ["scripts/staging/replay_open_intelligence.py"],
        "config_digest": "3" * 64,
        "run_id": receipt.run_id,
        "signal_date": receipt.signal_date.isoformat(),
        "started_at": "2026-09-04T09:00:00.000000Z",
        "completed_at": "2026-09-04T10:00:00.000000Z",
        "status": "succeeded",
        "run_receipt_digest": blocked_digest,
        "row_set_digest": receipt.row_set_digest,
        "persisted_row_family_counts": [
            ["candidates", receipt.candidate_count],
            ["evidence", receipt.evidence_count],
            ["membership", receipt.membership_count],
            ["lineage", receipt.lineage_count],
            ["predictions", receipt.prediction_count],
            ["outcomes", 0],
            ["analysis", receipt.analysis_count],
        ],
    }
    proof_chain = chain("r3_proof_issue", proof_manifest, proof_payload, minute=5)
    reconstructed = importlib.import_module(
        "src.analysis.open_intelligence.general_question_release_material"
    ).reconstruct_release_contract(
        receipt=receipt,
        batch=batch,
        execution_proof_bytes=canonical_bytes(proof_payload),
        execution_proof_manifest_sha256=proof_manifest_digest,
        review_receipt=review,
    )
    release_manifest = manifest(
        "r3_release",
        artifact_updates={
            "release_contract": reconstructed["contract_digest"],
            "execution_proof": hashlib.sha256(canonical_bytes(proof_payload)).hexdigest(),
            "quality_review_receipt": hashlib.sha256(canonical_bytes(review)).hexdigest(),
            "blocked_run_receipt": hashlib.sha256(
                canonical_bytes(replace(receipt, display_release_state="blocked"))
            ).hexdigest(),
        },
    )
    released_at = datetime(2026, 9, 4, 12, 51, tzinfo=UTC)
    release_row = {
        "run_id": receipt.run_id,
        "run_receipt_digest": run_receipt_digest(receipt),
        "source_window_digest": receipt.source_window_digest,
        "candidate_projection_digest": reconstructed["contract"]["candidate_projection_digest"],
        "packet_digest": packet["packet_digest"],
        "review_receipt_digest": review["receipt_digest"],
        "approval_addendum_sha256": reconstructed["contract_digest"],
        "released_at": released_at,
        "release_contract_version": "open_intelligence_quality_release_v2",
    }
    release_report = {
        "run_id": receipt.run_id,
        "blocked_receipt_digest": blocked_digest,
        "released_receipt_digest": run_receipt_digest(receipt),
        "run_receipt_digest": run_receipt_digest(receipt),
        "source_window_digest": receipt.source_window_digest,
        "candidate_projection_digest": release_row["candidate_projection_digest"],
        "packet_digest": packet["packet_digest"],
        "review_receipt_digest": review["receipt_digest"],
        "approval_addendum_sha256": reconstructed["contract_digest"],
        "released_at": released_at,
        "release_contract_version": "open_intelligence_quality_release_v2",
        "approved_by": "durable_execution_approval",
        "approval_document": "r3-release-contract-v1",
    }
    release_chain = chain(
        "r3_release",
        release_manifest,
        release_report,
        minute=50,
        result_reference=(
            "bq://ogilvy-trends-v2.trends_v2_staging."
            f"open_intelligence_quality_release_records_v2#{receipt.run_id}"
        ),
    )
    review_json = canonical(review)
    review_row = {
        "review_store_contract_version": "open_intelligence_quality_review_store_v1",
        "run_id": receipt.run_id,
        "canonical_review_receipt_json": review_json,
        "review_receipt_digest": review["receipt_digest"],
        "artifact_sha256": hashlib.sha256(review_json.encode()).hexdigest(),
        "source_window_digest": receipt.source_window_digest,
        "candidate_projection_digest": release_row["candidate_projection_digest"],
        "packet_digest": packet["packet_digest"],
        "registered_by": REGISTERED_BY,
        "registered_at": datetime(2026, 9, 4, 12, 30, tzinfo=UTC),
    }
    material = {
        "run_id": receipt.run_id,
        "source_copy_run_count": 1,
        "source_copy_completeness": [
            {"source_table": name, "manifest_rows": 1, "matched_rows": 1}
            for name in ("enriched_content", "event_ledger", "seed_candidates", "seed_graph")
        ],
        "release_count": 1,
        "release_rows": [release_row],
        "review_count": 1,
        "review_rows": [review_row],
        "candidate_projection_digest": release_row["candidate_projection_digest"],
        "packet_digest": packet["packet_digest"],
        "execution_chains": [
            {"operation": row["operation"], "chain_count": 1, "rows": [row]}
            for row in (apply_chain, proof_chain, release_chain)
        ],
    }
    _, request, intake = planning_fixture.inputs()
    plan = planning_fixture.stored_plan(request, intake)
    return request, plan, intake, receipt, run_rows, material


def validate(values, profile="legacy_r16_v2"):
    request, plan, intake, receipt, run_rows, material = values
    return module().validate_released_run_admission(
        request,
        plan,
        intake,
        receipt=receipt,
        run_rows=run_rows,
        release_material=material,
        producer_profile_id=profile,
    )


def test_legacy_r16_chain_reconstructs_release_without_issuing_evidence_authority():
    result = validate(inputs())
    assert result.run_id == release_fixture.RUN_ID
    assert result.producer_profile_id == "legacy_r16_v2"
    assert result.evidence_authority is False
    assert "current_source_copy_validation" in result.remaining_checks
    assert result.release_contract_digest == result.release["approval_addendum_sha256"]
    result.release["run_id"] = "mutated"
    assert validate(inputs()).release["run_id"] == release_fixture.RUN_ID


def test_canonical_query_capture_key_order_does_not_change_release_authority():
    values = list(inputs())
    values[-1] = json.loads(canonical_bytes(values[-1]))
    result = validate(values)
    assert result.run_id == release_fixture.RUN_ID
    assert set(result.release) == set(live_quality.QUALITY_RELEASE_FIELDS)


@pytest.mark.parametrize("profile", ["unknown", "legacy_r16_v3"])
def test_unknown_or_v3_profile_refuses(profile):
    with pytest.raises(ValueError, match="release_admission_invalid"):
        validate(inputs(), profile)


@pytest.mark.parametrize("mutation", ["release_run", "review_digest", "chain_run"])
def test_resealed_cross_run_or_digest_mutations_refuse(mutation):
    values = list(inputs())
    material = copy.deepcopy(values[-1])
    values[-1] = material
    if mutation == "release_run":
        material["release_rows"][0]["run_id"] = "foreign_run"
    elif mutation == "review_digest":
        material["review_rows"][0]["review_receipt_digest"] = "f" * 64
    else:
        chain_row = material["execution_chains"][0]["rows"][0]
        result = json.loads(chain_row["result_json"])
        payload = json.loads(result["canonical_result_json"])
        payload["run_id"] = "foreign_run"
        result["canonical_result_json"] = canonical(payload)
        result["result_digest"] = hashlib.sha256(
            result["canonical_result_json"].encode()
        ).hexdigest()
        result["result_id"] = execution_approval.result_id(
            result["consumption_id"],
            result["result_reference"],
            result["result_digest"],
            result["status"],
            datetime.fromisoformat(result["completed_at"]),
        )
        chain_row["result_json"] = canonical(result)
    with pytest.raises(ValueError, match="release_admission_invalid"):
        validate(tuple(values))


def test_resealed_unknown_producer_source_refuses():
    values = list(inputs())
    material = copy.deepcopy(values[-1])
    values[-1] = material
    chain_row = material["execution_chains"][0]["rows"][0]
    approval = json.loads(chain_row["approval_json"])
    manifest_value = json.loads(approval["canonical_manifest_json"])
    manifest_value["source_sha"] = "f" * 40
    approval["canonical_manifest_json"] = canonical(manifest_value)
    manifest_digest = execution_approval.manifest_sha256(manifest_value, **RETAINED)
    approved_at = datetime.fromisoformat(approval["approved_at"])
    approval["manifest_sha256"] = manifest_digest
    approval["approval_id"] = execution_approval.approval_id(
        manifest_digest, approval["approved_by"], approved_at
    )
    phrase = (
        "I approve one staging execution of r3_apply for manifest SHA256 "
        f"{manifest_digest}. Production remains unchanged."
    )
    approval["approval_phrase_sha256"] = hashlib.sha256(phrase.encode()).hexdigest()
    chain_row["approval_json"] = canonical(approval)
    consumption = json.loads(chain_row["consumption_json"])
    consumption["approval_id"] = approval["approval_id"]
    consumption["manifest_sha256"] = manifest_digest
    consumption["source_sha"] = "f" * 40
    consumption["consumption_id"] = execution_approval.consumption_id(
        approval["approval_id"],
        consumption["execution_name"],
        datetime.fromisoformat(consumption["consumed_at"]),
    )
    chain_row["consumption_json"] = canonical(consumption)
    result = json.loads(chain_row["result_json"])
    result["approval_id"] = approval["approval_id"]
    result["manifest_sha256"] = manifest_digest
    result["consumption_id"] = consumption["consumption_id"]
    result["result_id"] = execution_approval.result_id(
        result["consumption_id"],
        result["result_reference"],
        result["result_digest"],
        result["status"],
        datetime.fromisoformat(result["completed_at"]),
    )
    chain_row["result_json"] = canonical(result)
    with pytest.raises(ValueError, match="release_admission_invalid"):
        validate(tuple(values))
