"""A locality view that cannot be read (C4 v3 sections 8.4 and 10): the candidate statement is run again without it.

A row admitted under the v1 pack scope never reads the view, so its outcome does not change (tests L-13 and the one
below). A row admitted under locality_v2.1 then has no locality row, which is unreadable: it is held as a data issue and
counted in the banner, and the brief goes on."""

import json

from core.brief import gatectx, job
from core.brief import market_scope as brief_market_scope
from core.brief.tests.test_brief_golden_path import ITEM, HonestModel, brief, world
from core.brief.tests.test_brief_job import EARLY, Client, FakeChain, FakeConfirm, pin_brief_model_cap
from core.brief.tests.test_locality_authority import v2_world
from core.brief.tests.test_locality_shadow import RefusingClient, add_locality
from core.detect.tests.fixtures import D


def test_the_second_read_is_the_first_statement_with_the_view_replaced_by_an_empty_one():
    first, second = job.QUERIES["candidates"], job.QUERIES["candidates_without_locality"]
    assert "v_item_locality_current" in first and "v_item_locality_current" not in second
    assert first.replace("{core}.v_item_locality_current lo", job._NO_LOCALITY) == second


def test_a_row_on_the_v2_basis_is_held_as_a_data_issue_when_the_view_cannot_be_read(monkeypatch, capsys):
    con = v2_world(status="local")
    add_locality(con, 20, 20)
    monkeypatch.setattr(job, "read_market_scope", brief_market_scope.read_market_scope, raising=False)
    pin_brief_model_cap(monkeypatch, EARLY)
    client, model = RefusingClient(con), HonestModel(True)
    job.run(client, D, chain=FakeChain(), model=model, make_sc=lambda run_id: object(), clock=lambda: EARLY,
            build_ctx=gatectx.build_ctx, confirm=FakeConfirm(), core="core", agent="agent")
    payload = json.loads({r["market"]: r for r in client.inserted["agent.briefs"]}["ZA"]["payload"])
    assert payload["cards"] == []
    [held] = payload["held_back"]["items"]
    assert (held["item_id"], held["reason"], held["locality_v2"]["status"]) == (ITEM, "data_issue", "unreadable")
    assert any(b["kind"] == "data_issue" for b in payload["banners"])
    assert not (model.writer or model.support or model.critic)
    assert "locality_view_read_failed" in capsys.readouterr().err


def test_a_row_on_the_v1_basis_is_unchanged_when_the_view_cannot_be_read(monkeypatch):
    baseline = brief(world(located=True), HonestModel(True), monkeypatch)[1]
    con = world(located=True)
    add_locality(con, 8, 0)                                     # a not_local row that the v1 basis must not read
    monkeypatch.setattr("core.brief.tests.test_brief_golden_path.Client", RefusingClient)
    assert brief(con, HonestModel(True), monkeypatch)[1] == baseline

