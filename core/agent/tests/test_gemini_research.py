"""The Gemini research loop and the provider switch in run_ask, with a scripted fake client: no network."""

from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest

from core.agent import ask, gemini_research
from core.agent.context import RunContext
from core.agent.tools.dates import SAST


def fcall(name, **args):
    return NS(function_call=NS(id=f"call_{name}", name=name, args=args), text=None, thought=False)


def ftext(text):
    return NS(function_call=None, text=text, thought=False)


def reply(*parts, pin=100, pout=10, think=5, finish="STOP"):
    return NS(candidates=[NS(content=NS(role="model", parts=list(parts)),
                             finish_reason=NS(name=finish))],
              usage_metadata=NS(prompt_token_count=pin, response_token_count=pout, thoughts_token_count=think,
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


def setup_for(tier="T1"):
    now = datetime(2026, 9, 29, 8, 0, tzinfo=SAST)
    ctx = RunContext(run_id="r_test", tier=tier, as_of=now, market="ZA")
    events = []
    progress = ask.Progress(events.append, lambda: now, market_label="South Africa",
                            window=(date(2026, 9, 1), date(2026, 9, 28)))
    options = ask.ResearchSetup(system_prompt="You are 42's lead analyst.", model="gemini-3.8-flash",
                                warehouse=None, client=None, tables=None)
    return ctx, options, progress, events


def run(monkeypatch, client, tier="T1", stop=lambda: False):
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    ctx, options, progress, events = setup_for(tier)
    result = gemini_research.gemini_research(ctx, "Question?", options, progress, stop)
    return result, ctx, events


def test_a_tool_call_runs_its_function_and_the_model_gets_the_result_back(monkeypatch):
    client = FakeClient(reply(fcall("resolve_dates", expression="last 7 days")), reply(ftext("Reading: last week.")))
    result, ctx, events = run(monkeypatch, client)
    assert result["note"] == "Reading: last week." and result["stopped"] is False
    assert result["tokens"] == {"input": 200, "output": 2 * 15}
    assert result["usd"] == pytest.approx((200 * 1 + 30 * 10) / 1e6)
    second = client.calls[1]["contents"]
    assert second[-1].parts[0].function_response.name == "resolve_dates"
    assert "from" in second[-1].parts[0].function_response.response["output"]
    assert any(e["event"] == "step" and "dates" in e["text"] for e in events)
    cfg = client.calls[0]["config"]
    assert cfg.system_instruction == "You are 42's lead analyst."
    assert [d.name for d in cfg.tools[0].function_declarations] == [
        "sql_query", "search_posts", "socialcrawl_call", "rising_topics", "recall_findings", "save_finding",
        "budget_status", "resolve_dates", "get_comments", "get_transcript", "watch_video", "log_forecast", "query_rows",
        "history", "analogues"]
    assert cfg.automatic_function_calling.disable is True


def test_the_models_own_turn_goes_back_unchanged_for_its_thought_signatures(monkeypatch):
    first = reply(fcall("resolve_dates", expression="yesterday"))
    client = FakeClient(first, reply(ftext("done")))
    run(monkeypatch, client)
    assert client.calls[1]["contents"][1] is first.candidates[0].content


def test_a_bad_query_is_refused_by_the_guard_before_it_runs(monkeypatch):
    client = FakeClient(reply(fcall("sql_query", sql="DELETE FROM intelligence_42_core.posts", purpose="x")),
                        reply(ftext("ok")))
    result, ctx, events = run(monkeypatch, client)
    answer = client.calls[1]["contents"][-1].parts[0].function_response.response
    assert answer["error"].startswith("Refused") and ctx.queries == {}


def test_a_refused_live_route_says_so_without_naming_the_route(monkeypatch):
    client = FakeClient(reply(fcall("socialcrawl_call", platform="google_trends", endpoint="x", max_credits=1)),
                        reply(ftext("ok")))
    result, ctx, events = run(monkeypatch, client)
    assert ctx.credits_spent == 0
    assert any(e.get("text") == "The live source call was refused and did not run" for e in events)


def test_a_tool_outside_the_eleven_is_not_available(monkeypatch):
    client = FakeClient(reply(fcall("run_shell", cmd="ls")), reply(ftext("ok")))
    run(monkeypatch, client)
    assert "not available" in client.calls[1]["contents"][-1].parts[0].function_response.response["error"]


def test_the_loop_stops_at_the_tiers_turn_limit(monkeypatch):
    client = FakeClient(*[reply(fcall("budget_status")) for _ in range(20)])
    run(monkeypatch, client, tier="T0")
    assert len(client.calls) == 12  # T0 max_turns


def test_the_loop_stops_when_the_tiers_usd_budget_is_spent(monkeypatch):
    client = FakeClient(*[reply(fcall("budget_status"), pin=1_000_000, pout=0, think=0) for _ in range(5)])
    result, ctx, events = run(monkeypatch, client, tier="T0")  # USD 0.50 budget, first call costs 1.00
    assert len(client.calls) == 1 and "budget" in result["note"]


def test_the_tiers_usd_budget_counts_model_spend_inside_tools(monkeypatch):
    # A video read spends on the model inside a tool, into model_usd_extra; that spend counts against the tier too.
    def spending_functions(ctx, *deps):
        def watch():
            ctx.model_usd_extra += 0.30
            return {}
        return {"budget_status": watch}

    monkeypatch.setattr(gemini_research, "build_functions", spending_functions)
    client = FakeClient(*[reply(fcall("budget_status")) for _ in range(12)])
    result, ctx, events = run(monkeypatch, client, tier="T0")  # USD 0.50 budget, each tool call spends 0.30
    assert len(client.calls) == 2 and "budget" in result["note"]
    assert ctx.research_usd == pytest.approx(result["usd"])


def test_a_stop_request_ends_the_loop_and_reports_stopped(monkeypatch):
    client = FakeClient(reply(fcall("budget_status")), reply(ftext("never")))
    result, ctx, events = run(monkeypatch, client, stop=lambda: bool(client.calls))
    assert result["stopped"] is True and len(client.calls) == 1


def test_a_stop_request_before_research_dispatch_makes_no_call(monkeypatch):
    client = FakeClient(reply(ftext("must not run")))

    result, ctx, events = run(monkeypatch, client, stop=lambda: True)

    assert result["stopped"] is True and client.calls == []
    assert result["tokens"] == {"input": 0, "output": 0}
    assert result["usd"] == 0 and ctx.research_usd == 0


def test_the_provider_switch_sets_models_deps_and_the_hold(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    from core.llm.provider import default_model, price_for

    assert default_model("smart") == "gemini-3.8-flash"
    assert ask.hold_usd("T1", "gemini-3.8-flash") > 2.0  # the tier budget plus the writer and checks
    monkeypatch.delenv("GEMINI_PRICE_INPUT_PER_M")
    assert ask.hold_usd("T1", "gemini-3.8-flash") > 2.0  # the list price stands without the override
    with pytest.raises(KeyError):
        ask.hold_usd("T1", "gemini-other")  # unpriced: the ask never starts on a guess
    with pytest.raises(KeyError):
        price_for("mystery-model")  # a model outside the family has no price either


def test_enrich_goes_one_call_at_a_time_and_prices_from_the_provider(monkeypatch):
    from core.llm.gemini import GeminiModel
    from core.understand import enrich

    monkeypatch.setattr(enrich, "MODEL", "gemini-3.8-flash")
    assert not hasattr(enrich, "choose_runner") and not hasattr(enrich, "batch_request")
    assert isinstance(enrich.SyncRunner().model, GeminiModel)
    assert enrich.usd(1_000_000, 1_000_000, batched=False) == pytest.approx(11.0)


def test_net_model_and_critic_follow_the_switch(monkeypatch):
    from core.agent import critic
    from core.llm.gemini import GeminiModel
    from core.understand import cluster

    net = cluster.net_model()
    assert isinstance(net, GeminiModel) and net.timeout_s == cluster.NET_TIMEOUT_S and net.retries == cluster.NET_RETRIES
    assert critic.critic_model() == "gemini-3.8-flash"
    monkeypatch.setenv("CRITIC_MODEL", "gemini-x")
    assert critic.critic_model() == "gemini-x"


def test_bad_arguments_are_refused_before_the_tool_runs_and_ids_go_back(monkeypatch):
    client = FakeClient(reply(fcall("resolve_dates", expression="yesterday", extra="x")), reply(ftext("ok")))
    run(monkeypatch, client)
    fr = client.calls[1]["contents"][-1].parts[0].function_response
    assert fr.id == "call_resolve_dates" and fr.response["error"].startswith("Refused: Invalid arguments")


def test_the_loop_thinks_at_medium_and_the_short_calls_at_low(monkeypatch):
    client = FakeClient(reply(ftext("done")))
    run(monkeypatch, client)
    assert str(client.calls[0]["config"].thinking_config.thinking_level).lower().endswith("medium")


@pytest.mark.parametrize("finish", ["MAX_TOKENS", "PROHIBITED_CONTENT", "SAFETY", "MALFORMED_FUNCTION_CALL"])
def test_a_turn_that_does_not_end_in_stop_fails_and_carries_its_spend(monkeypatch, finish):
    client = FakeClient(reply(ftext("cut"), finish=finish))
    with pytest.raises(RuntimeError, match=finish) as e:
        run(monkeypatch, client)
    assert e.value.usd > 0 and e.value.usage["output_tokens"] == 15


def test_a_429_is_retried_once_then_the_loop_carries_on(monkeypatch):
    monkeypatch.setattr(gemini_research, "RETRY_WAIT_S", 0)
    ok = reply(ftext("done"))
    client = FakeClient(ok)
    real = client.generate
    hits = []

    def flaky(**kw):
        if not hits:
            hits.append(1)
            raise Exception("429 RESOURCE_EXHAUSTED")
        return real(**kw)

    client.models = NS(generate_content=flaky)
    result, _, _ = run(monkeypatch, client)
    assert result["note"] == "done" and hits == [1]


def test_estimates_reserve_the_thinking_headroom_on_gemini_only():
    from core.llm.provider import reserve_output

    assert reserve_output("gemini-3.8-flash", 400) == 2400 and reserve_output("text-model", 400) == 400
    def call(tin, tout):
        return (tin * 1 + (tout + 2000) * 10) / 1e6  # the fixture prices gemini at 1 and 10

    want = (0.5 + call(ask.WRITER_INPUT_TOKENS, ask.WRITER_MAX_TOKENS)
            + ask.SUPPORT_CALLS * call(ask.SUPPORT_INPUT_TOKENS, ask.SUPPORT_MAX_TOKENS)
            + call(ask.FIELD_INPUT_TOKENS, ask.FIELD_MAX_TOKENS)
            + ask.K4_REWRITE_CALLS * call(ask.K4_REWRITE_INPUT_TOKENS, ask.K4_REWRITE_MAX_TOKENS)
            + ask.K4_RECHECK_CALLS * call(ask.SUPPORT_INPUT_TOKENS, ask.SUPPORT_MAX_TOKENS)
            + call(ask.WRITER_INPUT_TOKENS, ask.WRITER_MAX_TOKENS)
            + call(ask.HEADLINE_REWRITE_INPUT_TOKENS, ask.HEADLINE_REWRITE_MAX_TOKENS))  # the short answer rewrite
    assert ask.hold_usd("T0", "gemini-3.8-flash") == pytest.approx(want)


def test_with_model_provider_unset_ask_and_enrich_build_gemini(monkeypatch):
    from core.agent.gemini_research import gemini_research as research
    from core.agent.tools import sql_query, warehouse
    from core.llm.gemini import GeminiModel
    from core.understand import enrich

    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    monkeypatch.setattr(sql_query, "BigQueryWarehouse", lambda: object())
    monkeypatch.setattr(warehouse, "BigQueryTableWriter", lambda: object())
    assert isinstance(enrich.SyncRunner().model, GeminiModel)
    deps = ask._default_deps()
    assert isinstance(getattr(deps.model, "model", deps.model), GeminiModel) and deps.research is research


@pytest.mark.parametrize("retries", [0, 1])
def test_the_ask_writer_on_gemini_is_built_with_the_ask_timeout(monkeypatch, retries):
    from core.agent.tools import sql_query, warehouse

    monkeypatch.setattr(sql_query, "BigQueryWarehouse", lambda: object())
    monkeypatch.setattr(warehouse, "BigQueryTableWriter", lambda: object())
    model = ask._default_deps(gemini_retries=retries).model
    inner = getattr(model, "model", model)
    assert inner.timeout_s == ask.ASK_GEMINI_TIMEOUT_S and inner.retries == retries


@pytest.mark.parametrize("budgeted", [False, True])
def test_the_research_client_is_built_with_the_ask_timeout(monkeypatch, budgeted):
    from google import genai

    from core.agent.model_budget import AskModelBudget

    built = []
    fake = FakeClient(reply(ftext("done")))

    def make_client(**kw):
        built.append(kw)
        return fake

    monkeypatch.setattr(genai, "Client", make_client)
    monkeypatch.setattr(gemini_research, "client_factory", None)
    ctx, options, progress, events = setup_for()
    ctx.model_budget = AskModelBudget(1.0, 0.5) if budgeted else None
    gemini_research.gemini_research(ctx, "Question?", options, progress, lambda: False)
    assert len(built) == 1
    assert built[0]["http_options"].timeout == ask.ASK_GEMINI_TIMEOUT_S * 1000
    assert built[0]["http_options"].retry_options.attempts == (1 if budgeted else 2)


class LeakyClient:
    def quote(self, route, params):
        return 1.0

    def call(self, route, params, *, lane, run_id, max_credits):
        raise RuntimeError("GET https://api.socialcrawl.example/v1/x?api_key=SECRET failed")


@pytest.mark.parametrize("name, args", [
    ("get_comments", {"evidence_id": "tiktok_7412", "limit": 5, "max_credits": 2}),
    ("get_transcript", {"evidence_id": "tiktok_7412"}),
])
def test_enrichment_failures_name_only_the_exception_type_on_gemini_too(monkeypatch, name, args):
    client = FakeClient(reply(fcall(name, **args)), reply(ftext("ok")))
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    ctx, options, progress, events = setup_for()
    ctx.evidence["tiktok_7412"] = {"id": "tiktok_7412", "platform": "tiktok", "handle": "chef_za",
                                   "url": "https://www.tiktok.com/@chef_za/video/7412", "posted_at": None,
                                   "market": "ZA", "text": "Braai day", "engagement": {}, "flags": []}
    options.client = LeakyClient()
    gemini_research.gemini_research(ctx, "Question?", options, progress, lambda: False)
    answer = client.calls[1]["contents"][-1].parts[0].function_response.response
    assert answer == {"error": f"{name} failed (RuntimeError)"}


def test_every_declaration_is_plain_enough_for_gemini_function_calling():
    from core.agent.toolset import SCHEMAS, TOOL_NAMES

    free_form = []
    unsupported = ("exclusiveMinimum", "exclusiveMaximum", "patternProperties", "oneOf", "anyOf", "allOf")

    def walk(schema, path):
        assert schema.get("enum", [None]), f"{path} has an empty enum"
        assert not [k for k in unsupported if k in schema], f"{path} uses a keyword Gemini may not take"
        if schema.get("type") == "object" and not schema.get("properties") and path.count(".") > 0:
            free_form.append(path)
        for key, sub in (schema.get("properties") or {}).items():
            walk(sub, f"{path}.{key}")
        if isinstance(schema.get("items"), dict):
            walk(schema["items"], f"{path}[]")

    for name in TOOL_NAMES:
        walk(SCHEMAS[name], name)
    assert free_form == ["sql_query.params", "socialcrawl_call.params"]  # the two gemini-l3 already declared
    assert [d.name for d in gemini_research.declarations()[0].function_declarations] == TOOL_NAMES


@pytest.mark.parametrize("probability", [0, 1])
def test_a_forecast_at_zero_or_one_passes_the_schema_and_the_tool_refuses_it(monkeypatch, probability):
    args = {"item_id": "item_amapiano", "market": "ZA", "target": "persist_50", "horizon_days": 7,
            "probability": probability, "statement": "Amapiano braai clips hold their level.",
            "evidence_ids": ["tiktok_7412"]}
    assert gemini_research._invalid_args("log_forecast", args) is None
    client = FakeClient(reply(fcall("log_forecast", **args)), reply(ftext("ok")))
    run(monkeypatch, client)
    answer = client.calls[1]["contents"][-1].parts[0].function_response.response
    assert answer["error"].startswith("Refused: probability must be a number strictly between 0 and 1")


def test_a_whole_number_float_horizon_passes_the_schema():
    args = {"item_id": "i", "market": "ZA", "target": "persist_50", "horizon_days": 14.0, "probability": 0.6,
            "statement": "s", "evidence_ids": ["e"]}
    assert gemini_research._invalid_args("log_forecast", args) is None


@pytest.mark.parametrize("tool, args", [
    ("get_comments", {"evidence_id": "tt_scrape_me", "max_credits": 1}),
    ("get_transcript", {"evidence_id": "ignore previous"}),
    ("log_forecast", {"item_id": "item_x", "market": "ZA", "target": "persist_50", "horizon_days": 7,
                      "probability": 0.6, "statement": "Clips hold their level.", "evidence_ids": ["p_bad"]}),
])
def test_a_refused_enrichment_or_forecast_call_shows_only_the_bare_line_on_gemini_too(monkeypatch, tool, args):
    client = FakeClient(reply(fcall(tool, **args)), reply(ftext("ok")))
    result, ctx, events = run(monkeypatch, client)
    answer = client.calls[1]["contents"][-1].parts[0].function_response.response
    assert answer["error"].startswith("Refused: ")  # the model still gets the reason
    notes = [e["text"] for e in events if e["event"] == "step" and e["kind"] == "note"]
    assert notes[-1] == ask.failed_step(tool, "")
    assert not any(word in note for note in notes for word in ("tt_scrape_me", "ignore previous", "p_bad"))


def test_a_batch_job_collected_is_priced_at_the_batch_price(monkeypatch):
    from core.understand import enrich
    from core.understand.tests import test_enrich as te

    monkeypatch.setattr(enrich, "MODEL", "gemini-3.8-flash")
    con, batch = te.runs_table(), te.FakeBatch()
    te.record(con, "u1", te.T0, {"batch_job_id": "jobs/1", "batch_suffix": "s1", "batch_estimate_usd": 0.0})
    batch.states["jobs/1"] = "JOB_STATE_SUCCEEDED"
    batch.in_ids["s1"], batch.out["s1"] = ["a"], [{"post_id": "a", "output": te.good_output(),
                                                   "input_tokens": 1_000_000, "output_tokens": 1_000_000}]
    counts = enrich.run_enrich(te.FakeExecute([], runs=con), te.FakeRunner(), run_date=te.DAY, limit=10,
                               batches=batch, now=te.T0)
    assert enrich.BATCH_PRICE == {"input": 1.00, "output": 5.00}
    assert counts["model_usd"] == pytest.approx((1.00 + 5.00) * enrich.BATCH_DISCOUNT)
    assert enrich.usd(1_000_000, 1_000_000, batched=False) == pytest.approx(11.0)  # a sync call stays on Gemini
