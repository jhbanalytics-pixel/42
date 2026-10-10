"""Two published cards of one market collapse into one only when they share evidence AND say the same event, judged on
the FINAL decision (after the G10 hold in _market_payload), so a card that ends up held never takes another card with it.

Rebuilt from the 10 October 2026 ZA board and from every pair the two Opus reviews found merging wrongly: rugby and
cricket, mens and womens sides, the Swahili word "mali", a nickname that is an ordinary word, a shorter title inside a
longer one, and the market's own name standing alone."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.brief import job, same_event
from core.brief.tests.test_brief_job import FakeConfirm, FakeModel, add_item, all_cards, brief, held_items, payload, world
from core.brief.tests.test_brief_job import busy_waits, market_scope_is_valid_by_default  # noqa: F401  (autouse)
from core.brief.tests.test_brief_merge import link

SAFE = "South Africa vs Egypt"
BAFANA = "Bafana Bafana vs Egypt"


def same(a, b, market="ZA", **kw):
    return same_event.same_event(a, b, market, **kw)


# What counts as one event: equal words once nicknames are replaced, saying more than the market's own name


@pytest.mark.parametrize("a,b", [
    (BAFANA, SAFE),
    (SAFE, BAFANA),
    ("South Africa vs Egypt", "south africa v egypt"),
    ("Egypt vs South Africa", SAFE),
    ("Bafana Bafana against Egypt", "The South Africa vs Egypt"),
])
def test_titles_with_equal_words_once_nicknames_are_replaced_are_one_event(a, b):
    assert same(a, b)


@pytest.mark.parametrize("a,b", [
    ("banyana, south africa, egypt", SAFE),
    ("banyana, south africa, egypt", "bafana, pitso, egypt"),
    ("Banyana Banyana vs Egypt", BAFANA),
    ("springboks, south africa, england", "south africa, england"),
    ("springboks, south africa, england", "proteas, south africa, england"),
    ("Bafana", "South Africa petrol price"),
    ("Sanibonani bafana", "South Africa"),
    ("Bafana vs Egypt", SAFE),
    ("South Africa women vs Egypt", SAFE),
    ("South Africa U20 vs Egypt", SAFE),
    ("bafana, pitso, egypt", SAFE),
    ("South Africa vs Nigeria", SAFE),
    ("Egypt", SAFE),
    ("Amapiano night", "Gqom night"),
    ("South Africa", "South Africa"),
    ("", SAFE),
    (None, None),
])
def test_a_longer_title_a_different_side_or_sport_or_a_bare_market_name_is_not_one_event(a, b):
    assert not same(a, b) and not same(b, a)


@pytest.mark.parametrize("a,b", [
    ("mali ya umma, kenya", "Kenya vs Mali"),
    ("mali ya umma, kenya", "kenya, wizi wa mali"),
    ("Kenya", "Kenya Finance Bill"),
    ("Kenya", "Kenya"),
    ("mali", "mali ya umma kenya"),
])
def test_in_kenya_a_swahili_word_or_the_market_name_alone_is_not_one_event(a, b):
    assert not same(a, b, "KE") and not same(b, a, "KE")


def test_the_markets_own_name_is_one_token_that_does_not_count_as_something_shared():
    assert same("Kenya Finance Bill", "Finance Bill, Kenya", "KE")
    assert not same("Kenya", "Kenya", "KE")
    assert not same("Nigeria", "Nigeria", "NG")
    assert not same("South Africa", "South Africa")


def test_an_alias_is_read_as_a_whole_word():
    assert not same("abafana bafana vs Egypt", SAFE)
    assert not same("abafana bafana vs Egypt", "asouth africa vs Egypt")


def test_a_nickname_at_either_end_of_a_title_is_read():
    assert same("Bafana Bafana vs Egypt", SAFE) and same("Egypt vs Bafana Bafana", SAFE)


def test_accents_and_case_do_not_change_what_a_title_says():
    assert same("BAFANÀ BAFANÀ vs Égypt", SAFE)


def test_the_nickname_table_is_only_bafana_bafana_and_says_albert_or_thapelo_review_every_entry():
    text = Path(same_event.ALIASES).read_text(encoding="utf-8")
    assert "Albert or Thapelo" in text and "before" in text
    assert same_event.aliases() == {"bafana bafana": "south africa"}


def test_a_table_given_by_path_is_the_one_read_and_the_longest_nickname_is_read_first(tmp_path):
    other = tmp_path / "aliases.yaml"
    other.write_text("aliases:\n  eagles: ghana\n  super eagles: nigeria\n", encoding="utf-8")
    assert same("Super Eagles vs Egypt", "Nigeria vs Egypt", "NG", path=other)
    assert not same("Bafana Bafana vs Egypt", SAFE, path=other)


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


def test_a_post_with_no_handle_is_not_a_creator_on_either_side():
    a = candidate("a", BAFANA, 1, handles=["@x"])
    b = candidate("b", SAFE, 1, handles=["@y"])
    for c in (a, b):
        c["pack"]["evidence"] += [{"id": f"{c['row']['item_id']}_n{i}", "platform": platform, "handle": handle}
                                  for i, (platform, handle) in enumerate(
                                      [("tiktok", None), ("instagram", None), ("x", ""), ("youtube", None)])]
        del c["pack"]["evidence"][-1]["handle"]
    assert not same_event.shares_evidence(a, b)


def test_the_same_handle_on_two_platforms_is_two_creators():
    a = candidate("a", BAFANA, 1, handles=["@x", "@y"])
    b = candidate("b", SAFE, 1, handles=["@x", "@y"])
    for e in b["pack"]["evidence"]:
        e["platform"] = "instagram"
    assert not same_event.shares_evidence(a, b)


# The collapse on the final decision: only a published card absorbs another, and the higher ranked one keeps the card


def pair(cand, decision=None):
    cand = dict(cand, decision=decision or cand["decision"])
    return cand, {**cand["row"], "decision": cand["decision"], "also": []}


def collapse(*pairs):
    items, merged = job._collapse_events("ZA", list(pairs))
    return [i["item_id"] for i in items], merged


def test_two_published_cards_that_share_a_post_and_say_one_event_become_one():
    bafana = pair(candidate("bafana", BAFANA, 0.97, posts={"p1", "p2", "p3"}))
    safe = pair(candidate("safe", SAFE, 0.96, posts={"p3", "p4", "p5"}))
    kept, merged = collapse(bafana, safe)
    assert kept == ["bafana"]
    assert merged == [{"market": "ZA", "into": "bafana", "item_id": "safe", "by": "same_event"}]
    assert bafana[1]["also"] == [{"item_id": "safe", "title": SAFE}]


def test_the_higher_ranked_card_keeps_the_card_whichever_way_round_they_are_listed():
    bafana = pair(candidate("bafana", BAFANA, 0.97, posts={"p1"}))
    safe = pair(candidate("safe", SAFE, 0.96, posts={"p1"}))
    kept, merged = collapse(safe, bafana)
    assert kept == ["bafana"] and [m["into"] for m in merged] == ["bafana"]


def test_the_same_event_without_shared_evidence_stays_two_cards():
    bafana = pair(candidate("bafana", BAFANA, 0.97, posts={"p1"}, handles=["@a", "@b"]))
    safe = pair(candidate("safe", SAFE, 0.96, posts={"p2"}, handles=["@c", "@d"]))
    kept, merged = collapse(bafana, safe)
    assert kept == ["bafana", "safe"] and merged == []


def test_shared_evidence_without_the_same_event_stays_two_cards():
    rugby = pair(candidate("rugby", "springboks, south africa, england", 0.97, posts={"p1"}))
    cricket = pair(candidate("cricket", "proteas, south africa, england", 0.96, posts={"p1"}))
    kept, merged = collapse(rugby, cricket)
    assert kept == ["rugby", "cricket"] and merged == []


def test_a_held_higher_card_never_takes_the_lower_published_card_with_it():
    held = pair(candidate("bafana", BAFANA, 0.97, posts={"p1"}, where="held_back", publish=False))
    safe = pair(candidate("safe", SAFE, 0.96, posts={"p1"}))
    kept, merged = collapse(held, safe)
    assert kept == ["bafana", "safe"] and merged == [] and held[1]["also"] == [] and safe[1]["also"] == []


def test_a_lower_card_held_at_the_final_decision_stays_its_own_held_item():
    bafana = pair(candidate("bafana", BAFANA, 0.97, posts={"p1"}))
    held = pair(candidate("safe", SAFE, 0.96, posts={"p1"}, where="held_back", publish=False))
    kept, merged = collapse(bafana, held)
    assert kept == ["bafana", "safe"] and merged == []


def test_a_decision_given_as_a_dict_is_read_the_same_way():
    bafana = pair(candidate("bafana", BAFANA, 0.97, posts={"p1"}), {"publish": True, "where": "today"})
    safe = pair(candidate("safe", SAFE, 0.96, posts={"p1"}), {"publish": True, "where": "today"})
    kept, merged = collapse(bafana, safe)
    assert kept == ["bafana"] and len(merged) == 1


def test_a_card_that_is_published_but_not_for_today_is_not_collapsed():
    bafana = pair(candidate("bafana", BAFANA, 0.97, posts={"p1"}))
    other = pair(candidate("safe", SAFE, 0.96, posts={"p1"}, where="more"))
    kept, merged = collapse(bafana, other)
    assert kept == ["bafana", "safe"] and merged == []


def test_a_decision_that_does_not_publish_is_not_collapsed_whatever_its_place():
    bafana = pair(candidate("bafana", BAFANA, 0.97, posts={"p1"}))
    unpublished = pair(candidate("safe", SAFE, 0.96, posts={"p1"}, publish=False))
    kept, merged = collapse(bafana, unpublished)
    assert kept == ["bafana", "safe"] and merged == []


def test_three_items_for_one_event_that_all_share_with_the_highest_collapse_into_it():
    a = pair(candidate("a", BAFANA, 0.97, posts={"p1"}))
    b = pair(candidate("b", SAFE, 0.96, posts={"p1"}))
    c = pair(candidate("c", "South Africa v Egypt", 0.95, posts={"p1"}))
    kept, merged = collapse(c, a, b)
    assert kept == ["a"] and [m["item_id"] for m in merged] == ["b", "c"]
    assert [x["item_id"] for x in a[1]["also"]] == ["b", "c"]


def test_a_card_absorbed_does_not_become_a_card_others_can_collapse_into():
    a = pair(candidate("a", BAFANA, 0.97, posts={"p1"}))
    b = pair(candidate("b", SAFE, 0.96, posts={"p1", "p2"}))
    c = pair(candidate("c", "South Africa v Egypt", 0.95, posts={"p2"}))
    kept, merged = collapse(a, b, c)
    assert kept == ["a", "c"] and [m["item_id"] for m in merged] == ["b"]


def test_a_market_without_cards_collapses_to_nothing():
    assert collapse() == ([], [])


# End to end on the fixture world


def cards(r):
    return {c["item_id"]: c for c in all_cards(payload(r, "ZA"))}


def world_with_the_match():
    con = world(n=1)
    add_item(con, "ZA", "za_bafana", 0.97, label=BAFANA, creators3=3, posts3=13)
    add_item(con, "ZA", "za_safe", 0.96, label=SAFE, creators3=2, posts3=5)
    return con


class BafanaWriterFails(FakeModel):
    """The writer cites a post that does not exist whenever the prompt is the Bafana card's, so it fails its checks."""

    def complete_json(self, **kw):
        out, usage = super().complete_json(**kw)
        if "claims" in kw["schema"]["properties"] and BAFANA in kw["user"]:
            for claim in out["claims"]:
                claim["evidence_ids"] = ["no_such_post"]
        return out, usage


