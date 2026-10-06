"""Golden tests for research synthesis validation and evidence payload."""

import json
from datetime import date
from pathlib import Path

import pytest

from src.api import persona_registry, research, synth

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "research"
FROZEN = date(2026, 6, 15)


@pytest.fixture(autouse=True)
def _reset_registry():
    persona_registry._registry_cache = None
    yield
    persona_registry._registry_cache = None


def _golden():
    return json.loads((FIXTURES / "golden_digital_architect.json").read_text(encoding="utf-8"))


def _golden_refs(monkeypatch):
    g = _golden()
    monkeypatch.setattr(research.bq, "latest_trend_date", lambda: FROZEN)
    monkeypatch.setattr(research.bq, "fetch_research_seeds", lambda *a, **k: g["seeds"])
    monkeypatch.setattr(research.bq, "fetch_research_digest", lambda *a, **k: g["digest"])
    monkeypatch.setattr(
        research.bq,
        "fetch_topic_briefs_for_markets",
        lambda groups, markets, trend_date=None: [
            b for b in g["briefs"] if b["market"] in {m.lower() for m in markets}
        ],
    )
    monkeypatch.setattr(
        research.bq,
        "fetch_rising_search_terms_for_markets",
        lambda markets, anchors, trend_date=None: [
            t for t in g["rising_terms"] if t["market"] in {m.lower() for m in markets}
        ],
    )
    monkeypatch.setattr(
        research.bq,
        "fetch_research_posts",
        lambda markets, groups, limit=30: [p for p in g["posts"] if p["market"] in markets],
    )
    monkeypatch.setattr(research.bq, "fetch_posts_and_comments_for_topics", lambda *a, **k: [])
    monkeypatch.setattr(research.bq, "fetch_comment_threads_for_posts", lambda posts, per_post=10: {})
    monkeypatch.setattr(
        research.bq,
        "fetch_lexicon_rows_for_markets",
        lambda markets: [r for r in g["lexicon"] if r["market"] in {m.lower() for m in markets}],
    )
    ev = research.build_research_evidence("audience_neutral", ["ke", "ng"], trend_date=FROZEN)
    return ev["refs"]


def test_evidence_blob_includes_metrics(monkeypatch):
    refs = _golden_refs(monkeypatch)
    blob = synth._research_evidence_blob(refs)
    assert "search_velocity=" in blob or "metrics=" in blob
    assert "engagement=" in blob or "fintech_mpesa" in blob
    assert "headline:" in blob or "synthesis:" in blob


def test_validate_research_rejects_generic_response(monkeypatch):
    refs = _golden_refs(monkeypatch)
    bad = {
        "title": "Market Research Perspective: test",
        "grounded": {
            "behavioural_synthesis": [
                {
                    "text": (
                        "In Nigeria, sapa hustle creators use mobile tools for client "
                        "pricing scripts with search velocity 0.72 on tech_gemini_ai."
                    ),
                    "evidence_indices": [1],
                }
            ]
        },
        "activation": {
            "responses": [
                {
                    "market": "NG",
                    "response": "Write funny social media captions with humor for a viral campaign.",
                }
            ]
        },
    }
    out = synth._validate_research(bad, refs)
    assert out is not None
    assert out["activation"]["responses"] == []


def test_validate_research_accepts_grounded_response(monkeypatch):
    refs = _golden_refs(monkeypatch)
    brief_idx = next(
        i for i, r in enumerate(refs, start=1) if r.get("query_group") == "tech_gemini_ai"
    )
    good = {
        "title": "Market Research Perspective: test",
        "grounded": {
            "behavioural_synthesis": [
                {
                    "text": (
                        "In Nigeria, sapa hustle creators route mobile planning into client "
                        f"DM pricing; search velocity 0.72 on tech_gemini_ai ref [{brief_idx}]."
                    ),
                    "evidence_indices": [brief_idx],
                }
            ]
        },
        "activation": {
            "responses": [
                {
                    "market": "NG",
                    "user_friction": (
                        "Lagos side-hustle creators need to price gigs and reply to clients "
                        "while sapa pressure keeps cash flow tight."
                    ),
                    "behaviour_trigger": "tech_gemini_ai hustle workflows and sapa pricing scripts",
                    "response": (
                        "Run a Lagos creator diary around tech_gemini_ai hustle workflows and sapa "
                        "pricing scripts, then compare three client reply routines before deciding "
                        "which cash-flow friction needs deeper investigation."
                    ),
                    "evidence_indices": [brief_idx],
                }
            ]
        },
    }
    out = synth._validate_research(good, refs)
    assert out is not None
    assert len(out["activation"]["responses"]) == 1
    response = out["activation"]["responses"][0]["response"].lower()
    assert "tech_gemini_ai" in response


