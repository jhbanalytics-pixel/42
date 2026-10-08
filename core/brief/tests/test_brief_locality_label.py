"""W8-DEC-17 on the card: Local needs the lower end of the 95% Wilson interval of local_share to be 0.5 or more,
otherwise the card carries Market unconfirmed. The card still publishes; G6 removal is not in this file's reach."""

import pytest

from core.brief.tests.test_brief_payload import FLAG_WORD, build, cand, decision


def card(share, known, **kw):
    return build([cand(1, geo_status="local", local_share=share, geo_known_posts7=known, **kw)])["cards"][0]


def test_five_of_eight_local_reads_market_unconfirmed_and_still_publishes():
    c = card(5 / 8, 8)
    assert (c["flag"], c["flag_word"]) == ("market_unconfirmed", FLAG_WORD["market_unconfirmed"])
    assert c["explained"] is True and c["explanation"]


def test_seven_of_eight_local_carries_no_flag():
    assert card(7 / 8, 8)["flag"] is None


@pytest.mark.parametrize("share,known,flag", [
    (9 / 10, 10, None), (8 / 10, 10, "market_unconfirmed"), (60 / 100, 100, None), (59 / 100, 100, "market_unconfirmed"),
    (32 / 50, 50, None), (30 / 50, 50, "market_unconfirmed"),
])
def test_the_flag_follows_the_interval(share, known, flag):
    assert card(share, known)["flag"] == flag


def test_the_new_flag_takes_the_place_the_old_unconfirmed_flag_has_before_authenticity():
    c = card(5 / 8, 8, authenticity="check_pattern")
    assert c["flag"] == "market_unconfirmed"


def test_a_gate_flag_still_wins():
    c = card(5 / 8, 8, decision=decision(flag="Data issue"))
    assert c["flag"] == "data_issue"


def test_a_clear_local_card_with_an_authenticity_flag_keeps_it():
    assert card(7 / 8, 8, authenticity="check_pattern")["flag"] == "check_pattern"


def test_a_card_with_no_counts_is_unchanged():
    c = build([cand(1, geo_status="local")])["cards"][0]
    assert c["flag"] is None


def test_a_local_status_under_the_removal_share_is_not_confirmed_either():
    assert card(0.2, 12)["flag"] == "market_unconfirmed"


def test_the_old_unconfirmed_status_is_unchanged():
    c = build([cand(1, geo_status="market_unconfirmed", local_share=0.2, geo_known_posts7=5)])["cards"][0]
    assert c["flag"] == "market_unconfirmed"
