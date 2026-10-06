"""The compiled client overlay: entities, purpose, dimensions, noise, lens and kernels."""

from __future__ import annotations

import copy
import math
from types import MappingProxyType

import pytest
from src.analysis.open_intelligence import client_overlay as co
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.enrichment import topic_overlays

from tests.unit.test_topic_overlay_bsa import bsa_scope, general_scope

BRIEF = "Brand South Africa Pulse Brief 02092026.docx"
NOISE_DEFINITION = (
    "Strip betting and odds accounts, crypto and forex promotion, ticket resale, adult "
    "content, giveaway and follow-farming, and pure fixture or scoreline posts where South "
    "Africa is only a team name."
)
PURPOSE_DEFINITION = (
    "No political-party, electoral or voter-targeting use cases in the demo, and no "
    "individual profiling."
)
OBSERVED = "2026-09-10T08:00:00Z"
VERIFIED_HANDLE = {
    "entity_id": "entity_bsa",
    "platform": "x",
    "handle": "@fixture_official",
    "observed_at": OBSERVED,
    "evidence_ref": "b01-fixture-verification",
}
AMBASSADOR = {
    "ref": "pyp_ambassador_fixture",
    "entity_id": "entity_pyp",
    "handle": "@fixture_ambassador",
    "observed_at": OBSERVED,
    "evidence_ref": "b01-fixture-verification",
}
# The protected client input outside the repository holds this shape with the values B01
# has not supplied; the compiler refuses every placeholder form it can carry.
PLACEHOLDER_INPUT = {
    "verified_handles": [
        {**VERIFIED_HANDLE, "handle": "@TBC_official_handle"},
    ],
    "ambassador_refs": [
        {**AMBASSADOR, "handle": "@placeholder"},
    ],
}

# Each row: dimension, distinction, positive texts, negative texts (same dimension, other
# distinction or no distinction). The general classifier control in test_topic_classifier
# runs every text below through the default topic groups with and without the overlay.
BSA_DIMENSION_FIXTURES = [
    (
        "tourism",
        "travel_intent",
        [
            "Planning a trip to Cape Town next March, is South Africa safe to visit?",
            "Thinking of going to the Garden Route, any itinerary tips?",
        ],
        [
            "We stayed at the Kruger lodge, 4/5 on Tripadvisor, would recommend.",
            "Table Mountain cable car was closed for wind on Tuesday.",
        ],
    ),
    (
        "tourism",
        "review",
        [
            "We stayed at the Kruger lodge, 4/5 on Tripadvisor, would recommend.",
            "Booking.com review: the Drakensberg cabin was spotless, five-star hosts.",
        ],
        [
            "Planning a trip to Cape Town next March, is South Africa safe to visit?",
            "New flight routes and new air links to Cape Town announced.",
        ],
    ),
    (
        "talent",
        "inbound",
        [
            "Moving to South Africa on a critical skills visa in January.",
            "Jobs in South Africa for remote engineers, work permit sorted.",
        ],
        [
            "Brain drain again: South Africans leaving for Dubai in record numbers.",
            "University and research rankings improved for two campuses.",
        ],
    ),
    (
        "talent",
        "outbound",
        [
            "Brain drain again: South Africans leaving for Dubai in record numbers.",
            "Semigration to the Western Cape and emigration to Australia both rose.",
        ],
        [
            "Moving to South Africa on a critical skills visa in January.",
            "University and research rankings improved for two campuses.",
        ],
    ),
    (
        "prominence",
        "mention_share",
        [
            "Share of voice for South Africa rose during the G20 hosting year.",
            "How often is South Africa mentioned in the BRICS coverage?",
        ],
        [
            "Reach and impressions on the Springboks tour doubled.",
            "The African Union summit opened in Addis.",
        ],
    ),
    (
        "prominence",
        "reach",
        [
            "Reach and impressions on the Springboks tour doubled.",
            "Viewership for the Amapiano set peaked at the ICJ recess.",
        ],
        [
            "Share of voice for South Africa rose during the G20 hosting year.",
            "The African Union summit opened in Addis.",
        ],
    ),
    (
        "prominence",
        "comparator",
        [
            "Share of voice for South Africa compared with Kenya and Nigeria.",
            "Benchmarked against Australia and New Zealand on state visits.",
        ],
        [
            "Reach and impressions on the Springboks tour doubled.",
            "The African Union summit opened in Addis.",
        ],
    ),
]

# Each row: exclusion code, texts the definition strips, texts the negative controls keep.
BSA_NOISE_FIXTURES = [
    (
        "betting_spam",
        ["Free bets on the Bokke game tonight, odds 2.1, bet now"],
        ["Gambling regulator opens a betting inquiry after the match-fixing claims"],
    ),
    (
        "crypto_forex_promotion",
        ["Join my forex signals group, guaranteed returns, 100x this month"],
        ["Reserve Bank warns on the rand as the crypto regulation draft lands"],
    ),
    (
        "giveaways",
        ["Giveaway! Retweet to win a Springbok jersey, winner announced Friday"],
        ["Tourism board official competition awards bursaries to ten guides"],
    ),
    (
        "ticket_resale",
        ["Selling 2 tickets for the Test, DM for tickets, viagogo link"],
        ["Sold out: fans travelled from Nairobi as the stadium record fell"],
    ),
    (
        "promotional_adult_content",
        ["OnlyFans link in bio for adult content, sugar daddy wanted"],
        ["Court hears the trafficking case as police arrest two in Durban"],
    ),
    (
        "follow_farming",
        ["Follow for follow, f4f, retweet to gain followers, mutuals only"],
        ["Following the inquiry into the tender, followed by a court date"],
    ),
    (
        "pure_scoreline_noise",
        ["FT: South Africa 22-17 Australia", "Springboks 31-7 Argentina, half-time"],
        ["Springboks fans filled the stadium for the flypast, a tourism boost"],
    ),
]


