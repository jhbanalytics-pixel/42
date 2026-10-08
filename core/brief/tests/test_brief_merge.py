"""One card per set of posts (core/brief/job.py): a Today-bound candidate whose posts are nearly all on a higher
card merges into that card before confirm and explain, on DuckDB fixture tables with the fakes of
test_brief_job."""

import json
from types import SimpleNamespace

import pytest

from core.brief import job
from core.brief.tests.test_brief_job import (FakeConfirm, FakeModel, add_item, all_cards, brief, held_items,
                                             payload, world)
from core.detect.tests import duck
from core.detect.tests.fixtures import at, day, rid

OWN_LINE = "12 creators and 20 posts in 3 days, 2.5 times usual"


def link(con, item_id, post_ids):
    """Tag posts already sighted in the market with one more item, as one video with several hashtags is."""
    duck.load(con, "core.post_items", [{"post_id": p, "item_id": item_id, "via": "hashtag"} for p in post_ids])


def cards(r, market="ZA"):
    return {c["item_id"]: c for c in all_cards(payload(r, market))}


def confirmed(r):
    return [c["item_id"] for call in r.confirm.calls for c in call["candidates"]]


def merge_candidate(item_id, worth, posts, evidence=()):
    return {"row": {"item_id": item_id, "title": f"#{item_id}", "worth_raw": worth, "market_scope": "market",
                    "market_posts7": 3, "total_posts7": 3, "market_share7": 1.0},
            "decision": SimpleNamespace(publish=True, where="today"), "posts": set(posts),
            "pack": {"evidence": list(evidence), "numbers": [], "facts": []}}


@pytest.fixture(autouse=True)
def market_scope_is_valid_by_default(monkeypatch):
    def read_market_scope(client, row, d, market, *, core, agent):
        return {"market_scope": "market", "market_posts7": 3, "total_posts7": 3, "market_share7": 1.0}

    monkeypatch.setattr(job, "read_market_scope", read_market_scope)


def prompts(r):
    return "\n".join(c["user"] for c in r.model.calls)


def test_a_card_whose_posts_are_all_on_a_higher_card_merges_into_it_before_confirm_and_explain():
    con = world(n=3)
    add_item(con, "ZA", "za_dup", 0.93, posts=0)
    link(con, "za_dup", ["za1_p1", "za1_p2", "za1_p3"])
    r = brief(con, model=FakeModel(), confirm=FakeConfirm())
    za = cards(r)
    assert list(za) == ["za1", "za2", "za3"]
    assert za["za1"]["also"] == [{"item_id": "za_dup", "title": "#za_dup"}]
    assert za["za1"]["count_line"] == OWN_LINE + " (#za1 only)"
    assert za["za2"]["also"] == [] and za["za2"]["count_line"] == OWN_LINE
    assert "za_dup" not in held_items(r, "ZA")
    assert r.counts["merged"] == [{"market": "ZA", "into": "za1", "item_id": "za_dup"}]
    assert r.chain.finished[0]["counts"]["merged"] == r.counts["merged"]
    assert "za_dup" not in confirmed(r) and "za1" in confirmed(r)
    assert "#za_dup" not in prompts(r) and "#za1" in prompts(r)
    assert r.counts["cards"] == 9


def test_two_cards_with_enough_posts_of_their_own_both_stay():
    con = world(n=2)
    add_item(con, "ZA", "za_share", 0.93)
    link(con, "za_share", ["za1_p1", "za1_p2"])
    r = brief(con)
    za = cards(r)
    assert list(za) == ["za1", "za_share", "za2"]
    assert za["za1"]["also"] == [] and za["za_share"]["also"] == []
    assert r.counts["merged"] == []
    assert "za_share" in confirmed(r) and "#za_share" in prompts(r)


