"""Boundary tests for deterministic dynamic-signal outcomes."""

from __future__ import annotations

import importlib
import importlib.util
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from src.analysis.open_intelligence.predictions import PREDICTION_ROW_FIELDS

ROOT = Path(__file__).resolve().parents[2]
PREDICTION_ID = "pred_" + "a" * 64
SIGNAL_ID = "sig_" + "b" * 64
PREDICTED_AT = datetime(2026, 8, 25, 6, 50, tzinfo=UTC)
EVALUATION_DATE = date(2026, 9, 1)
EVALUATED_AT = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)


def outcome_module():
    name = "src.analysis.open_intelligence.outcomes"
    assert importlib.util.find_spec(name) is not None, "outcome module is missing"
    return importlib.import_module(name)


def prediction(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "client_scope_id": "fixture_scope",
        "market_scope": ["za"],
        "brand_config_id": "fixture_brand",
        "audience_lens_ids": [],
        "theme_id": "fixture_theme",
        "run_id": "prediction_run_001",
        "contract_version": "2.0.0",
        "prediction_id": PREDICTION_ID,
        "signal_id": SIGNAL_ID,
        "signal_date": date(2026, 8, 25),
        "market": "za",
        "discovery_mode": "dynamic",
        "source_families": ["reddit", "youtube"],
        "evidence_state": "ready",
        "first_seen_at": datetime(2026, 8, 22, 11, 0, tzinfo=UTC),
        "predicted_at": PREDICTED_AT,
        "expected_trajectory": "sustained",
        "evaluation_date": EVALUATION_DATE,
        "baseline": {"velocity": 0.68, "breadth": 0.62, "evidence_family_count": 2},
        "promotion_target": {
            "velocity": 0.6,
            "breadth": 0.6,
            "evidence_family_count": 2,
        },
        "invalidation_condition": "Breadth falls below 0.60 before 2026-09-01.",
        "cluster_build_version": "hybrid_graph_v1",
        "source_family_map_version": "channel_family_v1",
        "rule_version": "prediction_rules_v1",
        "display_eligible": False,
    }
    row.update(overrides)
    assert tuple(row) == PREDICTION_ROW_FIELDS
    return row


def observations(module, daily_overrides=None):
    daily_overrides = daily_overrides or {}
    rows = []
    for offset in range(1, 8):
        values = {
            "observed_at": PREDICTED_AT.replace(hour=12) + timedelta(days=offset),
            "velocity": 0.62,
            "breadth": 0.6,
            "evidence_family_count": 2,
        }
        values.update(daily_overrides.get(offset, {}))
        rows.append(module.OutcomeObservation(**values))
    return tuple(rows)


def build(module, rows=(), **overrides):
    values = {
        "prediction": prediction(),
        "observations": rows,
        "evaluated_at": EVALUATED_AT,
        "evaluation_run_id": "outcome_run_001",
        "rules": module.OutcomeRules(),
    }
    values.update(overrides)
    return module.build_signal_outcome_row(**values)


def schema_fields() -> tuple[str, ...]:
    schema = (ROOT / "infra" / "bigquery_schemas" / "signal_outcomes_v2.sql").read_text(
        encoding="utf-8"
    )
    column_block = schema.split(")\nPARTITION BY", maxsplit=1)[0]
    return tuple(re.findall(r"^  ([a-z_]+) ", column_block, re.M))


def test_outcome_fields_match_approved_schema():
    module = outcome_module()

    assert schema_fields() == module.OUTCOME_ROW_FIELDS


def test_peaked_outcome_uses_full_window_but_reports_evaluation_metrics():
    module = outcome_module()
    rows = observations(
        module,
        {
            1: {"velocity": 0.65, "breadth": 0.5, "evidence_family_count": 2},
            3: {"velocity": 0.8, "breadth": 0.7, "evidence_family_count": 3},
            7: {"velocity": 0.65, "breadth": 0.61, "evidence_family_count": 2},
        },
    )

    result = build(module, rows)

    assert tuple(result) == module.OUTCOME_ROW_FIELDS
    assert result == {
        "client_scope_id": "fixture_scope",
        "market_scope": ["za"],
        "brand_config_id": "fixture_brand",
        "audience_lens_ids": [],
        "theme_id": "fixture_theme",
        "run_id": "outcome_run_001",
        "contract_version": "2.0.0",
        "outcome_id": "out_1939cc6418950e557ca266e73b4279dac77f713b7a7a84b7494bb16796cb249a",
        "prediction_id": PREDICTION_ID,
        "signal_id": SIGNAL_ID,
        "signal_date": date(2026, 8, 25),
        "market": "za",
        "discovery_mode": "dynamic",
        "source_families": ["reddit", "youtube"],
        "source_family_map_version": "channel_family_v1",
        "evaluation_date": EVALUATION_DATE,
        "evaluated_at": EVALUATED_AT,
        "outcome": "peaked",
        "observed_velocity": 0.65,
        "observed_breadth": 0.61,
        "observed_evidence_family_count": 2,
        "human_calibration_label": None,
        "human_reviewed_at": None,
        "resolution_reason": "promotion_baseline_exceeded",
        "rule_version": "outcome_rules_v1",
    }


