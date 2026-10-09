"""Tests for the publish gate's context (core/brief/gatectx.py), run on DuckDB fixture tables."""

from datetime import datetime, timezone

import pytest

from core.brief import gatectx
from core.detect.tests import duck
from core.detect.tests.fixtures import D, counter, day, health, rid, run
from core.trust.gate import gate_card

UTC = timezone.utc
SERIES = "i1|ZA|feed_tiktok|p1"
GATE_KEYS = {"valid_days", "lane_classes", "campaign_hashtags", "political", "corroborated_unbiased",
             "explanation_passed", "sponsored_share", "paid_key"}
ZA_TERMS = gatectx.load_political_terms("ZA")


def world():
    con = duck.connect()
    duck.load(con, "agent.runs", [run("collect", day(i)) for i in range(20)] + [run("stats", D)])
    return con


def item(**over):
    row = {"item_id": "i1", "market": "ZA", "kind": "hashtag", "main_series_id": SERIES, "state": "rising",
           "run_id": rid("detect", D)}
    row.update(over)
    return row


def sight(con, pid, lane_class, *, platform="tiktok", seen=D, market="ZA", item_id="i1", lane="sweep", series=None):
    duck.load(con, "core.post_items", [{"post_id": pid, "item_id": item_id, "via": "hashtag"}])
    duck.load(con, "core.post_observations", [{
        "post_id": pid, "observed_at": datetime(seen.year, seen.month, seen.day, 10, tzinfo=UTC),
        "observed_date": seen, "market": market, "platform": platform, "lane": lane, "lane_class": lane_class,
        "series": series, "run_id": rid("collect", seen)}])


def series_platform(con, platform, series_id=SERIES):
    duck.load(con, "core.series_test", [{"metric_date": D, "series_id": series_id, "item_id": "i1", "market": "ZA",
                                         "platform": platform, "run_id": rid("stats", D)}])


def ev(pid, platform, handle, flags=(), text="caption"):
    return {"id": pid, "platform": platform, "handle": handle, "flags": list(flags), "text": text,
            "quote_text": text}


def ctx(con, row=None, evidence=(), numbers=(), terms=ZA_TERMS, market="ZA", **kw):
    return gatectx.build_ctx(duck.Client(con), row or item(), D, market, list(evidence), numbers=list(numbers),
                             campaign_hashtags=kw.pop("campaign_hashtags", []), political_terms=terms,
                             core="core", agent="agent", **kw)


NUMBER = {"value": 31, "unit": "creators in 3 days", "query_id": "q_creators3_abc", "run_id": "detect-20260920",
          "result_hash": "sha256:aa"}


# valid_days


def test_valid_days_reads_the_main_platform_on_d_then_d_minus_1_then_d_minus_2():
    con = world()
    series_platform(con, "tiktok")
    duck.load(con, "core.collection_health", [
        health("feed_tiktok", D), health("feed_tiktok", day(1), valid=False), health("feed_tiktok", day(2)),
        health("board_youtube", D, platform="youtube", valid=False)])
    assert ctx(con)["valid_days"] == [True, False, True]


def test_one_invalid_series_on_the_platform_makes_the_day_invalid():
    con = world()
    series_platform(con, "tiktok")
    duck.load(con, "core.collection_health", [
        health("feed_tiktok", D), health("board_tiktok_hashtag", D, valid=False),
        health("feed_tiktok", day(1)), health("feed_tiktok", day(2))])
    assert ctx(con)["valid_days"] == [False, True, True]


def test_search_and_watchlist_health_never_makes_a_day_invalid():
    # DATA.md 3.2: search_presence and watchlist rows are never baseline, so their validity is not G1's.
    con = world()
    series_platform(con, "tiktok")
    duck.load(con, "core.collection_health", [
        health("feed_tiktok", D), health("search_tiktok", D, lane_class="search_presence", valid=False),
        health("feed_tiktok", day(1)), health("watch_tiktok", day(1), lane_class="watchlist", valid=False),
        health("feed_tiktok", day(2)), health("placebo_tiktok", day(2), lane_class="search_presence",
                                              valid=False)])
    assert ctx(con)["valid_days"] == [True, True, True]


