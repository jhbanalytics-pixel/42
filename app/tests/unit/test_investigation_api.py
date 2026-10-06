from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import PreconditionFailed

from src.api import main

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def open_test_gate(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)


class ReadBlob:
    def __init__(self, data):
        self.data = data

    def download_as_bytes(self):
        return self.data


class WriteBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name

    def upload_from_string(self, data, *, content_type, if_generation_match):
        self.bucket.upload_count += 1
        if self.name in self.bucket.objects and if_generation_match == 0:
            raise PreconditionFailed("exists")
        self.bucket.objects[self.name] = data


class Bucket:
    def __init__(self):
        self.name = "listening-post-staging-cache"
        self.objects = {}
        self.upload_count = 0

    def blob(self, name):
        return WriteBlob(self, name)

    def get_blob(self, name):
        data = self.objects.get(name)
        return ReadBlob(data) if data is not None else None


def request_payload(**overrides):
    payload = {
        "client_scope_id": "ogilvy_default",
        "market_scope": ["za", "ng", "ke"],
        "brand_config_id": None,
        "audience_lens_ids": [],
        "theme_id": None,
        "run_id": "investigation_run_001",
        "contract_version": "2.1.0",
        "decision_question": "Which emerging behaviour should the brand act on in six weeks?",
        "time_horizon_days": 42,
        "brand_context": None,
        "known_assumptions": [],
        "change_my_mind_if": [],
        "research_role_id": None,
        "research_role_version": None,
        "output_mode": "internal_working_paper",
    }
    payload.update(overrides)
    return payload


