"""Ask speed, fix 1 (ASK-LATENCY.md): independent counts go out in one research turn.

414 of 683 tool-bearing research turns in 59 staging Asks held a single sql_query, each turn re-sending the whole
history. The loop already runs consecutive read-only calls at the same time (gemini_research.PARALLEL_TOOLS); the model
has to be told to send them together. These tests pin the instruction, and prove that a batched turn changes nothing
about how each query is checked: the same guard, the same byte cap, the same recorded result, in fewer model calls.
"""

import threading
from datetime import date, datetime

import pytest

from core.agent import ask, gemini_research, skills
from core.agent.context import RunContext
from core.agent.tests.test_gemini_research_budget import FakeClient, fcall, ftext, reply
from core.agent.tools.dates import SAST
from core.agent.tools.sql_query import MAX_BYTES_BILLED
from core.agent.toolset import DESCRIPTIONS

WAIT_S = 5
GOOD = "SELECT COUNT(*) AS posts FROM intelligence_42_core.posts p WHERE p.platform = '{p}' AND p.post_date = DATE('2026-09-28')"
OFF_LIMITS = "SELECT table_name FROM intelligence_42_core.INFORMATION_SCHEMA.TABLES"
HUGE = "SELECT COUNT(*) AS posts FROM intelligence_42_core.posts p /* HUGE */"
PLATFORMS = ("tiktok", "x", "instagram", "youtube")


def test_the_research_prompt_tells_the_model_to_send_independent_counts_in_one_turn():
    prompt = ask.render_skill(as_of=datetime(2026, 10, 8, 8, tzinfo=SAST), markets=["ZA"],
                              window=(date(2026, 10, 1), date(2026, 10, 7)), tier="T1", skill=skills.DEFAULT)
    flat = " ".join(prompt.split())
    assert "separate sql_query calls in the same turn" in flat
    assert "up to four run at the same time" in flat
    assert "only a query that needs another query's result waits" in flat.lower()


def test_the_sql_query_tool_description_says_independent_counts_belong_in_one_turn():
    flat = " ".join(DESCRIPTIONS["sql_query"].split())
    assert "separate sql_query calls in the same turn" in flat
    assert "up to four run at the same time" in flat
    assert "each is checked and capped as if it ran alone" in flat


class CountingWarehouse:
    """Dry runs report the bytes a query would scan; runs wait until the parties are all running at once."""

    def __init__(self, parties=0):
        self.barrier = threading.Barrier(parties, timeout=WAIT_S) if parties else None
        self.lock = threading.Lock()
        self.dry_runs, self.runs = [], []

    def dry_run(self, sql, params):
        with self.lock:
            self.dry_runs.append(sql)
        return {"bytes": MAX_BYTES_BILLED + 1 if "HUGE" in sql else 1000,
                "tables": ["ogilvy-trends-v2.intelligence_42_core.posts"]}

    def run(self, sql, params, max_bytes_billed):
        with self.lock:
            self.runs.append((sql, max_bytes_billed))
        if self.barrier is not None:
            self.barrier.wait()
        return [{"posts": 40 + len(sql) % 7}]


def research(monkeypatch, warehouse, turns):
    now = datetime(2026, 9, 29, 8, 0, tzinfo=SAST)
    ctx = RunContext(run_id="r_batch", tier="T1", as_of=now, market="ZA", window_start=date(2026, 9, 1),
                     window_end=date(2026, 9, 28))
    progress = ask.Progress([].append, lambda: now, market_label="South Africa",
                            window=(date(2026, 9, 1), date(2026, 9, 28)))
    options = ask.ResearchSetup(system_prompt="You are 42's lead analyst.", model="gemini-3.8-flash",
                                warehouse=warehouse, client=None, tables=None)
    client = FakeClient(*turns, reply(ftext("Reading: done.")))
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "1")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "10")
    result = gemini_research.gemini_research(ctx, "Question?", options, progress, lambda: False)
    return result, ctx, client


def call(call_id, sql, purpose, **extra):
    part = fcall("sql_query", sql=sql, purpose=purpose, **extra)
    part.function_call.id = call_id
    return part


def count(platform, **extra):
    return call(platform, GOOD.format(p=platform), f"posts on {platform}", **extra)


def test_one_turn_of_counts_runs_them_together_through_the_same_guard_and_byte_cap(monkeypatch):
    warehouse = CountingWarehouse(parties=len(PLATFORMS))  # opens only when all four counts are running at once
    turn = reply(count("tiktok"), count("x"),
                 call("off_limits", OFF_LIMITS, "every table"),
                 count("instagram", max_bytes_billed=10**12),  # the model asks for more than the cap allows
                 call("too_wide", HUGE, "too wide a scan"),
                 count("youtube"))
    result, ctx, client = research(monkeypatch, warehouse, [turn])

    assert len(client.calls) == 2  # one turn of counts, one finishing note
    answers = {p.function_response.id: p.function_response for p in client.calls[1]["contents"][-1].parts}
    assert len(answers) == 6
    assert {name for name, a in answers.items() if "output" in a.response} == set(PLATFORMS)
    refused = [a.response["error"] for a in answers.values() if "error" in a.response]
    assert len(refused) == 2
    assert any("INFORMATION_SCHEMA" in e for e in refused)  # the same read-only guard, before any warehouse call
    assert any("byte cap" in e for e in refused)  # the same dry-run cap, before any query is billed
    assert OFF_LIMITS not in warehouse.dry_runs
    assert [sql for sql, _ in warehouse.runs if "HUGE" in sql] == []
    assert len(warehouse.runs) == len(PLATFORMS)
    assert all(cap <= MAX_BYTES_BILLED for _, cap in warehouse.runs)  # 10**12 was cut to the cap
    assert len(ctx.queries) == len(PLATFORMS)


def test_batching_changes_the_number_of_model_calls_and_nothing_else(monkeypatch):
    batched_wh = CountingWarehouse(parties=len(PLATFORMS))
    _, batched_ctx, batched_client = research(monkeypatch, batched_wh, [reply(*[count(p) for p in PLATFORMS])])
    serial_wh = CountingWarehouse()
    _, serial_ctx, serial_client = research(monkeypatch, serial_wh, [reply(count(p)) for p in PLATFORMS])

    assert len(batched_client.calls) == 2 < len(serial_client.calls) == 5

    def recorded(ctx):
        return {q["sql"]: (q["rows"], q["result_hash"], q["purpose"]) for q in ctx.queries.values()}

    assert recorded(batched_ctx) == recorded(serial_ctx)
    assert sorted(batched_wh.runs) == sorted(serial_wh.runs)  # the same SQL under the same byte cap
    assert sorted(batched_wh.dry_runs) == sorted(serial_wh.dry_runs)
