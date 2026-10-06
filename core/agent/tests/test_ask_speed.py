"""Ask speed and reliability (Albert, 4 October: a T1 ask was still researching at 5 min 48 s on step 12, then a Gemini
server error ended it): a 5xx from Vertex gets one more try, and each research turn logs its seconds."""

import pytest

from core.agent import ask, gemini_research
from core.agent.context import TIERS
from core.agent.model_budget import AskModelBudget
from core.agent.tests.test_gemini_research_budget import FakeClient, fcall, ftext, make_budget, reply, setup


class ServerError(Exception):
    """Shaped like google.genai.errors.ServerError: a 5xx with its status code."""

    def __init__(self, code=503):
        super().__init__(f"{code} UNAVAILABLE")
        self.code = code


class ClientError(Exception):
    def __init__(self, code=400):
        super().__init__(f"{code} INVALID_ARGUMENT")
        self.code = code


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(gemini_research.time, "sleep", lambda seconds: None)
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "1")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "10")
    monkeypatch.setenv("GEMINI_THINKING_HEADROOM", "2000")


class Clock:
    """Each model call takes `per_call` seconds."""

    def __init__(self, per_call):
        self.now, self.per_call = 0.0, per_call

    def __call__(self):
        return self.now


def timed(client, clock):
    real = client.generate_content

    def generate_content(**kwargs):
        clock.now += clock.per_call
        return real(**kwargs)

    client.models.generate_content = generate_content
    return client


def research(monkeypatch, client, *, budget=None, tier="T1", clock=None):
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    if clock is not None:
        monkeypatch.setattr(gemini_research, "clock", clock)
    ctx, options, emit = setup(budget)
    ctx.tier = tier
    if tier == "T3":  # an investigation's limits come from its plan
        ctx.limits = {**TIERS["T3"], "max_budget_usd": 6.0}
    steps = []
    emit._emit = steps.append
    result = gemini_research.gemini_research(ctx, "What is trending?", options, emit, lambda: False)
    return result, steps


def test_research_takes_no_time_limit_and_runs_to_its_finishing_note(monkeypatch):
    # Albert, 4 October: speed comes from better code and infrastructure, not from cutting research short.
    clock = Clock(per_call=500)
    client = timed(FakeClient(reply(fcall("not_a_tool")), reply(fcall("not_a_tool")), reply(ftext("Done."))), clock)

    result, _ = research(monkeypatch, client, clock=clock)

    assert len(client.calls) == 3
    assert result["note"] == "Done."
    assert not hasattr(gemini_research, "RESEARCH_SECONDS")


def test_a_research_turn_that_hits_a_server_error_runs_once_more_on_a_fresh_reserve(monkeypatch):
    budget, _ = make_budget()
    client = FakeClient(ServerError(503), reply(ftext("Found it.")))

    result, _ = research(monkeypatch, client, budget=budget)

    assert len(client.calls) == 2
    assert result["note"] == "Found it."
    assert result["stopped"] is False
    assert not budget.stopped
    assert budget.conservative_usd > 0  # the failed call's full reserve stays booked


def test_a_second_server_error_ends_research_and_stops_the_budget(monkeypatch):
    budget, _ = make_budget()
    client = FakeClient(ServerError(500), ServerError(503), reply(ftext("must not run")))

    with pytest.raises(ServerError) as caught:
        research(monkeypatch, client, budget=budget)

    assert len(client.calls) == 2
    assert caught.value.before_dispatch is False
    assert budget.stopped


def test_a_client_error_is_not_retried(monkeypatch):
    budget, _ = make_budget()
    client = FakeClient(ClientError(400), reply(ftext("must not run")))

    with pytest.raises(ClientError):
        research(monkeypatch, client, budget=budget)

    assert len(client.calls) == 1
    assert budget.stopped


def test_a_server_error_retry_the_question_budget_cannot_afford_stops_with_what_was_found(monkeypatch):
    budget, _ = make_budget(hold=0.15, research=0.15)  # one research reserve (about USD 0.1) fits, a second does not
    client = FakeClient(ServerError(503), reply(ftext("must not run")))

    result, _ = research(monkeypatch, client, budget=budget)

    assert len(client.calls) == 1
    assert result["stopped"] is True
    assert "budget for this question is spent" in result["note"]


def test_a_server_error_retry_past_the_research_share_hands_over_to_the_writer(monkeypatch):
    budget, _ = make_budget(hold=10, research=0.15)
    client = FakeClient(reply(fcall("not_a_tool")), ServerError(503), reply(ftext("must not run")))

    result, _ = research(monkeypatch, client, budget=budget)

    assert len(client.calls) == 2
    assert result["stopped"] is False  # the writer still runs on what the first turn found
    assert "budget for research is spent" in result["note"]


def test_research_without_a_budget_leaves_a_server_error_to_the_http_client_retry(monkeypatch):
    # T2 and T3 build the client with one HTTP retry, which covers a 5xx; the loop adds none on top.
    client = FakeClient(ServerError(502), reply(ftext("must not run")))

    with pytest.raises(ServerError):
        research(monkeypatch, client)

    assert len(client.calls) == 1


class Writer:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def complete_json(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, {"input_tokens": 5, "output_tokens": 5, "usd": 0.000055}


KWARGS = {"system": "s", "user": "u", "schema": {"type": "object"}, "model": "gemini-3.8-flash", "max_tokens": 100}


def budget():
    return AskModelBudget(10, 10, price_for_fn=lambda model: {"input": 1, "output": 10},
                          reserve_output_fn=lambda model, tokens: tokens + 2000)


def test_a_writer_call_that_hits_a_server_error_runs_once_more_on_a_fresh_reserve():
    b = budget()
    model = ask._StopAwareModel(Writer(ServerError(503), {"ok": True}), lambda: False, b)

    out, usage = model.complete_json(**KWARGS)

    assert out == {"ok": True} and model.model.calls == 2
    assert not b.stopped
    assert b.conservative_usd > 0


def test_a_writer_call_keeps_failing_after_one_retry():
    b = budget()
    model = ask._StopAwareModel(Writer(ServerError(503), ServerError(500), {"ok": True}), lambda: False, b)

    with pytest.raises(ServerError):
        model.complete_json(**KWARGS)

    assert model.model.calls == 2
    assert b.stopped


def test_a_writer_call_failing_for_another_reason_is_not_retried():
    b = budget()
    model = ask._StopAwareModel(Writer(RuntimeError("returned no text"), {"ok": True}), lambda: False, b)

    with pytest.raises(RuntimeError):
        model.complete_json(**KWARGS)

    assert model.model.calls == 1
    assert b.stopped


def test_a_stop_request_cancels_the_retry():
    b = budget()
    stops = iter([False, True])
    model = ask._StopAwareModel(Writer(ServerError(503), {"ok": True}), lambda: next(stops), b)

    with pytest.raises(ServerError):
        model.complete_json(**KWARGS)

    assert model.model.calls == 1
