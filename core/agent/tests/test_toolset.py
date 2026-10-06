"""42's fourteen tools without a model SDK (core/agent/toolset.py): the guard every call passes before its function
runs, and the result and error wording run_plain gives the research loop."""

import json
import sys
import types
from datetime import datetime

import pytest

from core.agent import toolset
from core.agent.context import Refused, RunContext

AS_OF = datetime.fromisoformat("2026-09-28T06:10:00+02:00")
GOOD_SQL = "SELECT post_id, text FROM intelligence_42_core.posts WHERE market = 'ZA' LIMIT 10"


def make_ctx(tier="T0", **kw):
    return RunContext(run_id="run_1", tier=tier, as_of=AS_OF, market="ZA", **kw)


class FakeWarehouse:
    def __init__(self, fail=None):
        self.fail = fail

    def dry_run(self, sql, params):
        return {"bytes": 1_000, "tables": ["ogilvy-trends-v2.intelligence_42_core.posts"]}

    def run(self, sql, params, max_bytes_billed):
        if self.fail:
            raise self.fail
        if "suppressions" in sql:
            return []  # the live search's suppression read: nobody suppressed
        return [{"post_id": "p1", "text": "amapiano at the braai"}]


class FakeClient:
    def __init__(self, quote=2.0):
        self._quote = quote
        self.calls = []

    def quote(self, route, params):
        return self._quote

    def call(self, route, params, *, lane, run_id, max_credits):
        self.calls.append(route)
        return {"items": [{"id": "7412", "text": "hello"}], "next_cursor": None, "credits_charged": 1.0,
                "status": "ok", "cache_hit": False}


class FakeWriter:
    pass


@pytest.fixture
def warehouse_module(monkeypatch):
    """Stand-in for core/agent/tools/warehouse.py, which lands in parallel. Records each call and its dependency."""
    calls = []
    mod = types.ModuleType("core.agent.tools.warehouse")

    def search_posts(ctx, warehouse, query, platforms=None, since=None, until=None, min_engagement=0, author=None,
                     sort="engagement", limit=25):
        calls.append(("search_posts", warehouse, query))
        if query == "refuse me":
            raise Refused("search_posts limit is 100")
        return [{"evidence_id": "tt_1", "text": "hi"}]

    def rising_topics(ctx, warehouse, date=None, window_days=7, market=None, kind=None, min_platforms=1):
        calls.append(("rising_topics", warehouse, window_days))
        return [{"topic": "amapiano"}]

    def recall_findings(ctx, warehouse, query, since=None, status="current"):
        calls.append(("recall_findings", warehouse, status))
        return []

    def save_finding(ctx, writer, claim, evidence_ids, label, topic, query_ids, review_by, warehouse=None):
        calls.append(("save_finding", writer, claim))
        return "f_1"

    mod.search_posts, mod.rising_topics, mod.recall_findings, mod.save_finding = (
        search_posts, rising_topics, recall_findings, save_finding)
    monkeypatch.setitem(sys.modules, "core.agent.tools.warehouse", mod)
    return calls


class Tool:
    """One tool as the research loop runs it: its plain function through run_plain."""

    def __init__(self, name, fn):
        self.name, self.fn = name, fn


def tools_by_name(ctx, warehouse=None, client=None, writer=None):
    functions = toolset.build_functions(ctx, warehouse or FakeWarehouse(), client or FakeClient(), writer or FakeWriter())
    return {name: Tool(name, functions[name]) for name in toolset.TOOL_NAMES}


def call(tool, args):
    out, is_error = toolset.run_plain(tool.name, tool.fn, args)
    return {"content": [{"type": "text", "text": out}], **({"is_error": True} if is_error else {})}


def text(result):
    return result["content"][0]["text"]


def refusals(ctx, name, args):
    """The guard's refusal for one call, as a list: empty when the call may run."""
    try:
        toolset.guard(ctx, name, args)
    except Refused as e:
        return [str(e)]
    return []


# Names and schemas


def test_tool_names():
    assert toolset.TOOL_NAMES == ["sql_query", "search_posts", "socialcrawl_call", "rising_topics", "recall_findings",
                                  "save_finding", "budget_status", "resolve_dates", "get_comments", "get_transcript",
                                  "watch_video", "log_forecast", "history", "analogues"]


