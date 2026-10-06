"""Frozen copy pins must survive resealed current metadata and unchanged counts."""

import copy as copying
import hashlib
import importlib
from dataclasses import replace

import pytest
from scripts.staging import copy_open_intelligence_replay_sources as original


def module():
    return importlib.import_module(
        "src.analysis.open_intelligence.general_question_copy_validation"
    )


def proof_rows():
    return [
        {
            "copy_run_id": original.COPY_RUN_ID,
            "table_name": table.name,
            "target_rows": table.source_rows,
            "target_distinct_ids": table.source_rows,
            "target_null_keys": 0,
            "manifest_rows": table.source_rows,
            "manifest_distinct_ids": table.source_rows,
            "manifest_null_ids": 0,
            "manifest_header_mismatches": 0,
            "target_manifest_mismatches": 0,
            "receipt_rows": 1,
            "receipt_header_mismatches": 0,
            "unexpected_manifest_rows": 0,
            "unexpected_receipt_rows": 0,
            "target_source_set_digest": table.source_set_digest,
            "manifest_source_set_digest": table.source_set_digest,
        }
        for table in original._table_contracts().values()
    ]


def validate(rows):
    api = module()
    return api.validate_current_source_copy_proof(rows, query=api.build_current_source_copy_query())


def test_fixed_query_reuses_original_hash_domains_casts_order_and_chunking():
    query = module().build_current_source_copy_query()
    assert query.template_id == "current_source_copy_v1"
    assert query.candidate_limit == 0
    assert query.transport_row_limit == 4
    assert query.parameters == ()
    assert query.sql_digest == hashlib.sha256(query.sql.encode()).hexdigest()
    for table in original._table_contracts().values():
        assert original._copy_id_expr(table, "target") in query.sql
        assert original._content_hash_expr(table, "target") in query.sql
        assert original._digest_select("target_" + table.name) in query.sql
        assert original._digest_select("manifest_" + table.name) in query.sql
    assert "`ogilvy-trends-v2.trends_v2_dev." not in query.sql
    assert "FULL OUTER JOIN" in query.sql
    assert "IS DISTINCT FROM" in query.sql
    assert not any(
        word in query.sql for word in ("INSERT INTO", "DELETE FROM", "CREATE TEMP", "UPDATE ")
    )


def test_exact_frozen_aggregates_validate_content_only_and_are_detached():
    rows = proof_rows()
    result = validate(rows[::-1])
    assert result["content_validated"] is True
    assert result["physical_schema_required"] is True
    assert result["source_authority"] is False
    assert result["tables"] == rows
    assert len(module().source_copy_schema_requirements()) == 6
    result["tables"][0]["target_rows"] = 0
    assert rows[0]["target_rows"] == 5158


@pytest.mark.parametrize("table", list(original.SOURCE_COPY_TABLES))
def test_same_count_changed_bytes_or_requested_keys_cannot_be_resealed(table):
    rows = proof_rows()
    row = next(item for item in rows if item["table_name"] == table)
    changed = hashlib.sha256(b"changed current source bytes or sample_row_ids").hexdigest()
    row["target_source_set_digest"] = changed
    row["manifest_source_set_digest"] = changed
    # The current manifest and digest now agree, but the original copy pin does not.
    if table in ("seed_graph", "seed_candidates"):
        enriched = next(item for item in rows if item["table_name"] == "enriched_content")
        enriched["target_source_set_digest"] = changed
        enriched["manifest_source_set_digest"] = changed
    with pytest.raises(ValueError, match="current_source_copy_invalid"):
        validate(rows)


@pytest.mark.parametrize(
    "field",
    [
        "target_rows",
        "target_distinct_ids",
        "target_null_keys",
        "manifest_rows",
        "manifest_distinct_ids",
        "manifest_null_ids",
        "manifest_header_mismatches",
        "target_manifest_mismatches",
        "receipt_rows",
        "receipt_header_mismatches",
        "unexpected_manifest_rows",
        "unexpected_receipt_rows",
    ],
)
def test_missing_extra_duplicate_and_header_guards_cannot_be_ignored(field):
    rows = proof_rows()
    rows[0][field] += 1
    with pytest.raises(ValueError):
        validate(rows)


@pytest.mark.parametrize("value", [True, None, -1, 1.0, "0"])
def test_unknown_or_coerced_counts_refuse(value):
    rows = proof_rows()
    rows[0]["target_null_keys"] = value
    with pytest.raises(ValueError):
        validate(rows)


def test_duplicate_missing_foreign_and_mutated_descriptors_refuse():
    rows = proof_rows()
    for changed in (rows[:-1], [*rows, rows[0]], [rows[0]] * 4):
        with pytest.raises(ValueError):
            validate(changed)
    changed = copying.deepcopy(rows)
    changed[0]["copy_run_id"] = "foreign"
    with pytest.raises(ValueError):
        validate(changed)
    query = module().build_current_source_copy_query()
    with pytest.raises(ValueError):
        module().validate_current_source_copy_proof(rows, query=replace(query, sql="SELECT 1"))
    with pytest.raises(ValueError):
        module().build_current_source_copy_query(profile="unknown")
