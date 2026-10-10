"""The run.timings record (ASK-LATENCY.md, "Timing record for lane 1"): a versioned, numbers-and-enums-only account of
where an Ask's seconds went, stored beside run.phase_seconds in the runs row's record JSON.

Each test takes its expected value from the thing the record is meant to explain (the run's seconds, the budget ledger,
the fake provider's own usage, the fake tool's own delay), never from the record itself.
"""

import json
import threading
import time
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest

from core.agent import ask, gemini_research
from core.agent.context import RunContext
from core.agent.model_budget import AskModelBudget
from core.agent.tests.test_ask import COUNT_SQL, NOW, Harness, make_research
from core.agent.tests.test_ask_model_budget import configure_gemini
from core.agent.tests.test_gemini_research_budget import FakeClient, fcall, ftext, reply, usage
from core.agent.tools.dates import SAST
from core.agent.writer import WRITER_SCHEMA

WAIT_S = 5
SECRET = "ZZQXPLOT"


def through_gemini(monkeypatch, client):
    """A Harness research that runs the real Gemini loop against a fake provider client."""
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    return lambda ctx, prompt, options, emit, should_stop: gemini_research.gemini_research(
        ctx, prompt, options, emit, should_stop)


def research_turns(cached=0, prompts=(800, 1000, 1500), term="amapiano"):
    """Three turns, one tool each so query ids are the same on every run: a stored-post search, a count, the note."""
    return [reply(fcall("search_posts", query=term), metadata=usage(prompt=prompts[0], response=40, thoughts=10)),
            reply(fcall("sql_query", sql=COUNT_SQL, purpose=f"posts and authors for {term}", params={"term": term}),
                  metadata=usage(prompt=prompts[1], response=50, thoughts=20, cached=cached)),
            reply(ftext("Reading: amapiano, South Africa, this week."),
                  metadata=usage(prompt=prompts[2], response=30, thoughts=0))]


def spans(run, kind=None):
    found = run["timings"]["spans"]
    return [s for s in found if kind is None or s["kind"] == kind]


def test_phase_spans_cover_the_run_including_the_time_phase_seconds_leaves_out(monkeypatch):
    configure_gemini(monkeypatch)
    ticks = [0.0]
    monkeypatch.setattr(ask.time, "monotonic", lambda: ticks[0])
    base = make_research(live=False)

    def research(*args):
        ticks[0] += 3
        return base(*args)

    h = Harness(research=research)
    plain_model = h.model.complete_json

    def model(**kwargs):
        if kwargs["schema"] is WRITER_SCHEMA:
            ticks[0] += 4
        return plain_model(**kwargs)

    h.model.complete_json = model
    plain_insert = h.tables.insert

    def insert(table, rows, row_ids=None):
        ticks[0] += 1  # the claim_checks write: in no phase_seconds bucket today
        return plain_insert(table, rows, row_ids)

    h.tables.insert = insert

    run = h.run()["run"]

    assert run["seconds"] == 8
    phases = {s["name"]: s["t1"] - s["t0"] for s in spans(run, "phase")}
    assert run["timings"]["version"] == 1 and run["timings"]["total_s"] == run["seconds"]
    assert sum(phases.values()) == pytest.approx(run["seconds"], abs=0.5)
    assert phases["research"] == pytest.approx(3, abs=0.01)
    assert phases["write"] == pytest.approx(4, abs=0.01)
    assert phases["finalise"] == pytest.approx(1, abs=0.01)
    assert sum(run["phase_seconds"].values()) < run["seconds"]  # the gap the new spans close
    ordered = spans(run, "phase")
    assert all(a["t1"] == b["t0"] for a, b in zip(ordered, ordered[1:]))  # no gap and no overlap between phases
    assert ordered[0]["t0"] == 0


