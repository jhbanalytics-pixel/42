from datetime import date, datetime
from types import SimpleNamespace

import pytest

from core.agent import ask, gemini_research
from core.agent.context import RunContext
from core.agent.model_budget import AskModelBudget
from core.agent.tools.dates import SAST

_DEFAULT_USAGE = object()


def fcall(name, **args):
    return SimpleNamespace(function_call=SimpleNamespace(id="call_1", name=name, args=args), text=None, thought=False)


def ftext(text):
    return SimpleNamespace(function_call=None, text=text, thought=False)


def usage(*, prompt=100, response=10, thoughts=5, tool_use=0, cached=0, candidates=None):
    return SimpleNamespace(prompt_token_count=prompt, response_token_count=response,
                           candidates_token_count=candidates, thoughts_token_count=thoughts,
                           tool_use_prompt_token_count=tool_use, cached_content_token_count=cached)


def reply(*parts, metadata=_DEFAULT_USAGE):
    if metadata is _DEFAULT_USAGE:
        metadata = usage()
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(role="model", parts=list(parts)),
                                    finish_reason=SimpleNamespace(name="STOP"))],
        usage_metadata=metadata,
    )


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.models = SimpleNamespace(generate_content=self.generate_content)

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_budget(*, hold=10, research=10, input_rate=1, output_rate=10):
    reserved_output = []

    def reserve_output(model, max_tokens):
        reserved_output.append(max_tokens)
        return max_tokens + 2000

    budget = AskModelBudget(
        hold,
        research,
        price_for_fn=lambda model: {"input": input_rate, "output": output_rate},
        reserve_output_fn=reserve_output,
    )
    return budget, reserved_output


def setup(budget=None):
    now = datetime(2026, 9, 29, 8, 0, tzinfo=SAST)
    ctx = RunContext(run_id="r_budget_test", tier="T1", as_of=now, market="ZA")
    if budget is not None:
        ctx.model_budget = budget
    events = []
    emit = ask.Progress(events.append, lambda: now, market_label="South Africa",
                        window=(date(2026, 9, 1), date(2026, 9, 28)))
    options = ask.ResearchSetup(system_prompt="You are the research analyst.", model="gemini-3.8-flash",
                                warehouse=None, client=None, tables=None)
    return ctx, options, emit


def run(monkeypatch, client, budget=None):
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    ctx, options, emit = setup(budget)
    result = gemini_research.gemini_research(ctx, "Question?", options, emit, lambda: False)
    return result, ctx


@pytest.fixture(autouse=True)
def gemini_prices(monkeypatch):
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "1")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "10")
    monkeypatch.setenv("GEMINI_THINKING_HEADROOM", "2000")


@pytest.mark.parametrize("metadata", [
    None,
    usage(prompt=400, response=None, candidates=None, thoughts=10),
    SimpleNamespace(prompt_token_count=400, response_token_count=10, candidates_token_count=None,
                    thoughts_token_count=5),
])
def test_missing_or_partial_usage_books_full_ticket_and_never_dispatches_again(monkeypatch, metadata):
    budget, _ = make_budget()
    client = FakeClient(reply(fcall("not_a_tool"), metadata=metadata), reply(ftext("must not run")))

    result, _ = run(monkeypatch, client, budget)

    assert len(client.calls) == 1
    assert budget.stopped
    assert budget.conservative_usd == pytest.approx(budget.booked_usd)
    assert budget.booked_usd > 0
    assert result["stopped"] is True
    assert "usage" in result["note"].lower()


def test_unreadable_usage_books_full_ticket_and_returns_a_reason(monkeypatch):
    budget, _ = make_budget()
    response = reply(ftext("Available evidence."))

    class UnreadableResponse:
        candidates = response.candidates

        @property
        def usage_metadata(self):
            raise RuntimeError("usage metadata unavailable")

    client = FakeClient(UnreadableResponse())

    result, _ = run(monkeypatch, client, budget)

    assert len(client.calls) == 1
    assert result["note"].startswith("Available evidence.")
    assert "usage" in result["note"].lower()
    assert budget.stopped
    assert budget.conservative_usd == pytest.approx(budget.booked_usd)


