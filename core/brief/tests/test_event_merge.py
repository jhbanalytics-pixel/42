"""One event, one Today card (core/brief/job.py _merge_events). On 12 October 2026 all 4 ZA cards were the MTN8 final
and 4 of NG's 7 were one Liverpool and Manchester City match: _merge only merges a card that adds fewer than 3 posts
not already on a higher card, and nothing compared what the cards said.

After the explanation gate (G10), the published cards of one market and run that describe the same event
(core/brief/same_event.py) become one card: the one with the most creators in its 3 days keeps its place and lists the
others under also. The merge only merges. A held card neither absorbs nor is absorbed, a card Today would not admit
takes no part, and nothing held is ever shown."""

import json
import re
from datetime import date
from pathlib import Path

import pytest

from core.brief import job
from core.brief.tests.test_brief_job import (FakeConfirm, FakeModel, add_item, all_cards, brief, draft, held_items,
                                             payload, world)
from core.brief.tests.test_brief_job import busy_waits, market_scope_is_valid_by_default  # noqa: F401  (autouse)
from core.detect.tests import duck
from core.trust.gate import Decision

REPLAY = json.loads((Path(__file__).parent / "fixtures" / "same_event_replay.json").read_text(encoding="utf-8"))

MTN8 = "Local creators are posting clips, likely because Mamelodi Sundowns beat Orlando Pirates in the MTN8 final."
MTN8_2 = "Local creators are posting reactions, likely because Mamelodi Sundowns claimed the MTN8 over Orlando Pirates."
DERBY = "Local creators are posting clips, likely because Kaizer Chiefs drew with Golden Arrows in the Betway league."


def link(con, item_id, post_ids):
    duck.load(con, "core.post_items", [{"post_id": p, "item_id": item_id, "via": "hashtag"} for p in post_ids])


class EventModel(FakeModel):
    """FakeModel whose writer gives each item the explanation its label names; a label in fail gets no draft, so its
    explanation fails its checks (G10)."""

    def __init__(self, by_label, fail=()):
        super().__init__()
        self.by_label, self.fail = by_label, set(fail)

    def complete_json(self, *, system, user, schema, model, max_tokens):
        out, usage = super().complete_json(system=system, user=user, schema=schema, model=model,
                                           max_tokens=max_tokens)
        if "verdict" in schema["properties"] or "ruled_out" in schema["properties"]:
            return out, usage
        label = next(lbl for lbl in self.by_label if lbl in user)
        if label in self.fail:
            raise RuntimeError("writer failed")
        return {**out, "explanation": self.by_label[label]}, usage


def za_cards(r):
    return {c["item_id"]: c for c in all_cards(payload(r, "ZA"))}


def two_items(con, *, a_creators=4, b_creators=9, shared=3):
    """za_a ranks first by worth; za_b has more creators in its 3 days; they share `shared` of za_a's posts."""
    add_item(con, "ZA", "za_a", 0.97, label="mtn8 final", creators3=a_creators, posts3=12)
    add_item(con, "ZA", "za_b", 0.96, label="sundowns pirates", creators3=b_creators, posts3=15)
    link(con, "za_b", [f"za_a_p{k}" for k in range(1, shared + 1)])


def test_two_cards_of_one_event_become_the_card_with_the_most_creators():
    con = world(n=0)
    two_items(con)
    r = brief(con, model=EventModel({"mtn8 final": MTN8, "sundowns pirates": MTN8_2}), confirm=FakeConfirm())
    za = za_cards(r)
    assert list(za) == ["za_b"]
    assert za["za_b"]["rank"] == 1
    assert za["za_b"]["also"] == [{"item_id": "za_a", "title": "mtn8 final"}]
    assert payload(r, "ZA")["held_back"]["count"] == 0
    assert {"market": "ZA", "into": "za_b", "item_id": "za_a", "by": "same_event"} in r.chain.finished[0]["counts"][
        "merged"]


def test_two_cards_of_different_events_stay_two_cards():
    con = world(n=0)
    two_items(con)
    r = brief(con, model=EventModel({"mtn8 final": MTN8, "sundowns pirates": DERBY}), confirm=FakeConfirm())
    za = za_cards(r)
    assert sorted(za) == ["za_a", "za_b"]
    assert za["za_a"]["also"] == [] and za["za_b"]["also"] == []
    assert r.chain.finished[0]["counts"]["merged"] == []


def test_cards_of_one_event_that_share_fewer_than_three_posts_stay_two_cards():
    con = world(n=0)
    two_items(con, shared=2)
    r = brief(con, model=EventModel({"mtn8 final": MTN8, "sundowns pirates": MTN8_2}), confirm=FakeConfirm())
    assert sorted(za_cards(r)) == ["za_a", "za_b"]


