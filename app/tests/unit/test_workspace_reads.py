from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import investigations, main, workspace_reads

client = TestClient(main.app)
WORKSPACE_UNAVAILABLE_BYTES = (
    b'{"detail":{"code":"workspace_unavailable","message":"Workspace is unavailable."}}'
)


@pytest.fixture(autouse=True)
def open_gate(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)


def resolved_scope():
    frame = investigations.InvestigationFrame(
        client_scope_id="ogilvy_default",
        market_scope=("za", "ng", "ke"),
        brand_config_id=None,
        audience_lens_ids=(),
        theme_id=None,
        run_id="investigation_run_001",
        contract_version="2.1.0",
        decision_question="Which behaviour warrants deeper investigation?",
        time_horizon_days=42,
        brand_context=None,
        known_assumptions=(),
        change_my_mind_if=(),
        research_role_id=None,
        research_role_version=None,
        output_mode="internal_working_paper",
    )
    response = investigations.build_plan_ready(
        frame=frame,
        plan=investigations.default_plan_for_frame(frame),
        investigation_id=investigations.investigation_id_for_frame(frame),
        created_at=datetime(2026, 8, 28, 8, tzinfo=UTC),
        active_role_versions={},
    )
    return SimpleNamespace(
        investigation_id=response["investigation_id"],
        frame=frame,
        response=response,
        scope_payload={key: response[key] for key in (
            "client_scope_id", "market_scope", "brand_config_id", "audience_lens_ids",
            "theme_id", "run_id", "contract_version",
        )},
        scope_digest="0" * 64,
        output_mode=frame.output_mode,
    )


def test_browser_plan_fixture_matches_the_current_producer():
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/workspaces/investigation_plan_v1.json").read_text(encoding="utf-8"))
    scope = resolved_scope()
    frame = replace(scope.frame, decision_question=fixture["question"])
    response = investigations.build_plan_ready(
        frame=frame, plan=investigations.default_plan_for_frame(frame),
        investigation_id=investigations.investigation_id_for_frame(frame),
        created_at=datetime(2026, 8, 28, 8, tzinfo=UTC), active_role_versions={},
    )
    scope.frame, scope.response, scope.investigation_id = frame, response, response["investigation_id"]
    assert fixture == {
        "investigationId": scope.investigation_id, "question": frame.decision_question,
        "status": workspace_reads.status_resource(scope),
        "claims": workspace_reads.claims_resource(scope),
        "decision": workspace_reads.decision_resource(scope),
    }


