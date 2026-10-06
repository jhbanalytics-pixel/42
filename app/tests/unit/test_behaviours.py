"""Behaviour scan: derive logic and endpoint gate."""

import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from src.api import behaviours, bq, main, research

FROZEN = date(2026, 6, 15)

client = TestClient(main.app)


TOPICS = [
    {
        "market": "ng",
        "query_group": "economy_sapa_hustle",
        "trend_score": 0.82,
        "item_count": 140,
        "cultural_context": "Everyday Nigerians turn low-balance money jokes into shared hustle humour. It spreads fast.",
        "headline": "Sapa humour",
    },
    {
        "market": "ng",
        "query_group": "fintech_mpesa",
        "trend_score": 0.61,
        "item_count": 90,
        "cultural_context": "Constant conversation around mobile money signalling everyday use.",
        "headline": "Mobile money",
    },
    {
        "market": "ke",
        "query_group": "music_gengetone",
        "trend_score": 0.55,
        "item_count": 70,
        "cultural_context": "Gengetone is the sound of Nairobi street pride right now.",
        "headline": "Gengetone",
    },
]

POSTS = [
    {"market": "ng", "topic_groups": ["economy_sapa_hustle"], "platform": "tiktok", "author_handle": "@a", "url": "https://tiktok.com/@a/1", "text": "sapa is real this month", "engagement_total": 900, "content_type": "post", "voice_kind": "post"},
    {"market": "ng", "topic_groups": ["economy_sapa_hustle"], "platform": "instagram", "author_handle": "@b", "url": "https://instagram.com/p/b", "text": "low balance jokes", "engagement_total": 500, "content_type": "post", "voice_kind": "post"},
    {"market": "ng", "topic_groups": ["economy_sapa_hustle"], "platform": "instagram", "author_handle": "@c", "url": "", "text": "comment without link", "engagement_total": 300, "content_type": "instagram_post_comment", "voice_kind": "comment"},
    {"market": "ng", "topic_groups": ["economy_sapa_hustle"], "platform": "threads", "author_handle": "@d", "url": "https://threads.net/@d/1", "text": "extra one", "engagement_total": 100, "content_type": "post", "voice_kind": "post"},
    {"market": "ke", "topic_groups": ["music_gengetone"], "platform": "tiktok", "author_handle": "@e", "url": "https://tiktok.com/@e/2", "text": "gengetone banger", "engagement_total": 400, "content_type": "post", "voice_kind": "post"},
]

