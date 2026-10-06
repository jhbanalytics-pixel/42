"""The support check's marker for posts from the market's own feeds (Albert, 2 and 3 Oct).

The critic is told which cited posts are own-feed local (found in the market's own feeds, location unknown) and
that such a post counts as seen in that market's feeds, one confidence step lower. The support check saw the same
post only as location unknown, so it failed a claim the critic would accept. It now gets the same own_feed_local
marker, built by the same helper the critic uses, and the same rule. A located post, an unmarked post and a post
located in another market carry no marker. Every other support rule stays as it was.
"""

import re

from core.brief import explain
from core.brief.tests.test_brief_critic_own_feeds import ledger, own_feed_pack
from core.brief.tests.test_brief_explain import heads
from core.brief.tests.test_brief_explain_local import CANDIDATE, CitesWhatItIsShown, islamicvideo_pack, run_ng

CLAIM = {"text": "Seen in Nigeria's feeds, posts share Friday reminder clips.", "label": "single_source",
         "numbers": []}


def support_marks(market="NG", pack=None):
    pack = pack or own_feed_pack()
    prompt = explain._support_user(CLAIM, pack["evidence"], pack, market)
    return {h["id"]: h.get("own_feed_local") for h in heads(prompt)}


def test_the_support_prompt_marks_an_own_feed_post_and_no_located_or_unmarked_post():
    assert support_marks() == {
        "ev_feed_a": True,
        "ev_feed_b": True,
        "ev_ng": None,
        "ev_gh": None,
        "ev_ke": None,
        "ev_feed_uncited": True,
    }


def test_the_support_marker_is_the_one_the_critic_gets():
    pack = own_feed_pack()
    critic = explain._critic_user(CANDIDATE, "NG", "A sentence.", ledger(), pack, ("c1", "c2"))
    critic_marked = {h["id"] for h in heads(critic) if h["own_feed_local"]}
    support_marked = {i for i, mark in support_marks(pack=pack).items() if mark}
    assert support_marked == critic_marked == {"ev_feed_a", "ev_feed_b", "ev_feed_uncited"}


def test_a_post_located_in_another_market_is_never_marked_local():
    pack = own_feed_pack()
    pack["evidence"][0].update(market="KE", flags=["market_assumed"])
    marks = support_marks(pack=pack)
    assert marks["ev_feed_a"] is None and marks["ev_gh"] is None and marks["ev_feed_b"] is True
    assert explain._own_feed_local(pack["evidence"][0], "NG") is False
    assert explain._own_feed_local(pack["evidence"][3], "NG") is False


def test_a_located_post_is_never_marked_own_feed_local():
    pack = own_feed_pack()
    assert explain._own_feed_local(pack["evidence"][2], "NG") is False


def test_no_post_is_marked_without_a_market():
    assert set(support_marks(market=None).values()) == {None}


def test_the_sentence_check_carries_the_marker_for_the_posts_it_rests_on():
    pack = own_feed_pack()
    records = {r["id"]: r for r in pack["evidence"]}
    prompt = explain._sentence_user("Seen in Nigeria's feeds, reminders likely follow Friday prayers.",
                                    ledger(), ("c1",), records, pack, "NG")
    assert {h["id"]: h.get("own_feed_local") for h in heads(prompt)} == {
        "ev_feed_a": True, "ev_feed_b": True, "ev_ng": None}


def test_explain_trend_gives_every_support_check_the_marker():
    model = CitesWhatItIsShown()
    run_ng(model, islamicvideo_pack())
    support = [c["user"] for c in model.calls if c["support"]]
    assert support
    for user in support:
        marks = {h["id"]: h.get("own_feed_local") for h in heads(user)}
        assert marks and set(marks) <= {"ev_a", "ev_b"} and set(marks.values()) == {True}


def support_text():
    return " ".join(explain.SUPPORT_SYSTEM.split())


def test_support_system_reads_an_own_feed_post_as_the_critic_does():
    assert ("A cited post marked own_feed_local true was found in the market's own feeds and its location is "
            "unknown. It can support a claim that words it as seen in that market's feeds, such as \"seen in "
            "Kenya's feeds\", never as what people there are or do, and it stands one confidence step lower than a "
            "post located in the market. It never counts as a post located in the market, and a post located in "
            "another market is never local.") in support_text()
    assert "one confidence step lower than a post located in the market" in " ".join(explain.CRITIC_SYSTEM.split())


def test_the_support_rules_around_it_are_unchanged():
    text = support_text()
    for kept in (
        "A physical place or people claim, such as \"in Kenya\" or \"Nigerian creators\", is supported only when a "
        "cited post's market shows that place. A post whose location is unknown supports no place claim about "
        "physical location or people, whatever its text or handle suggests.",
        "A post whose source_market is another market supports nothing about the market a claim names. Source-only "
        "market evidence carries a claim label one step below located evidence.",
        "A spread beyond one market, such as \"across the continent\", \"African creators\" or \"West Africa\", is "
        "supported only when cited posts are located in each market it implies.",
        "is checked as an observation, not as interpretation",
    ):
        assert kept in text
    assert set(explain.SUPPORT_SCHEMA["properties"]) == {"verdict", "reason"}


def test_the_new_support_wording_carries_no_nationality_or_age_framing():
    banned = re.compile(r"\b(?:nigerians?|kenyans?|south africans?|ghanaians?|gen ?z|millennials?|boomers?|youth|"
                        r"young|teens?|teenagers?|older|elderly|generation)\b", re.I)
    start = explain.SUPPORT_SYSTEM.index("A cited post marked own_feed_local true")
    new = explain.SUPPORT_SYSTEM[start:explain.SUPPORT_SYSTEM.index("A spread beyond one market")]
    assert not banned.search(new)