def test_a_day_with_only_search_health_rows_has_no_baseline_reading():
    con = world()
    series_platform(con, "tiktok")
    duck.load(con, "core.collection_health", [
        health("search_tiktok", D, lane_class="search_presence", valid=False),
        health("feed_tiktok", day(1)), health("feed_tiktok", day(2))])
    assert ctx(con)["valid_days"] == [None, True, True]


def test_an_invalid_rank_panel_or_counter_row_still_makes_the_day_invalid():
    con = world()
    series_platform(con, "tiktok")
    duck.load(con, "core.collection_health", [
        health("feed_tiktok", D, valid=False), health("search_tiktok", D, lane_class="search_presence"),
        health("feed_tiktok", day(1)), health("panel_tiktok", day(1), lane_class="panel", valid=False),
        health("feed_tiktok", day(2)), health("counter_tiktok", day(2), lane_class="unbiased_counter",
                                              valid=False)])
    got = ctx(con)
    assert got["valid_days"] == [False, False, False]
    assert gate_card({"item_id": "i1"}, got).rule == "G1"


def test_a_day_with_no_health_row_is_none_not_invalid():
    con = world()
    series_platform(con, "tiktok")
    duck.load(con, "core.collection_health", [health("feed_tiktok", D), health("feed_tiktok", day(1))])
    assert ctx(con)["valid_days"] == [True, True, None]


def test_a_missing_middle_day_after_earlier_baseline_history_is_invalid_and_holds_g1():
    # TRUST.md G1 excuses only days with no history yet. A day after history began whose collect run failed has no
    # good-run health row, so it is a gap in the baseline, not warm-up.
    con = world()
    series_platform(con, "tiktok")
    duck.load(con, "core.collection_health", [
        health("feed_tiktok", day(3)), health("feed_tiktok", day(1)), health("feed_tiktok", D)])
    got = ctx(con)
    assert got["valid_days"] == [True, True, False]
    assert gate_card({"item_id": "i1"}, got).rule == "G1"


def test_a_missing_day_with_no_history_before_it_is_still_warm_up():
    con = world()
    series_platform(con, "tiktok")
    duck.load(con, "core.collection_health", [health("feed_tiktok", day(1)), health("feed_tiktok", D)])
    assert ctx(con)["valid_days"] == [True, True, None]


def test_history_in_another_market_or_on_another_platform_or_only_in_search_is_not_history_here():
    con = world()
    series_platform(con, "tiktok")
    duck.load(con, "core.collection_health", [
        health("feed_tiktok", day(4), market="NG"), health("board_youtube", day(4), platform="youtube"),
        health("search_tiktok", day(4), lane_class="search_presence"),
        health("feed_tiktok", day(1)), health("feed_tiktok", D)])
    assert ctx(con)["valid_days"] == [True, True, None]


def test_health_from_another_market_or_a_superseded_run_is_ignored():
    con = world()
    series_platform(con, "tiktok")
    duck.load(con, "core.collection_health", [
        health("feed_tiktok", D, market="NG", valid=False),
        health("feed_tiktok", D, valid=False, run_id="collect-old"),
        health("feed_tiktok", day(1)), health("feed_tiktok", day(2))])
    assert ctx(con)["valid_days"] == [None, True, True]


def test_main_platform_comes_from_the_main_series_before_the_sightings():
    con = world()
    series_platform(con, "youtube")
    for i in range(3):
        sight(con, f"t{i}", "unbiased_rank", platform="tiktok")
    duck.load(con, "core.collection_health", [
        health("feed_tiktok", D, valid=False), health("board_youtube", D, platform="youtube")])
    assert ctx(con)["valid_days"] == [True, None, None]


def test_without_a_main_series_the_platform_with_most_sightings_is_main():
    con = world()
    sight(con, "t1", "unbiased_rank", platform="tiktok")
    sight(con, "f1", "panel", platform="facebook")
    sight(con, "f2", "panel", platform="facebook")
    duck.load(con, "core.collection_health", [
        health("feed_tiktok", D, valid=False), health("panel_fb_hub", D, platform="facebook", lane_class="panel")])
    assert ctx(con, item(main_series_id=None))["valid_days"] == [True, None, None]


def test_no_main_series_and_no_sightings_gives_warm_up_days():
    con = world()
    duck.load(con, "core.collection_health", [health("feed_tiktok", D, valid=False)])
    assert ctx(con, item(main_series_id=None))["valid_days"] == [None, None, None]



