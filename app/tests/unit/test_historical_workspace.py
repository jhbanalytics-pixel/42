from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import main

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def open_gate(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)


@pytest.mark.parametrize("mode", ["analogue", "recurrence", "replay"])
def test_exact_mode_body_reaches_internal_adapter(monkeypatch, mode):
    from src.api import historical_workspace

    calls = []
    monkeypatch.setattr(
        historical_workspace,
        "read_historical_workspace",
        lambda investigation_id, selected: calls.append((investigation_id, selected))
        or historical_workspace.unavailable_payload(investigation_id, selected),
    )

    response = client.post(
        "/api/internal/v2/investigations/inv_case/historical/read",
        json={"mode": mode},
    )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "X-Passcode"
    assert response.json()["detail"]["code"] == "workspace_unavailable"
    assert calls == [("inv_case", mode)]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"mode": "diffusion"},
        {"mode": "analogue", "run_id": "caller_override"},
        {"mode": "analogue", "client_scope_id": "ogilvy_default"},
    ],
)
def test_invalid_or_expanded_historical_body_refuses_before_adapter(monkeypatch, body):
    from src.api import historical_workspace

    calls = []
    monkeypatch.setattr(
        historical_workspace,
        "read_historical_workspace",
        lambda *_args: calls.append(True),
    )

    response = client.post(
        "/api/internal/v2/investigations/inv_case/historical/read",
        json=body,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "workspace_request_invalid"
    assert calls == []


def test_default_internal_owner_returns_unavailable_without_fixture_fallback(monkeypatch):
    from src.api import historical_workspace, workspace_scope

    monkeypatch.setattr(
        workspace_scope,
        "resolve_workspace_scope",
        lambda _value: type("Scope", (), {"output_mode": "internal_working_paper"})(),
    )

    response = historical_workspace.read_historical_workspace("inv_case", "analogue")

    assert response == historical_workspace.unavailable_payload("inv_case", "analogue")
    assert "items" not in response
    assert "fixture" not in repr(response).lower()


def test_historical_route_is_excluded_from_public_openapi():
    assert "/api/internal/v2/investigations/{investigation_id}/historical/read" not in main.app.openapi()["paths"]


def test_a_closed_gate_returns_401_before_any_historical_read(monkeypatch):
    """The audit's gap: every focused Historical test opened the gate, so
    nothing proved the route itself refuses an unauthenticated caller. The
    refusal must arrive before any read runs, so a probe cannot learn whether
    the investigation exists."""
    monkeypatch.delenv("LP_ALLOW_OPEN_GATE", raising=False)
    monkeypatch.setenv("UI_PASSCODE", "fixture-passcode")

    reads: list[str] = []
    from src.api import historical_workspace as hw

    def forbidden(*args: object, **kwargs: object) -> object:
        reads.append("read")
        raise AssertionError("a historical read ran before authentication")

    monkeypatch.setattr(hw, "read_historical_workspace", forbidden)
    response = client.post(
        "/api/internal/v2/investigations/inv_case/historical/read",
        json={"mode": "analogue"},
    )
    assert response.status_code == 401
    assert reads == []
