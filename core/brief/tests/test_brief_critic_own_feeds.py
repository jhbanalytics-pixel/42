"""The critic's scope for posts from the market's own feeds (Albert, 29 and 30 Sept, 2 Oct).

Specificity and the writer count a post found in the market's own feeds as local, but the critic was told only
which cited posts are located in the market. A pack whose local posts are all own-feed posts could then never
pass the critic's local why-now. The critic is now also told, in a separate field and a separate line, which
cited posts are own-feed local, and that such a post can show a local why-now worded as seen in that market's
feeds, one confidence step lower. located_in_market and every other critic rule stay as they were.
"""

import re

from core.brief import explain
from core.brief.tests.test_brief_explain import heads, outside_fences
from core.brief.tests.test_brief_explain_local import CANDIDATE, ng_post


def own_feed_pack():
    """Two cited own-feed posts with unknown location, one cited located post, one cited post found in Nigeria's
    feeds but located in Ghana, one cited post from Kenya's feeds and one own-feed post no resting claim cites."""
    return {
        "evidence": [
            ng_post("ev_feed_a", "@instablog9ja", "Jumat reminder before prayers today"),
            ng_post("ev_feed_b", "@lindaikejiblogofficial", "Friday reminder clip for everyone"),
            ng_post("ev_ng", "@abuja_deen", "Jumat reminder from our masjid this Friday", market="NG"),
            ng_post("ev_gh", "@deen_daily", "Short reminder on patience", market="GH"),
            ng_post("ev_ke", "@nairobi_gossip_club", "Reminder clip for the week", source_market="KE"),
            ng_post("ev_feed_uncited", "@gossipmilltv", "Another reminder clip"),
        ],
        "numbers": [],
        "facts": [],
    }


def ledger():
    return [
        {"id": "c1", "text": "Seen in Nigeria's feeds, two posts share Friday reminder clips.", "label": "single_source",
         "kind": "observation", "evidence_ids": ["ev_feed_a", "ev_feed_b", "ev_ng"]},
        {"id": "c2", "text": "Other posts share reminders.", "label": "single_source", "kind": "observation",
         "evidence_ids": ["ev_gh", "ev_ke"]},
        {"id": "c3", "text": "An uncited clip.", "label": "single_source", "kind": "observation",
         "evidence_ids": ["ev_feed_uncited"]},
    ]


def critic_prompt(rests_on=("c1", "c2")):
    return explain._critic_user(CANDIDATE, "NG", "Seen in Nigeria's feeds, reminders likely follow Friday prayers.",
                                ledger(), own_feed_pack(), rests_on)


def scope_lines(prompt):
    return [line for line in outside_fences(prompt).splitlines()
            if line.startswith("Your scope:") or ("own_feed_local" in line and not line.startswith("post "))]


def test_each_post_is_marked_own_feed_local_separately_from_located_in_market():
    marks = {h["id"]: (h["cited_by_resting_claims"], h["located_in_market"], h["own_feed_local"])
             for h in heads(critic_prompt())}
    assert marks == {
        "ev_feed_a": (True, False, True),
        "ev_feed_b": (True, False, True),
        "ev_ng": (True, True, False),
        "ev_gh": (True, False, False),
        "ev_ke": (True, False, False),
        "ev_feed_uncited": (False, False, True),
    }


def test_the_scope_names_the_cited_own_feed_posts_on_their_own_line_and_never_as_located():
    lines = scope_lines(critic_prompt())
    [scope] = [line for line in lines if line.startswith("Your scope:")]
    [own] = [line for line in lines if not line.startswith("Your scope:")]
    assert scope.endswith('marked located_in_market true: ["ev_ng"].')
    assert "own_feed_local" not in scope
    assert 'marked own_feed_local true: ["ev_feed_a", "ev_feed_b"]' in own
    assert "seen in Nigeria's feeds" in own
    assert "one confidence step lower" in own
    assert "ev_ng" not in own and "ev_gh" not in own and "ev_ke" not in own and "ev_feed_uncited" not in own


