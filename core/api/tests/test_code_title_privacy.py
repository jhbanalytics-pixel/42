"""No code title, digest, channel post or label names a hidden person (Albert's ruling of 12 Oct 2026, "no names";
ALBERT-FIXES-TRACKER item 28).

Every post here is shaped as the stored briefs write evidence: id, platform, handle, text, url and posted_at, and no
author name field (0 of the 442 records in the 12 Oct payloads carry one). A hidden person's display name therefore
comes from the hidden list, which reads it from their creators rows (today.hidden_names), never from the value the
projection is proving something about."""
import hashlib
import inspect
import json
import re
from pathlib import Path

import pytest

from core.api import channel, code_title, digest, discover, privacy, today
from core.api.store import FixtureStore

KEY = "tiktok:thabo_dances"
NAME = "Thabo Mokoena"
HIDDEN = (frozenset({KEY}), frozenset({"c_thabo"}), frozenset({NAME}))
DAY = "2026-10-12"


def post(n, handle, text):
    return {"id": f"p{n}", "platform": "tiktok", "handle": handle, "text": text,
            "url": f"https://www.tiktok.com/@{handle.lstrip('@')}/video/{n}", "posted_at": "2026-10-11T10:00:00+02:00"}


def card(title, *texts, kind="topic", item_id="i1", hidden_post=True):
    """A card as the brief stores it, with a post by the hidden person (when asked) and posts by others."""
    evidence = ([post(1, "thabo_dances", "Mzansi dance moves")] if hidden_post else []) + [
        post(n + 2, "other_creator", t) for n, t in enumerate(texts)]
    return {"item_id": item_id, "kind": kind, "title": title, "explained": False, "title_written": None,
            "state_word": "Rising", "market": "ZA", "date": DAY, "count_line": f"4 creators ({title} only)",
            "ask": f"What is behind {title} in South Africa this week?", "evidence": evidence}


def shown(value, hidden=HIDDEN):
    return today.without_hidden(value, hidden)


def leaks(*bodies):
    return [b for b in bodies if re.search(r"thabo|mokoena", json.dumps(b, ensure_ascii=False), re.I)]


# The hidden list knows the display name from the creators table.

class Creators(FixtureStore):
    """The fixtures with one hidden TikTok creator whose creators row carries a display name."""

    def __init__(self, display_name=NAME, by_id=True):
        super().__init__()
        self.row = {"creator_id": "c_thabo", "platform": "tiktok", "handle": "thabo_dances", "followers": 900,
                    "tier": "nano", "home_market": "ZA", "display_name": display_name}
        self.by_id = by_id

    def suppressed_creators(self):
        return {"c_thabo"} if self.by_id else set()

    def suppressions(self):
        held = [] if self.by_id else [{"suppression_id": "s1", "status": "active", "creator_id": None,
                                       "platform": "tiktok", "handle": "thabo_dances"}]
        return held

    def _optional(self, name):
        rows = super()._optional(name)
        return (list(rows or []) + [self.row]) if name == "people_creators" else rows


def test_the_hidden_list_names_a_hidden_creator_by_their_display_name():
    keys, ids, names = today.hidden_people(Creators())
    assert KEY in keys and NAME in names
    keys, ids, names = privacy.read_hidden(Creators())
    assert KEY in keys and NAME in names


def test_a_creator_hidden_by_handle_alone_is_named_too():
    keys, ids, names = privacy.read_hidden(Creators(by_id=False))
    assert KEY in keys and NAME in names


def test_a_failed_name_read_reads_as_an_unreadable_list():
    class Broken(Creators):
        def hidden_display_names(self, ids, keys):
            raise RuntimeError("Access Denied: creators")

    assert today.hidden_people(Broken()) is None
    assert privacy.read_hidden(Broken()) is None


def test_todays_reader_masks_the_name_learned_from_the_creators_row():
    class Briefs(Creators):
        def briefs(self, date):
            return [{"market": "ZA", "brief_date": date, "payload": {"cards": [card("thabo mokoena, amapiano",
                                                                                  "Amapiano is life", hidden_post=False)]}}]

    got = today._WithoutHidden(Briefs()).briefs(DAY)[0]["payload"]["cards"][0]
    assert got["title"] == "***, amapiano"
    assert code_title.shown_title(got) == "Posts about Amapiano"
    assert leaks(got["title"], got["ask"], got["count_line"], code_title.shown_title(got),
                 channel._card_words(got, "ZA", DAY)) == []


