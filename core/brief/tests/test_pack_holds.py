"""Holds when the pack caps leave too little evidence, the outlet registry and the pack's stage counts (C4 v2
section 19 rules 5 to 7). The floors themselves (3 showable, 2 local) are pinned here and must not move."""

import json

import pytest

from core.api import today
from core.brief import evidence, job, pack_order
from core.brief import payload as payload_module
from core.brief.specificity import MIN_EVIDENCE
from core.brief.tests import test_pack_order as pack_world
from core.brief.tests.test_brief_job import Client, add_item, brief, held_items, item, payload, world
from core.brief.tests.test_brief_job import busy_waits, market_scope_is_valid_by_default  # noqa: F401  (autouse)
from core.detect.tests import duck
from core.detect.tests.fixtures import D, at, day


def rec(evidence_id, *, market="ZA", source_market=None):
    return {"id": evidence_id, "platform": "tiktok", "handle": f"@{evidence_id}", "url": f"https://x/{evidence_id}",
            "posted_at": "2026-09-29T19:40:00+02:00", "market": market, "source_market": source_market,
            "text": "t", "quote_text": "t", "engagement": {"views": 10}, "flags": [], "thumbnail_url": None}


def candidate(evidence_list, stages=None):
    return {"market": "ZA", "ctx": {}, "pack": {"evidence": evidence_list}, "stages": stages or {},
            "row": {"market_scope": "market", "map_status": "active", "nameless": False,
                    "authenticity": "clear", "sponsored_share": 0}}


def stage(posts, showable=None, local=None, members=0):
    return {"posts": posts, "showable": posts if showable is None else showable,
            "local": posts if local is None else local, "members": members}


def stages(available, after_creator, after_outlet):
    return {"version": 1, "available": available, "after_creator_cap": after_creator,
            "after_outlet_cap": after_outlet}


@pytest.fixture
def gate_passes(monkeypatch):
    monkeypatch.setattr(job, "gate_card", lambda *_: job.Decision(True, "today", None, None, None, False))


# The floors do not move


def test_the_floors_are_three_showable_and_two_local():
    assert MIN_EVIDENCE == 3
    assert pack_order.FLOORS == {"showable": 3, "local": 2}


@pytest.mark.parametrize("showable,local,held", [(3, 2, False), (2, 2, True), (3, 1, True), (12, 2, False), (12, 1, True)])
def test_the_gate_still_holds_below_three_showable_or_two_local_posts_and_not_above(gate_passes, showable, local, held):
    posts = [rec(f"l{n}") for n in range(local)] + [rec(f"u{n}", market=None) for n in range(showable - local)]
    cand = candidate(posts)
    decision = job._gate(cand, None)
    assert (decision.where == "held_back") is held
    assert (cand["held_reason"] == "not_confirmed") is held


# The cause is the first stage below the floor


@pytest.mark.parametrize("counts,cause", [
    ((2, 2, 2, 2), "evidence_absent"),
    ((4, 2, 2, 2), "capped_by_creator"),
    ((5, 4, 1, 1), "capped_by_outlet"),
    ((5, 5, 5, 2), "capped_by_size"),
    ((1, 1, 1, 1), "evidence_absent"),
    ((3, 2, 2, 2), "capped_by_creator"),  # exactly at the floor of 3 is not below it
    ((3, 3, 2, 2), "capped_by_outlet"),
    ((3, 3, 3, 2), "capped_by_size"),
])
def test_the_cause_is_the_first_stage_at_which_the_count_falls_below_the_floor(counts, cause):
    a, b, c, f = counts
    posts = [rec(f"l{n}") for n in range(f)]
    detail = pack_order.hold_detail(stages(stage(a), stage(b), stage(c)), "showable", posts, "ZA")
    assert detail["cause"] == cause
    assert detail["block_version"] == 1 and detail["floor"] == "showable" and detail["minimum"] == 3
    assert detail["counts"] == {"available": a, "after_creator_cap": b, "after_outlet_cap": c, "final": f}


def test_the_local_floor_reads_the_local_counts_not_the_showable_ones():
    posts = [rec("a"), rec("b", market=None), rec("c", market=None)]
    detail = pack_order.hold_detail(stages(stage(6, local=4), stage(5, local=1), stage(5, local=1)), "local", posts, "ZA")
    assert detail["cause"] == "capped_by_creator" and detail["minimum"] == 2
    assert detail["counts"] == {"available": 4, "after_creator_cap": 1, "after_outlet_cap": 1, "final": 1}


