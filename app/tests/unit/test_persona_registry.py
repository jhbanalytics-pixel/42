"""Golden tests for the persona registry."""

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from src.api import persona_registry

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "research"
FROZEN_DATE = date(2026, 6, 15)


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    persona_registry._registry_cache = None
    persona_registry._yaml_meta_cache = None
    yield
    persona_registry._registry_cache = None
    persona_registry._yaml_meta_cache = None


def _load_golden():
    return json.loads((FIXTURES / "golden_digital_architect.json").read_text(encoding="utf-8"))


def test_load_registry_one_enabled_three_stubs():
    reg = persona_registry.load_registry(include_disabled=True)
    assert reg["audience_neutral"]["enabled"] is True
    assert reg["digital_architect_genz"]["enabled"] is False
    assert reg["hustle_economy_genz"]["enabled"] is False
    assert reg["ai_adopter_genz"]["enabled"] is False
    enabled = persona_registry.load_registry()
    assert set(enabled.keys()) == {"audience_neutral"}


def test_list_enabled_personas_exposes_picker_fields():
    listed = persona_registry.list_enabled_personas()
    assert len(listed) == 1
    p = listed[0]
    assert p["id"] == "audience_neutral"
    assert p["label"] == "Audience neutral"
    assert "no inferred demographic lens" in p["description"]
    assert p["product_frame"] == ""
    assert p["product_frame_options"] == []
    assert "tech_gemini_ai" in p["query_groups"]
    assert p["default_markets"] == ["ke", "ng"]


def test_resolve_persona_exact_and_alias():
    assert persona_registry.resolve_persona("audience_neutral")["id"] == "audience_neutral"
    hit = persona_registry.resolve_persona("audience neutral")
    assert hit is not None
    assert hit["id"] == "audience_neutral"
    assert persona_registry.resolve_persona("digital_architect_genz") is None


def test_resolve_persona_fail_closed_on_unknown():
    assert persona_registry.resolve_persona("random persona xyz") is None
    assert persona_registry.resolve_persona("hustle economy gen z") is None


def test_rank_and_filter_stable_order_golden():
    golden = _load_golden()
    persona = persona_registry.load_registry(include_disabled=True)[golden["persona_id"]]
    candidates = []
    for b in golden["briefs"]:
        candidates.append(
            {
                "ref_type": "brief",
                "market": b["market"],
                "query_group": b["query_group"],
                "text": " ".join([b["headline"], b["trend_synthesis"]]),
                "headline": b["headline"],
            }
        )
    for t in golden["rising_terms"]:
        candidates.append(
            {
                "ref_type": "google_trends_rising",
                "market": t["market"],
                "query_group": t["query_group"],
                "term": t["query_group"],
                "snippet": t["query_group"],
            }
        )
    refs = persona_registry.rank_and_filter(candidates, persona)
    assert len(refs) >= 2
    keys = [
        f"{r['ref_type']}|{r['market']}|{r.get('query_group','')}|{round(r.get('relevance_score', 0), 4)}"
        for r in refs
    ]
    digest = hashlib.sha256("\n".join(keys).encode()).hexdigest()
    assert digest == "b73af615f3d7b406e08b92284858a0ab87988a42ac9b147b816468ac8f961112"


def test_quality_gate_thin_strips_lanes():
    persona = persona_registry.get_persona("audience_neutral")
    thin_refs = [{"ref_type": "brief", "market": "ke", "relevance_score": 0.6}] * 5
    gate = persona_registry.quality_gate(thin_refs, persona)
    assert gate["level"] == "thin"
    assert gate["allow_inference"] is False
    assert gate["allow_positioning"] is False


def test_quality_gate_focus_scoped_allows_inference_with_scan_proof():
    persona = persona_registry.get_persona("audience_neutral")
    refs = [
        {
            "ref_type": "post",
            "market": "ng",
            "query_group": "fashion_ankara_asoebi",
            "relevance_score": 0.88,
            "text": "ankara drip",
            "source": "behaviour_scan",
        }
        for _ in range(3)
    ] + [{"ref_type": "brief", "market": "ng", "relevance_score": 0.7, "text": "brief line"}]
    gate = persona_registry.quality_gate(refs, persona, focus_scoped=True)
    assert gate["level"] == "moderate"
    assert gate["allow_inference"] is True


def test_quality_gate_moderate_between_floors(monkeypatch):
    monkeypatch.setenv("RESEARCH_EVIDENCE_FLOOR", "12")
    persona_registry.THIN_FLOOR = persona_registry.evidence_floor()
    persona_registry.RESEARCH_EVIDENCE_FLOOR = persona_registry.THIN_FLOOR
    persona = persona_registry.get_persona("audience_neutral")
    refs = [
        {"ref_type": "brief", "market": "ke", "relevance_score": 0.7, "text": "signal"}
        for _ in range(10)
    ]
    gate = persona_registry.quality_gate(refs, persona)
    assert gate["level"] == "moderate"
    assert gate["allow_inference"] is True
    assert gate["allow_positioning"] is False


def test_quality_gate_strong_allows_lanes(monkeypatch):
    monkeypatch.setenv("RESEARCH_EVIDENCE_FLOOR", "12")
    persona_registry.THIN_FLOOR = persona_registry.evidence_floor()
    persona_registry.RESEARCH_EVIDENCE_FLOOR = persona_registry.THIN_FLOOR
    persona = persona_registry.get_persona("audience_neutral")
    refs = []
    for i in range(12):
        refs.append(
            {
                "ref_type": "brief",
                "market": "ke",
                "query_group": "fintech_mpesa",
                "relevance_score": 0.9,
                "text": "mpesa hustle gemini",
            }
        )
    refs.append(
        {
            "ref_type": "google_trends_rising",
            "market": "ng",
            "query_group": "tech_gemini_ai",
            "relevance_score": 0.85,
        }
    )
    refs.append(
        {
            "ref_type": "brief",
            "market": "ng",
            "query_group": "economy_hustle",
            "relevance_score": 0.88,
            "text": "hustle ng",
        }
    )
    gate = persona_registry.quality_gate(refs, persona)
    assert gate["level"] == "strong"
    assert gate["allow_activation"] is True
