"""Controlled research failures and limits through Ask's answer boundary."""

import pytest
from types import SimpleNamespace

from core.agent import ask, gemini_research
from core.agent.answer import validate_answer
from core.agent.tests.test_ask_t2 import Lanes, t2
from core.agent.tests.test_ask import COUNT_SQL, Harness
from core.agent.tests.test_gemini_research import FakeClient, fcall, reply, setup_for
from core.agent.writer import WRITER_SCHEMA


class PartialLanes(Lanes):
    def __call__(self, ctx, prompt, options, emit, should_stop):
        result = super().__call__(ctx, prompt, options, emit, should_stop)
        if "X" == prompt.split("Your platforms: ", 1)[1].split(".", 1)[0]:
            raise RuntimeError("private failure text must not be published")
        return result


def test_requested_lane_without_a_source_call_is_not_reported_as_searched():
    def research(ctx, prompt, options, emit, should_stop):
        return {"note": "No source calls were made.", "tokens": {"input": 0, "output": 0}, "usd": 0.0}

    h = t2(research)
    out = h.run(tier="T2", question="What is behind amapiano on Reddit in South Africa this week?")
    assert out["run"]["source_status"] == []
    assert h.clients[0].calls == []
    assert not any("dedicated first-round lane" in gap["what"] for gap in out["answer"]["gaps"])
    assert validate_answer(out["answer"]) == []


def test_requested_lane_failure_retains_other_evidence_and_partial_answer():
    class FailedRequested(Lanes):
        def __call__(self, ctx, prompt, *args):
            result = super().__call__(ctx, prompt, *args)
            if "Your platforms: Reddit and Threads." in prompt:
                raise RuntimeError("requested lane failed")
            return result

    h = t2(FailedRequested(parties=4))
    out = h.run(tier="T2", question="What is behind amapiano on Reddit in South Africa this week?")
    assert out["answer"]["claims"]
    assert out["answer"]["status"] == "partial"
    assert any("Reddit" in gap["what"] and "researcher" in gap["what"].lower()
               for gap in out["answer"]["gaps"])
    assert "requested lane failed" not in str(out)
    assert validate_answer(out["answer"]) == []


def test_one_failed_researcher_keeps_checked_answer_and_explicit_partial_coverage():
    h = t2(PartialLanes(parties=4))
    out = h.run(tier="T2")
    assert validate_answer(out["answer"]) == []
    assert out["answer"]["claims"]
    assert out["answer"]["status"] != "complete"
    gaps = [gap for gap in out["answer"]["gaps"] if "researcher" in gap["what"].lower()]
    assert gaps and "incomplete" in gaps[0]["why"].lower()
    assert "private failure" not in str(out)
    prompt = next(call["user"] for call in h.model.calls if call["schema"] is WRITER_SCHEMA)
    assert "Research coverage" in prompt and "incomplete" in prompt
    assert out["run"]["credits"] == 1.0


def test_all_failed_researchers_still_fail_with_preserved_spend():
    class Broken(Lanes):
        def __call__(self, *args):
            super().__call__(*args)
            raise RuntimeError("all failed")

    h = t2(Broken(parties=4))
    with pytest.raises(RuntimeError, match="all failed") as err:
        h.run(tier="T2")
    assert err.value.run["credits"] == 1.0
    assert err.value.run["model_usd"] > 0


def test_last_turn_tool_result_is_retained_with_a_coverage_gap(monkeypatch):
    ctx, options, progress, _ = setup_for()
    ctx.limits = {**ctx.budget, "max_turns": 1}
    client = FakeClient(reply(fcall("resolve_dates", expression="yesterday")))
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    result = gemini_research.gemini_research(ctx, "Question?", options, progress, lambda: False)
    assert result["stopped"] is False
    assert len(client.calls) == 1
    assert ctx.research_gaps
    assert "turn limit" in ctx.research_gaps[0]["why"]
    assert "incomplete" in result["note"]


def test_final_turn_results_reach_writer_and_partial_answer_through_real_gates(monkeypatch):
    client = FakeClient(reply(fcall("search_posts", query="amapiano"),
                              fcall("sql_query", sql=COUNT_SQL, purpose="amapiano counts",
                                    params={"term": "amapiano"})))
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)

    def research(ctx, *args):
        ctx.limits = {**ctx.budget, "max_turns": 1}
        return gemini_research.gemini_research(ctx, *args)

    h = Harness(research=research, check=None)
    out = h.run()
    assert validate_answer(out["answer"]) == []
    assert out["answer"]["claims"]
    assert out["answer"]["status"] != "complete"
    assert any("turn limit" in gap["why"] for gap in out["answer"]["gaps"])
    prompt = next(call["user"] for call in h.model.calls if call["schema"] is WRITER_SCHEMA)
    assert "tt_1" in prompt and "amapiano counts" in prompt
    assert len(client.calls) == 1


def test_research_budget_limit_records_incomplete_coverage(monkeypatch):
    ctx, options, progress, _ = setup_for()
    ctx.limits = {**ctx.budget, "max_budget_usd": 0.00001}
    client = FakeClient(reply(fcall("budget_status")))
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    result = gemini_research.gemini_research(ctx, "Question?", options, progress, lambda: False)
    assert result["stopped"] is False
    assert len(client.calls) == 1
    assert "research model budget" in ctx.research_gaps[0]["why"]


@pytest.mark.parametrize("global_stop", [False, True])
def test_partial_recovery_does_not_swallow_stop_or_shared_budget_failure(global_stop):
    ctx, setup, progress, _ = setup_for()
    budget = SimpleNamespace(stopped=global_stop)
    ctx.model_budget = budget
    clients = [ask._Counted(None), ask._Counted(None)]
    jobs = [(ctx.lane(dict(ctx.budget)), str(i), clients[i]) for i in range(2)]
    error = RuntimeError("shared budget failed") if global_stop else ask._StopRequested()

    def research(lane, prompt, *args):
        if prompt == "0":
            raise error
        return {"note": "some evidence", "tokens": {"input": 1, "output": 1}, "usd": 0.01}

    with pytest.raises(type(error)) as caught:
        ask._research_lanes(SimpleNamespace(research=research), jobs, setup, progress,
                            lambda: False, ctx, ask._Counted(None))
    assert caught.value is error
    assert ctx.research_gaps == []
