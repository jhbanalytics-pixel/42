"""Deterministic Skeptic role and symmetric rival comparison."""

from __future__ import annotations

from dataclasses import dataclass

from src.analysis.open_intelligence.brain_analyst import _validate_analyst
from src.analysis.open_intelligence.brain_authority import (
    UNAVAILABLE_BRAIN_VALUE,
    _is_live_runtime,
)
from src.analysis.open_intelligence.brain_contract import BrainAdmission, canonical_digest
from src.analysis.open_intelligence.brain_observer import _bind_role, _RoleBase

FIT_DIMENSIONS = ("evidence_support", "source_independence", "market_fit", "historical_fit")


@dataclass(frozen=True, slots=True)
class HypothesisRecord:
    hypothesis_id: str
    hypothesis_role: str
    statement: str
    dependent_observation_ids: tuple[str, ...]
    supporting_evidence_ids: tuple[str, ...]
    challenging_evidence_ids: tuple[str, ...]
    fit_dimensions: tuple[str, ...]
    fit_value: float
    fit_method_id: str
    limitations: tuple[str, ...]
    admission: BrainAdmission


@dataclass(frozen=True, slots=True)
class HypothesisComparisonRecord:
    comparison_id: str
    leading_hypothesis_id: str
    rival_hypothesis_ids: tuple[str, ...]
    ordered_evidence_universe_ids: tuple[str, ...]
    ordered_fit_dimension_ids: tuple[str, ...]
    fit_method_id: str
    missing_value_method_id: str
    leading_fit_value: float
    rival_fit_values: tuple[float, ...]
    verdict: str
    limitations: tuple[str, ...]
    admission: BrainAdmission


@dataclass(frozen=True, slots=True)
class WhatWouldChangeRecord:
    change_id: str
    direction: str
    required_observation: str
    measurement_method_id: str
    decision_effect: str
    window: str
    market: str


@dataclass(frozen=True, slots=True)
class SkepticResult(_RoleBase):
    role_version: str
    signal_id: str
    snapshot_id: str
    rival_hypotheses: tuple[HypothesisRecord, ...]
    hypothesis_comparison: HypothesisComparisonRecord
    contradictions: tuple[str, ...]
    source_dependence: tuple[str, ...]
    geographic_ambiguity: tuple[str, ...]
    demographic_overreach: tuple[str, ...]
    staleness: tuple[str, ...]
    selection_bias: tuple[str, ...]
    what_would_change: tuple[WhatWouldChangeRecord, ...]
    verdict: str
    limitations: tuple[str, ...]
    missing_work: tuple[str, ...]
    result_digest: str


def _require_rivals(values):
    items = tuple(values)
    if not items:
        raise ValueError("rival is required")
    return items


def _validate_hypothesis_comparison(value, expected_evidence):
    if (
        tuple(value.ordered_evidence_universe_ids) != tuple(expected_evidence)
        or value.ordered_fit_dimension_ids != FIT_DIMENSIONS
        or value.fit_method_id != "hypothesis_fit_v1"
        or value.missing_value_method_id != "missing_unavailable_v1"
        or not value.rival_hypothesis_ids
        or len(value.rival_hypothesis_ids) != len(value.rival_fit_values)
    ):
        raise ValueError("hypothesis comparison is invalid")
    return value