def test_a_culture_desk_main_series_reads_its_own_routes_health():
    # The desk route names no platform, so its health rows carry none; the main series is on that route.
    con = world()
    series_platform(con, "instagram", "i1|ZA|panel_culture_desk|p1")
    duck.load(con, "core.collection_health", [
        health("panel_culture_desk", D, platform=None, lane_class="panel", valid=False, k=5.0),
        health("panel_culture_desk", day(1), platform=None, lane_class="panel", k=5.0),
        health("panel_culture_desk", day(2), platform=None, lane_class="panel", valid=False, k=5.0),
        health("board_instagram", day(1), platform="instagram", valid=False),
        health("panel_culture_desk", D, platform=None, lane_class="panel", market="NG", k=5.0)])
    got = ctx(con, item(main_series_id="i1|ZA|panel_culture_desk|p1"))
    assert got["valid_days"] == [False, False, False]
    assert gate_card({"item_id": "i1"}, got).rule == "G1"


def test_a_platformless_route_the_main_series_is_not_on_does_not_decide_the_day():
    con = world()
    series_platform(con, "tiktok")
    duck.load(con, "core.collection_health", [
        health("feed_tiktok", D), health("feed_tiktok", day(1)), health("feed_tiktok", day(2)),
        health("panel_culture_desk", D, platform=None, lane_class="panel", valid=False, k=5.0)])
    assert ctx(con)["valid_days"] == [True, True, True]


# lane_classes


def test_lane_classes_are_the_items_sightings_in_the_market_over_14_days():
    con = world()
    sight(con, "p1", "panel")
    sight(con, "p2", "search_presence", seen=day(13))
    sight(con, "p3", "watchlist", seen=day(14))
    sight(con, "p4", "unbiased_rank", market="NG")
    sight(con, "p5", "unbiased_rank", item_id="other")
    sight(con, "p6", "panel", seen=day(1))
    assert ctx(con)["lane_classes"] == ["panel", "search_presence"]


def test_a_post_stats_re_read_is_watchlist_not_a_measured_lane():
    # DATA.md 3.2: counter_post_views re-reads posts 42 chose, so it is context only, never a baseline lane.
    con = world()
    sight(con, "p1", "search_presence", lane="expansion")
    sight(con, "p1", "unbiased_counter", lane="watchlist", series="counter_post_views")
    got = ctx(con, item(main_series_id=None))
    assert got["lane_classes"] == ["search_presence", "watchlist"]
    assert gate_card({"item_id": "i1"}, got).rule == "G3"


def test_a_board_or_rank_list_row_makes_a_board_only_item_measured():
    con = world()
    sight(con, "p1", "search_presence")
    duck.load(con, "core.item_counter_daily", [
        counter("i1", "board_tiktok_hashtag", day(2), 1, unit="appearances", is_board=True)])
    assert ctx(con)["lane_classes"] == ["search_presence", "unbiased_rank"]


def test_rank_rows_count_but_counters_and_the_x_trends_seed_do_not():
    con = world()
    duck.load(con, "core.item_counter_daily", [
        counter("i1", "x_trends", D, 1, unit="appearances"),
        counter("i1", "counter_tiktok_hashtag", D, 40, lane_class="unbiased_counter", unit="delta"),
        counter("i1", "board_youtube", day(20), 3, platform="youtube", unit="rank")])
    assert ctx(con)["lane_classes"] == []
    duck.load(con, "core.item_counter_daily", [counter("i1", "board_youtube", D, 3, platform="youtube", unit="rank")])
    assert ctx(con)["lane_classes"] == ["unbiased_rank"]


# political


def test_the_starter_lists_hold_shared_and_market_terms():
    assert {"election", "ANC", "MK party", "Ramaphosa"} <= set(ZA_TERMS)
    assert "Ruto" not in ZA_TERMS and "Tinubu" not in ZA_TERMS
    assert {"INEC", "Tinubu", "Labour Party", "vote"} <= set(gatectx.load_political_terms("NG"))
    assert {"IEBC", "Ruto", "Azimio", "parliament"} <= set(gatectx.load_political_terms("KE"))


def test_campaign_hashtag_list_ships_empty():
    assert gatectx.load_campaign_hashtags() == []