def set_staging_runtime(monkeypatch):
    values = {
        "DEPLOYMENT_PROFILE": "open-intelligence-staging",
        "K_SERVICE": "listening-post-staging",
        "BQ_DATASET": "trends_v2_staging",
        "CACHE_BUCKET": "listening-post-staging-cache",
        "CACHE_PREFIX": "open-intelligence/v2/staging/",
        "APPLICATION_SOURCE": "open-intelligence-staging",
        "SOURCE_SHA": "a" * 40,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    for name in tuple(os.environ):
        if name != "UI_PASSCODE" and (
            name.endswith("_API_KEY")
            or name.endswith("_ACCESS_TOKEN")
            or name.endswith("_SECRET")
        ):
            monkeypatch.delenv(name, raising=False)


class EmptyBucket:
    name = "listening-post-staging-cache"

    def get_blob(self, _name):
        return None


@pytest.mark.parametrize(
    ("read_name", "path", "body"),
    [
        ("status", "/api/v2/investigations/inv_case/status", None),
        ("claims", "/api/v2/investigations/inv_case/claims/read", None),
        ("decision", "/api/v2/investigations/inv_case/decision/read", None),
        (
            "artifact",
            "/api/v2/investigations/inv_case/artifacts/ra_doc/read",
            None,
        ),
        (
            "historical",
            "/api/internal/v2/investigations/inv_case/historical/read",
            {"mode": "analogue"},
        ),
    ],
    ids=["status", "claims", "decision", "artifact", "historical"],
)
@pytest.mark.parametrize("failure", ["profile", "identity", "metadata"])
def test_workspace_read_runtime_failure_is_bounded_before_storage_client(
    monkeypatch, read_name, path, body, failure
):
    from google.cloud import storage

    from src.api import deployment_contract

    set_staging_runtime(monkeypatch)
    if failure == "profile":
        monkeypatch.setenv("DEPLOYMENT_PROFILE", "production")
        monkeypatch.setattr(
            deployment_contract,
            "_metadata_service_account",
            lambda: (_ for _ in ()).throw(AssertionError("metadata called")),
        )
    elif failure == "identity":
        monkeypatch.setattr(
            deployment_contract,
            "_metadata_service_account",
            lambda: "590353929363-compute@developer.gserviceaccount.com",
        )
    else:
        monkeypatch.setattr(
            deployment_contract,
            "_metadata_service_account",
            lambda: (_ for _ in ()).throw(TimeoutError("metadata timeout")),
        )
    storage_clients = []
    monkeypatch.setattr(
        storage,
        "Client",
        lambda: storage_clients.append(read_name)
        or type("Client", (), {"bucket": lambda _self, _name: EmptyBucket()})(),
    )

    response = client.post(path, json=body) if body is not None else client.post(path)

    assert response.status_code == 503
    assert response.content == WORKSPACE_UNAVAILABLE_BYTES
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "X-Passcode"
    assert storage_clients == []


def test_status_projects_exact_plan_ready_resource_with_private_headers(monkeypatch):
    from src.api import workspace_scope

    resolved = resolved_scope()
    monkeypatch.setattr(workspace_scope, "resolve_workspace_scope", lambda _value: resolved)

    response = client.post(f"/api/v2/investigations/{resolved.investigation_id}/status")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "X-Passcode"
    payload = response.json()
    assert tuple(payload) == (
        "investigation_id", "client_scope_id", "market_scope", "brand_config_id",
        "audience_lens_ids", "theme_id", "run_id", "contract_version",
        "research_role_id", "research_role_version", "status", "research_plan",
        "budget_state", "source_calls", "completed_work", "missing_work", "claim_ids",
        "decision_id", "artifact_ids", "created_at", "updated_at",
    )
    assert payload["status"] == "plan_ready"
    assert payload["claim_ids"] == []
    assert payload["decision_id"] is None
    assert payload["artifact_ids"] == []
    assert payload["missing_work"] == ["optional_socialcrawl_funding_unverified"]


def test_claims_and_decision_return_honest_empty_resources(monkeypatch):
    from src.api import workspace_scope

    resolved = resolved_scope()
    monkeypatch.setattr(workspace_scope, "resolve_workspace_scope", lambda _value: resolved)

    claims = client.post(f"/api/v2/investigations/{resolved.investigation_id}/claims/read")
    decision = client.post(f"/api/v2/investigations/{resolved.investigation_id}/decision/read")

    assert claims.status_code == decision.status_code == 200
    assert claims.json()["artifact_version"] == "unsealed"
    assert claims.json()["claims"] == []
    assert decision.json()["decision"] is None


def test_any_evidence_room_body_refuses_before_scope_lookup(monkeypatch):
    from src.api import workspace_scope

    called = []
    monkeypatch.setattr(workspace_scope, "resolve_workspace_scope", lambda _value: called.append(True))

    response = client.post("/api/v2/investigations/inv_case/status", json={})

    assert response.status_code == 400
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "X-Passcode"
    assert response.json()["detail"] == {
        "code": "workspace_request_invalid",
        "message": "Workspace request is invalid.",
    }
    assert called == []


def test_authentication_refuses_before_scope_lookup(monkeypatch):
    from src.api import workspace_scope

    monkeypatch.delenv("LP_ALLOW_OPEN_GATE", raising=False)
    monkeypatch.setenv("UI_PASSCODE", "secret")
    called = []
    monkeypatch.setattr(workspace_scope, "resolve_workspace_scope", lambda _value: called.append(True))

    response = client.post("/api/v2/investigations/inv_case/status")

    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "X-Passcode"
    assert called == []


def test_artifact_read_never_falls_back_to_global_legacy_lookup(monkeypatch):
    from src.api import bq, workspace_scope

    resolved = resolved_scope()
    monkeypatch.setattr(workspace_scope, "resolve_workspace_scope", lambda _value: resolved)
    legacy_calls = []
    monkeypatch.setattr(bq, "get_research_artifact", lambda _value: legacy_calls.append(True))

    response = client.post(
        f"/api/v2/investigations/{resolved.investigation_id}/artifacts/ra_doc/read"
    )

    assert response.status_code == 404
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "X-Passcode"
    assert response.json()["detail"]["code"] == "scope_invalid"
    assert legacy_calls == []


def test_public_openapi_has_exact_child_reads_without_request_bodies():
    paths = main.app.openapi()["paths"]
    expected = {
        "/api/v2/investigations/{investigation_id}/status",
        "/api/v2/investigations/{investigation_id}/claims/read",
        "/api/v2/investigations/{investigation_id}/decision/read",
        "/api/v2/investigations/{investigation_id}/artifacts/{artifact_id}/read",
    }
    assert expected.issubset(paths)
    for path in expected:
        operation = paths[path]["post"]
        assert "requestBody" not in operation
