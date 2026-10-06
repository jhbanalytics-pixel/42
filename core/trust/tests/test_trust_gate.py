"""Gate rules G1, G3, G4b, G5, G5b, G6, G8 and G10 for morning trend cards (TRUST.md section 4, Stage 1A)."""

from core.trust.gate import Decision, gate_card, market_banner


def card(**kw):
    base = {
        "item_id": "it_1",
        "market": "ZA",
        "kind": "hashtag",
        "state": "emerging",
        "untested": True,
        "authenticity": "not_assessed",
        "geo_status": "local",
        "local_share": 0.8,
        "geo_known_posts7": 20,
        "market_scope": "market",
        "market_posts7": 16,
        "total_posts7": 20,
        "market_share7": 0.8,
        "sponsored_share": 0.1,
        "moment": None,
        "hashtags": ["amapianostep"],
        "canonical_key": "#amapianostep",
    }
    base.update(kw)
    return base


def ctx(**kw):
    base = {
        "valid_days": [True, True, True],
        "lane_classes": {"unbiased_rank", "panel"},
        "campaign_hashtags": {"brandlaunch"},
        "political": False,
        "corroborated_unbiased": False,
        "explanation_passed": True,
    }
    base.update(kw)
    return base


def test_clean_card_publishes_to_today():
    d = gate_card(card(), ctx())
    assert isinstance(d, Decision)
    assert (d.publish, d.where, d.flag, d.rule, d.numbers_only) == (
        True,
        "today",
        None,
        None,
        False,
    )


# G1


def test_g1_invalid_day_holds_with_data_issue():
    d = gate_card(card(), ctx(valid_days=[True, False, True]))
    assert (d.publish, d.where, d.flag, d.rule) == (False, "held_back", "Data issue", "G1")
    assert d.reason


def test_g1_no_history_is_warm_up_not_invalid():
    d = gate_card(card(), ctx(valid_days=[None, None, True]))
    assert d.publish and d.where == "today"


# G3


def test_g3_seen_only_in_search_lanes_is_held_as_found_by_search():
    for lanes in (
        {"expansion"},
        {"agent_live"},
        {"expansion", "agent_live"},
        {"search_presence"},
        set(),
    ):
        d = gate_card(card(), ctx(lane_classes=lanes))
        assert (d.publish, d.where, d.rule) == (False, "held_back", "G3"), lanes
        assert "Found by search" in d.reason


def test_g3_seen_in_a_measured_lane_passes():
    d = gate_card(card(), ctx(lane_classes={"expansion", "unbiased_rank"}))
    assert d.publish


# G4b


def test_g4b_political_item_not_corroborated_is_held_not_assessed():
    d = gate_card(card(), ctx(political=True, corroborated_unbiased=False))
    assert (d.publish, d.where, d.flag, d.rule) == (False, "held_back", "Not assessed", "G4b")


def test_g4b_political_item_corroborated_in_unbiased_lane_publishes():
    d = gate_card(card(), ctx(political=True, corroborated_unbiased=True))
    assert d.publish and d.where == "today"


# G5


def test_g5_sponsored_share_half_or_more_is_paid_led_and_out_of_the_organic_list():
    d = gate_card(card(sponsored_share=0.5), ctx())
    assert (d.publish, d.where, d.flag, d.rule) == (False, "held_back", "Paid-led", "G5")


def test_g5_sponsored_share_below_half_passes():
    d = gate_card(card(sponsored_share=0.49), ctx())
    assert d.publish and d.flag is None


# G5b


def test_g5b_campaign_hashtag_is_paid_led():
    d = gate_card(card(hashtags=["BrandLaunch"], canonical_key="#brandlaunch"), ctx())
    assert (d.publish, d.flag, d.rule) == (False, "Paid-led", "G5b")


def test_g5b_campaign_hashtag_on_canonical_key_only_is_paid_led():
    d = gate_card(card(hashtags=[], canonical_key="#BrandLaunch"), ctx())
    assert d.rule == "G5b" and not d.publish


def test_g5b_tag_not_on_the_list_passes():
    d = gate_card(card(), ctx(campaign_hashtags={"othercampaign"}))
    assert d.publish


# G6


def test_g6_known_source_majority_does_not_add_market_unconfirmed_for_few_places():
    d = gate_card(card(geo_status="market_unconfirmed", geo_known_posts7=5, local_share=0.2), ctx())
    assert (d.publish, d.where, d.flag, d.rule) == (True, "today", None, None)


def test_g6_source_majority_overrides_legacy_physical_hold_without_mutation():
    row = card(geo_status="not_local", geo_known_posts7=12, local_share=0.2)
    before = (row["geo_status"], row["geo_known_posts7"], row["local_share"])
    d = gate_card(row, ctx())
    assert (d.publish, d.where, d.flag, d.rule) == (True, "today", None, None)
    assert (row["geo_status"], row["geo_known_posts7"], row["local_share"]) == before


