import importlib
import json
from dataclasses import replace
from datetime import date

import pytest
from scripts.staging import release_open_intelligence_run as legacy
from src.analysis.open_intelligence import execution_generations, live_quality
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.persistence import TABLE_BINDINGS, OpenIntelligenceRowBatch
from src.analysis.open_intelligence.run_receipts import build_run_receipt, run_receipt_digest

from tests.unit import test_dynamic_quality_review as review_fixture
from tests.unit import test_open_intelligence_release as release_fixture


def module():
    return importlib.import_module(
        "src.analysis.open_intelligence.general_question_release_material"
    )


def inputs(run_id=None):
    receipt = build_run_receipt(**release_fixture._receipt_fields())
    batch = review_fixture._batch()
    if run_id:
        receipt = replace(receipt, run_id=run_id)
        batch = OpenIntelligenceRowBatch(
            **{
                family: tuple({**row, "run_id": run_id} for row in getattr(batch, family))
                for family in TABLE_BINDINGS
            }
        )
    packet = live_quality.build_review_packet(receipt.run_id, batch)
    review = review_fixture._receipt(
        packet,
        run_id=receipt.run_id,
        candidate_projection_digest=live_quality.candidate_projection_digest(receipt.run_id, batch),
    )
    proof = release_fixture._execution_proof(run_receipt_digest(receipt))
    proof["run_id"] = receipt.run_id
    return {
        "receipt": receipt,
        "batch": batch,
        "execution_proof_bytes": canonical_bytes(proof),
        "execution_proof_manifest_sha256": "1" * 64,
        "review_receipt": review,
    }


def test_existing_legacy_release_bytes_match_actual_builder():
    values = inputs()
    result = module().reconstruct_release_contract(**values)
    expected = legacy._build_release_inputs(
        receipt=values["receipt"],
        execution_proof_bytes=values["execution_proof_bytes"],
        execution_proof_manifest_sha256=values["execution_proof_manifest_sha256"],
        quality_review_receipt=values["review_receipt"],
        review_packet=result["review_packet"],
        candidate_projection_digest=result["contract"]["candidate_projection_digest"],
        apply_binding=legacy._operation_binding(
            "r3_apply",
            manifest_version="open_intelligence_execution_manifest_v1",
            mode="historical_read",
            registry=execution_generations.active_generation().registry,
        )[1],
    )
    assert result["contract_bytes"] == expected.artifacts["release_contract"]
    assert result["contract_digest"] == expected.evidence.release_contract_digest
    assert "producer_profile_admission" in result["missing_checks"]
    assert "authority" not in result


def test_generic_run_uses_bound_run_without_changing_legacy_constant():
    result = module().reconstruct_release_contract(**inputs("synthetic_other_run"))
    assert result["contract"]["run_id"] == "synthetic_other_run"
    assert legacy.R3_RUN_ID == release_fixture.RUN_ID
    assert (
        result["contract_digest"]
        != module().reconstruct_release_contract(**inputs())["contract_digest"]
    )


def test_generic_date_binds_receipt_and_proof_without_original_date_constant():
    values = inputs("synthetic_other_day")
    values["receipt"] = replace(
        values["receipt"],
        signal_date=date(2026, 9, 5),
        observation_start=date(2026, 9, 5),
        observation_end=date(2026, 9, 5),
    )
    proof = json.loads(values["execution_proof_bytes"])
    proof["signal_date"] = "2026-09-05"
    proof["run_receipt_digest"] = run_receipt_digest(values["receipt"])
    values["execution_proof_bytes"] = canonical_bytes(proof)
    result = module().reconstruct_release_contract(**values)
    assert result["contract"]["blocked_run_receipt_digest"] == run_receipt_digest(values["receipt"])
    assert "complete_run_validation" in result["missing_checks"]


@pytest.mark.parametrize(
    "mutation", ["duplicate_key", "boolean_count", "noncanonical", "invalid_utf8"]
)
def test_malformed_proof_bytes_and_typed_counts_refuse(mutation):
    values = inputs()
    raw = values["execution_proof_bytes"]
    if mutation == "duplicate_key":
        raw = b'{"run_id":"foreign",' + raw[1:]
    elif mutation == "boolean_count":
        proof = json.loads(raw)
        proof["persisted_row_family_counts"][0][1] = True
        raw = canonical_bytes(proof)
    elif mutation == "noncanonical":
        raw += b"\n"
    else:
        raw = b"\xff"
    values["execution_proof_bytes"] = raw
    with pytest.raises(ValueError, match="release_material_invalid"):
        module().reconstruct_release_contract(**values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "other"),
        ("signal_date", "2026-09-04"),
        ("status", "failed"),
        ("source_sha", "d" * 40),
        ("row_set_digest", "d" * 64),
        ("run_receipt_digest", "d" * 64),
        ("r3_pre_execution_addendum_sha256", "d" * 64),
        ("persisted_row_family_counts", []),
        ("service_identity", "foreign"),
        ("completed_at", "2020-01-01T00:00:00Z"),
    ],
)
def test_resealed_proof_binding_mutations_refuse(field, value):
    values = inputs()
    proof = json.loads(values["execution_proof_bytes"])
    proof[field] = value
    values["execution_proof_bytes"] = canonical_bytes(proof)
    with pytest.raises(ValueError, match="release_material_invalid"):
        module().reconstruct_release_contract(**values)


def test_resealed_review_omission_refuses():
    values = inputs()
    values["review_receipt"]["reviewed_evidence_ids"] = ()
    values["review_receipt"]["receipt_digest"] = live_quality.review_receipt_digest(
        values["review_receipt"]
    )
    with pytest.raises(ValueError, match="release_material_invalid"):
        module().reconstruct_release_contract(**values)


def test_fixed_query_binds_run_and_all_three_chain_cardinalities():
    first = module().build_release_material_query("synthetic_one")
    second = module().build_release_material_query("synthetic_two")
    assert first.sql == second.sql
    assert first.sql_digest == second.sql_digest
    assert first.parameters_digest != second.parameters_digest
    assert first.parameters[0].value == "synthetic_one"
    assert "synthetic_one" not in first.sql
    assert first.candidate_limit == 0
    assert first.transport_row_limit == 1
    assert ";" not in first.sql
    assert "CALL " not in first.sql
    assert all(operation in first.sql for operation in ("r3_apply", "r3_proof_issue", "r3_release"))
    assert all(
        field in first.sql
        for field in (
            "release_count",
            "review_count",
            "chain_count",
            "approval_identity_count",
            "consumption_identity_count",
            "result_identity_count",
            "canonical_review_receipt_json",
            "candidate_projection_digest",
            "packet_digest",
        )
    )


@pytest.mark.parametrize("run_id", [None, True, "", "x'; SELECT 1", "x" * 129])
def test_invalid_query_run_refuses(run_id):
    with pytest.raises(ValueError, match="release_material_invalid"):
        module().build_release_material_query(run_id)