def test_fewer_than_3_posts_of_its_own_merges_and_the_kept_pack_takes_them():
    con = world(n=2)
    add_item(con, "ZA", "za_two", 0.93, posts=2)
    link(con, "za_two", ["za1_p1", "za1_p2", "za1_p3"])
    r = brief(con)
    za = cards(r)
    assert list(za) == ["za1", "za2"]
    assert za["za1"]["also"] == [{"item_id": "za_two", "title": "#za_two"}]
    assert sorted(za["za1"]["evidence_ids"]) == ["za1_p1", "za1_p2", "za1_p3", "za_two_p1", "za_two_p2"]
    assert {n["unit"]: n["value"] for n in za["za1"]["numbers"]} == {
        "creators in 3 days": 12, "posts in 3 days": 20, "times usual": 2.5}


def test_a_merged_item_joins_the_kept_card_it_overlaps_most():
    con = world(n=2)
    add_item(con, "ZA", "za_both", 0.89, posts=0)
    link(con, "za_both", ["za1_p1", "za1_p2", "za2_p1", "za2_p2", "za2_p3"])
    r = brief(con)
    za = cards(r)
    assert za["za2"]["also"] == [{"item_id": "za_both", "title": "#za_both"}]
    assert za["za1"]["also"] == []
    assert r.counts["merged"] == [{"market": "ZA", "into": "za2", "item_id": "za_both"}]


def test_several_merged_items_are_listed_in_rank_order_on_one_card():
    con = world(n=1)
    add_item(con, "ZA", "za_low", 0.5, posts=0)
    add_item(con, "ZA", "za_high", 0.9, posts=0)
    link(con, "za_low", ["za1_p1", "za1_p2", "za1_p3"])
    link(con, "za_high", ["za1_p1", "za1_p2", "za1_p3"])
    r = brief(con)
    assert [a["item_id"] for a in cards(r)["za1"]["also"]] == ["za_high", "za_low"]
    assert [m["item_id"] for m in r.counts["merged"]] == ["za_high", "za_low"]


def test_merging_stays_inside_one_market():
    con = world(n=1)
    add_item(con, "NG", "ng_dup", 0.5, posts=0)
    link(con, "ng_dup", ["za1_p1", "za1_p2", "za1_p3"])
    con.execute("UPDATE core.posts SET geo_market=NULL, geo_confidence=NULL, geo_source=NULL "
                "WHERE post_id IN ('za1_p1', 'za1_p2', 'za1_p3')")
    duck.load(con, "core.post_observations", [{
        "post_id": p, "observed_at": at(day(1), 10), "observed_date": day(1), "market": "NG", "platform": "tiktok",
        "lane": "sweep", "lane_class": "unbiased_rank", "run_id": rid("collect", day(1))}
        for p in ("za1_p1", "za1_p2", "za1_p3")])
    sightings = {market: {"source_market": market, "source_region": None, "route": "tiktok/trending",
                          "protocol": "tiktok/trending?feed=local", "observed_at": at(day(1), 10),
                          "obs_date": day(1)} for market in ("ZA", "NG")}
    duck.load(con, "core.source_market_fixture", [
        {"post_id": p, "source_markets": ["NG", "ZA"],
         "source_sightings": [sightings["ZA"], sightings["NG"]]}
        for p in ("za1_p1", "za1_p2", "za1_p3")])

    class FeedScopedModel(FakeModel):
        def complete_json(self, *, system, user, schema, model, max_tokens):
            output, usage = super().complete_json(system=system, user=user, schema=schema, model=model,
                                                  max_tokens=max_tokens)
            market = next((code for code in ("ZA", "NG") if f"Market: {code}." in user), None)
            if "explanation" in schema["properties"] and market:
                feed = {"ZA": "South Africa's feeds", "NG": "Nigeria's feeds"}[market]
                output["explanation"] = (f"The sound appears in {feed} through home-dance clips, "
                                         "likely because the shared routine is easy to repeat.")
                output["claims"][0]["text"] = f"The sound appears in {feed} in home-dance clips."
                output["claims"][1]["text"] = "The cited post shows friends dancing to the new sound at home."
                output["claims"][2]["text"] = "Friends dancing at home make the routine easy to repeat."
            return output, usage

    r = brief(con, model=FeedScopedModel())
    assert "ng_dup" in cards(r, "NG")
    shared_posts = {"za1_p1", "za1_p2", "za1_p3"}
    assert set(cards(r, "ZA")["za1"]["evidence_ids"]) == shared_posts
    assert set(cards(r, "NG")["ng_dup"]["evidence_ids"]) == shared_posts
    assert r.counts["merged"] == []