def test_sustained_requires_every_day_above_breadth_and_evidence_floor():
    module = outcome_module()

    result = build(module, observations(module))

    assert result["outcome"] == "sustained"
    assert result["resolution_reason"] == "breadth_and_evidence_floor_sustained"


def test_sustained_window_does_not_become_peaked_when_one_day_exceeds_baseline():
    module = outcome_module()
    rows = observations(
        module,
        {3: {"velocity": 0.8, "breadth": 0.7, "evidence_family_count": 3}},
    )

    result = build(module, rows)

    assert (result["outcome"], result["resolution_reason"]) == (
        "sustained",
        "breadth_and_evidence_floor_sustained",
    )


def test_outcome_id_uses_the_declared_natural_key_not_rule_version():
    module = outcome_module()
    rows = observations(module)

    first = build(module, rows)
    changed_rules = build(module, rows, rules=module.OutcomeRules(rule_version="outcome_rules_v2"))

    assert first["outcome_id"] == changed_rules["outcome_id"]
    assert first["rule_version"] != changed_rules["rule_version"]


def test_fizzled_requires_no_target_crossing_and_final_velocity_and_breadth_loss():
    module = outcome_module()
    rows = observations(
        module,
        {
            offset: {
                "velocity": 0.55 if offset < 7 else 0.3,
                "breadth": 0.5 if offset < 7 else 0.3,
                "evidence_family_count": 2 if offset < 7 else 1,
            }
            for offset in range(1, 8)
        },
    )

    result = build(module, rows)

    assert result["outcome"] == "fizzled"
    assert result["resolution_reason"] == "velocity_and_breadth_lost_before_target"


def test_target_crossing_prevents_a_late_decline_from_being_called_fizzled():
    module = outcome_module()
    daily = {
        offset: {"velocity": 0.5, "breadth": 0.5, "evidence_family_count": 2}
        for offset in range(1, 8)
    }
    daily[2] = {"velocity": 0.65, "breadth": 0.61, "evidence_family_count": 2}
    daily[7] = {"velocity": 0.3, "breadth": 0.3, "evidence_family_count": 1}

    result = build(module, observations(module, daily))

    assert (result["outcome"], result["resolution_reason"]) == (
        "unresolved",
        "outcome_unresolved",
    )


def test_timestamped_quality_noise_overrides_missing_metrics_after_window_closes():
    module = outcome_module()
    incomplete = observations(module, {2: {"velocity": None, "breadth": None}})
    finding = module.OutcomeQualityFinding(
        found_at=datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
        reason="duplicate_identity",
    )

    result = build(module, incomplete, quality_findings=(finding,))

    assert result["outcome"] == "noise"
    assert result["resolution_reason"] == "quality_failure:duplicate_identity"


def test_human_noise_overrides_missing_metrics_after_window_closes():
    module = outcome_module()
    incomplete = observations(module, {2: {"velocity": None, "breadth": None}})

    result = build(
        module,
        incomplete,
        human_calibration_label="noise",
        human_reviewed_at=datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
    )

    assert result["outcome"] == "noise"
    assert result["resolution_reason"] == "human_calibration_noise"


def test_open_or_incomplete_window_is_unresolved():
    module = outcome_module()
    complete = observations(module)
    open_window = build(
        module,
        complete[:-1],
        evaluated_at=datetime(2026, 8, 31, 18, 0, tzinfo=UTC),
    )
    missing_day = build(module, complete[:2] + complete[3:])
    incomplete_metrics = build(
        module,
        observations(module, {4: {"evidence_family_count": None}}),
    )

    assert (open_window["outcome"], open_window["resolution_reason"]) == (
        "unresolved",
        "evaluation_window_open",
    )
    assert (missing_day["outcome"], missing_day["resolution_reason"]) == (
        "unresolved",
        "daily_window_incomplete",
    )
    assert (incomplete_metrics["outcome"], incomplete_metrics["resolution_reason"]) == (
        "unresolved",
        "observed_metrics_incomplete",
    )


def test_complete_mixed_window_remains_unresolved():
    module = outcome_module()
    mixed = observations(
        module,
        {
            offset: {"velocity": 0.7, "breadth": 0.4, "evidence_family_count": 2}
            for offset in range(1, 8)
        },
    )

    result = build(module, mixed)

    assert (result["outcome"], result["resolution_reason"]) == (
        "unresolved",
        "outcome_unresolved",
    )


