"""A card publishes the same with detect's rival values in its pack."""

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
