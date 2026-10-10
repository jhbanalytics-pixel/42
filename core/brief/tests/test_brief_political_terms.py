"""The G4b political list (core/brief/political.yaml) carries the party, leader and electoral terms of the political
list in core/detect/sensitive.yaml, and leaves out generic political words and ambiguous names. It changes only which
topics read as political; the gate itself (core/trust/gate.py G4b) and sensitive enrichment are untouched.

Every expectation is pinned here. None is read from the file it checks."""

import json
from pathlib import Path

import pytest
import yaml

from core.brief import gatectx

# The 29 starter terms at a80be1d. The list may grow; it may never lose one.
STARTER = {
    "all": ["election", "elections", "vote", "voting", "parliament", "manifesto", "local elections"],
    "ZA": ["IEC", "ANC", "DA", "EFF", "MK party", "Ramaphosa", "Zuma", "Malema"],
    "NG": ["INEC", "APC", "PDP", "Labour Party", "Tinubu", "Atiku", "Obi"],
    "KE": ["IEBC", "UDA", "ODM", "Azimio", "Ruto", "Raila", "Gachagua"],
}

# Party, leader and electoral terms taken from the political list of core/detect/sensitive.yaml. ballot and voter
# were added with them and are companion-only terms under W8-DEC-06b (see NOT_ALONE below).
ADDED = {
    "all": ["electoral", "referendum"],
    "ZA": ["democratic alliance", "economic freedom fighters", "african national congress",
           "patriotic alliance", "rise mzansi", "black first land first", "mashatile", "steenhuisen", "mbeki",
           "mbalula", "mashaba", "lesufi", "godongwana", "gayton mckenzie", "south africa votes", "vote anc",
           "vote eff", "vote mk"],
    "NG": ["all progressives congress", "peoples democratic party", "obidient", "peter obi", "buhari", "shettima",
           "kwankwaso", "obasanjo", "wike", "nigeria decides", "vote apc", "vote pdp"],
    "KE": ["kenya kwanza", "kalonzo", "vote uda", "vote odm"],
}

# Generic words in the same sensitive.yaml list. They describe politics without naming a party, leader or election.
GENERIC = ["government", "government of national unity", "minister", "finance minister", "president", "presidency",
           "politics", "political", "politician", "democracy", "protest", "constitution", "senate", "senator",
           "governor", "opposition", "coalition", "coalition talks", "cabinet reshuffle", "state house", "junta",
           "land reform", "budget speech", "police brutality", "impeach", "secession", "sona"]

# Party names that sensitive.yaml keeps on a key rule because they sit inside ordinary words (whole_words_in_keys).
# Listing them here would swap that rule for the G4b one in the sensitive set, so they stay where they are.
KEPT_ON_THEIR_KEY_RULE = ["IFP", "actionsa"]

# Names that are also ordinary words, places or institutions. Left out, and not read as political on their own.
AMBIGUOUS = {
    "Kenyatta University open day this Saturday": "kenyatta",
    "Hlabisa hospital queues grow": "hlabisa",
    "Braai at Uhuru Gardens": "uhuru",
    "Rhoda Musyoka won the Under 12 chess title": "musyoka",
    "A gnu crossed the road at Kruger": "gnu",
    "Zondo is a town name here": "zondo",
}


def terms(market):
    return gatectx.load_political_terms(market)


def sensitive_political():
    path = Path(gatectx.__file__).parents[1] / "detect" / "sensitive.yaml"
    listed = yaml.safe_load(path.read_text(encoding="utf-8"))["political"]
    return {str(t).casefold() for t in listed}


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
def test_no_starter_term_is_lost(market):
    have = {t.casefold() for t in terms(market)}
    for term in STARTER["all"] + STARTER[market]:
        assert term.casefold() in have, term


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
def test_the_party_leader_and_electoral_terms_are_on_the_markets_list(market):
    have = {t.casefold() for t in terms(market)}
    for term in ADDED["all"] + ADDED[market]:
        assert term.casefold() in have, term


def test_every_added_term_comes_from_the_sensitive_political_list():
    source = sensitive_political()
    for group in ADDED.values():
        for term in group:
            assert term.casefold() in source, term


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
def test_generic_political_words_and_ambiguous_names_are_left_out(market):
    have = {t.casefold() for t in terms(market)}
    for word in GENERIC + KEPT_ON_THEIR_KEY_RULE + list(AMBIGUOUS.values()):
        assert word.casefold() not in have, word


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
def test_an_added_term_makes_a_caption_political(market):
    for term in ADDED["all"] + ADDED[market]:
        caption = f"Everyone is talking about {term} today" if not term.isupper() else f"Everyone is talking about {term}"
        assert gatectx._political(terms(market), [caption], []), term


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
@pytest.mark.parametrize("caption", list(AMBIGUOUS))
def test_an_ambiguous_name_alone_is_not_political(market, caption):
    assert not gatectx._political(terms(market), [caption], [])


