"""The critic's pattern line (core/brief/explain.py _shared_patterns, 18 Oct 2026).

Genre topics (skits, food, pets) were cut although their posts shared a sound or a format: the critic had to see that
by reading each post's fields one by one. Code now counts, over the posts shown only, each sound letter and each
format other than other and news_clip that 2 or more different creators share, with the post ids, the different
creators and the different posting days, and says none when nothing is shared. It adds a line to the prompt and
changes no gate or pass rule. A post's video_format, the video model's format for the clip, is counted beside its
enriched formats with the same exclusions (20 Oct 2026).
"""

import json

from core.brief import explain
from core.brief.tests.test_brief_explain import outside_fences

# The formats the line leaves out, as the line words them (core/brief/pattern_formats.py).
LEFT_OUT = "talking_head, other, news_clip, skit, tutorial, slideshow, meme_image, duet and stitch left out"
CANDIDATE = {"item_id": "it_skit", "kind": "hashtag", "title": "#officeskit", "state": "rising"}
LEDGER = [{"id": "c1", "text": "Two creators post office skits.", "label": "observed", "kind": "observation",
           "evidence_ids": ["p1", "p2"]}]


def post(eid, handle, day, *, sound=None, formats=(), platform="tiktok", video_format=None):
    return {"id": eid, "platform": platform, "handle": handle, "url": f"https://x/{eid}",
            "posted_at": f"2026-10-{day:02d}T09:00:00+01:00", "market": "NG", "source_market": "NG",
            "text": "office skit", "quote_text": "office skit", "engagement": {"views": 10}, "flags": [],
            "sound_id": sound, "formats": list(formats), "video_format": video_format}


def pattern_lines(pack):
    prompt = outside_fences(explain._critic_user(CANDIDATE, "NG", "A sentence.", LEDGER, pack, ("c1",)))
    [block] = [b for b in prompt.split("\n\n") if b.startswith("Shared sounds and formats, counted by code")]
    head, *rows = block.split("\n")
    return head, rows


def shared(pack):
    head, rows = pattern_lines(pack)
    return {} if rows == ["none"] else {r["pattern"]: r for r in map(json.loads, rows)}


def test_a_sound_and_a_format_shared_by_different_creators_are_counted_with_posts_creators_and_days():
    pack = {"evidence": [post("p1", "@ada", 10, sound="9911", formats=["dance"]),
                         post("p2", "bayo", 11, sound="9911", formats=["dance", "talking_head"]),
                         post("p3", "chi", 11, sound="9911", formats=["dance"]),
                         post("p4", "ada", 12, formats=["dance"])]}
    head, _ = pattern_lines(pack)
    # 22 Oct 2026: the critic's line counts the posts the writer may cite, and says so.
    assert ("over the 4 posts shown here that the writer may cite, posts located in another market left out, not the "
            "whole trend") in head
    got = shared(pack)
    assert got["sound A"] == {"pattern": "sound A", "post_ids": ["p1", "p2", "p3"], "creators": 3, "days": 2}
    # ada and @ada are one creator: the handle is read as creator_key reads it.
    assert got["format dance"] == {"pattern": "format dance", "post_ids": ["p1", "p2", "p3", "p4"], "creators": 3,
                                   "days": 3}
    assert "format talking_head" not in got  # talking_head shows no adopted practice, so it is never a pattern
    assert "9911" not in "\n".join(pattern_lines(pack)[1])  # the vendor sound id never reaches the model


def test_one_creator_other_news_clip_and_unenriched_posts_show_no_pattern():
    pack = {"evidence": [post("p1", "ada", 10, sound="s1", formats=["news_clip", "other"]),
                         post("p2", "ada", 11, sound="s1", formats=["dance"]),
                         post("p3", "bayo", 11, formats=["news_clip"]),
                         post("p4", "chi", 12, formats=["other"]),
                         post("p5", "dayo", 12)]}
    head, rows = pattern_lines(pack)
    assert rows == ["none"]
    assert LEFT_OUT in head and "2 or more different creators" in head


