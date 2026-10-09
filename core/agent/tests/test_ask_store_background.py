"""Ask speed, fix 3 (ASK-LATENCY.md): the whole-store count runs beside research instead of after it.

The count sat serially between research and the writer in 27 of 29 runs since 4 October (9 s at p50, 14 s at most). Its
SQL, market, window and parameters depend on nothing research finds, so it starts once the window is final. These tests
hold it to the serial run: the same queries under the same ids, the same result hashes, the same bytes, no model call
added, and a failed or abandoned count behaving as the serial one did.
"""

import threading

import pytest

from core.agent import ask
from core.agent.context import RunContext
from core.agent.tests.test_ask import COUNT_SQL, NOW, FakeWarehouse, Harness, make_research
from core.agent.tools.sql_query import MAX_BYTES_BILLED, sql_query
from core.agent.tools.warehouse import STORE_TOOL, store_breadth, store_totals

QUESTION = "What is behind amapiano in South Africa this week?"
WAIT_S = 5


def billed_for(sql):
    return 1000 + len(sql) % 977


class StoreWarehouse(FakeWarehouse):
    """Distinct rows for the whole-store reads, a byte ledger kept under a lock, and a flag for the first store read."""

    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.log = []          # (sql, bytes billed, max_bytes_billed) of every run, in arrival order
        self.billed = 0
        self.store_started = threading.Event()

    def dry_run(self, sql, params):
        return {"bytes": billed_for(sql), "tables": ["ogilvy-trends-v2.intelligence_42_core.posts"]}

    def run(self, sql, params, max_bytes_billed):
        if "GROUP BY p.platform ORDER BY posts DESC" in sql:
            self.store_started.set()
            rows = [{"platform": "tiktok", "posts": 4100, "creators": 900, "located_posts": 3000, "located_creators": 700},
                    {"platform": "x", "posts": 1200, "creators": 400, "located_posts": 900, "located_creators": 300}]
        elif "sound_id" in sql and "WITH s AS" in sql:
            rows = [{"platform": "tiktok", "sound_id": "7400000000000000001", "sound_title": "Log drum", "posts": 300,
                     "creators": 120, "located_posts": 250, "located_creators": 100, "sample_post_ids": ["tt_1"]}]
        elif "WITH t AS" in sql:
            rows = [{"platform": "tiktok", "hashtag": "amapiano", "posts": 800, "creators": 300, "located_posts": 700,
                     "located_creators": 250, "sample_post_ids": ["tt_2"]}]
        elif "WITH b AS" in sql:
            rows = [{"kind": "sound", "platform": "tiktok", "sound_id": "7400000000000000001", "hashtag": None,
                     "posts": 200, "creators": 90, "located_posts": 150, "located_creators": 70}]
        else:
            rows = super().run(sql, params, max_bytes_billed)
        with self.lock:
            self.log.append((sql, billed_for(sql), max_bytes_billed))
            self.billed += billed_for(sql)
        return rows


def research_that_counts(seen=None, wait_for_store=None):
    """One recorded count, as research does, optionally after waiting for the store read to start."""

    def research(ctx, prompt, options, emit, should_stop):
        if wait_for_store is not None:
            seen["overlapped"] = wait_for_store.wait(1.0)
        sql_query(ctx, options.warehouse, COUNT_SQL, purpose="posts, authors and platforms for amapiano",
                  params={"term": "amapiano"})
        return {"note": "Reading: amapiano.", "tokens": {"input": 1000, "output": 300}, "usd": 0.02}

    return research


def harness(research, store=store_breadth, **kwargs):
    h = Harness(research=research, **kwargs)
    h.warehouse = StoreWarehouse()
    h.deps.warehouse = h.warehouse
    h.deps.store_counts = store
    return h


def serial_reference():
    """What the serial run recorded: research's count, then the three whole-store reads, then the before-window read."""
    window = ask._window(QUESTION, NOW)
    ctx = RunContext(run_id="r_ref", tier="T1", as_of=NOW, market="ZA", window_start=window[0], window_end=window[1])
    wh = StoreWarehouse()
    sql_query(ctx, wh, COUNT_SQL, purpose="posts, authors and platforms for amapiano", params={"term": "amapiano"})
    ids = store_breadth(ctx, wh, ask.question_platforms(QUESTION))
    return ctx, wh, ids


def store_receipts(receipts):
    keep = ("purpose", "sql", "params", "result_hash", "row_count")
    return {qid: {k: r[k] for k in keep} for qid, r in receipts.items() if r["purpose"].startswith("Whole-store")}