def test_a_market_does_not_take_another_markets_terms():
    assert not gatectx._political(terms("KE"), ["Mashatile spoke at the rally"], [])
    assert gatectx._political(terms("ZA"), ["Mashatile spoke at the rally"], [])
    assert not gatectx._political(terms("ZA"), ["Kwankwaso spoke at the rally"], [])


# W8-DEC-06b (Albert, 9 Oct 2026): ballot and voter stay political only beside a party, leader or election term in the
# same post, with Kenyatta, Uhuru, Musyoka, Hlabisa and GNU added as leader companions; vote and voting are unchanged;
# the 31 terms of the sensitive list that W8-DEC-06 neither added nor excluded stay out.
# Every other term on the list is a party, leader or election term (or one of the 29 starters), so a post with ballot or
# voter beside one is political through that term. ballot and voter are companion-only terms: on their own they are
# not political, beside one of the five names they are, and the five names on their own are still not political.

NOT_ALONE = ["ballot", "voter"]
LEADER_COMPANIONS = ["Kenyatta", "Uhuru", "Musyoka", "Hlabisa", "GNU"]

SPORTS = ["Ballon d'Or ballot leaked", "Hall of Fame ballot", "MVP voter fatigue", "The voter for best goal is in",
          "Ballot for the club's player of the year opens"]

BESIDE = {"ZA": ["ANC", "Ramaphosa", "election"], "NG": ["APC", "Tinubu", "election"],
          "KE": ["UDA", "Ruto", "election"]}

# The 31 terms of the political list of core/detect/sensitive.yaml (104) that W8-DEC-06 neither added (38) nor named
# as excluded (35). Written out here, not read from the file under test.
UNLISTED = ["endsars", "finance bill", "mustgo", "mustfall", "put south africans first", "coup", "minister of finance",
            "abduction", "coup detat", "national assembly", "afriforum", "kill the boer", "sunday igboho", "ohanaeze",
            "state of the nation", "sona debate", "post sona", "tribe before nation", "state capture",
            "cadre deployment", "tenderpreneur", "expropriation", "phala phala", "rhodes must fall", "biafra", "ipob",
            "nnamdi kanu", "bad governance", "fuel subsidy", "maandamano", "housing levy"]


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
@pytest.mark.parametrize("caption", SPORTS)
def test_a_sports_ballot_or_voter_alone_is_not_political(market, caption):
    assert not gatectx._political(terms(market), [caption], [])


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
@pytest.mark.parametrize("word", NOT_ALONE)
def test_ballot_and_voter_are_political_beside_a_party_leader_or_election_term_in_the_same_post(market, word):
    for companion in BESIDE[market]:
        assert gatectx._political(terms(market), [f"The {word} turnout and the {companion} rally"], []), companion


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
@pytest.mark.parametrize("word", NOT_ALONE)
def test_ballot_and_voter_are_companion_only_terms_of_every_market(market, word):
    mine = [t for t in terms(market) if t.casefold() == word]
    assert len(mine) == 1 and isinstance(mine[0], gatectx.Companion)
    assert sorted(mine[0].beside) == sorted(LEADER_COMPANIONS)
    assert not any(isinstance(t, gatectx.Companion) for t in terms(market) if t.casefold() not in NOT_ALONE)


@pytest.mark.parametrize("word", NOT_ALONE)
def test_two_posts_that_each_hold_only_the_word_are_not_political(word):
    assert not gatectx._political(terms("ZA"), [f"Hall of Fame {word}", f"MVP {word} fatigue"], [])


@pytest.mark.parametrize("word", NOT_ALONE)
def test_another_markets_leader_beside_the_word_does_not_count(word):
    assert gatectx._political(terms("NG"), [f"{word} drive for Tinubu"], [])
    assert not gatectx._political(terms("ZA"), [f"{word} drive for Tinubu"], [])


@pytest.mark.parametrize("word", NOT_ALONE)
def test_a_hashtag_or_key_that_is_only_ballot_or_voter_is_not_political(word):
    assert not gatectx._political(terms("ZA"), [], [word, f"#{word}", f"{word}fatigue"])