def test_every_model_call_has_a_span_and_the_token_sums_equal_the_budget_ledger(monkeypatch):
    configure_gemini(monkeypatch)
    ledger = []
    settle = AskModelBudget.settle

    def recording(self, reservation, usage_, **kwargs):
        ledger.append(usage_)
        return settle(self, reservation, usage_, **kwargs)

    monkeypatch.setattr(AskModelBudget, "settle", recording)
    client = FakeClient(*research_turns(cached=600))
    h = Harness(research=through_gemini(monkeypatch, client))

    run = h.run()["run"]

    booked = [u for u in ledger if isinstance(u, dict)]
    assert len(ledger) == len(booked) >= 8  # three research turns, the writer, three support calls, the field check
    calls = spans(run, "model_call")
    assert len(calls) == len(ledger)
    assert sum(s["attrs"]["input"] for s in calls) == sum(u["input_tokens"] for u in booked)
    assert sum(s["attrs"]["output"] for s in calls) == sum(u["output_tokens"] for u in booked)
    purposes = [s["attrs"]["purpose"] for s in calls]
    assert purposes.count("research_turn") == 3
    assert {"write", "support", "field_check"} <= set(purposes)
    assert all(s["attrs"]["status"] == "ok" and s["attrs"]["attempt"] == 1 for s in calls)


def test_usage_keeps_the_cached_token_count_and_never_reports_more_than_the_input(monkeypatch):
    from core.llm.gemini import usage_of

    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "1")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "10")

    def response(cached):
        return NS(usage_metadata=NS(prompt_token_count=1000, cached_content_token_count=cached,
                                    response_token_count=40, candidates_token_count=None, thoughts_token_count=10,
                                    tool_use_prompt_token_count=0))

    assert usage_of(response(900), "gemini-3.8-flash")["cached_tokens"] == 900
    assert usage_of(response(1500), "gemini-3.8-flash")["cached_tokens"] == 1000
    assert usage_of(response(None), "gemini-3.8-flash")["cached_tokens"] == 0
    assert usage_of(response(900), "gemini-3.8-flash")["thinking_tokens"] == 10


def test_a_research_turn_span_records_the_cached_tokens_the_provider_reported(monkeypatch):
    configure_gemini(monkeypatch)
    client = FakeClient(*research_turns(cached=900))
    h = Harness(research=through_gemini(monkeypatch, client))

    run = h.run()["run"]

    turns = [s for s in spans(run, "model_call") if s["attrs"]["purpose"] == "research_turn"]
    assert [s["attrs"]["cached"] for s in turns] == [0, 900, 0]
    assert [s["attrs"]["input"] for s in turns] == [800, 1000, 1500]
    assert [s["attrs"]["thinking"] for s in turns] == [10, 20, 0]
    assert all(s["attrs"]["cached"] <= s["attrs"]["input"] for s in spans(run, "model_call"))


def research_in_parallel(monkeypatch, functions, calls):
    from core.agent.timings import Timings

    now = datetime(2026, 9, 29, 8, 0, tzinfo=SAST)
    timings = Timings()
    ctx = RunContext(run_id="r_test", tier="T1", as_of=now, market="ZA", window_start=date(2026, 9, 1),
                     window_end=date(2026, 9, 28), timings=timings)
    progress = ask.Progress([].append, lambda: now, market_label="South Africa",
                            window=(date(2026, 9, 1), date(2026, 9, 28)))
    options = ask.ResearchSetup(system_prompt="You are 42's lead analyst.", model="gemini-3.8-flash",
                                warehouse=None, client=None, tables=None)
    client = FakeClient(reply(*calls), reply(ftext("done")))
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    monkeypatch.setattr(gemini_research, "build_functions", lambda c, *a: functions(c))
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "1")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "10")
    gemini_research.gemini_research(ctx, "Question?", options, progress, lambda: False)
    return timings.snapshot(time.monotonic() - timings.started)


