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
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v1")           # the second read is the shadow authority's; v2 holds the market
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



class RecordingRefusingClient(RefusingClient):
    def __init__(self, con):
        super().__init__(con)
        self.executed = []

    def query(self, sql, job_config=None):
        self.executed.append(sql)
        return super().query(sql, job_config)


def run_with_failing_view(con, monkeypatch, authority):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, authority)
    monkeypatch.setattr(job, "read_market_scope", brief_market_scope.read_market_scope, raising=False)
    pin_brief_model_cap(monkeypatch, EARLY)
    client, model = RecordingRefusingClient(con), HonestModel(True)
    counts = job.run(client, D, chain=FakeChain(), model=model, make_sc=lambda run_id: object(), clock=lambda: EARLY,
                     build_ctx=gatectx.build_ctx, confirm=FakeConfirm(), core="core", agent="agent")
    payloads = {r["market"]: json.loads(r["payload"]) for r in client.inserted["agent.briefs"]}
    return counts, payloads, client, model


def test_under_v2_a_failed_locality_read_is_not_read_again_without_the_view(monkeypatch, capsys):
    """F1: the second read turned every v2 row of the market unreadable. Under v2 the market is held and the failure is
    recorded in the counts."""
    con = v2_world(status="local")
    add_locality(con, 20, 20)
    counts, payloads, client, model = run_with_failing_view(con, monkeypatch, "v2")
    assert not any(job._NO_LOCALITY in sql for sql in client.executed)
    assert counts["locality_view_failed"] == {"markets": list(job.MARKETS), "action": "held"}
    for market, payload in payloads.items():
        assert payload["cards"] == [] and payload["status"] == "data_issue", market
        assert any(b["kind"] == "data_issue" for b in payload["banners"]), market
    assert not (model.writer or model.support or model.critic)
    assert "locality_view_read_failed" in capsys.readouterr().err


def test_under_v1_a_failed_locality_read_is_read_again_and_recorded(monkeypatch):
    con = world(located=True)
    add_locality(con)
    counts, payloads, client, _ = run_with_failing_view(con, monkeypatch, "v1")
    assert any(job._NO_LOCALITY in sql for sql in client.executed)
    assert counts["locality_view_failed"] == {"markets": list(job.MARKETS), "action": "read_without_view"}
    assert [c["item_id"] for c in payloads["ZA"]["cards"]] == [ITEM]


def test_a_run_whose_locality_read_works_records_no_failure(monkeypatch):
    from core.brief.tests.test_brief_golden_path import brief

    con = world(located=True)
    add_locality(con)
    assert "locality_view_failed" not in brief(con, HonestModel(True), monkeypatch)[0]
