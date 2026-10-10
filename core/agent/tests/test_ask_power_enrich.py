"""Ask power, item 3 notes: the enrichment pool at T1, the prices in the tool text, comments before a transcript.

On 10 October a T1 Ask spent its 12 enrichment credits on one TikTok transcript (10) and two comment pages (1 each);
the transcript errored, and the next transcript and comment page were refused for want of pool. The pool at T1 is now
35% of the tier's credits (21 of 60), beside the 55% first round. T0, T2 and T3 keep their 20%. The model's own spend,
and so the hold, is untouched.
"""

import pytest

from core.agent import ask, skills
from core.agent.context import Refused, RunContext
from core.agent.tests.test_enrich_tools import AS_OF, FakeClient, make_ctx
from core.agent.toolset import DESCRIPTIONS
from core.agent.tools import enrich_tools
from core.agent.tools.enrich_tools import enrich_credits_left, get_comments, get_transcript
from core.collect.socialcrawl_client import quote_for


def lines(n=6):
    return [{"start": i * 3.0, "end": i * 3.0 + 3.0, "text": f"spoken words number {i} in the clip"} for i in range(n)]


def test_the_t1_pool_is_thirty_five_percent_of_its_credits():
    c = make_ctx("T1")
    assert enrich_credits_left(c) == pytest.approx(21.0)
    assert enrich_tools.ENRICH_SHARE == 0.2  # the default every other tier keeps


@pytest.mark.parametrize("tier,credits", [("T0", 2.0), ("T2", 60.0), ("T3", 120.0)])
def test_the_other_tiers_keep_twenty_percent(tier, credits):
    assert enrich_credits_left(make_ctx(tier)) == pytest.approx(credits)


def test_a_t2_researcher_keeps_twenty_percent_of_its_own_slice():
    c = make_ctx("T2")
    c.limits = {"credits": 100.0, "calls": 10, "max_turns": 10, "max_budget_usd": 1.0}
    assert enrich_credits_left(c) == pytest.approx(20.0)


def test_two_transcripts_and_comments_fit_at_t1_and_a_third_transcript_does_not():
    c = make_ctx("T1")
    c.evidence["tiktok_2"] = {**c.evidence["tiktok_7412"], "id": "tiktok_2", "url": "https://www.tiktok.com/@chef_za/video/2"}
    c.evidence["tiktok_3"] = {**c.evidence["tiktok_7412"], "id": "tiktok_3", "url": "https://www.tiktok.com/@chef_za/video/3"}
    get_transcript(c, FakeClient(quote=10.0, items=lines()), "tiktok_7412")
    get_transcript(c, FakeClient(quote=10.0, items=lines()), "tiktok_2")
    get_comments(c, FakeClient(quote=1.0), "tiktok_3", limit=5, max_credits=1.0)
    assert c.enrich_credits_spent == 21.0
    third = FakeClient(quote=10.0, items=lines())
    with pytest.raises(Refused, match="35%"):
        get_transcript(c, third, "tiktok_3")
    assert third.calls == []


def test_the_staging_run_of_10_october_now_gets_its_second_comment_page_and_a_second_look():
    c = make_ctx("T1")
    get_transcript(c, FakeClient(quote=10.0, items=[], status="error", charged=10.0), "tiktok_7412")  # errored, charged
    c.evidence["tiktok_2"] = {**c.evidence["tiktok_7412"], "id": "tiktok_2", "url": "https://www.tiktok.com/@chef_za/video/2"}
    get_comments(c, FakeClient(quote=1.0), "tiktok_7412", limit=5, max_credits=1.0)
    get_comments(c, FakeClient(quote=1.0), "tiktok_2", limit=5, max_credits=1.0)
    get_comments(c, FakeClient(quote=1.0), "tiktok_2", limit=5, max_credits=1.0)
    assert c.enrich_credits_spent == 13.0 and enrich_credits_left(c) == pytest.approx(8.0)


def test_the_comment_cap_passed_to_the_client_is_the_pools_remainder():
    c, client = make_ctx("T1"), FakeClient(quote=1.0)
    get_comments(c, client, "tiktok_7412", limit=5, max_credits=50)
    assert client.calls[0]["max_credits"] == pytest.approx(60 * enrich_tools.T1_ENRICH_SHARE)


# The prices the model is told are the prices the client quotes


def price(route):
    return f"{quote_for(route, 'GET', {'url': 'https://example.test/a'}):g}"


def test_the_transcript_text_states_the_quoted_prices():
    text = DESCRIPTIONS["get_transcript"]
    assert f"TikTok {price('tiktok/post/transcript')} credits" in text
    assert f"YouTube {price('youtube/video/transcript')} credits" in text


def test_the_comment_text_states_the_quoted_price_and_the_pool():
    text = DESCRIPTIONS["get_comments"]
    assert f"TikTok {price('tiktok/post/comments')} credit a page" in text
    assert "35% at T1" in text and "20%" in text


def test_the_skill_asks_for_comments_before_a_transcript():
    body = skills.load_skill("culture-read")
    assert "get_comments first" in body and "get_transcript" in body


def test_enrichment_does_not_move_the_t1_hold():
    assert ask.hold_usd("T1", ask.MODEL) == pytest.approx(4.916)
