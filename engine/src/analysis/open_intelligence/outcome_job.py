"""Staging-only read, evaluate, and persist orchestration for outcomes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import MappingProxyType

from src.analysis.open_intelligence import persistence
from src.analysis.open_intelligence.outcome_evaluator import (
    HumanCalibration,
    build_due_outcome_rows,
)
from src.analysis.open_intelligence.outcome_reader import (
    OBSERVATION_QUERY_VERSION_V2,
    TARGET_DATASET,
    TARGET_LOCATION,
    TARGET_PROJECT,
    read_due_outcome_inputs,
)
from src.analysis.open_intelligence.outcome_review_reader import (
    OutcomeReviewRecords,
    review_inputs_for,
)
from src.analysis.open_intelligence.outcomes import OutcomeQualityFinding, OutcomeRules
from src.contracts.open_intelligence import OUTCOME_STATES

_PREDICTION_ID = re.compile(r"pred_[0-9a-f]{64}\Z")
# v1 evaluates the v1 observation query with caller supplied review maps. v2 selects
# one completed daily snapshot per day by the evaluation cutoff and takes every review
# input from immutable review records; a prediction without a record stays unresolved.
EVALUATION_VERSION_V1 = "outcome_evaluation_v1"
EVALUATION_VERSION_V2 = "outcome_evaluation_v2"
EVALUATION_VERSIONS = (EVALUATION_VERSION_V1, EVALUATION_VERSION_V2)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _date(value: object, field: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{field} must be a date")
    return value


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class OutcomeJobResult:
    evaluation_run_id: str
    dry_run: bool
    due_prediction_count: int
    outcome_row_count: int
    state_counts: Mapping[str, int]
    missing_prediction_ids: tuple[str, ...]
    persistence_result: object | None
    evaluation_version: str = EVALUATION_VERSION_V1
    unreviewed_prediction_ids: tuple[str, ...] = ()
    # The written rows, carried so a closed cohort can be scored from what was
    # actually evaluated. None says the rows were not carried; an empty tuple
    # says the run wrote none. Absence is explicit, so a caller that needs the
    # rows refuses on None instead of scoring a week as an empty cohort.
    outcome_rows: tuple[Mapping[str, object], ...] | None = None

    def __post_init__(self) -> None:
        if self.evaluation_version not in EVALUATION_VERSIONS:
            raise ValueError("evaluation version is unsupported")
        if not isinstance(self.unreviewed_prediction_ids, (tuple, list)):
            raise ValueError("unreviewed prediction ids are invalid")
        unreviewed = tuple(sorted(self.unreviewed_prediction_ids))
        if (
            len(unreviewed) != len(set(unreviewed))
            or any(
                not isinstance(item, str) or _PREDICTION_ID.fullmatch(item) is None
                for item in unreviewed
            )
            or (unreviewed and self.evaluation_version != EVALUATION_VERSION_V2)
        ):
            raise ValueError("unreviewed prediction ids are inconsistent")
        object.__setattr__(self, "unreviewed_prediction_ids", unreviewed)
        object.__setattr__(
            self,
            "evaluation_run_id",
            _text(self.evaluation_run_id, "evaluation run id"),
        )
        if type(self.dry_run) is not bool:
            raise ValueError("dry run must be boolean")
        for field in ("due_prediction_count", "outcome_row_count"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field.replace('_', ' ')} is invalid")
        if self.outcome_row_count != self.due_prediction_count:
            raise ValueError("outcome row count must equal due prediction count")
        if not isinstance(self.state_counts, Mapping) or tuple(self.state_counts) != OUTCOME_STATES:
            raise ValueError("state counts fields are invalid")
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in self.state_counts.values()
            )
            or sum(self.state_counts.values()) != self.outcome_row_count
        ):
            raise ValueError("state counts do not match outcome rows")
        if not isinstance(self.missing_prediction_ids, (tuple, list)):
            raise ValueError("missing prediction ids are invalid")
        missing = tuple(
            sorted(
                _text(prediction_id, "missing prediction id")
                for prediction_id in self.missing_prediction_ids
            )
        )
        if (
            len(missing) != len(set(missing))
            or len(missing) > self.due_prediction_count
            or any(_PREDICTION_ID.fullmatch(prediction_id) is None for prediction_id in missing)
        ):
            raise ValueError("missing prediction ids are inconsistent")
        if self.outcome_row_count == 0 and self.persistence_result is not None:
            raise ValueError("empty outcome job cannot carry persistence result")
        if self.outcome_row_count > 0 and self.persistence_result is None:
            raise ValueError("nonempty outcome job requires persistence result")
        if self.persistence_result is not None:
            if not isinstance(self.persistence_result, persistence.PersistenceResult):
                raise ValueError("persistence result must be PersistenceResult")
            expected_counts = dict.fromkeys(persistence.TABLE_BINDINGS, 0)
            expected_counts["outcomes"] = self.outcome_row_count
            validated_counts = self.persistence_result.validated_counts
            if (
                self.persistence_result.dry_run is not self.dry_run
                or self.persistence_result.project != TARGET_PROJECT
                or self.persistence_result.dataset != TARGET_DATASET
                or not isinstance(validated_counts, Mapping)
                or set(validated_counts) != set(persistence.TABLE_BINDINGS)
                or any(type(value) is not int or value < 0 for value in validated_counts.values())
                or dict(validated_counts) != expected_counts
            ):
                raise ValueError("persistence result does not match outcome job")
        rows = self.outcome_rows
        if rows is not None:
            if not isinstance(rows, (tuple, list)) or any(
                not isinstance(row, Mapping) for row in rows
            ):
                raise ValueError("outcome rows are invalid")
            if len(rows) != self.outcome_row_count:
                raise ValueError("outcome rows do not match the outcome row count")
            object.__setattr__(self, "outcome_rows", tuple(rows))
        object.__setattr__(self, "state_counts", MappingProxyType(dict(self.state_counts)))
        object.__setattr__(
            self,
            "missing_prediction_ids",
            missing,
        )


def run_outcome_evaluation(
    *,
    client: object,
    dataset: str,
    evaluation_date: date,
    evaluated_at: datetime,
    client_scope_id: str,
    evaluation_run_id: str,
    outcome_rules: OutcomeRules,
    persistence_rule_bundle: object,
    dry_run: bool,
    quality_findings_by_prediction: Mapping[str, tuple[OutcomeQualityFinding, ...]],
    human_calibrations_by_prediction: Mapping[str, HumanCalibration],
    evaluation_version: str = EVALUATION_VERSION_V1,
    review_records: OutcomeReviewRecords | None = None,
    carry_outcome_rows: bool = False,
    verified_result: OutcomeJobResult | None = None,
) -> OutcomeJobResult:
    """Compose the reviewed staging reader, evaluator, and persistence boundary.

    A write given a verified result persists exactly the rows that dry run
    carried, with no new read or evaluation, so the rows a caller checked are
    the rows that land.
    """
    if evaluation_version not in EVALUATION_VERSIONS:
        raise ValueError("evaluation version is unsupported")
    if type(carry_outcome_rows) is not bool:
        raise ValueError("carry outcome rows must be boolean")
    reviewed = evaluation_version == EVALUATION_VERSION_V2
    if reviewed:
        if review_records is None:
            raise ValueError("review records are required under outcome_evaluation_v2")
        if not isinstance(review_records, OutcomeReviewRecords):
            raise ValueError("review records are invalid")
        if quality_findings_by_prediction or human_calibrations_by_prediction:
            raise ValueError("review inputs come from review records under outcome_evaluation_v2")
    elif review_records is not None:
        raise ValueError("review records require outcome_evaluation_v2")
    if getattr(client, "project", None) != TARGET_PROJECT or dataset != TARGET_DATASET:
        raise ValueError("exact staging target is required")
    evaluation_date = _date(evaluation_date, "evaluation date")
    evaluated_at = _timestamp(evaluated_at, "evaluated at")
    if evaluated_at.date() < evaluation_date:
        raise ValueError("evaluated at cannot be before evaluation date")
    client_scope_id = _text(client_scope_id, "client scope id")
    evaluation_run_id = _text(evaluation_run_id, "evaluation run id")
    if not isinstance(outcome_rules, OutcomeRules):
        raise ValueError("outcome rules are invalid")
    if type(dry_run) is not bool:
        raise ValueError("dry run must be boolean")
    persistence.validate_rule_bundle(persistence_rule_bundle)
    if verified_result is not None:
        return _persist_verified_result(
            client=client,
            verified_result=verified_result,
            evaluation_run_id=evaluation_run_id,
            evaluation_version=evaluation_version,
            persistence_rule_bundle=persistence_rule_bundle,
            dry_run=dry_run,
            carry_outcome_rows=carry_outcome_rows,
        )

    reader_options: dict[str, object] = {}
    if reviewed:
        reader_options = {
            "observation_query_version": OBSERVATION_QUERY_VERSION_V2,
            "evaluation_cutoff": evaluated_at,
        }
    read_result = read_due_outcome_inputs(
        client=client,
        dataset=dataset,
        evaluation_date=evaluation_date,
        client_scope_id=client_scope_id,
        evaluation_run_id=evaluation_run_id,
        **reader_options,
    )
    due_count = len(read_result.predictions)
    if due_count == 0:
        return OutcomeJobResult(
            evaluation_run_id=evaluation_run_id,
            dry_run=dry_run,
            due_prediction_count=0,
            outcome_row_count=0,
            state_counts=MappingProxyType(dict.fromkeys(OUTCOME_STATES, 0)),
            missing_prediction_ids=(),
            persistence_result=None,
            evaluation_version=evaluation_version,
            outcome_rows=() if carry_outcome_rows else None,
        )

    evaluator_options: dict[str, object] = {}
    if reviewed:
        inputs = review_inputs_for(
            review_records,
            prediction_ids=tuple(row["prediction_id"] for row in read_result.predictions),
            evaluation_date=evaluation_date,
        )
        quality_findings_by_prediction = inputs.quality_findings_by_prediction
        human_calibrations_by_prediction = inputs.human_calibrations_by_prediction
        evaluator_options = {"reviewed_prediction_ids": inputs.reviewed_prediction_ids}
    evaluation = build_due_outcome_rows(
        read_result=read_result,
        evaluated_at=evaluated_at,
        evaluation_run_id=evaluation_run_id,
        rules=outcome_rules,
        quality_findings_by_prediction=quality_findings_by_prediction,
        human_calibrations_by_prediction=human_calibrations_by_prediction,
        **evaluator_options,
    )
    if len(evaluation.rows) != due_count:
        raise ValueError("evaluator row count does not match due predictions")
    persistence_result = _persist_outcome_rows(
        client=client,
        rows=evaluation.rows,
        persistence_rule_bundle=persistence_rule_bundle,
        dry_run=dry_run,
    )
    return OutcomeJobResult(
        evaluation_run_id=evaluation_run_id,
        dry_run=dry_run,
        due_prediction_count=due_count,
        outcome_row_count=len(evaluation.rows),
        state_counts=evaluation.state_counts,
        missing_prediction_ids=evaluation.missing_prediction_ids,
        persistence_result=persistence_result,
        evaluation_version=evaluation_version,
        unreviewed_prediction_ids=evaluation.unreviewed_prediction_ids,
        outcome_rows=tuple(evaluation.rows) if carry_outcome_rows else None,
    )


def _persist_outcome_rows(
    *, client: object, rows: object, persistence_rule_bundle: object, dry_run: bool
) -> object:
    batch = persistence.OpenIntelligenceRowBatch(
        candidates=(),
        evidence=(),
        membership=(),
        lineage=(),
        predictions=(),
        outcomes=rows,
    )
    persistence_client = (
        persistence.PersistenceTarget(
            project=TARGET_PROJECT,
            dataset=TARGET_DATASET,
            location=TARGET_LOCATION,
            writer_identity=persistence.TARGET_WRITER_IDENTITY,
        )
        if dry_run
        else client
    )
    return persistence.persist_open_intelligence_rows(
        project=TARGET_PROJECT,
        dataset=TARGET_DATASET,
        client=persistence_client,
        batch=batch,
        rule_bundle=persistence_rule_bundle,
        dry_run=dry_run,
    )


def _persist_verified_result(
    *,
    client: object,
    verified_result: object,
    evaluation_run_id: str,
    evaluation_version: str,
    persistence_rule_bundle: object,
    dry_run: bool,
    carry_outcome_rows: bool,
) -> OutcomeJobResult:
    """Write the rows of this run's own carried dry run, and nothing else."""
    if (
        not isinstance(verified_result, OutcomeJobResult)
        or dry_run
        or not carry_outcome_rows
        or verified_result.dry_run is not True
        or verified_result.evaluation_run_id != evaluation_run_id
        or verified_result.evaluation_version != evaluation_version
        or verified_result.outcome_rows is None
    ):
        raise ValueError("verified result must be this run's carried dry run")
    rows = verified_result.outcome_rows
    for row in rows:
        run_id = row.get("run_id")
        if not isinstance(run_id, str) or run_id.strip() != evaluation_run_id:
            raise ValueError("verified result rows belong to another run")
    persistence_result = (
        _persist_outcome_rows(
            client=client,
            rows=rows,
            persistence_rule_bundle=persistence_rule_bundle,
            dry_run=False,
        )
        if rows
        else None
    )
    return OutcomeJobResult(
        evaluation_run_id=evaluation_run_id,
        dry_run=False,
        due_prediction_count=verified_result.due_prediction_count,
        outcome_row_count=verified_result.outcome_row_count,
        state_counts=verified_result.state_counts,
        missing_prediction_ids=verified_result.missing_prediction_ids,
        persistence_result=persistence_result,
        evaluation_version=evaluation_version,
        unreviewed_prediction_ids=verified_result.unreviewed_prediction_ids,
        outcome_rows=rows,
    )
