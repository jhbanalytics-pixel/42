"""Internal Historical workspace adapter with no fixture or data fallback."""

from __future__ import annotations

from . import workspace_scope

MODES = frozenset({"analogue", "recurrence", "replay"})


def unavailable_payload(investigation_id: str, mode: str) -> dict[str, object]:
    return {
        "detail": {
            "code": "workspace_unavailable",
            "message": "Workspace is unavailable.",
        }
    }


def read_historical_workspace(investigation_id: str, mode: str) -> dict[str, object]:
    if mode not in MODES:
        raise workspace_scope.WorkspaceScopeError(
            400,
            "workspace_request_invalid",
            "Workspace request is invalid.",
        )
    scope = workspace_scope.resolve_workspace_scope(investigation_id)
    if scope.output_mode != "internal_working_paper":
        raise workspace_scope.WorkspaceScopeError(
            404,
            "scope_invalid",
            "Workspace is unavailable for this scope.",
        )
    return unavailable_payload(investigation_id, mode)
