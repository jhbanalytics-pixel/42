"""Boundary tests for pure weekly outcome evaluation."""

from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType

import pytest
from src.analysis.open_intelligence import outcome_reader, outcomes

PREDICTION_ID = "pred_" + "a" * 64
SIGNAL_ID = "sig_" + "b" * 64
EVALUATION_DATE = date(2026, 9, 1)
EVALUATED_AT = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)


def evaluator_module():
    name = "src.analysis.open_intelligence.outcome_evaluator"
    assert importlib.util.find_spec(name) is not None, "outcome evaluator module is missing"
    return importlib.import_module(name)


def prediction(**overrides):
    from tests.unit.test_outcome_reader import prediction_row

    return prediction_row(**overrides)


def observation(offset, **overrides):
    values = {
        "observed_at": datetime(2026, 8, 26, tzinfo=UTC) + timedelta(days=offset),
        "velocity": 0.62,
        "breadth": 0.6,
        "evidence_family_count": 2,
    }
    values.update(overrides)
    return outcomes.OutcomeObservation(**values)


def read_result(rows=None, observations_by_prediction=None):
    rows = (prediction(),) if rows is None else rows
    observations_by_prediction = (
        {PREDICTION_ID: tuple(observation(offset) for offset in range(7))}
        if observations_by_prediction is None
        else observations_by_prediction
    )
    missing = tuple(
        row["prediction_id"]
        for row in rows
        if not observations_by_prediction.get(row["prediction_id"], ())
    )
    return outcome_reader.OutcomeReadResult(
        predictions=tuple(rows),
        observations_by_prediction=MappingProxyType(dict(observations_by_prediction)),
        missing_prediction_ids=missing,
    )


def evaluate(module, result=None, **overrides):
    values = {
        "read_result": read_result() if result is None else result,
        "evaluated_at": EVALUATED_AT,
        "evaluation_run_id": "outcome_run_001",
        "rules": outcomes.OutcomeRules(),
        "quality_findings_by_prediction": {},
        "human_calibrations_by_prediction": {},
    }
    values.update(overrides)
    return module.build_due_outcome_rows(**values)


def test_complete_daily_window_builds_one_sustained_outcome():
    module = evaluator_module()

    result = evaluate(module)

    assert len(result.rows) == 1
    assert tuple(result.rows[0]) == outcomes.OUTCOME_ROW_FIELDS
    assert result.rows[0]["prediction_id"] == PREDICTION_ID
    assert result.rows[0]["run_id"] == "outcome_run_001"
    assert result.rows[0]["outcome"] == "sustained"
    assert result.state_counts == {
        "peaked": 0,
        "sustained": 1,
        "fizzled": 0,
        "noise": 0,
        "unresolved": 0,
    }
    assert result.missing_prediction_ids == ()
    assert result.observation_counts_by_prediction == {PREDICTION_ID: 7}
    with pytest.raises(AttributeError):
        result.rows[0]["market_scope"].append("ng")


def test_missing_daily_history_emits_unresolved_instead_of_dropping_prediction():
    module = evaluator_module()
    result = read_result(observations_by_prediction={PREDICTION_ID: ()})

    evaluated = evaluate(module, result)

    assert len(evaluated.rows) == 1
    assert evaluated.rows[0]["outcome"] == "unresolved"
    assert evaluated.rows[0]["resolution_reason"] == "daily_window_incomplete"
    assert evaluated.missing_prediction_ids == (PREDICTION_ID,)
    assert evaluated.observation_counts_by_prediction == {PREDICTION_ID: 0}


def test_timestamped_quality_and_human_noise_are_forwarded_without_model_judgment():
    module = evaluator_module()
    quality = outcomes.OutcomeQualityFinding(
        found_at=datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
        reason="duplicate_identity",
    )

    quality_result = evaluate(
        module,
        quality_findings_by_prediction={PREDICTION_ID: (quality,)},
    )
    human_result = evaluate(
        module,
        human_calibrations_by_prediction={
            PREDICTION_ID: module.HumanCalibration(
                label="noise",
                reviewed_at=datetime(2026, 8, 30, 10, 0, tzinfo=UTC),
            )
        },
    )

    assert quality_result.rows[0]["resolution_reason"] == "quality_failure:duplicate_identity"
    assert human_result.rows[0]["resolution_reason"] == "human_calibration_noise"