def document():
    return copy.deepcopy(topic_overlays.load_topic_overlay("bsa"))


def compiled():
    return co.compile_client_overlay(document())


def lens_record(index, **fields):
    return {
        "market": "za",
        "platform": "news",
        "source_row_id": f"row_{index}",
        "content_digest": fields.pop("content_digest", f"{index:064x}"),
        **fields,
    }


def test_bsa_document_compiles_read_only_with_a_digest_over_the_validated_document():
    overlay = compiled()

    assert isinstance(overlay, MappingProxyType)
    assert set(co.CLIENT_FIELDS) <= set(overlay)
    assert overlay["client_overlay_contract_version"] == co.CLIENT_OVERLAY_VERSION
    assert overlay["entities"]["entity_bsa"]["property"] == "Brand South Africa"
    assert overlay["entities"]["entity_bsa"]["rule"] == "exact_phrase_or_handle"
    assert "Global South" in overlay["entities"]["entity_gsa"]["exclusions"]
    assert overlay["entities"]["entity_pyp"]["rule"] == "require_south_africa_cooccurrence"
    assert overlay["verified_handles"] == ()
    assert overlay["ambassador_refs"] == ()
    assert overlay["spokesperson_policy"] == co.SPOKESPERSON_POLICY
    assert overlay["risk_topics"][0] == "safety_and_crime"
    assert overlay["languages"]["domestic"] == ("en", "af", "zu", "xh", "st", "tn")
    assert overlay["languages"]["international"] == ("de", "fr", "nl", "pt", "zh-Hans", "ar")
    assert overlay["comparator_set_ref"] == "bsa_comparator_set_v1"
    assert overlay["search_visibility_policy_ref"] == "bsa_search_visibility_policy_v1"
    assert overlay["purpose_policy"]["policy_version"] == co.PURPOSE_POLICY_VERSION
    assert overlay["lens_policy"]["policy_version"] == co.LENS_POLICY_VERSION
    assert overlay["noise_controls"]["policy_version"] == co.NOISE_POLICY_VERSION
    digest_input = {
        key: value for key, value in co._thaw(overlay).items() if not key.endswith("_digest")
    }
    assert overlay["overlay_digest"] == canonical_digest(digest_input)
    assert overlay["purpose_policy_digest"] == canonical_digest(co._thaw(overlay["purpose_policy"]))
    assert co.compile_client_overlay(document())["overlay_digest"] == overlay["overlay_digest"]
    with pytest.raises(TypeError):
        overlay["entities"] = {}
    with pytest.raises(TypeError):
        overlay["purpose_policy"]["prohibited_purposes"] = ()
    assert isinstance(overlay["purpose_policy"]["prohibited_purposes"], tuple)
    matches = topic_overlays.entity_matches
    assert matches(overlay, "entity_bsa", "Brand South Africa hosted the Nation Brand Forum")
    assert not matches(overlay, "entity_gsa", "a Global South African perspective on trade")
    assert matches(overlay, "entity_pyp", "#PlayYourPart this weekend")