def test_rejects_future_leakage_duplicate_days_and_post_evaluation_data():
    module = outcome_module()
    complete = observations(module)
    duplicate_day = (
        *complete,
        module.OutcomeObservation(
            observed_at=complete[0].observed_at.replace(hour=13),
            velocity=0.5,
            breadth=0.5,
            evidence_family_count=2,
        ),
    )
    after_window = (
        *complete,
        module.OutcomeObservation(
            observed_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
            velocity=1.0,
            breadth=1.0,
            evidence_family_count=4,
        ),
    )

    with pytest.raises(ValueError, match="duplicate observation date"):
        build(module, duplicate_day)
    with pytest.raises(ValueError, match="contracted evaluation window"):
        build(module, after_window)
    with pytest.raises(ValueError, match="evaluated at"):
        build(
            module,
            complete,
            evaluated_at=datetime(2026, 9, 1, 11, 0, tzinfo=UTC),
        )
    late_finding = module.OutcomeQualityFinding(
        found_at=datetime(2026, 9, 2, 9, 0, tzinfo=UTC),
        reason="duplicate_identity",
    )
    with pytest.raises(ValueError, match="contracted evaluation window"):
        build(
            module,
            complete,
            evaluated_at=datetime(2026, 9, 3, 18, 0, tzinfo=UTC),
            quality_findings=(late_finding,),
        )


