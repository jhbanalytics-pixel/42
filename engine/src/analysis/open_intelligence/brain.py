"""Public dark, zero-write Intelligence Brain orchestration."""

from __future__ import annotations

from src.analysis.open_intelligence.brain_analyst import _run_analyst, _validate_analyst
from src.analysis.open_intelligence.brain_authority import (
    UNAVAILABLE_BRAIN_VALUE,
    _is_live_runtime,
    _issue_brain_evidence_snapshot,
    _issue_brain_runtime_request,
    _validate_brain_evidence_snapshot,
    _validate_brain_runtime_request,
)
from src.analysis.open_intelligence.brain_contract import (
    BRAIN_CONTRACT_VERSION,
    DARK_DEPTH_POLICY_DIGEST,
    BrainAdmission,
    IntelligenceBrainIntent,
    IntelligenceBrainResult,
    canonical_digest,
)
from src.analysis.open_intelligence.brain_editor import _run_editor, _validate_editor
from src.analysis.open_intelligence.brain_graph import (
    build_intelligence_evidence_graph,
    validate_intelligence_evidence_graph,
)
from src.analysis.open_intelligence.brain_historian import _run_historian, _validate_historian
from src.analysis.open_intelligence.brain_observer import _run_observer, _validate_observer
from src.analysis.open_intelligence.brain_semantics import (
    evaluate_live_intelligence_journey as _evaluate_live_intelligence_journey,
)
from src.analysis.open_intelligence.brain_skeptic import _run_skeptic, _validate_skeptic
from src.analysis.open_intelligence.brain_strategist import _run_strategist, _validate_strategist

_RESULT_CAPABILITY = object()
_RESULT_AUTHORITIES: dict[int, tuple[object, ...]] = {}


def _aggregate_admission(roles, *, live_mode: bool = False) -> BrainAdmission:
    if live_mode:
        return UNAVAILABLE_BRAIN_VALUE.admission
    missing_work = tuple(
        sorted({item for role in roles for item in getattr(role, "missing_work", ())})
    )
    approval_state = "pending" if "human approval" in missing_work else "not_required"
    return BrainAdmission(
        "available",
        "validated",
        approval_state,
        approval_state == "not_required",
        missing_work,
        None,
    )


def run_intelligence_brain(intent: IntelligenceBrainIntent) -> IntelligenceBrainResult:
    runtime = _issue_brain_runtime_request(intent)
    snapshot = _issue_brain_evidence_snapshot(runtime)
    return _run_intelligence_brain_from_authority(runtime, snapshot)


def evaluate_live_intelligence_journey(value: object) -> dict[str, object]:
    return _evaluate_live_intelligence_journey(value)


def history_requirement_resolver(runtime, snapshot):
    """Build the resolver the question snapshot builder calls for history requirements.

    It closes over the brain runtime, the evidence snapshot and one observer
    run, the same context the historian gets inside the brain, and serves
    every history requirement through the historian without a model call.
    """
    from src.analysis.open_intelligence.general_question_plan import resolve_history_requirements

    _validate_brain_runtime_request(runtime)
    _validate_brain_evidence_snapshot(runtime, snapshot)
    observer = _run_observer(runtime, snapshot)

    def resolve(plan):
        return resolve_history_requirements(
            plan, runtime=runtime, snapshot=snapshot, observer=observer
        )

    return resolve


def _run_intelligence_brain_from_authority(runtime, snapshot) -> IntelligenceBrainResult:
    _validate_brain_runtime_request(runtime)
    _validate_brain_evidence_snapshot(runtime, snapshot)
    live_mode = _is_live_runtime(runtime)
    observer = _run_observer(runtime, snapshot)
    historian = _run_historian(runtime, snapshot, observer)
    analyst = _run_analyst(runtime, snapshot, observer, historian)
    skeptic = _run_skeptic(runtime, snapshot, observer, historian, analyst)
    strategist = _run_strategist(runtime, snapshot, observer, historian, analyst, skeptic)
    editor = _run_editor(runtime, snapshot, observer, historian, analyst, skeptic, strategist)
    roles = (observer, historian, analyst, skeptic, strategist, editor)
    graph = build_intelligence_evidence_graph(snapshot, roles)
    admission = _aggregate_admission(roles, live_mode=live_mode)
    values = {
        "contract_version": BRAIN_CONTRACT_VERSION,
        "run_id": runtime.run_id,
        "signal_id": runtime.signal_id,
        "signal_date": runtime.signal_date.isoformat(),
        "client_scope_id": runtime.client_scope_id,
        "market_scope": runtime.market_scope,
        "brand_config_id": runtime.brand_config_id,
        "audience_lens_ids": runtime.audience_lens_ids,
        "theme_id": runtime.theme_id,
        "research_depth": runtime.research_depth,
        "depth_policy_id": runtime.depth_policy_id,
        "depth_policy_digest": runtime.depth_policy_digest,
        "as_of": runtime.as_of,
        "snapshot_id": snapshot.snapshot_id,
        "observer": observer,
        "historian": historian,
        "analyst": analyst,
        "skeptic": skeptic,
        "strategist": strategist,
        "editor": editor,
        "evidence_graph": graph,
        "evidence_state": snapshot.evidence_state,
        "overall_admission": admission,
        "limitations": (
            ("semantic_method_not_authorized",) if live_mode else ("dark frozen kernel",)
        ),
        "missing_work": admission.reason_codes,
        "model_usage_receipt_ids": (),
    }
    result = IntelligenceBrainResult(**values, result_digest=canonical_digest(values))
    _RESULT_AUTHORITIES[id(result)] = (
        _RESULT_CAPABILITY,
        result,
        canonical_digest(result),
        runtime,
        snapshot,
        roles,
        graph,
    )
    validate_intelligence_brain_result(result)
    return result


def validate_intelligence_brain_result(value: IntelligenceBrainResult) -> None:
    authority = _RESULT_AUTHORITIES.get(id(value))
    if (
        authority is None
        or authority[0] is not _RESULT_CAPABILITY
        or authority[1] is not value
        or authority[2] != canonical_digest(value)
    ):
        raise ValueError("brain result authority is invalid")
    _, _, _, runtime, snapshot, roles, graph = authority
    _validate_brain_runtime_request(runtime)
    _validate_brain_evidence_snapshot(runtime, snapshot)
    observer, historian, analyst, skeptic, strategist, editor = roles
    _validate_observer(runtime, snapshot, observer)
    _validate_historian(runtime, snapshot, observer, historian)
    _validate_analyst(runtime, snapshot, observer, historian, analyst)
    _validate_skeptic(runtime, snapshot, observer, historian, analyst, skeptic)
    _validate_strategist(runtime, snapshot, observer, historian, analyst, skeptic, strategist)
    _validate_editor(runtime, snapshot, observer, historian, analyst, skeptic, strategist, editor)
    validate_intelligence_evidence_graph(graph, snapshot, roles)
    if value.depth_policy_digest != DARK_DEPTH_POLICY_DIGEST or value.model_usage_receipt_ids:
        raise ValueError("brain result authority is invalid")


__all__ = (
    "IntelligenceBrainIntent",
    "IntelligenceBrainResult",
    "run_intelligence_brain",
    "validate_intelligence_brain_result",
)
