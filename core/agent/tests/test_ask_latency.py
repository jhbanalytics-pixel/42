"""Ask latency: independent warehouse work runs at the same time, with the same checks and the same results.

Each test holds its fake calls on a barrier that only opens when the work it names runs together, so a sequential run
fails on the barrier's timeout instead of passing slowly.
"""

import copy
import threading
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest

from core.agent import ask, checks, gemini_research, writer
from core.agent.context import RunContext
from core.agent.tools.dates import SAST
from core.agent.tests.test_checks import (RUN, SQL, ROWS, STORED, WINDOW, AS_OF, make_ctx, make_draft, number,
                                          verdict)

WAIT_S = 5


class BarrierWarehouse:
    """run() waits until `parties` re-runs are in flight together."""

    def __init__(self, parties, rows_by_param=None, fail_param=None):
        self.barrier = threading.Barrier(parties, timeout=WAIT_S)
        self.rows_by_param = rows_by_param or {}
        self.fail_param = fail_param
        self.calls = []
        self.lock = threading.Lock()

    def dry_run(self, sql, params):
        raise AssertionError("checks never dry-run")

    def run(self, sql, params, max_bytes_billed):
        with self.lock:
            self.calls.append((sql, dict(params), max_bytes_billed))
        self.barrier.wait()
        tag = params.get("tag")
        if tag == self.fail_param:
            raise RuntimeError("warehouse unavailable")
        return copy.deepcopy(self.rows_by_param.get(tag, ROWS))


def two_query_ctx():
    ctx = make_ctx()
    ctx.record_query(SQL, {"tag": "sundaylunch"}, copy.deepcopy(ROWS), "daily #sundaylunch posts and ratio")
    return ctx


def second_number(ctx, value, unit):
    q = ctx.queries["q_2"]
    return {"value": value, "unit": unit, "query_id": "q_2", "run_id": RUN, "result_hash": q["result_hash"]}


def two_query_draft(ctx):
    draft = make_draft(ctx)
    draft["claims"][0]["numbers"] = [number(ctx, 412, "posts on 27 September"),
                                     second_number(ctx, 2.8, "times the usual daily count")]
    return draft


def test_k2_reruns_distinct_queries_at_the_same_time():
    ctx = two_query_ctx()
    wh = BarrierWarehouse(2)
    answer, verdicts = checks.check_answer(two_query_draft(ctx), ctx, wh, window=WINDOW, markets=["ZA"])
    assert verdict(verdicts, "c1", "K2")["verdict"] == "pass"
    assert sorted(c[1]["tag"] for c in wh.calls) == ["7colours", "sundaylunch"]  # each query once


def test_k2_concurrent_rerun_failure_still_cuts_the_claim():
    ctx = two_query_ctx()
    wh = BarrierWarehouse(2, fail_param="sundaylunch")
    _, verdicts = checks.check_answer(two_query_draft(ctx), ctx, wh, window=WINDOW, markets=["ZA"])
    row = verdict(verdicts, "c1", "K2")
    assert row["verdict"] == "cut" and "re-run of q_2 failed (RuntimeError)" in row["reason"]


def test_k2_concurrent_rerun_that_no_longer_returns_the_number_still_cuts():
    ctx = two_query_ctx()
    changed = [{"day": "2026-09-27", "posts": 9, "ratio": 1.1}]
    wh = BarrierWarehouse(2, rows_by_param={"sundaylunch": changed})
    _, verdicts = checks.check_answer(two_query_draft(ctx), ctx, wh, window=WINDOW, markets=["ZA"])
    row = verdict(verdicts, "c1", "K2")
    assert row["verdict"] == "cut" and "re-run of q_2 no longer returns it" in row["reason"]


def test_writer_numeral_scan_reruns_distinct_queries_at_the_same_time():
    ctx = two_query_ctx()
    wh = BarrierWarehouse(2)
    issues = writer.unpinned_claim_numerals(two_query_draft(ctx), ctx, wh, window=WINDOW)
    assert issues == []
    assert len(wh.calls) == 2


def test_a_number_that_fails_before_its_rerun_is_never_rerun():
    """The prefetch takes only the numbers _number_problem would re-run: a bad hash never reaches the warehouse."""
    ctx = two_query_ctx()
    draft = two_query_draft(ctx)
    draft["claims"][0]["numbers"][1]["result_hash"] = "sha256:" + "0" * 64
    wh = BarrierWarehouse(1)
    _, verdicts = checks.check_answer(draft, ctx, wh, window=WINDOW, markets=["ZA"])
    assert [c[1]["tag"] for c in wh.calls] == ["7colours"]
    assert verdict(verdicts, "c1", "K2")["verdict"] == "cut"


# The research loop: read-only calls the model sends in one turn run together.

def fcall(name, call_id, **args):
    return NS(function_call=NS(id=call_id, name=name, args=args), text=None, thought=False)


def ftext(text):
    return NS(function_call=None, text=text, thought=False)


def reply(*parts):
    return NS(candidates=[NS(content=NS(role="model", parts=list(parts)), finish_reason=NS(name="STOP"))],
              usage_metadata=NS(prompt_token_count=100, response_token_count=10, thoughts_token_count=5,
                                tool_use_prompt_token_count=0, candidates_token_count=None))


