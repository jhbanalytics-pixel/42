"""Deterministic Editor role and citation validation."""

from __future__ import annotations

from dataclasses import dataclass

from src.analysis.open_intelligence.brain_authority import (
    UNAVAILABLE_BRAIN_VALUE,
    _is_live_runtime,
)
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.brain_observer import _bind_role, _RoleBase
from src.analysis.open_intelligence.brain_strategist import _validate_strategist


@dataclass(frozen=True, slots=True)
class CitationMapRecord:
    clause_id: str
    clause_text_digest: str
    claim_id: str
    claim_digest: str
    required_evidence_ids: tuple[str, ...]
    cited_evidence_ids: tuple[str, ...]
    citation_labels: tuple[str, ...]
    citation_precision_state: str
    citation_completeness_state: str


@dataclass(frozen=True, slots=True)
class EditorResult(_RoleBase):
    role_version: str
    signal_id: str
    snapshot_id: str
    red_thread: str
    concise_answer: str
    observation_claim_ids: tuple[str, ...]
    interpretation_claim_ids: tuple[str, ...]
    recommendation_claim_ids: tuple[str, ...]
    citation_map: tuple[CitationMapRecord, ...]
    limitations: tuple[str, ...]
    abstentions: tuple[str, ...]
    client_read_eligible: bool
    result_digest: str


def _validate_citation(value: CitationMapRecord) -> CitationMapRecord:
    required, cited = set(value.required_evidence_ids), set(value.cited_evidence_ids)
    if not cited.issubset(required) or value.citation_precision_state != "exact":
        raise ValueError("citation invalid")
    if required != cited or value.citation_completeness_state != "complete":
        raise ValueError("citation incomplete")
    if len(value.citation_labels) != len(cited):
        raise ValueError("citation invalid")
    return value


def _citation(clause, claim_id, evidence_ids):
    evidence = tuple(evidence_ids)
    return CitationMapRecord(
        "clause_" + canonical_digest(clause),
        canonical_digest(clause),
        claim_id,
        canonical_digest((claim_id, clause, evidence)),
        evidence,
        evidence,
        tuple(f"[E{index}]" for index in range(1, len(evidence) + 1)),
        "exact",
        "complete",
    )


def _run_editor(
    runtime, snapshot, observer, historian, analyst, skeptic, strategist
) -> EditorResult:
    _validate_strategist(runtime, snapshot, observer, historian, analyst, skeptic, strategist)
    if _is_live_runtime(runtime):
        values = {
            "role_version": "editor_live_v1",
            "signal_id": snapshot.signal_id,
            "snapshot_id": snapshot.snapshot_id,
            "red_thread": UNAVAILABLE_BRAIN_VALUE,
            "concise_answer": UNAVAILABLE_BRAIN_VALUE,
            "observation_claim_ids": UNAVAILABLE_BRAIN_VALUE,
            "interpretation_claim_ids": UNAVAILABLE_BRAIN_VALUE,
            "recommendation_claim_ids": UNAVAILABLE_BRAIN_VALUE,
            "citation_map": UNAVAILABLE_BRAIN_VALUE,
            "limitations": ("semantic_method_not_authorized",),
            "abstentions": UNAVAILABLE_BRAIN_VALUE,
            "client_read_eligible": False,
        }
        result = EditorResult(**values, result_digest=canonical_digest(values))
        return _bind_role(
            result,
            "editor",
            runtime,
            snapshot,
            (observer, historian, analyst, skeptic, strategist),
        )
    clauses = (
        (analyst.why_now.statement, analyst.why_now.claim_id),
        (strategist.cultural_tension.statement, strategist.cultural_tension.tension_id),
        (strategist.opportunity.statement, strategist.opportunity.opportunity_id),
    )
    citations = tuple(_citation(text, claim, snapshot.evidence_ids) for text, claim in clauses)
    for item in citations:
        _validate_citation(item)
    values = {
        "role_version": "editor_v1",
        "signal_id": snapshot.signal_id,
        "snapshot_id": snapshot.snapshot_id,
        "red_thread": "Independent participation is spreading while permission remains unresolved.",
        "concise_answer": "Test one locally permitted participation format before transfer.",
        "observation_claim_ids": (observer.what_changed_claim_id,),
        "interpretation_claim_ids": (
            analyst.why_now.claim_id,
            strategist.cultural_tension.tension_id,
        ),
        "recommendation_claim_ids": (strategist.opportunity.opportunity_id,),
        "citation_map": citations,
        "limitations": tuple(strategist.limitations),
        "abstentions": ("audience claim unavailable",),
        "client_read_eligible": False,
    }
    result = EditorResult(**values, result_digest=canonical_digest(values))
    return _bind_role(
        result, "editor", runtime, snapshot, (observer, historian, analyst, skeptic, strategist)
    )


def _validate_editor(runtime, snapshot, observer, historian, analyst, skeptic, strategist, value):
    from src.analysis.open_intelligence.brain_observer import _validate_role

    _validate_strategist(runtime, snapshot, observer, historian, analyst, skeptic, strategist)
    if _is_live_runtime(runtime):
        if value.client_read_eligible or any(
            getattr(value, field) != UNAVAILABLE_BRAIN_VALUE
            for field in (
                "red_thread",
                "concise_answer",
                "observation_claim_ids",
                "interpretation_claim_ids",
                "recommendation_claim_ids",
                "citation_map",
                "abstentions",
            )
        ):
            raise ValueError("live editor authority is invalid")
        return _validate_role(
            value,
            "editor",
            runtime,
            snapshot,
            (observer, historian, analyst, skeptic, strategist),
        )
    for item in value.citation_map:
        _validate_citation(item)
    if value.client_read_eligible:
        raise ValueError("client read is unapproved")
    return _validate_role(
        value, "editor", runtime, snapshot, (observer, historian, analyst, skeptic, strategist)
    )


__all__ = []