def test_every_tool_has_a_closed_schema_and_a_description(warehouse_module):
    functions = toolset.build_functions(make_ctx(), FakeWarehouse(), FakeClient(), FakeWriter())
    assert list(functions) == toolset.TOOL_NAMES
    for name in toolset.TOOL_NAMES:
        schema = toolset.SCHEMAS[name]
        assert schema["type"] == "object"
        assert "properties" in schema
        assert schema.get("additionalProperties") is False
        assert toolset.DESCRIPTIONS[name]


# The guard


@pytest.mark.parametrize("sql", [
    "DELETE FROM intelligence_42_core.posts WHERE TRUE",
    "INSERT INTO intelligence_42_core.posts (post_id) VALUES ('x')",
    "DROP TABLE intelligence_42_core.posts",
    "SELECT * FROM intelligence_42_core.posts; DELETE FROM intelligence_42_core.posts WHERE TRUE",
    "SELECT * FROM trends_v2.posts",
    "SELECT * FROM other-project.intelligence_42_core.posts",
    "",
])
def test_sql_guard_denies(sql):
    assert refusals(make_ctx(), "sql_query", {"sql": sql, "purpose": "test"})


def test_sql_guard_denies_missing_sql():
    assert refusals(make_ctx(), "sql_query", {"purpose": "test"})


def test_sql_guard_reason_names_dataset():
    reasons = refusals(make_ctx(), "sql_query", {"sql": "SELECT * FROM trends_v2.posts", "purpose": "t"})
    assert any("trends_v2" in r for r in reasons)


def test_sql_guard_allows_good_select():
    assert refusals(make_ctx(), "sql_query", {"sql": GOOD_SQL, "purpose": "test"}) == []


@pytest.mark.parametrize("platform,endpoint", [
    ("google_trends", "interest"),
    ("google_trends", "explore"),
    ("google_trends", "rising"),
    ("prism", "ai-visibility"),
    ("tiktok", "user/audience"),
])
def test_socialcrawl_guard_denies_forbidden_route(platform, endpoint):
    args = {"platform": platform, "endpoint": endpoint, "params": {}, "max_credits": 1}
    assert refusals(make_ctx(), "socialcrawl_call", args)


@pytest.mark.parametrize("endpoint,params", [("trending", {"location": "ZA"})])
def test_socialcrawl_guard_allows_only_approved_google_trends_routes(endpoint, params):
    args = {"platform": "google_trends", "endpoint": endpoint, "params": params, "max_credits": 1}
    assert refusals(make_ctx(), "socialcrawl_call", args) == []


@pytest.mark.parametrize("route", [
    "google_trends?x=1/interest",
    "prism/trend-board?x=1",
    "prism/trend-board#x",
    "prism/x/../trend-board",
    "prism/./trend-board",
    "google%5Ftrends/interest",
    "prism/trend%2Dboard",
    "tiktok/user/audience;x",
    "prism/trend-board\u200b",
    "search/everywhere?x=1",
    "search/./everywhere",
    "tiktok/trending",
    "prism/brand-mentions",
])
def test_socialcrawl_guard_denies_bypass_and_unlisted_routes(route):
    platform, _, endpoint = route.partition("/")
    args = {"platform": platform, "endpoint": endpoint, "params": {}, "max_credits": 1}
    assert refusals(make_ctx(), "socialcrawl_call", args)


def test_socialcrawl_guard_denies_platform_with_a_slash():
    args = {"platform": "tiktok/post", "endpoint": "comments", "params": {}, "max_credits": 1}
    assert refusals(make_ctx(), "socialcrawl_call", args)


def test_guard_and_tool_share_one_normaliser():
    from core.agent.tools import socialcrawl

    assert toolset.normalise_route is socialcrawl.normalise_route


def test_socialcrawl_guard_denies_over_credit_budget():
    args = {"platform": "tiktok", "endpoint": "search/top", "params": {"q": "amapiano"}, "max_credits": 11}
    assert refusals(make_ctx("T0"), "socialcrawl_call", args)