# The terms that name the hidden person.

@pytest.mark.parametrize("label, masked", [
    ("thabo mokoena, amapiano", "***, amapiano"),
    ("thabomokoena", "***"),
    ("fans of thabo mokoena, amapiano", "***, amapiano"),
    ("mokoena thabo, amapiano", "***, amapiano"),
    ("thabo, mokoena, amapiano", "***, ***, amapiano"),
    ("mokoena, amapiano, thabo", "***, amapiano, ***"),
    ("thabo mokoena \u2013 amapiano", "***, amapiano"),
    ("thabo\u0085mokoena, amapiano", "***, amapiano"),
    ("thabo dances, amapiano", "***, amapiano"),
    ("#thabodances", "***"),
])
def test_a_label_term_that_names_the_hidden_person_is_masked(label, masked):
    got = shown({"cards": [card(label, "Amapiano is life")]})["cards"][0]
    assert got["title"] == masked
    assert leaks(code_title.shown_title(got)) == []


def test_za_card_03s_label_masks_a_hidden_footballer_whose_names_are_two_terms():
    hidden = (frozenset({"tiktok:rele_10"}), frozenset(), frozenset({"Relebohile Mofokeng"}))
    got = shown({"cards": [card("bafanabafana, mofokeng, relebohile", "Bafana")]}, hidden)["cards"][0]
    assert got["title"] == "bafanabafana, ***, ***"
    assert code_title.shown_title(got) == "Posts about Bafana Bafana"


def test_one_word_of_a_two_word_name_is_not_the_name():
    got = shown({"cards": [card("thabo, amapiano", "Amapiano")]})["cards"][0]
    assert got["title"] == "thabo, amapiano"


def test_accents_do_not_hide_a_name():
    hidden = (frozenset(), frozenset({"c_zoe"}), frozenset({"Zo\u00eb Mokoena"}))
    for label in ("zoe mokoena, amapiano", "zo\u00eb mokoena, amapiano", "zoe\u0308 mokoena, amapiano"):
        assert shown({"cards": [card(label, hidden_post=False)]}, hidden)["cards"][0]["title"] == "***, amapiano"


def test_a_label_with_nothing_to_mask_is_left_exactly_as_it_was():
    label = "amapiano \u2013 live,  kaizer chiefs"
    assert shown({"cards": [card(label, "Amapiano")]})["cards"][0]["title"] == label


def test_a_one_word_name_masks_only_an_equal_term():
    hidden = (frozenset(), frozenset({"UC" + "a" * 22}), frozenset({"Music"}))
    out = shown({"cards": [card("music, amapiano", hidden_post=False),
                           card("Music festival buzz", kind="hashtag", item_id="i2", hidden_post=False)]}, hidden)
    assert [c["title"] for c in out["cards"]] == ["***, amapiano", "Music festival buzz"]


def test_the_title_label_aliases_ask_and_count_line_are_all_masked():
    value = {"card": dict(card("thabo mokoena, amapiano", "Amapiano"), label="thabo mokoena, amapiano"),
             "aliases": ["thabo mokoena", "thabo mokoena amapiano", "thabomokoena live", "amapiano"]}
    got = shown(value)
    c = got["card"]
    assert (c["title"], c["label"]) == ("***, amapiano", "***, amapiano")
    assert c["ask"] == "What is behind ***, amapiano in South Africa this week?"
    assert c["count_line"] == "4 creators (***, amapiano only)"
    assert got["aliases"] == ["***", "***", "***", "amapiano"]


def test_another_creators_own_item_is_not_term_masked():
    """A creator item is an account name: only the hidden person's own item is masked, by the projection's own
    rule, so a different account that shares words keeps its name."""
    other = dict(card("Thabo Mokoena Fan Page", kind="creator", item_id="someone_else", hidden_post=False))
    assert shown({"cards": [other]})["cards"][0]["title"] == "Thabo Mokoena Fan Page"


def test_post_text_is_left_alone():
    value = {"cards": [card("amapiano", "Big up thabo mokoena", hidden_post=False)]}
    value["cards"][0]["evidence"][0]["title"] = "thabo mokoena dance tutorial"
    got = shown(value)["cards"][0]
    assert got["evidence"][0]["text"] == "Big up thabo mokoena"
    assert got["evidence"][0]["title"] == "thabo mokoena dance tutorial"


