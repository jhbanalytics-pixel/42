"""Integration tests for the research pipeline (mocked BQ + Vertex)."""

import json
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import bq, investigation_scopes, main, persona_registry, research, synth

client = TestClient(main.app)
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "research"
FROZEN = date(2026, 6, 15)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.setenv("CACHE_BUCKET", "")
    monkeypatch.setenv("RESEARCH_PERSIST_BACKEND", "gcs")
    synth._CACHE.clear()
    main._research_hits.clear()
    with main._research_jobs_lock:
        main._research_jobs.clear()
    with main._generation_capacity_lock:
        main._generation_active = 0
    yield
    _wait_research_jobs()
    synth._CACHE.clear()
    main._research_hits.clear()


def _golden():
    return json.loads((FIXTURES / "golden_digital_architect.json").read_text(encoding="utf-8"))


def _wait_research_jobs(timeout: float = 8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with main._research_jobs_lock:
            if not main._research_jobs:
                return
        time.sleep(0.02)
    raise AssertionError("research job never finished")


@pytest.mark.parametrize(
    ("path", "payload", "frame_index"),
    [
        ("/api/research/generate", {"persona_id": "audience_neutral"}, 3),
        (
            "/api/research/generate-batch",
            {
                "persona_id": "audience_neutral",
                "behaviours": [{"market": "za", "label": "Street football"}],
            },
            2,
        ),
        (
            "/api/research/generate-consolidated",
            {
                "persona_id": "audience_neutral",
                "markets": ["za"],
                "behaviours": [{"market": "za", "label": "Street football"}],
            },
            2,
        ),
    ],
)
def test_research_generation_preserves_omitted_blank_and_explicit_frames(
    monkeypatch, path, payload, frame_index
):
    captured = []

    def capture(jobs, key, target, args):
        captured.append(args[frame_index])
        jobs.discard(key)
        main._release_generation_capacity()

    monkeypatch.setattr(main, "_check_research_rate_limit", lambda *args: None)
    monkeypatch.setattr(main, "_start_generation_thread", capture)

    for marker, expected in ((None, None), ("", ""), ("  Mobile service  ", "Mobile service")):
        body = dict(payload)
        if marker is not None:
            body["product_frame"] = marker
        response = client.post(path, json=body)
        assert response.status_code == 202
        assert captured[-1] == expected


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/api/research/generate",
            {"persona_id": "audience_neutral", "markets": ["za"]},
        ),
        (
            "/api/research/generate-batch",
            {
                "persona_id": "audience_neutral",
                "behaviours": [{"market": "za", "label": "Street football"}],
            },
        ),
        (
            "/api/research/generate-consolidated",
            {
                "persona_id": "audience_neutral",
                "markets": ["za"],
                "behaviours": [{"market": "za", "label": "Street football"}],
            },
        ),
    ],
)
def test_research_generation_uses_shared_capacity(monkeypatch, path, payload):


    def finish_research(key, *args):
        with main._research_jobs_lock:
            main._research_jobs.discard(key)

    monkeypatch.setenv("GENERATION_MAX_CONCURRENT", "1")
    monkeypatch.setattr(main, "_run_research_job", finish_research)
    monkeypatch.setattr(main, "_run_research_batch_job", finish_research)
    monkeypatch.setattr(main, "_run_research_consolidated_job", finish_research)

    try:
        assert main._try_acquire_generation_capacity() is True

        response = client.post(path, json=payload)

        assert response.status_code == 429
        assert response.headers["Retry-After"] == "30"
        assert response.json() == {
            "error": "capacity_busy",
            "message": "The desk is at capacity. Try again shortly.",
            "retry_after_seconds": 30,
        }
    finally:
        main._release_generation_capacity()