def test_grounded_responses_reference_query_groups(monkeypatch):
    refs = _golden_refs(monkeypatch)
    qgroups = {r.get("query_group") for r in refs if r.get("query_group")}
    assert qgroups

    brief_ref = next(r for r in refs if r.get("ref_type") == "brief" and r.get("query_group"))
    idx = refs.index(brief_ref) + 1
    qg = brief_ref["query_group"]

    doc = {
        "title": "Market Research Perspective: Audience neutral",
        "grounded": {
            "behavioural_synthesis": [
                {
                    "text": (
                        f"In {brief_ref['market'].upper()}, {qg.replace('_', ' ')} shows "
                        f"search velocity {brief_ref.get('search_velocity_score', 0.6):.2f} "
                        "as creators stack mobile money with AI craft tools."
                    ),
                    "evidence_indices": [idx],
                }
            ]
        },
        "activation": {
            "responses": [
                {
                    "market": brief_ref["market"].upper(),
                    "user_friction": (
                        "A hustle operator needs to price gigs, draft outreach, and manage "
                        "cash flow without sounding corporate."
                    ),
                    "behaviour_trigger": f"{qg} and mobile-money cash-flow framing",
                    "response": (
                        f"Observe three hustle operators in {brief_ref['market'].upper()} and map "
                        f"how the {qg} trend changes pricing, outreach, and mobile-money cash flow. "
                        "Use the result to decide which friction needs a deeper evidence plan."
                    ),
                    "evidence_indices": [idx],
                }
            ]
        },
    }
    out = synth._validate_research(doc, refs)
    assert out is not None
    responses = out["activation"]["responses"]
    assert len(responses) == 1
    blob = responses[0]["response"].lower()
    assert qg.lower() in blob or qg.replace("_", " ").lower() in blob


def test_quality_gate_blocks_activation_without_corroboration():
    persona = persona_registry.get_persona("audience_neutral")
    refs = [
        {"ref_type": "brief", "market": "ke", "relevance_score": 0.7, "text": "signal"}
        for _ in range(10)
    ]
    gate = persona_registry.quality_gate(refs, persona)
    assert gate["level"] == "moderate"
    assert gate["allow_inference"] is True
    assert gate["allow_activation"] is False
    assert gate["strong_corroboration"] is False


def test_strip_bq_scalars_from_prose():
    raw = (
        "Nigeria hustle signal shows trend_score=0.3751 and velocity=0.0749 on "
        "tech_gemini_ai while creators stack mobile money."
    )
    cleaned = synth._strip_bq_scalars(raw)
    assert "trend_score=" not in cleaned
    assert "velocity=" not in cleaned
    assert "tech_gemini_ai" in cleaned


def test_validate_research_rejects_invented_product_names(monkeypatch):
    refs = _golden_refs(monkeypatch)
    brief_idx = next(
        i for i, r in enumerate(refs, start=1) if r.get("query_group") == "tech_gemini_ai"
    )
    bad = {
        "title": "Market Research Perspective: test",
        "grounded": {
            "behavioural_synthesis": [
                {
                    "text": (
                        "In Nigeria, creators use Gemini Nano Banana for hustle pricing "
                        f"with search velocity 0.72 on tech_gemini_ai ref [{brief_idx}]."
                    ),
                    "evidence_indices": [brief_idx],
                }
            ]
        },
    }
    out = synth._validate_research(bad, refs)
    assert out is None