def test_end_to_end_two_cards_for_one_event_that_share_a_post_show_as_one():
    con = world_with_the_match()
    link(con, "za_safe", ["za_bafana_p1"])
    r = brief(con, model=FakeModel(), confirm=FakeConfirm())
    shown = cards(r)
    assert "za_bafana" in shown and "za_safe" not in shown
    assert shown["za_bafana"]["also"] == [{"item_id": "za_safe", "title": SAFE}]
    assert shown["za_bafana"]["count_line"].endswith(f"({BAFANA} only)")
    assert r.counts["merged"] == [{"market": "ZA", "into": "za_bafana", "item_id": "za_safe", "by": "same_event"}]


def test_end_to_end_the_same_event_with_nothing_shared_stays_two_cards():
    r = brief(world_with_the_match(), model=FakeModel(), confirm=FakeConfirm())
    assert {"za_bafana", "za_safe"} <= set(cards(r))
    assert r.counts["merged"] == []


def test_end_to_end_when_the_higher_card_is_held_at_g10_the_other_card_is_still_a_card():
    con = world_with_the_match()
    link(con, "za_safe", ["za_bafana_p1"])
    r = brief(con, model=BafanaWriterFails(), confirm=FakeConfirm())
    assert "za_bafana" in held_items(r, "ZA") and held_items(r, "ZA")["za_bafana"]["reason"] == "explanation_failed"
    assert "za_safe" in cards(r) and "za_safe" not in held_items(r, "ZA")
    assert r.counts["merged"] == []


def test_end_to_end_a_lower_card_held_at_g10_stays_held_and_is_not_listed_under_the_kept_card():
    con = world_with_the_match()
    link(con, "za_safe", ["za_bafana_p1"])

    class SafeWriterFails(BafanaWriterFails):
        def complete_json(self, **kw):
            out, usage = FakeModel.complete_json(self, **kw)
            if "claims" in kw["schema"]["properties"] and SAFE in kw["user"]:
                for claim in out["claims"]:
                    claim["evidence_ids"] = ["no_such_post"]
            return out, usage

    r = brief(con, model=SafeWriterFails(), confirm=FakeConfirm())
    assert "za_bafana" in cards(r) and cards(r)["za_bafana"]["also"] == []
    assert held_items(r, "ZA")["za_safe"]["reason"] == "explanation_failed"
    assert r.counts["merged"] == []
