from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from core.agent import checks, writer
from core.agent.context import RunContext
from core.agent.model_budget import BudgetRefused
from core.agent.tests.test_ask import NOW, SEARCH_EMBED_USD
from core.agent.tests.test_ask_model_budget import make_budget
from core.agent.tests.test_sql_tool import FakeWarehouse
from core.agent.tools.sql_query import sql_query

SQL = ("SELECT COUNT(*) AS n FROM intelligence_42_agent.tvf_search_posts"
       "(@q, NULL, @since, @until, 5)")
PARAMS = {"q": "query", "since": NOW.date(), "until": NOW.date()}


def context(cap):
    return RunContext(run_id="r_rerun_cap", tier="T1", as_of=NOW,
                      model_budget=make_budget(hold=cap, research=cap))


def pin(ctx, query):
    return {"query_id": query["query_id"], "value": 3, "run_id": ctx.run_id,
            "result_hash": query["result_hash"]}


def recorded(ctx, query="query"):
    qid, digest = ctx.record_query(SQL, {**PARAMS, "q": query}, [{"n": 3}], "semantic count")
    return {"query_id": qid, "result_hash": digest}


def test_final_number_rerun_cannot_dispatch_after_the_embedding_hold_is_spent():
    ctx = context(SEARCH_EMBED_USD)
    warehouse = FakeWarehouse(rows=[{"n": 3}])
    query = sql_query(ctx, warehouse, SQL, purpose="semantic count", params=PARAMS)
    problem = checks._number_problem(pin(ctx, query), ctx, warehouse, {})
    assert problem is not None and "failed (BudgetRefused)" in problem
    assert len(warehouse.runs) == 1
    assert ctx.model_budget.booked_usd == ctx.model_usd_extra == SEARCH_EMBED_USD


def test_allowed_number_rerun_books_once_and_cached_verification_does_not_dispatch_again():
    ctx = context(2 * SEARCH_EMBED_USD)
    warehouse = FakeWarehouse(rows=[{"n": 3}])
    query = sql_query(ctx, warehouse, SQL, purpose="semantic count", params=PARAMS)
    reruns = {}
    assert checks._number_problem(pin(ctx, query), ctx, warehouse, reruns) is None
    assert checks._number_problem(pin(ctx, query), ctx, warehouse, reruns) is None
    assert len(warehouse.runs) == 2
    assert ctx.model_budget.booked_usd == ctx.model_usd_extra == 2 * SEARCH_EMBED_USD


def test_numeric_preflight_cannot_bypass_the_embedding_hold():
    ctx = context(SEARCH_EMBED_USD)
    warehouse = FakeWarehouse(rows=[{"n": 3}])
    query = sql_query(ctx, warehouse, SQL, purpose="semantic count", params=PARAMS)
    draft = {"claims": [{"id": "c1", "text": "There were 3 posts.", "numbers": [pin(ctx, query)],
                         "evidence_ids": []}]}
    issues = writer.unpinned_claim_numerals(draft, ctx, warehouse, window=(NOW.date(), NOW.date()))
    assert issues and "failed (BudgetRefused)" in issues[0]["invalid_numbers"][0]["reason"]
    assert len(warehouse.runs) == 1
    assert ctx.model_budget.booked_usd == SEARCH_EMBED_USD


def test_failed_number_rerun_keeps_its_ceiling_and_blocks_an_unaffordable_retry():
    ctx = context(0.0005)
    query = recorded(ctx)

    class Warehouse(FakeWarehouse):
        def run(self, sql, params, cap):
            super().run(sql, params, cap)
            raise RuntimeError("query failed after dispatch")

    warehouse = Warehouse(rows=[{"n": 3}])
    assert "failed (RuntimeError)" in checks._number_problem(pin(ctx, query), ctx, warehouse, {})
    assert "failed (BudgetRefused)" in checks._number_problem(pin(ctx, query), ctx, warehouse, {})
    assert len(warehouse.runs) == 1
    assert ctx.model_budget.booked_usd == ctx.model_usd_extra == SEARCH_EMBED_USD


def test_parallel_prefetch_reruns_share_one_embedding_ceiling():
    ctx = context(0.0005)
    queries = [recorded(ctx, text) for text in ("one", "two")]
    claims = [{"numbers": [pin(ctx, query)]} for query in queries]
    entered, release, lock = Event(), Event(), Lock()

    class Warehouse(FakeWarehouse):
        def run(self, sql, params, cap):
            with lock:
                rows = super().run(sql, params, cap)
            entered.set()
            assert release.wait(5)
            return rows

    warehouse = Warehouse(rows=[{"n": 3}])
    reruns = {}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(checks.prefetch_reruns, claims, ctx, warehouse, reruns)
        try:
            assert entered.wait(5)
        finally:
            release.set()
        future.result(timeout=5)
    assert len(warehouse.runs) == 1
    assert sum(isinstance(result, BudgetRefused) for result in reruns.values()) == 1
    assert ctx.model_budget.booked_usd == ctx.model_usd_extra == SEARCH_EMBED_USD


def test_pure_warehouse_rerun_remains_free_when_the_model_hold_is_spent():
    ctx = context(SEARCH_EMBED_USD)
    ticket = ctx.model_budget.reserve_cost(SEARCH_EMBED_USD, research=True)
    ctx.model_budget.settle_cost(ticket)
    qid, digest = ctx.record_query("SELECT 3 AS n", {}, [{"n": 3}], "pure count")
    query = {"query_id": qid, "result_hash": digest}
    warehouse = FakeWarehouse(rows=[{"n": 3}])
    assert checks._number_problem(pin(ctx, query), ctx, warehouse, {}) is None
    assert len(warehouse.runs) == 1
    assert warehouse.dry_runs == []
    assert ctx.model_budget.booked_usd == SEARCH_EMBED_USD
    assert ctx.model_usd_extra == 0