def test_a_held_candidate_claims_no_posts():
    con = world(n=1)
    add_item(con, "ZA", "za_coord", 0.99, posts=0, authenticity="likely_coordinated")
    link(con, "za_coord", ["za1_p1", "za1_p2", "za1_p3"])
    r = brief(con)
    assert "za1" in cards(r) and cards(r)["za1"]["also"] == []
    assert held_items(r, "ZA")["za_coord"]["reason"] == "likely_coordinated"
    assert r.counts["merged"] == []


def test_a_lower_card_shows_its_own_posts_first():
    con = world(n=1)
    add_item(con, "ZA", "za_share", 0.93)
    link(con, "za_share", ["za1_p1", "za1_p2"])
    r = brief(con)
    share = cards(r)["za_share"]
    assert sorted(share["evidence_ids"][:3]) == ["za_share_p1", "za_share_p2", "za_share_p3"]
    assert sorted(share["evidence_ids"][3:]) == ["za1_p1", "za1_p2"]


def test_transitive_overlap_merges_using_full_post_sets_when_the_pack_is_full():
    shown = [{"id": f"shown_{i}", "handle": f"@creator_{i}"} for i in range(job.PACK_MAX)]
    a = merge_candidate("A", 0.9, {"p1", "p2", "p3"}, shown)
    b = merge_candidate("B", 0.8, {"p1", "p2", "p4"}, [{"id": "p4", "handle": "@b"}])
    c = merge_candidate("C", 0.7, {"p4", "p5", "p6"})
    by_market = {market: [] for market in job.MARKETS}
    by_market["ZA"] = [c, b, a]

    merged = job._merge(by_market)

    assert merged == [
        {"market": "ZA", "into": "A", "item_id": "B"},
        {"market": "ZA", "into": "A", "item_id": "C"},
    ]
    assert by_market["ZA"] == [a]
    assert a["posts"] == {"p1", "p2", "p3", "p4", "p5", "p6"}
    assert [item["item_id"] for item in a["also"]] == ["B", "C"]
    assert len(a["pack"]["evidence"]) == job.PACK_MAX
    assert "p4" not in {evidence["id"] for evidence in a["pack"]["evidence"]}


def test_later_overlap_uses_merged_posts_and_keeps_higher_card_on_a_tie():
    a = merge_candidate("A", 0.9, {"p1", "p2", "p3"})
    b = merge_candidate("B", 0.8, {"p1", "p2", "p4"})
    d = merge_candidate("D", 0.7, {"p7", "p8", "p9"})
    c = merge_candidate("C", 0.6, {"p1", "p4", "p7", "p8"})
    by_market = {market: [] for market in job.MARKETS}
    by_market["ZA"] = [a, b, d, c]

    merged = job._merge(by_market)

    assert merged == [
        {"market": "ZA", "into": "A", "item_id": "B"},
        {"market": "ZA", "into": "A", "item_id": "C"},
    ]
    assert [card["row"]["item_id"] for card in by_market["ZA"]] == ["A", "D"]
    assert [item["item_id"] for item in a["also"]] == ["B", "C"]



def test_an_item_merged_into_a_card_that_is_then_held_stays_listed_on_the_held_item():
    # Rule 5 (RULES.md): held-back items are shown with their reason, never dropped silently. The merged item leaves
    # by_market before confirm and explain, so when its host is held for an explanation that failed its checks the
    # held item has to carry it, as the card does.
    con = world(n=3)
    add_item(con, "ZA", "za_dup", 0.93, posts=0)
    link(con, "za_dup", ["za1_p1", "za1_p2", "za1_p3"])
    r = brief(con, model=FakeModel(ruled_out=False), confirm=FakeConfirm())
    assert r.counts["merged"] == [{"market": "ZA", "into": "za1", "item_id": "za_dup"}]
    held = held_items(r, "ZA")
    assert held["za1"]["reason"] == "explanation_failed"
    assert held["za1"]["also"] == [{"item_id": "za_dup", "title": "#za_dup"}]
    assert "also" not in held["za2"]