def test_political_matches_label_canonical_key_hashtags_or_evidence_text():
    con = world()
    assert ctx(con, item(label="Ramaphosa at the rally"))["political"] is True
    assert ctx(con, item(canonical_key="electionday2026"))["political"] is True
    assert ctx(con, item(hashtags=["#VoteANC"]))["political"] is True
    assert ctx(con, item(hashtags=["#ANC"]))["political"] is True
    assert ctx(con, evidence=[ev("p1", "tiktok", "a", text="Queue at the voting station was long")])["political"]
    assert ctx(con, evidence=[{**ev("p1", "tiktok", "a", text="dance"), "quote_text": "the MK Party march"}])[
        "political"] is True


def test_political_reads_the_label_from_the_cultural_map_when_the_row_has_none():
    con = world()
    duck.load(con, "core.cultural_map", [{"item_id": "i1", "kind": "topic", "canonical_key": "zuma_court",
                                          "label": "Zuma court date", "valid_to": None}])
    assert ctx(con)["political"] is True


def test_not_political_is_false_never_none():
    con = world()
    row = item(label="Shaya step", canonical_key="shayastep", hashtags=["#dance", "#DAnce", "#ancestors"])
    evidence = [ev("p1", "tiktok", "a", text="da real one, e dey sweet"), ev("p2", "tiktok", "b", text="Obituary")]
    assert ctx(con, row, evidence)["political"] is False


def test_short_party_names_match_only_as_capitals_in_text():
    con = world()
    assert ctx(con, evidence=[ev("p1", "tiktok", "a", text="The DA said so")])["political"] is True
    assert ctx(con, evidence=[ev("p1", "tiktok", "a", text="the da said so")])["political"] is False


def test_another_markets_terms_do_not_count():
    con = world()
    assert ctx(con, item(label="Ruto meets fans"))["political"] is False
    assert ctx(con, item(label="Ruto meets fans", market="KE"), market="KE",
               terms=gatectx.load_political_terms("KE"))["political"] is True


def test_ballot_or_voter_counts_only_beside_a_party_leader_or_election_term_in_the_same_post():
    con = world()
    sport = ev("p1", "tiktok", "a", text="Hall of Fame ballot is out")
    assert ctx(con, evidence=[sport])["political"] is False
    assert ctx(con, evidence=[ev("p1", "tiktok", "a", text="MVP voter fatigue")])["political"] is False
    assert ctx(con, evidence=[ev("p1", "tiktok", "a", text="The ballot papers for the election are out")])[
        "political"] is True
    assert ctx(con, evidence=[ev("p1", "tiktok", "a", text="Every voter in the ANC queue")])["political"] is True
    # A post's text and its quote are the same post.
    assert ctx(con, evidence=[{**ev("p1", "tiktok", "a", text="Hall of Fame ballot"),
                               "quote_text": "the ANC march"}])["political"] is True
    # The label is not a post: a label that is only the word is not political.
    assert ctx(con, item(label="Hall of Fame ballot"))["political"] is False
    assert ctx(con, item(label="ANC voter drive"))["political"] is True
    assert ctx(con, item(hashtags=["#ballot", "#voter"]))["political"] is False
    assert ctx(con, item(canonical_key="ballondorballot"))["political"] is False
    # Beside one of the five leader names a ballot or a voter is political; the name alone is not.
    assert ctx(con, evidence=[ev("p1", "tiktok", "a", text="Kenyatta ballot")])["political"] is True
    assert ctx(con, evidence=[ev("p1", "tiktok", "a", text="Uhuru voter drive")])["political"] is True
    assert ctx(con, evidence=[ev("p1", "tiktok", "a", text="Kenyatta University open day")])["political"] is False
    assert ctx(con, evidence=[ev("p1", "tiktok", "a", text="Kenyatta University open day"),
                              ev("p2", "tiktok", "b", text="Hall of Fame ballot")])["political"] is False


def test_a_party_or_election_acronym_fused_into_a_ballot_or_voter_hashtag_is_political():
    con = world()
    for market, tag in [("ZA", "#ANCvoterdrive"), ("ZA", "#EFFvoters"), ("ZA", "#DAballot"), ("ZA", "#IECvoterroll"),
                        ("KE", "#IEBCvoter"), ("NG", "#INECvoter")]:
        terms = gatectx.load_political_terms(market)
        row = item(market=market, hashtags=[tag])
        assert ctx(con, row, market=market, terms=terms)["political"] is True, tag
    # Without a party or election term in the tag the word is still only a ballot or a voter.
    assert ctx(con, item(hashtags=["#ballot"]))["political"] is False
    assert ctx(con, item(hashtags=["#voter"]))["political"] is False
    assert ctx(con, item(canonical_key="ballondorballot"))["political"] is False
    # An acronym that runs on into more capitals is another word, not the party.
    assert ctx(con, item(hashtags=["#DAILYballot"]))["political"] is False
    assert ctx(con, item(hashtags=["#PDAballot"]))["political"] is False
    # A lower case party stays clear as a hashtag value: the two letter DA is read only in capitals.
    assert ctx(con, item(hashtags=["#daballot"]))["political"] is False


