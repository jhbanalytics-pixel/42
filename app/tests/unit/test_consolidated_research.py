"""LP-R3 consolidated doc and LP-R5 evidence pack."""

import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from src.api import bq, main, research, synth

client = TestClient(main.app)
FROZEN = date(2026, 6, 15)

BEHAVIOURS = [
    {
        "id": "ng:economy_sapa_hustle",
        "market": "ng",
        "query_group": "economy_sapa_hustle",
        "signal_topic": "Sapa hustle",
        "behaviour": "Nigerians turn low-balance jokes into shared hustle humour.",
        "metric": {"post_count": 140, "comment_count": 22, "engagement_total": 9000},
        "examples": [{"text": "sapa is real", "platform": "tiktok", "voice_kind": "post"}],
    },
    {
        "id": "ke:music_gengetone",
        "market": "ke",
        "query_group": "music_gengetone",
        "signal_topic": "Gengetone",
        "behaviour": "Gengetone is the sound of Nairobi street pride right now.",
        "metric": {"post_count": 70, "comment_count": 5, "engagement_total": 800},
        "examples": [],
    },
]

VOICE_ROWS = [
    {
        "market": "ng",
        "topic_group": "economy_sapa_hustle",
        "platform": "tiktok",
        "text": "sapa is real this month",
        "engagement_total": 900,
        "content_type": "post",
        "voice_kind": "post",
        "url": "https://tiktok.com/@a/1",
    },
    {
        "market": "ng",
        "topic_group": "economy_sapa_hustle",
        "platform": "instagram",
        "text": "comment without link",
        "engagement_total": 300,
        "content_type": "instagram_post_comment",
        "voice_kind": "comment",
        "url": "",
    },
]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    main._research_hits.clear()
    with main._research_jobs_lock:
        main._research_jobs.clear()
    yield
    deadline = time.time() + 8
    while time.time() < deadline:
        with main._research_jobs_lock:
            if not main._research_jobs:
                break
        time.sleep(0.02)
    main._research_hits.clear()


def _patch_gather(monkeypatch):
    monkeypatch.setattr(bq, "latest_trend_date", lambda: FROZEN)
    monkeypatch.setattr(bq, "fetch_research_seeds", lambda *a, **k: [])
    monkeypatch.setattr(bq, "fetch_research_digest", lambda *a, **k: None)
    monkeypatch.setattr(bq, "fetch_topic_briefs_for_markets", lambda *a, **k: [])
    monkeypatch.setattr(bq, "fetch_rising_search_terms_for_markets", lambda *a, **k: [])
    monkeypatch.setattr(bq, "fetch_research_posts", lambda *a, **k: [])
    monkeypatch.setattr(bq, "fetch_lexicon_rows_for_markets", lambda *a, **k: [])
    monkeypatch.setattr(bq, "fetch_posts_and_comments_for_topics", lambda *a, **k: list(VOICE_ROWS))
    monkeypatch.setattr(bq, "fetch_comment_threads_for_posts", lambda posts, per_post=10: {})


def test_build_consolidated_evidence_sections(monkeypatch):
    _patch_gather(monkeypatch)
    ev = research.build_consolidated_evidence(
        "audience_neutral",
        BEHAVIOURS,
        trend_date=FROZEN,
    )
    assert ev["consolidated"] is True
    assert len(ev["behaviour_sections"]) == 2
    assert ev["behaviour_count"] == 2
    assert len(ev["refs"]) >= 2
    sec = ev["behaviour_sections"][0]
    assert sec["voice_posts"] or sec["voice_comments"] or sec["refs"]


def test_synthesize_consolidated_includes_cross_cutting(monkeypatch):
    _patch_gather(monkeypatch)
    ev = research.build_consolidated_evidence("audience_neutral", BEHAVIOURS, trend_date=FROZEN)
    doc = {
        "title": "Consolidated Research: Digital Architect",
        "behaviour_sections": [
            {
                "market": "NG",
                "signal_topic": "Sapa hustle",
                "behaviour_read": (
                    "In Nigeria, sapa hustle humour shows creators turning low-balance money pressure "
                    "into peer support and pricing scripts on tech_gemini_ai workflows."
                ),
                "voice_read": (
                    "Posts and comments cite everyday hustle banter and client DM pricing pressure "
                    "across Lagos side-hustle creators in Nigeria."
                ),
                "proof_stats": "140 posts, 22 comments, 9000 engagement on economy_sapa_hustle in Nigeria.",
                "strategic_implication": (
                    "The team should compare client reply routines before deciding which pricing "
                    "friction needs a deeper Nigeria investigation."
                ),
                "evidence_indices": [1],
            },
            {
                "market": "KE",
                "signal_topic": "Gengetone",
                "behaviour_read": (
                    "In Kenya, Gengetone street pride shows Nairobi creators using music culture as "
                    "identity signalling across music_gengetone conversations."
                ),
                "voice_read": (
                    "Posts show Nairobi creators stacking Gengetone sound with street pride cues "
                    "across Kenya street culture."
                ),
                "proof_stats": "70 posts, 5 comments, 800 engagement on music_gengetone in Kenya.",
                "strategic_implication": (
                    "The team should test whether release-week creator routines explain the Kenya "
                    "Gengetone street-pride signal."
                ),
                "evidence_indices": [1],
            },
        ],
        "cross_cutting": {
            "themes": ["Hustle survival", "Street pride"],
            "summary": (
                "Nigeria sapa hustle and Kenya Gengetone both show creators turning daily pressure "
                "into shareable cultural mechanics across markets."
            ),
        },
    }
    out = synth._validate_consolidated(doc, ev)
    assert out is not None
    assert len(out["behaviour_sections"]) == 2
    assert out.get("cross_cutting", {}).get("summary")


