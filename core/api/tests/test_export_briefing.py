"""The export follows the page's briefing order and tells comments from posts (answer fixture shaped like a live ask)."""
import html
import json
import re
from pathlib import Path

from core.api.export import render_answer_html

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def page():
    record = json.loads((FIXTURES / "ask_comments_repeat.json").read_text(encoding="utf-8"))
    return render_answer_html(record)


def text_of(markup):
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


def test_comments_are_labelled_as_comments_on_the_post_and_link_to_it():
    markup = page()
    assert "Comment on @lerato_clips_22&#x27;s post (TikTok)" in markup
    assert "ngwenya.tales&#x27;s post" not in markup  # its comments backed only a merged claim, so they are gone
    assert "<h3>[5] TikTok · pretty.zodwa</h3>" not in markup
    assert "TikTok · mkhize.lwazi" not in markup
    # the comment's link is the post's link
    assert markup.count("https://www.tiktok.com/@lerato_clips_22/video/7701000000000000002") >= 3


def test_what_commenters_said_sits_apart_from_the_posts_in_the_finding():
    markup = page()
    finding = re.search(r'<article class="finding">(?:(?!</article>).)*What commenters said.*?</article>', markup, re.S)
    assert finding, "the commenters row belongs to the finding that cites the comments"
    block = finding.group(0)
    assert '<ul class="posts">' not in block  # commenters are not posts
    assert "On <a" in block and "lerato_clips_22" in block


def test_the_scope_line_says_what_the_answer_rests_on_before_what_was_read():
    scope = re.search(r'<p class="scope">(.*?)</p>', page()).group(1)
    assert scope == ("Answer based on 3 posts by 3 creators and 2 comments · 169 posts read on 8 platforms · "
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


def test_the_research_count_matches_the_lines_listed():
    markup = page()
    summary = re.search(r"How this was researched · (\d+) steps?</summary>(.*?)</details>", markup, re.S)
    assert int(summary.group(1)) == len(re.findall(r"<li>", summary.group(2))) == 32


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
             "What we do not know", "How this was researched", "<h2>Sources</h2>", "Technical details"]
    at = [markup.index(token) for token in order]
    assert at == sorted(at), dict(zip(order, at))


def test_a_gap_that_starts_with_a_verb_is_not_labelled_searched_again():
    markup = page()
    assert "Searched: Searched" not in markup and "Searched: Inspected" not in markup
    assert "<p class=\"note\">Inspected audio metadata" in markup
    assert "<p class=\"note\">Searched posts matching #funnyclip" in markup