PRODUCTION_FUSED = [("ZA", "#ANCvoterdrive"), ("ZA", "#EFFvoters"), ("ZA", "#DAballot"), ("ZA", "#IECvoterroll"),
                    ("KE", "#IEBCvoter"), ("NG", "#INECvoter"), ("ZA", "#ANCVoterDrive")]


def production_row(market, tag):
    """A production item: the stored key is casefolded, the label keeps the capitals, no hashtags field."""
    row = item(market=market, label=tag, canonical_key=tag.lstrip("#").casefold())
    assert "hashtags" not in row
    return row


@pytest.mark.parametrize("market,tag", PRODUCTION_FUSED)
def test_a_fused_party_tag_is_political_on_a_production_row_whose_key_is_casefolded(market, tag):
    con = world()
    terms = gatectx.load_political_terms(market)
    row = production_row(market, tag)
    assert row["canonical_key"] == tag.lstrip("#").lower()
    assert ctx(con, row, market=market, terms=terms)["political"] is True


@pytest.mark.parametrize("market,tag", PRODUCTION_FUSED)
def test_a_fused_party_tag_in_a_post_caption_is_political(market, tag):
    con = world()
    terms = gatectx.load_political_terms(market)
    caption = ev("p1", "tiktok", "a", text=f"Out now {tag} #fyp")
    assert ctx(con, item(market=market), evidence=[caption], market=market, terms=terms)["political"] is True


@pytest.mark.parametrize("tag", ["#ballot", "#voter", "#DAILYballot", "#PDAballot", "#Ballot", "#VOTER"])
def test_a_production_row_with_no_party_in_its_tag_stays_clear(tag):
    con = world()
    assert ctx(con, production_row("ZA", tag))["political"] is False
    assert ctx(con, item(), evidence=[ev("p1", "tiktok", "a", text=f"Out now {tag}")])["political"] is False


def test_the_capitals_of_a_fused_tag_are_read_after_nfkc():
    con = world()
    # Full width capitals in the hashtags field reach the capitals check as a tag, so only its own NFKC folds them.
    # DA has two letters, so only the capitals check can see it; the longer acronyms are also read by the fused check.
    assert ctx(con, item(hashtags=["#ＤＡballot"]))["political"] is True
    assert ctx(con, item(hashtags=["#ＡＮＣvoterdrive"]))["political"] is True
    # A full width hash sign hides the tag from a plain word pattern until the text is normalised.
    assert ctx(con, item(label="＃ANCvoterdrive"))["political"] is True
    # Full width letters that are no party stay clear.
    assert ctx(con, item(hashtags=["#ＢＡＬＬＯＴ"]))["political"] is False


# "#revoteballot" is in this list on purpose. Its earlier pin said clear; the CR-2 ruling reads vote, a plain election
# term that W8-DEC-06b leaves unchanged, as a term fused straight onto ballot, so the tag is political.
FUSED_PARTY_POSITIVES = [
    ("ZA", "#Iecvoter"), ("ZA", "#IECVOTER"), ("ZA", "#iecvoters"), ("ZA", "#Ancvoter"), ("ZA", "#effvoter"),
    ("ZA", "#zumaballot"), ("ZA", "#ballotMalema"), ("KE", "#rutovoter"), ("KE", "#Udaballot"),
    ("NG", "#Obiballot"), ("NG", "#wikevoter"), ("NG", "#ballotwike"), ("ZA", "#voteballot"), ("ZA", "#Voteballot"),
    ("ZA", "#ballotvote"), ("ZA", "#votervoting"), ("ZA", "#electionballot"), ("ZA", "#ballotselection"),
    ("ZA", "#votersparliament"), ("ZA", "#revoteballot"), ("NG", "#tinubuvoters"), ("KE", "#voterraila"),
    ("NG", "#votersobi"), ("NG", "#ballotswike"), ("ZA", "#ballotsvote"),
]