def _run_skeptic(runtime, snapshot, observer, historian, analyst) -> SkepticResult:
    _validate_analyst(runtime, snapshot, observer, historian, analyst)
    if _is_live_runtime(runtime):
        values = {
            "role_version": "skeptic_live_v1",
            "signal_id": snapshot.signal_id,
            "snapshot_id": snapshot.snapshot_id,
            "rival_hypotheses": UNAVAILABLE_BRAIN_VALUE,
            "hypothesis_comparison": UNAVAILABLE_BRAIN_VALUE,
            "contradictions": UNAVAILABLE_BRAIN_VALUE,
            "source_dependence": UNAVAILABLE_BRAIN_VALUE,
            "geographic_ambiguity": UNAVAILABLE_BRAIN_VALUE,
            "demographic_overreach": UNAVAILABLE_BRAIN_VALUE,
            "staleness": UNAVAILABLE_BRAIN_VALUE,
            "selection_bias": UNAVAILABLE_BRAIN_VALUE,
            "what_would_change": UNAVAILABLE_BRAIN_VALUE,
            "verdict": UNAVAILABLE_BRAIN_VALUE,
            "limitations": ("semantic_method_not_authorized",),
            "missing_work": ("semantic_method_not_authorized",),
        }
        result = SkepticResult(**values, result_digest=canonical_digest(values))
        return _bind_role(result, "skeptic", runtime, snapshot, (observer, historian, analyst))
    admission = BrainAdmission("available", "validated", "not_required", True, (), None)
    observation_ids = tuple(
        item.observation_id for item in observer.observations if item.evidence_ids
    )
    leading = HypothesisRecord(
        "hyp_leading",
        "leading",
        analyst.leading_hypothesis["statement"],
        observation_ids,
        tuple(snapshot.evidence_ids),
        (),
        FIT_DIMENSIONS,
        0.75,
        "hypothesis_fit_v1",
        ("association only",),
        admission,
    )
    rival = HypothesisRecord(
        "hyp_rival",
        "rival",
        "Coordinated promotion explains the apparent spread.",
        observation_ids,
        (snapshot.evidence_ids[0],),
        (snapshot.evidence_ids[1],),
        FIT_DIMENSIONS,
        0.42,
        "hypothesis_fit_v1",
        ("frozen rival",),
        admission,
    )
    rivals = _require_rivals((rival,))
    comparison = HypothesisComparisonRecord(
        "hcmp_" + canonical_digest((snapshot.snapshot_id, "comparison")),
        leading.hypothesis_id,
        tuple(item.hypothesis_id for item in rivals),
        tuple(snapshot.evidence_ids),
        FIT_DIMENSIONS,
        "hypothesis_fit_v1",
        "missing_unavailable_v1",
        leading.fit_value,
        tuple(item.fit_value for item in rivals),
        "leading_fits_better",
        (),
        admission,
    )
    _validate_hypothesis_comparison(comparison, tuple(snapshot.evidence_ids))
    changes = tuple(
        WhatWouldChangeRecord(
            f"change_{direction}",
            direction,
            f"Observe evidence that would {direction} the fit.",
            "hypothesis_fit_v1",
            direction,
            "next closed window",
            snapshot.market_ids[0],
        )
        for direction in ("strengthen", "weaken", "reverse")
    )
    values = {
        "role_version": "skeptic_v1",
        "signal_id": snapshot.signal_id,
        "snapshot_id": snapshot.snapshot_id,
        "rival_hypotheses": rivals,
        "hypothesis_comparison": comparison,
        "contradictions": ("one source challenges coordinated promotion",),
        "source_dependence": (),
        "geographic_ambiguity": ("NG movement remains thin",),
        "demographic_overreach": ("no audience inference",),
        "staleness": (),
        "selection_bias": ("frozen sample",),
        "what_would_change": changes,
        "verdict": comparison.verdict,
        "limitations": ("frozen comparison",),
        "missing_work": (),
    }
    result = SkepticResult(**values, result_digest=canonical_digest(values))
    return _bind_role(result, "skeptic", runtime, snapshot, (observer, historian, analyst))


def _validate_skeptic(runtime, snapshot, observer, historian, analyst, value):
    from src.analysis.open_intelligence.brain_observer import _validate_role

    _validate_analyst(runtime, snapshot, observer, historian, analyst)
    if _is_live_runtime(runtime):
        if any(
            getattr(value, field) != UNAVAILABLE_BRAIN_VALUE
            for field in (
                "rival_hypotheses",
                "hypothesis_comparison",
                "contradictions",
                "source_dependence",
                "geographic_ambiguity",
                "demographic_overreach",
                "staleness",
                "selection_bias",
                "what_would_change",
                "verdict",
            )
        ):
            raise ValueError("live skeptic authority is invalid")
        return _validate_role(value, "skeptic", runtime, snapshot, (observer, historian, analyst))
    _validate_hypothesis_comparison(value.hypothesis_comparison, tuple(snapshot.evidence_ids))
    _require_rivals(value.rival_hypotheses)
    return _validate_role(value, "skeptic", runtime, snapshot, (observer, historian, analyst))


__all__ = []
