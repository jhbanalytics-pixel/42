"""failed_reason names the check that held the card (review of e25311fe): a breach holds the whole card, so
it is named even on a claim the sentence does not rest on; the critic's menu item is named only when one item
matches without a negation or contrast, otherwise the plain critic line stands."""

from core.brief import job


def _critic(text):
    return f"critic: simplest non-cultural explanation: {text}; not ruled out: evidence does not exclude it"


def test_a_breach_on_a_claim_the_sentence_does_not_rest_on_is_named():
    result = {"reason": "breach", "rests_on": ["c1", "c3"],
              "checks": [{"claim_id": "c1", "rule": "K1", "verdict": "pass", "detail": "ok"},
                         {"claim_id": "c2", "rule": "K6", "verdict": "breach", "detail": "age term"},
                         {"claim_id": "c3", "rule": "K1", "verdict": "pass", "detail": "ok"}]}
    line = job.failed_reason(result)
    assert line.startswith("Banned term check:")
    assert line != job.NO_REST


def test_a_breach_on_a_rested_claim_is_named_as_rested():
    result = {"reason": "breach", "rests_on": ["c2"],
              "checks": [{"claim_id": "c2", "rule": "K6", "verdict": "breach", "detail": "age term"}]}
    assert "the explanation rests on" in job.failed_reason(result)


def test_a_negated_menu_item_gives_the_plain_critic_line():
    assert job._menu(_critic("organic fan reposts, not a paid campaign, of one viral clip")) is None
    assert job._menu(_critic("a collection artefact rather than a news event")) is None


def test_two_menu_items_give_the_plain_critic_line():
    assert job._menu(_critic("the hashtag collects unrelated posts from a generic event tag")) is None


def test_one_clear_menu_item_is_named():
    assert job._menu(_critic("a scraping artefact grouping generic tags")) == "a scraping or collection artefact"
    assert job._menu(_critic("a paid campaign by a drinks brand")) == "a paid campaign"


def test_known_is_not_read_as_a_negation():
    assert not job._NEGATION.search("a known collection artefact")
    assert job._NEGATION.search("not a paid campaign")


def test_a_collective_is_not_read_as_a_collection_artefact():
    assert job._menu(_critic("reposts by a local amapiano collective")) is None


# K3 names which part of the place check failed, in fixed wording that never carries the detail's ids, times or
# place names (2 Oct: two NG cards were held on K3 and the stored reason could not say which part failed).

def _k3(detail, claim_id="c1"):
    return {"claim_id": claim_id, "rule": "K3", "verdict": "cut", "detail": detail}


def test_k3_reasons_name_the_failed_part_of_the_place_check():
    cases = {
        "obs1_ab posted 2026-09-20T10:00:00+00:00, outside the window":
            "Place check: a claim cited a post outside the window",
        "obs1_ab has no readable posted_at": "Place check: a claim cited a post outside the window",
        "obs1_ab is located in 'KE', not NG": "Place check: a claim cited a post located in another market",
        "names Nigeria but cites no record located there or from its feeds":
            "Place check: a claim named a place no cited post is located in or comes from its feeds",
        "names Nigeria in feed wording but cites no record with source market NG":
            "Place check: a claim used a market's feed wording without a post from that market's feeds",
        "names Nigeria on source-market evidence without feed-scoped wording":
            "Place check: a claim described a place or people from posts only seen in that market's feeds",
        "names Nigeria outside its feed-scoped phrase; source evidence cannot support place or people claims":
            "Place check: a claim described a place or people from posts only seen in that market's feeds",
    }
    for detail, reason in cases.items():
        assert job.check_reason(_k3(detail)) == reason
        assert "Nigeria" not in reason and "obs1" not in reason and "KE" not in reason


def test_k3_sentence_and_before_repair_rows_keep_their_framing():
    sentence = _k3("short_answer: names Nigeria on source-market evidence without feed-scoped wording", None)
    assert job.check_reason(sentence) == (
        "Place check: the explanation described a place or people from posts only seen in that market's feeds")
    before = _k3("before repair: names Nigeria but cites no record located there or from its feeds")
    assert job.check_reason(before) == (
        "before repair: Place check: a claim named a place no cited post is located in or comes from its feeds")


def test_k3_rested_claim_and_unknown_detail():
    result = {"reason": "failed_checks", "rests_on": ["c1"],
              "checks": [_k3("obs1_ab is located in 'KE', not NG")]}
    assert job.failed_reason(result) == "Place check: a claim the explanation rests on cited a post located in another market"
    assert job.check_reason(_k3("something new")) == ("Place check: a claim cited a post outside the window or "
                                                      "market, or named a place no cited post is located in")
    assert job.check_reason(_k3("short_answer: something new", None)) == (
        "Place check: the explanation named a place no cited post is located in")
