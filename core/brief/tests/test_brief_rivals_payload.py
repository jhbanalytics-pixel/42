"""A card publishes the same with detect's rival values in its pack; only the critic audit entry gains the shadow record."""

import copy

from core.brief.tests.test_brief_payload import build, cand, decision, num

RIVAL = {**num(0.5, "share of 7-day posts in the busiest 10 minutes", 7), "rival_field": "burst_share"}
POSTS7 = {**num(20, "posts in 7 days", 8), "rival_field": "posts7"}


def with_rivals(c):
    out = copy.deepcopy(c)
    out["numbers"] = out["numbers"] + [RIVAL, POSTS7]
    return out


def test_rival_numbers_never_reach_a_cards_numbers_or_count_line():
    base, rival = build([cand(1)])["cards"][0], build([with_rivals(cand(1))])["cards"][0]
    assert rival == base
    assert rival["numbers"] == cand(1)["numbers"]


def test_rival_numbers_never_reach_a_held_items_numbers_or_count_line_when_untested_either():
    for untested in (False, True):
        held = decision("held_back", rule="G10")
        base = build([cand(1, untested=untested, decision=held)])["held_back"]["items"][0]
        rival = build([with_rivals(cand(1, untested=untested, decision=held))])["held_back"]["items"][0]
        assert rival == base


def test_an_untested_item_with_only_rival_numbers_after_its_two_counts_keeps_its_count_line():
    c = cand(1, untested=True)
    c["numbers"] = c["numbers"][:1] + [RIVAL]
    assert build([c])["cards"][0]["count_line"] == "31 creators in 3 days"


def test_the_critic_audit_entry_carries_the_shadow_record_and_the_rest_of_the_payload_is_unchanged():
    answer = {"non_cultural_explanation": "a paid campaign", "ruled_out": True, "local_why_now": True, "reason": "r"}
    record = {"found": ["burst"], "not_assessed": ["regime_break"], "inputs": {}, "critic_ruled_out": True,
              "disagreement": True}
    base = build([cand(1, critic=answer)])
    shadow = build([with_rivals(cand(1, critic={**answer, "code_rivals": record}))])
    assert shadow["critic"] == [{"item_id": "it_1", **answer, "code_rivals": record}]
    assert {**shadow, "critic": None} == {**base, "critic": None}


def test_a_whole_job_sends_the_rival_numbers_to_the_model_and_leaves_them_off_the_cards_and_the_gate():
    from core.brief.tests.test_brief_job import all_cards, brief, payload, world

    r = brief(world(n=1))
    assert any("q_burst_share" in c["user"] for c in r.model.calls if not c["support"] and not c["critic"])
    assert any("q_burst_share" in c["user"] for c in r.model.calls if c["critic"])
    cards = [c for m in ("ZA", "NG", "KE") for c in all_cards(payload(r, m))]
    assert cards
    for card in cards:
        assert {n["unit"] for n in card["numbers"]} <= {"creators in 3 days", "posts in 3 days", "times usual"}
        assert not any("rival_field" in n for n in card["numbers"])
    for call in r.ctx.calls:
        assert not any("rival_field" in n for n in call["numbers"])


def test_a_whole_job_publishes_the_same_cards_when_the_code_finds_a_rival_the_critic_ruled_out():
    from core.brief.tests.test_brief_job import MARKETS, all_cards, brief, payload, world

    base_world, rival_world = world(n=1), world(n=1)
    rival_world.execute("UPDATE core.item_state SET moment = 'Heritage Day'")
    base, rival = brief(base_world), brief(rival_world)
    assert len(rival.model.calls) == len(base.model.calls)
    for m in MARKETS:
        a, b = payload(base, m), payload(rival, m)
        assert all_cards(a) and b["cards"] == a["cards"] and b["more"] == a["more"]
        assert b["held_back"] == a["held_back"] and b["status"] == a["status"] and b["headline"] == a["headline"]
        assert [c["item_id"] for c in b["critic"]] == [c["item_id"] for c in a["critic"]]
        for before, after in zip(a["critic"], b["critic"]):
            record, base_record = after.pop("code_rivals"), before.pop("code_rivals")
            assert after == before and base_record["found"] == [] and base_record["disagreement"] is False
            assert record["found"] == ["calendar_moment"] and record["disagreement"] is True
    assert [(c["rule"], c["verdict"]) for c in rival.client.inserted["agent.claim_checks"]] == [
        (c["rule"], c["verdict"]) for c in base.client.inserted["agent.claim_checks"]]


def test_the_run_counts_say_how_many_rival_reads_were_ok_failed_or_missing_a_cutoff():
    from core.brief.tests.test_brief_job import brief, world

    ok = brief(world(n=1)).counts["rival_reads"]
    assert ok["ok"] >= 1 and ok["failed"] == 0 and ok["cutoff_missing"] == 0
    con = world(n=1)
    con.execute("UPDATE agent.runs SET started_at = NULL WHERE stage = 'detect'")
    assert brief(con).counts["rival_reads"]["cutoff_missing"] >= 1
