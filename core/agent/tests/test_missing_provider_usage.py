"""Ask accounting retains admitted estimates when provider usage is unknown."""

import pytest

from core.agent import ask, gemini_research, writer
from core.agent.tests.test_ask import CONSERVATIVE_NOTICE, FakeModel, Harness, make_research
from core.agent.tests.test_ask_model_budget import configure_gemini, make_budget, _guarded_call
from core.agent.tests.test_gemini_research import FakeClient, ftext, reply, setup_for


UNKNOWN = {"input_tokens": None, "output_tokens": None, "cached_tokens": None,
           "thinking_tokens": None, "usd": None, "usage_unknown": True}


def test_unknown_success_returns_content_and_exact_admitted_estimate():
    class Model:
        def complete_json(self, **kwargs):
            return {"answer": "retained"}, dict(UNKNOWN)

    budget = make_budget(hold=0.01, research=0.01)
    model = ask._StopAwareModel(Model(), lambda: False, budget)
    content, usage = _guarded_call(model)
    assert content == {"answer": "retained"}
    assert usage["usage_unknown"] and usage["reserved"]
    assert usage["usd"] == budget.booked_usd == budget.conservative_usd > 0
    assert not budget.stopped
    assert writer._usage_or_reserve(usage, "unpriced", 900000, 900000) == usage


def test_unknown_malformed_failure_keeps_one_admitted_estimate_and_stops():
    error = RuntimeError("unusable output")
    error.usage, error.usd, error.reserve_model_estimate = None, None, True

    class Model:
        def complete_json(self, **kwargs):
            raise error

    budget = make_budget(hold=0.01, research=0.01)
    model = ask._StopAwareModel(Model(), lambda: False, budget)
    with pytest.raises(RuntimeError) as caught:
        _guarded_call(model)
    assert caught.value is error
    assert budget.stopped and budget.booked_usd == budget.conservative_usd > 0
    assert error.usage["usage_unknown"] and error.usage["reserved"]
    assert error.usage["usd"] == budget.booked_usd


def test_unknown_writer_and_check_usage_keeps_checked_answer_and_estimate_notice(monkeypatch):
    configure_gemini(monkeypatch)

    class Model(FakeModel):
        def complete_json(self, **kwargs):
            content, _ = super().complete_json(**kwargs)
            return content, dict(UNKNOWN)

    seen = {}
    base = make_research(live=False)

    def research(ctx, *args):
        seen["budget"] = ctx.model_budget
        ticket = ctx.model_budget.reserve_cost(0.02, research=True)
        result = base(ctx, *args)
        ctx.model_budget.settle_cost(ticket, result["usd"])
        return result

    out = Harness(model=Model(), research=research, check=None).run()
    assert out["answer"]["claims"]
    assert CONSERVATIVE_NOTICE in out["run"]["notices"]
    assert out["run"]["model_usd"] == pytest.approx(seen["budget"].booked_usd)
    assert seen["budget"].booked_usd > 0
    assert not seen["budget"].stopped


def test_unguarded_research_unknown_usage_stops_with_an_estimate_not_zero(monkeypatch):
    ctx, options, progress, _ = setup_for()
    response = reply(ftext("retained note"))
    response.usage_metadata = None
    client = FakeClient(response)
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    result = gemini_research.gemini_research(ctx, "Question?", options, progress, lambda: False)
    assert result["note"].startswith("retained note")
    assert result["stopped"] and result["usage_unknown"]
    assert result["usd"] == ctx.research_usd > 0
    assert len(client.calls) == 1


def test_guarded_research_unknown_usage_retains_exact_reserve_once(monkeypatch):
    ctx, options, progress, _ = setup_for()
    budget = make_budget(hold=1, research=1)
    ctx.model_budget = budget
    response = reply(ftext("retained note"))
    response.usage_metadata = None
    monkeypatch.setattr(gemini_research, "client_factory", lambda: FakeClient(response))
    result = gemini_research.gemini_research(ctx, "Question?", options, progress, lambda: False)
    assert result["usage_unknown"] and result["stopped"]
    assert result["usd"] == ctx.research_usd == budget.booked_usd == budget.conservative_usd > 0
    assert result["tokens"]["input"] > 0 and result["tokens"]["output"] > 0