def test_a_card_held_at_g10_absorbs_nothing_and_the_other_card_stays_a_card():
    con = world(n=0)
    two_items(con)
    r = brief(con, model=EventModel({"mtn8 final": MTN8, "sundowns pirates": MTN8_2}, fail={"sundowns pirates"}),
              confirm=FakeConfirm())
    za = za_cards(r)
    assert list(za) == ["za_a"] and za["za_a"]["also"] == []
    held = held_items(r, "ZA")
    assert list(held) == ["za_b"] and held["za_b"]["reason"] == "explanation_failed"
    assert "also" not in held["za_b"]
    assert r.chain.finished[0]["counts"]["merged"] == []


def test_a_weaker_card_held_at_g10_stays_its_own_held_item():
    con = world(n=0)
    two_items(con)
    r = brief(con, model=EventModel({"mtn8 final": MTN8, "sundowns pirates": MTN8_2}, fail={"mtn8 final"}),
              confirm=FakeConfirm())
    za = za_cards(r)
    assert list(za) == ["za_b"] and za["za_b"]["also"] == []
    assert list(held_items(r, "ZA")) == ["za_a"]


# _merge_events on its own, on payload items


def item(item_id, explanation, creators3, *, worth=0.5, publish=True, also=(), posts3=10, where=None):
    numbers = [] if creators3 is None else [{"value": creators3, "unit": "creators in 3 days"},
                                            {"value": posts3, "unit": "posts in 3 days"}]
    where = where or ("today" if publish else "held_back")
    return {"item_id": item_id, "title": f"#{item_id}", "worth_raw": worth, "explanation": explanation,
            "numbers": numbers, "also": list(also), "decision": Decision(publish, where, None, None, None, False)}


@pytest.fixture
def admitted(monkeypatch):
    """Every item is one Today would admit, unless a test names it in the set this returns."""
    refused = set()
    monkeypatch.setattr(job, "_today_admits", lambda it, market, d: it["item_id"] not in refused)
    return refused


POSTS = {"p1", "p2", "p3"}


def run_merge(items, posts=None):
    merged = []
    job._merge_events("ZA", date(2026, 10, 12), items, posts or {i["item_id"]: POSTS for i in items}, merged)
    return merged


def shown(items):
    return [i["item_id"] for i in items if not i.get("merged_into")]


def test_the_strongest_is_read_from_creators_then_posts_then_rank(admitted):
    items = [item("first", MTN8, 3, worth=0.9), item("broad", MTN8_2, 8, worth=0.2), item("mid", MTN8, 5, worth=0.5)]
    merged = run_merge(items)
    assert shown(items) == ["broad"]
    assert items[1]["also"] == [{"item_id": "mid", "title": "#mid"}, {"item_id": "first", "title": "#first"}]
    assert [m["item_id"] for m in merged] == ["mid", "first"]


def test_creators_outrank_posts_when_they_disagree(admitted):
    items = [item("posts", MTN8, 4, posts3=40, worth=0.9), item("creators", MTN8_2, 6, posts3=8, worth=0.1)]
    run_merge(items)
    assert shown(items) == ["creators"]


def test_a_count_that_is_not_a_number_reads_as_no_count(admitted):
    items = [item("flag", MTN8, 5, worth=0.9), item("real", MTN8_2, 0, posts3=0, worth=0.1)]
    items[0]["numbers"] = [{"value": True, "unit": "creators in 3 days"}, {"value": True, "unit": "posts in 3 days"}]
    run_merge(items)
    assert shown(items) == ["real"]


def test_an_item_routed_to_today_but_not_published_takes_no_part(admitted):
    items = [item("unpublished", MTN8, 9, publish=False, where="today"), item("card", MTN8_2, 2)]
    assert run_merge(items) == [] and shown(items) == ["unpublished", "card"]


def test_a_card_without_counts_is_weakest_and_ties_go_to_the_higher_rank(admitted):
    items = [item("low", MTN8, None, worth=0.9), item("top", MTN8_2, 5, worth=0.8), item("twin", MTN8, 5, worth=0.1)]
    run_merge(items)
    assert shown(items) == ["top"]


def test_an_absorbed_card_brings_its_own_also_with_it(admitted):
    items = [item("kept", MTN8, 9), item("other", MTN8_2, 2, also=[{"item_id": "tag", "title": "#tag"}])]
    run_merge(items)
    assert items[0]["also"] == [{"item_id": "other", "title": "#other"}, {"item_id": "tag", "title": "#tag"}]


def test_an_absorbed_card_never_absorbs_a_third_one(admitted):
    # bridge matches kept and far; kept and far share only two names, so far stays a card of its own.
    kept = "Local fans posted, likely because Mamelodi Sundowns beat Orlando Pirates in the MTN8 final."
    bridge = ("Local fans posted, likely because Mamelodi Sundowns, Orlando Pirates, the MTN8, Moses Mabhida and "
              "Durban drew crowds.")
    far = "Local fans posted, likely because the MTN8 drew crowds to Moses Mabhida in Durban."
    items = [item("kept", kept, 9), item("bridge", bridge, 5), item("far", far, 2)]
    run_merge(items)
    assert shown(items) == ["kept", "far"]