def _patch_golden(monkeypatch):
    g = _golden()

    monkeypatch.setattr(bq, "latest_trend_date", lambda: FROZEN)
    monkeypatch.setattr(
        bq,
        "fetch_research_seeds",
        lambda markets, trend_date=None: g["seeds"],
    )
    monkeypatch.setattr(
        bq,
        "fetch_research_digest",
        lambda trend_date=None: g["digest"],
    )

    def briefs_for_markets(groups, markets, trend_date=None):
        want = {m.lower() for m in markets}
        return [b for b in g["briefs"] if b["market"] in want]

    monkeypatch.setattr(bq, "fetch_topic_briefs_for_markets", briefs_for_markets)
    monkeypatch.setattr(
        bq,
        "fetch_rising_search_terms_for_markets",
        lambda markets, anchors, trend_date=None: [
            t for t in g["rising_terms"] if t["market"] in {m.lower() for m in markets}
        ],
    )
    monkeypatch.setattr(
        bq,
        "fetch_research_posts",
        lambda markets, groups, limit=80: [p for p in g["posts"] if p["market"] in markets],
    )
    monkeypatch.setattr(
        bq,
        "fetch_posts_and_comments_for_topics",
        lambda markets, groups, **kw: [p for p in g["posts"] if p["market"] in {m.lower() for m in markets}],
    )
    monkeypatch.setattr(bq, "fetch_comment_threads_for_posts", lambda posts, per_post=10: {})
    monkeypatch.setattr(
        bq,
        "fetch_lexicon_rows_for_markets",
        lambda markets: [r for r in g["lexicon"] if r["market"] in {m.lower() for m in markets}],
    )


def test_build_research_evidence_golden_hash(monkeypatch):
    _patch_golden(monkeypatch)
    desk_calls = {"n": 0}

    def _no_desk(_market):
        desk_calls["n"] += 1
        return []

    monkeypatch.setattr(bq, "fetch_desk_rows", _no_desk)
    ev = research.build_research_evidence(
        "audience_neutral",
        ["ke", "ng"],
        trend_date=FROZEN,
    )
    assert desk_calls["n"] == 0
    assert ev["query_groups"] == _golden()["expected_query_groups"]
    assert ev["trend_date"] == "2026-06-15"
    assert len(ev["refs"]) >= 8
    if len(ev["refs"]) >= persona_registry.THIN_FLOOR:
        assert ev["quality"]["level"] in ("moderate", "strong")


def test_build_research_evidence_three_markets(monkeypatch):
    _patch_golden(monkeypatch)
    ev = research.build_research_evidence(
        "audience_neutral",
        ["za", "ng", "ke"],
        trend_date=FROZEN,
    )
    assert set(ev["markets"]) == {"za", "ng", "ke"}
    assert ev["trend_date"] == "2026-06-15"
    assert len(ev["refs"]) >= 8


def test_run_research_job_thin_skips_synth(monkeypatch):
    _patch_golden(monkeypatch)
    monkeypatch.setattr(bq, "fetch_research_seeds", lambda *a, **k: [])
    monkeypatch.setattr(bq, "fetch_research_digest", lambda *a, **k: None)
    monkeypatch.setattr(bq, "fetch_topic_briefs_for_markets", lambda *a, **k: [])
    monkeypatch.setattr(bq, "fetch_rising_search_terms_for_markets", lambda *a, **k: [])
    monkeypatch.setattr(bq, "fetch_research_posts", lambda *a, **k: [])
    monkeypatch.setattr(bq, "fetch_posts_and_comments_for_topics", lambda *a, **k: [])
    monkeypatch.setattr(bq, "fetch_lexicon_rows_for_markets", lambda *a, **k: [])
    called = {"synth": 0}

    def _no_synth(ev):
        called["synth"] += 1
        return None

    monkeypatch.setattr(synth, "synthesize_research", _no_synth)
    out = research.run_research_job("audience_neutral", ["ke"], None)
    assert called["synth"] == 0
    assert out["doc"].get("thin") is True


