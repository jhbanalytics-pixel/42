from concurrent.futures import ThreadPoolExecutor
from math import ceil
from threading import Event

import pytest

from core.agent import ask
from core.agent.context import RunContext
from core.agent.model_budget import BudgetRefused
from core.agent.tests.test_ask import Harness, NOW
from core.agent.tests.test_ask_model_budget import configure_gemini, make_budget
from core.agent.tests.test_warehouse_tools import FakeWarehouse, SplitWarehouse
from core.agent.tools.warehouse import search_posts
from core.agent.tools.sql_query import sql_query
from core.understand.embed import EMBED_USD_PER_MILLION_TOKENS, MAX_TOKENS_PER_POST


def test_submicro_confirmed_research_budget_refuses_without_leaking_a_hold(monkeypatch):
    configure_gemini(monkeypatch)
    approved = ask.gate_usd(ask.MODEL, 1) - ask.once_usd(ask.MODEL) + 1e-9
    plan = {"sub_questions": [{"id": "q1", "text": "Which sounds are spreading?",
                              "platforms": ["tiktok"], "credits": 0}],
            "gap_round": False, "max_credits": 0, "max_model_usd": approved}
    before = dict(ask._IN_FLIGHT)
    try:
        with pytest.raises(ValueError, match="research"):
            Harness().run(tier="T3", plan=plan, max_credits=0, max_model_usd=approved)
        assert ask._IN_FLIGHT == before
    finally:
        ask._IN_FLIGHT.clear()
        ask._IN_FLIGHT.update(before)


def test_budget_constructor_failure_never_registers_a_daily_hold(monkeypatch):
    configure_gemini(monkeypatch)

    def unavailable(*args, **kwargs):
        raise RuntimeError("budget unavailable")

    monkeypatch.setattr(ask, "AskModelBudget", unavailable)
    before = dict(ask._IN_FLIGHT)
    try:
        with pytest.raises(RuntimeError, match="budget unavailable"):
            Harness().run()
        assert ask._IN_FLIGHT == before
    finally:
        ask._IN_FLIGHT.clear()
        ask._IN_FLIGHT.update(before)


def test_cancel_during_seven_second_spend_read_records_planning(monkeypatch):
    configure_gemini(monkeypatch)
    ticks = [0.0]
    monkeypatch.setattr(ask.time, "monotonic", lambda: ticks[0])
    h = Harness()

    def spent():
        ticks[0] = 7
        h.stop_flag["stop"] = True
        return 0

    h.deps.spent_today_usd = spent
    out = h.run()
    assert out["run"]["phase_seconds"]["plan"] == 7
    assert out["run"]["phase_seconds"]["research"] == 0
    assert h.model.calls == []


def context(budget):
    return RunContext(run_id="r_review", tier="T1", as_of=NOW, model_budget=budget)


def semantic_runs(warehouse):
    return [call for call in warehouse.runs if "tvf_search_posts" in call[0]]


def test_semantic_embedding_cannot_dispatch_after_the_shared_hold_is_spent():
    budget = make_budget(hold=0.002, research=0.002)
    ticket = budget.reserve("gemini-test", 1000, 1000)
    budget.settle(ticket, {"input_tokens": 1000, "output_tokens": 1000, "usd": 0.002})
    ctx, warehouse = context(budget), FakeWarehouse()
    result = search_posts(ctx, warehouse, "x" * 1024)
    assert semantic_runs(warehouse) == []
    assert result["semantic"] == "unavailable"
    assert budget.booked_usd == 0.002
    assert ctx.model_usd_extra == 0


def test_concurrent_semantic_queries_reserve_before_dispatch():
    budget = make_budget(hold=0.0004, research=0.0004)
    entered, release = Event(), Event()

    class Warehouse(FakeWarehouse):
        def run(self, sql, params, cap):
            if "tvf_search_posts" in sql:
                entered.set()
                assert release.wait(5)
            return super().run(sql, params, cap)

    first, second = Warehouse(), FakeWarehouse()
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(search_posts, context(budget), first, "x" * 1024)
        assert entered.wait(5)
        try:
            two = pool.submit(search_posts, context(budget), second, "x" * 1024)
            out = two.result(timeout=5)
            assert semantic_runs(second) == []
            assert out["semantic"] == "unavailable"
            assert budget.booked_usd <= 0.0004
        finally:
            release.set()
        one.result(timeout=5)
    assert len(semantic_runs(first)) == 1
    assert budget.booked_usd == ceil(MAX_TOKENS_PER_POST * EMBED_USD_PER_MILLION_TOKENS) / 1e6


