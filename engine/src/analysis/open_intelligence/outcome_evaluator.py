"""Pure weekly outcome evaluation over bounded reader results."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from src.analysis.open_intelligence.outcome_reader import OutcomeReadResult
from src.analysis.open_intelligence.outcomes import (
    OUTCOME_ROW_FIELDS,
    OutcomeQualityFinding,
    OutcomeRules,
    build_signal_outcome_row,
    validate_prediction_row,
)
from src.contracts.open_intelligence import OUTCOME_STATES

REVIEW_RECORD_MISSING = "review_record_missing"
_PREDICTION_ID = re.compile(r"pred_[0-9a-f]{64}\Z")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class HumanCalibration:
    label: str
    reviewed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", _text(self.label, "human calibration label"))
        object.__setattr__(
            self,
            "reviewed_at",
            _timestamp(self.reviewed_at, "human calibration reviewed at"),
        )


@dataclass(frozen=True, slots=True)
class OutcomeEvaluationResult:
    rows: tuple[Mapping[str, object], ...]
    state_counts: Mapping[str, int]
    missing_prediction_ids: tuple[str, ...]
    observation_counts_by_prediction: Mapping[str, int]
    unreviewed_prediction_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.rows, (tuple, list)) or any(
            not isinstance(row, Mapping) or tuple(row) != OUTCOME_ROW_FIELDS for row in self.rows
        ):
            raise ValueError("outcome evaluation rows are invalid")
        rows = tuple(
            sorted(
                (_freeze(row) for row in self.rows),
                key=lambda row: (row["market"], row["prediction_id"]),
            )
        )
        natural_keys = tuple(
            (row["prediction_id"], row["evaluation_date"], row["run_id"]) for row in rows
        )
        if len(natural_keys) != len(set(natural_keys)):
            raise ValueError("duplicate outcome natural key")
        if not isinstance(self.state_counts, Mapping) or tuple(self.state_counts) != OUTCOME_STATES:
            raise ValueError("state counts fields are invalid")
        expected_counts = dict.fromkeys(OUTCOME_STATES, 0)
        for row in rows:
            if row["outcome"] not in OUTCOME_STATES:
                raise ValueError("outcome state is invalid")
            expected_counts[row["outcome"]] += 1
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in self.state_counts.values()
            )
            or dict(self.state_counts) != expected_counts
        ):
            raise ValueError("state counts do not match rows")
        prediction_ids = {row["prediction_id"] for row in rows}
        if (
            not isinstance(self.observation_counts_by_prediction, Mapping)
            or set(self.observation_counts_by_prediction) != prediction_ids
        ):
            raise ValueError("observation counts do not match rows")
        observation_counts = {}
        for prediction_id, count in self.observation_counts_by_prediction.items():
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("observation count is invalid")
            observation_counts[prediction_id] = count
        if not isinstance(self.missing_prediction_ids, (tuple, list)):
            raise ValueError("missing prediction ids are invalid")
        missing = tuple(sorted(self.missing_prediction_ids))
        expected_missing = tuple(
            sorted(
                prediction_id for prediction_id, count in observation_counts.items() if count == 0
            )
        )
        if len(missing) != len(set(missing)) or missing != expected_missing:
            raise ValueError("missing prediction ids do not match rows")
        if not isinstance(self.unreviewed_prediction_ids, (tuple, list)):
            raise ValueError("unreviewed prediction ids are invalid")
        unreviewed = tuple(sorted(self.unreviewed_prediction_ids))
        expected_unreviewed = tuple(
            sorted(
                row["prediction_id"]
                for row in rows
                if row["resolution_reason"] == REVIEW_RECORD_MISSING
            )
        )
        if len(unreviewed) != len(set(unreviewed)) or unreviewed != expected_unreviewed:
            raise ValueError("unreviewed prediction ids do not match rows")
        object.__setattr__(self, "unreviewed_prediction_ids", unreviewed)
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "state_counts", MappingProxyType(expected_counts))
        object.__setattr__(self, "missing_prediction_ids", missing)
        object.__setattr__(
            self,
            "observation_counts_by_prediction",
            MappingProxyType(observation_counts),
        )


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


def _quality_inputs(value: object) -> dict[str, tuple[OutcomeQualityFinding, ...]]:
    if not isinstance(value, Mapping):
        raise ValueError("quality findings by prediction must be a mapping")
    output = {}
    for prediction_id, findings in value.items():
        prediction_id = _text(prediction_id, "quality prediction id")
        if prediction_id in output:
            raise ValueError("duplicate normalized quality prediction id")
        if not isinstance(findings, (tuple, list)) or any(
            not isinstance(item, OutcomeQualityFinding) for item in findings
        ):
            raise ValueError("quality findings are invalid")
        output[prediction_id] = tuple(findings)
    return output


def _human_inputs(value: object) -> dict[str, HumanCalibration]:
    if not isinstance(value, Mapping):
        raise ValueError("human calibrations by prediction must be a mapping")
    output = {}
    for prediction_id, calibration in value.items():
        prediction_id = _text(prediction_id, "human prediction id")
        if prediction_id in output:
            raise ValueError("duplicate normalized human prediction id")
        if not isinstance(calibration, HumanCalibration):
            raise ValueError("human calibration is invalid")
        output[prediction_id] = calibration
    return output


def _reviewed_inputs(value: object) -> set[str] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Collection):
        raise ValueError("reviewed prediction ids are invalid")
    reviewed = set()
    for item in value:
        if not isinstance(item, str) or _PREDICTION_ID.fullmatch(item) is None:
            raise ValueError("reviewed prediction ids are invalid")
        reviewed.add(item)
    return reviewed


def build_due_outcome_rows(
    *,
    read_result: OutcomeReadResult,
    evaluated_at: datetime,
    evaluation_run_id: str,
    rules: OutcomeRules,
    quality_findings_by_prediction: Mapping[str, tuple[OutcomeQualityFinding, ...]],
    human_calibrations_by_prediction: Mapping[str, HumanCalibration],
    reviewed_prediction_ids: Collection[str] | None = None,
) -> OutcomeEvaluationResult:
    """Classify every due prediction without I/O or model judgment.

    When reviewed_prediction_ids is given, a due prediction outside it has no
    immutable review record and stays unresolved as review_record_missing.
    """
    if not isinstance(read_result, OutcomeReadResult):
        raise ValueError("outcome read result is invalid")
    evaluated_at = _timestamp(evaluated_at, "evaluated at")
    evaluation_run_id = _text(evaluation_run_id, "evaluation run id")
    if not isinstance(rules, OutcomeRules):
        raise ValueError("outcome rules are invalid")

    predictions = tuple(read_result.predictions)
    prediction_contracts = tuple(validate_prediction_row(row) for row in predictions)
    prediction_ids = tuple(contract["prediction_id"] for contract in prediction_contracts)
    if len(prediction_ids) != len(set(prediction_ids)):
        raise ValueError("duplicate prediction id")
    prediction_set = set(prediction_ids)
    if (
        not isinstance(read_result.observations_by_prediction, Mapping)
        or set(read_result.observations_by_prediction) != prediction_set
    ):
        raise ValueError("observations by prediction are inconsistent")
    expected_missing = tuple(
        sorted(
            prediction_id
            for prediction_id in prediction_ids
            if not read_result.observations_by_prediction[prediction_id]
        )
    )
    if tuple(sorted(read_result.missing_prediction_ids)) != expected_missing:
        raise ValueError("missing prediction ids are inconsistent")

    quality = _quality_inputs(quality_findings_by_prediction)
    human = _human_inputs(human_calibrations_by_prediction)
    if not set(quality).issubset(prediction_set) or not set(human).issubset(prediction_set):
        raise ValueError("external input references unknown prediction")
    reviewed = _reviewed_inputs(reviewed_prediction_ids)
    if reviewed is not None and (
        not set(quality).issubset(reviewed) or not set(human).issubset(reviewed)
    ):
        raise ValueError("review inputs reference an unreviewed prediction")

    ordered = tuple(
        sorted(
            zip(predictions, prediction_contracts, strict=True),
            key=lambda item: (item[1]["market"], item[1]["prediction_id"]),
        )
    )
    rows = []
    for prediction, contract in ordered:
        prediction_id = contract["prediction_id"]
        if evaluated_at.date() < contract["evaluation_date"]:
            raise ValueError("prediction evaluation date is after evaluated at")
        calibration = human.get(prediction_id)
        row = build_signal_outcome_row(
            prediction=prediction,
            observations=read_result.observations_by_prediction[prediction_id],
            evaluated_at=evaluated_at,
            evaluation_run_id=evaluation_run_id,
            rules=rules,
            quality_findings=quality.get(prediction_id, ()),
            human_calibration_label=None if calibration is None else calibration.label,
            human_reviewed_at=None if calibration is None else calibration.reviewed_at,
            review_record_missing=reviewed is not None and prediction_id not in reviewed,
        )
        rows.append(MappingProxyType(dict(row)))

    counts = dict.fromkeys(OUTCOME_STATES, 0)
    for row in rows:
        counts[row["outcome"]] += 1
    return OutcomeEvaluationResult(
        rows=tuple(rows),
        state_counts=MappingProxyType(counts),
        missing_prediction_ids=expected_missing,
        observation_counts_by_prediction=MappingProxyType(
            {
                prediction_id: len(read_result.observations_by_prediction[prediction_id])
                for prediction_id in prediction_ids
            }
        ),
        unreviewed_prediction_ids=tuple(
            row["prediction_id"]
            for row in rows
            if row["resolution_reason"] == REVIEW_RECORD_MISSING
        ),
    )