def test_a_changed_field_changes_the_digest_and_the_raw_document_is_never_mutated():
    raw = document()
    before = copy.deepcopy(raw)
    base = co.compile_client_overlay(raw)
    assert raw == before
    changed = document()
    changed["risk_topics"].append("fixture_topic")
    assert co.compile_client_overlay(changed)["overlay_digest"] != base["overlay_digest"]


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda d: d.update(unknown_field=1), "unknown_field:unknown_field"),
        (lambda d: d.pop("risk_topics"), "missing_field:risk_topics"),
        (lambda d: d["entities"]["entity_gsa"].update(aliases=[]), "entity_invalid:entity_gsa"),
        (lambda d: d["entities"]["entity_bsa"].update(property="  "), "entity_invalid:entity_bsa"),
        (
            lambda d: d["entities"]["entity_gsa"].update(exclusions=[]),
            "entity_rule_invalid:entity_gsa",
        ),
        (
            lambda d: d.update(verified_handles=[{**VERIFIED_HANDLE, "observed_at": None}]),
            "handle_unverified:@fixture_official",
        ),
        (
            lambda d: d.update(
                verified_handles=[{k: v for k, v in VERIFIED_HANDLE.items() if k != "observed_at"}]
            ),
            "handle_unverified:@fixture_official",
        ),
        (
            lambda d: d.update(verified_handles=[{**VERIFIED_HANDLE, "observed_at": "2026-09-10"}]),
            "handle_unverified:@fixture_official",
        ),
        (
            lambda d: d.update(verified_handles=PLACEHOLDER_INPUT["verified_handles"]),
            "placeholder_input:handle_invalid:0",
        ),
        (
            lambda d: d.update(
                verified_handles=[{**VERIFIED_HANDLE, "evidence_ref": "<supply from B01>"}]
            ),
            "placeholder_input:handle_invalid:0",
        ),
        (
            lambda d: d.update(ambassador_refs=PLACEHOLDER_INPUT["ambassador_refs"]),
            "placeholder_input:ambassador_invalid:0",
        ),
        (
            lambda d: d.update(ambassador_refs=[{**AMBASSADOR, "observed_at": None}]),
            "ambassador_unverified:pyp_ambassador_fixture",
        ),
        (
            lambda d: d.update(ambassador_refs=[{**AMBASSADOR, "entity_id": "entity_bsa"}]),
            "ambassador_invalid:0",
        ),
        (
            lambda d: d["spokesperson_policy"].update(official_sentiment_scoring="allowed"),
            "spokesperson_policy_invalid",
        ),
        (
            lambda d: d["spokesperson_policy"].update(names=["an official"]),
            "spokesperson_policy_invalid",
        ),
        (
            lambda d: d["purpose_policy"].update(policy_version="client_purpose_policy_v0"),
            "purpose_policy_version_invalid",
        ),
        (
            lambda d: d["purpose_policy"]["prohibited_purposes"].pop(),
            "purpose_policy_invalid",
        ),
        (
            lambda d: d["lens_policy"]["clusters"]["investment"].update(
                priority_basis="raw_volume"
            ),
            "lens_policy_invalid",
        ),
        (
            lambda d: d["lens_policy"]["geography_rule"].update(
                subject_fills_author_geography=True
            ),
            "lens_policy_invalid",
        ),
        (lambda d: d["noise_controls"]["exclusions"].pop(0), "noise_controls_invalid"),
        (
            lambda d: d["dimensions"]["talent"].pop("distinctions"),
            "dimension_invalid:talent",
        ),
        (lambda d: d["languages"].update(international=["en"]), "languages_invalid"),
        (
            lambda d: d.update(comparator_set_ref="TBC"),
            "placeholder_input:comparator_set_ref_invalid",
        ),
        (lambda d: d.update(contract_version="topic_overlay_v0"), "overlay_version_invalid"),
    ],
)
def test_compiler_refuses_a_changed_document(mutate, code):
    raw = document()
    mutate(raw)
    with pytest.raises(co.ClientOverlayInvalid) as caught:
        co.compile_client_overlay(raw)
    assert caught.value.code == code


def test_verified_handle_and_ambassador_are_retained_with_their_observation_time():
    raw = document()
    raw["verified_handles"] = [VERIFIED_HANDLE]
    raw["ambassador_refs"] = [AMBASSADOR]
    overlay = co.compile_client_overlay(raw)

    assert overlay["verified_handles"][0]["observed_at"] == OBSERVED
    assert overlay["verified_handles"][0]["evidence_ref"] == "b01-fixture-verification"
    assert overlay["ambassador_refs"][0]["observed_at"] == OBSERVED
    assert overlay["entities"]["entity_pyp"]["known_ambassador_handles"] == ("@fixture_ambassador",)
    assert raw["entities"]["entity_pyp"]["known_ambassador_handles"] == []
    matches = topic_overlays.entity_matches
    assert matches(overlay, "entity_pyp", "Play your part, says @fixture_ambassador")
    assert not matches(compiled(), "entity_pyp", "Play your part, says @fixture_ambassador")
    assert overlay["spokesperson_policy"]["individual_profiling"] == "refused"


def test_purpose_policy_names_the_brief_and_refuses_each_prohibited_purpose():
    overlay = compiled()
    policy = overlay["purpose_policy"]
    assert PURPOSE_DEFINITION in policy["source_definition"]
    assert overlay["provenance"]["source_document"] == BRIEF
    assert tuple(p["code"] for p in policy["prohibited_purposes"]) == co.PURPOSE_CODES
    cases = {
        "political_party_purpose": "Track the ruling party campaign messaging in Gauteng",
        "electoral_purpose": "How is the election being discussed by Global South Africans?",
        "voter_targeting_purpose": "Which narratives could persuade undecided voters in Durban?",
        "agency_position_live_regulatory_matter": (
            "Should Brand South Africa oppose the draft bill under regulatory review?"
        ),
    }
    for code, text in cases.items():
        evaluation = co.evaluate_client_purpose(policy, [text])
        assert evaluation["state"] == "prohibited", text
        assert code in evaluation["purpose_codes"], text
        assert evaluation["review_override"] == "refused"
        assert evaluation["evidence"][0]["matched"]
    permitted = [
        "What are Global South Africans saying about load shedding this week?",
        "What do we know about support for the regulator decision on tariffs?",
        "Which narratives about crime in South Africa reached international outlets?",
        "How did the party at the Cape Town jazz festival trend?",
    ]
    for text in permitted:
        assert co.evaluate_client_purpose(policy, [text])["state"] == "permitted", text


def test_purpose_enforcement_is_scoped_to_the_client_and_names_its_stage():
    text = "Which party messaging reached undecided voters before the election?"
    for stage, code in co.PURPOSE_STAGES.items():
        with pytest.raises(co.ClientPurposeProhibited) as caught:
            co.refuse_prohibited_client_purpose(bsa_scope(), [text], stage=stage)
        assert caught.value.code == code
        assert caught.value.stage == stage
        assert set(caught.value.purpose_codes) >= {"electoral_purpose", "voter_targeting_purpose"}
        assert caught.value.policy_version == co.PURPOSE_POLICY_VERSION
    assert co.refuse_prohibited_client_purpose(general_scope(), [text], stage="admission") is None
    ok = co.refuse_prohibited_client_purpose(
        bsa_scope(), ["Load shedding stage six"], stage="export"
    )
    assert ok["state"] == "permitted"
    with pytest.raises(ValueError):
        co.refuse_prohibited_client_purpose(bsa_scope(), [text], stage="review")


