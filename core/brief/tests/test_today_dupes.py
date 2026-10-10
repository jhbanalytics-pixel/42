"""Two defects on the 10 October 2026 ZA Today board, rebuilt from rows shaped like that morning's two items.

Card 01 "Bafana Bafana vs Egypt" was the topic labelled "bafana, pitso, egypt": 3 creators and 13 posts in 3 days.
Card 02 "South Africa vs Egypt" was a second item for the same match: 0 creators and 0 posts in 3 days, its examples
all older than the counted days. The two shared no post, so the post overlap merge (core/brief/job.py _merge) left
both, and the 2 local rule counts the 7 day pack, so the card with nobody posting in the counted days passed it.
"""

from types import SimpleNamespace

import pytest

from core.brief import job
from core.brief.tests.test_brief_job import FakeConfirm, FakeModel, add_item, all_cards, brief, held_items, payload, world
from core.brief.tests.test_brief_job import busy_waits, market_scope_is_valid_by_default  # noqa: F401  (autouse)

BAFANA = dict(label="bafana, pitso, egypt", creators3=3, posts3=13)
SAFE = dict(label="South Africa vs Egypt")


def rec(evidence_id, *, market="ZA"):
    return {"id": evidence_id, "platform": "tiktok", "handle": f"@{evidence_id}", "url": f"https://x/{evidence_id}",
            "posted_at": "2026-10-04T19:40:00+02:00", "market": market, "source_market": None,
            "text": "t", "quote_text": "t", "engagement": {"views": 10}, "flags": [], "thumbnail_url": None}


def number(unit, value):
    return {"value": value, "unit": unit, "query_id": f"q_{unit}", "run_id": "r", "result_hash": "sha256:x"}


def gate_candidate(creators3, posts3):
    """Three showable local posts in the 7 day pack, as card 02 had, with the pinned 3 day counts beside them."""
    numbers = [n for n in (None if creators3 is None else number("creators in 3 days", creators3),
                           None if posts3 is None else number("posts in 3 days", posts3)) if n]
    return {"market": "ZA", "ctx": {}, "stages": {},
            "pack": {"evidence": [rec("a"), rec("b"), rec("c")], "numbers": numbers, "facts": []},
            "row": {"market_scope": "market", "map_status": "active", "nameless": False,
                    "authenticity": "clear", "sponsored_share": 0}}


@pytest.fixture
def gate_passes(monkeypatch):
    monkeypatch.setattr(job, "gate_card", lambda *_: job.Decision(True, "today", None, None, None, False))


# Defect 2: a card nobody posted for in the counted days


def test_a_card_with_zero_creators_in_the_counted_days_is_held_with_a_plain_reason(gate_passes):
    cand = gate_candidate(0, 0)
    decision = job._gate(cand, None)
    assert decision.publish is False and decision.where == "held_back"
    assert decision.reason == "No creator posted about it in the last 3 days"
    assert cand["held_reason"] == "too_few_creators"


def test_the_counted_days_hold_is_not_a_floor_hold_so_no_search_is_spent_on_it(gate_passes):
    cand = gate_candidate(0, 0)
    job._gate(cand, None)
    assert cand["floor_held"] is False
    assert not job._floor_held(cand)


@pytest.mark.parametrize("creators3,posts3", [(0, 3), (0, None), (None, 0)])
def test_a_measured_zero_in_either_counted_figure_holds_it(gate_passes, creators3, posts3):
    assert job._gate(gate_candidate(creators3, posts3), None).where == "held_back"


@pytest.mark.parametrize("creators3,posts3", [(1, 1), (2, 3), (12, 20), (None, None)])
def test_a_card_with_a_creator_in_the_counted_days_or_no_reading_is_not_held_by_it(gate_passes, creators3, posts3):
    cand = gate_candidate(creators3, posts3)
    decision = job._gate(cand, None)
    assert decision.where == "today" and cand["held_reason"] is None


def test_the_floors_are_checked_first_and_keep_their_own_reason(gate_passes):
    cand = gate_candidate(0, 0)
    cand["pack"]["evidence"] = cand["pack"]["evidence"][:2]
    decision = job._gate(cand, None)
    assert cand["held_reason"] == "not_confirmed" and cand["floor_held"] is True
    assert decision.reason.startswith("Fewer than 3 posts 42 can show")


def test_end_to_end_the_zero_creator_card_is_held_and_not_shown():
    con = world(n=1)
    add_item(con, "ZA", "za_bafana", 0.97, **BAFANA)
    add_item(con, "ZA", "za_safe", 0.96, creators3=0, posts3=0, **SAFE)
    r = brief(con, model=FakeModel(), confirm=FakeConfirm())
    assert "za_safe" not in [c["item_id"] for c in all_cards(payload(r, "ZA"))]
    held = held_items(r, "ZA")["za_safe"]
    assert held["reason"] == "too_few_creators"
    assert held["reason_text"] == "No creator posted about it in the last 3 days"
    assert "za_safe" not in [call_c["item_id"] for call in r.confirm.calls for call_c in call["candidates"]]


