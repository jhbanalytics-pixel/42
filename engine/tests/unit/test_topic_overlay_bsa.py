"""The first client overlay is a configuration inside general 42, not a fork of it."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.general_question_plan import build_question_planning_context
from src.analysis.open_intelligence.general_question_policy import build_intake_context
from src.analysis.open_intelligence.general_question_request import normalize_question_request
from src.enrichment import topic_overlays

from tests.unit.test_engine_audience_neutrality import DEMOGRAPHIC_DEFAULT, _strings

ROOT = Path(__file__).resolve().parents[2]
OVERLAY_PATH = ROOT / "configs" / "topic_overlays" / "bsa.yaml"
SOURCE_DOCUMENT = "Brand South Africa Pulse Brief 02092026.docx"
SOURCE_SHA256 = "c6a24db66063de5176d98b2ea6004823ce4446893f44049541d989662997da4a"
SEVEN = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
)
BSA_ALIASES = [
    "Brand South Africa",
    "BrandSA",
    "Brand SA",
    "#BrandSouthAfrica",
    "#BrandSA",
    "brandsouthafrica.com",
    "Nation Brand",
    "State of the Nation Brand",
    "Nation Brand Forum",
]
GSA_ALIASES = [
    "Global South Africans",
    "#GlobalSouthAfricans",
    "South African diaspora",
    "South Africans abroad",
    "expat South Africans",
    "Saffa",
    "Saffas",
    "South African expats in London / Dubai / Sydney / Toronto / Amsterdam",
]
PYP_ALIASES = [
    "Play Your Part",
    "#PlayYourPart",
    "#PlayYourPartSA",
    "Play Your Part ambassador",
    "active citizenship",
    "active citizenry",
]


def general_scope():
    return {
        "client_scope_id": "ogilvy_default",
        "market_scope": ["ke", "ng", "za"],
        "brand_config_id": None,
        "audience_lens_ids": [],
        "theme_id": None,
    }


def bsa_scope(**overrides):
    return {
        **general_scope(),
        "client_scope_id": "bsa_pulse",
        "brand_config_id": "bsa",
        **overrides,
    }


def planning_context(message, *, scope, index):
    request = normalize_question_request(
        {"message": message},
        scope=scope,
        request_id=str(UUID(int=index)),
        admitted_at=datetime(2026, 9, 9, 8, tzinfo=UTC),
        policy_digest="a" * 64,
    )
    return build_question_planning_context(
        request, build_intake_context(request, selected_market="za")
    )


def test_bsa_overlay_loads_through_the_loader_with_exact_entities_dimensions_and_provenance():
    overlay = topic_overlays.load_topic_overlay("bsa")

    assert overlay["contract_version"] == "topic_overlay_v1"
    assert overlay["brand_config_id"] == "bsa"
    assert overlay["client_scope_id"] == "bsa_pulse"
    assert overlay["provenance"] == {
        "source_document": SOURCE_DOCUMENT,
        "source_sha256": SOURCE_SHA256,
    }
    assert list(overlay["entities"]) == ["entity_bsa", "entity_gsa", "entity_pyp"]
    assert overlay["entities"]["entity_bsa"]["property"] == "Brand South Africa"
    assert overlay["entities"]["entity_bsa"]["aliases"] == BSA_ALIASES
    assert overlay["entities"]["entity_gsa"]["property"] == "Global South Africans"
    assert overlay["entities"]["entity_gsa"]["aliases"] == GSA_ALIASES
    assert overlay["entities"]["entity_pyp"]["property"] == "Play Your Part"
    assert overlay["entities"]["entity_pyp"]["aliases"] == PYP_ALIASES
    assert [d["label"] for d in overlay["dimensions"].values()] == [
        "Tourism",
        "Investment",
        "Exports",
        "Talent",
        "Prominence",
    ]
    assert overlay["authorized_markets"] == ["za", "ng", "ke"]
    assert "United States" in overlay["uncovered_markets"]
    assert not set(overlay["uncovered_markets"]) & {"za", "ng", "ke", "South Africa"}
    raw = OVERLAY_PATH.read_text(encoding="utf-8")
    assert "\u2014" not in raw
    assert "\u2013" not in raw
    assert "-" * 2 not in raw
    assert yaml.safe_load(raw)["provenance"]["source_sha256"] == SOURCE_SHA256


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda o: o.update(contract_version="topic_overlay_v0"), "overlay_version_invalid"),
        (
            lambda o: o["provenance"].update(source_document="C:/somewhere/brief.docx"),
            "provenance_invalid",
        ),
        (lambda o: o["provenance"].update(source_sha256="abc"), "provenance_invalid"),
        (lambda o: o.update(authorized_markets=["za", "us"]), "markets_invalid"),
        (
            lambda o: o["audience_framing"].update(default_generation_language="gen_z"),
            "audience_framing_invalid",
        ),
        (
            lambda o: o["entities"]["entity_pyp"].pop("cooccurrence_any"),
            "entity_invalid:entity_pyp",
        ),
        (lambda o: o["entities"]["entity_gsa"].update(aliases=[]), "entity_invalid:entity_gsa"),
        (lambda o: o["dimensions"]["tourism"].pop("terms"), "dimension_invalid:tourism"),
    ],
)
def test_loader_refuses_a_changed_overlay(tmp_path, mutate, code):
    raw = yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8"))
    mutate(raw)
    (tmp_path / "bsa.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(topic_overlays.TopicOverlayInvalid) as caught:
        topic_overlays.load_topic_overlay("bsa", root=tmp_path)
    assert caught.value.code == code


def test_general_scope_has_no_overlay_and_the_bsa_scope_loads_its_lens():
    assert topic_overlays.overlay_for_scope(general_scope()) is None
    overlay = topic_overlays.overlay_for_scope(bsa_scope())
    assert overlay["overlay_id"] == "bsa"
    with pytest.raises(topic_overlays.TopicOverlayInvalid):
        topic_overlays.overlay_for_scope(bsa_scope(brand_config_id="unknown_brand"))


def test_three_properties_separately_jointly_and_with_their_negative_fixtures():
    overlay = topic_overlays.load_topic_overlay("bsa")
    matches = topic_overlays.entity_matches

    assert matches(overlay, "entity_bsa", "Brand South Africa hosted the Nation Brand Forum.")
    assert matches(overlay, "entity_bsa", "Follow #BrandSA for the update")
    assert not matches(overlay, "entity_bsa", "Who is the biggest brand in South Africa?")
    assert not matches(overlay, "entity_bsa", "Nando's took brand of the year again")

    assert matches(overlay, "entity_gsa", "Global South Africans gathered in London")
    assert matches(overlay, "entity_gsa", "Saffas abroad miss biltong")
    assert not matches(overlay, "entity_gsa", "Global South Academy opens in Accra")
    assert not matches(overlay, "entity_gsa", "a Global South African perspective on trade")
    assert not matches(overlay, "entity_gsa", "Global South Africa trade summit")

    assert matches(overlay, "entity_pyp", "#PlayYourPart this weekend")
    assert matches(overlay, "entity_pyp", "Play Your Part ambassadors met Brand South Africa")
    assert matches(overlay, "entity_pyp", "active citizenship in South Africa is rising")
    assert not matches(overlay, "entity_pyp", "everyone must play your part in the office cleanup")
    assert not matches(overlay, "entity_pyp", "active citizenship matters everywhere")

    for entity_id, fixtures in overlay["negative_fixtures"].items():
        for text in fixtures:
            assert not matches(overlay, entity_id, text), (entity_id, text)

    joint = "Brand South Africa asked Global South Africans to Play Your Part at home"
    assert all(matches(overlay, entity_id, joint) for entity_id in overlay["entities"])
    assert not any(matches(overlay, entity_id, "") for entity_id in overlay["entities"])


def test_audience_neutral_commuting_question_injects_no_generation_language_under_bsa():
    overlay = topic_overlays.load_topic_overlay("bsa")
    context = planning_context(
        "Which commuter routes into Johannesburg changed during the taxi strike?",
        scope=bsa_scope(),
        index=11,
    )
    view = topic_overlays.overlay_planning_view(overlay, context["request"])

    assert {key: context["request"][key] for key in SEVEN} == {
        **bsa_scope(),
        "run_id": "question_" + UUID(int=11).hex,
        "contract_version": "general_cultural_question_v1",
    }
    assert view["audience_framing"] == "none"
    assert view["audience_lens_ids"] == []
    assert view["prevalence_claims_allowed"] is False
    leaked = [text for text in _strings(context) if DEMOGRAPHIC_DEFAULT.search(text)]
    leaked += [text for text in _strings(view) if DEMOGRAPHIC_DEFAULT.search(text)]
    leaked += [text for text in _strings(overlay) if DEMOGRAPHIC_DEFAULT.search(text)]
    assert leaked == []
    assert view["matched_entities"] == []
    assert view["relevant_dimensions"] == []


def test_named_audience_question_keeps_its_requested_lens_without_population_claims():
    overlay = topic_overlays.load_topic_overlay("bsa")
    context = planning_context(
        "What are Gen Z students in Cape Town saying about Play Your Part and jobs in South Africa?",
        scope=bsa_scope(audience_lens_ids=["explicit_gen_z_lens"]),
        index=12,
    )
    view = topic_overlays.overlay_planning_view(overlay, context["request"])

    assert context["request"]["audience_lens_ids"] == ["explicit_gen_z_lens"]
    assert "Gen Z students" in context["request"]["question"]
    assert view["audience_framing"] == "requested_sample"
    assert view["audience_lens_ids"] == ["explicit_gen_z_lens"]
    assert view["prevalence_claims_allowed"] is False
    assert view["matched_entities"] == ["entity_pyp"]
    assert view["relevant_dimensions"] == ["Tourism", "Talent"]
    assert view["uncovered_markets"] == overlay["uncovered_markets"]
    assert (view["run_id"], view["contract_version"], view["client_scope_id"]) == (
        "question_" + UUID(int=12).hex,
        "general_cultural_question_v1",
        "bsa_pulse",
    )
    neutral = copy.deepcopy(context["request"])
    neutral["audience_lens_ids"] = []
    assert topic_overlays.overlay_planning_view(overlay, neutral)["audience_framing"] == "none"


def test_scope_binding_changes_for_every_authoritative_field():
    scope = {
        "client_scope_id": "fixture-a",
        "market_scope": ["za"],
        "brand_config_id": None,
        "audience_lens_ids": [],
        "theme_id": None,
        "run_id": "fixture-run",
        "contract_version": "2.1.0",
    }
    changes = {
        "client_scope_id": "fixture-b",
        "market_scope": ["ng"],
        "brand_config_id": "bsa",
        "audience_lens_ids": ["fixture-lens"],
        "theme_id": "fixture-theme",
        "run_id": "other-run",
        "contract_version": "fixture-new",
    }
    for key, value in changes.items():
        assert canonical_digest({**scope, key: value}) != canonical_digest(scope)