def test_behaviour_sections_number_citations_contiguously(monkeypatch):
    """Citation numbering is 1-based and contiguous across behaviour sections.

    synth renumbers every evidence index as ``ref_start - 1 + idx``, so an
    off-by-one in either bound silently renumbers every citation in the
    rendered doc. build_consolidated_evidence assigns the bounds in a second
    pass over the sections, which is where a drift would appear.
    """
    _patch_gather(monkeypatch)
    ev = research.build_consolidated_evidence("audience_neutral", BEHAVIOURS, trend_date=FROZEN)
    sections = ev["behaviour_sections"]
    assert len(sections) >= 2
    assert sections[0]["ref_start"] == 1
    for section in sections:
        span = len(section["refs"])
        assert section["ref_end"] == section["ref_start"] + span - 1
    for prev, nxt in zip(sections, sections[1:]):
        assert nxt["ref_start"] == prev["ref_end"] + 1


def test_render_evidence_pack_html_post_and_comment_subsections():
    graph = {
        "behaviour_sections": [
            {
                "market": "ng",
                "signal_topic": "Sapa hustle",
                "metric": {"post_count": 140, "comment_count": 22, "engagement_total": 9000},
                "voice_posts": [VOICE_ROWS[0]],
                "voice_comments": [VOICE_ROWS[1]],
            }
        ]
    }
    html = synth.render_evidence_pack_html({"title": "Pack"}, graph)
    assert "42 · Evidence pack" in html
    assert "PULSE" not in html
    assert "pack-post" in html or " · post" in html
    assert "Standalone comments" in html or "pack-comment" in html
    assert "sapa is real" in html
    assert "comment without link" in html
    assert "instagram_post_comment" in html


def test_api_generate_consolidated(monkeypatch):
    _patch_gather(monkeypatch)
    monkeypatch.setattr(
        synth,
        "synthesize_consolidated_research",
        lambda ev: {
            "title": "Consolidated Research: Digital Architect",
            "behaviour_sections": [
                {
                    "market": "NG",
                    "signal_topic": "Sapa hustle",
                    "behaviour_read": (
                        "In Nigeria, sapa hustle humour shows creators turning low-balance money pressure "
                        "into peer support across economy_sapa_hustle workflows."
                    ),
                    "voice_read": (
                        "Posts and comments show everyday hustle banter across Lagos creators in Nigeria."
                    ),
                    "proof_stats": "140 posts, 22 comments, 9000 engagement in Nigeria.",
                    "strategic_implication": (
                        "The team should investigate client reply workflows for Nigeria hustle operators."
                    ),
                    "evidence_indices": [1],
                }
            ],
            "consolidated": True,
        },
    )
    saved = {}

    def _persist(result):
        saved["result"] = result
        result["artifact_id"] = "ra_consol123"
        return "ra_consol123"

    monkeypatch.setattr(research, "persist_consolidated_result", _persist)
    r = client.post(
        "/api/research/generate-consolidated",
        json={"persona_id": "audience_neutral", "behaviours": BEHAVIOURS[:1]},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    payload = None
    deadline = time.time() + 8
    while time.time() < deadline:
        s = client.get("/api/research/status", params={"job_id": job_id})
        body = s.json()
        if not body.get("pending"):
            payload = body
            break
        time.sleep(0.02)
    assert payload is not None
    assert payload.get("consolidated") is True
    assert payload.get("artifact_id") == "ra_consol123"


def test_api_evidence_pack_html(monkeypatch):
    graph = {
        "behaviour_sections": [
            {
                "market": "ng",
                "signal_topic": "Sapa hustle",
                "metric": {"post_count": 140, "comment_count": 22},
                "voice_posts": [VOICE_ROWS[0]],
                "voice_comments": [VOICE_ROWS[1]],
            }
        ]
    }
    from src.api import investigation_scopes

    monkeypatch.setattr(
        bq,
        "get_research_artifact",
        lambda aid: {
            "artifact_id": aid,
            "client_scope_id": investigation_scopes.default_client_scope_id(),
            "doc_json": {"title": "Consolidated pack"},
            "evidence_graph": graph,
        },
    )
    r = client.get("/api/research/ra_0123456789abcdef/evidence-pack.html")
    assert r.status_code == 200
    assert 'filename="42-evidence-ra_012345678.html"' in r.headers["content-disposition"]
    assert "text/html" in r.headers.get("content-type", "")
    assert "pack-post" in r.text or " · post" in r.text
    assert "Standalone comments" in r.text or "pack-comment" in r.text