def test_the_purpose_gate_refuses_a_foreign_client_scope_carrying_the_brand_name():
    """The production gate is where a brand name must stop selecting a policy.

    Every other test of this gate hands it either the BSA scope or general 42, so
    a gate that still reached the configuration from the brand name alone would
    pass all of them. A client scope no registry entry names, carrying the brand
    configuration an entry does carry, is what separates the two.
    """
    foreign = {"brand_config_id": "bsa", "client_scope_id": "ogilvy_default"}
    with pytest.raises(topic_overlays.TopicOverlayInvalid) as caught:
        co.refuse_prohibited_client_purpose(
            foreign, ["Load shedding stage six"], stage="admission"
        )
    assert caught.value.code == "client_lens_unauthorized"
    # The same brand name still reaches nothing at any stage.
    for stage in co.PURPOSE_STAGES:
        with pytest.raises(topic_overlays.TopicOverlayInvalid):
            co.refuse_prohibited_client_purpose(foreign, ["anything"], stage=stage)
    # And the same call one layer down refuses for the same reason.
    with pytest.raises(topic_overlays.TopicOverlayInvalid):
        co.compiled_overlay_for_scope(foreign)


@pytest.mark.parametrize("dimension, distinction, positives, negatives", BSA_DIMENSION_FIXTURES)
def test_dimension_distinctions_separate_intent_direction_share_and_reach(
    dimension, distinction, positives, negatives
):
    overlay = compiled()
    for text in positives:
        result = co.classify_dimensions(overlay, text)
        assert dimension in result, text
        assert distinction in result[dimension]["distinctions"], text
    for text in negatives:
        result = co.classify_dimensions(overlay, text)
        assert distinction not in result.get(dimension, {}).get("distinctions", []), text
    assert co.classify_dimensions(overlay, "") == {}


def test_investment_and_exports_carry_terms_without_distinctions():
    overlay = compiled()
    result = co.classify_dimensions(
        overlay, "Foreign direct investment through the JSE and citrus exports under AGOA"
    )
    assert result["investment"]["distinctions"] == []
    assert result["exports"]["distinctions"] == []
    assert "AGOA" in result["exports"]["terms"]


@pytest.mark.parametrize("code, stripped, retained", BSA_NOISE_FIXTURES)
def test_noise_controls_use_the_brief_definition_with_negative_controls(code, stripped, retained):
    overlay = compiled()
    controls = overlay["noise_controls"]
    assert NOISE_DEFINITION in controls["source_definition"]
    assert tuple(e["code"] for e in controls["exclusions"]) == co.NOISE_CODES
    for text in stripped:
        result = co.apply_noise_controls(overlay, text)
        assert result["excluded"] is True, text
        assert code in result["exclusion_codes"], text
        assert result["visible"] is False
    for text in retained:
        result = co.apply_noise_controls(overlay, text)
        assert result["excluded"] is False, text
        assert code not in result["exclusion_codes"], text
        assert result["visible"] is True


def test_an_eligible_positive_story_stays_visible_beside_a_risk_narrative():
    overlay = compiled()
    positive = "Springboks fans filled the stadium for the flypast, a tourism boost for Durban"
    risk = "Tourist robbed at gunpoint outside the stadium, travel advisory under review"
    seen = co.apply_noise_controls(overlay, positive)
    assert seen["visible"] is True
    assert seen["watch"] == ["opportunity", "event"]
    assert co.apply_noise_controls(overlay, risk)["visible"] is True
    assert co.apply_noise_controls(overlay, risk)["watch"] == []
    assert co.apply_noise_controls(overlay, "")["visible"] is True


def test_lens_policy_freezes_authority_pickup_and_geography_from_admitted_metadata():
    overlay = compiled()
    lens = overlay["lens_policy"]
    assert lens["clusters"]["investment"]["priority_basis"] == "source_authority_over_volume"
    assert (
        lens["clusters"]["energy"]["priority_basis"] == "verified_independent_international_pickup"
    )
    assert lens["clusters"]["land"]["priority_basis"] == "author_geography_reported_explicitly"
    assert lens["authority_definition"]["basis"] == "admitted_metadata_and_reviewed_source_roles"
    assert lens["authority_definition"]["missing"] == "unknown"
    assert lens["pickup_definition"]["missing"] == "unknown"
    assert lens["geography_rule"] == {"subject_fills_author_geography": False, "missing": "unknown"}
    assert "author authority rather than volume" in lens["clusters"]["investment"]["source_note"]
    with pytest.raises(co.ClientOverlayInvalid) as caught:
        co.assess_cluster_priority(overlay, "tourism", [], origin_authorities={})
    assert caught.value.code == "lens_cluster_unknown"


WIRE_AUTHORITIES = {"wire_original": "a" * 64}


