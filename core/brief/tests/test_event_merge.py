"""One event, one Today card, in shadow (core/brief/job.py _merge_events, RELEASE-E-PLAN item 8h and the W8 header:
every W8 item records and gates nothing until E3). On 12 October 2026 all 4 ZA cards were the MTN8 final and 4 of NG's
7 were one Liverpool and Manchester City match.

After the explanation gate (G10), the job works out which published cards of one market and run describe the same
event (core/brief/same_event.py) and records it in the run's counts under event_merge_shadow: the kept card (the
higher ranked, 8h), each card it would take and why. Today's cards, holds and order do not change, and nothing reads
the record. Switching to a live merge is an E3 line for Albert after the shadow data."""

import json
from datetime import date
from pathlib import Path

import pytest

from core.brief import job
from core.brief.payload import build_market_payload
from core.brief.tests.test_brief_job import (FakeConfirm, FakeModel, add_item, add_post, all_cards, brief,
                                             held_items, payload, rows, world)
from core.brief.tests.test_brief_job import busy_waits, market_scope_is_valid_by_default  # noqa: F401  (autouse)
from core.detect.tests import duck
from core.trust.gate import Decision

REPLAY = json.loads((Path(__file__).parent / "fixtures" / "same_event_replay.json").read_text(encoding="utf-8"))

MTN8 = ("Local creators are posting clips on 10 Oct, likely because Mamelodi Sundowns beat Orlando Pirates in the MTN8 "
        "final.")
MTN8_2 = ("Local creators are posting reactions, likely because Mamelodi Sundowns claimed the MTN8 final over Orlando "
          "Pirates on 10 Oct.")
DERBY = ("Local creators are posting clips, likely because Kaizer Chiefs drew with Golden Arrows in the Betway league on "
         "10 Oct.")


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
    return [c["item_id"] for c in all_cards(payload(r, "ZA"))]


def shadow(r):
    return r.chain.finished[0]["counts"]["event_merge_shadow"]


def fourth(con):
    """A fourth post of za_b's own (add_item makes three)."""
    add_post(con, "ZA", "za_b", "za_b_p4", "za_b_c4", "instagram", geo="ZA")


def two_items(con, *, shared=3):
    """za_a ranks first by worth; za_b has more creators in its 3 days, so Today shows za_b first (e-order-breadth);
    they share `shared` of za_a's posts. za_b has 4 posts of its own, so the shared posts are under half of its posts
    and it is judged on its own rather than folded before judging (job._fold_plan)."""
    add_item(con, "ZA", "za_a", 0.97, label="mtn8 final", creators3=4, posts3=12)
    add_item(con, "ZA", "za_b", 0.96, label="sundowns pirates", creators3=9, posts3=15)
    fourth(con)
    link(con, "za_b", [f"za_a_p{k}" for k in range(1, shared + 1)])


def test_shadow_records_the_pair_and_publication_holds_the_undated_event_fragment():
    con = world(n=0)
    two_items(con)
    r = brief(con, model=EventModel({"mtn8 final": MTN8, "sundowns pirates": MTN8_2}), confirm=FakeConfirm())
    assert za_cards(r) == ["za_b"]
    assert [c["also"] for c in all_cards(payload(r, "ZA"))] == [[]]
    assert payload(r, "ZA")["held_back"]["count"] == 1
    held = payload(r, "ZA")["held_back"]["items"][0]
    assert held["item_id"] == "za_a"
    assert held["event_fragment"]["reference_item_id"] == "za_b"
    assert held["event_fragment"]["missing"] == ["event_date"]
    assert shadow(r)["ZA"] == {"judged": 2, "merges": [
        {"kept": "za_b", "item_id": "za_a", "shared_posts": 3, "shared_names": 3, "date": ["10-10"]}], "near": []}
    assert shadow(r)["NG"] == shadow(r)["KE"] == {"judged": 0, "merges": [], "near": []}
    assert r.chain.finished[0]["counts"]["merged"] == []