def test_talking_head_shared_by_different_creators_is_no_pattern_while_a_specific_format_beside_it_is():
    # 18 Oct 2026: the prompts say talking_head alone shows no adopted practice, yet the line counted it as a shared
    # format once two creators used it, so the critic was handed a pattern the prompt told it to ignore. 23 Oct 2026:
    # a genre such as tutorial is left out the same way (test_brief_genre_formats.py).
    pack = {"evidence": [post("p1", "ada", 10, formats=["talking_head"]),
                         post("p2", "bayo", 11, formats=["talking_head", "dance", "tutorial"]),
                         post("p3", "chi", 12, formats=["dance", "tutorial"])]}
    assert explain.PATTERN_SKIP_FORMATS[:3] == ("talking_head", "other", "news_clip")
    got = shared(pack)
    assert "format talking_head" not in got and "format tutorial" not in got
    assert got["format dance"] == {"pattern": "format dance", "post_ids": ["p2", "p3"], "creators": 2, "days": 2}


def test_the_same_sound_id_on_two_platforms_is_two_sounds():
    pack = {"evidence": [post("p1", "ada", 10, sound="s1"), post("p2", "bayo", 11, sound="s1", platform="instagram")]}
    assert shared(pack) == {}


def test_a_post_without_a_handle_adds_no_creator_and_an_unreadable_date_adds_no_day():
    a, b, c = post("p1", "ada", 10, sound="s1"), post("p2", "", 11, sound="s1"), post("p3", "bayo", 12, sound="s1")
    c["posted_at"] = "not a date"
    assert shared({"evidence": [a, b]}) == {}
    assert shared({"evidence": [a, b, c]})["sound A"] == {"pattern": "sound A", "post_ids": ["p1", "p2", "p3"],
                                                         "creators": 2, "days": 2}


def test_the_critic_is_told_what_the_line_is_and_that_a_posting_day_is_no_why_now():
    text = " ".join(explain.CRITIC_SYSTEM.split())
    assert ("A line counted by code lists each sound letter and each format, talking_head, other, news_clip, skit, "
            "tutorial, slideshow, meme_image, duet and stitch left out,") in text
    assert "It counts only those posts, never the whole trend" in text
    assert "among the posts shown here that the writer may cite" in text
    assert "A posting day shows when a post was published, never a why-now." in text


def test_a_video_format_shared_by_three_creators_is_counted_and_a_generic_one_is_not():
    # 20 Oct 2026: the video model's format was shown to the critic in each post's head but never counted, so clips
    # the video model read as one dance, with no enriched format, showed no shared format on the line.
    pack = {"evidence": [post("p1", "ada", 10, video_format="dance"),
                         post("p2", "bayo", 11, video_format="dance"),
                         post("p3", "chi", 12, formats=["dance"], video_format="dance"),
                         post("p4", "dayo", 12, video_format="talking_head"),
                         post("p5", "efe", 13, video_format="talking_head"),
                         post("p6", "femi", 13, video_format="news_clip"),
                         post("p7", "gbenga", 14, video_format="news_clip"),
                         post("p8", "hauwa", 14, video_format="other"),
                         post("p9", "ife", 15, formats=["other"], video_format="other")]}
    head, _ = pattern_lines(pack)
    assert f"each format in formats or video_format ({LEFT_OUT})" in head
    # p3 names dance in both fields and is counted once; talking_head, news_clip and other are never a pattern.
    assert shared(pack) == {"format dance": {"pattern": "format dance", "post_ids": ["p1", "p2", "p3"],
                                             "creators": 3, "days": 3}}


def test_a_post_whose_caption_and_clip_name_different_formats_counts_toward_both():
    pack = {"evidence": [post("p1", "ada", 10, formats=["skit"], video_format="dance"),
                         post("p2", "bayo", 11, formats=["skit"]),
                         post("p3", "chi", 12, video_format="dance")]}
    # The clip's dance counts beside the caption's format; skit, a genre, counts toward nothing (23 Oct 2026).
    assert shared(pack) == {
        "format dance": {"pattern": "format dance", "post_ids": ["p1", "p3"], "creators": 2, "days": 2}}


def test_the_writer_pattern_line_counts_the_video_format_too():
    from core.brief.tests.test_brief_explain import W_END, W_START

    pack = {"evidence": [post("p1", "ada", 10, video_format="dance"), post("p2", "bayo", 11, video_format="dance"),
                         post("p3", "chi", 12, video_format="dance")]}
    user = outside_fences(explain._writer_user(CANDIDATE, pack, "NG", W_START, W_END))
    [block] = [b for b in user.split("\n\n") if b.startswith("Shared sounds and formats, counted by code")]
    assert json.loads(block.split("\n")[1]) == {"pattern": "format dance", "post_ids": ["p1", "p2", "p3"],
                                                 "creators": 3, "days": 3}
