"""The export follows the page's briefing order and tells comments from posts (answer fixture shaped like a live ask)."""
import html
import json
import re
from pathlib import Path

from core.api.export import render_answer_html

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def page():
    """The fixture with c1 and c7 saying c2 again (same quoted posts and words, same figures and queries)."""
    record = json.loads((FIXTURES / "ask_comments_repeat.json").read_text(encoding="utf-8"))
    claims = {c["id"]: c for c in record["answer"]["claims"]}
    claims["c1"]["numbers"] = json.loads(json.dumps(claims["c2"]["numbers"]))
    claims["c7"]["quotes"] = json.loads(json.dumps(claims["c2"]["quotes"]))
    claims["c7"]["numbers"] = json.loads(json.dumps(claims["c2"]["numbers"]))
    return render_answer_html(record)


def text_of(markup):
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


def test_comments_are_labelled_as_comments_on_the_post_and_link_to_it():
    markup = page()
    assert "Comment on @lerato_clips_22&#x27;s post (TikTok)" in markup
    assert markup.count("Comment on @ngwenya.tales&#x27;s post (TikTok)") == 2  # c3 is its own finding, it stays
    assert "<h3>[5] TikTok · pretty.zodwa</h3>" not in markup
    assert "TikTok · mkhize.lwazi" not in markup
    # the comment's link is the post's link
    assert markup.count("https://www.tiktok.com/@lerato_clips_22/video/7701000000000000002") >= 3


def test_what_commenters_said_sits_apart_from_the_posts_in_the_finding():
    markup = page()
    blocks = [m.group(0) for m in re.finditer(r'<article class="finding">(?:(?!</article>).)*?</article>', markup, re.S)
              if "What commenters said" in m.group(0)]
    assert blocks, "the commenters row belongs to the findings that cite the comments"
    for block in blocks:  # commenters are not posts: a posts list comes first and the row is outside it
        if '<ul class="posts">' in block:
            assert block.index("</ul>", block.index('<ul class="posts">')) < block.index("What commenters said")
    only = [block for block in blocks if '<ul class="posts">' not in block]
    assert only and "On <a" in only[0] and "lerato_clips_22" in only[0]


def test_the_scope_line_says_what_the_answer_rests_on_before_what_was_read():
    scope = re.search(r'<p class="scope">(.*?)</p>', page()).group(1)
    assert scope == ("Answer based on 3 posts by 3 creators and 4 comments · 169 posts read on 8 platforms · "
                     "3 208 posts in the store for 4 to 10 October 2026")


def test_one_confidence_tag_for_the_whole_answer():
    markup = page()
    assert '<span class="tag">Single source</span>' in markup
    assert markup.count('class="tag"') == 1


def test_each_distinct_figure_is_shown_once():
    markup = page()
    block = re.search(r'<ul class="figures">(.*?)</ul>', markup, re.S).group(1)
    items = [text_of(i).split() and " ".join(text_of(i).split()) for i in re.findall(r"<li[^>]*>(.*?)</li>", block, re.S)]
    assert len(items) == len(set(items))
    assert items.count("3 posts") == 1 and items.count("3 creators") == 1


def test_platforms_are_counted_once_and_a_failed_call_is_a_plain_note():
    markup = page()
    assert "TikTok 100" in markup and "Instagram 40" in markup and "YouTube 29" in markup
    assert markup.count("TikTok 100") == 1
    assert "1 TikTok search failed" in markup


def test_the_export_lists_no_research_steps_because_a_step_can_name_a_person_the_readers_never_saw():
    markup = page()
    assert "How this was researched" not in markup
    assert "Counting posts tagged #funnyclip, step 1" not in markup and "Reading TikTok post 0 for its words" not in markup


def test_internal_text_stays_inside_the_technical_details():
    markup = page()
    outside = re.sub(r"<details>.*?</details>", "", markup, flags=re.S)
    for leak in ("tiktok/post/transcript", "Why: error", "stored evidence", "the what to watch next text",
                 "did not report what it cost"):
        assert leak not in outside
    assert "did not report what it cost" in markup  # the model-usage notice is kept, in the technical details


def test_the_sections_come_in_the_briefing_order():
    markup = page()
    order = ["<h1>", 'class="tag"', 'class="scope"', "Short answer", "What we found", "The numbers",
             "What we do not know", "<h2>Sources</h2>", "Technical details"]
    at = [markup.index(token) for token in order]
    assert at == sorted(at), dict(zip(order, at))


def test_a_gap_that_starts_with_a_verb_is_not_labelled_searched_again():
    markup = page()
    assert "Searched: Searched" not in markup and "Searched: Inspected" not in markup
    assert "<p class=\"note\">Inspected audio metadata" in markup
    assert "<p class=\"note\">Searched posts matching #funnyclip" in markup
