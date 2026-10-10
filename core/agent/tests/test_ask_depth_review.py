"""Ask depth, review conditions: the sweep has a time bound, a late sweep is dropped, a research record is never
overwritten, sweep evidence survives a failed store count, the sweep and the counts keep their query ids, wording b takes
the question's market, and a market may be named by its full name."""

import threading
import time
from datetime import date, datetime

import pytest

from core.agent import ask
from core.agent.context import RunContext
from core.agent.tests.test_ask import make_research
from core.agent.tests.test_ask_depth_sweep import (AskWarehouse, QUESTION, SweepWarehouse, make_row, sweep_harness)
from core.agent.tests.test_warehouse_tools import SplitWarehouse, kw
from core.agent.tools import sql_query as sql_module
from core.agent.tools import warehouse as wh_module
from core.agent.tools.warehouse import search_posts, topic_sweep

NOW = datetime(2026, 10, 10, 6, 0)


def capture_ctx(h, before=None):
    """Wrap the harness's research so the test can read the run's context afterwards."""
    inner, seen = h.deps.research, {}

    def research(ctx, *args, **kwargs):
        seen["ctx"] = ctx
        if before:
            before(ctx)
        return inner(ctx, *args, **kwargs)

    h.deps.research = research
    return seen


def slow_sweep(release, finished):
    def sweep(ctx, warehouse, topic, platforms=None):
        ctx.evidence["partial"] = {"id": "partial", "platform": "x", "handle": "p", "url": "https://p", "text": "p"}
        release.wait(10)
        ctx.evidence["late"] = {"id": "late", "platform": "x", "handle": "late", "url": "https://l", "text": "late"}
        finished.set()
        return {"posts": 1, "searches": 1, "failed": {}, "family": []}
    return sweep


def timed_run(h):
    started = time.monotonic()
    out = h.run(question=QUESTION)
    return out, time.monotonic() - started


# Review, Important 2: a time bound


def test_a_slow_sweep_is_dropped_at_the_deadline_and_the_ask_stays_near_its_old_time(monkeypatch):
    monkeypatch.setattr(ask, "SWEEP_JOIN_S", 0.3)
    plain = sweep_harness()
    plain.deps.topic_sweep = None
    _, old = timed_run(plain)
    release, finished = threading.Event(), threading.Event()
    h = sweep_harness()
    h.deps.topic_sweep = slow_sweep(release, finished)
    seen = capture_ctx(h)
    out, took = timed_run(h)
    try:
        assert took < old + 1.5, (took, old)
        release.set()
        assert finished.wait(5)
        assert "late" not in seen["ctx"].evidence  # the result that came after the deadline was dropped
        assert "partial" not in seen["ctx"].evidence  # and so was what the unfinished sweep had already written
        assert not any(r["purpose"].startswith("Topic sweep") for r in out["query_receipts"].values())
    finally:
        release.set()


def test_a_sweep_that_finishes_inside_the_wait_is_kept(monkeypatch):
    monkeypatch.setattr(ask, "SWEEP_JOIN_S", 5)
    release, finished = threading.Event(), threading.Event()
    release.set()
    h = sweep_harness()
    h.deps.topic_sweep = slow_sweep(release, finished)
    seen = capture_ctx(h)
    h.run(question=QUESTION)
    assert finished.wait(5) and "late" in seen["ctx"].evidence


def test_the_sweep_stops_starting_searches_after_its_own_deadline(monkeypatch):
    monkeypatch.setattr(wh_module, "SWEEP_DEADLINE_S", 0.05)

    class Slow(SweepWarehouse):
        def run(self, sql, params, max_bytes_billed):
            time.sleep(0.1)
            return super().run(sql, params, max_bytes_billed)

    ctx = RunContext(run_id="r_dl", tier="T1", as_of=NOW, market="ZA", window_start=date(2026, 10, 4),
                     window_end=date(2026, 10, 10))
    out = topic_sweep(ctx, Slow(by_engagement=[make_row("e1")]), ["load shedding"], platforms=None)
    assert out["searches"] == 1 and set(out["failed"]) == {"recent", "semantic a", "semantic b"}
    assert all("deadline" in reason for reason in out["failed"].values())
    assert set(ctx.evidence) == {"e1"}


def test_the_sweep_sets_a_query_timeout_for_its_reads_and_clears_it_after(monkeypatch):
    monkeypatch.setattr(wh_module, "SWEEP_QUERY_TIMEOUT_S", 7)
    seen = []

    class Watching(SweepWarehouse):
        def run(self, sql, params, max_bytes_billed):
            seen.append(sql_module.QUERY_TIMEOUT_S.get())
            return super().run(sql, params, max_bytes_billed)

    ctx = RunContext(run_id="r_to", tier="T1", as_of=NOW, market="ZA", window_start=date(2026, 10, 4),
                     window_end=date(2026, 10, 10))
    topic_sweep(ctx, Watching(), ["#humor"], platforms=None)
    assert seen and set(seen) == {7}
    assert sql_module.QUERY_TIMEOUT_S.get() is None


@pytest.mark.parametrize("sql,timeout", [
    ("SELECT 1", 7), ("SELECT 1", None),
    ("SELECT * FROM intelligence_42_agent.tvf_search_posts(@q, NULL, @since, @until, @k) s", 7)])