def test_parallel_sql_spans_share_a_batch_overlap_and_the_batch_takes_as_long_as_its_slowest_member(monkeypatch):
    delays = {"first": 0.2, "second": 1.2, "third": 0.4}
    barrier = threading.Barrier(3, timeout=WAIT_S)

    def functions(bound):
        def sql_query(sql, purpose, **_):
            barrier.wait()  # only opens when all three are running at the same time
            time.sleep(delays[purpose])
            return {"rows": [], "row_count": 7, "bytes": 1234, "query_id": None, "purpose": purpose}

        return {"sql_query": sql_query}

    sql = "SELECT COUNT(*) AS posts FROM intelligence_42_core.posts p WHERE p.post_date = DATE('2026-09-28')"
    snapshot = research_in_parallel(monkeypatch, functions,
                                    [fcall("sql_query", sql=sql, purpose=name) for name in delays])

    tools = [s for s in snapshot["spans"] if s["kind"] == "tool_call"]
    assert len(tools) == 3
    assert len({s["attrs"]["batch"] for s in tools}) == 1
    assert sorted(s["attrs"]["lane"] for s in tools) == [0, 1, 2]
    assert max(s["t0"] for s in tools) < min(s["t1"] for s in tools)  # every member started before any finished
    wall = max(s["t1"] for s in tools) - min(s["t0"] for s in tools)
    longest = max(s["t1"] - s["t0"] for s in tools)
    assert wall == pytest.approx(longest, abs=0.35)  # margins are wide so a busy machine does not fail it
    assert wall >= delays["second"] - 0.01
    assert wall < sum(delays.values()) - 0.2  # not the serial sum
    assert {s["attrs"]["rows"] for s in tools} == {7} and {s["attrs"]["bytes"] for s in tools} == {1234}
    assert {s["attrs"]["status"] for s in tools} == {"ok"}


def test_calls_in_separate_batches_get_different_batch_ids(monkeypatch):
    def functions(bound):
        return {"sql_query": lambda sql, purpose, **_: {"row_count": 1, "bytes": 1},
                "socialcrawl_call": lambda **_: {"items": []}}

    sql = "SELECT COUNT(*) AS posts FROM intelligence_42_core.posts p WHERE p.post_date = DATE('2026-09-28')"
    snapshot = research_in_parallel(monkeypatch, functions, [
        fcall("sql_query", sql=sql, purpose="one"),
        fcall("socialcrawl_call", platform="tiktok", endpoint="search", params={"query": "amapiano"}, max_credits=1),
        fcall("sql_query", sql=sql, purpose="two")])

    tools = [s for s in snapshot["spans"] if s["kind"] == "tool_call"]
    assert [s["attrs"]["tool"] for s in tools] == ["sql_query", "socialcrawl_call", "sql_query"]
    assert len({s["attrs"]["batch"] for s in tools}) == 3