@pytest.mark.parametrize("counts,cause", [((2, 1, 1, 1), "capped_by_creator"), ((2, 2, 1, 1), "capped_by_outlet"),
                                           ((2, 2, 2, 1), "capped_by_size"), ((1, 1, 1, 1), "evidence_absent")])
def test_the_local_floor_of_two_is_also_met_at_exactly_two(counts, cause):
    a, b, c, f = counts
    posts = [rec(f"l{n}") for n in range(f)]
    detail = pack_order.hold_detail(stages(stage(a), stage(b), stage(c)), "local", posts, "ZA")
    assert detail["cause"] == cause and detail["minimum"] == 2


def test_the_final_count_never_reads_lower_than_the_posts_the_gate_read():
    """A merge or a second read can put more posts in the pack than the query's own read had: 1 at the query's end,
    2 in the evidence the gate read. The final count is then the 2, and no mask is involved."""
    posts = [rec("a"), rec("b")]
    counts = stages(stage(2), stage(2), stage(2))
    counts["final"] = stage(1)
    detail = pack_order.hold_detail(counts, "showable", posts, "ZA")
    assert detail["counts"] == {"available": 2, "after_creator_cap": 2, "after_outlet_cap": 2, "final": 2}
    assert detail["cause"] == "evidence_absent"
    assert pack_order.masked_after_ranking(counts, "showable", posts, "ZA") is False


def test_posts_removed_after_the_ranking_are_not_blamed_on_the_12_post_limit_and_their_number_is_not_shown():
    posts = [rec("a"), rec("b")]  # 4 reached the end of the query, 2 were taken out afterwards
    counts = stages(stage(4), stage(4), stage(4))
    counts["final"] = stage(4)
    detail = pack_order.hold_detail(counts, "showable", posts, "ZA")
    assert detail["cause"] == "removed_after_ranking"
    assert detail["counts"] == {"available": 4, "after_creator_cap": 4, "after_outlet_cap": 4, "final": 4}
    assert pack_order.hold_text("Fewer than 3 posts 42 can show", detail) == "Fewer than 3 posts 42 can show"


def test_the_12_post_limit_is_still_named_when_it_alone_left_too_little_even_if_posts_were_also_removed():
    posts = [rec("a")]
    counts = stages(stage(20), stage(20), stage(20))
    counts["final"] = stage(2)
    assert pack_order.hold_detail(counts, "showable", posts, "ZA")["cause"] == "capped_by_size"


def test_a_final_count_above_an_earlier_stage_count_is_not_read_as_a_cap():
    posts = [rec(f"l{n}") for n in range(3)]  # a merge or a regrow added posts the first read did not have
    assert pack_order.hold_detail(stages(stage(1), stage(1), stage(1)), "showable", posts, "ZA")["counts"] == {
        "available": 3, "after_creator_cap": 3, "after_outlet_cap": 3, "final": 3}


def test_a_pack_with_no_stage_counts_reads_every_stage_as_the_final_pack():
    posts = [rec("a"), rec("b")]
    detail = pack_order.hold_detail({}, "showable", posts, "ZA")
    assert detail["cause"] == "evidence_absent"
    assert detail["counts"] == {"available": 2, "after_creator_cap": 2, "after_outlet_cap": 2, "final": 2}


def test_hold_base_reads_the_wording_whatever_cap_it_names():
    base = "Fewer than 3 posts 42 can show"
    for cap in ("creator cap", "outlet cap", "12-post limit"):
        assert pack_order.hold_base(f"{base}: the {cap} left 2 of 4") == base
    assert pack_order.hold_base(base) == base and pack_order.hold_base("Platform-generic tag") == "Platform-generic tag"


