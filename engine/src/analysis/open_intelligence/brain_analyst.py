"""Deterministic Analyst and prediction learning role."""

from __future__ import annotations

from dataclasses import dataclass

from src.analysis.open_intelligence.brain_authority import (
    UNAVAILABLE_BRAIN_VALUE,
    _is_live_runtime,
    _snapshot_outcomes,
    _snapshot_payload,
    _snapshot_prediction_window,
)
from src.analysis.open_intelligence.brain_contract import BrainAdmission, canonical_digest
from src.analysis.open_intelligence.brain_historian import _validate_historian
from src.analysis.open_intelligence.brain_observer import _bind_role, _RoleBase, _validate_observer


def _movement_ids(values) -> tuple[str, ...]:
    items = tuple(values)
    if not items:
        raise ValueError("Why Now movement is unavailable")
    if set(items) <= {"engagement_quality"}:
        raise ValueError("volume or engagement alone cannot admit Why Now")
    return items


@dataclass(frozen=True, slots=True)
class WhyNowAssessment:
    claim_id: str
    statement: str
    dependent_observation_ids: tuple[str, ...]
    novelty_measurement_id: str | None
    velocity_measurement_id: str | None
    breadth_measurement_id: str | None
    source_independence_measurement_id: str | None
    persistence_measurement_id: str | None
    creator_spread_measurement_id: str | None
    engagement_quality_measurement_id: str | None
    search_movement_measurement_id: str | None
    geographic_movement_measurement_id: str | None
    historical_rarity_measurement_id: str | None
    method_id: str
    evidence_ids: tuple[str, ...]
    confidence_value: float
    confidence_method_id: str
    limitations: tuple[str, ...]
    admission: BrainAdmission


@dataclass(frozen=True, slots=True)
class PredictionLearningResult:
    prediction_id: str
    expected_direction: str
    expected_window_start: str
    expected_window_end: str
    confidence_value: float
    confidence_method_id: str
    supporting_evidence_ids: tuple[str, ...]
    rival_outcome: str
    invalidation_conditions: tuple[str, ...]
    outcome_state: str
    observed_outcome_ids: tuple[str, ...]
    correct_elements: tuple[str, ...]
    wrong_elements: tuple[str, ...]
    timing_assessment: str
    useful_source_ids: tuple[str, ...]
    misleading_factor_ids: tuple[str, ...]
    failed_assumptions: tuple[str, ...]
    proposed_changes: tuple[str, ...]
    admission: BrainAdmission
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalystResult(_RoleBase):
    role_version: str
    signal_id: str
    snapshot_id: str
    why_now: WhyNowAssessment
    leading_hypothesis: object
    evidence_strength: float
    novelty_assessment: str
    market_comparison: tuple[str, ...]
    causal_status: str
    prediction_learning: PredictionLearningResult
    limitations: tuple[str, ...]
    missing_work: tuple[str, ...]
    result_digest: str