def test_validate_research_dedupes_evidence_indices(monkeypatch):
    refs = _golden_refs(monkeypatch)
    brief_idx = next(
        i for i, r in enumerate(refs, start=1) if r.get("query_group") == "tech_gemini_ai"
    )
    doc = {
        "title": "Market Research Perspective: test",
        "grounded": {
            "behavioural_synthesis": [
                {
                    "text": (
                        "In Nigeria, sapa hustle creators route Google AI Mode into client "
                        f"DM pricing; search velocity 0.72 on tech_gemini_ai ref [{brief_idx}]."
                    ),
                    "evidence_indices": [brief_idx, brief_idx],
                }
            ]
        },
    }
    out = synth._validate_research(doc, refs)
    assert out is not None
    indices = out["grounded"]["user_insights"][0]["evidence_indices"]
    assert indices == [brief_idx]


def test_validate_research_rejects_response_rephrasing_positioning(monkeypatch):
    refs = _golden_refs(monkeypatch)
    brief_idx = next(
        i for i, r in enumerate(refs, start=1) if r.get("query_group") == "tech_gemini_ai"
    )
    positioning = (
        "Shift Nigeria and Kenya hustle operators from sapa survival framing toward "
        "daily pricing routines grounded in tech_gemini_ai mobile money "
        "workflows and fintech_mpesa cash-flow mechanics across both markets."
    )
    doc = {
        "title": "Market Research Perspective: test",
        "grounded": {
            "behavioural_synthesis": [
                {
                    "text": (
                        "In Nigeria, sapa hustle creators route mobile planning into client "
                        f"DM pricing; search velocity 0.72 on tech_gemini_ai ref [{brief_idx}]."
                    ),
                    "evidence_indices": [brief_idx],
                }
            ]
        },
        "inference": {"goal": positioning},
        "activation": {
            "responses": [
                {
                    "market": "KE",
                    "user_friction": "Kenya operators need a clearer view of pricing pressure.",
                    "behaviour_trigger": "tech_gemini_ai mobile money workflows",
                    "response": positioning,
                    "evidence_indices": [brief_idx],
                }
            ]
        },
    }
    out = synth._validate_research(doc, refs)
    assert out is not None
    assert out["activation"]["responses"] == []


def test_validate_research_rejects_unsupported_audience_claim(monkeypatch):
    refs = _golden_refs(monkeypatch)
    brief_idx = next(
        i for i, r in enumerate(refs, start=1) if r.get("query_group") == "fintech_mpesa"
    )
    doc = {
        "title": "Market Research Perspective: test",
        "grounded": {
            "behavioural_synthesis": [
                {
                    "text": (
                        "In Kenya, fintech_mpesa balance humour shows Gen Z turning low-balance "
                        f"money pressure into peer support, anchored by ref [{brief_idx}]."
                    ),
                    "evidence_indices": [brief_idx],
                }
            ]
        },
    }
    out = synth._validate_research(doc, refs)
    assert out is None


def test_research_ignores_requested_product_frame():
    assert synth._research_allowed_products({"product_frame": "Any vendor product"}) == []


def test_validate_research_drops_legacy_storyboard(monkeypatch):
    refs = _golden_refs(monkeypatch)
    brief_idx = next(i for i, r in enumerate(refs, start=1) if r.get("query_group") == "tech_gemini_ai")
    base = {
        "title": "Market Research Perspective: test",
        "grounded": {
            "user_insights": [
                {
                    "text": (
                        "In Nigeria, sapa hustle creators use mobile planning for client "
                        "pricing scripts with search velocity 0.72 on tech_gemini_ai."
                    ),
                    "evidence_indices": [brief_idx],
                }
            ],
        },
        "inference": {},
        "activation": {
            "responses": [],
            "storyboard": {
                "friction": (
                    "Nairobi commuters lose time when fare apps fail mid-route and "
                    "they cannot compare options quickly."
                ),
                "interaction": (
                    "A planning service compares routes, fare options, and timing in one "
                    "ask while the user is still on the matatu."
                ),
                "resolution": (
                    "The user picks the cheapest reliable route before boarding and "
                    "avoids the sapa surprise at the end of the trip."
                ),
                "why_it_works": (
                    "It turns tech_gemini_ai hustle stress into a practical commute win "
                    "grounded in Nigeria daily money pressure."
                ),
                "evidence_indices": [brief_idx],
            },
        },
    }
    out = synth._validate_research(base, refs)
    assert out is not None
    assert "storyboard" not in out["activation"]