CLEAR_FUSED = ["#ballondorballot", "#agendaballot", "#effortvoter", "#ancestorsvoter", "#dancevoter",
               "#romancevoter", "#advanceballot", "#chiefvoter", "#stuffballot", "#cadballot", "#mediaballot",
               "#adballot", "#ballotbox", "#voterfatigue", "#votersroll"]


@pytest.mark.parametrize("market,tag", FUSED_PARTY_POSITIVES)
def test_a_party_leader_or_election_term_fused_straight_onto_ballot_or_voter_is_political(market, tag):
    con = world()
    terms = gatectx.load_political_terms(market)
    assert ctx(con, item(market=market, hashtags=[tag]), market=market, terms=terms)["political"] is True
    assert ctx(con, production_row(market, tag), market=market, terms=terms)["political"] is True


@pytest.mark.parametrize("tag", CLEAR_FUSED)
@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
def test_a_word_that_only_looks_like_a_term_fused_onto_ballot_or_voter_stays_clear(market, tag):
    con = world()
    terms = gatectx.load_political_terms(market)
    assert ctx(con, item(market=market, hashtags=[tag]), market=market, terms=terms)["political"] is False
    assert ctx(con, production_row(market, tag), market=market, terms=terms)["political"] is False


def test_a_two_letter_term_fused_onto_ballot_is_not_enough_in_lower_case():
    con = world()
    assert ctx(con, production_row("ZA", "#daballot"))["political"] is False
    assert ctx(con, production_row("ZA", "#DAballot"))["political"] is True


# corroborated_unbiased


def measured(con, *pids, lane_class="unbiased_rank"):
    for pid in pids:
        sight(con, pid, lane_class)


def test_two_measured_authors_on_two_platforms_corroborate():
    con = world()
    measured(con, "p1")
    measured(con, "p2", lane_class="panel")
    evidence = [ev("p1", "tiktok", "@thandi"), ev("p2", "facebook", "@jozi_hub")]
    assert ctx(con, evidence=evidence)["corroborated_unbiased"] is True


def test_two_authors_on_one_platform_without_a_number_do_not():
    con = world()
    measured(con, "p1", "p2")
    evidence = [ev("p1", "tiktok", "a"), ev("p2", "tiktok", "b")]
    assert ctx(con, evidence=evidence)["corroborated_unbiased"] is False


def test_one_author_on_two_platforms_is_one_author():
    con = world()
    measured(con, "p1", "p2")
    evidence = [ev("p1", "tiktok", "@Thandi"), ev("p2", "instagram", "thandi")]
    assert ctx(con, evidence=evidence)["corroborated_unbiased"] is False


def test_three_authors_plus_a_pinned_number_corroborate():
    con = world()
    measured(con, "p1", "p2", "p3")
    evidence = [ev("p1", "tiktok", "a"), ev("p2", "tiktok", "b"), ev("p3", "tiktok", "c")]
    assert ctx(con, evidence=evidence)["corroborated_unbiased"] is False
    assert ctx(con, evidence=evidence, numbers=[NUMBER])["corroborated_unbiased"] is True
    unpinned = {**NUMBER, "query_id": None}
    assert ctx(con, evidence=evidence, numbers=[unpinned])["corroborated_unbiased"] is False


def test_search_only_posts_do_not_count():
    con = world()
    measured(con, "p1")
    sight(con, "p2", "search_presence")
    evidence = [ev("p1", "tiktok", "a"), ev("p2", "youtube", "b")]
    assert ctx(con, evidence=evidence)["corroborated_unbiased"] is False


def test_a_measured_sighting_in_another_market_does_not_count():
    con = world()
    measured(con, "p1")
    sight(con, "p2", "panel", market="NG")
    evidence = [ev("p1", "tiktok", "a"), ev("p2", "youtube", "b")]
    assert ctx(con, evidence=evidence)["corroborated_unbiased"] is False


def test_flagged_brand_paid_and_generated_authors_are_not_independent():
    con = world()
    for i, flag in enumerate(("brand", "paid", "sponsored", "brand_owned", "generated", "near_duplicate",
                              "flagged")):
        measured(con, f"p{i}", f"q{i}")
        evidence = [ev(f"p{i}", "tiktok", "a"), ev(f"q{i}", "youtube", "b", flags=[flag])]
        assert ctx(con, evidence=evidence)["corroborated_unbiased"] is False, flag
        evidence[1]["flags"] = []
        assert ctx(con, evidence=evidence)["corroborated_unbiased"] is True, flag