def test_socialcrawl_guard_denies_when_credits_spent():
    args = {"platform": "tiktok", "endpoint": "search/top", "params": {}, "max_credits": 1}
    assert refusals(make_ctx("T0", credits_spent=10), "socialcrawl_call", args)


def test_socialcrawl_guard_denies_when_no_calls_left():
    args = {"platform": "tiktok", "endpoint": "search/top", "params": {}, "max_credits": 1}
    assert refusals(make_ctx("T0", calls_made=8), "socialcrawl_call", args)


@pytest.mark.parametrize("max_credits", [None, "lots"])
def test_socialcrawl_guard_denies_bad_max_credits(max_credits):
    args = {"platform": "tiktok", "endpoint": "search/top", "params": {}}
    if max_credits is not None:
        args["max_credits"] = max_credits
    assert refusals(make_ctx(), "socialcrawl_call", args)


def test_socialcrawl_guard_reads_budget_at_call_time():
    ctx = make_ctx("T0")
    args = {"platform": "tiktok", "endpoint": "search/top", "params": {}, "max_credits": 5}
    assert refusals(ctx, "socialcrawl_call", args) == []
    ctx.credits_spent = 8
    assert refusals(ctx, "socialcrawl_call", args)


def test_socialcrawl_guard_allows_search_everywhere_like_the_tool():
    # socialcrawl_call adds the generated-answer excludes itself, so the guard must not refuse their absence.
    args = {"platform": "search", "endpoint": "everywhere", "params": {"q": "amapiano"}, "max_credits": 2}
    assert refusals(make_ctx(), "socialcrawl_call", args) == []


def test_socialcrawl_guard_allows_good_call():
    args = {"platform": "tiktok", "endpoint": "search/top", "params": {"q": "amapiano"}, "max_credits": 5}
    assert refusals(make_ctx(), "socialcrawl_call", args) == []


@pytest.mark.parametrize("name", toolset.TOOL_NAMES)
def test_the_guard_passes_every_other_tool(name):
    if name not in ("sql_query", "socialcrawl_call"):
        assert refusals(make_ctx(), name, {}) == []


# Handlers

@pytest.mark.parametrize("sql", ["DELETE FROM intelligence_42_core.posts WHERE TRUE",
                                 "INSERT INTO intelligence_42_core.posts (post_id) VALUES ('x')",
                                 "DROP TABLE intelligence_42_core.posts",
                                 "SELECT * FROM trends_v2.posts"])
def test_sql_handler_refuses(warehouse_module, sql):
    result = call(tools_by_name(make_ctx())["sql_query"], {"sql": sql, "purpose": "t"})
    assert result["is_error"] is True
    assert text(result).startswith("Refused: ")


def test_sql_handler_returns_json(warehouse_module):
    ctx = make_ctx()
    result = call(tools_by_name(ctx)["sql_query"], {"sql": GOOD_SQL, "purpose": "t"})
    assert not result.get("is_error")
    body = json.loads(text(result))
    assert body["query_id"] == "q_1"
    assert body["rows"] == [{"post_id": "p1", "text": "amapiano at the braai"}]
    assert "q_1" in ctx.queries


def test_other_exception_becomes_error_naming_tool(warehouse_module):
    wh = FakeWarehouse(fail=RuntimeError("Unrecognized name: foo\nTraceback (most recent call last):\n  File x"))
    result = call(tools_by_name(make_ctx(), warehouse=wh)["sql_query"], {"sql": GOOD_SQL, "purpose": "t"})
    assert result["is_error"] is True
    assert "sql_query" in text(result)
    assert "Unrecognized name: foo" in text(result)
    assert "Traceback" not in text(result)


LEAKY = "GET https://api.example/x?key=SECRET failed: 403 Forbidden"


class LeakyClient(FakeClient):
    def call(self, route, params, *, lane, run_id, max_credits):
        raise RuntimeError(LEAKY)