def test_the_hold_text_names_the_cap_that_removed_the_posts_and_keeps_the_old_text_when_none_did():
    base = "Fewer than 2 supported local posts"
    outlet = {"cause": "capped_by_outlet", "counts": {"available": 5, "after_creator_cap": 4, "after_outlet_cap": 1,
                                                       "final": 1}}
    creator = {"cause": "capped_by_creator", "counts": {"available": 5, "after_creator_cap": 1, "after_outlet_cap": 1,
                                                         "final": 1}}
    size = {"cause": "capped_by_size", "counts": {"available": 9, "after_creator_cap": 9, "after_outlet_cap": 9,
                                                   "final": 1}}
    absent = {"cause": "evidence_absent", "counts": {"available": 1, "after_creator_cap": 1, "after_outlet_cap": 1,
                                                      "final": 1}}
    assert pack_order.hold_text(base, outlet) == "Fewer than 2 supported local posts: the outlet cap left 1 of 4"
    assert pack_order.hold_text(base, creator) == "Fewer than 2 supported local posts: the creator cap left 1 of 5"
    assert pack_order.hold_text(base, size) == "Fewer than 2 supported local posts: the 12-post limit left 1 of 9"
    assert pack_order.hold_text(base, absent) == base


# The gate carries the detail


def test_a_floor_hold_carries_its_cause_and_counts_and_the_text_names_the_cap(gate_passes):
    posts = [rec("a"), rec("b", market=None), rec("c", market=None)]
    cand = candidate(posts, stages(stage(6, local=4), stage(5, local=4), stage(5, local=1)))
    decision = job._gate(cand, None)
    assert decision.where == "held_back" and cand["held_reason"] == "not_confirmed" and cand["floor_held"]
    assert decision.reason == "Fewer than 2 supported local posts: the outlet cap left 1 of 4"
    assert cand["held_reason_detail"] == {
        "block_version": 1, "floor": "local", "minimum": 2, "cause": "capped_by_outlet",
        "counts": {"available": 4, "after_creator_cap": 4, "after_outlet_cap": 1, "final": 1}}


def test_a_showable_hold_by_the_creator_cap_says_so(gate_passes):
    posts = [rec("a"), rec("b")]
    cand = candidate(posts, stages(stage(4), stage(2), stage(2)))
    decision = job._gate(cand, None)
    assert decision.reason == "Fewer than 3 posts 42 can show: the creator cap left 2 of 4"
    assert cand["held_reason_detail"]["floor"] == "showable" and cand["held_reason_detail"]["cause"] == "capped_by_creator"


def test_a_hold_that_no_cap_caused_keeps_the_old_wording_and_still_has_a_detail(gate_passes):
    cand = candidate([rec("a"), rec("b")])
    decision = job._gate(cand, None)
    assert decision.reason == "Fewer than 3 posts 42 can show"
    assert cand["held_reason_detail"]["cause"] == "evidence_absent"


def test_a_card_that_passes_has_no_detail(gate_passes):
    cand = candidate([rec(f"l{n}") for n in range(3)])
    assert job._gate(cand, None).where == "today" and cand["held_reason_detail"] is None


def test_every_floor_hold_in_a_job_run_has_a_detail_and_its_text_still_groups_in_the_held_summary():
    con = world(n=2)
    add_item(con, "NG", "two_posts", 0.98, posts=2, creators3=2)
    add_item(con, "NG", "samsung", 0.99, posts=0, state="new_to_42", untested=True, creators3=2)
    r = brief(con)
    held = held_items(r, "NG")
    for item_id in ("two_posts", "samsung"):
        assert held[item_id]["reason"] == "not_confirmed"
        assert held[item_id]["reason_text"] == "Fewer than 3 posts 42 can show"
        detail = held[item_id]["held_reason_detail"]
        assert detail["block_version"] == 1 and detail["cause"] == "evidence_absent"
    assert held["two_posts"]["held_reason_detail"]["counts"] == {
        "available": 2, "after_creator_cap": 2, "after_outlet_cap": 2, "final": 2}
    assert held["samsung"]["held_reason_detail"]["counts"]["available"] == 0


def test_a_floor_hold_the_suppression_mask_caused_in_a_job_run_has_a_detail_like_any_other(monkeypatch):
    # The query leaves out creators the list names by id; the mask this is about takes out those it names by handle.
    monkeypatch.setattr(job, "read_hidden", lambda *a, **k: ({"tiktok:masked_c1"}, set(), set()))
    con = world(n=2)
    add_item(con, "NG", "masked", 0.98, posts=3, creators3=2)  # 3 posts reach the end of the query
    add_item(con, "NG", "two_posts", 0.97, posts=2, creators3=2)  # 2 posts, nobody hidden
    r = brief(con)
    held = held_items(r, "NG")
    for item_id in ("masked", "two_posts"):
        assert held[item_id]["reason"] == "not_confirmed"
        assert held[item_id]["reason_text"] == "Fewer than 3 posts 42 can show"
        assert len(held[item_id]["evidence"]) == 2
        assert held[item_id]["held_reason_detail"]["cause"] == "evidence_absent"
        assert held[item_id]["held_reason_detail"]["counts"] == {
            "available": 2, "after_creator_cap": 2, "after_outlet_cap": 2, "final": 2}
    stored = payload(r, "NG")
    assert [a["item_id"] for a in stored["hold_audit"]] == ["masked"]  # the mask's own counts are kept apart
    assert stored["hold_audit"][0]["cause"] == "removed_after_ranking" and stored["hold_audit"][0]["counts"]["final"] == 3


