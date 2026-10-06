from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "infra/bigquery_schemas/open_intelligence_quality_review_receipts_v1.sql"
REGISTER = (
    ROOT / "infra/bigquery_routines/sp_register_open_intelligence_quality_review_receipt_v1.sql"
)
READ = ROOT / "infra/bigquery_routines/sp_read_open_intelligence_quality_review_receipt_v1.sql"

STORE_FIELDS = (
    ("review_store_contract_version", "STRING"),
    ("run_id", "STRING"),
    ("canonical_review_receipt_json", "STRING"),
    ("review_receipt_digest", "STRING"),
    ("artifact_sha256", "STRING"),
    ("source_window_digest", "STRING"),
    ("candidate_projection_digest", "STRING"),
    ("packet_digest", "STRING"),
    ("registered_by", "STRING"),
    ("registered_at", "TIMESTAMP"),
)


def _sql(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parameters(sql: str, routine: str) -> tuple[tuple[str, str], ...]:
    match = re.search(
        rf"CREATE OR REPLACE PROCEDURE `\{{project\}}\.\{{dataset\}}\.{routine}`\((.*?)\)\s*BEGIN",
        sql,
        re.DOTALL,
    )
    assert match is not None
    return tuple(
        (item.group(1), item.group(2))
        for item in re.finditer(r"\b([a-z][a-z0-9_]*)\s+(STRING|TIMESTAMP)\b", match.group(1))
    )


def test_quality_review_receipt_store_has_the_exact_unpartitioned_schema():
    sql = _sql(SCHEMA)
    body = re.search(r"\((.*)\)\s*OPTIONS", sql, re.DOTALL)
    assert body is not None
    fields = tuple(
        (match.group(1), match.group(2))
        for match in re.finditer(
            r"^\s*([a-z][a-z0-9_]*)\s+(STRING|TIMESTAMP)\s+NOT NULL\b",
            body.group(1),
            re.MULTILINE,
        )
    )
    assert fields == STORE_FIELDS
    assert (
        "CREATE TABLE IF NOT EXISTS `{project}.{dataset}.open_intelligence_quality_review_receipts_v1`"
        in sql
    )
    assert "PARTITION BY" not in sql
    assert "CLUSTER BY" not in sql
    assert "expiration" not in sql.lower()


def test_registration_routine_has_only_the_two_approved_inputs_and_derives_the_human():
    sql = _sql(REGISTER)
    assert _parameters(sql, "sp_register_open_intelligence_quality_review_receipt_v1") == (
        ("p_canonical_review_receipt_json", "STRING"),
        ("p_artifact_sha256", "STRING"),
    )
    assert "ASSERT SESSION_USER() = 'albert.meintjes@ogilvy.co.za'" in sql
    assert "'open-intelligence-execution-approver-v1:', SESSION_USER()" in sql
    assert "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab" in sql
    for forbidden in (
        "p_actor",
        "p_registered_at",
        "p_project",
        "p_dataset",
        "p_table",
        "p_run_id",
        "p_correction",
    ):
        assert forbidden not in sql


def test_registration_uses_the_declared_canonical_receipt_input_everywhere():
    sql = _sql(REGISTER)
    assert "v_canonical_review_receipt_json" not in sql
    assert sql.count("p_canonical_review_receipt_json") >= 6


def test_quality_review_routines_reference_only_declared_local_variables():
    for path in (REGISTER, READ):
        sql = _sql(path)
        declared = set(re.findall(r"\bDECLARE\s+(v_[a-z0-9_]+)\b", sql))
        used = set(re.findall(r"\b(v_[a-z0-9_]+)\b", sql))
        assert used <= declared


def test_registration_routine_recomputes_exact_receipt_and_current_authority():
    sql = _sql(REGISTER)
    for marker in (
        "SAFE.PARSE_JSON(p_canonical_review_receipt_json, wide_number_mode => 'exact')",
        "JSON_KEYS(v_review_receipt_json, 1, mode => 'strict')",
        "v_reconstructed_review_receipt_json = p_canonical_review_receipt_json",
        "v_review_receipt_digest = LOWER(TO_HEX(SHA256(v_review_receipt_preimage_json)))",
        "v_artifact_sha256 = LOWER(TO_HEX(SHA256(p_canonical_review_receipt_json)))",
        "open_intelligence_run_receipts_v1",
        "signal_candidates_v2",
        "signal_evidence_v2",
        "signal_membership_v2",
        "dynamic_quality_projection_v2",
        "dynamic_quality_review_v1",
        "reviewed_evidence_ids",
        "foreign_market_evidence_ids",
        "factual_conflict_evidence_ids",
        "uncertain_evidence_ids",
        "reviewed_by') = 'Albert'",
        "decision') = 'approved'",
    ):
        assert marker in sql
    for comparison in (
        "v_source_window_digest = v_stored_source_window_digest",
        "v_candidate_projection_digest = v_stored_candidate_projection_digest",
        "v_packet_digest = v_stored_packet_digest",
        "v_review_receipt_digest = v_stored_review_receipt_digest",
        "v_artifact_sha256 = p_artifact_sha256",
    ):
        assert comparison in sql


def test_both_routines_recompute_source_window_from_manifest_and_current_rows():
    for path in (REGISTER, READ):
        sql = _sql(path)
        for table in ("enriched_content", "event_ledger", "seed_candidates", "seed_graph"):
            assert f"`{{project}}.{{dataset}}.{table}`" in sql
            assert f"manifest.target_table = '{table}'" in sql
        assert "open_intelligence_source_copy_manifest_v1" in sql
        assert "target_match.content_hash = manifest.source_content_sha256" in sql
        assert "manifest_rows != matched_rows" in sql
        assert "v_source_window_digest = LOWER(TO_HEX(SHA256(CONCAT(" in sql
        assert "SELECT source_window_digest FROM" not in sql


def test_registration_is_transactional_conflict_only_and_reads_back_every_field():
    sql = _sql(REGISTER)
    assert "BEGIN TRANSACTION;" in sql
    assert "CURRENT_TIMESTAMP()" in sql
    assert "run_id = v_run_id" in sql
    assert "review_receipt_digest = v_review_receipt_digest" in sql
    assert "artifact_sha256 = v_artifact_sha256" in sql
    assert "AS 'quality_review_receipt_conflict'" in sql
    assert "INSERT INTO `{project}.{dataset}.open_intelligence_quality_review_receipts_v1`" in sql
    assert "ASSERT @@row_count = 1" in sql
    assert "COMMIT TRANSACTION;" in sql
    assert "SELECT review_store_contract_version, run_id, canonical_review_receipt_json" in sql
    assert "candidate_projection_digest, packet_digest, registered_by, registered_at" in sql


def test_registration_returns_an_exact_existing_receipt_without_reinserting():
    sql = _sql(REGISTER)
    assert "DECLARE v_existing_count INT64 DEFAULT 0;" in sql
    assert "DECLARE v_exact_count INT64 DEFAULT 0;" in sql
    assert "SET v_existing_count = (" in sql
    assert "SET v_exact_count = (" in sql
    assert "ASSERT v_existing_count = v_exact_count" in sql
    assert "ASSERT v_exact_count <= 1" in sql
    assert "IF v_exact_count = 0 THEN" in sql
    assert "ELSE\n    SET v_registered_at = (" in sql


def test_read_routine_has_one_typed_run_input_and_exact_caller_allowlist():
    sql = _sql(READ)
    assert _parameters(sql, "sp_read_open_intelligence_quality_review_receipt_v1") == (
        ("p_run_id", "STRING"),
    )
    assert "SESSION_USER() IN (" in sql
    assert sql.count("trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com") == 1
    assert sql.count("trends-engine-oi-r3-release@ogilvy-trends-v2.iam.gserviceaccount.com") == 1
    assert "trends-engine-oi-r3-proof@" not in sql
    assert "trends-engine-staging@" not in sql


def test_read_routine_refuses_cardinality_and_drift_then_returns_the_complete_row():
    sql = _sql(READ)
    for marker in (
        "AS 'quality_review_receipt_unavailable'",
        "v_reconstructed_review_receipt_json = v_receipt.canonical_review_receipt_json",
        "v_review_receipt_digest = v_receipt.review_receipt_digest",
        "v_artifact_sha256 = v_receipt.artifact_sha256",
        "v_source_window_digest = v_receipt.source_window_digest",
        "v_candidate_projection_digest = v_receipt.candidate_projection_digest",
        "v_packet_digest = v_receipt.packet_digest",
        "open_intelligence_run_receipts_v1",
        "signal_candidates_v2",
        "signal_evidence_v2",
        "signal_membership_v2",
        "AS 'quality_review_receipt_stale'",
    ):
        assert marker in sql
    assert "SELECT review_store_contract_version, run_id, canonical_review_receipt_json" in sql
    assert "candidate_projection_digest, packet_digest, registered_by, registered_at" in sql


def test_receipt_store_sql_is_staging_template_only_and_has_no_direct_override_surface():
    combined = "\n".join(_sql(path) for path in (SCHEMA, REGISTER, READ))
    assert "production" not in combined.lower()
    assert "EXECUTE IMMEDIATE" not in combined
    assert "{project}.{dataset}" in combined
