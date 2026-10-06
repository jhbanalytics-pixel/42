import inspect
import re
from pathlib import Path

from src.analysis.open_intelligence import execution_approval as runtime

ROOT = Path(__file__).resolve().parents[2]
ROUTINE = ROOT / "infra" / "bigquery_routines" / "sp_read_open_intelligence_execution_result_v1.sql"


def _sql() -> str:
    return ROUTINE.read_text(encoding="utf-8")


def _compact(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def test_execution_result_read_routine_has_exact_input_and_output_contract():
    sql = _sql()
    signature = re.search(
        r"sp_read_open_intelligence_execution_result_v1`\s*\((.*?)\)\s*BEGIN",
        sql,
        re.S,
    )
    assert signature is not None
    assert _compact(signature.group(1)) == "p_consumption_id STRING"
    final_select = sql[sql.rindex("SELECT r.result_contract_version") :].split("\n  FROM", 1)[0]
    assert re.findall(r"\bAS\s+(\w+)", final_select, re.I) == [
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
    ]


def test_execution_result_read_routine_enforces_exact_operation_identity_matrix():
    sql = _compact(_sql())
    expected = {
        "bootstrap_migration_apply": (
            "trends-engine-oi-bootstrap@ogilvy-trends-v2.iam.gserviceaccount.com"
        ),
        "migration_apply": "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
        "collection_exposure_issue": (
            "trends-engine-oi-exposure@ogilvy-trends-v2.iam.gserviceaccount.com"
        ),
        "r3_apply": "trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com",
        "r3_proof_issue": ("trends-engine-oi-r3-proof@ogilvy-trends-v2.iam.gserviceaccount.com"),
        "r3_release": "trends-engine-oi-r3-release@ogilvy-trends-v2.iam.gserviceaccount.com",
        "brain_read": "trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com",
        "wave1_pilot": "trends-engine-oi-wave1@ogilvy-trends-v2.iam.gserviceaccount.com",
    }
    for operation, identity in expected.items():
        assert f"WHEN '{operation}' THEN '{identity}'" in sql
    assert "ASSERT SESSION_USER() = v_expected_identity" in sql
    assert "ELSE NULL END" in sql


def test_execution_result_read_routine_fails_closed_on_chain_cardinality_and_drift():
    sql = _compact(_sql())
    assert "REGEXP_CONTAINS(v_consumption_id, r'^exc_[0-9a-f]{64}$')" in sql
    assert "COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1`" in sql
    assert "= 1 AS 'execution_approval_unavailable'" in sql
    assert "SET v_result_count = ( SELECT COUNT(*)" in sql
    assert "ASSERT v_result_count <= 1 AS 'execution_result_conflict'" in sql
    assert "r.approval_id = v_consumption.approval_id" in sql
    assert "r.manifest_sha256 = v_consumption.manifest_sha256" in sql
    assert "r.operation = v_consumption.operation" in sql
    assert "r.execution_name = v_consumption.execution_name" in sql
    assert "SAFE.PARSE_JSON(r.canonical_result_json, wide_number_mode => 'exact')" in sql
    assert "r.result_digest = LOWER(TO_HEX(SHA256(r.canonical_result_json)))" in sql
    assert "r.status IN ('succeeded', 'failed')" in sql
    assert "LIMIT " not in sql.upper()
    assert "ORDER BY" not in sql.upper()


def test_runtime_result_reader_uses_only_the_authorized_routine():
    source = inspect.getsource(runtime._default_result_reader)
    assert "sp_read_open_intelligence_execution_result_v1" in source
    assert "open_intelligence_execution_results_v1" not in source