def test_duplicate_financial_wire_copies_do_not_inflate_investment_importance():
    overlay = compiled()
    wire = {
        "origin_id": "wire_original",
        "origin_authority_digest": "a" * 64,
        "source_role": "financial_wire_original",
    }
    copies = [
        lens_record(i, platform=f"outlet_{i}", content_digest="c" * 64, **wire) for i in range(30)
    ]
    one = co.assess_cluster_priority(
        overlay, "investment", copies[:1], origin_authorities=WIRE_AUTHORITIES
    )
    many = co.assess_cluster_priority(
        overlay, "investment", copies, origin_authorities=WIRE_AUTHORITIES
    )
    assert (one["raw_volume"], many["raw_volume"]) == (1, 30)
    assert one["authoritative_origin_count"] == many["authoritative_origin_count"] == 1
    assert one["priority"] == many["priority"] == "elevated"
    volume_only = [lens_record(i, platform=f"blog_{i}") for i in range(30)]
    quiet = co.assess_cluster_priority(
        overlay, "investment", volume_only, origin_authorities=WIRE_AUTHORITIES
    )
    assert quiet["raw_volume"] == 30
    assert quiet["authoritative_origin_count"] == 0
    assert quiet["unknown_authority_count"] == 30
    assert quiet["priority"] == "baseline"
    unproven = [lens_record(1, source_role="official_statement")]
    assert (
        co.assess_cluster_priority(
            overlay, "investment", unproven, origin_authorities=WIRE_AUTHORITIES
        )["priority"]
        == "baseline"
    )


def self_asserted_copies(digest="a" * 64):
    """Thirty copies of one wire story, each naming an origin id it asserts for itself."""
    return [
        lens_record(
            i,
            platform=f"outlet_{i}",
            content_digest="c" * 64,
            origin_id=f"wire_copy_{i}",
            origin_authority_digest=digest,
            source_role="financial_wire_original",
        )
        for i in range(30)
    ]


def test_a_self_asserted_origin_digest_is_never_authority_for_investment_priority():
    """Formatting hex is not proof of authority, so it can never reach elevated.

    Each copy names its own origin id and asserts a well shaped digest for it. Shape
    validation says only that the caller can format hex, so an origin the injected
    authority set does not name counts as unknown, never as an independent authority.
    """
    overlay = compiled()
    copies = self_asserted_copies()
    unregistered = co.assess_cluster_priority(
        overlay, "investment", copies, origin_authorities=WIRE_AUTHORITIES
    )
    assert unregistered["authoritative_origins"] == {}
    assert unregistered["authoritative_origin_count"] == 0
    assert unregistered["unknown_authority_count"] == 30
    assert unregistered["raw_volume"] == 30
    assert unregistered["priority"] == "baseline"
    empty = co.assess_cluster_priority(overlay, "investment", copies, origin_authorities={})
    assert empty["authoritative_origin_count"] == 0
    assert empty["unknown_authority_count"] == 30
    assert empty["priority"] == "baseline"


def test_a_registered_origin_asserting_the_wrong_digest_counts_as_unknown():
    overlay = compiled()
    wrong = [
        lens_record(
            i,
            platform=f"outlet_{i}",
            content_digest="c" * 64,
            origin_id="wire_original",
            origin_authority_digest="e" * 64,
            source_role="financial_wire_original",
        )
        for i in range(30)
    ]
    result = co.assess_cluster_priority(
        overlay, "investment", wrong, origin_authorities=WIRE_AUTHORITIES
    )
    assert result["authoritative_origin_count"] == 0
    assert result["unknown_authority_count"] == 30
    assert result["priority"] == "baseline"


def test_an_unverified_international_origin_never_elevates_energy_pickup():
    overlay = compiled()
    pickup = lens_record(
        99,
        platform="news",
        author_geography="de",
        source_role="international_outlet",
        origin_id="self_asserted_outlet",
        origin_authority_digest="d" * 64,
    )
    quiet = co.assess_cluster_priority(
        overlay, "energy", [pickup], origin_authorities={"international_original": "d" * 64}
    )
    assert quiet["qualifying_pickup"] == []
    assert quiet["unknown_pickup_count"] == 1
    assert quiet["priority"] == "baseline"


@pytest.mark.parametrize(
    "authorities",
    [
        None,
        [],
        {"wire_original": "A" * 64},
        {"wire_original": "a" * 63},
        {"wire_original": "a" * 64 + "not-hex-at-all"},
        {"wire_original": None},
        {"Wire_Original": "a" * 64},
        {"wire_original ": "a" * 64},
        {"rss": "a" * 64},
        {"": "a" * 64},
        {7: "a" * 64},
    ],
)
def test_an_invalid_origin_authority_set_refuses_by_name(authorities):
    overlay = compiled()
    with pytest.raises(co.ClientOverlayInvalid) as caught:
        co.assess_cluster_priority(
            overlay, "investment", self_asserted_copies(), origin_authorities=authorities
        )
    assert caught.value.code == "lens_origin_authorities_invalid"


def test_the_origin_authority_set_has_no_default():
    overlay = compiled()
    with pytest.raises(TypeError):
        co.assess_cluster_priority(overlay, "investment", self_asserted_copies())