VOICE_METRICS = {
    ("ng", "economy_sapa_hustle"): {"post_count": 140, "comment_count": 22, "engagement_total": 9000, "platform_count": 3},
    ("ng", "fintech_mpesa"): {"post_count": 90, "comment_count": 0, "engagement_total": 1200, "platform_count": 2},
    ("ke", "music_gengetone"): {"post_count": 70, "comment_count": 5, "engagement_total": 800, "platform_count": 1},
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    main._research_hits.clear()
    with main._research_jobs_lock:
        main._research_jobs.clear()
    yield
    deadline = time.time() + 5
    while time.time() < deadline:
        with main._research_jobs_lock:
            if not main._research_jobs:
                break
        time.sleep(0.02)
    main._research_hits.clear()


def _patch(monkeypatch):
    monkeypatch.setattr(bq, "latest_trend_date", lambda: FROZEN)
    monkeypatch.setattr(bq, "fetch_market_topics", lambda markets, per_market, trend_date=None: [t for t in TOPICS if t["market"] in markets])

    def fake_voice(markets, groups, *, per_topic_posts=3, per_topic_comments=5):
        out = []
        for p in POSTS:
            if p["market"] not in markets:
                continue
            hit = set(p["topic_groups"]) & set(groups)
            if not hit:
                continue
            row = dict(p)
            row["topic_group"] = sorted(hit)[0]
            out.append(row)
        return out

    monkeypatch.setattr(bq, "fetch_posts_and_comments_for_topics", fake_voice)
    monkeypatch.setattr(
        bq,
        "fetch_voice_metrics_for_topics",
        lambda markets, groups: {k: v for k, v in VOICE_METRICS.items() if k[0] in markets and k[1] in groups},
    )


def test_scan_groups_by_market_with_examples(monkeypatch):
    _patch(monkeypatch)
    out = behaviours.scan_market_behaviours(["ng", "ke"], 10)
    assert out["markets"] == ["ng", "ke"]
    ng = out["behaviours"]["ng"]
    assert len(ng) == 2
    top = ng[0]
    assert top["query_group"] == "economy_sapa_hustle"
    assert top["metric"]["post_count"] == 140
    assert top["metric"]["comment_count"] == 22
    assert top["metric"]["engagement_total"] == 9000
    assert out["client_metrics_default"] is True
    assert 0 < len(top["examples"]) <= behaviours.EXAMPLES_PER_BEHAVIOUR
    assert "tiktok" in top["platforms"]
    # behaviour is a humanised one-liner, not the raw slug
    assert "economy_sapa_hustle" not in top["behaviour"]
    # examples carry valid post links; a non-url is dropped to empty
    urls = [e["url"] for e in top["examples"]]
    assert any(u.startswith("https://") for u in urls)
    assert all(u == "" or u.startswith("http") for u in urls)
    assert any(e.get("voice_kind") == "comment" for e in top["examples"])


def test_scan_caps_examples_at_three(monkeypatch):
    _patch(monkeypatch)
    out = behaviours.scan_market_behaviours(["ng"], 10)
    sapa = out["behaviours"]["ng"][0]
    assert len(sapa["examples"]) == behaviours.EXAMPLES_PER_BEHAVIOUR


def test_scan_empty_markets():
    out = behaviours.scan_market_behaviours([], 10)
    assert out["behaviours"] == {}


def test_scan_selects_proof_backed_and_caps(monkeypatch):
    # ng has two candidates; sapa has posts, fintech does not. Cap at 1 must keep
    # the proof-backed behaviour, not the higher raw score with no examples.
    topics = [
        {"market": "ng", "query_group": "fintech_mpesa", "trend_score": 0.99, "item_count": 5, "cultural_context": "high score no posts", "headline": "x"},
        {"market": "ng", "query_group": "economy_sapa_hustle", "trend_score": 0.40, "item_count": 140, "cultural_context": "sapa humour as survival", "headline": "y"},
    ]
    monkeypatch.setattr(bq, "latest_trend_date", lambda: FROZEN)
    monkeypatch.setattr(bq, "fetch_market_topics", lambda markets, per_market, trend_date=None: topics)

    def fake_voice(markets, groups, *, per_topic_posts=3, per_topic_comments=5):
        out = []
        for p in POSTS:
            if p["market"] != "ng":
                continue
            hit = set(p["topic_groups"]) & set(groups)
            if not hit:
                continue
            row = dict(p)
            row["topic_group"] = sorted(hit)[0]
            out.append(row)
        return out

    monkeypatch.setattr(bq, "fetch_posts_and_comments_for_topics", fake_voice)
    monkeypatch.setattr(
        bq,
        "fetch_voice_metrics_for_topics",
        lambda markets, groups: {k: v for k, v in VOICE_METRICS.items() if k[0] in markets and k[1] in groups},
    )
    out = behaviours.scan_market_behaviours(["ng"], 1)
    ng = out["behaviours"]["ng"]
    assert len(ng) == 1
    assert ng[0]["query_group"] == "economy_sapa_hustle"
    assert len(ng[0]["examples"]) >= 1


def test_endpoint_hidden_when_flag_off(monkeypatch):
    monkeypatch.delenv("BEHAVIOUR_SCAN_ENABLED", raising=False)
    r = client.get("/api/research/behaviours?markets=ng")
    assert r.status_code == 404


def test_endpoint_returns_scan_when_flag_on(monkeypatch):
    monkeypatch.setenv("BEHAVIOUR_SCAN_ENABLED", "true")
    _patch(monkeypatch)
    r = client.get("/api/research/behaviours?markets=ng,ke")
    assert r.status_code == 200
    data = r.json()
    assert data["markets"] == ["ng", "ke"]
    assert data["behaviours"]["ng"][0]["query_group"] == "economy_sapa_hustle"


def test_endpoint_rejects_bad_market(monkeypatch):
    monkeypatch.setenv("BEHAVIOUR_SCAN_ENABLED", "true")
    r = client.get("/api/research/behaviours?markets=us")
    assert r.status_code == 400


def test_behaviours_rate_limit_has_own_cap(monkeypatch):
    monkeypatch.setenv("BEHAVIOUR_SCAN_ENABLED", "true")
    _patch(monkeypatch)
    main._research_hits.clear()
    ip = "203.0.113.77"
    hdr = {"x-forwarded-for": ip}
    cap = main.RESEARCH_BEHAVIOURS_MAX
    for _ in range(cap):
        r = client.get("/api/research/behaviours?markets=ng", headers=hdr)
        assert r.status_code == 200
    r = client.get("/api/research/behaviours?markets=ng", headers=hdr)
    assert r.status_code == 429
    assert "behaviours" in r.json()["detail"]


def test_focus_scan_examples_boost_gate(monkeypatch):
    monkeypatch.setattr(bq, "latest_trend_date", lambda: FROZEN)
    monkeypatch.setattr(research, "_parallel_gather", lambda *a, **k: [])
    monkeypatch.setattr(
        research.persona_registry,
        "rank_and_filter",
        lambda c, p, **kw: c,
    )

    ev = research.build_research_evidence(
        "audience_neutral",
        ["ng"],
        focus_query_groups=["fashion_ankara_asoebi"],
        focus_note="Ankara drip",
        focus_examples=[
            {"text": "asoebi season", "url": "https://example.com/p1", "platform": "tiktok"},
            {"text": "ankara fit check", "url": "https://example.com/p2", "platform": "instagram"},
            {"text": "owambe drip", "url": "https://example.com/p3", "platform": "tiktok"},
        ],
    )
    assert ev["signal_quality"] == "moderate"
    assert any(r.get("source") == "behaviour_scan" for r in ev["refs"])


def test_focus_narrows_gather_and_carries_note(monkeypatch):
    seen = {}

    def fake_gather(persona, mks, td, start, degraded, progress_key):
        seen["query_groups"] = list(persona.get("query_groups") or [])
        return []

    monkeypatch.setattr(bq, "latest_trend_date", lambda: FROZEN)
    monkeypatch.setattr(research, "_parallel_gather", fake_gather)
    monkeypatch.setattr(research.persona_registry, "rank_and_filter", lambda c, p, **kw: c)
    monkeypatch.setattr(research.persona_registry, "quality_gate", lambda refs, p, **kw: {"level": "thin"})

    ev = research.build_research_evidence(
        "audience_neutral",
        ["ng"],
        focus_query_groups=["economy_sapa_hustle", ""],
        focus_note="Sapa humour as survival language",
    )
    assert seen["query_groups"] == ["economy_sapa_hustle"]
    assert ev["focus_query_groups"] == ["economy_sapa_hustle"]
    assert ev["focus_note"] == "Sapa humour as survival language"


def test_generate_endpoint_forwards_focus(monkeypatch):
    captured = {}

    def fake_run(persona_id, markets, product_frame, *, progress_key=None, focus_query_groups=None, focus_note="", focus_examples=None, **kw):
        captured["focus_query_groups"] = focus_query_groups
        captured["focus_note"] = focus_note
        return {"status": "completed", "doc": {"json": {}, "markdown": "", "sources": []}, "evidence_graph": {}, "signal_quality": "thin"}

    monkeypatch.setattr(research, "run_research_job", fake_run)
    monkeypatch.setattr(research, "persist_job_result", lambda result, parent_artifact_id=None: "ra_test")

    r = client.post(
        "/api/research/generate",
        json={
            "persona_id": "audience_neutral",
            "markets": ["ng"],
            "focus_query_groups": ["economy_sapa_hustle"],
            "focus_note": "Sapa humour as survival language",
        },
    )
    assert r.status_code == 202
    deadline = time.time() + 5
    while time.time() < deadline and "focus_note" not in captured:
        time.sleep(0.02)
    assert captured.get("focus_query_groups") == ["economy_sapa_hustle"]
    assert captured.get("focus_note") == "Sapa humour as survival language"


def test_focus_payload_for_behaviour():
    groups, note = research.focus_payload_for_behaviour(
        {
            "query_group": "economy_sapa_hustle",
            "behaviour": "Sapa humour as survival language",
            "note": "Lead with hustle fatigue",
        }
    )
    assert groups == ["economy_sapa_hustle"]
    assert note == "Sapa humour as survival language [note: Lead with hustle fatigue]"


def test_generate_forwards_parent_artifact(monkeypatch):
    captured = {}

    def fake_run(persona_id, markets, product_frame, *, progress_key=None, focus_query_groups=None, focus_note="", focus_examples=None, **kw):
        return {
            "status": "completed",
            "doc": {"json": {}, "markdown": "", "sources": []},
            "evidence_graph": {},
            "signal_quality": "thin",
        }

    def fake_persist(result, parent_artifact_id=None):
        captured["parent_artifact_id"] = parent_artifact_id
        result["artifact_id"] = "ra_child"
        return "ra_child"

    monkeypatch.setattr(research, "run_research_job", fake_run)
    monkeypatch.setattr(research, "persist_job_result", fake_persist)

    r = client.post(
        "/api/research/generate",
        json={
            "persona_id": "audience_neutral",
            "markets": ["ng"],
            "focus_query_groups": ["economy_sapa_hustle"],
            "focus_note": "Sapa humour",
            "parent_artifact_id": "ra_parent",
        },
    )
    assert r.status_code == 202
    deadline = time.time() + 5
    while time.time() < deadline and "parent_artifact_id" not in captured:
        time.sleep(0.02)
    assert captured.get("parent_artifact_id") == "ra_parent"


def test_batch_generate_endpoint_runs_all_behaviours(monkeypatch):
    """One POST /generate-batch queues N briefs (one rate-limit slot)."""
    calls = []

    def fake_run(
        persona_id,
        markets,
        product_frame,
        *,
        progress_key=None,
        focus_query_groups=None,
        focus_note="",
        focus_examples=None,
        **kw,
    ):
        calls.append(
            {
                "markets": list(markets or []),
                "focus_query_groups": list(focus_query_groups or []),
                "focus_note": focus_note,
            }
        )
        return {
            "status": "completed",
            "doc": {"json": {}, "markdown": "", "sources": []},
            "evidence_graph": {},
            "signal_quality": "moderate",
        }

    monkeypatch.setattr(research, "run_research_job", fake_run)
    monkeypatch.setattr(research, "persist_job_result", lambda result, parent_artifact_id=None: f"ra_{len(calls)}")

    rows = [
        {"market": "za", "query_group": "sport_rugby", "behaviour": "Springboks talk", "signal_topic": "Rugby"},
        {"market": "ng", "query_group": "culture_japa", "behaviour": "Japa rising", "signal_topic": "Japa"},
        {"market": "ke", "query_group": "culture_sheng", "behaviour": "Sheng spreads", "signal_topic": "Sheng"},
    ]
    r = client.post(
        "/api/research/generate-batch",
        json={"persona_id": "audience_neutral", "behaviours": rows},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    deadline = time.time() + 8
    payload = None
    while time.time() < deadline:
        st = client.get(f"/api/research/status?job_id={job_id}")
        payload = st.json()
        if payload.get("status") == "completed" and payload.get("batch_results"):
            break
        time.sleep(0.05)

    assert payload is not None
    assert len(payload.get("batch_results") or []) == len(rows)
    assert len(calls) == len(rows)
    for row, call in zip(rows, calls, strict=True):
        groups, note = research.focus_payload_for_behaviour(row)
        assert call["focus_query_groups"] == groups
        assert call["focus_note"] == note
        assert call["markets"] == [row["market"]]


def test_batch_generate_rate_limit_is_separate_bucket(monkeypatch):
    monkeypatch.setattr(
        research,
        "run_research_job",
        lambda *a, **k: {
            "status": "completed",
            "doc": {"json": {}, "markdown": "", "sources": []},
            "evidence_graph": {},
            "signal_quality": "moderate",
        },
    )
    monkeypatch.setattr(research, "persist_job_result", lambda result, parent_artifact_id=None: "ra_x")
    main._research_hits.clear()
    ip = "203.0.113.88"
    hdr = {"x-forwarded-for": ip}
    row = {"market": "ng", "query_group": "economy_sapa_hustle", "behaviour": "Sapa"}
    cap = main._research_batch_generate_cap()
    for _ in range(cap):
        r = client.post(
            "/api/research/generate-batch",
            json={"persona_id": "audience_neutral", "behaviours": [row]},
            headers=hdr,
        )
        assert r.status_code == 202
    r = client.post(
        "/api/research/generate-batch",
        json={"persona_id": "audience_neutral", "behaviours": [row]},
        headers=hdr,
    )
    assert r.status_code == 429
