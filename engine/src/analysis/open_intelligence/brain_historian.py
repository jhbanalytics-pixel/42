"""Historian role over frozen source-owned evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from src.analysis.open_intelligence.brain_authority import (
    UNAVAILABLE_BRAIN_VALUE,
    _is_live_runtime,
    _snapshot_outcomes,
    _snapshot_payload,
    _validate_brain_evidence_snapshot,
)
from src.analysis.open_intelligence.brain_contract import BrainAdmission, canonical_digest
from src.analysis.open_intelligence.brain_observer import _bind_role, _RoleBase, _validate_observer


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class HistoricalAnalogueRecord:
    historical_object_id: str
    relationship_state: str
    relevance_reasons: tuple[str, ...]
    comparable_conditions: tuple[str, ...]
    material_differences: tuple[str, ...]
    what_happened_next: tuple[str, ...]
    transferable_lessons: tuple[str, ...]
    nontransferable_lessons: tuple[str, ...]
    source_window_start: datetime
    source_window_end: datetime
    source_created_at: datetime
    source_first_observed_at: datetime
    source_last_observed_at: datetime
    selection_cutoff: datetime
    evidence_ids: tuple[str, ...]
    confidence_value: float
    confidence_method_id: str
    limitations: tuple[str, ...]
    admission: BrainAdmission


@dataclass(frozen=True, slots=True)
class HistorianResult(_RoleBase):
    role_version: str
    signal_id: str
    snapshot_id: str
    analogue_candidates: tuple[HistoricalAnalogueRecord, ...]
    selected_analogue: HistoricalAnalogueRecord
    recurrence: object
    diffusion: object
    prior_outcomes: tuple[str, ...]
    source_behavior_changes: tuple[str, ...]
    limitations: tuple[str, ...]
    missing_work: tuple[str, ...]
    result_digest: str


def _run_historian(runtime, snapshot, observer) -> HistorianResult:
    _validate_brain_evidence_snapshot(runtime, snapshot)
    _validate_observer(runtime, snapshot, observer)
    if _is_live_runtime(runtime):
        values = {
            "role_version": "historian_live_v1",
            "signal_id": snapshot.signal_id,
            "snapshot_id": snapshot.snapshot_id,
            "analogue_candidates": UNAVAILABLE_BRAIN_VALUE,
            "selected_analogue": UNAVAILABLE_BRAIN_VALUE,
            "recurrence": UNAVAILABLE_BRAIN_VALUE,
            "diffusion": UNAVAILABLE_BRAIN_VALUE,
            "prior_outcomes": UNAVAILABLE_BRAIN_VALUE,
            "source_behavior_changes": UNAVAILABLE_BRAIN_VALUE,
            "limitations": ("historical_object_source_not_authorized",),
            "missing_work": ("historical_object_source_not_authorized",),
        }
        result = HistorianResult(**values, result_digest=canonical_digest(values))
        return _bind_role(result, "historian", runtime, snapshot, (observer,))
    payload = _snapshot_payload(snapshot)
    outcomes = _snapshot_outcomes(snapshot)
    item = payload["historical"]
    if item["historical_object_id"] not in snapshot.historical_object_ids:
        raise ValueError("Historical analogue unavailable")
    created = _timestamp(item["source_created_at"])
    first = _timestamp(item["source_first_observed_at"])
    last = _timestamp(item["source_last_observed_at"])
    cutoff = _timestamp(item["selection_cutoff"])
    if any(value >= snapshot.as_of for value in (created, first, last, cutoff)) or any(
        value > cutoff for value in (created, first, last)
    ):
        raise ValueError("Historical future leak")
    admission = BrainAdmission("available", "validated", "not_required", True, (), None)
    analogue = HistoricalAnalogueRecord(
        item["historical_object_id"],
        "analogue",
        ("trajectory similarity",),
        ("two independent source families",),
        ("market context differs",),
        ("participation stabilized",),
        ("test the participation mechanic",),
        ("do not transfer audience prevalence",),
        created,
        last,
        created,
        first,
        last,
        cutoff,
        tuple(snapshot.evidence_ids),
        0.7,
        "historical_similarity_v1",
        ("frozen precedent",),
        admission,
    )
    values = {
        "role_version": "historian_v1",
        "signal_id": snapshot.signal_id,
        "snapshot_id": snapshot.snapshot_id,
        "analogue_candidates": (analogue,),
        "selected_analogue": analogue,
        "recurrence": {"relationship_state": "recurrence", "display_eligible": False},
        "diffusion": {"relationship_state": "diffusion_shadow", "display_eligible": False},
        "prior_outcomes": tuple(outcome.outcome_id for outcome in outcomes),
        "source_behavior_changes": ("forum and video moved independently",),
        "limitations": ("frozen history only",),
        "missing_work": (),
    }
    result = HistorianResult(**values, result_digest=canonical_digest(values))
    return _bind_role(result, "historian", runtime, snapshot, (observer,))


HISTORY_REQUIREMENT_KIND = "history"
_NO_COMPARABLE_HISTORY = "Historical analogue unavailable"


@dataclass(frozen=True, slots=True)
class InsufficientHistory:
    """Typed answer for a history requirement that no retained history can serve."""

    requirement_id: str
    signal_id: str
    as_of: datetime
    reason: str
    state: str = "insufficient_history"


@dataclass(frozen=True, slots=True)
class HistoryRequirementAnswer:
    requirement_id: str
    signal_id: str
    as_of: datetime
    analogue: HistoricalAnalogueRecord
    provenance: Mapping[str, object]
    historian: HistorianResult
    state: str = "analogue"


def _history_requirement_id(requirement: object) -> str:
    if not isinstance(requirement, Mapping) or requirement.get("kind") != HISTORY_REQUIREMENT_KIND:
        raise ValueError("history_requirement_invalid")
    identity = requirement.get("requirement_id")
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("history_requirement_invalid")
    return identity


def answer_history_requirement(
    requirement: object, *, runtime, snapshot, observer
) -> HistoryRequirementAnswer | InsufficientHistory:
    """Serve one history requirement from the historian over retained evidence.

    No model call happens here. A known earlier source yields the historian's
    analogue with its exact provenance, a future source is refused by the
    historian's leak check, and absent comparable history returns a typed
    InsufficientHistory rather than an empty list.
    """
    requirement_id = _history_requirement_id(requirement)
    try:
        result = _run_historian(runtime, snapshot, observer)
    except ValueError as error:
        if str(error) != _NO_COMPARABLE_HISTORY:
            raise
        return InsufficientHistory(
            requirement_id, snapshot.signal_id, snapshot.as_of, "no_comparable_history"
        )
    analogue = result.selected_analogue
    if not isinstance(analogue, HistoricalAnalogueRecord):
        return InsufficientHistory(
            requirement_id,
            snapshot.signal_id,
            snapshot.as_of,
            "historical_object_source_not_authorized",
        )
    provenance = MappingProxyType(
        {
            "historical_object_id": analogue.historical_object_id,
            "source_created_at": analogue.source_created_at.isoformat(),
            "source_first_observed_at": analogue.source_first_observed_at.isoformat(),
            "source_last_observed_at": analogue.source_last_observed_at.isoformat(),
            "selection_cutoff": analogue.selection_cutoff.isoformat(),
            "evidence_ids": tuple(analogue.evidence_ids),
            "signal_id": result.signal_id,
            "snapshot_id": result.snapshot_id,
            "result_digest": result.result_digest,
        }
    )
    return HistoryRequirementAnswer(
        requirement_id, snapshot.signal_id, snapshot.as_of, analogue, provenance, result
    )


def _validate_historian(runtime, snapshot, observer, value):
    from src.analysis.open_intelligence.brain_observer import _validate_role

    if _is_live_runtime(runtime):
        if any(
            getattr(value, field) != UNAVAILABLE_BRAIN_VALUE
            for field in (
                "analogue_candidates",
                "selected_analogue",
                "recurrence",
                "diffusion",
                "prior_outcomes",
                "source_behavior_changes",
            )
        ):
            raise ValueError("live historian authority is invalid")
        return _validate_role(value, "historian", runtime, snapshot, (observer,))
    _validate_observer(runtime, snapshot, observer)
    return _validate_role(value, "historian", runtime, snapshot, (observer,))


__all__ = []