def one_creator_item(con, item_id, n):
    add_item(con, "NG", item_id, 0.98, posts=0, creators3=2)
    for k in range(n):
        pid = f"{item_id}_p{k}"
        duck.load(con, "core.posts", [{
            "post_id": pid, "platform": "tiktok", "creator_id": f"{item_id}_same", "creator_tier_at_post": "micro",
            "text": "t", "published_at": at(day(1), 9), "post_date": day(1),
            "geo_market": "NG", "geo_confidence": 0.9, "geo_source": "ext_region", "engagement": 100 - k}])
        duck.load(con, "core.post_items", [{"post_id": pid, "item_id": item_id, "via": "hashtag"}])
        duck.load(con, "core.post_observations", [{
            "post_id": pid, "observed_at": at(day(1), 10), "observed_date": day(1), "market": "NG",
            "platform": "tiktok", "lane": "sweep", "lane_class": "unbiased_rank"}])
    duck.load(con, "core.creators", [{"creator_id": f"{item_id}_same", "platform": "tiktok",
                                      "handle": f"@{item_id}", "coord_score": 0}])


def test_a_hold_the_creator_cap_caused_is_named_in_a_job_run_and_all_floor_holds_stay_one_group():
    con = world(n=2)
    one_creator_item(con, "one_creator", 4)
    one_creator_item(con, "other_creator", 5)  # a different count, so a different wording
    r = brief(con)
    held = held_items(r, "NG")["one_creator"]
    assert held["reason"] == "not_confirmed"
    assert held["reason_text"] == "Fewer than 3 posts 42 can show: the creator cap left 2 of 4"
    assert held["held_reason_detail"]["cause"] == "capped_by_creator"
    assert held["held_reason_detail"]["counts"] == {"available": 4, "after_creator_cap": 2, "after_outlet_cap": 2,
                                                    "final": 2}
    assert held_items(r, "NG")["other_creator"]["reason_text"] == "Fewer than 3 posts 42 can show: the creator cap left 2 of 5"
    assert payload(r, "NG")["held_back"]["text"] == "2 held back: with fewer than 3 posts"


def test_the_holds_report_groups_floor_holds_by_their_wording_not_by_their_counts():
    from core.brief import holds_report
    base = "Fewer than 3 posts 42 can show"
    held = [{"market": "NG", "title": f"t{n}", "reason_text": f"{base}: the creator cap left 2 of {n + 3}",
             "reason": "not_confirmed"} for n in range(3)]
    assert [(text, len(items)) for text, items in holds_report.causes(held)] == [(base, 3)]


# The pack carries the stage counts


def test_build_pack_passes_the_registry_and_the_default_cap_and_returns_the_stage_counts(monkeypatch):
    seen = {}
    real = pack_order.outlet_keys
    monkeypatch.setattr(evidence.pack_order, "outlet_keys", lambda *a, **k: seen.setdefault("keys", real(*a, **k)))
    con = world(n=1, markets=("NG",))
    state = duck.query(con, "SELECT s.* FROM {core}.v_item_state_current s WHERE s.item_id = @i AND s.market = 'NG'",
                       {"i": item("NG", 1)})[0]
    client = Client(con)
    counts = {}
    pack, _, _ = evidence.build_pack(client, state, D, "NG", core="core", agent="agent", stages=counts)
    assert seen["keys"] == []
    assert set(pack) == {"evidence", "numbers", "facts"}  # the pack keeps its three keys
    assert counts["version"] == 1
    assert counts["available"] == {"posts": 3, "showable": 3, "local": 3, "members": 0}
    assert counts["after_outlet_cap"]["posts"] == len(pack["evidence"]) == 3


