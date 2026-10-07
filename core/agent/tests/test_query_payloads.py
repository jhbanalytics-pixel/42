import copy
import json
from datetime import datetime

import pytest

from core.agent import ask, gemini_research, toolset, writer
from core.agent.context import RunContext, result_hash
from core.agent.tests.test_gemini_research import FakeClient, fcall, ftext, reply, setup_for
from core.agent.tools.sql_query import sql_query

SQL = "SELECT cohort, posts FROM intelligence_42_core.posts WHERE geo_market = @market"
PARAMS = {"market": "ZA"}


class FrozenWarehouse:
    def __init__(self, rows):
        self.rows = rows
        self.dry_runs = self.runs = 0

    def dry_run(self, sql, params):
        self.dry_runs += 1
        return {"bytes": 1234, "tables": ["intelligence_42_core.posts"]}

    def run(self, sql, params, max_bytes_billed):
        self.runs += 1
        assert self.runs == 1
        return self.rows


def frozen_rows():
    return [{"row": i, "cohort": "ZA north" if i % 2 else "ZA south",
             "posts": 7 if i in (12, 498) else 1000 + i, "text": "recorded facts " * 10}
            for i in range(500)]


def execute(ctx, functions, name, **args):
    text, failed = gemini_research._run_call(ctx, functions, name, args)
    assert not failed, text
    return json.loads(text)


@pytest.mark.parametrize("reverse", [False, True])
def test_query_preview_keeps_recorded_order_and_every_row_retrievable(reverse):
    rows = frozen_rows()[:: -1 if reverse else 1]
    ctx = RunContext(run_id="frozen", tier="T1", as_of=datetime(2026, 10, 7))
    wh = FrozenWarehouse(rows)
    functions = toolset.build_functions(ctx, wh, None, None)
    preview = execute(ctx, functions, "sql_query", sql=SQL, purpose="posts by cohort", params=PARAMS)
    assert preview["rows"] == rows[:10]
    assert preview["query_id"] == "q_1" and preview["row_count"] == preview["recorded_row_count"] == 500
    assert preview["columns"] == ["cohort", "posts", "row", "text"]
    assert preview["preview"] == {"offset": 0, "limit": 10, "returned": 10,
                                  "has_more": True, "next_offset": 10}
    assert preview["result_hash"] == result_hash(rows)
    assert preview["bytes"] == 1234 and preview["truncated"] is False
    before = copy.deepcopy(ctx.queries)
    writer_before = writer._pack(ctx, [])
    fetched = []
    for offset in range(0, 500, 50):
        page = execute(ctx, functions, "query_rows", query_id="q_1", offset=offset, limit=50)
        assert page["preview"]["offset"] == offset and page["preview"]["returned"] == 50
        assert page["result_hash"] == preview["result_hash"]
        fetched.extend(page["rows"])
    assert fetched == rows and fetched[-1] == rows[499]
    assert ctx.queries == before and ctx.queries["q_1"]["rows"] is rows
    assert writer._pack(ctx, []) == writer_before
    assert wh.dry_runs == wh.runs == 1
    assert ctx.calls_made == ctx.credits_spent == 0


def test_query_rows_keeps_tail_cohort_and_scope_for_writer_checks():
    rows = frozen_rows()
    ctx = RunContext(run_id="frozen", tier="T1", as_of=datetime(2026, 10, 7))
    wh = FrozenWarehouse(rows)
    functions = toolset.build_functions(ctx, wh, None, None)
    execute(ctx, functions, "sql_query", sql=SQL, purpose="posts by cohort", params=PARAMS)
    claim = {"text": "ZA north has 1499 posts", "numbers": [{"value": 1499, "unit": "posts", "query_id": "q_1"}]}
    before = writer._claim_query_scope(claim, ctx.queries)
    page = execute(ctx, functions, "query_rows", query_id="q_1", offset=499, limit=1)
    assert page["rows"] == [rows[499]]
    assert page["preview"]["has_more"] is False and page["preview"]["next_offset"] is None
    assert writer._claim_query_scope(claim, ctx.queries) == before
    assert before["queries"]["q_1"]["sql"] == SQL and before["queries"]["q_1"]["params"] == PARAMS
    assert before["numbers"][0]["matching_rows"] == [rows[499]]
    duplicate = execute(ctx, functions, "query_rows", query_id="q_1", offset=498, limit=1)
    assert duplicate["rows"] == [rows[498]] and duplicate["rows"][0]["posts"] == rows[12]["posts"]
    assert duplicate["rows"][0]["cohort"] != rows[499]["cohort"]


@pytest.mark.parametrize("args", [
    {"query_id": "q_foreign"}, {"query_id": "q_1", "offset": -1},
    {"query_id": "q_1", "offset": 501}, {"query_id": "q_1", "offset": 1.5},
    {"query_id": "q_1", "offset": True}, {"query_id": "q_1", "limit": 0},
    {"query_id": "q_1", "limit": 51}, {"query_id": "q_1", "limit": 1.5},
    {"query_id": "q_1", "unexpected": "x"},
])
def test_query_rows_refuses_invalid_requests_without_a_warehouse_call(args):
    ctx = RunContext(run_id="frozen", tier="T1", as_of=datetime(2026, 10, 7))
    ctx.record_query(SQL, PARAMS, frozen_rows(), "posts by cohort")
    wh = FrozenWarehouse([])
    functions = toolset.build_functions(ctx, wh, None, None)
    text, failed = gemini_research._run_call(ctx, functions, "query_rows", args)
    assert failed and text.startswith("Refused:"), text
    assert wh.dry_runs == wh.runs == 0