def test_the_store_count_runs_while_research_is_still_going():
    seen = {}
    h = harness(None)
    h.deps.research = research_that_counts(seen, wait_for_store=h.warehouse.store_started)

    h.run(question=QUESTION)

    assert seen["overlapped"] is True  # research was still running when the first store read arrived


def test_the_background_count_records_what_the_serial_run_recorded():
    ref_ctx, ref_wh, ref_ids = serial_reference()
    h = harness(research_that_counts())

    out = h.run(question=QUESTION)

    expected = store_receipts(ask.query_receipts(ref_ctx))
    assert len(expected) == 4  # totals, sounds, hashtags and the window before
    assert store_receipts(out["query_receipts"]) == expected  # same ids, SQL, params, purposes and result hashes
    assert out["run"]["store"] == store_totals(ref_ctx, ref_ids["totals"])
    assert {qid for qid, q in ref_ctx.queries.items() if q.get("tool") == STORE_TOOL} == set(expected)
    steps = [e["text"] for e in h.events if e.get("event") == "step"]
    assert sum(t.startswith("Counting every stored post") for t in steps) == 1


def test_the_bytes_billed_for_the_store_reads_equal_the_serial_run_and_no_read_is_lost():
    ref_ctx, ref_wh, _ = serial_reference()
    store_sql = {q["sql"] for q in ref_ctx.queries.values() if q.get("tool") == STORE_TOOL}
    h = harness(research_that_counts())

    h.run(question=QUESTION)

    ran = [entry for entry in h.warehouse.log if entry[0] in store_sql]
    assert len(ran) == len(store_sql) == 4  # each store read ran exactly once
    assert sum(b for _, b, _ in ran) == sum(b for sql, b, _ in ref_wh.log if sql in store_sql)
    assert all(cap == MAX_BYTES_BILLED for _, _, cap in ran)
    assert h.warehouse.billed == sum(b for _, b, _ in h.warehouse.log)  # the ledger lost no update to a race


def test_a_background_count_adds_no_model_call():
    plain = Harness(research=research_that_counts())
    plain.run(question=QUESTION)
    background = harness(research_that_counts())

    background.run(question=QUESTION)

    assert len(background.model.calls) == len(plain.model.calls) > 0
    assert [c["schema"] for c in background.model.calls] == [c["schema"] for c in plain.model.calls]


def test_a_count_that_fails_keeps_the_reads_it_made_and_the_serial_notice():
    def failing(ctx, warehouse, platforms=None):
        sql_query(ctx, warehouse, COUNT_SQL, purpose="the one store read that ran", params={"term": "x"})
        raise RuntimeError("the second store read failed")

    h = harness(research_that_counts(), store=failing)

    out = h.run(question=QUESTION)

    assert ask.STORE_FAILED in out["run"]["notices"]
    assert [r["purpose"] for r in out["query_receipts"].values()][:2] == [
        "posts, authors and platforms for amapiano", "the one store read that ran"]
    assert list(out["query_receipts"])[:2] == ["q_1", "q_2"]  # numbered after research's count, as the serial run did
    assert "store" not in out["run"]


def test_a_stopped_ask_drops_the_count_and_returns_promptly():
    flag = {"stop": False}
    h = harness(make_research(stop_flag=flag), stop_flag=flag)

    out = h.run(question=QUESTION)

    assert out["answer"]["status"] == "insufficient_evidence"
    assert store_receipts(out["query_receipts"]) == {}
    assert "store" not in out["run"]


def test_a_count_started_beside_research_is_not_run_again_by_the_writer_gate():
    calls = []

    def counting(ctx, warehouse, platforms=None):
        calls.append(threading.current_thread() is not threading.main_thread())
        return store_breadth(ctx, warehouse, platforms)

    h = harness(research_that_counts(), store=counting)

    h.run(question=QUESTION)

    assert calls == [True]  # once, on its own thread


def test_the_warehouse_client_is_built_once_when_research_and_the_store_count_ask_for_it_together(monkeypatch):
    """Both start a BigQuery read in the first seconds of an ask, each through the same lazy client."""
    import time

    from google.cloud import bigquery

    from core.agent.tools.sql_query import BigQueryWarehouse

    built = []

    def slow_client(*args, **kwargs):
        time.sleep(0.05)  # long enough for every thread to see no client yet
        built.append(object())
        return built[-1]

    monkeypatch.setattr(bigquery, "Client", slow_client)
    warehouse = BigQueryWarehouse()
    gate = threading.Barrier(6, timeout=WAIT_S)

    def ask_for_client():
        gate.wait()
        warehouse._bq()

    threads = [threading.Thread(target=ask_for_client) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(built) == 1
    assert warehouse._client is built[0]