def test_socialcrawl_failure_names_only_the_exception_type(warehouse_module):
    tool = tools_by_name(make_ctx(), client=LeakyClient())["socialcrawl_call"]
    result = call(tool, {"platform": "tiktok", "endpoint": "search/top", "params": {"q": "x"}, "max_credits": 5})
    assert result["is_error"] is True
    assert text(result) == "socialcrawl_call failed (RuntimeError)"


@pytest.mark.parametrize("message", [
    LEAKY,
    "403 Forbidden: https://api.example/x?key=SECRET",
    "bad request to /v1/x?key=SECRET&page=2",
    "see HTTP://API.EXAMPLE/X?KEY=SECRET",
])
def test_other_failures_drop_urls_and_query_strings(warehouse_module, message):
    wh = FakeWarehouse(fail=RuntimeError(message))
    result = call(tools_by_name(make_ctx(), warehouse=wh)["sql_query"], {"sql": GOOD_SQL, "purpose": "t"})
    assert result["is_error"] is True
    assert text(result).startswith("sql_query failed (RuntimeError)")
    assert "SECRET" not in text(result)
    assert "api.example" not in text(result).lower()


def test_other_failures_keep_the_readable_part(warehouse_module):
    wh = FakeWarehouse(fail=RuntimeError(LEAKY))
    result = call(tools_by_name(make_ctx(), warehouse=wh)["sql_query"], {"sql": GOOD_SQL, "purpose": "t"})
    assert "403 Forbidden" in text(result)


def test_socialcrawl_handler_refuses_over_budget(warehouse_module):
    client = FakeClient(quote=50.0)
    tool = tools_by_name(make_ctx("T0"), client=client)["socialcrawl_call"]
    result = call(tool, {"platform": "tiktok", "endpoint": "search/top", "params": {}, "max_credits": 60})
    assert result["is_error"] is True
    assert text(result).startswith("Refused: ")
    assert client.calls == []


def test_socialcrawl_handler_refuses_forbidden_route(warehouse_module):
    client = FakeClient()
    tool = tools_by_name(make_ctx(), client=client)["socialcrawl_call"]
    result = call(tool, {"platform": "google_trends", "endpoint": "interest", "params": {}, "max_credits": 1})
    assert result["is_error"] is True
    assert "Never used" in text(result)
    assert client.calls == []


def test_socialcrawl_handler_returns_json(warehouse_module):
    ctx = make_ctx()
    client = FakeClient()
    tool = tools_by_name(ctx, client=client)["socialcrawl_call"]
    result = call(tool, {"platform": "tiktok", "endpoint": "search/top", "params": {"q": "x"}, "max_credits": 5})
    assert not result.get("is_error")
    assert json.loads(text(result))["evidence_ids"] == ["tiktok_7412"]
    assert client.calls == ["tiktok/search/top"]
    assert ctx.calls_made == 1


def test_budget_status_handler(warehouse_module):
    result = call(tools_by_name(make_ctx("T0", credits_spent=4))["budget_status"], {})
    assert json.loads(text(result)) == {"credits_left": 6.0, "calls_left": 8}


def test_resolve_dates_handler(warehouse_module):
    result = call(tools_by_name(make_ctx())["resolve_dates"], {"expression": "last 7 days"})
    assert json.loads(text(result)) == {"from": "2026-09-22", "to": "2026-09-28"}


def test_resolve_dates_handler_refuses(warehouse_module):
    result = call(tools_by_name(make_ctx())["resolve_dates"], {"expression": "next week"})
    assert result["is_error"] is True
    assert "cannot resolve dates" in text(result)


def test_warehouse_handlers_route_dependencies(warehouse_module):
    wh, writer = FakeWarehouse(), FakeWriter()
    tools = tools_by_name(make_ctx(), warehouse=wh, writer=writer)
    assert json.loads(text(call(tools["search_posts"], {"query": "amapiano"})))[0]["evidence_id"] == "tt_1"
    assert json.loads(text(call(tools["rising_topics"], {"window_days": 3}))) == [{"topic": "amapiano"}]
    assert json.loads(text(call(tools["recall_findings"], {"query": "amapiano", "status": "stale"}))) == []
    saved = call(tools["save_finding"], {"claim": "c", "evidence_ids": ["tt_1"], "label": "observed",
                                         "topic": "amapiano", "query_ids": ["q_1"], "review_by": "2026-10-05"})
    assert json.loads(text(saved)) == "f_1"
    assert warehouse_module == [("search_posts", wh, "amapiano"), ("rising_topics", wh, 3),
                                ("recall_findings", wh, "stale"), ("save_finding", writer, "c")]