def _run_analyst(runtime, snapshot, observer, historian) -> AnalystResult:
    _validate_observer(runtime, snapshot, observer)
    _validate_historian(runtime, snapshot, observer, historian)
    if _is_live_runtime(runtime):
        values = {
            "role_version": "analyst_live_v1",
            "signal_id": snapshot.signal_id,
            "snapshot_id": snapshot.snapshot_id,
            "why_now": UNAVAILABLE_BRAIN_VALUE,
            "leading_hypothesis": UNAVAILABLE_BRAIN_VALUE,
            "evidence_strength": UNAVAILABLE_BRAIN_VALUE,
            "novelty_assessment": UNAVAILABLE_BRAIN_VALUE,
            "market_comparison": UNAVAILABLE_BRAIN_VALUE,
            "causal_status": UNAVAILABLE_BRAIN_VALUE,
            "prediction_learning": UNAVAILABLE_BRAIN_VALUE,
            "limitations": ("semantic_method_not_authorized",),
            "missing_work": ("semantic_method_not_authorized",),
        }
        result = AnalystResult(**values, result_digest=canonical_digest(values))
        return _bind_role(result, "analyst", runtime, snapshot, (observer, historian))
    movement = _movement_ids(observer.movement_profile)
    payload = _snapshot_payload(snapshot)
    ids = {item["metric_name"]: item["measurement_id"] for item in payload["measurements"]}
    admission = BrainAdmission("available", "validated", "not_required", True, (), None)
    observation_ids = tuple(
        item.observation_id for item in observer.observations if item.observation_type in movement
    )
    why_now = WhyNowAssessment(
        "clm_" + canonical_digest((snapshot.snapshot_id, "why_now")),
        "Multiple measured movement dimensions changed in the bounded window.",
        observation_ids,
        ids.get("novelty"),
        ids.get("velocity"),
        ids.get("breadth"),
        ids.get("source_independence"),
        ids.get("persistence"),
        ids.get("creator_spread"),
        ids.get("engagement_quality"),
        ids.get("search_movement"),
        ids.get("geographic_movement"),
        ids.get("historical_rarity"),
        "why_now_movement_v1",
        tuple(snapshot.evidence_ids),
        0.7,
        "bounded_movement_confidence_v1",
        ("frozen evidence",),
        admission,
    )
    prediction_id, expected_direction, window_start, window_end = _snapshot_prediction_window(
        snapshot
    )
    qualifying_outcomes = tuple(
        outcome
        for outcome in _snapshot_outcomes(snapshot)
        if outcome.prediction_id == prediction_id
        and window_start <= outcome.observed_at.date() <= window_end
    )
    resolved = snapshot.as_of.date() > window_end and bool(qualifying_outcomes)
    prediction = PredictionLearningResult(
        prediction_id,
        expected_direction,
        window_start.isoformat(),
        window_end.isoformat(),
        0.6,
        "prediction_confidence_v1",
        tuple(snapshot.evidence_ids),
        "signal fades",
        ("velocity falls below threshold",),
        "resolved" if resolved else "unresolved",
        tuple(outcome.outcome_id for outcome in qualifying_outcomes) if resolved else (),
        ("direction",) if resolved else (),
        ("timing",) if resolved else (),
        "one day late" if resolved else "not yet assessable",
        tuple(snapshot.source_family_ids),
        ("raw engagement",),
        ("constant growth",) if resolved else (),
        ("review source allocation, do not auto-apply",) if resolved else (),
        admission,
        ("frozen outcome",) if resolved else ("expected window is open or outcome unavailable",),
    )
    values = {
        "role_version": "analyst_v1",
        "signal_id": snapshot.signal_id,
        "snapshot_id": snapshot.snapshot_id,
        "why_now": why_now,
        "leading_hypothesis": {
            "hypothesis_id": "hyp_leading",
            "statement": "Independent participation is spreading.",
            "evidence_ids": tuple(snapshot.evidence_ids),
        },
        "evidence_strength": 0.7,
        "novelty_assessment": "measured novelty",
        "market_comparison": tuple(snapshot.market_ids),
        "causal_status": "causal_claim_unavailable",
        "prediction_learning": prediction,
        "limitations": ("association only",),
        "missing_work": (),
    }
    result = AnalystResult(**values, result_digest=canonical_digest(values))
    return _bind_role(result, "analyst", runtime, snapshot, (observer, historian))


def _validate_analyst(runtime, snapshot, observer, historian, value):
    from src.analysis.open_intelligence.brain_observer import _validate_role

    _validate_historian(runtime, snapshot, observer, historian)
    if _is_live_runtime(runtime) and any(
        getattr(value, field) != UNAVAILABLE_BRAIN_VALUE
        for field in (
            "why_now",
            "leading_hypothesis",
            "evidence_strength",
            "novelty_assessment",
            "market_comparison",
            "causal_status",
            "prediction_learning",
        )
    ):
        raise ValueError("live analyst authority is invalid")
    return _validate_role(value, "analyst", runtime, snapshot, (observer, historian))


__all__ = []