def test_the_shadow_changes_nothing_a_reader_sees(monkeypatch):
    def run_once():
        con = world(n=0)
        two_items(con)
        r = brief(con, model=EventModel({"mtn8 final": MTN8, "sundowns pirates": MTN8_2}), confirm=FakeConfirm())
        counts = dict(r.chain.finished[0]["counts"])
        counts.pop("event_merge_shadow")
        return rows(r), counts

    with_shadow = run_once()
    monkeypatch.setattr(job, "_merge_events", lambda *a, **k: {"judged": 0, "merges": [], "near": []})
    assert run_once() == with_shadow


def test_the_kept_card_is_the_one_today_shows_first_when_breadth_and_worth_disagree():
    # Accounts are tied, so posts decide Today's order (core/brief/payload.py _today_order): za_b leads on screen
    # with the lower worth, and the shadow keeps the card Today shows first, not the worth leader.
    con = world(n=0)
    add_item(con, "ZA", "za_a", 0.97, label="mtn8 final", creators3=6, posts3=10)
    add_item(con, "ZA", "za_b", 0.96, label="sundowns pirates", creators3=6, posts3=18)
    fourth(con)
    link(con, "za_b", [f"za_a_p{k}" for k in range(1, 4)])
    r = brief(con, model=EventModel({"mtn8 final": MTN8, "sundowns pirates": MTN8_2}), confirm=FakeConfirm())
    assert za_cards(r) == ["za_b"]
    assert payload(r, "ZA")["held_back"]["items"][0]["item_id"] == "za_a"
    assert [(m["kept"], m["item_id"]) for m in shadow(r)["ZA"]["merges"]] == [("za_b", "za_a")]
    assert shadow(r)["ZA"]["merges"][0]["kept"] == za_cards(r)[0]

def test_two_cards_of_different_events_record_nothing():
    con = world(n=0)
    two_items(con)
    r = brief(con, model=EventModel({"mtn8 final": MTN8, "sundowns pirates": DERBY}), confirm=FakeConfirm())
    assert za_cards(r) == ["za_b", "za_a"]
    assert shadow(r)["ZA"]["merges"] == []


def test_cards_of_one_event_that_share_fewer_than_three_posts_record_nothing():
    con = world(n=0)
    two_items(con, shared=2)
    r = brief(con, model=EventModel({"mtn8 final": MTN8, "sundowns pirates": MTN8_2}), confirm=FakeConfirm())
    assert shadow(r)["ZA"]["merges"] == []


def test_member_posts_count_every_post_of_the_post_set_not_only_the_pack():
    # Three shared posts by one creator: a pack keeps 2 per creator, the post set keeps all 3.
    con = world(n=0)
    add_item(con, "ZA", "za_a", 0.97, label="mtn8 final")
    add_item(con, "ZA", "za_b", 0.96, label="sundowns pirates")
    fourth(con)
    for k in (1, 2, 3):
        add_post(con, "ZA", "za_a", f"solo_p{k}", "solo", "tiktok", geo="ZA")
        link(con, "za_b", [f"solo_p{k}"])
    # add_post loads the creator with each post; one creator row, as the table holds it.
    con.execute("CREATE OR REPLACE TABLE core.creators AS SELECT DISTINCT * FROM core.creators")
    r = brief(con, model=EventModel({"mtn8 final": MTN8, "sundowns pirates": MTN8_2}), confirm=FakeConfirm())
    assert [m["shared_posts"] for m in shadow(r)["ZA"]["merges"]] == [3]


def test_a_card_held_at_g10_takes_no_part():
    con = world(n=0)
    two_items(con)
    r = brief(con, model=EventModel({"mtn8 final": MTN8, "sundowns pirates": MTN8_2}, fail={"mtn8 final"}),
              confirm=FakeConfirm())
    assert za_cards(r) == ["za_b"]
    assert list(held_items(r, "ZA")) == ["za_a"]
    assert shadow(r)["ZA"] == {"judged": 1, "merges": [], "near": []}


