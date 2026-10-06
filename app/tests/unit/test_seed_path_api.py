"""Seed Explorer API route auth and shape."""

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
    bq._seed_graph_cache.clear()
    yield


def test_seed_path_requires_passcode(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    r = client.get("/api/seed-path", params={"keyword": "amapiano", "market": "za"})
    assert r.status_code == 401


def test_seed_path_returns_adjacency_and_path(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")

    def fake_adj(keyword, market):
        return {
            "keyword": keyword,
            "market": market,
            "trend_date": "2026-07-01",
            "found": True,
            "co_occur_terms": [{"term": "piano", "count": 2, "term_type": "token"}],
            "topic_overlap": [{"topic": "music_amapiano", "label": "Amapiano", "row_count": 200}],
            "near_topics": [],
            "bridge_creators": [],
            "lexicon_hits": [],
        }

    def fake_path(keyword, market):
        return {
            "keyword": keyword,
            "market": market,
            "trend_date": "2026-07-01",
            "term": keyword,
            "term_type": "token",
            "channels": [
                {
                    "platform": "TikTok",
                    "first_seen": "2026-06-20",
                    "row_count": 120,
                    "watched_since": False,
                }
            ],
            "span_days": 0,
            "confidence": "thin",
            "coverage_note": "",
        }

    monkeypatch.setattr(bq, "fetch_seed_graph_adjacency", fake_adj)
    monkeypatch.setattr(bq, "fetch_seed_path", fake_path)

    r = client.get(
        "/api/seed-path",
        params={"keyword": "amapiano", "market": "za"},
        headers={"X-Passcode": "s3cret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["keyword"] == "amapiano"
    assert body["adjacency"]["found"] is True
    assert body["path"]["term"] == "amapiano"


def test_seed_path_rejects_empty_keyword(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    r = client.get(
        "/api/seed-path",
        params={"keyword": "  ", "market": "za"},
        headers={"X-Passcode": "s3cret"},
    )
    assert r.status_code == 400

def test_seed_path_bq_failure_returns_503(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")

    def boom(keyword, market):
        raise RuntimeError("bq down")

    monkeypatch.setattr(bq, "fetch_seed_graph_adjacency", boom)
    monkeypatch.setattr(bq, "fetch_seed_path", boom)

    r = client.get(
        "/api/seed-path",
        params={"keyword": "amapiano", "market": "za"},
        headers={"X-Passcode": "s3cret"},
    )
    assert r.status_code == 503
    assert "not available" in r.json()["detail"]
