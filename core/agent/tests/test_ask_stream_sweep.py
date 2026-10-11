"""Release C review I4: the sweep's posts are the code's, not the model's. They are not streamed as evidence events
unless a claim cites one, and then its evidence event comes before the claim event that cites it."""

import copy
from datetime import date, datetime, timezone

from core.agent import ask
from core.agent.context import RunContext
from core.agent.tests import test_ask as base_ask
from core.agent.tests.test_ask_depth_sweep import QUESTION, AskWarehouse, make_row, sweep_harness

LISTED = "obs1_" + "d" * 32  # a post a store-count row lists, so the gate fetches and streams
AT = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)


class ListingWarehouse(AskWarehouse):
    def run(self, sql, params, max_bytes_billed):
        if "ORDER BY COALESCE(p.published_at" in sql or params.get("k") == 100:  # the sweep's searches only
            return [make_row("sw1", "x", day=date(2026, 9, 25), published_at=AT),
                    make_row("sw2", "tiktok", day=date(2026, 9, 25), published_at=AT)]
        rows = super().run(sql, params, max_bytes_billed)
        if "WITH t AS" in sql:
            rows = [dict(r, sample_post_ids=["tt_2", LISTED]) for r in rows]
        return rows


def run_with_a_claim_on_sw2():
    h = sweep_harness()
    h.warehouse = h.deps.warehouse = ListingWarehouse()
    written = copy.deepcopy(base_ask.WRITER_OUT)
    written["claims"].append({"id": "c5", "text": "A Johannesburg amapiano post drew attention.",
                              "label": "single_source", "kind": "observation", "evidence_ids": ["sw2"], "quotes": [],
                              "numbers": []})
    h.model.writer_out = written
    return h, h.run(question=QUESTION)


def test_sweep_posts_nobody_cites_are_not_streamed_as_evidence_i4():
    h, out = run_with_a_claim_on_sw2()
    streamed = [e["evidence"]["id"] for e in h.events if e["event"] == "evidence"]
    assert "sw1" not in streamed and "sw2" in streamed
    assert out["run"]["posts"] == 8  # both sweep posts are still in the run's evidence


def test_a_cited_sweep_post_is_streamed_before_the_claim_that_cites_it_i4():
    h, _ = run_with_a_claim_on_sw2()
    order = [(e["event"], (e.get("evidence") or e.get("claim"))["id"]) for e in h.events
             if e["event"] in ("evidence", "claim")]
    assert order.index(("evidence", "sw2")) < order.index(("claim", "c5"))
    assert order.count(("evidence", "sw2")) == 1


def test_progress_holds_back_ids_until_released_and_never_sends_one_twice_i4():
    sent = []
    progress = ask.Progress(sent.append, lambda: datetime(2026, 9, 28, 6, 0), market_label="South Africa",
                            window=(date(2026, 9, 22), date(2026, 9, 28)))
    ctx = RunContext(run_id="r_1", tier="T1", as_of=datetime(2026, 9, 28, 6, 0), market="ZA")
    ctx.evidence.update({"a": {"id": "a"}, "b": {"id": "b"}, "c": {"id": "c"}})
    progress.new_evidence(ctx)
    ctx.evidence.update({"s1": {"id": "s1"}, "s2": {"id": "s2"}, "d": {"id": "d"}})
    progress.hold_back({"s1", "s2", "a"})  # a was already sent, so holding it back changes nothing
    progress.new_evidence(ctx)
    assert [e["evidence"]["id"] for e in sent] == ["a", "b", "c", "d"]
    progress.release(ctx, ["s2", "d", "missing"])
    progress.release(ctx, ["s2"])
    progress.new_evidence(ctx)
    assert [e["evidence"]["id"] for e in sent] == ["a", "b", "c", "d", "s2"]