@pytest.mark.parametrize(
    "origin_id", ["Wire-Story-7", "wire story 7", "wire-story-7 ", 7, "rss", "a" * 129]
)
def test_an_observation_origin_outside_the_grammar_refuses_through_the_overlay_contract(origin_id):
    """The module's contract is ClientOverlayInvalid, so no bare ValueError may escape it.

    The declared grammar refuses a case or whitespace variant that previously only grouped
    badly, so the identity kernel now raises where it did not before and the wrapper is
    what keeps a caller of this module holding one error type.
    """
    overlay = compiled()
    records = [
        lens_record(
            0,
            origin_id=origin_id,
            origin_authority_digest="a" * 64,
            source_role="financial_wire_original",
        )
    ]
    with pytest.raises(co.ClientOverlayInvalid) as caught:
        co.assess_cluster_priority(overlay, "investment", records, origin_authorities={})
    assert caught.value.code == "lens_observations_invalid"


ENERGY_AUTHORITIES = {
    **{f"domestic_{i}": "b" * 64 for i in range(50)},
    "international_original": "d" * 64,
}


def test_one_qualified_international_energy_pickup_changes_priority_with_a_shown_basis():
    overlay = compiled()
    domestic = [
        lens_record(
            i,
            platform="x",
            author_geography="za",
            source_role="domestic_outlet",
            origin_id=f"domestic_{i}",
            origin_authority_digest="b" * 64,
        )
        for i in range(50)
    ]
    saturated = co.assess_cluster_priority(
        overlay, "energy", domestic, origin_authorities=ENERGY_AUTHORITIES
    )
    assert saturated["priority"] == "baseline"
    assert saturated["qualifying_pickup"] == []
    pickup = lens_record(
        99,
        platform="news",
        author_geography="de",
        source_role="international_outlet",
        origin_id="international_original",
        origin_authority_digest="d" * 64,
    )
    elevated = co.assess_cluster_priority(
        overlay, "energy", [*domestic, pickup], origin_authorities=ENERGY_AUTHORITIES
    )
    assert elevated["priority"] == "elevated"
    assert elevated["qualifying_pickup"] == [
        {
            "observation_key": ("za", "news", "row_99"),
            "author_geography": "de",
            "source_role": "international_outlet",
            "origin_id": "international_original",
        }
    ]
    repost = {**pickup, "repost_of": "row_1"}
    assert (
        co.assess_cluster_priority(
            overlay, "energy", [*domestic, repost], origin_authorities=ENERGY_AUTHORITIES
        )["priority"]
        == "baseline"
    )
    unverified = {k: v for k, v in pickup.items() if k != "origin_authority_digest"}
    unverified.pop("origin_id")
    unknown = co.assess_cluster_priority(
        overlay, "energy", [*domestic, unverified], origin_authorities=ENERGY_AUTHORITIES
    )
    assert unknown["priority"] == "baseline"
    assert unknown["unknown_pickup_count"] == 1


def test_a_south_africa_subject_never_fills_missing_author_geography():
    overlay = compiled()
    records = [
        lens_record(1, subject_country="za", author_geography="us"),
        lens_record(2, subject_country="za", author_geography="us"),
        lens_record(3, subject_country="za"),
        lens_record(4, subject_country="za", author_geography="za"),
    ]
    result = co.assess_cluster_priority(overlay, "land", records, origin_authorities={})
    assert result["author_geography"] == {"us": 2, "za": 1}
    assert result["unknown_geography_count"] == 1
    assert result["subject_country_used_for_geography"] is False
    assert result["raw_volume"] == 4


def test_zero_baseline_is_not_a_zero_variance():
    result = co.calibration_difference(0, 3)
    assert result["relative_difference"] is None
    assert result["requires_explanation"] is True
    assert co.calibration_difference(0, 0)["requires_explanation"] is False


def measurement(value, unit="net_sentiment_points", scope=None):
    return {
        "value": value,
        "unit": unit,
        "scope": scope or {"period": "2026-05-18/2026-06-15", "market": "za", "cluster": "all"},
    }


def test_evaluate_calibration_flags_every_difference_over_ten_percent_without_a_pass():
    over = co.evaluate_calibration(measurement(77), measurement(85))
    assert over["requires_explanation"] is True
    assert over["explanation"] == "source_backed_explanation_required"
    assert over["accuracy_pass"] is None
    assert over["reviewer_decision"] == "required"
    assert math.isclose(over["relative_difference"], 8 / 77)
    within = co.evaluate_calibration(measurement(77), measurement(80))
    assert within["requires_explanation"] is False
    assert within["explanation"] == "not_required"
    assert within["accuracy_pass"] is None
    exact = co.evaluate_calibration(measurement(-77), measurement(-77))
    assert exact["absolute_difference"] == 0
    assert exact["accuracy_pass"] is None
    zero = co.evaluate_calibration(measurement(0), measurement(3))
    assert zero["relative_difference"] is None
    assert zero["requires_explanation"] is True
    assert zero["threshold"] == 0.10


@pytest.mark.parametrize(
    "expected, observed, code",
    [
        (measurement(float("nan")), measurement(1), "calibration_value_invalid:expected"),
        (measurement(1), measurement(float("inf")), "calibration_value_invalid:observed"),
        (measurement(True), measurement(1), "calibration_value_invalid:expected"),
        (measurement("77"), measurement(1), "calibration_value_invalid:expected"),
        (measurement(1), measurement(1, unit="messages"), "calibration_unit_mismatch"),
        (measurement(1), measurement(1, scope={"period": "other"}), "calibration_scope_mismatch"),
        ({"value": 1}, measurement(1), "calibration_field_invalid:expected"),
        (
            {"value": 1, "unit": "net_sentiment_points", "scope": {}},
            measurement(1),
            "calibration_field_invalid:expected",
        ),
    ],
)
def test_evaluate_calibration_refuses_incompatible_measurements(expected, observed, code):
    with pytest.raises(co.CalibrationInvalid) as caught:
        co.evaluate_calibration(expected, observed)
    assert caught.value.code == code