def test_one_event_without_a_date_on_one_side_is_recorded_as_a_near_pair():
    con = world(n=0)
    two_items(con)
    undated = MTN8_2.replace(" on 10 Oct", "")
    r = brief(con, model=EventModel({"mtn8 final": MTN8, "sundowns pirates": undated}), confirm=FakeConfirm())
    assert za_cards(r) == ["za_b", "za_a"]
    assert shadow(r)["ZA"] == {"judged": 2, "merges": [], "near": [
        {"a": "za_b", "b": "za_a", "shared_posts": 3, "shared_names": 3, "dated": 1}]}


def test_a_shadow_that_raises_is_logged_and_never_fails_the_brief(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("shadow broke")

    model = {"mtn8 final": MTN8, "sundowns pirates": MTN8_2}
    con = world(n=0)
    two_items(con)
    plain = brief(con, model=EventModel(model), confirm=FakeConfirm())
    monkeypatch.setattr(job, "_merge_events", boom)
    con = world(n=0)
    two_items(con)
    r = brief(con, model=EventModel(model), confirm=FakeConfirm())
    assert r.chain.finished[0]["status"] == "ok"
    assert shadow(r) == {m: {"error": "RuntimeError"} for m in ("ZA", "NG", "KE")}
    assert rows(r) == rows(plain)
    assert "event_merge_shadow_failed RuntimeError" in capsys.readouterr().err


def test_the_counts_size_bound_is_read_without_the_shadow(monkeypatch):
    from core.brief import title_purity

    seen = []

    def wire_bytes(counts):
        seen.append(set(counts))
        return 0

    monkeypatch.setattr(title_purity, "wire_bytes", wire_bytes)
    monkeypatch.setattr(title_purity, "project_receipts", lambda tasks, results: ([{"item_id": "x"}], 0))
    con = world(n=0)
    two_items(con)
    r = brief(con, model=EventModel({"mtn8 final": MTN8, "sundowns pirates": MTN8_2}), confirm=FakeConfirm())
    assert seen and all("event_merge_shadow" not in keys for keys in seen)
    assert shadow(r)["ZA"]["merges"]


def test_the_data_issue_path_records_that_the_shadow_did_not_run():
    from core.brief.tests.test_brief_job import LATE, FakeChain

    r = brief(world(n=1), chain=FakeChain(raises="upstream"), clock=lambda: LATE)
    assert r.chain.finished[0]["status"] == "ok"
    assert shadow(r) == {m: {"not_run": "data_issue"} for m in ("ZA", "NG", "KE")}


def test_a_failed_run_records_that_the_shadow_did_not_run(monkeypatch):
    from core.brief.tests.test_brief_job import FakeChain

    def broken(*a, **k):
        raise RuntimeError("brief broke")

    monkeypatch.setattr(job, "_brief", broken)
    chain = FakeChain()
    with pytest.raises(RuntimeError):
        brief(world(n=1), chain=chain)
    assert chain.finished[0]["status"] == "failed"
    assert chain.finished[0]["counts"]["event_merge_shadow"] == {m: {"not_run": "run_failed"}
                                                                  for m in ("ZA", "NG", "KE")}


# _merge_events on its own, on payload items


def item(item_id, explanation, creators3=5, *, worth=0.5, publish=True, where=None):
    where = where or ("today" if publish else "held_back")
    return {"item_id": item_id, "title": f"#{item_id}", "worth_raw": worth, "explanation": explanation,
            "numbers": [{"value": creators3, "unit": "creators in 3 days"}, {"value": 10, "unit": "posts in 3 days"}],
            "also": [], "decision": Decision(publish, where, None, None, None, False)}


class Refused(set):
    pass


@pytest.fixture
def refusals(monkeypatch):
    refused = Refused()
    refused.asked = []

    def admits(it, market, d):
        refused.asked.append(it["item_id"])
        return it["item_id"] not in refused

    monkeypatch.setattr(job, "_today_admits", admits)
    return refused


POSTS = {"p1", "p2", "p3"}


def run_merge(items, posts=None):
    return job._merge_events("ZA", date(2026, 10, 12), items, posts or {i["item_id"]: POSTS for i in items})


def pairs(result):
    return [(m["kept"], m["item_id"]) for m in result["merges"]]


def test_the_higher_ranked_card_is_kept_whatever_the_counts(refusals):
    items = [item("low", MTN8, 2, worth=0.1), item("high", MTN8_2, 30, worth=0.9)]
    assert pairs(run_merge(items)) == [("high", "low")]


def test_nothing_on_the_items_changes(refusals):
    items = [item("a", MTN8, worth=0.9), item("b", MTN8_2, worth=0.1)]
    before = json.dumps(items, default=repr, sort_keys=True)
    assert pairs(run_merge(items)) == [("a", "b")]
    assert json.dumps(items, default=repr, sort_keys=True) == before


def test_the_payload_is_the_same_with_and_without_the_shadow(refusals):
    def items():
        out = [item("a", MTN8, worth=0.9), item("b", MTN8_2, worth=0.1)]
        for i in out:
            i.update(kind="hashtag", state="emerging", evidence=[], claims=[], explanation_claim_ids=[],
                     sparkline=None, specificity=None, critic={"ruled_out": True})
        return out

    plain = build_market_payload("ZA", "2026-10-12", items(), moments=[], boards=[], banners=[])
    shadowed = items()
    assert pairs(run_merge(shadowed)) == [("a", "b")]
    assert build_market_payload("ZA", "2026-10-12", shadowed, moments=[], boards=[], banners=[]) == plain


def test_a_card_joins_a_kept_card_only_if_it_is_one_event_with_every_card_already_taken(refusals):
    # REVIEW-opus-d-event-merge I1: a roundup card naming two matches must not take both of them.
    roundup = ("Creators posted on 11 Oct, likely because Manchester City beat Liverpool and Chelsea beat Bournemouth "
               "in the Premier League.")
    city = "Creators posted, likely because Manchester City beat Liverpool in the Premier League on 11 Oct."
    chelsea = "Creators posted, likely because Chelsea beat Bournemouth in the Premier League on 11 Oct."
    items = [item("roundup", roundup, worth=0.9), item("city", city, worth=0.5), item("chelsea", chelsea, worth=0.1)]
    assert pairs(run_merge(items)) == [("roundup", "city")]


def test_a_card_joins_the_first_kept_card_it_matches(refusals):
    one = "Fans posted on 10 Oct, likely because Mamelodi Sundowns, Orlando Pirates and Kaizer Chiefs met."
    two = "Fans posted on 10 Oct, likely because Golden Arrows, Chippa United and Richards Bay met."
    both = ("Fans posted on 10 Oct, likely because Mamelodi Sundowns, Orlando Pirates, Kaizer Chiefs, Golden Arrows, "
            "Chippa United and Richards Bay met.")
    items = [item("one", one, worth=0.9), item("two", two, worth=0.8), item("both", both, worth=0.7)]
    assert pairs(run_merge(items)) == [("one", "both")]


def test_a_held_item_or_one_routed_to_today_but_not_published_takes_no_part(refusals):
    items = [item("held", MTN8, publish=False, worth=0.9), item("unpublished", MTN8, publish=False, where="today",
                                                                  worth=0.8), item("card", MTN8_2, worth=0.1)]
    assert run_merge(items) == {"judged": 1, "merges": [], "near": []}


def test_a_card_today_would_not_admit_takes_no_part(refusals):
    refusals.add("strong")
    assert run_merge([item("strong", MTN8, worth=0.9), item("weak", MTN8_2, worth=0.1)])["merges"] == []
    refusals.clear()
    refusals.add("weak")
    assert run_merge([item("strong", MTN8, worth=0.9), item("weak", MTN8_2, worth=0.1)])["merges"] == []


def test_only_the_cards_the_payload_would_show_are_judged(refusals):
    items = [item(f"c{k}", MTN8, worth=1 - k / 100) for k in range(12)]
    result = run_merge(items)
    assert result["judged"] == 10
    assert sorted(refusals.asked) == sorted(f"c{k}" for k in range(10))
    assert [m["item_id"] for m in result["merges"]] == [f"c{k}" for k in range(1, 10)]


def test_judged_counts_only_the_cards_today_would_admit(refusals):
    refusals.add("refused")
    items = [item("a", MTN8, worth=0.9), item("refused", MTN8, worth=0.5), item("b", MTN8_2, worth=0.1)]
    result = run_merge(items)
    assert result["judged"] == 2 and pairs(result) == [("a", "b")]


def test_a_card_taken_into_a_kept_card_never_becomes_a_kept_card(refusals):
    one = "Fans posted on 10 Oct, likely because Mamelodi Sundowns, Orlando Pirates and Kaizer Chiefs met."
    both = ("Fans posted on 10 Oct, likely because Mamelodi Sundowns, Orlando Pirates, Kaizer Chiefs, Golden Arrows, "
            "Chippa United and Richards Bay met.")
    two = "Fans posted on 10 Oct, likely because Golden Arrows, Chippa United and Richards Bay met."
    items = [item("one", one, worth=0.9), item("both", both, worth=0.8), item("two", two, worth=0.7)]
    assert pairs(run_merge(items)) == [("one", "both")]


def test_near_pairs_are_every_pair_that_fails_only_on_dates(refusals):
    undated = MTN8_2.replace(" on 10 Oct", "")
    other_day = MTN8_2.replace("10 Oct", "3 Oct")
    women = MTN8.replace("Orlando Pirates", "Orlando Pirates Women")
    items = [item("a", MTN8, worth=0.9), item("b", undated, worth=0.8), item("c", other_day, worth=0.7),
             item("d", women, worth=0.6), item("e", MTN8_2, worth=0.5)]
    result = run_merge(items)
    assert pairs(result) == [("a", "e")]
    assert [(n["a"], n["b"], n["dated"]) for n in result["near"]] == [
        ("a", "b", 1), ("a", "c", 2), ("b", "c", 1), ("b", "e", 1), ("c", "e", 2)]
    assert all(set(n) == {"a", "b", "shared_posts", "shared_names", "dated"} for n in result["near"])


def test_member_posts_are_read_per_item(refusals):
    items = [item("a", MTN8, worth=0.9), item("b", MTN8_2, worth=0.1)]
    assert run_merge(items, {"a": {"p1", "p2", "p3"}, "b": {"p1", "p2", "x"}})["merges"] == []


# Replay of every stored brief with 2 or more explained cards (fixtures/same_event_replay.json). Every card in it was
# a published card, so each is admitted here.

BRIEFS = {("2026-09-29", "KE", "brief-20260929-d26a26c2fa00"): ["59090e1b52", "4a433cfd13"],
          ("2026-09-29", "NG", "brief-20260929-d26a26c2fa00"): ["65d987c234", "10c0dd5f5c"],
          ("2026-09-29", "NG", "brief-20260929-d85aced54f9c"): ["8aad723fe3", "10c0dd5f5c", "18edce810b"],
          ("2026-10-05", "NG", "brief-20261005-8e019e9c7270"): ["306b75a27e", "1df3f88973"],
          ("2026-10-07", "KE", "brief-20261007-665e5edab6ab"): ["1df3f88973", "11598e2bc3"],
          ("2026-10-10", "ZA", "brief-20261010-51fd5f698437"): ["e9fcecfee6", "59090e1b52"],
          ("2026-10-12", "NG", "brief-20261012-a15d3f51d4e1"): ["ad7f67c05b", "59090e1b52", "9cfaabf463", "6a9690baaa",
                                                                 "b1001f23b8", "3791be7f80", "04a0aad7b9"],
          ("2026-10-12", "ZA", "brief-20261012-a15d3f51d4e1"): ["e980768970", "2e884d7ea2", "4652356782",
                                                                 "81bdee2c0e"]}
# Pinned by hand from reading the explanations. NG 12 Oct: the Liverpool and Manchester City match is on 4 cards, all
# naming 11 Oct, kept on the card Today shows first of them (e-order-breadth: 3791be7f80, 33 accounts posting). ZA 12
# Oct: the 4 MTN8 cards are one event, but only the stored first, with 2 accounts and so last in Today's order, names
# its date, so the rule, which needs both cards to name the same date, records none.
EXPECTED = {("2026-10-12", "NG"): [("3791be7f80", "ad7f67c05b"), ("3791be7f80", "9cfaabf463"),
                                   ("3791be7f80", "59090e1b52")]}
# Near pairs, every one also pinned by hand: one event on both cards, with a date on one side only, each pair in
# Today's order. ZA 12 Oct: the 4 MTN8 cards, all 6 pairs. NG 12 Oct: the Manchester United and Carrick story, whose
# #manchesterunited card names no date.
NEAR = {("2026-10-12", "ZA"): [("2e884d7ea2", "4652356782"), ("2e884d7ea2", "81bdee2c0e"),
                               ("2e884d7ea2", "e980768970"), ("4652356782", "81bdee2c0e"),
                               ("4652356782", "e980768970"), ("81bdee2c0e", "e980768970")],
        ("2026-10-12", "NG"): [("04a0aad7b9", "6a9690baaa")]}


def replay(brief_):
    items = [item(c["item_id"], c["explanation"], c["creators3"], worth=1 - c["rank"] / 100) for c in brief_["cards"]]
    return run_merge(items, {c["item_id"]: set(c["member_posts"]) for c in brief_["cards"]})


def short(result):
    return [(m["kept"][:10], m["item_id"][:10]) for m in result["merges"]]


def test_the_replay_fixture_holds_every_brief_and_card():
    assert {(b["brief_date"], b["market"], b["run_id"]): [c["item_id"][:10] for c in b["cards"]]
            for b in REPLAY["briefs"]} == BRIEFS


@pytest.mark.parametrize("key", sorted(BRIEFS))
def test_replay_records_only_cards_of_one_event(refusals, key):
    brief_ = next(b for b in REPLAY["briefs"] if (b["brief_date"], b["market"], b["run_id"]) == key)
    result = replay(brief_)
    assert short(result) == EXPECTED.get(key[:2], [])
    assert [(n["a"][:10], n["b"][:10]) for n in result["near"]] == NEAR.get(key[:2], [])


def test_replay_12_october_za_shows_the_mtn8_cards_as_near_pairs(refusals):
    za = next(b for b in REPLAY["briefs"] if (b["brief_date"], b["market"]) == ("2026-10-12", "ZA"))
    result = replay(za)
    assert result["merges"] == []
    assert {n["a"] for n in result["near"]} | {n["b"] for n in result["near"]} == {c["item_id"] for c in za["cards"]}
    assert len(result["near"]) == 6
    assert all(n["shared_posts"] >= 3 and n["shared_names"] >= 3 for n in result["near"])
    # Only the MTN8 Cup Final card names its date, and it is last in Today's order: its 3 pairs have one dated side,
    # the other 3 have none.
    assert [n["dated"] for n in result["near"]] == [0, 0, 1, 0, 1, 1]


@pytest.mark.parametrize("key,apart", [
    (("2026-10-07", "KE"), ("1df3f88973", "11598e2bc3")),  # Dangote and Sifuna
    (("2026-10-12", "NG"), ("ad7f67c05b", "b1001f23b8")),  # Liverpool and City, Chelsea and Bournemouth
    (("2026-10-12", "NG"), ("ad7f67c05b", "6a9690baaa")),  # Liverpool and City, Manchester United
    (("2026-10-12", "NG"), ("3791be7f80", "04a0aad7b9")),  # the same two stories as topics
])
def test_replay_keeps_the_named_pairs_apart(refusals, key, apart):
    brief_ = next(b for b in REPLAY["briefs"] if (b["brief_date"], b["market"]) == key)
    ids = {c["item_id"][:10] for c in brief_["cards"]}
    assert set(apart) <= ids
    merged = {frozenset(p) for p in short(replay(brief_))}
    groups = {}
    for kept, taken in short(replay(brief_)):
        groups.setdefault(kept, {kept}).add(taken)
    assert frozenset(apart) not in merged
    assert not any(set(apart) <= g for g in groups.values())