def test_records_without_a_handle_are_not_authors():
    con = world()
    measured(con, "p1", "p2")
    evidence = [ev("p1", "tiktok", "a"), ev("p2", "youtube", None)]
    assert ctx(con, evidence=evidence)["corroborated_unbiased"] is False


# The whole context


def test_context_has_exactly_the_gate_keys_and_passes_lists_through():
    con = world()
    c = ctx(con, campaign_hashtags=["#OgilvyCampaign"], explanation_passed=True)
    assert set(c) == GATE_KEYS
    assert c["campaign_hashtags"] == ["#OgilvyCampaign"]
    assert c["explanation_passed"] is True
    assert ctx(con)["explanation_passed"] is None


def test_the_gate_reads_the_context():
    con = world()
    series_platform(con, "tiktok")
    duck.load(con, "core.collection_health", [
        health("feed_tiktok", D), health("feed_tiktok", day(1), valid=False), health("feed_tiktok", day(2))])
    measured(con, "p1")
    card = {"state": "rising", "geo_known_posts7": 10, "local_share": 0.9, "hashtags": ["shaya"],
            "canonical_key": "shaya", "market_scope": "market", "market_posts7": 6, "total_posts7": 8,
            "market_share7": 0.75}
    assert gate_card(card, ctx(con, explanation_passed=True)).rule == "G1"

    con = world()
    sight(con, "p1", "search_presence")
    assert gate_card(card, ctx(con, explanation_passed=True)).rule == "G3"

    con = world()
    measured(con, "p1")
    decision = gate_card(card, ctx(con, item(label="Malema speech"), [ev("p1", "tiktok", "a")],
                                   explanation_passed=True))
    assert decision.rule == "G4b"

    decision = gate_card(card, ctx(con, item(label="Shaya step"), [ev("p1", "tiktok", "a")],
                                   explanation_passed=True))
    assert decision.publish is True and decision.rule is None


# Paid markers in captions and hashtags


def test_caption_markers_and_post_hashtags_make_a_record_sponsored():
    con = world()
    duck.load(con, "core.posts", [{"post_id": "p_tag", "post_date": D, "hashtags": ["#AD", "dance"]},
                                  {"post_id": "p_plain", "post_date": D, "hashtags": ["adventure"]}])
    evidence = [ev("p_text", "tiktok", "@a", text="New drop #Sponsored today"),
                ev("p_phrase", "tiktok", "@b", text="In Paid Partnership with a brand"),
                ev("p_tag", "tiktok", "@c", text="dance"),
                ev("p_plain", "tiktok", "@d", text="#adventure time, #adidas #ads2 #collabs"),
                ev("p_flag", "tiktok", "@e", flags=["sponsored"], text="plain"),
                ev("p_none", "tiktok", "@f", text="#fyp"),
                ev("p_glued", "tiktok", "@g", text="shop at brand#ad and ##ads")]
    c = ctx(con, evidence=evidence)
    assert c["sponsored_share"] == pytest.approx(4 / 7)
    assert c["paid_key"] is None
    assert ctx(con)["sponsored_share"] == 0


def test_an_item_whose_own_key_is_a_paid_marker_is_flagged():
    con = world()
    for key in ("#ad", "PaidPartnership", "collab", "spon"):
        assert ctx(con, row=item(canonical_key=key, label=None))["paid_key"] == key.lstrip("#").lower()
    assert ctx(con, row=item(canonical_key="adidas", label=None))["paid_key"] is None


def test_collab_and_partner_in_posts_are_not_paid_markers():
    con = world()
    evidence = [ev("p1", "tiktok", "@a", text="New track #collab"), ev("p2", "tiktok", "@b", text="#Partner remix"),
                ev("p3", "tiktok", "@c", text="#advert out now"), ev("p4", "tiktok", "@d", text="#advertisement")]
    assert ctx(con, evidence=evidence)["sponsored_share"] == pytest.approx(2 / 4)
    duck.load(con, "core.posts", [{"post_id": "p1", "post_date": D, "hashtags": ["collab", "#partner"]}])
    assert ctx(con, evidence=evidence[:1])["sponsored_share"] == 0