def test_human_calibration_is_timestamp_bound():
    module = outcome_module()
    rows = observations(module)

    with pytest.raises(ValueError, match="human reviewed at is required"):
        build(module, rows, human_calibration_label="noise")
    with pytest.raises(ValueError, match="human calibration label is required"):
        build(
            module,
            rows,
            human_reviewed_at=datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="human reviewed at cannot be after evaluated at"):
        build(
            module,
            rows,
            human_calibration_label="noise",
            human_reviewed_at=datetime(2026, 9, 1, 19, 0, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="contracted evaluation window"):
        build(
            module,
            rows,
            evaluated_at=datetime(2026, 9, 3, 18, 0, tzinfo=UTC),
            human_calibration_label="noise",
            human_reviewed_at=datetime(2026, 9, 2, 9, 0, tzinfo=UTC),
        )


def test_prediction_contract_quality_failures_and_observation_metrics_fail_closed():
    module = outcome_module()
    rows = observations(module)

    with pytest.raises(ValueError, match="prediction row fields"):
        build(module, rows, prediction={**prediction(), "extra": True})
    with pytest.raises(ValueError, match="quality findings"):
        build(module, rows, quality_findings=("vibes",))
    historical = build(
        module,
        rows,
        prediction=prediction(
            source_families=["brand24", "reddit"],
            source_family_map_version="channel_family_v0",
        ),
    )
    assert historical["source_families"] == ["brand24", "reddit"]
    assert historical["source_family_map_version"] == "channel_family_v0"
    with pytest.raises(ValueError, match="aggregate source family"):
        build(module, rows, prediction=prediction(source_families=["all", "reddit"]))
    early_evaluation = prediction(evaluation_date=date(2026, 8, 25))
    finding = module.OutcomeQualityFinding(
        found_at=datetime(2026, 8, 26, 9, 0, tzinfo=UTC),
        reason="duplicate_identity",
    )
    with pytest.raises(ValueError, match="evaluation date must be after signal date"):
        build(
            module,
            (),
            prediction=early_evaluation,
            quality_findings=(finding,),
        )
    with pytest.raises(ValueError, match="velocity"):
        module.OutcomeObservation(
            observed_at=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
            velocity=True,
            breadth=0.5,
            evidence_family_count=2,
        )


def test_observation_order_is_deterministic():
    module = outcome_module()
    rows = observations(
        module,
        {3: {"velocity": 0.8, "breadth": 0.7, "evidence_family_count": 3}},
    )

    assert build(module, rows) == build(module, tuple(reversed(rows)))


def prediction_v2(**overrides: object) -> dict[str, object]:
    from src.analysis.open_intelligence.predictions import (
        PREDICTION_ROW_FIELDS_V2,
        PREDICTION_TIME_CONTRACT_V2,
    )

    row = prediction(
        evaluation_date=date(2026, 9, 2),
        invalidation_condition="Breadth falls below 0.60 before 2026-09-02.",
    )
    row.update(
        {
            "time_contract_version": PREDICTION_TIME_CONTRACT_V2,
            "available_at": datetime(2026, 8, 26, 6, 50, tzinfo=UTC),
            "evaluation_window_start": date(2026, 8, 27),
            "evaluation_horizon_days": 7,
        }
    )
    row.update(overrides)
    assert tuple(row) == PREDICTION_ROW_FIELDS_V2
    return row


def observations_v2(module, daily_overrides=None):
    daily_overrides = daily_overrides or {}
    rows = []
    for offset in range(7):
        day = date(2026, 8, 27) + timedelta(days=offset)
        midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
        values = {
            "observed_at": midnight,
            "velocity": 0.62,
            "breadth": 0.6,
            "evidence_family_count": 2,
            "available_at": midnight + timedelta(days=1, minutes=40),
            "run_id": f"daily_run_{day.isoformat()}",
        }
        values.update(daily_overrides.get(offset, {}))
        rows.append(module.OutcomeObservation(**values))
    return tuple(rows)


def test_delayed_v1_prediction_rejects_its_first_midnight_observation():
    module = outcome_module()
    delayed = prediction(predicted_at=PREDICTED_AT + timedelta(days=1))
    midnight_rows = tuple(
        module.OutcomeObservation(
            observed_at=datetime(2026, 8, 26, tzinfo=UTC) + timedelta(days=offset),
            velocity=0.62,
            breadth=0.6,
            evidence_family_count=2,
        )
        for offset in range(7)
    )

    with pytest.raises(ValueError, match="observation must be after predicted at"):
        build(module, midnight_rows, prediction=delayed)


def test_delayed_v2_prediction_evaluates_from_its_declared_window_start():
    module = outcome_module()

    row = build(
        module,
        observations_v2(module),
        prediction=prediction_v2(),
        evaluated_at=datetime(2026, 9, 3, 5, 0, tzinfo=UTC),
    )

    assert row["outcome"] == "sustained"
    assert row["evaluation_date"] == date(2026, 9, 2)
    assert row["observed_evidence_family_count"] == 2


def test_v2_contract_refuses_undeclared_or_inconsistent_windows():
    module = outcome_module()
    cases = (
        ({"evaluation_window_start": date(2026, 8, 26)}, "window start must follow availability"),
        ({"available_at": datetime(2026, 8, 25, 6, 49, tzinfo=UTC)}, "before predicted at"),
        ({"evaluation_horizon_days": 0}, "horizon must be a positive integer"),
        ({"evaluation_date": date(2026, 9, 3)}, "must close the declared horizon"),
        ({"time_contract_version": "open_intelligence_prediction_time_v9"}, "time contract"),
    )
    for overrides, message in cases:
        with pytest.raises(ValueError, match=message):
            module.validate_prediction_row(prediction_v2(**overrides))


def test_v2_observations_require_availability_no_later_than_evaluation():
    module = outcome_module()
    evaluated_at = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    late = observations_v2(module, {6: {"available_at": evaluated_at + timedelta(minutes=1)}})
    with pytest.raises(ValueError, match="availability cannot be after evaluated at"):
        build(module, late, prediction=prediction_v2(), evaluated_at=evaluated_at)
    unstamped = observations_v2(
        module, {offset: {"available_at": None, "run_id": None} for offset in range(7)}
    )
    with pytest.raises(ValueError, match="observation availability is required"):
        build(module, unstamped, prediction=prediction_v2(), evaluated_at=evaluated_at)
    with pytest.raises(ValueError, match="availability precedes its observation date"):
        module.OutcomeObservation(
            observed_at=datetime(2026, 8, 27, tzinfo=UTC),
            velocity=0.6,
            breadth=0.6,
            evidence_family_count=2,
            available_at=datetime(2026, 8, 26, 23, 59, tzinfo=UTC),
            run_id="daily_run",
        )
    with pytest.raises(ValueError, match="run id"):
        module.OutcomeObservation(
            observed_at=datetime(2026, 8, 27, tzinfo=UTC),
            velocity=0.6,
            breadth=0.6,
            evidence_family_count=2,
            available_at=datetime(2026, 8, 28, tzinfo=UTC),
            run_id=None,
        )


def test_missing_review_record_keeps_a_complete_window_unresolved():
    module = outcome_module()

    row = build(module, observations(module), review_record_missing=True)

    assert row["outcome"] == "unresolved"
    assert row["resolution_reason"] == "review_record_missing"
    assert row["observed_velocity"] == 0.62
    with pytest.raises(ValueError, match="review record missing cannot carry review inputs"):
        build(
            module,
            observations(module),
            review_record_missing=True,
            human_calibration_label="noise",
            human_reviewed_at=EVALUATED_AT - timedelta(hours=1),
        )
    still_open = build(
        module,
        observations(module)[:3],
        evaluated_at=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
        review_record_missing=True,
    )
    assert still_open["resolution_reason"] == "evaluation_window_open"