def test_the_unlisted_terms_are_exactly_what_the_sensitive_list_holds_beyond_the_added_and_the_excluded():
    taken = {t.casefold() for group in ADDED.values() for t in group} | set(NOT_ALONE)
    named = {t.casefold() for t in GENERIC + KEPT_ON_THEIR_KEY_RULE + list(AMBIGUOUS.values())}
    assert len(taken) == 38 and len(named) == 35
    assert sorted(sensitive_political() - taken - named) == sorted(UNLISTED)
    assert len(UNLISTED) == 31 and len(sensitive_political()) == 104


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
def test_the_unlisted_terms_stay_out_of_the_political_list(market):
    have = {t.casefold() for t in terms(market)}
    for term in UNLISTED:
        assert term.casefold() not in have, term


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
@pytest.mark.parametrize("term", UNLISTED)
def test_a_caption_with_an_unlisted_term_is_not_political(market, term):
    assert not gatectx._political(terms(market), [f"Everyone is talking about {term} today"], [])


# The whole list is pinned. Every term above is checked one way (present, or absent); this checks the other way, that
# nothing else is on it, so a term added later (voters, ballot box) is a change the tests notice.

@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
def test_the_markets_list_is_exactly_the_starters_and_the_added_terms(market):
    expected = STARTER["all"] + ADDED["all"] + STARTER[market] + ADDED[market] + NOT_ALONE
    assert sorted(terms(market)) == sorted(expected)


def test_the_file_holds_the_all_list_and_the_three_markets_and_nothing_else():
    path = Path(gatectx.__file__).parent / "political.yaml"
    lists = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert sorted(lists) == ["KE", "NG", "ZA", "all", "companion_only", "companions"]
    assert sorted(lists["all"]) == sorted(STARTER["all"] + ADDED["all"])
    assert sorted(lists["companion_only"]) == sorted(NOT_ALONE)
    assert sorted(lists["companions"]) == sorted(LEADER_COMPANIONS)
    for market in ("ZA", "NG", "KE"):
        assert sorted(lists[market]) == sorted(STARTER[market] + ADDED[market])


# The five names as leader companions of ballot and voter

@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
@pytest.mark.parametrize("caption", ["Kenyatta ballot", "Uhuru voter drive", "Musyoka ballot box", "Hlabisa voter roll",
                                     "GNU ballot", "The ballot at Kenyatta's rally", "Voter turnout, says Uhuru"])
def test_ballot_and_voter_beside_one_of_the_five_names_are_political(market, caption):
    assert gatectx._political(terms(market), [caption], [])


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
@pytest.mark.parametrize("name", LEADER_COMPANIONS)
@pytest.mark.parametrize("word", NOT_ALONE)
def test_each_name_makes_each_word_political_in_the_same_post_only(market, name, word):
    assert gatectx._political(terms(market), [f"{name} and the {word} count"], [])
    assert not gatectx._political(terms(market), [f"{name} open day", f"Hall of Fame {word}"], [])


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
def test_the_five_names_alone_are_still_not_political_and_a_lower_case_gnu_is_not_a_name(market):
    for caption in AMBIGUOUS:
        assert not gatectx._political(terms(market), [caption], [])
    assert not gatectx._political(terms(market), ["A gnu ballot at the zoo"], [])
    assert gatectx._political(terms(market), ["A GNU ballot at the zoo"], [])


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
@pytest.mark.parametrize("caption", ["Hall of Fame ballot", "The sports voter poll for MVP", "Ballot for the player of the year"])
def test_a_sports_ballot_or_voter_post_is_not_political(market, caption):
    assert not gatectx._political(terms(market), [caption], [])


def test_a_ballot_beside_a_party_is_political_and_a_fan_vote_still_is():
    assert gatectx._political(terms("ZA"), ["Ballot for the ANC list is out"], [])
    for market in ("ZA", "NG", "KE"):
        assert gatectx._political(terms(market), ["Fan vote for MVP"], [])
        assert gatectx._political(terms(market), ["Vote now: Ballon d'Or ballot"], [])
        assert gatectx._political(terms(market), ["Voting closes tonight"], [])


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
def test_a_tag_or_key_holding_a_word_and_a_name_is_political_and_one_holding_either_alone_is_not(market):
    for tag in ("KenyattaBallot", "#uhuruvoterdrive", "MusyokaVoter", "HlabisaBallot"):
        assert gatectx._political(terms(market), [], [tag]), tag
    for tag in ("Kenyatta", "#Uhuru", "ballot", "voterfatigue", "GNU"):
        assert not gatectx._political(terms(market), [], [tag]), tag


def test_the_companion_loader_gives_plain_strings_to_every_caller():
    for market in ("ZA", "NG", "KE"):
        assert all(isinstance(t, str) for t in terms(market))
        assert json.dumps(terms(market))


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
def test_a_name_in_one_tag_and_a_word_in_another_is_not_political(market):
    assert not gatectx._political(terms(market), [], ["Kenyatta", "ballot"])
    assert not gatectx._political(terms(market), [], ["Uhuru", "voter"])
