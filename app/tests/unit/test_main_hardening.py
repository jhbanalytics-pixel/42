"""Auth rate limit and market-topics labels."""

import pytest
from fastapi.testclient import TestClient
from src.api import bq, main

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    main._market_cache.clear()
    main._auth_hits.clear()
    yield
    main._auth_hits.clear()


def test_auth_verify_rate_limited(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    for _ in range(main.AUTH_RATE_LIMIT_MAX):
        r = client.post("/api/auth/verify", json={"passcode": "wrong"})
        assert r.status_code == 401
    r = client.post("/api/auth/verify", json={"passcode": "wrong"})
    assert r.status_code == 429
    # Even the correct passcode is refused while the window is saturated.
    r = client.post("/api/auth/verify", json={"passcode": "s3cret"})
    assert r.status_code == 429


def test_auth_verify_allows_within_window(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    r = client.post("/api/auth/verify", json={"passcode": "s3cret"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_market_topics_carry_labels(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    rows = [{"market": "za", "query_group": "music_amapiano", "trend_score": 0.9}]
    monkeypatch.setattr(bq, "fetch_market_topics", lambda mks, per_market=10: rows)
    r = client.get(
        "/api/market-topics",
        params={"markets": "za"},
        headers={"X-Passcode": "s3cret"},
    )
    assert r.status_code == 200
    topic = r.json()["topics"][0]
    assert topic["label"] == bq.topic_label("music_amapiano")


def test_bq_client_defaults_project(monkeypatch):
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    captured = {}

    class FakeClient:
        def __init__(self, project=None):
            captured["project"] = project

    import sys
    import types

    fake_bigquery = types.SimpleNamespace(Client=FakeClient)
    fake_cloud = types.SimpleNamespace(bigquery=fake_bigquery)
    monkeypatch.setitem(sys.modules, "google.cloud", fake_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bigquery)
    monkeypatch.setattr(bq, "_client", None)
    try:
        bq._get_client()
    finally:
        bq._client = None
    assert captured["project"] == "ogilvy-trends-v2"
