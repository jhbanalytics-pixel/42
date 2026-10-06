"""Request grammar and access gates for the Fieldwork read.

Fieldwork is a read-only projection. Every test here guards a way the route
could quietly become something else: a caller-selectable scope, a data path
that runs before authentication, or a private route drifting into the public
API surface.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import main

client = TestClient(main.app)

ROUTE = "/api/internal/v2/fieldwork/read"
VERSION = "fieldwork_workspace_v1"


@pytest.fixture(autouse=True)
def open_gate(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)


def test_the_exact_request_is_accepted():
    response = client.get(f"{ROUTE}?contract_version={VERSION}")
    assert response.status_code in (200, 503)


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?",
        "?contract_version=",
        "?contract_version=%20",
        f"?contract_version={VERSION}&contract_version={VERSION}",
        f"?contract_version={VERSION}&lane=active",
        f"?contract_version={VERSION}&market=za",
        f"?contract_version={VERSION}&actor=someone",
        f"?contract_version={VERSION}&investigation_id=inv_1",
        f"?lane=active&contract_version={VERSION}",
        "?version=fieldwork_workspace_v1",
    ],
    ids=[
        "missing",
        "bare_question_mark",
        "blank",
        "whitespace",
        "duplicated",
        "extra_lane",
        "extra_market",
        "extra_actor",
        "extra_investigation",
        "extra_leading",
        "wrong_parameter_name",
    ],
)
def test_only_the_exact_request_grammar_is_accepted(query: str):
    """Break caught: a caller selecting its own scope through the query."""
    response = client.get(f"{ROUTE}{query}")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "workspace_request_invalid"


@pytest.mark.parametrize(
    "version",
    [
        "fieldwork_workspace_v3",
        "fieldwork_workspace_v0",
        "workspace_v1",
        "FIELDWORK_WORKSPACE_V1",
    ],
)
def test_an_unsupported_version_is_named_as_such(version: str):
    # Distinct from workspace_request_invalid: the grammar was right, the
    # contract is not one this build serves.
    response = client.get(f"{ROUTE}?contract_version={version}")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "contract_version_unsupported"


def test_authentication_fails_before_any_read(monkeypatch):
    """Break caught: storage or Source Lab touched before the gate closes."""
    monkeypatch.delenv("LP_ALLOW_OPEN_GATE", raising=False)
    monkeypatch.setenv("UI_PASSCODE", "fixture-passcode")

    reads: list[str] = []

    from src.api import fieldwork

    def forbidden(*args: object, **kwargs: object) -> object:
        reads.append("read")
        raise AssertionError("a read ran before authentication")

    monkeypatch.setattr(fieldwork, "read_fieldwork_workspace", forbidden)

    response = client.get(f"{ROUTE}?contract_version={VERSION}")

    assert response.status_code == 401
    assert reads == []


def test_the_private_route_never_enters_the_public_api_surface():
    """Break caught: an internal projection published as a public contract.

    This service publishes no OpenAPI at all, and the route is additionally
    marked include_in_schema=False, so it stays private even if a schema is
    ever turned on.
    """
    assert main.app.openapi_url is None
    assert client.get("/openapi.json").status_code == 404
    generated = main.app.openapi()
    assert ROUTE not in generated.get("paths", {})
    assert not any("fieldwork" in path for path in generated.get("paths", {}))


def test_only_get_is_served():
    for method in (client.post, client.put, client.patch, client.delete):
        response = method(f"{ROUTE}?contract_version={VERSION}")
        assert response.status_code in (404, 405)


def test_v2_reads_investigations_with_full_server_scope(monkeypatch):
    from src.api import fieldwork, general_question_routes, investigation_index

    scope = dict(
        client_scope_id="ogilvy_default",
        market_scope=["za"],
        brand_config_id=None,
        audience_lens_ids=[],
        theme_id=None,
    )
    calls = []
    bucket = object()
    monkeypatch.setattr(general_question_routes, "_current_scope", lambda: scope)
    monkeypatch.setattr(main, "_investigation_bucket", lambda: bucket)

    def observe(**kwargs):
        calls.append(kwargs)
        return dict(scope=scope, rows=[], coverage=fieldwork._unavailable_coverage_v2())

    monkeypatch.setattr(
        investigation_index, "observe_fieldwork_investigations", observe
    )
    response = client.get(f"{ROUTE}?contract_version=fieldwork_workspace_v2")
    assert response.status_code == 200
    assert calls == [dict(bucket=bucket, scope=scope)]
    assert response.json()["contract_version"] == "fieldwork_workspace_v2"
    assert response.json()["research_operation_count"] is None


def test_v2_authentication_precedes_scope_and_storage(monkeypatch):
    from src.api import general_question_routes

    monkeypatch.delenv("LP_ALLOW_OPEN_GATE", raising=False)
    monkeypatch.setenv("UI_PASSCODE", "fixture-passcode")
    calls = []

    def forbidden():
        calls.append("read")
        raise AssertionError("scope read before authentication")

    monkeypatch.setattr(general_question_routes, "_current_scope", forbidden)
    monkeypatch.setattr(main, "_investigation_bucket", forbidden)
    response = client.get(f"{ROUTE}?contract_version=fieldwork_workspace_v2")
    assert response.status_code == 401
    assert calls == []


def test_v2_storage_failure_is_partial_input_not_fabricated_zero(monkeypatch):
    def unavailable():
        raise OSError("private detail")

    monkeypatch.setattr(main, "_investigation_bucket", unavailable)
    response = client.get(f"{ROUTE}?contract_version=fieldwork_workspace_v2")
    assert response.status_code == 200
    assert response.json()["research_operation_count"] is None
    assert (
        response.json()["operation_coverage"]["investigation_plans"]["state"]
        == "unavailable"
    )
    assert "private detail" not in response.text
