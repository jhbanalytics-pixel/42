"""Read-only Evidence Room projections over one resolved investigation frame."""

from __future__ import annotations

from .workspace_scope import ResolvedWorkspaceScope


def _identity(scope: ResolvedWorkspaceScope) -> dict[str, object]:
    return {
        "investigation_id": scope.investigation_id,
        **scope.scope_payload,
    }


def status_resource(scope: ResolvedWorkspaceScope) -> dict[str, object]:
    response = scope.response
    plan = dict(response["research_plan"])
    token_values = tuple(plan.get("token_budget") or {})
    return {
        **_identity(scope),
        "research_role_id": response.get("research_role_id"),
        "research_role_version": response.get("research_role_version"),
        "status": "plan_ready",
        "research_plan": plan,
        "budget_state": {
            "credit_ceiling": plan.get("credit_ceiling"),
            "credits_reserved": 0,
            "credits_used": 0,
            "token_ceiling": sum((plan.get("token_budget") or {}).values()) if token_values else None,
            "tokens_reserved": None,
            "tokens_used": None,
            "state": "not_configured" if not token_values else "available",
        },
        "source_calls": [],
        "completed_work": [],
        "missing_work": list(plan.get("known_gaps") or []),
        "claim_ids": [],
        "decision_id": None,
        "artifact_ids": [],
        "created_at": response["created_at"],
        "updated_at": response["updated_at"],
    }


def claims_resource(scope: ResolvedWorkspaceScope) -> dict[str, object]:
    return {
        **_identity(scope),
        "artifact_version": "unsealed",
        "claims": [],
        "updated_at": scope.response["updated_at"],
    }


def decision_resource(scope: ResolvedWorkspaceScope) -> dict[str, object]:
    return {
        **_identity(scope),
        "decision": None,
        "updated_at": scope.response["updated_at"],
    }