def test_warehouse_handler_refused(warehouse_module):
    result = call(tools_by_name(make_ctx())["search_posts"], {"query": "refuse me"})
    assert result["is_error"] is True
    assert text(result) == "Refused: search_posts limit is 100"


# Enrichment and forecast tools (BUILD.md 1.17)


class EnrichClient(FakeClient):
    def call(self, route, params, *, lane, run_id, max_credits):
        self.calls.append(route)
        return {"items": [{"id": "c1", "author": {"username": "kota_queen"}, "text": "Joburg does it better"}],
                "next_cursor": None, "credits_charged": 1.0, "status": "ok", "cache_hit": False}


class InsertWriter:
    def __init__(self):
        self.inserts = []

    def insert(self, table, rows, row_ids=None):
        self.inserts.append((table, rows, row_ids))


def ctx_with_post(tier="T1"):
    ctx = make_ctx(tier)
    ctx.evidence["tiktok_7412"] = {"id": "tiktok_7412", "platform": "tiktok", "handle": "chef_za",
                                   "url": "https://www.tiktok.com/@chef_za/video/7412", "posted_at": None,
                                   "market": "ZA", "text": "Braai day", "engagement": {}, "flags": []}
    return ctx


def test_get_comments_handler_returns_comment_evidence(warehouse_module):
    ctx, client = ctx_with_post(), EnrichClient(quote=1.0)
    result = call(tools_by_name(ctx, client=client)["get_comments"],
                  {"evidence_id": "tiktok_7412", "limit": 5, "max_credits": 2})
    assert not result.get("is_error")
    body = json.loads(text(result))
    assert body["evidence_ids"] == ["tiktok_comment_c1"]
    assert client.calls == ["tiktok/post/comments"]
    assert "tiktok_comment_c1" in ctx.evidence


def test_get_transcript_handler_refuses_an_unknown_id(warehouse_module):
    client = EnrichClient()
    result = call(tools_by_name(ctx_with_post(), client=client)["get_transcript"], {"evidence_id": "ghost"})
    assert result["is_error"] is True and text(result).startswith("Refused: ")
    assert client.calls == []


@pytest.mark.parametrize("name, args", [
    ("get_comments", {"evidence_id": "tiktok_7412", "limit": 5, "max_credits": 2}),
    ("get_transcript", {"evidence_id": "tiktok_7412"}),
])
def test_enrichment_failures_name_only_the_exception_type(warehouse_module, name, args):
    result = call(tools_by_name(ctx_with_post(), client=LeakyClient(quote=1.0))[name], args)
    assert result["is_error"] is True
    assert text(result) == f"{name} failed (RuntimeError)"


def test_log_forecast_handler_writes_through_the_writer(warehouse_module):
    ctx, writer = ctx_with_post(), InsertWriter()
    result = call(tools_by_name(ctx, writer=writer)["log_forecast"], {
        "item_id": "item_amapiano", "market": "ZA", "target": "persist_50", "horizon_days": 7, "probability": 0.6,
        "statement": "Amapiano braai clips hold their level in Durban.", "evidence_ids": ["tiktok_7412"]})
    assert not result.get("is_error")
    fid = json.loads(text(result))["forecast_id"]
    assert writer.inserts[0][0] == "intelligence_42_agent.forecasts"
    assert fid in ctx.forecasts


def test_log_forecast_handler_refuses_a_bad_target(warehouse_module):
    writer = InsertWriter()
    result = call(tools_by_name(ctx_with_post(), writer=writer)["log_forecast"], {
        "item_id": "item_amapiano", "market": "ZA", "target": "viral", "horizon_days": 7, "probability": 0.6,
        "statement": "It will go viral.", "evidence_ids": ["tiktok_7412"]})
    assert result["is_error"] is True and text(result).startswith("Refused: ")
    assert writer.inserts == []
