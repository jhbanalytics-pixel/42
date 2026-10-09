"""The morning brief with locality_v2 as the single admission gate (C4 v3 sections 11.2 and 11.6, tests L-19 to L-22).

The golden-path world is the honest local ZA trend. Each test marks its item_state row as written under
locality_basis = locality_v2.1 (the rule the switch release makes the detect step use) and gives it the retained
locality row the case needs. The model and the confirm lane are scripted; the pack, the gate context, the claim
checks, the gate and the payload are the real modules."""

import json

import pytest

from core.brief import gatectx, job
from core.brief import market_scope as brief_market_scope
from core.brief.tests.test_brief_golden_path import ITEM, HonestModel, world
from core.brief.tests.test_brief_job import EARLY, Client, FakeChain, FakeConfirm, pin_brief_model_cap
from core.brief.tests.test_locality_shadow import add_locality
from core.trust import locality
from core.detect.tests.fixtures import D

V2 = "locality_v2.1"


def v2_world(*, eligible=True, geo_status="market_unconfirmed", status=None):
    con = world(located=True)
    con.execute("UPDATE core.item_state SET locality_basis = ?, eligible = ?, eligible_v1 = TRUE, geo_status = ?, "
                "locality_status = ?", [V2, eligible, geo_status, status])
    return con


def run_brief(con, monkeypatch, *, scope=None):
    monkeypatch.setattr(job, "read_market_scope", scope or brief_market_scope.read_market_scope, raising=False)
    pin_brief_model_cap(monkeypatch, EARLY)
    model, confirm, client = HonestModel(True), FakeConfirm(), Client(con)
    counts = job.run(client, D, chain=FakeChain(), model=model, make_sc=lambda run_id: object(), clock=lambda: EARLY,
                     build_ctx=gatectx.build_ctx, confirm=confirm, core="core", agent="agent")
    rows = {r["market"]: r for r in client.inserted.get("agent.briefs", [])}
    return counts, json.loads(rows["ZA"]["payload"]), model, confirm


def confirmed(confirm):
    return [c["item_id"] for call in confirm.calls for c in call["candidates"]]


def spend(model):
    return len(model.writer) + len(model.support) + len(model.critic)


def test_d1_v1_rejects_v2_admits_the_candidate_is_judged_and_published_with_the_flag(monkeypatch):
    con = v2_world(geo_status="not_local", status="market_unconfirmed")      # detect's v1 geo_status said not_local
    add_locality(con, 3, 3)
    _, payload, model, _ = run_brief(con, monkeypatch)
    [card] = payload["cards"]
    assert card["item_id"] == ITEM and card["explained"] is True and payload["held_back"]["count"] == 0
    assert card["locality_v2"]["status"] == "market_unconfirmed" and card["flag"] == "market_unconfirmed"
    assert card["market_scope"] == "market" and card["market_scope_basis"] == V2
    assert spend(model) > 0


def test_d3_v1_admits_v2_rejects_the_candidate_is_held_visibly_and_costs_nothing(monkeypatch):
    con = v2_world(eligible=False, geo_status="local", status="not_local")
    add_locality(con, 8, 0)
    counts, payload, model, confirm = run_brief(con, monkeypatch)
    assert payload["cards"] == []
    [held] = payload["held_back"]["items"]
    assert (held["item_id"], held["rule"], held["reason"]) == (ITEM, "G6", "not_local")
    assert "0 of 8" in held["reason_text"] and held["locality_v2"]["status"] == "not_local"
    assert spend(model) == 0 and ITEM not in confirmed(confirm)
    assert payload["locality_audit"]["not_local_total"] == 1 and payload["locality_audit"]["items"] == []   # represented


def test_d2_the_pack_rejects_and_v2_admits_the_candidate_is_judged_and_not_dropped(monkeypatch):
    con = v2_world(status="local")
    add_locality(con, 20, 20)

    def pack_says_global(client, row, d, market, core, agent):
        return {"market_scope": "global", "market_posts7": 0, "total_posts7": 12, "market_share7": 0.0}

    _, payload, model, _ = run_brief(con, monkeypatch, scope=pack_says_global)
    [card] = payload["cards"]
    assert card["market_scope"] == "market" and card["flag"] is None and card["explained"] is True
    assert card["pack_scope_v1"]["market_scope"] == "global"               # V1-B stays as an observation, by its own name
    assert spend(model) > 0


def test_a_failed_v1_pack_read_does_not_hold_a_candidate_v2_admits(monkeypatch):
    con = v2_world(status="local")
    add_locality(con, 20, 20)

    def broken(client, row, d, market, core, agent):
        raise RuntimeError("scope view unavailable")

    _, payload, _, _ = run_brief(con, monkeypatch, scope=broken)
    assert [c["item_id"] for c in payload["cards"]] == [ITEM]


def test_the_label_follows_the_wilson_bound_not_the_status(monkeypatch):
    con = v2_world(status="local")
    add_locality(con, 8, 6)                                                 # local by status, 0.409 by the bound
    _, payload, _, _ = run_brief(con, monkeypatch)
    [card] = payload["cards"]
    assert (card["locality_v2"]["status"], card["locality_v2"]["label"], card["flag"]) == (
        "local", "market_unconfirmed", "market_unconfirmed")


@pytest.mark.parametrize("how", ["no_row", "contradicting_row", "unverified_row"])
def test_l22_a_row_that_is_absent_or_invalid_is_held_as_a_data_issue_counted_in_the_banner(monkeypatch, how):
    con = v2_world(status="unreadable" if how != "no_row" else "missing")
    if how == "contradicting_row":
        add_locality(con, 8, 0, status="local")
    elif how == "unverified_row":
        add_locality(con, 8, 5, verified=False)
    counts, payload, model, confirm = run_brief(con, monkeypatch)
    assert payload["cards"] == []
    [held] = payload["held_back"]["items"]
    assert (held["item_id"], held["reason"]) == (ITEM, "data_issue") and held["reason"] != "not_local"
    assert any(b["kind"] == "data_issue" for b in payload["banners"]) and payload["status"] == "data_issue"
    assert spend(model) == 0 and ITEM not in confirmed(confirm)


def test_a_candidate_on_the_v1_basis_is_untouched_by_a_v2_row_that_disagrees(monkeypatch):
    con = world(located=True)                                               # locality_basis is NULL: written under v1
    add_locality(con, 8, 0)                                                 # a not_local v2 row beside it
    _, payload, model, _ = run_brief(con, monkeypatch)
    [card] = payload["cards"]
    assert card["explained"] is True and card["locality_v2"]["status"] == "not_local"
    assert card.get("market_scope_basis") == ("v1" if locality.LOCALITY_AUTHORITY == "v2" else None)   # M4
    assert "locality_audit" not in payload                                  # the audit lists only rows written under v2