def test_g6_fifty_one_of_one_hundred_passes_even_below_legacy_point_six():
    d = gate_card(
        card(
            geo_status="local",
            geo_known_posts7=100,
            local_share=0.51,
            market_scope="market",
            market_posts7=51,
            total_posts7=100,
            market_share7=0.51,
        ),
        ctx(),
    )
    assert (d.publish, d.where, d.flag, d.rule) == (True, "today", None, None)


def test_g6_exactly_fifty_percent_is_global_and_held():
    d = gate_card(
        card(
            market_scope="global",
            market_posts7=50,
            total_posts7=100,
            market_share7=0.5,
        ),
        ctx(),
    )
    assert (d.publish, d.where, d.flag, d.rule) == (False, "held_back", None, "G6")
    assert "global" in d.reason.lower()


def test_g6_missing_or_assumed_source_scope_fails_closed():
    missing = card()
    for field in ("market_scope", "market_posts7", "total_posts7", "market_share7"):
        del missing[field]
    assumed = card(market_scope="assumed")
    for row in (missing, assumed):
        d = gate_card(row, ctx())
        assert (d.publish, d.where, d.flag, d.rule) == (
            False,
            "held_back",
            "Market unconfirmed",
            "G6",
        )


def test_g6_zero_and_invalid_counts_fail_closed():
    invalid_rows = (
        card(market_scope="market", market_posts7=0, total_posts7=0, market_share7=0.0),
        card(market_scope="market", market_posts7=101, total_posts7=100, market_share7=1.01),
        card(market_scope="market", market_posts7=51.0, total_posts7=100, market_share7=0.51),
        card(market_scope="market", market_posts7=True, total_posts7=100, market_share7=0.01),
    )
    for row in invalid_rows:
        d = gate_card(row, ctx())
        assert (d.publish, d.where, d.flag, d.rule) == (
            False,
            "held_back",
            "Market unconfirmed",
            "G6",
        )


def test_g6_stale_share_or_scope_contradicting_counts_fails_closed():
    stale_share = card(
        market_scope="market",
        market_posts7=51,
        total_posts7=100,
        market_share7=0.49,
    )
    stale_scope = card(
        market_scope="global",
        market_posts7=51,
        total_posts7=100,
        market_share7=0.51,
    )
    for row in (stale_share, stale_scope):
        d = gate_card(row, ctx())
        assert (d.publish, d.where, d.flag, d.rule) == (
            False,
            "held_back",
            "Market unconfirmed",
            "G6",
        )
        assert "inconsistent" in d.reason.lower()


def test_g6_market_source_majority_passes():
    d = gate_card(card(geo_known_posts7=8, local_share=0.6), ctx())
    assert d.publish and d.flag is None


# G8


def test_g8_seasonal_goes_to_moments():
    d = gate_card(card(state="seasonal", moment="Heritage Day"), ctx())
    assert (d.publish, d.where, d.rule) == (True, "moments", "G8")


def test_g8_non_seasonal_goes_to_today():
    d = gate_card(card(state="rising"), ctx())
    assert d.where == "today"


# G10


def test_g10_failed_explanation_publishes_numbers_only():
    d = gate_card(card(), ctx(explanation_passed=False))
    assert d.publish and d.numbers_only and d.rule == "G10"


def test_g10_passed_explanation_is_not_numbers_only():
    assert gate_card(card(), ctx(explanation_passed=True)).numbers_only is False


# Precedence


def test_data_issue_wins_over_other_holds():
    d = gate_card(card(sponsored_share=0.9), ctx(valid_days=[False, True, True], political=True))
    assert d.rule == "G1" and d.flag == "Data issue"


# Banner


def test_banner_when_over_thirty_percent_held_for_data():
    held = gate_card(card(), ctx(valid_days=[False, True, True]))
    ok = gate_card(card(), ctx())
    assert market_banner([held, ok]) == "Data issue"
    assert market_banner([held, ok, ok]) == "Data issue"


def test_no_banner_at_or_under_thirty_percent():
    held = gate_card(card(), ctx(valid_days=[False, True, True]))
    ok = gate_card(card(), ctx())
    paid = gate_card(card(sponsored_share=0.9), ctx())
    assert market_banner([held] + [ok] * 3) is None
    assert market_banner([held] * 3 + [ok] * 7) is None
    assert market_banner([held, paid, paid] + [ok] * 7) is None
    assert market_banner([]) is None


def test_n10_missing_or_unknown_political_classification_holds():
    no_key = ctx()
    del no_key["political"]
    for c in (no_key, ctx(political=None)):
        d = gate_card(card(), c)
        assert (d.publish, d.rule, d.flag) == (False, "G4b", "Not assessed")