# Every reader masks the same way; only the brief job's write path opts out.

def test_the_safe_value_is_the_default():
    assert inspect.signature(today.without_hidden).parameters["label_terms"].default is True


def test_only_the_brief_jobs_calls_opt_out_and_every_one_of_them_does():
    root = Path(today.__file__).resolve().parents[2]
    calls = {}
    for path in sorted((root / "core").rglob("*.py")):
        if "tests" in path.parts:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bwithout_hidden\(", line) and not line.lstrip().startswith("def "):
                calls[f"{path.relative_to(root).as_posix()}:{n}"] = "label_terms=False" in line
    brief = {k: v for k, v in calls.items() if k.startswith("core/brief/")}
    assert len(brief) == 7 and all(brief.values()), brief
    assert not any(v for k, v in calls.items() if not k.startswith("core/brief/")), calls


def test_the_brief_jobs_projection_leaves_label_terms_alone():
    value = {"cards": [card("thabo mokoena, amapiano", "Amapiano")]}
    assert today.without_hidden(value, HIDDEN, label_terms=False)["cards"][0]["title"] == "thabo mokoena, amapiano"


@pytest.mark.parametrize("value, path", [
    ({"breaking": [{"item_id": "b1", "market": "ZA", "kind": "hashtag", "title": "#thabomokoena"}]},
     ("breaking", 0, "title")),
    ({"not_assessed": {"items": [{"item_id": "n1", "title": "thabo mokoena, amapiano", "reason_text": "x"}]}},
     ("not_assessed", "items", 0, "title")),
    ({"news": [{"seed_date": DAY, "title": "thabo mokoena, amapiano", "matched_by": "seed"}]}, ("news", 0, "title")),
    ({"subjects": [{"item_id": "s1", "label": "thabo mokoena, amapiano"}]}, ("subjects", 0, "label")),
    ({"items": [{"item_id": "h1", "kind": "topic", "label": "thabo mokoena, amapiano"}]}, ("items", 0, "label")),
    ([{"item_id": "m1", "kind": "topic", "label": "thabo mokoena, amapiano"}], (0, "label")),
])
def test_every_label_surface_is_masked(value, path):
    got = shown(value)
    for step in path:
        got = got[step]
    assert leaks(got) == [], got


def test_discover_projects_the_run_at_once_as_today_does():
    """A name the projection learns from one item (the hidden person's own creator item, titled by its map label)
    masks every other item of the request, on Discover as on Today."""
    own = hashlib.sha256(f"creator|{KEY}".encode("utf-8")).hexdigest()

    def states(run, market):
        base = FixtureStore().item_states(run, market)[0]
        return [dict(base, item_id=own, kind="creator", label=NAME, canonical_key=KEY, creator_name=None),
                dict(base, item_id="i_topic", kind="topic", label="thabo mokoena, amapiano",
                     canonical_key="topic:x")] if market in ("ZA", "all") else []

    class Store(FixtureStore):
        def item_states(self, run, market):
            return states(run, market)

    run = discover._run(Store())
    cards, _ = discover._cards(Store(), run, "ZA", hidden=(frozenset({KEY}), frozenset(), frozenset()))
    titles = {c["item_id"]: c["title"] for _, c, _ in cards}
    assert titles["i_topic"] == "***, amapiano"
    assert NAME not in json.dumps([c for _, c, _ in cards])


# A watch label saved before the person was hidden.

def _alerts(monkeypatch, label, hidden=HIDDEN, waiting=False):
    masked = shown({"cards": [card("thabo mokoena, amapiano", "Amapiano")]}, hidden)["cards"][0]
    monkeypatch.setattr(discover, "_run", lambda store, until=None: {"run_date": DAY, "run_id": "r1"})
    monkeypatch.setattr(discover, "_cards", lambda store, run, market, hidden=None: (
        [({"item_id": "i1", "market": "ZA"}, dict(masked), None)], None))
    monkeypatch.setattr(discover, "hidden_people", lambda store: hidden)
    fired = [{"watch_id": "w1", "waiting": "Waiting for a second detect run"}] if waiting else [
        {"watch_id": "w1", "label": label, "item_id": "i1", "market": "ZA", "fired_because": "Entered Rising",
         "since": DAY}]
    monkeypatch.setattr(discover.alerts, "evaluate", lambda *a, **k: fired)

    class Store:
        def latest_detect_run(self, until=None):
            return None

        def watch_matches(self, run):
            return {}

    return discover.build_alerts(Store(), [{"watch_id": "w1", "label": label}])


@pytest.mark.parametrize("label", ["Posts about Thabo Mokoena and Amapiano", "thabo mokoena, amapiano",
                                   "Thabo Mokoena watch"])
def test_a_watch_label_saved_before_the_person_was_hidden_is_masked(monkeypatch, label):
    out = _alerts(monkeypatch, label)
    assert out["alerts"][0]["label"] == "Posts about Amapiano"
    text = digest.render_text(out, "https://example.test")
    html = digest.render_html(out, "https://example.test")
    assert leaks(out["alerts"][0]["label"], text, html) == []
    waiting = _alerts(monkeypatch, label, waiting=True)
    assert leaks(waiting["waiting"]) == []


def test_a_watch_label_on_a_card_with_no_code_title_is_masked(monkeypatch):
    """A card from a brief written before title_written has no code title to fall back on; the label is masked."""
    masked = shown({"cards": [card("thabo mokoena, amapiano", "Amapiano")]})["cards"][0]
    masked.pop("title_written")
    monkeypatch.setattr(discover, "_run", lambda store, until=None: {"run_date": DAY, "run_id": "r1"})
    monkeypatch.setattr(discover, "_cards", lambda store, run, market, hidden=None: (
        [({"item_id": "i1", "market": "ZA"}, dict(masked), None)], None))
    monkeypatch.setattr(discover, "hidden_people", lambda store: HIDDEN)
    monkeypatch.setattr(discover.alerts, "evaluate", lambda *a, **k: [
        {"watch_id": "w1", "label": "Thabo Mokoena watch", "item_id": "i1", "market": "ZA",
         "fired_because": "Entered Rising", "since": DAY}])

    class Store:
        def latest_detect_run(self, until=None):
            return None

        def watch_matches(self, run):
            return {}

    out = discover.build_alerts(Store(), [{"watch_id": "w1", "label": "Thabo Mokoena watch"}])
    assert out["alerts"][0]["label"] == "***"


def test_a_watch_label_naming_no_one_is_kept(monkeypatch):
    assert _alerts(monkeypatch, "Amapiano watch")["alerts"][0]["label"] == "Amapiano watch"


def test_the_watch_list_body_masks_labels():
    body = {"watches": [{"watch_id": "w1", "label": "Posts about Thabo Mokoena and Amapiano", "status": "active"},
                        {"watch_id": "w2", "label": "Amapiano watch", "status": "active"}]}
    got = privacy.mask(body, HIDDEN)
    assert [w["label"] for w in got["watches"]] == ["***", "Amapiano watch"]


# With the list unreadable only the value's own authors' handles are known (the display name is Albert's call, N7).

def test_with_the_list_unreadable_a_handle_term_of_an_author_is_masked():
    for label in ("thabo dances, amapiano", "thabodances, amapiano"):
        got = today.without_hidden({"cards": [card(label, "Amapiano")]}, None)["cards"][0]
        assert got["title"] == "***, amapiano"


def test_bigquery_reads_the_display_names_in_the_hidden_lists_one_job(monkeypatch):
    """The display names come back from the job that reads who is hidden (store.hidden_people_rows), so learning
    them costs no extra job (test_privacy_reads.py pins one job)."""
    from core.api import store as store_mod
    from core.api.tests.test_privacy_reads import bigquery_store

    monkeypatch.setattr(store_mod, "_CATALOG", {})
    rows = [{"creator_id": "c_thabo", "platform": "tiktok", "handle": "thabo_dances", "display_name": NAME},
            {"creator_id": None, "platform": "tiktok", "handle": "other_hidden", "display_name": " Zinhle Dube "}]
    store, client = bigquery_store(hidden_rows=rows, post_rows=[])
    keys, ids, names = privacy.read_hidden(store)
    assert {NAME, "Zinhle Dube"} <= names and len(client.sql) == 1
    assert "display_name" in client.sql[0]
