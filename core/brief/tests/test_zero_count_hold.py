"""A card whose counted 3 days hold no creator or no post is held (10 October 2026 ZA board, card 02: "0 creators and
0 posts in 3 days" over examples older than those days). The floors read the 7 day pack and the card's counts are the
last 3 days, so nothing compared the two windows before this hold (core/brief/job.py _gate)."""

import pytest

from core.brief import job
from core.brief.tests.test_brief_job import FakeConfirm, FakeModel, add_item, all_cards, brief, held_items, payload, world
from core.brief.tests.test_brief_job import busy_waits, market_scope_is_valid_by_default  # noqa: F401  (autouse)


def rec(evidence_id, *, market="ZA"):
    return {"id": evidence_id, "platform": "tiktok", "handle": f"@{evidence_id}", "url": f"https://x/{evidence_id}",
            "posted_at": "2026-10-04T19:40:00+02:00", "market": market, "source_market": None,
            "text": "t", "quote_text": "t", "engagement": {"views": 10}, "flags": [], "thumbnail_url": None}


def number(unit, value):
    return {"value": value, "unit": unit, "query_id": f"q_{unit}", "run_id": "r", "result_hash": "sha256:x"}


def gate_candidate(creators3, posts3, *, extra=()):
    """Three showable local posts in the 7 day pack, as card 02 had, with the pinned 3 day counts beside them."""
    numbers = [n for n in (None if creators3 is None else number("creators in 3 days", creators3),
                           None if posts3 is None else number("posts in 3 days", posts3)) if n]
    return {"market": "ZA", "ctx": {}, "stages": {},
            "pack": {"evidence": [rec("a"), rec("b"), rec("c")], "numbers": [*extra, *numbers], "facts": []},
            "row": {"market_scope": "market", "map_status": "active", "nameless": False,
                    "authenticity": "clear", "sponsored_share": 0}}


@pytest.fixture
def gate_passes(monkeypatch):
    monkeypatch.setattr(job, "gate_card", lambda *_: job.Decision(True, "today", None, None, None, False))


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


def test_each_figure_is_read_by_its_unit_not_by_its_place_in_the_pack(gate_passes):
    other = number("times usual", 0)
    posts_first = gate_candidate(4, 9)
    posts_first["pack"]["numbers"].reverse()
    assert job._gate(posts_first, None).where == "today"
    assert job._gate(gate_candidate(4, 9, extra=[other]), None).where == "today"
    zero_after = gate_candidate(0, 9)
    zero_after["pack"]["numbers"].insert(0, number("posts in 3 days", 9))
    assert job._gate(zero_after, None).where == "held_back"
    zero_posts = gate_candidate(5, 0)
    zero_posts["pack"]["numbers"].reverse()
    assert job._gate(zero_posts, None).where == "held_back"


def test_a_hold_the_gate_already_made_keeps_its_own_reason(monkeypatch):
    monkeypatch.setattr(job, "gate_card", lambda *_: job.Decision(False, "held_back", None, "Not local to this market",
                                                                   "G6", False))
    cand = gate_candidate(0, 0)
    decision = job._gate(cand, None)
    assert decision.rule == "G6" and decision.reason == "Not local to this market"
    assert cand["held_reason"] is None


def test_the_floors_are_checked_first_and_keep_their_own_reason(gate_passes):
    cand = gate_candidate(0, 0)
    cand["pack"]["evidence"] = cand["pack"]["evidence"][:2]
    decision = job._gate(cand, None)
    assert cand["held_reason"] == "not_confirmed"
    assert decision.reason.startswith("Fewer than 3 posts 42 can show")


def test_a_floor_held_card_with_a_measured_zero_is_not_searched_because_no_search_can_lift_it(gate_passes):
    cand = gate_candidate(0, 0)
    cand["pack"]["evidence"] = cand["pack"]["evidence"][:2]
    job._gate(cand, None)
    assert cand["floor_held"] is True and not job._floor_held(cand)


def test_a_floor_held_card_without_a_measured_zero_is_still_searched(gate_passes):
    cand = gate_candidate(4, 9)
    cand["pack"]["evidence"] = cand["pack"]["evidence"][:2]
    job._gate(cand, None)
    assert job._floor_held(cand)


def test_end_to_end_the_zero_creator_card_is_held_and_not_shown():
    con = world(n=1)
    add_item(con, "ZA", "za_bafana", 0.97, label="bafana, pitso, egypt", creators3=3, posts3=13)
    add_item(con, "ZA", "za_safe", 0.96, label="South Africa vs Egypt", creators3=0, posts3=0)
    r = brief(con, model=FakeModel(), confirm=FakeConfirm())
    assert "za_safe" not in [c["item_id"] for c in all_cards(payload(r, "ZA"))]
    held = held_items(r, "ZA")["za_safe"]
    assert held["reason"] == "too_few_creators"
    assert held["reason_text"] == "No creator posted about it in the last 3 days"
    assert "za_safe" not in [call_c["item_id"] for call in r.confirm.calls for call_c in call["candidates"]]