def test_build_pack_reads_an_empty_pack_the_outlet_cap_left_as_no_posts_with_its_counts(monkeypatch):
    monkeypatch.setattr(pack_order, "OUTLET_CAP", 0)
    con = pack_world.outlet_only_world()
    duck.load(con, "agent.runs", [])
    duck.load(con, "core.item_state", [{"metric_date": D, "market": "NG", "item_id": "i1", "kind": "hashtag",
                                        "state": "emerging", "untested": True, "run_id": "detect-1",
                                        "rule_version": "r1"}])
    row = {"item_id": "i1", "run_id": "detect-1", "state": "emerging", "untested": True, "main_series_id": None}
    counts = {}
    pack, _, _ = evidence.build_pack(Client(con), row, pack_world.D, "NG", core="core", agent="agent",
                                     hidden=(set(), set(), set()), stages=counts)
    assert pack["evidence"] == []
    assert counts["available"]["posts"] == 3 and counts["after_outlet_cap"]["posts"] == 0


@pytest.mark.parametrize("cap,cause,text", [
    (12, "capped_by_size", "Fewer than 3 posts 42 can show: the 12-post limit left 0 of 3"),
    (3, "capped_by_outlet", "Fewer than 3 posts 42 can show: the outlet cap left 0 of 3"),
    (0, "capped_by_outlet", "Fewer than 3 posts 42 can show: the outlet cap left 0 of 3")])
def test_an_outlet_cap_below_12_is_named_when_it_removed_the_showable_posts_from_inside_the_first_12_places(
        gate_passes, monkeypatch, cap, cause, text):
    monkeypatch.setattr(pack_order, "OUTLET_CAP", cap)
    con = pack_world.abroad_outlets_world()
    duck.load(con, "core.item_state", [{"metric_date": D, "market": "NG", "item_id": "i1", "kind": "hashtag",
                                        "state": "emerging", "untested": True, "run_id": "detect-1",
                                        "rule_version": "r1"}])
    row = {"item_id": "i1", "run_id": "detect-1", "state": "emerging", "untested": True, "main_series_id": None}
    counts = {}
    pack, _, _ = evidence.build_pack(Client(con), row, pack_world.D, "NG", core="core", agent="agent",
                                     hidden=(set(), set(), set()), stages=counts)
    assert len(pack["evidence"]) == min(cap, 12)
    cand = candidate(pack["evidence"])
    cand.update(market="NG", stages=counts)
    decision = job._gate(cand, None)
    assert (cand["held_reason_detail"]["cause"], decision.reason) == (cause, text)
    assert cand["held_reason_detail"]["counts"] == {"available": 3, "after_creator_cap": 3, "after_outlet_cap": 3 if cap == 12 else 0, "final": 0}


def test_posts_the_suppression_mask_removes_after_the_query_are_not_blamed_on_the_12_post_limit(gate_passes):
    con = duck.connect()
    pack_world.clusters(con)
    for n in range(4):
        pack_world.post(con, f"p{n}", creator=f"h{n}", eng=100 - n)
    duck.load(con, "core.item_state", [{"metric_date": D, "market": "NG", "item_id": "i1", "kind": "hashtag",
                                        "state": "emerging", "untested": True, "run_id": "detect-1",
                                        "rule_version": "r1"}])
    row = {"item_id": "i1", "run_id": "detect-1", "state": "emerging", "untested": True, "main_series_id": None}
    counts = {}
    pack, _, _ = evidence.build_pack(Client(con), row, pack_world.D, "NG", core="core", agent="agent",
                                     hidden=({"tiktok:h0", "tiktok:h1"}, set(), set()), stages=counts)
    assert len(pack["evidence"]) == 2 and counts["after_outlet_cap"]["posts"] == 4
    cand = candidate(pack["evidence"])
    cand.update(market="NG", stages=counts)  # the posts are NG posts, so the floor counts them as showable in NG
    decision = job._gate(cand, None)
    assert decision.reason == "Fewer than 3 posts 42 can show"  # says nothing of a limit, or of what was removed
    # What is served is counted over the posts a reader can see, so it shows nothing of the posts that were hidden.
    assert cand["held_reason_detail"]["cause"] == "evidence_absent"
    assert cand["held_reason_detail"]["counts"] == {"available": 2, "after_creator_cap": 2, "after_outlet_cap": 2,
                                                    "final": 2}
    assert cand["held_reason_audit"]["cause"] == "removed_after_ranking"
    assert cand["held_reason_audit"]["counts"] == {"available": 4, "after_creator_cap": 4, "after_outlet_cap": 4,
                                                   "final": 4}