def test_known_usage_settles_and_reserves_the_raw_output_allowance(monkeypatch):
    budget, reserved_output = make_budget()
    client = FakeClient(reply(ftext("Reading.")))

    result, _ = run(monkeypatch, client, budget)

    assert result["note"] == "Reading."
    assert reserved_output == [8000]
    assert client.calls[0]["config"].max_output_tokens == 10000
    assert budget.booked_usd == pytest.approx((100 + 15 * 10) / 1_000_000)
    assert budget.conservative_usd == 0
    assert not budget.stopped


def test_sdk_usage_defaults_optional_counts_to_zero_only_when_total_reconciles(monkeypatch):
    from google.genai import types

    budget, _ = make_budget()
    metadata = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=100,
        candidates_token_count=15,
        total_token_count=115,
    )
    client = FakeClient(reply(ftext("Reading."), metadata=metadata))

    result, _ = run(monkeypatch, client, budget)

    assert result["note"] == "Reading."
    assert result["stopped"] is False
    assert result["tokens"] == {"input": 100, "output": 15}
    assert budget.booked_usd == pytest.approx((100 + 15 * 10) / 1_000_000)
    assert budget.conservative_usd == 0


@pytest.mark.parametrize("counts", [
    {"prompt_token_count": 100, "candidates_token_count": 15, "total_token_count": 116},
    {"prompt_token_count": 100, "candidates_token_count": 15, "total_token_count": 115,
     "tool_use_prompt_token_count": -1},
    {"prompt_token_count": 100, "candidates_token_count": 15, "total_token_count": 115,
     "cached_content_token_count": 101},
])
def test_sdk_usage_with_inconsistent_or_malformed_counts_books_full_ticket(monkeypatch, counts):
    from google.genai import types

    budget, _ = make_budget()
    metadata = types.GenerateContentResponseUsageMetadata(**counts)
    client = FakeClient(reply(ftext("Reading."), metadata=metadata))

    result, _ = run(monkeypatch, client, budget)

    assert result["stopped"] is True
    assert "usage" in result["note"].lower()
    assert budget.stopped
    assert budget.conservative_usd == pytest.approx(budget.booked_usd)


def test_two_dollar_research_cap_refuses_the_next_call_before_dispatch(monkeypatch):
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "1.5")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "190")
    budget, _ = make_budget(hold=3, research=2, input_rate=1.5, output_rate=190)
    client = FakeClient(reply(fcall("not_a_tool"), metadata=usage(response=7400, thoughts=2000)),
                        reply(ftext("must not run")))

    result, _ = run(monkeypatch, client, budget)

    assert len(client.calls) == 1
    assert budget.booked_usd == pytest.approx((100 * 1.5 + 9400 * 190) / 1_000_000)
    assert budget.booked_usd < 2
    assert "budget" in result["note"].lower()


def test_research_cap_leaves_the_shared_budget_available_for_the_writer(monkeypatch):
    budget, _ = make_budget(hold=0.25, research=0.05)
    client = FakeClient(reply(ftext("must not run")))

    result, _ = run(monkeypatch, client, budget)

    assert client.calls == []
    assert result["stopped"] is False
    assert "research" in result["note"].lower() and "budget" in result["note"].lower()
    writer_ticket = budget.reserve("gemini-3.8-flash", 1000, 8000, research=False)
    assert writer_ticket is not None


def test_question_cap_stops_before_a_model_dispatch(monkeypatch):
    budget, _ = make_budget(hold=0.05, research=0.05)
    client = FakeClient(reply(ftext("must not run")))

    result, _ = run(monkeypatch, client, budget)

    assert client.calls == []
    assert result["stopped"] is True
    assert "question" in result["note"].lower() and "budget" in result["note"].lower()


@pytest.mark.parametrize("message", ["429 RESOURCE_EXHAUSTED", "500 INTERNAL"])
def test_guarded_provider_errors_are_not_retried_and_book_the_ticket(monkeypatch, message):
    budget, _ = make_budget()
    client = FakeClient(RuntimeError(message))

    with pytest.raises(RuntimeError) as raised:
        run(monkeypatch, client, budget)

    assert len(client.calls) == 1
    assert budget.stopped
    assert budget.conservative_usd == pytest.approx(budget.booked_usd)
    assert raised.value.before_dispatch is False