POLICY = {
    "policy_version": co.COORDINATION_POLICY_VERSION,
    "synchrony_window_seconds": 300,
    "min_accounts": 3,
    "recurring_carrier_min_observations": 3,
}


def post(row, account, at, *, content="e" * 64, repost_of=None, platform="x", **extra):
    return {
        "market": "za",
        "platform": platform,
        "source_row_id": row,
        "content_digest": content,
        "account_id": account,
        "published_at": at,
        "repost_of": repost_of,
        **extra,
    }


def test_coordination_counts_say_whether_origin_authority_was_projected_at_all():
    """A zero independent origin count is a measurement or an absence, and it says which.

    The coordination fields carry no origin, so the zero the assessment publishes means the
    field was never projected. The same zero over records that do carry the field, all of
    them null, is a measured zero. The two must not read alike.
    """
    absent = [
        post("r1", "acc_a", "2026-09-01T08:00:00Z"),
        post("r2", "acc_b", "2026-09-01T08:01:00Z"),
        post("r3", "acc_c", "2026-09-01T08:02:00Z"),
    ]
    unprojected = co.assess_coordination(absent, POLICY)
    assert unprojected["independent_origin_count"] == 0
    assert unprojected["unknown_origin_count"] == 3
    assert unprojected["origin_authority_projection"] == {
        "state": "not_projected",
        "projected_records": 0,
        "unprojected_records": 3,
    }
    null = [
        post(row, account, at, origin_id=None, origin_authority_digest=None)
        for row, account, at in (
            ("r1", "acc_a", "2026-09-01T08:00:00Z"),
            ("r2", "acc_b", "2026-09-01T08:01:00Z"),
            ("r3", "acc_c", "2026-09-01T08:02:00Z"),
        )
    ]
    measured = co.assess_coordination(null, POLICY)
    assert measured["independent_origin_count"] == 0
    assert measured["unknown_origin_count"] == 3
    assert measured["origin_authority_projection"] == {
        "state": "projected",
        "projected_records": 3,
        "unprojected_records": 0,
    }


def test_independent_same_phrase_posts_are_not_a_coordination_candidate():
    posts = [
        post("r1", "acc_a", "2026-09-01T08:00:00Z"),
        post("r2", "acc_b", "2026-09-02T09:30:00Z"),
        post("r3", "acc_c", "2026-09-03T11:00:00Z"),
    ]
    result = co.assess_coordination(posts, POLICY)
    assert result["candidates"] == []
    assert result["unassessed"][0]["reason"] == "shared_content_outside_synchrony_window"
    assert result["unassessed"][0]["account_ids"] == ["acc_a", "acc_b", "acc_c"]
    assert result["earliest_observed"] == {
        "observation_key": ("za", "x", "r1"),
        "published_at": "2026-09-01T08:00:00Z",
        "originator_claim": False,
    }
    assert result["intent_claims"] == "none"
    assert result["recurring_carriers"] == []
    assert result["unknown_origin_count"] == 3


def test_a_real_repost_chain_is_disclosed_ancestry_not_coordination():
    posts = [
        post("root", "acc_root", "2026-09-01T08:00:00Z", content="1" * 64),
        post("s1", "acc_1", "2026-09-01T08:01:00Z", content="1" * 64, repost_of="root"),
        post("s2", "acc_2", "2026-09-01T08:02:00Z", content="1" * 64, repost_of="s1"),
        post("s3", "acc_3", "2026-09-01T08:02:30Z", content="1" * 64, repost_of="root"),
    ]
    result = co.assess_coordination(posts, POLICY)
    assert result["repost_chains"] == [
        {
            "root_key": ("za", "x", "root"),
            "member_keys": [("za", "x", "s1"), ("za", "x", "s2"), ("za", "x", "s3")],
            "disclosed": True,
        }
    ]
    assert result["candidates"] == []
    assert result["unassessed"] == []
    assert result["earliest_observed"]["originator_claim"] is False


def test_missing_ancestry_refuses_rather_than_inventing_an_origin():
    posts = [
        post("child", "acc_1", "2026-09-01T08:01:00Z", repost_of="never_collected"),
    ]
    with pytest.raises(co.CoordinationInvalid) as caught:
        co.assess_coordination(posts, POLICY)
    assert caught.value.code == "coordination_ancestry_invalid"
    loop = [
        post("a", "acc_1", "2026-09-01T08:01:00Z", repost_of="b"),
        post("b", "acc_2", "2026-09-01T08:02:00Z", repost_of="a"),
    ]
    with pytest.raises(co.CoordinationInvalid):
        co.assess_coordination(loop, POLICY)