class FakeClient:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.models = NS(generate_content=self.generate)

    def generate(self, **kw):
        self.calls.append({**kw, "contents": list(kw["contents"])})
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def gemini_env(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "1")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "10")


def research(monkeypatch, client, functions):
    now = datetime(2026, 9, 29, 8, 0, tzinfo=SAST)
    ctx = RunContext(run_id="r_test", tier="T1", as_of=now, market="ZA", window_start=date(2026, 9, 1),
                     window_end=date(2026, 9, 28))
    events = []
    progress = ask.Progress(events.append, lambda: now, market_label="South Africa",
                            window=(date(2026, 9, 1), date(2026, 9, 28)))
    options = ask.ResearchSetup(system_prompt="You are 42's lead analyst.", model="gemini-3.8-flash",
                                warehouse=None, client=None, tables=None)
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    monkeypatch.setattr(gemini_research, "build_functions", lambda c, *a: functions(c))
    result = gemini_research.gemini_research(ctx, "Question?", options, progress, lambda: False)
    return result, ctx, events


def post(eid, text):
    return {"id": eid, "platform": "TikTok", "text": text}


def test_read_calls_in_one_turn_run_together_and_fold_back_in_call_order(monkeypatch):
    barrier = threading.Barrier(2, timeout=WAIT_S)

    def functions(bound):
        def recall_findings(query, **_):
            barrier.wait()
            bound.evidence["tt_1"] = post("tt_1", "first")
            bound.model_usd_extra += 0.25
            bound.emit("sql", query_id="q_a", purpose="recall")
            return {"findings": [], "who": "recall"}

        def rising_topics(**_):
            barrier.wait()
            bound.evidence["tt_2"] = post("tt_2", "second")
            bound.evidence["tt_1"] = post("tt_1", "second wins, as it would in order")
            bound.model_usd_extra += 0.5
            bound.emit("sql", query_id="q_b", purpose="rising")
            return {"topics": [], "who": "rising"}

        return {"recall_findings": recall_findings, "rising_topics": rising_topics}

    client = FakeClient(reply(fcall("recall_findings", "a", query="amapiano"), fcall("rising_topics", "b")),
                        reply(ftext("Reading: done.")))
    result, ctx, events = research(monkeypatch, client, functions)

    returned = client.calls[1]["contents"][-1].parts
    assert [p.function_response.name for p in returned] == ["recall_findings", "rising_topics"]
    assert [p.function_response.id for p in returned] == ["a", "b"]
    assert '"who": "recall"' in returned[0].function_response.response["output"]
    assert list(ctx.evidence) == ["tt_1", "tt_2"]
    assert ctx.evidence["tt_1"]["text"] == "second wins, as it would in order"
    assert ctx.model_usd_extra == pytest.approx(0.75)
    assert [e["query_id"] for e in ctx.events if e.get("step") == "sql"] == ["q_a", "q_b"]
    steps = [e["text"] for e in events if e.get("event") == "step"]
    assert steps[0].startswith("Checking saved findings") and steps[1].startswith("Reading rising topics")
    assert result["note"] == "Reading: done."


def test_a_live_call_never_runs_alongside_another_call(monkeypatch):
    active, peak, lock = [0], [0], threading.Lock()

    def tracked(out):
        def fn(**_):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                threading.Event().wait(0.05)
                return out
            finally:
                with lock:
                    active[0] -= 1
        return fn

    def functions(bound):
        return {"recall_findings": tracked({"findings": []}), "socialcrawl_call": tracked({"items": []}),
                "rising_topics": tracked({"topics": []})}

    client = FakeClient(reply(fcall("recall_findings", "a", query="amapiano"),
                              fcall("socialcrawl_call", "b", platform="tiktok", endpoint="search",
                                    params={"query": "amapiano"}, max_credits=1),
                              fcall("rising_topics", "c")),
                        reply(ftext("done")))
    result, ctx, events = research(monkeypatch, client, functions)
    assert peak[0] == 1
    names = [p.function_response.name for p in client.calls[1]["contents"][-1].parts]
    assert names == ["recall_findings", "socialcrawl_call", "rising_topics"]


def test_a_failing_read_call_in_a_batch_reports_its_error_and_the_others_still_return(monkeypatch):
    barrier = threading.Barrier(2, timeout=WAIT_S)

    def functions(bound):
        def recall_findings(query, **_):
            barrier.wait()
            raise RuntimeError("recall table missing")

        def rising_topics(**_):
            barrier.wait()
            return {"topics": ["amapiano"]}

        return {"recall_findings": recall_findings, "rising_topics": rising_topics}

    client = FakeClient(reply(fcall("recall_findings", "a", query="amapiano"), fcall("rising_topics", "b")),
                        reply(ftext("done")))
    _, _, events = research(monkeypatch, client, functions)
    parts = client.calls[1]["contents"][-1].parts
    assert "recall table missing" in parts[0].function_response.response["error"]
    assert "amapiano" in parts[1].function_response.response["output"]
    assert any(e.get("event") == "step" and e["text"] == "Checking saved findings did not run" for e in events)