def served_hold(cand, decision):
    """The held item as the brief stores it and as the API serves it to a reader."""
    cand["decision"] = {"publish": decision.publish, "where": decision.where, "flag": decision.flag,
                        "reason": decision.reason, "rule": decision.rule, "numbers_only": decision.numbers_only}
    cand.setdefault("title", "t")
    cand.setdefault("item_id", "i1")
    cand["evidence"] = cand["pack"]["evidence"]
    stored = payload_module._held_item(cand)
    return stored, today._held_item(stored, {})


def served_in_world(posts, hidden_keys):
    """The hold of item i1 as stored and as served, in a world of these (post id, creator, engagement) posts."""
    con = duck.connect()
    pack_world.clusters(con)
    for post_id, creator, eng in posts:
        pack_world.post(con, post_id, creator=creator, eng=eng)
    duck.load(con, "core.item_state", [{"metric_date": D, "market": "NG", "item_id": "i1", "kind": "hashtag",
                                        "state": "emerging", "untested": True, "run_id": "detect-1",
                                        "rule_version": "r1"}])
    row = {"item_id": "i1", "run_id": "detect-1", "state": "emerging", "untested": True, "main_series_id": None}
    counts = {}
    pack, _, _ = evidence.build_pack(Client(con), row, pack_world.D, "NG", core="core", agent="agent",
                                     hidden=(hidden_keys, set(), set()), stages=counts)
    cand = candidate(pack["evidence"])
    cand.update(market="NG", stages=counts)
    stored, served = served_hold(cand, job._gate(cand, None))
    return cand, stored, served


def test_a_reader_of_the_api_cannot_tell_from_a_hold_that_posts_were_hidden(gate_passes):
    """The reviewer's probe. World A: 4 posts reached the end of the query, 2 of them by people on the hidden list.
    World B: the same 2 visible posts and nobody hidden. The served items must be identical, byte for byte."""
    visible = [("p2", "h2", 98), ("p3", "h3", 97)]
    cand_a, stored_a, served_a = served_in_world([("p0", "h0", 100), ("p1", "h1", 99)] + visible,
                                                 {"tiktok:h0", "tiktok:h1"})
    cand_b, stored_b, served_b = served_in_world(visible, set())
    assert len(served_a["evidence"]) == 2 and served_a["reason_text"] == "Fewer than 3 posts 42 can show"
    assert json.dumps(served_a, sort_keys=True) == json.dumps(served_b, sort_keys=True)
    assert json.dumps(stored_a, sort_keys=True) == json.dumps(stored_b, sort_keys=True)
    detail = served_a["held_reason_detail"]  # counted over the posts a reader can see, as in world B
    assert detail["cause"] == "evidence_absent"
    assert detail["counts"] == {"available": 2, "after_creator_cap": 2, "after_outlet_cap": 2, "final": 2}
    # The counts of the pack before the mask stay with the audit record, which the API does not serve.
    assert cand_a["held_reason_audit"]["cause"] == "removed_after_ranking"
    assert cand_a["held_reason_audit"]["counts"]["final"] == 4
    assert cand_b["held_reason_audit"] is None
    assert "removed_after_ranking" not in json.dumps(served_a) and "4" not in str(served_a["held_reason_detail"])


def test_the_audit_record_reaches_the_candidate_the_payload_is_built_from(gate_passes):
    cand = candidate([rec("a")], stages=stages(stage(4), stage(4), stage(4)))
    cand["stages"]["final"] = stage(4)
    cand["pack"]["numbers"] = []
    cand.update(sparkline=None, decision=job._gate(cand, None))
    item = job._payload_candidate(cand, None)
    assert item["held_reason_audit"]["cause"] == "removed_after_ranking"
    assert item["held_reason_detail"]["cause"] == "evidence_absent"
    assert item["held_reason_detail"]["counts"]["final"] == 1


@pytest.mark.parametrize("floor_posts,shown,counts", [
    ((4, 2, 2, 2), 1, {"available": 4, "after_creator_cap": 2, "after_outlet_cap": 2, "final": 2}),
    ((4, 4, 4, 3), 2, {"available": 4, "after_creator_cap": 4, "after_outlet_cap": 4, "final": 3})])
