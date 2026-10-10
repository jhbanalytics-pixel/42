"""Two published cards of one market collapse into one only when they share evidence AND name the same event, and
never when that would take a card away: the higher ranked card must itself be published.

Rebuilt from the 10 October 2026 ZA board (card 01 the topic "bafana, pitso, egypt", card 02 "South Africa vs Egypt")
and from the review probes that a team name test alone merged wrongly: rugby and cricket, mens and womens sides,
and the Swahili word "mali"."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.brief import job, same_event
from core.brief.tests.test_brief_job import FakeConfirm, FakeModel, add_item, all_cards, brief, payload, world
from core.brief.tests.test_brief_job import busy_waits, market_scope_is_valid_by_default  # noqa: F401  (autouse)
from core.brief.tests.test_brief_merge import link

BAFANA = "bafana, pitso, egypt"
SAFE = "South Africa vs Egypt"


# What counts as naming one event


@pytest.mark.parametrize("a,b", [
    (BAFANA, SAFE),
    (SAFE, BAFANA),
    ("Bafana Bafana vs Egypt", SAFE),
    ("South Africa vs Egypt", "south africa v egypt"),
    ("South Africa vs Egypt", "South Africa vs Egypt friendly tonight"),
])
def test_titles_that_say_the_same_fixture_name_one_event(a, b):
    assert same_event.same_event(a, b)


@pytest.mark.parametrize("a,b", [
    ("springboks, south africa, england", "proteas, south africa, england"),
    ("banyana, south africa, egypt", BAFANA),
    ("Banyana Banyana vs Egypt", BAFANA),
    ("mali ya umma, kenya", "kenya, wizi wa mali"),
    ("Nigeria vs Ghana jollof war", "Super Eagles vs Black Stars"),
    ("mali", "mali ya umma kenya"),
    ("South Africa vs Nigeria", SAFE),
    ("Egypt", SAFE),
    ("Egypt fans in Johannesburg", "Egypt visa rules"),
    ("Amapiano night", "Gqom night"),
    ("", SAFE),
    (None, None),
])
def test_different_sports_sides_stories_or_a_shared_word_are_not_one_event(a, b):
    assert not same_event.same_event(a, b) and not same_event.same_event(b, a)


def test_an_alias_is_read_as_a_whole_word():
    assert not same_event.same_event("abafana vs Egypt", SAFE)
    assert not same_event.same_event("bafanas vs Egypt", SAFE)


def test_accents_and_case_do_not_change_what_a_title_says():
    assert same_event.same_event("BAFANÀ BAFANÀ vs Égypt", SAFE)


def test_the_nickname_table_is_only_what_albert_reported_for_10_october_and_asks_for_review():
    text = Path(same_event.ALIASES).read_text(encoding="utf-8")
    assert "Albert" in text and "Thapelo" in text
    assert same_event.aliases() == {"bafana bafana": "south africa", "bafana": "south africa"}


def test_a_table_given_by_path_is_the_one_read(tmp_path):
    other = tmp_path / "aliases.yaml"
    other.write_text("aliases:\n  super eagles: nigeria\n", encoding="utf-8")
    assert same_event.same_event("Super Eagles vs Ghana", "Nigeria vs Ghana", path=other)
    assert not same_event.same_event(BAFANA, SAFE, path=other)


# What counts as sharing evidence


def candidate(item_id, title, worth, *, posts=(), handles=(), where="today", publish=True):
    evidence = [{"id": f"{item_id}_{h}", "platform": "tiktok", "handle": h} for h in handles]
    return {"row": {"item_id": item_id, "title": title, "worth_raw": worth, "market_scope": "market"},
            "decision": SimpleNamespace(publish=publish, where=where), "posts": set(posts),
            "pack": {"evidence": evidence, "numbers": [], "facts": []}}


def test_one_shared_post_is_shared_evidence():
    assert same_event.shares_evidence(candidate("a", BAFANA, 1, posts={"p1", "p2"}),
                                      candidate("b", SAFE, 1, posts={"p2", "p3"}))


def test_no_shared_post_or_creator_is_no_shared_evidence():
    assert not same_event.shares_evidence(candidate("a", BAFANA, 1, posts={"p1"}, handles=["@x", "@y"]),
                                          candidate("b", SAFE, 1, posts={"p2"}, handles=["@z", "@w"]))


def test_one_shared_creator_is_not_enough_because_one_creator_posts_about_everything():
    assert not same_event.shares_evidence(candidate("a", BAFANA, 1, handles=["@x", "@y"]),
                                          candidate("b", SAFE, 1, handles=["@x", "@z"]))


def test_two_shared_creators_are_shared_evidence_and_case_does_not_split_a_creator():
    assert same_event.shares_evidence(candidate("a", BAFANA, 1, handles=["@X", "@y", "@q"]),
                                      candidate("b", SAFE, 1, handles=["@x", "@Y"]))


def test_a_missing_handle_is_not_a_shared_creator():
    a = candidate("a", BAFANA, 1, handles=["@x"])
    b = candidate("b", SAFE, 1, handles=["@y"])
    for c in (a, b):
        c["pack"]["evidence"] += [{"id": f"{c['row']['item_id']}_n1", "platform": "tiktok", "handle": None},
                                  {"id": f"{c['row']['item_id']}_n2", "platform": "tiktok"}]
    assert not same_event.shares_evidence(a, b)


def test_the_same_handle_on_two_platforms_is_two_creators():
    a = candidate("a", BAFANA, 1, handles=["@x", "@y"])
    b = candidate("b", SAFE, 1, handles=["@x", "@y"])
    for e in b["pack"]["evidence"]:
        e["platform"] = "instagram"
    assert not same_event.shares_evidence(a, b)


# The collapse: only when both cards are published, and the higher ranked one keeps the card


def collapse(*cands):
    by_market = {"ZA": list(cands), "NG": [], "KE": []}
    return job._collapse_events(by_market, "ZA"), by_market["ZA"]


def test_two_published_cards_that_share_a_post_and_name_one_event_become_one():
    bafana = candidate("bafana", BAFANA, 0.97, posts={"p1", "p2", "p3"})
    safe = candidate("safe", SAFE, 0.96, posts={"p3", "p4", "p5"})
    merged, left = collapse(bafana, safe)
    assert merged == [{"market": "ZA", "into": "bafana", "item_id": "safe", "by": "same_event"}]
    assert left == [bafana]
    assert bafana["also"] == [{"item_id": "safe", "title": SAFE}]


def test_the_higher_ranked_card_keeps_the_card_whichever_way_round_they_are_listed():
    bafana = candidate("bafana", "Bafana Bafana vs Egypt", 0.97, posts={"p1"})
    safe = candidate("safe", SAFE, 0.96, posts={"p1"})
    merged, left = collapse(safe, bafana)
    assert [m["into"] for m in merged] == ["bafana"] and left == [bafana]


def test_the_same_event_without_shared_evidence_stays_two_cards():
    bafana = candidate("bafana", BAFANA, 0.97, posts={"p1"}, handles=["@a", "@b"])
    safe = candidate("safe", SAFE, 0.96, posts={"p2"}, handles=["@c", "@d"])
    merged, left = collapse(bafana, safe)
    assert merged == [] and left == [bafana, safe]


def test_shared_evidence_without_the_same_event_stays_two_cards():
    rugby = candidate("rugby", "springboks, south africa, england", 0.97, posts={"p1"})
    cricket = candidate("cricket", "proteas, south africa, england", 0.96, posts={"p1"})
    merged, left = collapse(rugby, cricket)
    assert merged == [] and left == [rugby, cricket]


def test_a_held_higher_card_never_takes_the_lower_published_card_with_it():
    held = candidate("bafana", BAFANA, 0.97, posts={"p1"}, where="held_back", publish=False)
    safe = candidate("safe", SAFE, 0.96, posts={"p1"})
    merged, left = collapse(held, safe)
    assert merged == [] and left == [held, safe] and "also" not in held and "also" not in safe


def test_a_held_lower_card_stays_its_own_held_item():
    bafana = candidate("bafana", BAFANA, 0.97, posts={"p1"})
    held = candidate("safe", SAFE, 0.96, posts={"p1"}, where="held_back", publish=False)
    merged, left = collapse(bafana, held)
    assert merged == [] and left == [bafana, held]


def test_a_card_that_is_published_but_not_for_today_is_not_collapsed():
    bafana = candidate("bafana", BAFANA, 0.97, posts={"p1"})
    other = candidate("safe", SAFE, 0.96, posts={"p1"}, where="more")
    merged, left = collapse(bafana, other)
    assert merged == [] and left == [bafana, other]


def test_three_items_for_one_event_all_collapse_into_the_highest():
    a = candidate("a", BAFANA, 0.97, posts={"p1"})
    b = candidate("b", SAFE, 0.96, posts={"p1"})
    c = candidate("c", "South Africa v Egypt", 0.95, posts={"p1"})
    merged, left = collapse(c, a, b)
    assert [m["item_id"] for m in merged] == ["b", "c"] and left == [a]
    assert [x["item_id"] for x in a["also"]] == ["b", "c"]


def test_a_market_without_candidates_collapses_to_nothing():
    assert collapse() == ([], [])


# End to end on the fixture world


def cards(r):
    return {c["item_id"]: c for c in all_cards(payload(r, "ZA"))}


def test_end_to_end_two_cards_for_one_event_that_share_a_post_show_as_one():
    con = world(n=1)
    add_item(con, "ZA", "za_bafana", 0.97, label=BAFANA, creators3=3, posts3=13)
    add_item(con, "ZA", "za_safe", 0.96, label=SAFE, creators3=2, posts3=5)
    link(con, "za_safe", ["za_bafana_p1"])
    r = brief(con, model=FakeModel(), confirm=FakeConfirm())
    shown = cards(r)
    assert "za_bafana" in shown and "za_safe" not in shown
    assert shown["za_bafana"]["also"] == [{"item_id": "za_safe", "title": SAFE}]
    assert r.counts["merged"] == [{"market": "ZA", "into": "za_bafana", "item_id": "za_safe", "by": "same_event"}]


def test_end_to_end_the_same_event_with_nothing_shared_stays_two_cards():
    con = world(n=1)
    add_item(con, "ZA", "za_bafana", 0.97, label=BAFANA, creators3=3, posts3=13)
    add_item(con, "ZA", "za_safe", 0.96, label=SAFE, creators3=2, posts3=5)
    r = brief(con, model=FakeModel(), confirm=FakeConfirm())
    assert {"za_bafana", "za_safe"} <= set(cards(r))
    assert r.counts["merged"] == []


def test_a_nickname_at_either_end_of_a_title_is_read():
    assert same_event.same_event("Bafana vs Egypt", SAFE) and same_event.same_event("Egypt vs Bafana", SAFE)


def test_a_nickname_inside_a_longer_word_is_not_replaced():
    assert not same_event.same_event("abafana vs Egypt", "asouth africa vs Egypt")
