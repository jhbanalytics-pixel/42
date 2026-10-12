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
    assert len(CASES) == 58


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


HIDDEN = (frozenset({"tiktok:thabo_dances"}), frozenset(), ())


def _hidden_card(title, *others, kind="topic"):
    """A card with one post by the hidden person (handle thabo_dances, display name Thabo Mokoena) and others'."""
    return {"cards": [{"item_id": "i1", "kind": kind, "title": title, "explained": False, "title_written": None,
                       "state_word": "Rising", "market": "ZA", "date": "2026-10-12",
                       "count_line": f"4 creators ({title} only)", "evidence": [
                           {"id": "p1", "platform": "tiktok", "handle": "@thabo_dances", "author_name": "Thabo Mokoena",
                            "text": "Thabo Mokoena dances"},
                           *({"id": f"p{i + 2}", "platform": "tiktok", "handle": "@other", "text": t}
                             for i, t in enumerate(others))]}]}


def _leaks(*bodies):
    return [b for b in bodies if "thabo" in json.dumps(b, ensure_ascii=False).lower()]


def test_a_hidden_persons_handle_term_is_left_out_of_the_code_title():
    """The projection masks a suppressed creator's handle in every title (today.without_hidden); the code title leaves
    the masked term out and names the rest."""
    card = today.without_hidden(_hidden_card("@thabo_dances, amapiano", "Amapiano is life"), HIDDEN, label_terms=True)["cards"][0]
    assert code_title.code_title(card) == "Posts about Amapiano"
    assert _leaks(card, code_title.shown_title(card), channel._card_words(card, "ZA", "2026-10-12")) == []


def test_a_hidden_persons_display_name_term_is_left_out_of_the_code_title():
    """Albert's lead ruling, 12 Oct 2026: a code title, and the digest and channel post it feeds, never holds the
    display name or handle of a hidden person. The projection masks a label term that is their name, so the code
    title drops it, and says this topic when nothing is left."""
    card = today.without_hidden(_hidden_card("thabo mokoena, amapiano", "Amapiano is life"), HIDDEN, label_terms=True)["cards"][0]
    assert card["title"] == "***, amapiano"
    assert code_title.code_title(card) == "Posts about Amapiano"
    words = channel._card_words(card, "ZA", "2026-10-12")
    assert words == "Posts about Amapiano, Rising, 4 creators (Amapiano only)"
    joined = today.without_hidden(_hidden_card("thabomokoena", "Big up Thabo Mokoena"), HIDDEN, label_terms=True)["cards"][0]
    assert joined["title"] == "***"
    assert code_title.code_title(joined) == "Posts about this topic"
    resp = {"date": "2026-10-12", "waiting": [], "alerts": [
        {"watch_id": "w1", "label": code_title.shown_title(c), "item_id": "i1", "market": "ZA",
         "fired_because": "Entered Rising", "card": c} for c in (card, joined)]}
    # Other people's posts may still say the name (the projection leaves free text alone); the titles never do.
    assert _leaks(card["title"], joined["title"], code_title.shown_title(card), code_title.shown_title(joined), words,
                  channel._card_words(joined, "ZA", "2026-10-12"), digest.render_text(resp, "https://example.test"),
                  digest.render_html(resp, "https://example.test")) == []


def test_a_name_on_the_hidden_list_is_masked_only_as_a_whole_term():
    """A name from the hidden list (a hidden YouTube channel's display name) masks a label term that is that name;
    a longer term that only holds a one word name stays, as a name such as Music is also a word."""
    out = today.without_hidden({"cards": [
        {"item_id": "a", "kind": "topic", "title": "music, amapiano", "title_written": None},
        {"item_id": "b", "kind": "hashtag", "title": "Music festival buzz", "title_written": None}]},
        (set(), {"UC" + "a" * 22}, {"Music"}), label_terms=True)
    assert [c["title"] for c in out["cards"]] == ["***, amapiano", "Music festival buzz"]
    assert code_title.code_title(out["cards"][0]) == "Posts about Amapiano"


def test_with_the_list_unreadable_no_evidence_authors_name_is_a_term():
    """While the hidden list cannot be read no evidence author may be named, so a label term that is one goes."""
    card = today.without_hidden(_hidden_card("thabo mokoena, amapiano", "Amapiano is life"), None, label_terms=True)["cards"][0]
    assert code_title.code_title(card) == "Posts about Amapiano"
    assert _leaks(card["title"], code_title.shown_title(card), channel._card_words(card, "ZA", "2026-10-12")) == []


def test_casing_comes_only_from_posts_the_projection_leaves():
    payload = {"cards": [{"item_id": "i1", "kind": "topic", "title": "mofokeng", "explained": False,
                          "evidence": [{"id": "p1", "platform": "tiktok", "handle": "@hidden_one", "text": "MOFOKENG"},
                                       {"id": "p2", "platform": "tiktok", "handle": "@hidden_one", "text": "MOFOKENG"},
                                       {"id": "p3", "platform": "tiktok", "handle": "@other", "text": "mofokeng"}]}]}
    shown = today.without_hidden(payload, (frozenset({"tiktok:hidden_one"}), frozenset(), ()), label_terms=True)
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
    monkeypatch.setattr(discover, "_cards", lambda store, run, market: ([({"item_id": "za03", "market": "ZA"}, card, None)], None))
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
    monkeypatch.setattr(discover, "_cards", lambda store, run, market: ([({"item_id": "za03", "market": "ZA"}, dict(ZA_03), None)], None))
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


def test_the_brief_jobs_projection_leaves_label_terms_alone():
    """The brief job writes its payloads through without_hidden (core/brief/job.py) and feeds the writer from it, so
    the label term masking is the readers' alone and the stored payload and the title gate's input are unchanged."""
    card = today.without_hidden(_hidden_card("thabo mokoena, amapiano", "Amapiano is life"), HIDDEN)["cards"][0]
    assert card["title"] == "thabo mokoena, amapiano"


def test_todays_reader_masks_a_hidden_persons_label_term():
    class Store:
        def suppressed_creators(self):
            return ["c1"]

        def creators_by_id(self, ids):
            return [{"creator_id": "c1", "platform": "tiktok", "handle": "thabo_dances"}]

        def map_items(self, ids):
            return []

        def briefs(self, date):
            return [{"market": "ZA", "brief_date": date, "payload": _hidden_card("thabo mokoena, amapiano", "Amapiano")}]

    row = today._WithoutHidden(Store()).briefs("2026-10-12")[0]
    card = row["payload"]["cards"][0]
    assert card["title"] == "***, amapiano"
    assert code_title.shown_title(card) == "Posts about Amapiano"


def test_a_hidden_persons_full_name_inside_a_longer_term_is_masked():
    card = today.without_hidden(_hidden_card("thabo mokoena fans, amapiano", "Amapiano is life"), HIDDEN,
                                label_terms=True)["cards"][0]
    assert card["title"] == "***, amapiano"
    assert code_title.code_title(card) == "Posts about Amapiano"


def test_with_the_list_unreadable_a_joined_author_name_is_masked_too():
    card = today.without_hidden(_hidden_card("thabomokoena, amapiano", "Amapiano is life"), None,
                                label_terms=True)["cards"][0]
    assert card["title"] == "***, amapiano"
    assert _leaks(card["title"], code_title.shown_title(card)) == []
