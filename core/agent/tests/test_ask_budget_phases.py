import json

import pytest

from core.agent import ask, gemini_research, investigate
from core.agent.tests.test_ask import Harness, NOW, K4IntegrationModel, make_research
from core.agent.tests.test_ask_model_budget import configure_gemini, make_budget
from core.agent.tests.test_gemini_research_budget import FakeClient, fcall, reply, setup
from core.agent.writer import K4_REWRITE_SCHEMA


@pytest.mark.parametrize("tier", ["T2", "T3"])
def test_all_gemini_research_lanes_share_the_approved_hold(monkeypatch, tier):
    configure_gemini(monkeypatch)
    seen = []

    def research(ctx, *args):
        seen.append(getattr(ctx, "model_budget", None))
        return {"note": "", "tokens": {}, "usd": 0, "stopped": True}

    h = Harness(research=research)
    request = {"tier": tier}
    if tier == "T3":
        plan = {"sub_questions": [{"id": "q1", "text": "Which sounds are spreading?",
                                  "platforms": ["tiktok"], "credits": 0}],
                "gap_round": False, "max_credits": 0, "max_model_usd": 5}
        request.update(plan=plan, max_credits=0, max_model_usd=5)
    out = h.run(**request)
    assert seen and all(budget is not None and budget is seen[0] for budget in seen)
    approved = 5 if tier == "T3" else ask.hold_usd(tier, ask.MODEL)
    assert 0 <= approved - seen[0].cap_micros / 1e6 < 0.000001
    assert out["run"]["model_usd"] == 0


def test_confirmed_plan_near_the_previous_floor_keeps_its_approved_hold(monkeypatch):
    configure_gemini(monkeypatch)
    floor = ask.gate_usd(ask.MODEL, 1) - ask.once_usd(ask.MODEL)
    approved = floor + ask.once_usd(ask.MODEL) / 2
    plan = {"sub_questions": [{"id": "q1", "text": "Which sounds are spreading?",
                              "platforms": ["tiktok"], "credits": 0}],
            "gap_round": False, "max_credits": 0, "max_model_usd": approved}
    request = {"plan": plan, "max_credits": 0, "max_model_usd": approved}
    result = ask._investigation(request, model=ask.MODEL, now=NOW)
    assert result["max_model_usd"] == approved
    assert result["plan"]["max_model_usd"] == approved
    assert investigate.lane_usd(result["plan"], approved, ask.MODEL) >= 0
    with pytest.raises(ValueError, match="max_model_usd"):
        investigate.validate_plan(plan, ask.MODEL, now=NOW)


def test_research_stop_after_model_response_prevents_tool_dispatch(monkeypatch):
    budget = make_budget(hold=1, research=1)
    ctx, options, emit = setup(budget)
    stopped = [False]
    client = FakeClient(reply(fcall("resolve_dates", text="this week")))
    original = client.generate_content

    def generate(**kwargs):
        response = original(**kwargs)
        stopped[0] = True
        return response

    client.models.generate_content = generate
    ran = []
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    monkeypatch.setattr(gemini_research, "build_functions", lambda *args: {"resolve_dates": object()})
    monkeypatch.setattr(gemini_research, "_run_call", lambda *args: ran.append(True) or ("{}", False))
    result = gemini_research.gemini_research(ctx, "Question?", options, emit, lambda: stopped[0])
    assert result["stopped"] is True
    assert ran == []
    assert budget.booked_usd > 0


def test_phase_seconds_survive_the_run_record_with_no_query_text(monkeypatch, tmp_path):
    configure_gemini(monkeypatch)
    ticks = [0.0]
    monkeypatch.setattr(ask.time, "monotonic", lambda: ticks[0])
    original = make_research(live=False)

    def research(*args):
        ticks[0] += 3
        return original(*args)

    h = Harness(research=research)
    out = h.run()
    record = json.loads(json.dumps(out))
    phases = record["run"]["phase_seconds"]
    assert set(phases) == {"plan", "research", "write", "checks", "rewrite"}
    assert phases["research"] == 3
    assert all(type(value) in (int, float) and value >= 0 for value in phases.values())
    assert "question" not in json.dumps(phases).lower()
    from core.api import agent_app, store

    monkeypatch.setenv("F42_DATA", "fixture")
    monkeypatch.setattr(agent_app, "SINK", [])
    held = agent_app.Ask({"ask_id": "a_phase_test", "question": "Which sounds are spreading?", "tier": "T1"})
    held.record.update(answer=out["answer"], run=out["run"], status="complete", finished_at=NOW.isoformat())
    assert agent_app.sink(held)
    row = agent_app.SINK[0]
    stored = json.loads(row["record"])
    assert stored["run"]["phase_seconds"] == phases
    (tmp_path / "runs.json").write_text(json.dumps([{**row, "record": stored}]), encoding="utf-8")
    reopened = store.FixtureStore(tmp_path).ask_record("a_phase_test")
    assert reopened["run"]["phase_seconds"] == phases


def test_cancel_during_retry_wait_prevents_another_structured_model_call(monkeypatch):
    stopped = [False]
    calls = []

    class Model:
        def complete_json(self, **kwargs):
            calls.append(kwargs)
            error = RuntimeError("server failed")
            error.code = 500
            raise error

    monkeypatch.setattr(gemini_research.time, "sleep", lambda _: stopped.__setitem__(0, True))
    budget = make_budget(hold=1, research=1)
    guarded = ask._StopAwareModel(Model(), lambda: stopped[0], budget)
    with pytest.raises(ask._StopRequested):
        guarded.complete_json(system="s", user="u", schema={}, model="gemini-test", max_tokens=10)
    assert len(calls) == 1
    assert budget.booked_usd > 0


def test_budget_never_rounds_the_approved_hold_up():
    budget = make_budget(hold=0.0010001, research=0.0010001)
    assert budget.cap_micros == budget.research_cap_micros == 1000


def test_claim_narrowing_time_is_recorded_as_rewrite(monkeypatch):
    configure_gemini(monkeypatch)
    ticks = [0.0]
    monkeypatch.setattr(ask.time, "monotonic", lambda: ticks[0])

    class Model(K4IntegrationModel):
        def complete_json(self, **kwargs):
            if kwargs["schema"] is K4_REWRITE_SCHEMA:
                ticks[0] += 2
            return super().complete_json(**kwargs)

    out = Harness(model=Model()).run()
    assert out["run"]["phase_seconds"]["rewrite"] == 2
