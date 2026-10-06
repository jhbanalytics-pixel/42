import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROUTINE = (
    ROOT / "infra" / "bigquery_routines" / "sp_read_open_intelligence_execution_result_chain_v1.sql"
)


def _sql() -> str:
    return ROUTINE.read_text(encoding="utf-8")


def _compact(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def test_result_chain_routine_schema_is_exact():
    sql = _sql()
    signature = re.search(
        r"sp_read_open_intelligence_execution_result_chain_v1`\s*\((.*?)\)\s*BEGIN",
        sql,
        re.S,
    )
    assert signature is not None
    assert _compact(signature.group(1)) == "p_source_operation STRING, p_run_id STRING"

    final_select = sql[sql.rindex("SELECT a.approval_id") :].split("\n  FROM", 1)[0]
    aliases = re.findall(r"\bAS\s+(\w+)", final_select, re.I)
    assert aliases == [
        "approval_id",
        "manifest_sha256",
        "canonical_manifest_json",
        "consumption_id",
        "execution_name",
        "job_resource",
        "source_sha",
        "image_uri",
        "result_id",
        "result_reference",
        "canonical_result_json",
        "result_digest",
        "status",
        "completed_at",
    ]


def test_result_chain_routine_enforces_the_exact_caller_matrix():
    sql = _compact(_sql())
    assert sql.count("SESSION_USER()") == 1
    assert (
        "WHEN 'trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com' "
        "THEN v_source_operation = 'collection_exposure_issue'"
    ) in sql
    assert (
        "WHEN 'trends-engine-oi-r3-proof@ogilvy-trends-v2.iam.gserviceaccount.com' "
        "THEN v_source_operation = 'r3_apply'"
    ) in sql
    assert (
        "WHEN 'trends-engine-oi-r3-release@ogilvy-trends-v2.iam.gserviceaccount.com' "
        "THEN v_source_operation IN ('r3_apply', 'r3_proof_issue')"
    ) in sql
    assert "ELSE FALSE END AS 'execution_result_chain_identity_invalid'" in sql
    identities = set(
        re.findall(r"trends-engine-[^']+@ogilvy-trends-v2[.]iam[.]gserviceaccount[.]com", sql)
    )
    assert identities == {
        "trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com",
        "trends-engine-oi-r3-proof@ogilvy-trends-v2.iam.gserviceaccount.com",
        "trends-engine-oi-r3-release@ogilvy-trends-v2.iam.gserviceaccount.com",
    }


def test_result_chain_routine_requires_exactly_one_complete_succeeded_chain():
    sql = _compact(_sql())
    assert "SET v_chain_count = ( SELECT COUNT(*)" in sql
    assert "ASSERT v_chain_count = 1 AS 'execution_result_chain_unavailable'" in sql
    assert sql.count("r.status = 'succeeded'") == 2
    assert (
        sql.count("consumption_contract_version = 'open_intelligence_execution_consumption_v1'")
        >= 2
    )
    assert sql.count("result_contract_version = 'open_intelligence_execution_result_v1'") >= 2
    assert sql.count("approval_contract_version = 'open_intelligence_execution_approval_v1'") >= 2
    assert (
        sql.count(
            "SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` AS linked_c"
        )
        == 2
    )
    assert (
        sql.count(
            "SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_results_v1` AS linked_r"
        )
        == 2
    )
    assert "LIMIT " not in sql.upper()
    assert "ORDER BY" not in sql.upper()
    assert "EXECUTE IMMEDIATE" not in sql.upper()


def test_result_chain_routine_recomputes_manifest_and_result_byte_digests():
    sql = _compact(_sql())
    assert sql.count("SAFE.PARSE_JSON(a.canonical_manifest_json, wide_number_mode => 'exact')") >= 2
    assert sql.count("a.manifest_sha256 = LOWER(TO_HEX(SHA256(a.canonical_manifest_json)))") == 2
    assert sql.count("SAFE.PARSE_JSON(r.canonical_result_json, wide_number_mode => 'exact')") >= 2
    assert sql.count("r.result_digest = LOWER(TO_HEX(SHA256(r.canonical_result_json)))") == 2


def test_result_chain_routine_binds_manifest_to_the_consumed_execution():
    sql = _compact(_sql())
    required_twice = (
        "c.operation = a.operation",
        "r.operation = a.operation",
        "c.manifest_sha256 = a.manifest_sha256",
        "r.manifest_sha256 = a.manifest_sha256",
        "r.execution_name = c.execution_name",
        "JSON_VALUE(a.canonical_manifest_json, '$.operation') = a.operation",
        "JSON_VALUE(a.canonical_manifest_json, '$.job_resource') = c.job_resource",
        "JSON_VALUE(a.canonical_manifest_json, '$.source_sha') = c.source_sha",
        "JSON_VALUE(a.canonical_manifest_json, '$.image_uri') = c.image_uri",
        "STARTS_WITH(c.execution_name, CONCAT(c.job_resource, '/executions/'))",
    )
    for fragment in required_twice:
        assert sql.count(fragment) >= 2, fragment
    assert sql.count("REGEXP_CONTAINS(c.source_sha, r'^[0-9a-f]{40}$')") == 2
    assert (
        sql.count(
            "REGEXP_CONTAINS(c.image_uri, r'^us-central1-docker[.]pkg[.]dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:[0-9a-f]{64}$')"
        )
        == 2
    )


def test_result_chain_routine_binds_the_exact_run_without_fallback():
    sql = _compact(_sql())
    assert sql.count("JSON_VALUE(r.canonical_result_json, '$.run_id') = v_run_id") == 2
    assert sql.count("a.operation = v_source_operation") == 2
    assert sql.count("c.operation = v_source_operation") == 2
    assert sql.count("r.operation = v_source_operation") == 2
    assert "MAX(" not in sql.upper()
    assert "STARTS_WITH(JSON_VALUE(r.canonical_result_json, '$.run_id')" not in sql
