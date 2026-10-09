"""W8-DEC-12 in the explainer: a news public-feed post is feed evidence only. The writer is not told it is a local
post, and its handle is not a local creator reacting in the news-driven rule."""

import json
import re

from core.brief import explain
from core.brief.tests.test_brief_explain import (
    CANDIDATE, NEWS_REACTION, W_END, W_START, FakeModel, assert_numbers_only, critic_row, good, make_pack, outside_fences,
    run)


def writer_heads(user):
    return {head["id"]: head for head in
            (json.loads(m) for m in re.findall(r"^post (\{.*\})$", outside_fences(user), flags=re.M))}


def writer_scope(user):
    [line] = [l for l in outside_fences(user).splitlines() if l.startswith("Posts you may cite")]
    return line


def writer_counts(user):
    [line] = [l for l in outside_fences(user).splitlines() if l.startswith("Counts, for your wording only")]
    return line


def feed_post(eid, platform, handle, *, market=None, source="ZA"):
    return {"id": eid, "platform": platform, "handle": handle, "url": f"https://x/{eid}",
            "posted_at": "2026-09-26T19:40:00+02:00", "market": market, "source_market": source,
            "text": "Shaya step at every braai this weekend", "engagement": {"views": 900},
            "flags": [] if market else ["market_assumed"]}


def mixed_pack():
    pack = make_pack()
    pack["evidence"] = [
        pack["evidence"][0], pack["evidence"][2],
        feed_post("news_a", "news", "@outlet_a"), feed_post("news_b", "news", "@outlet_b"),
        feed_post("own_c", "tiktok", "@feed_creator"),
        feed_post("news_foreign", "news", "@outlet_c", market="NG", source="NG"),
    ]
    return pack


# The writer prompt


def test_a_news_post_found_in_the_markets_feed_is_marked_feed_evidence_and_not_local():
    user = explain._writer_user(CANDIDATE, mixed_pack(), "ZA", W_START, W_END)
    heads = writer_heads(user)

    assert heads["news_a"]["local"] is False and heads["news_a"]["feed_evidence"] is True
    assert heads["news_b"]["local"] is False and heads["news_b"]["feed_evidence"] is True


def test_other_posts_keep_their_marks():
    heads = writer_heads(explain._writer_user(CANDIDATE, mixed_pack(), "ZA", W_START, W_END))

    for eid in ("tt_1", "tt_3", "own_c"):
        assert heads[eid]["local"] is True and "feed_evidence" not in heads[eid]
    assert heads["news_foreign"]["citable"] is False and "feed_evidence" not in heads["news_foreign"]


def test_the_scope_line_lists_news_as_feed_evidence_and_not_among_the_local_posts():
    scope = writer_scope(explain._writer_user(CANDIDATE, mixed_pack(), "ZA", W_START, W_END))

    assert 'marked local true: ["tt_1", "tt_3", "own_c"]' in scope
    assert 'found in South Africa\'s feeds with location unknown: ["own_c"]' in scope
    assert 'marked feed_evidence true: ["news_a", "news_b"]' in scope
    assert "never count among the local posts" in scope


def test_the_counts_line_counts_local_posts_without_news():
    counts = writer_counts(explain._writer_user(CANDIDATE, mixed_pack(), "ZA", W_START, W_END))

    assert "3 local posts from 3 different creators" in counts


def test_a_pack_without_news_gets_the_same_scope_line_as_before():
    scope = writer_scope(explain._writer_user(CANDIDATE, make_pack(), "ZA", W_START, W_END))

    assert "feed_evidence" not in scope
    assert 'marked local true: ["tt_1", "ig_2", "tt_3"]' in scope
    assert all("feed_evidence" not in head for head in
               writer_heads(explain._writer_user(CANDIDATE, make_pack(), "ZA", W_START, W_END)).values())


def test_the_critic_still_marks_a_news_own_feed_post_own_feed_local_so_feed_wording_is_kept():
    assert explain._own_feed_local(feed_post("n", "news", "@o"), "ZA") is True


# The news-driven rule


def news_cited_pack(platform):
    """Two local posts by one creator, and a third post by another handle on the given platform. All three are cited
    by the claims the sentence rests on, so the two local posts satisfy the 2-local rule and the handles are two."""
    pack = make_pack()
    pack["evidence"][1] = dict(pack["evidence"][1], platform=platform)
    pack["evidence"][2] = dict(pack["evidence"][2], handle="@thandi_moves")
    return pack


def test_a_news_handle_does_not_make_a_second_local_creator_reacting():
    result = run(FakeModel([good()], critic=NEWS_REACTION), pack=news_cited_pack("news"))

    assert_numbers_only(result, "failed_checks")
    assert critic_row(result)["verdict"] == "cut"


def test_the_same_two_handles_with_the_second_on_another_platform_pass_the_news_driven_rule():
    result = run(FakeModel([good()], critic=NEWS_REACTION), pack=news_cited_pack("instagram"))

    assert result["reason"] is None and result["news_driven"] is True


def test_one_news_handle_beside_two_local_creators_still_passes():
    pack = make_pack()
    pack["evidence"][1] = dict(pack["evidence"][1], platform="news")
    result = run(FakeModel([good()], critic=NEWS_REACTION), pack=pack)

    assert result["reason"] is None and result["news_driven"] is True