def test_failed_embedding_keeps_its_ceiling_and_blocks_an_unaffordable_retry():
    budget = make_budget(hold=0.0005, research=0.0005)
    ctx = context(budget)
    warehouse = SplitWarehouse([], [], fail=RuntimeError("query failed after dispatch"))
    search_posts(ctx, warehouse, "x" * 1024)
    first = budget.booked_usd
    search_posts(ctx, warehouse, "x" * 1024)
    ceiling = MAX_TOKENS_PER_POST * EMBED_USD_PER_MILLION_TOKENS / 1e6
    assert first >= ceiling
    assert budget.booked_usd == first
    assert len(semantic_runs(warehouse)) == 1


def test_direct_semantic_sql_cannot_bypass_the_model_hold():
    budget = make_budget(hold=0.0005, research=0.0005)
    warehouse = FakeWarehouse()
    call = "intelligence_42_agent.tvf_search_posts(@q, NULL, @since, @until, 1)"
    with pytest.raises(BudgetRefused, match="budget_exhausted"):
        sql_query(context(budget), warehouse, f"SELECT * FROM {call} a CROSS JOIN {call} b",
                  purpose="direct semantic query", params={"q": "query", "since": NOW.date(), "until": NOW.date()})
    assert warehouse.runs == []
    assert budget.booked_usd == 0


def test_semantic_dry_run_refusal_books_no_embedding_cost():
    budget = make_budget(hold=0.0005, research=0.0005)

    class Warehouse(FakeWarehouse):
        def dry_run(self, sql, params):
            result = super().dry_run(sql, params)
            return {**result, "bytes": 10**12} if "tvf_search_posts" in sql else result

    warehouse = Warehouse()
    out = search_posts(context(budget), warehouse, "query")
    assert out["semantic"] == "unavailable"
    assert semantic_runs(warehouse) == []
    assert budget.booked_usd == 0


def test_query_cost_reserve_stays_within_the_approved_hold():
    budget = make_budget(hold=0.00031, research=0.00031)
    ctx = context(budget)
    search_posts(ctx, FakeWarehouse(), "x" * 10000)
    ceiling = ceil(MAX_TOKENS_PER_POST * EMBED_USD_PER_MILLION_TOKENS) / 1e6
    assert ctx.model_usd_extra == ceiling
    assert budget.booked_usd <= 0.00031
    assert not budget.ceiling_exceeded


def test_successful_embedding_without_native_usage_keeps_the_full_reserve():
    budget = make_budget(hold=0.0005, research=0.0005)
    ctx = context(budget)
    warehouse = FakeWarehouse()
    search_posts(ctx, warehouse, "query")
    ceiling = ceil(MAX_TOKENS_PER_POST * EMBED_USD_PER_MILLION_TOKENS) / 1e6
    assert len(semantic_runs(warehouse)) == 1
    assert budget.booked_usd == budget.conservative_usd == ceiling
    assert ctx.model_usd_extra == ceiling


def test_successive_embedding_calls_cannot_reuse_unverified_room():
    budget = make_budget(hold=0.0005, research=0.0005)
    first, second = FakeWarehouse(), FakeWarehouse()
    search_posts(context(budget), first, "query")
    search_posts(context(budget), second, "query")
    ceiling = ceil(MAX_TOKENS_PER_POST * EMBED_USD_PER_MILLION_TOKENS) / 1e6
    assert len(semantic_runs(first)) == 1
    assert semantic_runs(second) == []
    assert budget.booked_usd == budget.conservative_usd == ceiling


def test_verified_native_model_usage_can_release_unused_reservation():
    budget = make_budget(hold=0.002, research=0.002)
    first = budget.reserve("gemini-test", 1000, 1000)
    assert budget.settle(first, {"input_tokens": 100, "output_tokens": 100, "usd": 0.0002})
    assert budget.booked_usd == 0.0002
    assert budget.conservative_usd == 0
    budget.reserve("gemini-test", 1000, 500)
    assert budget.booked_usd == 0.0017