def set_foundation(monkeypatch, bucket):
    monkeypatch.setenv("CACHE_PREFIX", "open-intelligence/v2/staging/")
    monkeypatch.setattr(main, "_investigation_bucket", lambda: bucket)
    monkeypatch.setattr(
        main,
        "_investigation_now",
        lambda: datetime(2026, 8, 26, 8, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(main, "_active_research_role_versions", lambda: {})
    monkeypatch.setattr(main, "_validate_investigation_runtime", lambda: None)


def test_create_investigation_returns_persisted_plan_ready_contract(monkeypatch):
    bucket = Bucket()
    set_foundation(monkeypatch, bucket)

    response = client.post("/api/v2/investigations", json=request_payload())

    assert response.status_code == 200
    data = response.json()
    assert data["investigation_id"].startswith("inv_")
    assert len(data["investigation_id"]) == 68
    assert data["status"] == "plan_ready"
    assert data["contract_version"] == "2.1.0"
    assert data["market_scope"] == ["za", "ng", "ke"]
    assert data["audience_lens_ids"] == []
    assert data["research_plan"] == {
        "questions": [
            "Which emerging behaviour should the brand act on in six weeks?",
            "What changed inside the declared market and time horizon?",
            "What evidence supports and opposes acting now?",
            "Which materially different responses should be compared?",
        ],
        "required_source_families": ["social", "search", "news", "history"],
        "socialcrawl_lanes": [],
        "historical_window_days": 365,
        "credit_ceiling": 0,
        "token_budget": {},
        "stopping_conditions": [
            "Stop if a factual claim has no resolvable receipt.",
            "Stop if a blocking contradiction remains unresolved.",
            "Stop before any paid source call without an approved funding gate.",
        ],
        "known_gaps": ["optional_socialcrawl_funding_unverified"],
    }
    assert bucket.upload_count == 1
    assert len(bucket.objects) == 1


def test_identical_retry_returns_existing_record_without_second_write(monkeypatch):
    bucket = Bucket()
    set_foundation(monkeypatch, bucket)
    payload = request_payload()

    first = client.post("/api/v2/investigations", json=payload)
    second = client.post("/api/v2/investigations", json=payload)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert bucket.upload_count == 1


def test_changed_frame_with_same_run_id_gets_distinct_investigation(monkeypatch):
    bucket = Bucket()
    set_foundation(monkeypatch, bucket)

    first = client.post("/api/v2/investigations", json=request_payload())
    second = client.post(
        "/api/v2/investigations",
        json=request_payload(decision_question="Which behaviour should the brand avoid?"),
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["investigation_id"] != second.json()["investigation_id"]
    assert len(bucket.objects) == 2


def test_wrong_version_and_stale_role_fail_with_approved_errors(monkeypatch):
    bucket = Bucket()
    set_foundation(monkeypatch, bucket)

    wrong_version = client.post(
        "/api/v2/investigations",
        json=request_payload(contract_version="2.0.0"),
    )
    assert wrong_version.status_code == 400
    assert wrong_version.json()["detail"]["code"] == "contract_version_unsupported"

    for payload in (
        request_payload(client_scope_id="unknown_scope"),
        request_payload(audience_lens_ids=["gen_z_inferred"]),
    ):
        invalid_scope = client.post("/api/v2/investigations", json=payload)
        assert invalid_scope.status_code == 404
        assert invalid_scope.json()["detail"] == {
            "code": "scope_invalid",
            "message": "Investigation scope is invalid.",
        }

    monkeypatch.setattr(
        main,
        "_active_research_role_versions",
        lambda: {"strategist": "active_version"},
    )
    stale_role = client.post(
        "/api/v2/investigations",
        json=request_payload(
            research_role_id="strategist",
            research_role_version="stale_version",
        ),
    )
    assert stale_role.status_code == 404
    assert stale_role.json()["detail"]["code"] == "scope_invalid"
    assert bucket.upload_count == 0


def test_storage_unavailable_fails_before_plan_is_presented(monkeypatch):
    set_foundation(monkeypatch, None)

    response = client.post("/api/v2/investigations", json=request_payload())

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "investigation_storage_unavailable",
        "message": "Investigation storage is unavailable.",
    }


def test_endpoint_refuses_process_that_did_not_start_as_exact_staging(monkeypatch):
    bucket = Bucket()
    monkeypatch.setenv("CACHE_PREFIX", "open-intelligence/v2/staging/")
    monkeypatch.setattr(main, "_investigation_bucket", lambda: bucket)
    monkeypatch.setattr(
        main,
        "_investigation_now",
        lambda: datetime(2026, 8, 26, 8, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(main, "_active_research_role_versions", lambda: {})

    response = client.post("/api/v2/investigations", json=request_payload())

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "investigation_runtime_invalid"
    assert bucket.upload_count == 0


def test_endpoint_rejects_unknown_request_field(monkeypatch):
    bucket = Bucket()
    set_foundation(monkeypatch, bucket)

    response = client.post(
        "/api/v2/investigations",
        json={**request_payload(), "change_my_mind_when": ["misspelled field"]},
    )

    assert response.status_code == 422
    assert bucket.upload_count == 0


def test_role_authority_uses_explicit_pinned_digest_mapping(monkeypatch):
    captured = []

    def available(*, approved_role_digests):
        captured.append(approved_role_digests)
        return (SimpleNamespace(role_id="strategist", role_version="v1"),)

    monkeypatch.setattr(main.research_role_registry, "list_available_roles", available)

    assert main._active_research_role_versions() == {"strategist": "v1"}
    assert captured == [main.research_role_registry.APPROVED_ROLE_DIGESTS]


def test_investigation_bucket_accessor_rejects_wrong_bucket(monkeypatch):
    bucket = Bucket()
    bucket.name = "production-cache"

    class Client:
        def bucket(self, _name):
            return bucket

    monkeypatch.setattr(main, "_new_investigation_storage_client", lambda: Client())

    with pytest.raises(main.deployment_contract.DeploymentContractError, match="bucket"):
        main._investigation_bucket()


def test_investigation_bucket_accessor_uses_dedicated_staging_client(monkeypatch):
    bucket = Bucket()
    requested = []

    class Client:
        def bucket(self, name):
            requested.append(name)
            return bucket

    monkeypatch.setattr(main, "_new_investigation_storage_client", lambda: Client())

    assert main._investigation_bucket() is bucket
    assert requested == ["listening-post-staging-cache"]


def test_endpoint_maps_wrong_bucket_to_nonrevealing_runtime_error(monkeypatch):
    monkeypatch.setenv("CACHE_PREFIX", "open-intelligence/v2/staging/")
    monkeypatch.setattr(main, "_validate_investigation_runtime", lambda: None)
    monkeypatch.setattr(
        main,
        "_investigation_bucket",
        lambda: (_ for _ in ()).throw(
            main.deployment_contract.DeploymentContractError("wrong bucket")
        ),
    )
    monkeypatch.setattr(main, "_active_research_role_versions", lambda: {})

    response = client.post("/api/v2/investigations", json=request_payload())

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "investigation_runtime_invalid",
        "message": "Investigation runtime is unavailable.",
    }