def test_api_research_generate_three_markets(monkeypatch):
    _patch_golden(monkeypatch)
    monkeypatch.setattr(
        synth,
        "synthesize_research",
        lambda ev: {
            "title": "Market Research Perspective: Audience neutral",
            "grounded": {"behavioural_synthesis": [{"text": "Pan-SSA hustle", "evidence_indices": [1]}]},
            "inference": {},
            "activation": {"google_prompts": [], "provenance": "inference"},
        },
    )
    r = client.post(
        "/api/research/generate",
        json={"persona_id": "audience_neutral", "markets": ["za", "ng", "ke"]},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    deadline = time.time() + 8.0
    payload = None
    while time.time() < deadline:
        s = client.get("/api/research/status", params={"job_id": job_id})
        body = s.json()
        if not body.get("pending"):
            payload = body
            break
        time.sleep(0.02)
    assert payload is not None
    assert payload.get("status") in ("completed", "completed_degraded")


def test_api_research_generate_and_status(monkeypatch):
    _patch_golden(monkeypatch)
    monkeypatch.setattr(
        synth,
        "synthesize_research",
        lambda ev: {
            "title": "Market Research Perspective: Audience neutral",
            "grounded": {
                "behavioural_synthesis": [
                    {"text": "Hustle stacks on mobile money", "evidence_indices": [1, 2]}
                ],
                "voice_read": [],
            },
            "inference": {},
            "activation": {"google_prompts": [], "provenance": "inference"},
        },
    )
    r = client.post(
        "/api/research/generate",
        json={"persona_id": "audience_neutral", "markets": ["ke", "ng"]},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    deadline = time.time() + 8.0
    payload = None
    while time.time() < deadline:
        s = client.get("/api/research/status", params={"job_id": job_id})
        body = s.json()
        if not body.get("pending"):
            payload = body
            break
        time.sleep(0.02)
    assert payload is not None
    assert payload.get("status") in ("completed", "completed_degraded")
    assert payload.get("artifact_id")


def test_api_research_personas():
    r = client.get("/api/research/personas")
    assert r.status_code == 200
    personas = r.json()["personas"]
    assert len(personas) == 1
    p = personas[0]
    assert p["id"] == "audience_neutral"
    assert p["description"]
    assert p["product_frame"] == ""
    assert p["product_frame_options"] == []
    assert "fintech_mpesa" in p["query_groups"]
    assert p["default_markets"] == ["ke", "ng"]
    topics = {t["id"]: t for t in p["signal_topics"]}
    assert topics["economy_sapa_hustle"]["label"] == "Sapa hustle economy"
    assert topics["tech_gemini_ai"]["label"] == "Gemini & AI adoption"
    assert topics["economy_sapa_hustle"]["hint"]


def test_research_rate_limit_generate(monkeypatch):
    _patch_golden(monkeypatch)
    monkeypatch.setattr(
        research,
        "run_research_job",
        lambda *a, **k: {"status": "completed", "doc": {"thin": True}, "evidence_graph": {}},
    )
    monkeypatch.setattr(
        research,
        "persist_job_result",
        lambda r, parent_artifact_id=None: "ra_test",
    )
    ip = "203.0.113.50"
    cap = main._research_generate_cap()
    for _ in range(cap):
        r = client.post(
            "/api/research/generate",
            json={"persona_id": "audience_neutral"},
            headers={"x-forwarded-for": ip},
        )
        assert r.status_code == 202
    r = client.post(
        "/api/research/generate",
        json={"persona_id": "audience_neutral"},
        headers={"x-forwarded-for": ip},
    )
    assert r.status_code == 429


def test_research_export_html_post_renders_without_artifact_lookup(monkeypatch):
    doc = {
        "title": "Market Research Perspective: Digital Architect",
        "grounded": {"user_insights": [{"text": "Nigeria hustle signal.", "evidence_indices": [1]}]},
    }
    r = client.post(
        "/api/research/export-html",
        json={
            "doc_json": doc,
            "persona_label": "Digital Architect",
            "markets": ["ng"],
            "artifact_id": "ra_deadbeef1234",
        },
    )
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert 'filename="42-brief-ra_deadbeef1.html"' in r.headers.get("content-disposition", "")
    assert "pulse-brief" not in r.headers.get("content-disposition", "")
    assert "Digital Architect" in r.text
    assert "Nigeria hustle signal." in r.text


def test_research_export_html_get_404_when_cache_miss(monkeypatch):
    monkeypatch.setattr(bq, "get_research_artifact", lambda _aid: None)
    r = client.get("/api/research/ra_0000000000000001/export.html")
    assert r.status_code == 404


def test_research_export_html_get_works_when_artifact_present(monkeypatch):
    doc = {"title": "Cached brief", "grounded": {"objective": {"text": "Objective line."}}}
    monkeypatch.setattr(
        bq,
        "get_research_artifact",
        lambda aid: {
            "artifact_id": aid,
            "client_scope_id": investigation_scopes.default_client_scope_id(),
            "doc_json": doc,
            "persona_label": "Digital Architect",
            "markets": ["za"],
        },
    )
    r = client.get("/api/research/ra_cacded0000000001/export.html")
    assert r.status_code == 200
    assert "Cached brief" in r.text
    assert "Objective line." in r.text


LEGACY_ARTIFACT_ID = "ra_1e6ac0de00000001"


def _legacy_research_row():
    legacy_doc = {
        "title": "Historical brief",
        "grounded": {"voice_of_genz": ["A grounded historical voice"]},
    }
    return {
        "artifact_id": LEGACY_ARTIFACT_ID,
        "client_scope_id": investigation_scopes.default_client_scope_id(),
        "doc_json": legacy_doc,
        "synthesis": legacy_doc,
        "doc_markdown": "# Historical brief",
        "persona_id": "digital_architect_genz",
        "persona_label": "Digital Architect Gen Z",
        "markets": ["ng"],
        "evidence_graph": {"refs": []},
    }


def _assert_neutral_historical_doc(doc_json):
    assert doc_json["grounded"]["voice_read"] == ["A grounded historical voice"]
    assert "voice_of_genz" not in json.dumps(doc_json)


def test_historical_artifact_read_migrates_voice_without_mutating_stored_row(monkeypatch):
    row = _legacy_research_row()
    original = json.loads(json.dumps(row))
    monkeypatch.setattr(bq, "get_research_artifact", lambda _aid: row)

    response = client.get(f"/api/research/{LEGACY_ARTIFACT_ID}")

    assert response.status_code == 200
    _assert_neutral_historical_doc(response.json()["doc"]["json"])
    assert row == original


def test_historical_artifact_html_export_receives_neutral_doc(monkeypatch):
    row = _legacy_research_row()
    original = json.loads(json.dumps(row))
    captured = {}
    monkeypatch.setattr(bq, "get_research_artifact", lambda _aid: row)
    def capture_html(doc_json, **kwargs):
        captured["doc"] = doc_json
        return "<html></html>"

    monkeypatch.setattr(synth, "render_research_html", capture_html)

    response = client.get(f"/api/research/{LEGACY_ARTIFACT_ID}/export.html")

    assert response.status_code == 200
    _assert_neutral_historical_doc(captured["doc"])
    assert row == original


def test_historical_artifact_evidence_export_receives_neutral_doc(monkeypatch):
    row = _legacy_research_row()
    original = json.loads(json.dumps(row))
    captured = {}
    monkeypatch.setattr(bq, "get_research_artifact", lambda _aid: row)
    def capture_pack(doc_json, graph):
        captured["doc"] = doc_json
        return "<html></html>"

    monkeypatch.setattr(synth, "render_evidence_pack_html", capture_pack)

    response = client.get(f"/api/research/{LEGACY_ARTIFACT_ID}/evidence-pack.html")

    assert response.status_code == 200
    _assert_neutral_historical_doc(captured["doc"])
    assert row == original


def test_historical_artifact_post_export_receives_neutral_doc(monkeypatch):
    row = _legacy_research_row()
    original = json.loads(json.dumps(row))
    captured = {}
    def capture_html(doc_json, **kwargs):
        captured["doc"] = doc_json
        return "<html></html>"

    monkeypatch.setattr(synth, "render_research_html", capture_html)

    response = client.post(
        "/api/research/export-html",
        json={"doc_json": row["doc_json"], "artifact_id": row["artifact_id"]},
    )

    assert response.status_code == 200
    _assert_neutral_historical_doc(captured["doc"])
    assert row == original


def test_historical_artifact_refine_receives_neutral_doc(monkeypatch):
    row = _legacy_research_row()
    original = json.loads(json.dumps(row))
    captured = {}

    def fake_refine(artifact, instruction):
        captured["artifact"] = artifact
        return {
            **artifact,
            "doc_json": artifact["doc_json"],
            "doc_markdown": "# Refined",
            "refined": 1,
        }

    monkeypatch.setattr(bq, "get_research_artifact", lambda _aid: row)
    monkeypatch.setattr(research, "refine_research_artifact", fake_refine)
    monkeypatch.setattr(bq, "insert_research_artifact", lambda _row: "ra_refined123")

    response = client.post(
        "/api/research/refine",
        json={"artifact_id": row["artifact_id"], "instruction": "Sharpen this"},
    )

    assert response.status_code == 200
    _assert_neutral_historical_doc(captured["artifact"]["doc_json"])
    assert row == original