def test_a_hold_whose_pack_lost_posts_to_the_mask_names_no_cap_and_serves_no_counts(
        gate_passes, floor_posts, shown, counts):
    a, b, c, f = floor_posts
    cand = candidate([rec(f"l{n}") for n in range(shown)], stages=stages(stage(a), stage(b), stage(c)))
    cand["stages"]["final"] = stage(f)
    decision = job._gate(cand, None)
    assert decision.reason == "Fewer than 3 posts 42 can show"  # a cap is not named: it would place the loss
    visible = {name: shown for name in ("available", "after_creator_cap", "after_outlet_cap", "final")}
    assert cand["held_reason_detail"]["cause"] == "evidence_absent" and cand["held_reason_detail"]["counts"] == visible
    assert cand["held_reason_audit"]["counts"] == counts and cand["held_reason_audit"]["minimum"] == 3


def test_a_hold_that_lost_nothing_to_the_mask_still_serves_its_counts(gate_passes):
    cand = candidate([rec("a"), rec("b")], stages=stages(stage(4), stage(2), stage(2)))
    cand["stages"]["final"] = stage(2)
    decision = job._gate(cand, None)
    assert decision.reason == "Fewer than 3 posts 42 can show: the creator cap left 2 of 4"
    assert cand["held_reason_detail"]["cause"] == "capped_by_creator" and cand["held_reason_audit"] is None


def test_the_mask_check_reads_only_the_floors_own_kind_of_post_and_a_missing_stage_means_no_mask():
    posts = [rec("a"), rec("b", market="KE")]  # 2 posts, one showable here, one not
    both = {"final": stage(3, showable=2, local=1)}
    assert pack_order.masked_after_ranking(both, "showable", posts, "ZA") is True   # 2 showable were left, 1 shown
    assert pack_order.masked_after_ranking({"final": stage(2, showable=1, local=1)}, "showable", posts, "ZA") is False
    assert pack_order.masked_after_ranking({"final": stage(3, showable=1, local=1)}, "showable", posts, "ZA") is False
    assert pack_order.masked_after_ranking({}, "showable", posts, "ZA") is False
    assert pack_order.masked_after_ranking(None, "showable", posts, "ZA") is False


# The outlet registry


def write(tmp_path, text):
    path = tmp_path / "hubs.yaml"
    path.write_text(text, encoding="utf-8")
    return path


CONFIRMED = """
status: confirmed
markets:
  za:
    confirmed_by: Jo
    x:
      - {handle: IOL, platform: x, kind: news}
      - {handle: Soccer_Laduma, platform: twitter, kind: sport}
      - {handle: casspernyovest, platform: x, kind: music}
    culture_desk:
      - {handle: SomeDesk, platform: instagram, kind: news}
  ng:
    confirmed_by: null
    x:
      - {handle: ARISEtv, platform: x, kind: news}
  ke:
    confirmed_by: ""
    x:
      - {handle: KenyaNews, platform: x, kind: news}
"""


def test_the_shipped_hubs_file_is_a_draft_so_no_handle_classifies_and_only_the_news_platform_does():
    assert pack_order.outlet_keys() == []


def test_a_confirmed_market_contributes_its_news_and_sport_entries_by_normalised_key(tmp_path):
    assert pack_order.outlet_keys(write(tmp_path, CONFIRMED)) == [
        "instagram:somedesk", "x:iol", "x:soccer_laduma"]


def test_a_draft_file_contributes_nothing_even_when_a_market_names_who_confirmed_it(tmp_path):
    assert pack_order.outlet_keys(write(tmp_path, CONFIRMED.replace("status: confirmed", "status: draft"))) == []


@pytest.mark.parametrize("text", ["", "markets: 3", "status: confirmed\nmarkets: [1, 2]", "status: [", "status: confirmed"])
def test_a_file_that_cannot_be_read_as_a_registry_contributes_nothing(tmp_path, text):
    assert pack_order.outlet_keys(write(tmp_path, text)) == []


def test_a_missing_file_contributes_nothing(tmp_path):
    assert pack_order.outlet_keys(tmp_path / "absent.yaml") == []


def test_the_module_keeps_the_section_in_one_place():
    for name in ("OUTLET_CAP", "FLOORS", "outlet_keys", "read_stages", "hold_detail", "hold_text"):
        assert hasattr(pack_order, name)
