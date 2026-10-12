"""A card with no written title shows a plain title built in code from its cluster label (Albert, 12 Oct 2026, "code
titles"). On 12 Oct ZA card 03 showed "bafanabafana, mofokeng, relebohile" because the title check dropped the
writer's title. The cases live in fixtures/code_titles.json, which the app's tests read too."""
import json
import re
import unicodedata
from pathlib import Path

import pytest

from core.api import channel, code_title, digest, today

CASES = json.loads((Path(__file__).parent / "fixtures" / "code_titles.json").read_text(encoding="utf-8"))["cases"]
FIXED_WORDS = {"posts", "about", "and"}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_code_title_cases(case):
    assert code_title.code_title(case["card"]) == case["title"]


@pytest.mark.parametrize("case", [c for c in CASES if "count_line" in c], ids=lambda c: c["name"])
def test_code_count_line_names_the_card_as_its_title_does(case):
    card = dict(case["card"], title_written=None)
    assert code_title.code_count_line(card, card["count_line"]) == case["count_line"]


def test_every_case_is_read():
    # A parametrize over an empty list passes by skipping, so the number of cases is pinned here and in the app.
    assert len(CASES) == 61


@pytest.mark.parametrize("case", [c for c in CASES if "shown" in c], ids=lambda c: c["name"])
def test_the_code_title_a_card_shows(case):
    assert code_title.shown_code_title(case["card"]) == case["shown"]


def _words(text):
    text = unicodedata.normalize("NFC", text.replace("\ufeff", ""))
    return "".join(ch if unicodedata.category(ch)[0] in "LMN" else " " for ch in text).upper().split()


