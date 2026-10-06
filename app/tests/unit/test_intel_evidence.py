"""LP-R-INTEL: intelligence-first evidence gather, rank quotas, synthesis validation."""

from pathlib import Path

import pytest

from src.api import persona_registry, research, synth

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "research"


@pytest.fixture(autouse=True)
def _reset_registry():
    persona_registry._registry_cache = None
    yield
    persona_registry._registry_cache = None


def _persona():
    return persona_registry.get_persona("audience_neutral")


def test_rank_and_filter_reserves_engine_before_voice():
    persona = _persona()
    candidates = []
    for i in range(20):
        candidates.append(
            {
                "ref_type": "post",
                "market": "ng",
                "query_group": "tech_gemini_ai",
                "text": f"tiktok snippet {i}",
                "relevance_score": 0.95,
            }
        )
    for i in range(4):
        candidates.append(
            {
                "ref_type": "brief",
                "market": "ng",
                "query_group": "tech_gemini_ai",
                "headline": f"Brief headline {i}",
                "trend_synthesis": "Gemini hustle synthesis line.",
                "relevance_score": 0.6,
            }
        )
    candidates.append(
        {
            "ref_type": "seed",
            "market": "ng",
            "behaviour": "Seed behaviour on Gemini workflows",
            "relevance_score": 0.58,
        }
    )
    refs = persona_registry.rank_and_filter(candidates, persona)
    types = [r.get("ref_type") for r in refs]
    assert types.count("brief") >= 3
    assert types.count("seed") >= 1
    assert types.index("brief") < types.index("post")


def test_rank_and_filter_consolidated_floors():
    persona = _persona()
    candidates = []
    for i in range(8):
        candidates.append({"ref_type": "brief", "market": "ng", "query_group": "tech_gemini_ai", "text": f"b{i}"})
    for i in range(6):
        candidates.append({"ref_type": "seed", "market": "ng", "behaviour": f"s{i}"})
    for i in range(4):
        candidates.append(
            {
                "ref_type": "google_trends_rising",
                "market": "ng",
                "query_group": "tech_gemini_ai",
                "headline": f"Rising search {i}",
                "text": f"Search velocity {0.5 + i * 0.1}",
                "search_velocity_score": 0.5 + i * 0.1,
            }
        )
    for i in range(20):
        candidates.append({"ref_type": "post", "market": "ng", "text": f"p{i}", "platform": "tiktok"})
    refs = persona_registry.rank_and_filter(candidates, persona, consolidated=True)
    types = [r.get("ref_type") for r in refs]
    assert types.count("brief") >= 8
    assert types.count("seed") >= 5
    assert types.count("google_trends_rising") >= 3
    assert types.count("post") >= 5


def test_rank_and_filter_reserves_comments_when_posts_dominate():
    persona = _persona()
    candidates = []
    for i in range(20):
        candidates.append(
            {
                "ref_type": "post",
                "market": "ng",
                "query_group": "diaspora_japa",
                "text": f"high scoring post {i}",
                "relevance_score": 0.95,
            }
        )
    for i in range(8):
        candidates.append(
            {
                "ref_type": "comment",
                "market": "ng",
                "query_group": "diaspora_japa",
                "text": f"comment proof {i}",
                "voice_kind": "comment",
                "relevance_score": 0.4,
            }
        )
    refs = persona_registry.rank_and_filter(candidates, persona)
    assert any(r.get("ref_type") == "comment" for r in refs)
    assert sum(1 for r in refs if r.get("ref_type") == "comment") >= 2


def test_validate_research_rejects_post_only_objective():
    refs = [
        {"ref_type": "post", "market": "ng", "query_group": "tech_gemini_ai", "text": "nakamura tokyo drip"},
        {"ref_type": "post", "market": "ng", "query_group": "tech_gemini_ai", "text": "another tiktok"},
        {"ref_type": "brief", "market": "ng", "query_group": "tech_gemini_ai", "headline": "Gemini hustle", "trend_synthesis": "Search velocity 0.72"},
    ]
    bad = {
        "title": "Test",
        "grounded": {
            "objective": {
                "text": "In Nigeria, creators post about nakamura tokyo with no engine read.",
                "evidence_indices": [1, 2],
            },
            "user_insights": [],
        },
    }
    assert synth._validate_research(bad, refs) is None


def test_validate_research_accepts_engine_led_citations():
    refs = [
        {"ref_type": "post", "market": "ng", "query_group": "tech_gemini_ai", "text": "voice proof post"},
        {"ref_type": "brief", "market": "ng", "query_group": "tech_gemini_ai", "headline": "Gemini hustle", "trend_synthesis": "Search velocity 0.72 on tech_gemini_ai"},
        {"ref_type": "seed", "market": "ng", "behaviour": "Gemini pricing scripts in Lagos side hustle"},
    ]
    good = {
        "title": "Test",
        "grounded": {
            "objective": {
                "text": (
                    "In Nigeria, Gemini hustle workflows show search velocity 0.72 on tech_gemini_ai "
                    "with seed behaviours on pricing scripts."
                ),
                "evidence_indices": [2, 3],
            },
            "user_insights": [
                {
                    "text": (
                        "In Nigeria, tech_gemini_ai hustle creators use Gemini AI Mode for client pricing "
                        "with search velocity 0.72."
                    ),
                    "evidence_indices": [2],
                }
            ],
        },
    }
    out = synth._validate_research(good, refs)
    assert out is not None
    assert out["grounded"]["objective"]["evidence_indices"] == [2, 3]


def test_order_section_refs_brief_before_voice():
    engine = [
        {"ref_type": "brief", "market": "ng", "query_group": "economy_sapa_hustle", "headline": "Sapa read"},
        {"ref_type": "seed", "market": "ng", "behaviour": "Sapa humour"},
        {"ref_type": "google_trends_rising", "market": "ng", "query_group": "economy_sapa_hustle"},
    ]
    voice = [
        {"ref_type": "post", "market": "ng", "query_group": "economy_sapa_hustle", "text": "sapa is real", "platform": "tiktok"},
    ]
    ordered = research._order_section_refs(engine, voice, "economy_sapa_hustle", "ng")
    types = [r.get("ref_type") for r in ordered]
    assert types[0] == "brief"
    assert "post" in types
    assert types.index("brief") < types.index("post")


def test_seed_candidates_no_longer_drop_untagged_seeds():
    persona = {"seed_tags": ["mpesa"]}
    seeds = [{"markets": ["ke"], "behaviour": "Gengetone street pride", "the_shift": "Sound shift"}]
    out = research._seed_candidates(seeds, persona)
    assert len(out) == 1
    assert out[0]["ref_type"] == "seed"