def test_guarded_client_is_created_with_sdk_retries_disabled(monkeypatch):
    from google.genai import types

    budget, _ = make_budget()
    client = FakeClient(reply(ftext("Reading.")))
    retry_values = []

    class GeminiModelStub:
        def __init__(self, retries=1, timeout_s=None):
            retry_values.append(retries)
            self.client = client

        def config(self, **kwargs):
            return types.GenerateContentConfig(system_instruction=kwargs["system"], max_output_tokens=10000)

    monkeypatch.setattr(gemini_research, "client_factory", None)
    monkeypatch.setattr(gemini_research, "GeminiModel", GeminiModelStub)
    ctx, options, emit = setup(budget)

    result = gemini_research.gemini_research(ctx, "Question?", options, emit, lambda: False)

    assert result["note"] == "Reading."
    assert retry_values.count(0) == 1


def test_unmetered_path_keeps_its_existing_429_retry(monkeypatch):
    client = FakeClient(RuntimeError("429 RESOURCE_EXHAUSTED"), reply(ftext("Reading.")))

    result, _ = run(monkeypatch, client)

    assert result["note"] == "Reading."
    assert len(client.calls) == 2


# A5. A tool can stop the shared question budget between two research turns (watch_video's model failure with no usage
# books its reserve and stops it). The next turn's reserve then raises the budget's own stop reason, and only two
# reasons were handled, so the others (usage_unknown, bound_exceeded, ceiling_exceeded) failed the whole ask instead
# of ending research as a budget stop. Ending research makes no further call: it can only spend less.
def _stopping_tool(usage_arg, booked):
    def build(ctx, warehouse, client, tables):
        def stop_budget():
            budget = ctx.model_budget
            budget.settle(budget.reserve("gemini-test", 1000, 100, research=True), usage_arg)
            booked.append(budget.booked_usd)
            return {"ok": True}
        return {"budget_status": stop_budget}
    return build


@pytest.mark.parametrize(("usage_arg", "reason"), [
    (None, "usage_unknown"),
    ({"input_tokens": 5_000_000, "output_tokens": 5, "usd": 0.000001}, "bound_exceeded"),
    ({"input_tokens": 10, "output_tokens": 5, "usd": 0.5}, "ceiling_exceeded"),
])
def test_a_budget_stopped_by_a_tool_ends_research_as_a_stop_not_a_failure(monkeypatch, usage_arg, reason):
    budget, _ = make_budget()
    booked = []
    monkeypatch.setattr(gemini_research, "build_functions", _stopping_tool(usage_arg, booked))
    client = FakeClient(reply(fcall("budget_status")), reply(ftext("must not run")))

    result, _ = run(monkeypatch, client, budget)

    assert budget.stopped and budget.stop_reason == reason
    assert result["stopped"] is True and "stopped" in result["note"].lower()
    assert len(client.calls) == 1  # no model call went out after the stop
    assert booked and budget.booked_usd == booked[0]  # nothing was booked after the stop
    assert budget.booked_usd <= budget.cap_micros / 1_000_000  # inside the hold


def test_a_busy_retry_refused_by_a_stopped_budget_still_raises_the_earlier_failure(monkeypatch):
    # The retry branch keeps its contract: when the first attempt failed and the retry is refused, the failure stands.
    from google.genai import errors

    budget, _ = make_budget()
    busy = errors.ClientError(429, {"error": {"code": 429, "message": "Resource exhausted.",
                                              "status": "RESOURCE_EXHAUSTED"}})
    monkeypatch.setattr(gemini_research.time, "sleep", lambda s: None)
    calls = []

    def generate_content(**kwargs):
        calls.append(kwargs)
        other = budget.reserve("gemini-test", 1000, 1)
        budget.settle(other, None)
        raise busy

    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    with pytest.raises(errors.ClientError):
        run(monkeypatch, client, budget)
    assert len(calls) == 1