@pytest.mark.parametrize("case", [c for c in CASES if c["title"]], ids=lambda c: c["name"])
def test_a_code_title_never_invents_a_word(case):
    """Each word of the title is a whole word of the label, half of a long word the label writes twice, or one of the
    fixed words; a post or an explanation can change a word's casing, never its letters or where it splits."""
    label = _words(case["card"]["title"])
    halves = {w[:len(w) // 2] for w in label if len(w) >= 10 and len(w) % 2 == 0 and w[:len(w) // 2] == w[len(w) // 2:]}
    got = code_title.code_title(case["card"])
    assert isinstance(got, str) and got.startswith("Posts about ")
    fixed = {"POSTS", "ABOUT", "AND"} | ({"THIS", "TOPIC"} if got == "Posts about this topic" else set())
    assert all(w in fixed or w in label or w in halves for w in _words(got)), _words(got)


def test_a_code_title_names_no_cause():
    for case in CASES:
        got = code_title.code_title(case["card"])
        if case["title"]:
            assert got and not re.search(r"\b(because|after|due|why|caused|reacting|driven)\b", got, re.I)


def test_a_written_title_card_gets_no_code_title_count_line():
    card = {"kind": "topic", "title": "kaizer, chiefs", "explained": True, "title_written": "The Soweto derby",
            "count_line": "4 creators, 3 days (kaizer, chiefs only)"}
    assert code_title.code_count_line(card, card["count_line"]) == card["count_line"]
    assert code_title.shown_title(card) == "The Soweto derby"


def test_shown_title_is_written_then_code_then_label():
    assert code_title.shown_title({"kind": "topic", "title": "kaizer, chiefs", "explained": True,
                                   "title_written": "  The   Soweto derby "}) == "The Soweto derby"
    # A written title on a card that is not explained is not shown (core/brief/payload.py), so the code title is.
    assert code_title.shown_title({"kind": "topic", "title": "kaizer, chiefs", "explained": False,
                                   "title_written": "The Soweto derby"}) == "Posts about Kaizer and Chiefs"
    # A written title that only repeats the label is not shown either, as in the app.
    assert code_title.shown_title({"kind": "hashtag", "title": "#Shaya_Step", "explained": True,
                                   "title_written": "shaya step"}) == "Posts about #Shaya_Step"
    assert code_title.shown_title({"kind": "creator", "title": "Thabo Dance", "title_written": None}) == "Thabo Dance"


def test_a_card_from_a_brief_that_writes_no_titles_keeps_its_label():
    """Briefs before 6 October 2026 carry no title_written and f42-api adds none (contract.md section 4), and a
    Discover row with no brief card has none: no title check ran, so the card reads as before."""
    card = {"kind": "topic", "title": "kaizer, chiefs", "explained": True, "count_line": "4 creators (kaizer, chiefs only)"}
    assert code_title.shown_code_title(card) is None
    assert code_title.shown_title(card) == "kaizer, chiefs"
    assert code_title.code_count_line(card, card["count_line"]) == card["count_line"]
    assert code_title.shown_code_title(dict(card, title_written=None)) == "Posts about Kaizer and Chiefs"


def test_casing_comes_only_from_posts_the_projection_leaves():
    payload = {"cards": [{"item_id": "i1", "kind": "topic", "title": "mofokeng", "explained": False,
                          "evidence": [{"id": "p1", "platform": "tiktok", "handle": "@hidden_one", "text": "MOFOKENG"},
                                       {"id": "p2", "platform": "tiktok", "handle": "@hidden_one", "text": "MOFOKENG"},
                                       {"id": "p3", "platform": "tiktok", "handle": "@other", "text": "mofokeng"}]}]}
    shown = today.without_hidden(payload, (frozenset({"tiktok:hidden_one"}), frozenset(), ()))
    assert code_title.code_title(shown["cards"][0]) == "Posts about Mofokeng"
    assert code_title.code_title(payload["cards"][0]) == "Posts about MOFOKENG"


ZA_03 = {"item_id": "za03", "kind": "topic", "title": "bafanabafana, mofokeng, relebohile", "explained": True,
         "title_written": None, "state_word": "Rising", "market": "ZA", "date": "2026-10-12",
         "count_line": "5 creators and 11 posts in 3 days (bafanabafana, mofokeng, relebohile only)", "evidence": []}


def test_the_channel_post_names_the_card_by_its_code_title():
    words = channel._card_words(dict(ZA_03), "ZA", "2026-10-12")
    assert words.startswith("Posts about Bafana Bafana, Mofokeng and Relebohile, Rising, 5 creators")
    assert "bafanabafana" not in words


def test_the_digest_names_the_card_by_its_code_title():
    resp = {"date": "2026-10-12", "alerts": [{"watch_id": "w1", "label": "Posts about Bafana Bafana, Mofokeng and Relebohile",
                                              "item_id": "za03", "market": "ZA", "fired_because": "Entered Rising",
                                              "card": dict(ZA_03)}], "waiting": []}
    html = digest.render_html(resp, "https://example.test")
    text = digest.render_text(resp, "https://example.test")
    for body in (html, text):
        assert "bafanabafana" not in body
        assert "Posts about Bafana Bafana, Mofokeng and Relebohile" in body
        assert "(Bafana Bafana, Mofokeng and Relebohile only)" in body


def test_an_alert_is_labelled_by_the_code_title(monkeypatch):
    from core.api import discover

    card = dict(ZA_03)
    monkeypatch.setattr(discover, "_run", lambda store, until=None: {"run_date": "2026-10-12", "run_id": "r1"})
    monkeypatch.setattr(discover, "_cards", lambda store, run, market, hidden=None: ([({"item_id": "za03", "market": "ZA"}, card, None)], None))
    monkeypatch.setattr(discover.alerts, "evaluate", lambda *a, **k: [
        {"watch_id": "w1", "label": "bafanabafana, mofokeng, relebohile", "item_id": "za03", "market": "ZA",
         "fired_because": "Entered Rising", "since": "2026-10-12"}])

    class Store:
        def latest_detect_run(self, until=None):
            return None

        def watch_matches(self, run):
            return {}

    out = discover.build_alerts(Store(), [{"watch_id": "w1", "label": None}])
    assert out["alerts"][0]["label"] == "Posts about Bafana Bafana, Mofokeng and Relebohile"
    # A watch made from the card before code titles carries the raw label, which is the card's, not the user's words.
    out = discover.build_alerts(Store(), [{"watch_id": "w1", "label": "bafanabafana, mofokeng, relebohile"}])
    assert out["alerts"][0]["label"] == "Posts about Bafana Bafana, Mofokeng and Relebohile"


def test_a_watch_the_user_named_keeps_its_name(monkeypatch):
    """A label the user typed is theirs; only a written title replaces it (test_discover.py pins that)."""
    from core.api import discover

    monkeypatch.setattr(discover, "_run", lambda store, until=None: {"run_date": "2026-10-12", "run_id": "r1"})
    monkeypatch.setattr(discover, "_cards", lambda store, run, market, hidden=None: ([({"item_id": "za03", "market": "ZA"}, dict(ZA_03), None)], None))
    monkeypatch.setattr(discover.alerts, "evaluate", lambda *a, **k: [
        {"watch_id": "w1", "label": "Bafana watch", "item_id": "za03", "market": "ZA",
         "fired_because": "Entered Rising", "since": "2026-10-12"}])

    class Store:
        def latest_detect_run(self, until=None):
            return None

        def watch_matches(self, run):
            return {}

    out = discover.build_alerts(Store(), [{"watch_id": "w1", "label": "Bafana watch"}])
    assert out["alerts"][0]["label"] == "Bafana watch"


def test_the_digest_card_line_keeps_the_label_under_a_written_title():
    card = dict(ZA_03, title_written="MTN8 final", count_line="5 creators")
    resp = {"date": "2026-10-12", "waiting": [], "alerts": [
        {"watch_id": "w1", "label": "MTN8 final", "item_id": "za03", "market": "ZA", "fired_because": "Entered Rising",
         "card": card}]}
    text = digest.render_text(resp, "https://example.test")
    assert "MTN8 final, South Africa\n" in text
    assert "bafanabafana, mofokeng, relebohile: Rising, 5 creators" in text
    assert "Posts about" not in text


def test_the_digest_says_a_code_title_once_per_alert():
    """When the alert is labelled by the card's own code title the market line does not say it again."""
    title = "Posts about Bafana Bafana, Mofokeng and Relebohile"
    resp = {"date": "2026-10-12", "waiting": [], "alerts": [
        {"watch_id": "w1", "label": title, "item_id": "za03", "market": "ZA", "fired_because": "Entered Rising",
         "card": dict(ZA_03)}]}
    text = digest.render_text(resp, "https://example.test")
    html = digest.render_html(resp, "https://example.test")
    assert text.count(title) == 1 and html.count(title) == 1
    assert "\nSouth Africa\n" in text
    named = dict(resp, alerts=[dict(resp["alerts"][0], label="Bafana watch")])
    assert "Bafana watch, South Africa\n" in digest.render_text(named, "https://example.test")


def test_the_digest_head_keeps_the_label_when_a_written_title_leads_the_card():
    """The market line drops the label only when the label is the card's own code title; under a written title the
    card line says the raw label, so the head keeps the label."""
    card = dict(ZA_03, title_written="MTN8 final", count_line="5 creators")
    resp = {"date": "2026-10-12", "waiting": [], "alerts": [
        {"watch_id": "w1", "label": card["title"], "item_id": "za03", "market": "ZA",
         "fired_because": "Entered Rising", "card": card}]}
    lines = digest.render_text(resp, "https://example.test").splitlines()
    assert "bafanabafana, mofokeng, relebohile, South Africa" in lines