def test_every_string_in_the_record_is_in_a_fixed_enum_and_no_question_or_query_text_enters(monkeypatch):
    from core.agent import timings

    configure_gemini(monkeypatch)
    first, second, last = research_turns(cached=100, term=SECRET)
    first.candidates[0].content.parts.append(fcall("not_a_tool", note=SECRET))
    client = FakeClient(first, second, last)
    h = Harness(research=through_gemini(monkeypatch, client))

    run = h.run(question=f"What is behind {SECRET} in South Africa this week?")["run"]

    record = run["timings"]
    assert SECRET.lower() not in json.dumps(record).lower()
    strings, keys = [], set()

    def walk(node):
        if isinstance(node, dict):
            keys.update(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            strings.append(node)

    walk(record)
    assert strings and set(strings) <= timings.ENUM_VALUES
    assert keys <= timings.KEYS
    assert "other" in strings  # the unknown tool the model named
    assert all(isinstance(v, (int, float)) or isinstance(v, str)
               for s in record["spans"] for v in s["attrs"].values())


@pytest.mark.parametrize("broken", ["_open", "_close", "_snapshot"])
def test_a_failure_inside_the_timer_is_swallowed_and_every_check_still_runs(monkeypatch, broken):
    configure_gemini(monkeypatch)
    expected = Harness()
    expected_out = expected.run()
    rows = [(r["claim_id"], r["rule"], r["verdict"], r["checker"])
            for _, inserted in expected.tables.inserts for r in inserted]
    assert rows

    from core.agent import timings

    def boom(*args, **kwargs):
        raise RuntimeError("timer broke")

    monkeypatch.setattr(timings.Timings, broken, boom)
    h = Harness()
    out = h.run()

    assert [(r["claim_id"], r["rule"], r["verdict"], r["checker"])
            for _, inserted in h.tables.inserts for r in inserted] == rows
    assert [c["id"] for c in out["answer"]["claims"]] == [c["id"] for c in expected_out["answer"]["claims"]]
    assert out["answer"]["status"] == expected_out["answer"]["status"]
    assert out["run"]["tokens"] == expected_out["run"]["tokens"]
    assert json.loads(json.dumps(out["run"]["timings"]))["version"] == 1


def test_a_legacy_run_without_timings_reads_without_error():
    from core.agent import timings

    legacy = {"run_id": "r_old", "seconds": 150.1, "phase_seconds": {"plan": 1.3}}
    assert timings.read(legacy) is None
    assert timings.read({**legacy, "timings": None}) is None
    assert timings.read({**legacy, "timings": "garbled"}) is None
    assert timings.read({**legacy, "timings": {"version": 2, "spans": []}}) is None
    current = {"version": 1, "total_s": 1.0, "spans": [], "dropped": 0}
    assert timings.read({**legacy, "timings": current}) == current
    assert timings.read(None) is None


def test_spans_are_capped_and_the_overflow_is_counted_not_stored():
    from core.agent.timings import MAX_SPANS, Timings

    timings = Timings()
    for _ in range(MAX_SPANS + 50):
        timings.begin("io", "store_count").end()
    record = timings.snapshot(1.0)
    assert len(record["spans"]) <= MAX_SPANS
    assert record["dropped"] >= 50
    assert [s["name"] for s in record["spans"] if s["kind"] == "phase"] == ["setup"]  # phases are never crowded out


def test_a_failed_ask_keeps_its_timings_and_drops_them_before_it_drops_its_spend():
    from core.api import agent_app

    record = {"version": 1, "total_s": 5.0, "spans": [], "dropped": 0}
    kept = agent_app.failed_run({"run_id": "r_x", "tier": "T1", "model_usd": 0.5, "credits": 0, "timings": record})
    assert kept["timings"] == record
    huge = {"version": 1, "total_s": 5.0, "dropped": 0, "spans": [{"n": i, "kind": "io", "name": "store_count",
                                                                   "t0": 0, "t1": 1, "attrs": {}}
                                                                  for i in range(5000)]}
    assert len(json.dumps(huge)) > agent_app.MAX_RUN_BYTES
    trimmed = agent_app.failed_run({"run_id": "r_x", "tier": "T1", "model_usd": 0.5, "credits": 0, "timings": huge})
    assert trimmed["run_id"] == "r_x" and trimmed["model_usd"] == 0.5 and "timings" not in trimmed


def test_a_numeric_repair_is_one_numeric_repair_call_inside_the_numeric_repair_phase_after_a_prepare_phase():
    import copy

    from core.agent.tests.test_ask import WRITER_OUT, NumericRepairModel

    draft = copy.deepcopy(WRITER_OUT)
    draft["claims"][1]["text"] += " 20% faster."
    model = NumericRepairModel([draft, copy.deepcopy(WRITER_OUT)])
    h = Harness(check=None, model=model)
    h.deps.store_counts = lambda ctx, warehouse, platforms=None: {}

    run = h.run()["run"]

    assert len([c for c in model.calls if c["schema"] is WRITER_SCHEMA]) == 2  # counted at the model, not in the record
    calls = spans(run, "model_call")
    repairs = [s for s in calls if s["name"] == "numeric_repair"]
    writes = [s for s in calls if s["name"] == "write"]
    assert len(repairs) == 1 and len(writes) == 1
    assert repairs[0]["attrs"]["purpose"] == "numeric_repair" and writes[0]["attrs"]["purpose"] == "write"
    phases = spans(run, "phase")
    repair_phase = next(s for s in phases if s["name"] == "numeric_repair")
    write_phase = next(s for s in phases if s["name"] == "write")
    assert repair_phase["t0"] <= repairs[0]["t0"] and repairs[0]["t1"] <= repair_phase["t1"]
    assert write_phase["t0"] <= writes[0]["t0"] and writes[0]["t1"] <= write_phase["t1"]
    names = [s["name"] for s in phases]
    assert names.index("prepare") == names.index("store_count") + 1 and names.index("write") == names.index("prepare") + 1
    assert names.index("numeric_repair") > names.index("write")


def test_a_run_with_nothing_to_repair_has_no_numeric_repair_call_or_phase():
    run = Harness().run()["run"]

    assert not [s for s in spans(run, "model_call") if s["name"] == "numeric_repair"]
    assert "numeric_repair" not in [s["name"] for s in spans(run, "phase")]


def _api_error(cls, code, usage=None):
    exc = cls(code, {"error": {"message": "x", "status": "X"}})
    if usage is not None:
        exc.usage = usage
    return exc


@pytest.mark.parametrize("make, expected", [
    (lambda: __import__("core.llm.deadline", fromlist=["x"]).DeadlineExpired(), "timeout"),
    (lambda: __import__("httpx").ReadTimeout("read timed out"), "timeout"),
    (lambda: _api_error(__import__("google.genai.errors", fromlist=["x"]).ServerError, 504), "timeout"),
    (lambda: _api_error(__import__("google.genai.errors", fromlist=["x"]).ClientError, 408), "timeout"),
    (lambda: _api_error(__import__("google.genai.errors", fromlist=["x"]).ClientError, 429), "busy"),
    (lambda: _api_error(__import__("google.genai.errors", fromlist=["x"]).ClientError, 429,
                        usage={"input_tokens": 5}), "error"),
    (lambda: _api_error(__import__("google.genai.errors", fromlist=["x"]).ServerError, 500), "error"),
    (lambda: type("DeadlineExceeded", (Exception,), {})("provider deadline"), "timeout"),
    (lambda: RuntimeError("returned no text"), "error"),
])
def test_failure_status_names_how_a_failed_call_ended(make, expected):
    from core.agent import timings

    assert timings.failure_status(make()) == expected


def test_span_token_sums_equal_the_ledger_when_usage_is_unknown_and_the_budget_books_the_full_reserve(monkeypatch):
    from core.agent.tests.test_ask_model_budget import _IncompleteChecksModel

    configure_gemini(monkeypatch)
    ledger = []
    settle = AskModelBudget.settle

    def recording(self, reservation, usage_, **kwargs):
        ledger.append((reservation, usage_))
        return settle(self, reservation, usage_, **kwargs)

    monkeypatch.setattr(AskModelBudget, "settle", recording)
    h = Harness(model=_IncompleteChecksModel())

    run = h.run()["run"]

    unknown = [(r, u) for r, u in ledger if u is None]
    assert unknown and len(unknown) < len(ledger)  # the checks after the writer, and the writer itself
    booked_in = sum(u["input_tokens"] if u is not None else r.input_bound for r, u in ledger)
    booked_out = sum(u["output_tokens"] if u is not None else r.output_bound for r, u in ledger)
    calls = spans(run, "model_call")
    assert len(calls) == len(ledger)
    assert sum(s["attrs"]["input"] for s in calls) == booked_in
    assert sum(s["attrs"]["output"] for s in calls) == booked_out
    assert sum(1 for s in calls if s["attrs"].get("reserved") == 1) == len(unknown)