# Defect 1: one match, two cards


def event_candidate(item_id, worth, title, posts, *, label=None):
    return {"row": {"item_id": item_id, "title": title, "label": label or title, "canonical_key": item_id,
                    "worth_raw": worth, "market_scope": "market"},
            "decision": SimpleNamespace(publish=True, where="today"), "posts": set(posts),
            "pack": {"evidence": [], "numbers": [], "facts": []}}


def own_posts(prefix):
    return {f"{prefix}{n}" for n in range(1, 8)}


def merge(*cands):
    by_market = {"ZA": list(cands), "NG": [], "KE": []}
    return job._merge(by_market), by_market["ZA"]


def test_two_items_naming_one_match_through_a_team_nickname_are_one_card_though_they_share_no_post():
    bafana = event_candidate("bafana", 0.97, "bafana, pitso, egypt", own_posts("b"))
    safe = event_candidate("safe", 0.96, "South Africa vs Egypt", own_posts("s"))
    merged, left = merge(bafana, safe)
    assert merged == [{"market": "ZA", "into": "bafana", "item_id": "safe", "by": "same_event"}]
    assert left == [bafana]
    assert bafana["also"] == [{"item_id": "safe", "title": "South Africa vs Egypt"}]


def test_the_higher_ranked_item_keeps_the_card_whichever_way_round_they_are_listed():
    bafana = event_candidate("bafana", 0.97, "Bafana Bafana vs Egypt", own_posts("b"))
    safe = event_candidate("safe", 0.96, "South Africa vs Egypt", own_posts("s"))
    merged, left = merge(safe, bafana)
    assert [m["into"] for m in merged] == ["bafana"] and left == [bafana]


@pytest.mark.parametrize("other", ["South Africa vs Nigeria", "Banyana Banyana vs Egypt", "Egypt", "South Africa",
                                   "Bafana fans at the stadium", "Kaizer Chiefs vs Orlando Pirates"])
def test_a_different_match_or_a_single_name_is_never_merged_as_the_same_event(other):
    bafana = event_candidate("bafana", 0.97, "bafana, pitso, egypt", own_posts("b"))
    second = event_candidate("second", 0.96, other, own_posts("s"))
    merged, left = merge(bafana, second)
    assert merged == [] and left == [bafana, second]


def test_the_event_rule_leaves_the_post_overlap_merge_as_it_was():
    first = event_candidate("first", 0.97, "Amapiano night", own_posts("f"))
    second = event_candidate("second", 0.96, "Gqom night", {"f1", "f2"})
    merged, _ = merge(first, second)
    assert merged == [{"market": "ZA", "into": "first", "item_id": "second"}]


def test_end_to_end_one_card_for_the_match_with_the_other_title_listed_under_it():
    con = world(n=1)
    add_item(con, "ZA", "za_bafana", 0.97, **BAFANA)
    add_item(con, "ZA", "za_safe", 0.96, creators3=2, posts3=5, **SAFE)
    r = brief(con, model=FakeModel(), confirm=FakeConfirm())
    ids = [c["item_id"] for c in all_cards(payload(r, "ZA"))]
    assert "za_bafana" in ids and "za_safe" not in ids
    card = {c["item_id"]: c for c in all_cards(payload(r, "ZA"))}["za_bafana"]
    assert card["also"] == [{"item_id": "za_safe", "title": "South Africa vs Egypt"}]
    assert r.counts["merged"] == [{"market": "ZA", "into": "za_bafana", "item_id": "za_safe", "by": "same_event"}]


def test_a_candidate_regrown_by_confirm_into_the_same_event_merges_instead_of_returning_as_a_second_card():
    from core.brief.tests.test_brief_job import GrowingConfirm

    con = world(n=1)
    add_item(con, "NG", "ng_match", 0.99, label="Nigeria vs Ghana")
    add_item(con, "NG", "ng_nickname", 0.98, posts=1, creators3=2, label="Super Eagles vs Black Stars")
    r = brief(con, confirm=GrowingConfirm(con, {"ng_nickname": 3}))
    ids = [c["item_id"] for c in all_cards(payload(r, "NG"))]
    assert "ng_match" in ids and "ng_nickname" not in ids
    assert "ng_nickname" not in held_items(r, "NG")
    assert r.counts["merged"] == [{"market": "NG", "into": "ng_match", "item_id": "ng_nickname", "by": "same_event"}]