def test_synchronized_copied_text_is_a_candidate_with_evidence_and_competing_explanations():
    burst = [
        post(f"b{i}", f"acc_{i}", f"2026-09-01T08:0{i}:00Z", content="7" * 64) for i in range(4)
    ]
    carrier = [
        post(f"c{i}", "acc_carrier", f"2026-09-0{i + 1}T12:00:00Z", content=f"{i + 20:064x}")
        for i in range(3)
    ]
    result = co.assess_coordination([*burst, *carrier], POLICY)
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["account_ids"] == ["acc_0", "acc_1", "acc_2", "acc_3"]
    assert candidate["evidence"] == {
        "temporal": {"span_seconds": 180.0, "window_seconds": 300},
        "content": {"identical_content_digest": True},
        "account": {"distinct_accounts": 4},
        "ancestry": "undisclosed",
    }
    assert "coordinated posting" in candidate["competing_explanations"]
    assert "independent reuse of a shared phrase or source" in candidate["competing_explanations"]
    assert candidate["intent_claim"] is None
    assert "malicious" not in repr(result)
    assert result["recurring_carriers"] == [
        {
            "account_id": "acc_carrier",
            "observation_count": 3,
            "observation_keys": [("za", "x", "c0"), ("za", "x", "c1"), ("za", "x", "c2")],
        }
    ]
    two_accounts = burst[:2]
    below = co.assess_coordination(two_accounts, POLICY)
    assert below["candidates"] == []
    assert below["unassessed"][0]["reason"] == "shared_content_below_minimum_accounts"


def test_an_old_copy_does_not_hide_a_later_qualifying_burst():
    posts = [
        post("old", "acc_old", "2026-09-01T07:00:00Z", content="8" * 64),
        post("burst_a", "acc_a", "2026-09-01T08:00:00Z", content="8" * 64),
        post("burst_b", "acc_b", "2026-09-01T08:02:30Z", content="8" * 64),
        post("burst_c", "acc_c", "2026-09-01T08:05:00Z", content="8" * 64),
    ]

    result = co.assess_coordination(posts, POLICY)

    assert [candidate["observation_keys"] for candidate in result["candidates"]] == [
        [("za", "x", "burst_a"), ("za", "x", "burst_b"), ("za", "x", "burst_c")]
    ]
    assert result["candidates"][0]["evidence"]["temporal"] == {
        "span_seconds": 300.0,
        "window_seconds": 300,
    }
    assert result["unassessed"] == [
        {
            "content_digest": "8" * 64,
            "account_ids": ["acc_old"],
            "observation_keys": [("za", "x", "old")],
            "reason": "shared_content_outside_synchrony_window",
            "ancestry": "undisclosed",
        }
    ]
    assert result["intent_claims"] == "none"


def test_coordination_windows_are_maximal_permutation_stable_and_keep_separate_bursts():
    posts = [
        post("first_a", "acc_a", "2026-09-01T08:00:00.900000Z", content="9" * 64),
        post("first_repeat", "acc_a", "2026-09-01T08:01:00.100000Z", content="9" * 64),
        post("first_b", "acc_b", "2026-09-01T08:02:00.500000Z", content="9" * 64),
        post("first_c", "acc_c", "2026-09-01T08:04:59.900000Z", content="9" * 64),
        post("second_a", "acc_d", "2026-09-01T09:00:00Z", content="9" * 64),
        post("second_b", "acc_e", "2026-09-01T09:01:00Z", content="9" * 64),
        post("second_c", "acc_f", "2026-09-01T09:02:00Z", content="9" * 64),
    ]

    result = co.assess_coordination(posts, POLICY)
    reversed_result = co.assess_coordination(list(reversed(posts)), POLICY)

    assert result == reversed_result
    assert [candidate["observation_keys"] for candidate in result["candidates"]] == [
        [
            ("za", "x", "first_a"),
            ("za", "x", "first_b"),
            ("za", "x", "first_c"),
            ("za", "x", "first_repeat"),
        ],
        [("za", "x", "second_a"), ("za", "x", "second_b"), ("za", "x", "second_c")],
    ]
    assert [candidate["account_ids"] for candidate in result["candidates"]] == [
        ["acc_a", "acc_b", "acc_c"],
        ["acc_d", "acc_e", "acc_f"],
    ]
    assert result["unassessed"] == []
    assert result["intent_claims"] == "none"


@pytest.mark.parametrize(
    "policy, code",
    [
        ({**POLICY, "policy_version": "coordination_policy_v0"}, "coordination_policy_invalid"),
        ({**POLICY, "min_accounts": 1}, "coordination_policy_invalid"),
        ({**POLICY, "extra": 1}, "coordination_policy_invalid"),
    ],
)
def test_coordination_policy_is_validated(policy, code):
    with pytest.raises(co.CoordinationInvalid) as caught:
        co.assess_coordination([], policy)
    assert caught.value.code == code


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda p: p.pop("account_id"), "coordination_observation_invalid:fields"),
        (lambda p: p.update(market="us"), "coordination_observation_invalid:identity"),
        (
            lambda p: p.update(published_at="yesterday"),
            "coordination_observation_invalid:published_at",
        ),
        (lambda p: p.update(account_id=""), "coordination_observation_invalid:account_id"),
    ],
)
def test_coordination_observations_are_validated(mutate, code):
    record = post("r1", "acc", "2026-09-01T08:00:00Z")
    mutate(record)
    with pytest.raises(co.CoordinationInvalid) as caught:
        co.assess_coordination([record], POLICY)
    assert caught.value.code == code