def test_render_research_html_includes_title(monkeypatch):
    # Called for the patching, not the return value: _golden_refs stubs six bq
    # readers, so dropping the call would leave this test hitting BigQuery.
    _golden_refs(monkeypatch)
    doc = {
        "title": "Market Research Perspective: Digital Architect",
        "grounded": {"user_insights": [{"text": "Nigeria hustle signal.", "evidence_indices": [1]}]},
    }
    html = synth.render_research_html(doc, persona_label="Digital Architect", markets=["ng"])
    assert "Digital Architect" in html
    assert "<html" in html.lower()
    assert "Nigeria hustle signal." in html
    assert '<header class="hero">' in html
    assert "42 · Market research" in html
    assert "PULSE" not in html
    assert '<section class="card lane-signal">' in html
    assert "Know the user" in html


def test_research_prompts_and_thin_fallback_use_42_and_neutral_provenance():
    prompts = synth.RESEARCH_PROMPT_HEAD + synth.CONSOLIDATED_PROMPT_HEAD
    thin = synth.build_thin_research_doc(
        {
            "persona_label": "Strategist",
            "refs": [{"title": "Observed source"}],
            "quality_gate": {"level": "partial", "allow_inference": False},
        }
    )
    note = thin["json"]["grounded"]["note"]

    assert "42 research desk" in prompts
    assert "PULSE research desk" not in prompts
    assert "Need corroborating topic or search evidence" in note


def test_render_research_html_storyboard_and_refs(monkeypatch):
    refs = _golden_refs(monkeypatch)
    doc = {
        "title": "Storyboard brief",
        "grounded": {"objective": {"text": "Objective line.", "evidence_indices": [1]}},
        "activation": {
            "storyboard": {
                "friction": "User feels stuck.",
                "interaction": "Opens Google AI Mode.",
                "resolution": "Creates uplifting content.",
                "why_it_works": "Turns frustration into action.",
                "evidence_indices": [1],
            }
        },
        "_refs": refs[:1],
    }
    html = synth.render_research_html(doc, persona_label="Digital Architect", markets=["ng"])
    assert "storyboard-table" in html
    assert "User feels stuck." in html
    assert 'id="ref-1"' in html
    assert "Evidence sources" in html


def test_render_research_html_grouped_evidence_lists_all_refs():
    refs = [
        {"text": "Cited brief signal about sapa hustle in Nigeria."},
        {"headline": "Uncited headline only"},
    ]
    doc = {
        "title": "Grouped evidence export",
        "grounded": {
            "user_insights": [
                {"text": "Nigeria sapa pressure shapes daily hustle.", "evidence_indices": [1]},
            ]
        },
        "_refs": refs,
    }
    html = synth.render_research_html(doc)
    assert 'id="ref-1"' in html
    assert "Cited brief signal" in html
    assert "Engine signal" in html
    assert "Search &amp; reach" not in html


def test_render_research_html_grouped_voice_with_comments():
    refs = [
        {
            "ref_type": "brief",
            "market": "ng",
            "query_group": "diaspora_japa",
            "headline": "Japa migration read",
            "trend_synthesis": "Nigerians discuss exit routes abroad.",
        },
        {
            "ref_type": "post",
            "market": "ng",
            "query_group": "diaspora_japa",
            "platform": "tiktok",
            "text": "japa checklist thread",
            "url": "https://tiktok.com/@a/1",
            "engagement_total": 900,
        },
        {
            "ref_type": "comment",
            "market": "ng",
            "query_group": "diaspora_japa",
            "platform": "tiktok",
            "voice_kind": "comment",
            "text": "same here, visa stress is real",
            "url": "",
            "engagement_total": 120,
        },
        {
            "ref_type": "google_trends_rising",
            "market": "ng",
            "query_group": "diaspora_japa",
            "headline": "Rising search: diaspora japa",
            "text": "Search velocity 0.72",
        },
    ]
    doc = {
        "title": "Grouped evidence export",
        "grounded": {
            "objective": {
                "text": "Japa migration is a high-velocity topic in Nigeria.",
                "evidence_indices": [1],
            }
        },
        "_refs": refs,
    }
    html = synth.render_research_html(doc)
    assert "Engine signal" in html
    assert "Voice proof" in html
    assert "Search &amp; reach" in html
    assert "Comments" in html
    assert "Comment" in html
    assert "same here, visa stress is real" in html
    assert "sources · 1 engine · 2 voice · 1 search" in html