def test_the_bigquery_wrapper_hands_the_timeout_to_the_job(sql, timeout):
    got = []

    class Job:
        def result(self, **kwargs):
            got.append(kwargs)
            return []

    class Client:
        def query(self, text, **kwargs):
            return Job()

    wh = sql_module.BigQueryWarehouse()
    wh._client = Client()
    token = sql_module.QUERY_TIMEOUT_S.set(timeout)
    try:
        wh.run(sql, {"q": "x", "since": date(2026, 10, 4), "until": date(2026, 10, 10), "k": 2}, 1000)
    finally:
        sql_module.QUERY_TIMEOUT_S.reset(token)
    assert got and got[0].get("timeout") == timeout and ("timeout" in got[0]) == (timeout is not None)


# Review minors: S2, S20, S21, S22


def test_a_record_research_stored_is_not_replaced_by_the_sweeps_copy_of_the_same_post():
    h = sweep_harness()
    mine = {"id": "sw1", "platform": "x", "handle": "research", "url": "https://r", "text": "research record"}
    seen = capture_ctx(h, before=lambda ctx: ctx.evidence.__setitem__("sw1", mine))
    h.run(question=QUESTION)
    assert seen["ctx"].evidence["sw1"] is mine


def test_the_inline_sweep_does_not_replace_a_record_the_run_already_holds():
    ctx = RunContext(run_id="r_keep", tier="T1", as_of=NOW, market="ZA", window_start=date(2026, 10, 4),
                     window_end=date(2026, 10, 10))
    mine = {"id": "e1", "platform": "x", "handle": "research", "url": "https://r", "text": "research record"}
    ctx.evidence["e1"] = mine
    out = topic_sweep(ctx, SweepWarehouse(by_engagement=[make_row("e1"), make_row("e2")]), ["#humor"], platforms=None)
    assert ctx.evidence["e1"] is mine and "e2" in ctx.evidence and out["posts"] == 2


def test_sweep_evidence_and_queries_survive_a_failed_store_count():
    def failing(ctx, warehouse, platforms=None):
        raise RuntimeError("counts down")

    h = sweep_harness()
    h.deps.store_counts = failing
    seen = capture_ctx(h)
    out = h.run(question=QUESTION)
    assert ask.STORE_FAILED in out["run"]["notices"]
    assert {"sw1", "sw2"} <= set(seen["ctx"].evidence)
    assert sum(r["purpose"].startswith("Topic sweep") for r in out["query_receipts"].values()) == 4


def test_the_sweeps_queries_take_the_ids_after_the_counts_and_the_counts_keep_theirs():
    out = sweep_harness().run(question=QUESTION)
    receipts = sorted(out["query_receipts"].items(), key=lambda kv: int(kv[0].split("_")[1]))
    numbers = [int(qid.split("_")[1]) for qid, _ in receipts]
    assert numbers == list(range(1, len(numbers) + 1))

    def kind(purpose):
        return ("store" if purpose.startswith("Whole-store") else
                "sweep" if purpose.startswith(("Topic sweep", "search_posts related tags")) else "research")

    kinds = [kind(r["purpose"]) for _, r in receipts]
    assert kinds.count("store") == 4 and kinds.count("sweep") == 5
    first_store, first_sweep = kinds.index("store"), kinds.index("sweep")
    assert kinds == ["research"] * first_store + ["store"] * 4 + ["sweep"] * 5 and first_store + 4 == first_sweep


@pytest.mark.parametrize("market,name", [("ZA", "South Africa"), ("NG", "Nigeria"), ("KE", "Kenya"), (None, "Africa")])
def test_wording_b_names_the_questions_market(market, name):
    ctx = RunContext(run_id="r_w", tier="T1", as_of=NOW, market=market, window_start=date(2026, 10, 4),
                     window_end=date(2026, 10, 10))
    wh = SweepWarehouse()
    topic_sweep(ctx, wh, ["#humor"], platforms=None)
    queries = [p["q"] for sql, p in wh.runs if "tvf_search_posts" in sql]
    assert queries == ["humor", f"people posting about humor, {name}"]


# a market may be named by its full name as well as its code


@pytest.mark.parametrize("market,named", [("ZA", "South Africa"), ("ZA", " south africa "), ("NG", "Nigeria"),
                                          ("KE", "Kenya"), ("KE", "ke")])
def test_a_market_named_in_full_is_accepted_and_changes_nothing(market, named):
    plain = SplitWarehouse([kw("a")], [])
    search_posts(RunContext(run_id="r1", tier="T1", as_of=NOW, market=market), plain, "braai")
    full = SplitWarehouse([kw("a")], [])
    search_posts(RunContext(run_id="r2", tier="T1", as_of=NOW, market=market), full, "braai", market=named)
    assert [r[1] for r in full.runs] == [r[1] for r in plain.runs]


@pytest.mark.parametrize("market,named", [("ZA", "Nigeria"), ("NG", "South Africa"), ("KE", "ZA"), ("ZA", "Narnia")])
def test_another_market_by_name_or_code_is_still_refused(market, named):
    from core.agent.context import Refused

    wh = SplitWarehouse([kw("a")], [])
    with pytest.raises(Refused, match="market"):
        search_posts(RunContext(run_id="r3", tier="T1", as_of=NOW, market=market), wh, "braai", market=named)
    assert wh.runs == []
