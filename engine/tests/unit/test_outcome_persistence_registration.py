"""Boundary tests for the approved outcome persistence registration."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from src.analysis.open_intelligence import outcomes, persistence


def outcome_row(**overrides):
    row = {
        "client_scope_id": "fixture_scope",
        "market_scope": ["za"],
        "brand_config_id": "fixture_brand",
        "audience_lens_ids": [],
        "theme_id": "fixture_theme",
        "run_id": "outcome_run_001",
        "contract_version": "2.0.0",
        "outcome_id": "out_" + "a" * 64,
        "prediction_id": "pred_" + "b" * 64,
        "signal_id": "sig_" + "c" * 64,
        "signal_date": date(2026, 8, 25),
        "market": "za",
        "discovery_mode": "dynamic",
        "source_families": ["reddit", "youtube"],
        "source_family_map_version": "channel_family_v1",
        "evaluation_date": date(2026, 9, 1),
        "evaluated_at": datetime(2026, 9, 1, 18, 0, tzinfo=UTC),
        "outcome": "sustained",
        "observed_velocity": 0.62,
        "observed_breadth": 0.6,
        "observed_evidence_family_count": 2,
        "human_calibration_label": None,
        "human_reviewed_at": None,
        "resolution_reason": "breadth_and_evidence_floor_sustained",
        "rule_version": "outcome_rules_v1",
    }
    row.update(overrides)
    assert tuple(row) == outcomes.OUTCOME_ROW_FIELDS
    return row


def empty_batch(**overrides):
    values = {
        "candidates": (),
        "evidence": (),
        "membership": (),
        "lineage": (),
        "predictions": (),
        "outcomes": (),
    }
    values.update(overrides)
    return persistence.OpenIntelligenceRowBatch(**values)


def test_outcomes_register_the_existing_approved_table_and_natural_key():
    assert persistence.TABLE_BINDINGS["outcomes"] == "signal_outcomes_v2"
    assert persistence.ROW_FIELDS["outcomes"] == outcomes.OUTCOME_ROW_FIELDS
    assert persistence.NATURAL_KEYS["outcomes"] == (
        "prediction_id",
        "evaluation_date",
        "run_id",
    )


def test_batch_canonicalizes_outcome_types_and_preserves_nullable_measurements():
    row = outcome_row(
        observed_velocity=None,
        observed_breadth=None,
        observed_evidence_family_count=None,
    )

    batch = empty_batch(outcomes=(row,))

    assert batch.outcomes[0]["evaluated_at"] == row["evaluated_at"]
    assert batch.outcomes[0]["source_families"] == ("reddit", "youtube")
    assert batch.outcomes[0]["observed_velocity"] is None
    assert batch.outcomes[0]["observed_evidence_family_count"] is None


def test_outcome_typed_json_uses_timestamp_float_integer_repeated_and_null_types():
    payload = json.loads(persistence.canonical_typed_json("outcomes", outcome_row()))

    assert payload["evaluated_at"] == {
        "type": "timestamp",
        "value": "2026-09-01T18:00:00Z",
    }
    assert payload["observed_velocity"] == {"type": "float", "value": 0.62}
    assert payload["observed_evidence_family_count"] == {"type": "integer", "value": 2}
    assert payload["source_families"]["type"] == "repeated"
    assert payload["human_calibration_label"] == {"type": "null", "value": None}


def test_outcome_batch_rejects_duplicate_natural_key_and_wrong_fields():
    first = outcome_row(outcome_id="out_" + "a" * 64)
    duplicate_key = outcome_row(outcome_id="out_" + "d" * 64)

    with pytest.raises(persistence.BatchInvalid, match="duplicate natural keys"):
        empty_batch(outcomes=(first, duplicate_key))
    with pytest.raises(persistence.BatchInvalid, match="outcomes fields are invalid"):
        empty_batch(outcomes=({**first, "extra": True},))


def test_distinct_outcome_evaluation_runs_remain_distinct_natural_keys():
    first = outcome_row(run_id="outcome_run_001", outcome_id="out_" + "a" * 64)
    second = outcome_row(run_id="outcome_run_002", outcome_id="out_" + "d" * 64)

    batch = empty_batch(outcomes=(first, second))

    assert tuple(row["run_id"] for row in batch.outcomes) == (
        "outcome_run_001",
        "outcome_run_002",
    )


def test_outcome_schema_generation_matches_approved_types_and_nullability():
    fields = {
        field.name: (field.field_type, field.mode) for field in persistence._schema("outcomes")
    }

    assert fields["evaluated_at"] == ("TIMESTAMP", "REQUIRED")
    assert fields["observed_velocity"] == ("FLOAT64", "NULLABLE")
    assert fields["observed_breadth"] == ("FLOAT64", "NULLABLE")
    assert fields["observed_evidence_family_count"] == ("INT64", "NULLABLE")
    assert fields["human_calibration_label"] == ("STRING", "NULLABLE")
    assert fields["human_reviewed_at"] == ("TIMESTAMP", "NULLABLE")


def test_outcome_transaction_never_updates_and_refuses_a_changed_row_under_the_same_run():
    from types import SimpleNamespace

    temporary = SimpleNamespace(project="ogilvy-trends-v2", dataset_id="tmp", table_id="staged")
    sql = persistence._transaction_sql(
        "ogilvy-trends-v2", "trends_v2_staging", "outcomes", temporary
    )

    assert "ASSERT conflict_count = 0 AS 'immutable_conflict'" in sql
    assert "ROLLBACK TRANSACTION" in sql
    assert "WHEN NOT MATCHED THEN INSERT" not in sql
    assert "UPDATE" not in sql.upper().replace("ROLLBACK", "")
    assert "WHERE NOT EXISTS (SELECT 1 FROM" in sql
    assert "target.`prediction_id` = staged.`prediction_id`" in sql
    assert "target.`evaluation_date` = staged.`evaluation_date`" in sql
    assert "target.`run_id` = staged.`run_id`" in sql
    error = persistence._failure_for("outcomes", RuntimeError("assertion immutable_conflict"))
    assert isinstance(error, persistence.ImmutableConflict)