def test_a_held_item_takes_no_part(admitted):
    items = [item("held", MTN8, 9, publish=False), item("card", MTN8_2, 2)]
    merged = run_merge(items)
    assert shown(items) == ["held", "card"] and merged == []
    assert items[0]["also"] == [] and items[1]["also"] == []


def test_a_card_today_would_not_admit_takes_no_part(admitted):
    admitted.add("strong")
    items = [item("strong", MTN8, 9), item("weak", MTN8_2, 2)]
    assert run_merge(items) == [] and shown(items) == ["strong", "weak"]
    admitted.clear()
    admitted.add("weak")
    items = [item("strong", MTN8, 9), item("weak", MTN8_2, 2)]
    assert run_merge(items) == [] and shown(items) == ["strong", "weak"]


def test_member_posts_are_read_per_item(admitted):
    items = [item("a", MTN8, 9), item("b", MTN8_2, 2)]
    assert run_merge(items, {"a": {"p1", "p2", "p3"}, "b": {"p1", "p2", "x"}}) == []


def test_the_merge_never_shows_an_item_the_gate_held_and_never_holds_one_it_published(admitted):
    items = [item("a", MTN8, 9), item("b", MTN8_2, 2), item("c", MTN8, 4, publish=False)]
    before = [(i["item_id"], i["decision"]) for i in items]
    run_merge(items)
    assert [(i["item_id"], i["decision"]) for i in items] == before


def test_the_payload_shows_a_merged_item_nowhere_but_its_kept_cards_also(admitted):
    from core.brief.payload import build_market_payload

    a, b = item("a", MTN8, 9), item("b", MTN8_2, 2)
    for i in (a, b):
        i.update(kind="hashtag", state="emerging", evidence=[], claims=[], explanation_claim_ids=[],
                 sparkline=None, specificity=None, critic={"ruled_out": True})
    run_merge([a, b])
    p = build_market_payload("ZA", "2026-10-12", [a, b], moments=[], boards=[], banners=[])
    assert [c["item_id"] for c in p["cards"] + p["more"]] == ["a"]
    assert p["cards"][0]["also"] == [{"item_id": "b", "title": "#b"}]
    assert p["held_back"]["count"] == 0
    assert [c["item_id"] for c in p["critic"]] == ["a", "b"]


# Replay of every stored brief with 2 or more explained cards (fixtures/same_event_replay.json). Every card in it was
# a published card, so each is admitted here; the groups are pinned by hand from reading the explanations.

EXPECTED = {
    ("2026-10-12", "ZA"): {"2e884d7ea2": ["4652356782", "81bdee2c0e", "e980768970"]},
    ("2026-10-12", "NG"): {"3791be7f80": ["ad7f67c05b", "9cfaabf463", "59090e1b52"],
                           "04a0aad7b9": ["6a9690baaa"]},
}


def replay_items(brief_):
    return [item(c["item_id"], c["explanation"], c["creators3"], worth=1 - c["rank"] / 100,
                 also=c["also"]) | {"title": c["title"], "numbers": [
                     {"value": c["creators3"], "unit": "creators in 3 days"},
                     {"value": c["posts3"], "unit": "posts in 3 days"}]} for c in brief_["cards"]]


@pytest.mark.parametrize("brief_", REPLAY["briefs"], ids=lambda b: f"{b['brief_date']}-{b['market']}-{b['run_id']}")
def test_replay_merges_only_cards_of_one_event(admitted, brief_):
    items = replay_items(brief_)
    posts = {c["item_id"]: set(c["member_posts"]) for c in brief_["cards"]}
    merged = run_merge(items, posts)
    groups = {}
    for m in merged:
        groups.setdefault(m["into"][:10], []).append(m["item_id"][:10])
    assert groups == EXPECTED.get((brief_["brief_date"], brief_["market"]), {})


def test_replay_12_october_za_gives_one_mtn8_card(admitted):
    za = next(b for b in REPLAY["briefs"] if (b["brief_date"], b["market"]) == ("2026-10-12", "ZA"))
    items = replay_items(za)
    run_merge(items, {c["item_id"]: set(c["member_posts"]) for c in za["cards"]})
    kept = [i for i in items if not i.get("merged_into")]
    assert len(kept) == 1
    assert next(c for c in za["cards"] if c["item_id"] == kept[0]["item_id"])["title_written"] == "MTN8 final"
    assert [a["title"] for a in kept[0]["also"]] == ["bafanabafana, mofokeng, relebohile", "#sundowns", "#MTN8",
                                                     "#MTN8Final"]


def test_the_replay_fixture_holds_the_pairs_that_must_stay_apart():
    ng = next(b for b in REPLAY["briefs"] if (b["brief_date"], b["market"]) == ("2026-10-12", "NG"))
    titles = {c["item_id"][:10]: c["title_written"] or c["title"] for c in ng["cards"]}
    assert titles["b1001f23b8"] == "Chelsea vs Bournemouth"
    assert re.search("Manchester City", titles["ad7f67c05b"]) and titles["6a9690baaa"] == "#manchesterunited"
