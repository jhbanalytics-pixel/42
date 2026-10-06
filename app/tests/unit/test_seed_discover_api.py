"""Discover API route auth and shape."""

import pytest
from fastapi.testclient import TestClient

from src.api import main, seed_discover

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    main._market_cache.clear()
    yield


def test_seed_discover_requires_passcode(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    r = client.get("/api/seed-discover")
    assert r.status_code == 401


def test_seed_discover_returns_week_payload(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")

    def fake_build(*, market=None, status="pending"):
        return {
            "week_start": "2026-06-30",
            "week_end": "2026-07-06",
            "as_of": "2026-07-02",
            "dataset": "trends_v2_dev",
            "status": status,
            "markets": {
                "za": {
                    "pending_count": 1,
                    "weekly_count": 0,
                    "fast_count": 1,
                    "candidates": [
                        {
                            "id": "c1",
                            "term": "amapiano",
                            "market": "za",
                            "lane": "fast",
                            "score": 0.82,
                            "status": "pending",
                        }
                    ],
                }
            },
        }

    monkeypatch.setattr(seed_discover, "build_discover_payload", fake_build)

    r = client.get(
        "/api/seed-discover",
        params={"market": "za", "status": "pending"},
        headers={"X-Passcode": "s3cret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending"
    assert body["markets"]["za"]["candidates"][0]["term"] == "amapiano"


def test_seed_discover_rejects_bad_market(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    r = client.get(
        "/api/seed-discover",
        params={"market": "us"},
        headers={"X-Passcode": "s3cret"},
    )
    assert r.status_code == 400


def test_seed_discover_rejects_bad_status(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    r = client.get(
        "/api/seed-discover",
        params={"status": "nope"},
        headers={"X-Passcode": "s3cret"},
    )
    assert r.status_code == 400

def test_seed_discover_error_payload_is_not_cached(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    calls = {"n": 0}

    def flaky_build(*, market=None, status="pending"):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "week_start": "2026-06-30",
                "week_end": "2026-07-06",
                "as_of": "2026-07-02",
                "dataset": "trends_v2_dev",
                "status": status,
                "error": "Discovery proposals not available yet.",
                "markets": {},
            }
        return {
            "week_start": "2026-06-30",
            "week_end": "2026-07-06",
            "as_of": "2026-07-02",
            "dataset": "trends_v2_dev",
            "status": status,
            "markets": {},
        }

    monkeypatch.setattr(seed_discover, "build_discover_payload", flaky_build)
    headers = {"X-Passcode": "s3cret"}
    r1 = client.get("/api/seed-discover", headers=headers)
    assert "error" in r1.json()
    # The error payload was not cached, so the next hit reloads and succeeds.
    r2 = client.get("/api/seed-discover", headers=headers)
    assert "error" not in r2.json()
    assert calls["n"] == 2
    # The good payload IS cached: a third hit does not reload.
    r3 = client.get("/api/seed-discover", headers=headers)
    assert "error" not in r3.json()
    assert calls["n"] == 2
