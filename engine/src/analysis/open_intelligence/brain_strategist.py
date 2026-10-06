"""Strategist role and deterministic specificity gates."""

from __future__ import annotations

from dataclasses import dataclass

from src.analysis.open_intelligence.brain_authority import (
    UNAVAILABLE_BRAIN_VALUE,
    _is_live_runtime,
)
from src.analysis.open_intelligence.brain_contract import BrainAdmission, canonical_digest
from src.analysis.open_intelligence.brain_observer import _bind_role, _RoleBase
from src.analysis.open_intelligence.brain_skeptic import _validate_skeptic

SPECIFICITY_CODES = (
    "signal_specific_role",
    "market_specific_fit",
    "bounded_experiment",
    "measurable_scale_condition",
    "explicit_avoidance",
    "permission_risk",
    "timing_risk",
)


@dataclass(frozen=True, slots=True)
class CulturalTensionRecord:
    tension_id: str
    statement: str
    first_behavior_observation_ids: tuple[str, ...]
    second_behavior_observation_ids: tuple[str, ...]
    supporting_evidence_ids: tuple[str, ...]
    challenging_evidence_ids: tuple[str, ...]
    market_scope: tuple[str, ...]
    derivation_method_id: str
    confidence_value: float
    confidence_method_id: str
    limitations: tuple[str, ...]
    admission: BrainAdmission


@dataclass(frozen=True, slots=True)
class StrategicOpportunityRecord:
    opportunity_id: str
    statement: str
    approved_interpretation_claim_id: str
    tension_id: str
    brand_role: str
    why_role_fits: str
    what_to_avoid: str
    smallest_useful_experiment: str
    scale_evidence: str
    risk_too_early: str
    risk_too_late: str
    risk_without_permission: str
    supporting_evidence_ids: tuple[str, ...]
    challenging_evidence_ids: tuple[str, ...]
    market_scope: tuple[str, ...]
    limitations: tuple[str, ...]
    specificity_reason_codes: tuple[str, ...]
    admission: BrainAdmission


@dataclass(frozen=True, slots=True)
class AntiSlopResult:
    passed: bool
    reason_codes: tuple[str, ...]
    claim_ids: tuple[str, ...]
    clause_ids: tuple[str, ...]
    phrase_policy_version: str
    result_digest: str


@dataclass(frozen=True, slots=True)
class StrategistResult(_RoleBase):
    role_version: str
    signal_id: str
    snapshot_id: str
    cultural_tension: CulturalTensionRecord
    strategic_implication: str
    opportunity: StrategicOpportunityRecord
    possible_response: str
    what_would_change: tuple[object, ...]
    limitations: tuple[str, ...]
    missing_work: tuple[str, ...]
    result_digest: str


def _validate_tension(first, second):
    left, right = tuple(first), tuple(second)
    if not left or not right or set(left).intersection(right):
        raise ValueError("tension requires two disjoint behaviors")
    return left, right


def validate_brain_specificity(value: StrategicOpportunityRecord) -> AntiSlopResult:
    checks = {
        "signal_specific_role": value.brand_role,
        "market_specific_fit": value.why_role_fits and value.market_scope,
        "bounded_experiment": value.smallest_useful_experiment,
        "measurable_scale_condition": value.scale_evidence,
        "explicit_avoidance": value.what_to_avoid,
        "permission_risk": value.risk_without_permission,
        "timing_risk": value.risk_too_early and value.risk_too_late,
    }
    missing = tuple(sorted(code for code, present in checks.items() if not present))
    generic = any(
        phrase in value.statement.lower()
        for phrase in (
            "join the conversation",
            "authentic content",
            "partner with influencers",
            "tap into culture",
        )
    )
    reasons = list(missing)
    if generic:
        reasons.append("generic_phrase_family")
    if tuple(value.specificity_reason_codes) != SPECIFICITY_CODES:
        reasons.append("specificity_reason_codes")
    values = {
        "passed": not missing and "specificity_reason_codes" not in reasons,
        "reason_codes": tuple(sorted(set(reasons))),
        "claim_ids": (value.approved_interpretation_claim_id,),
        "clause_ids": (),
        "phrase_policy_version": "brain_generic_phrase_policy_v1",
    }
    return AntiSlopResult(**values, result_digest=canonical_digest(values))