@pytest.mark.parametrize("rows", [[], [{"n": 1}], [{"i": i} for i in range(501)]])
def test_preview_reports_empty_small_and_truncated_recorded_results(rows):
    ctx = RunContext(run_id="frozen", tier="T1", as_of=datetime(2026, 10, 7))
    wh = FrozenWarehouse(rows)
    functions = toolset.build_functions(ctx, wh, None, None)
    out = execute(ctx, functions, "sql_query", sql=SQL, purpose="posts by cohort", params=PARAMS)
    assert out["row_count"] == min(500, len(rows)) and out["recorded_row_count"] == len(rows)
    assert out["preview"]["returned"] == min(10, len(rows))
    assert out["truncated"] is (len(rows) > 500)
    end = execute(ctx, functions, "query_rows", query_id="q_1", offset=min(500, len(rows)))
    assert end["rows"] == [] and end["preview"]["next_offset"] is None
    if len(rows) > 500:
        assert execute(ctx, functions, "query_rows", query_id="q_1", offset=499)["rows"] == rows[499:500]
        assert ctx.queries["q_1"]["rows"] == rows


def test_sql_query_python_contract_stays_full_and_unrecorded_signals_are_not_compacted():
    rows = frozen_rows()
    ctx = RunContext(run_id="frozen", tier="T1", as_of=datetime(2026, 10, 7))
    full = sql_query(ctx, FrozenWarehouse(rows), SQL, "posts by cohort", PARAMS)
    assert full["rows"] == rows and "preview" not in full
    wh = FrozenWarehouse(rows)
    wh.dry_run = lambda sql, params: {"bytes": 1234, "tables": ["intelligence_42_core.google_search_signals"]}
    functions = toolset.build_functions(ctx, wh, None, None)
    signal = execute(ctx, functions, "sql_query", sql=SQL, purpose="search context", params=PARAMS)
    assert signal["query_id"] is None and signal["rows"] == rows and "preview" not in signal


def test_query_rows_cannot_read_another_questions_record():
    first = RunContext(run_id="first", tier="T1", as_of=datetime(2026, 10, 7))
    first.record_query(SQL, PARAMS, frozen_rows(), "posts by cohort")
    other = RunContext(run_id="other", tier="T1", as_of=first.as_of)
    wh = FrozenWarehouse([])
    functions = toolset.build_functions(other, wh, None, None)
    text, failed = gemini_research._run_call(other, functions, "query_rows", {"query_id": "q_1"})
    assert failed and text.startswith("Refused:")
    assert other.queries == {} and wh.dry_runs == wh.runs == 0


def test_cached_pages_run_on_shared_read_lanes_without_changing_records():
    ctx, options, progress, events = setup_for()
    rows = frozen_rows()
    ctx.record_query(SQL, PARAMS, rows, "posts by cohort")
    options.warehouse = FrozenWarehouse([])
    results = gemini_research._run_together(ctx, options, [
        ("query_rows", {"query_id": "q_1", "offset": 499, "limit": 1}),
        ("query_rows", {"query_id": "q_1", "offset": 12, "limit": 1}),
    ])
    assert [json.loads(out)["rows"] for ((out, failed), lane) in results] == [[rows[499]], [rows[12]]]
    assert not any(failed for ((out, failed), lane) in results)
    assert ctx.queries["q_1"]["rows"] is rows
    assert options.warehouse.dry_runs == options.warehouse.runs == 0


@pytest.mark.parametrize("name", ["query_rows", "mcp__f42__query_rows"])
def test_query_rows_progress_uses_plain_text_without_query_content(name):
    ctx, options, progress, events = setup_for()
    kind, text, platform = ask.describe_tool(name, {
        "query_id": "q_private_499", "offset": 499, "limit": 1,
        "sql": "SELECT private_value FROM intelligence_42_core.posts", "rows": [{"posts": 1499}],
    }, progress)
    progress.step(kind, text, platform=platform)
    assert events[-1]["text"] == "Reading saved query results"
    assert events[-1]["kind"] == "read" and events[-1]["platform"] is None


def test_same_question_frozen_replay_compacts_repeated_history_without_changing_writer_payload(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    rows = frozen_rows()
    ctx, options, progress, events = setup_for()
    options.warehouse = FrozenWarehouse(rows)
    question = "Which cohorts have the most recorded posts?"
    client = FakeClient(reply(fcall("sql_query", sql=SQL, purpose="posts by cohort", params=PARAMS)),
                        reply(fcall("query_rows", query_id="q_1", offset=499, limit=1)),
                        reply(ftext("The last recorded cohort has 1499 posts, query q_1.")))
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    result = gemini_research.gemini_research(ctx, question, options, progress, lambda: False)
    assert result["note"] == "The last recorded cohort has 1499 posts, query q_1."
    first = client.calls[1]["contents"][2].parts[0].function_response
    repeated = client.calls[2]["contents"][2].parts[0].function_response
    assert first.response == repeated.response and first.id == "call_sql_query"
    compact = first.response["output"].encode("utf-8")
    full = json.dumps({"rows": rows, "query_id": "q_1", "bytes": 1234,
                       "result_hash": result_hash(rows), "truncated": False}, ensure_ascii=False).encode("utf-8")
    assert len(compact) < len(full) / 10
    assert json.loads(compact)["rows"] == rows[:10]
    tail = json.loads(client.calls[2]["contents"][4].parts[0].function_response.response["output"])
    assert tail["rows"] == [rows[499]]
    baseline = RunContext(run_id="baseline", tier="T1", as_of=ctx.as_of)
    baseline.record_query(SQL, PARAMS, rows, "posts by cohort")
    assert ctx.queries == baseline.queries
    assert writer._pack(ctx, []) == writer._pack(baseline, [])
    assert options.warehouse.dry_runs == options.warehouse.runs == 1
    assert len(client.calls) == 3