def test_the_own_feed_scope_is_empty_when_the_sentence_rests_on_nothing():
    [own] = [line for line in scope_lines(critic_prompt(rests_on=())) if not line.startswith("Your scope:")]
    assert "marked own_feed_local true: []" in own


def test_a_market_assumed_post_stated_in_another_market_is_never_own_feed_local():
    pack = own_feed_pack()
    pack["evidence"][0].update(market="KE", flags=["market_assumed"])
    prompt = explain._critic_user(CANDIDATE, "NG", "A sentence.", ledger(), pack, ("c1",))
    marks = {h["id"]: h["own_feed_local"] for h in heads(prompt)}
    assert marks["ev_feed_a"] is False and marks["ev_feed_b"] is True


def critic_text():
    return " ".join(explain.CRITIC_SYSTEM.split())


def test_the_critic_is_told_an_own_feed_post_can_show_a_local_why_now_in_feed_wording_one_step_lower():
    text = critic_text()
    assert ("A cited post marked own_feed_local true was found in the market's own feeds and its location is "
            "unknown. It can show a local why-now when the sentence words it as seen in that market's feeds, such as "
            "\"seen in Kenya's feeds\", never as what people there are or do, and it stands one confidence step "
            "lower than a post located in the market.") in text


def test_the_critic_rules_around_it_are_unchanged():
    text = critic_text()
    for kept in (
        "Name the simplest non-cultural explanation for why these posts rose: a paid campaign or sponsored push, a "
        "platform feature change, a bot or coordinated push, a news event or scheduled event that alone accounts for "
        "the posts, a scraping or collection artefact, or a single viral post or one creator carrying the count.",
        "Evaluate only the why-now clause and the local posts cited by its supporting claims.",
        "Set local_why_now true only when those sources support a timely local cause or a cited post states the "
        "timing.",
        "Generic country labels, popularity alone, a missing time hook, unrelated local examples, and source-only "
        "posts used to claim physical local people or places require false. Source-only evidence can support feed "
        "wording only.",
    ):
        assert kept in text
    assert set(explain.CRITIC_SCHEMA["properties"]) == {"non_cultural_explanation", "ruled_out", "news_driven",
                                                         "scheduled_event", "local_reaction", "local_why_now",
                                                         "reason"}


def test_the_new_critic_wording_carries_no_nationality_or_age_framing():
    banned = re.compile(r"\b(?:nigerians?|kenyans?|south africans?|ghanaians?|gen ?z|millennials?|boomers?|youth|"
                        r"young|teens?|teenagers?|older|elderly|generation)\b", re.I)
    new = [line for line in explain.CRITIC_SYSTEM.splitlines() if "own_feed_local" in line]
    assert new and not [line for line in new if banned.search(line)]
    assert not [line for line in scope_lines(critic_prompt()) if banned.search(line)]


def test_an_own_feed_only_pack_gives_the_critic_cited_local_posts_to_weigh():
    pack = own_feed_pack()
    pack["evidence"] = [r for r in pack["evidence"] if r["id"] in ("ev_feed_a", "ev_feed_b", "ev_feed_uncited")]
    prompt = explain._critic_user(CANDIDATE, "NG", "A sentence.", ledger(), pack, ("c1",))
    rest = outside_fences(prompt)
    assert 'marked located_in_market true: [].' in rest
    assert 'marked own_feed_local true: ["ev_feed_a", "ev_feed_b"]' in rest


def test_the_critic_and_support_prompts_are_byte_for_byte_unchanged_by_the_writer_feed_wording_line():
    """BR-1 (4 Oct) clarified only the writer's law 12. What the critic and the support check trust is unchanged:
    their system prompts are pinned to the bytes they had at 80d3948c."""
    import hashlib

    assert hashlib.sha256(explain.CRITIC_SYSTEM.encode("utf-8")).hexdigest() == (
        "8c58bf3b6b15059a23060ecdb84c674acf8ddb80219da00ecaa4ede49e1090b6")
    assert hashlib.sha256(explain.SUPPORT_SYSTEM.encode("utf-8")).hexdigest() == (
        "0da133fe7fb64a1bf099d653b147370a55d17dc37dee38777b4657d0df69f123")