def test_unknown_prediction_inputs_and_wrong_value_types_fail_closed():
    module = evaluator_module()
    unknown_id = "pred_" + "f" * 64

    with pytest.raises(ValueError, match="unknown prediction"):
        evaluate(module, quality_findings_by_prediction={unknown_id: ()})
    with pytest.raises(ValueError, match="unknown prediction"):
        evaluate(
            module,
            human_calibrations_by_prediction={
                unknown_id: module.HumanCalibration(
                    label="noise",
                    reviewed_at=datetime(2026, 8, 30, tzinfo=UTC),
                )
            },
        )
    with pytest.raises(ValueError, match="quality findings"):
        evaluate(module, quality_findings_by_prediction={PREDICTION_ID: ("bad",)})
    with pytest.raises(ValueError, match="human calibration"):
        evaluate(module, human_calibrations_by_prediction={PREDICTION_ID: "bad"})
    quality = outcomes.OutcomeQualityFinding(
        found_at=datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
        reason="duplicate_identity",
    )
    with pytest.raises(ValueError, match="duplicate normalized quality"):
        evaluate(
            module,
            quality_findings_by_prediction={PREDICTION_ID: (quality,), f" {PREDICTION_ID} ": ()},
        )
    calibration = module.HumanCalibration(
        label="noise",
        reviewed_at=datetime(2026, 8, 30, 10, 0, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="duplicate normalized human"):
        evaluate(
            module,
            human_calibrations_by_prediction={
                PREDICTION_ID: calibration,
                f" {PREDICTION_ID} ": calibration,
            },
        )


def test_read_result_shape_missing_ids_and_evaluation_date_are_consistent():
    module = evaluator_module()
    malformed_missing = outcome_reader.OutcomeReadResult(
        predictions=(prediction(),),
        observations_by_prediction=MappingProxyType({PREDICTION_ID: ()}),
        missing_prediction_ids=(),
    )

    with pytest.raises(ValueError, match="missing prediction ids"):
        evaluate(module, malformed_missing)
    with pytest.raises(ValueError, match="evaluation date"):
        evaluate(module, read_result(rows=(prediction(evaluation_date=date(2026, 9, 2)),)))


def test_order_is_deterministic_and_duplicate_prediction_rows_fail_closed():
    module = evaluator_module()
    second_id = "pred_" + "c" * 64
    second_signal = "sig_" + "d" * 64
    first = prediction()
    second = prediction(prediction_id=second_id, signal_id=second_signal)
    observations_map = {
        PREDICTION_ID: tuple(observation(offset) for offset in range(7)),
        second_id: tuple(observation(offset) for offset in range(7)),
    }
    forward = read_result(rows=(first, second), observations_by_prediction=observations_map)
    reverse = read_result(rows=(second, first), observations_by_prediction=observations_map)

    assert evaluate(module, forward) == evaluate(module, reverse)
    with pytest.raises(ValueError, match="duplicate prediction"):
        evaluate(module, read_result(rows=(first, dict(first))))


def test_evaluation_result_rejects_count_duplicate_and_missing_id_contradictions():
    module = evaluator_module()
    valid = evaluate(module)

    with pytest.raises(ValueError, match="state counts"):
        module.OutcomeEvaluationResult(
            rows=valid.rows,
            state_counts={**valid.state_counts, "sustained": 0},
            missing_prediction_ids=(),
            observation_counts_by_prediction=valid.observation_counts_by_prediction,
        )
    with pytest.raises(ValueError, match="duplicate outcome"):
        module.OutcomeEvaluationResult(
            rows=(valid.rows[0], valid.rows[0]),
            state_counts={**valid.state_counts, "sustained": 2},
            missing_prediction_ids=(),
            observation_counts_by_prediction=valid.observation_counts_by_prediction,
        )
    with pytest.raises(ValueError, match="missing prediction"):
        module.OutcomeEvaluationResult(
            rows=valid.rows,
            state_counts=valid.state_counts,
            missing_prediction_ids=("pred_" + "f" * 64,),
            observation_counts_by_prediction=valid.observation_counts_by_prediction,
        )
    with pytest.raises(ValueError, match="missing prediction"):
        module.OutcomeEvaluationResult(
            rows=valid.rows,
            state_counts=valid.state_counts,
            missing_prediction_ids=(PREDICTION_ID,),
            observation_counts_by_prediction=valid.observation_counts_by_prediction,
        )
    missing = evaluate(
        module,
        read_result(observations_by_prediction={PREDICTION_ID: ()}),
    )
    with pytest.raises(ValueError, match="missing prediction"):
        module.OutcomeEvaluationResult(
            rows=missing.rows,
            state_counts=missing.state_counts,
            missing_prediction_ids=(),
            observation_counts_by_prediction=missing.observation_counts_by_prediction,
        )


def test_reviewed_set_gates_resolution_and_names_the_missing_record():
    module = evaluator_module()
    other_id = "pred_" + "c" * 64
    rows = (prediction(), prediction(prediction_id=other_id, signal_id="sig_" + "d" * 64))
    result = read_result(
        rows,
        {
            PREDICTION_ID: tuple(observation(offset) for offset in range(7)),
            other_id: tuple(observation(offset) for offset in range(7)),
        },
    )

    evaluation = evaluate(module, result, reviewed_prediction_ids=(PREDICTION_ID,))

    by_id = {row["prediction_id"]: row for row in evaluation.rows}
    assert by_id[PREDICTION_ID]["outcome"] == "sustained"
    assert by_id[other_id]["outcome"] == "unresolved"
    assert by_id[other_id]["resolution_reason"] == "review_record_missing"
    assert evaluation.unreviewed_prediction_ids == (other_id,)
    assert evaluation.state_counts["unresolved"] == 1
    ungated = evaluate(module, result)
    assert ungated.unreviewed_prediction_ids == ()
    assert all(row["outcome"] == "sustained" for row in ungated.rows)


def test_review_inputs_for_an_unreviewed_prediction_are_a_contradiction():
    module = evaluator_module()

    with pytest.raises(ValueError, match="unreviewed prediction"):
        evaluate(
            module,
            reviewed_prediction_ids=(),
            human_calibrations_by_prediction={
                PREDICTION_ID: module.HumanCalibration(
                    label="noise", reviewed_at=EVALUATED_AT - timedelta(hours=1)
                )
            },
        )
    with pytest.raises(ValueError, match="reviewed prediction ids"):
        evaluate(module, reviewed_prediction_ids=("not-a-prediction",))