def _run_strategist(runtime, snapshot, observer, historian, analyst, skeptic) -> StrategistResult:
    _validate_skeptic(runtime, snapshot, observer, historian, analyst, skeptic)
    if _is_live_runtime(runtime):
        values = {
            "role_version": "strategist_live_v1",
            "signal_id": snapshot.signal_id,
            "snapshot_id": snapshot.snapshot_id,
            "cultural_tension": UNAVAILABLE_BRAIN_VALUE,
            "strategic_implication": UNAVAILABLE_BRAIN_VALUE,
            "opportunity": UNAVAILABLE_BRAIN_VALUE,
            "possible_response": UNAVAILABLE_BRAIN_VALUE,
            "what_would_change": UNAVAILABLE_BRAIN_VALUE,
            "limitations": ("semantic_method_not_authorized",),
            "missing_work": ("semantic_method_not_authorized",),
        }
        result = StrategistResult(**values, result_digest=canonical_digest(values))
        return _bind_role(
            result, "strategist", runtime, snapshot, (observer, historian, analyst, skeptic)
        )
    if skeptic.verdict in {"insufficient", "contradictory"}:
        raise ValueError("strategy is unavailable")
    observed = tuple(item.observation_id for item in observer.observations if item.evidence_ids)
    first, second = _validate_tension(observed[:2], observed[2:4])
    admission = BrainAdmission("available", "validated", "not_required", True, (), None)
    tension = CulturalTensionRecord(
        "tension_" + canonical_digest((snapshot.snapshot_id, first, second)),
        "Visible participation is spreading while local permission remains uncertain.",
        first,
        second,
        tuple(snapshot.evidence_ids),
        (),
        tuple(snapshot.market_ids),
        "competing_behavior_v1",
        0.65,
        "tension_confidence_v1",
        ("frozen tension",),
        admission,
    )
    opportunity = StrategicOpportunityRecord(
        "opp_" + canonical_digest((snapshot.snapshot_id, "opportunity")),
        "Test a locally permitted participation role in ZA before transfer.",
        analyst.why_now.claim_id,
        tension.tension_id,
        "enable local participation",
        "the role fits the measured participation movement in ZA",
        "avoid claiming prevalence",
        "run one consented workshop in ZA",
        "scale only if independent participation rises",
        "the behavior may still be noise",
        "another actor may own the role",
        "local participants may not grant permission",
        tuple(snapshot.evidence_ids),
        (),
        tuple(snapshot.market_ids),
        ("human approval remains unavailable",),
        SPECIFICITY_CODES,
        admission,
    )
    if not validate_brain_specificity(opportunity).passed:
        raise ValueError("opportunity specificity is unavailable")
    values = {
        "role_version": "strategist_v1",
        "signal_id": snapshot.signal_id,
        "snapshot_id": snapshot.snapshot_id,
        "cultural_tension": tension,
        "strategic_implication": "Permission is part of the strategic value.",
        "opportunity": opportunity,
        "possible_response": opportunity.smallest_useful_experiment,
        "what_would_change": skeptic.what_would_change,
        "limitations": ("dark frozen kernel",),
        "missing_work": ("human approval",),
    }
    result = StrategistResult(**values, result_digest=canonical_digest(values))
    return _bind_role(
        result, "strategist", runtime, snapshot, (observer, historian, analyst, skeptic)
    )


def _validate_strategist(runtime, snapshot, observer, historian, analyst, skeptic, value):
    from src.analysis.open_intelligence.brain_observer import _validate_role

    _validate_skeptic(runtime, snapshot, observer, historian, analyst, skeptic)
    if _is_live_runtime(runtime):
        if any(
            getattr(value, field) != UNAVAILABLE_BRAIN_VALUE
            for field in (
                "cultural_tension",
                "strategic_implication",
                "opportunity",
                "possible_response",
                "what_would_change",
            )
        ):
            raise ValueError("live strategist authority is invalid")
        return _validate_role(
            value, "strategist", runtime, snapshot, (observer, historian, analyst, skeptic)
        )
    _validate_tension(
        value.cultural_tension.first_behavior_observation_ids,
        value.cultural_tension.second_behavior_observation_ids,
    )
    if not validate_brain_specificity(value.opportunity).passed:
        raise ValueError("opportunity specificity is unavailable")
    return _validate_role(
        value, "strategist", runtime, snapshot, (observer, historian, analyst, skeptic)
    )


__all__ = []
