"""Investigations at T3 (BUILD.md 3.4; AGENT.md effort tiers; core/api/contract.md section 13.1 on full-42-l4): the
plan, its estimate, the edit, and one whole investigation on fakes. No network, no model, no SocialCrawl."""

import copy
import re
import threading
from datetime import datetime

import pytest

from core.agent import ask, critic, investigate, skills
from core.agent.answer import validate_answer
from core.agent.context import TIERS, Refused
from core.agent.tests.test_ask import COUNT_SQL, Harness, NOW
from core.agent.tests.test_ask_t2 import CriticModel
from core.agent.tools.socialcrawl import socialcrawl_call
from core.agent.tools.sql_query import sql_query
from core.agent.tools.warehouse import search_posts
from core.agent.writer import FIELDS_SCHEMA, SUPPORT_SCHEMA, WRITER_SCHEMA
from core.llm.provider import price_for, reserve_output

QUESTION = "What is behind amapiano in South Africa this week?"
ASK_DAILY = 600          # SETUP.md caps table
BEFORE_CAP_CUT = datetime(2026, 10, 2, 23, 59, tzinfo=ask.SAST)
AFTER_CAP_CUT = datetime(2026, 10, 3, 0, 1, tzinfo=ask.SAST)
PLANNED = [
    {"text": "Which amapiano sounds are people dancing to on TikTok?", "platforms": ["tiktok"]},
    {"text": "What do people on X say about amapiano at taxi ranks?", "platforms": ["x", "tiktok"]},
    {"text": "Which amapiano clips travel on Instagram reels?", "platforms": ["instagram", "tiktok"]},
    {"text": "What do amapiano mixes on YouTube show about the log drum?", "platforms": ["youtube", "tiktok"]},
    {"text": "What does the news say about amapiano this week?", "platforms": ["news", "tiktok"]},
]


class PlannerModel(CriticModel):
    """CriticModel, plus the planner: returns `planned` for the plan schema, or raises `fail` there."""

    def __init__(self, *scripts, planned=None, fail=None, usd=0.004):
        super().__init__(*scripts)
        self.planned = PLANNED if planned is None else planned
        self.fail, self.usd = fail, usd

    def complete_json(self, *, system, user, schema, model, max_tokens):
        if schema is not investigate.PLAN_SCHEMA:
            return super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)
        self.calls.append({"model": model, "schema": schema, "user": user, "max_tokens": max_tokens})
        if self.fail:
            raise self.fail
        return {"sub_questions": copy.deepcopy(self.planned)}, {"input_tokens": 900, "output_tokens": 300,
                                                                "usd": self.usd}


class LedgerClient:
    """The SocialCrawl client with its ledger check: every call must follow a quote for the same route, as L1's client
    checks the ledger and the caps before it calls. Each call charges its quote. Research lanes run in threads, so the
    check is per thread: a call must follow that thread's own last op, a quote for the same route."""

    def __init__(self, mode, run_id=None, quote=3.0):
        self.mode, self.run_id, self.price = mode, run_id, quote
        self.ops, self.calls = [], []
        self.last = {}  # thread id: that thread's last op

    def quote(self, route, params):
        self.ops.append(("quote", route))
        self.last[threading.get_ident()] = ("quote", route)
        return self.price

    def call(self, route, params, *, lane, run_id, max_credits):
        assert self.last.get(threading.get_ident()) == ("quote", route), "a call without a ledger check first"
        assert max_credits >= self.price
        self.ops.append(("call", route))
        self.last[threading.get_ident()] = ("call", route)
        self.calls.append(route)
        return {"items": [], "next_cursor": None, "credits_charged": self.price, "status": "ok", "cache_hit": False}


class Greedy:
    """A researcher that spends everything it is allowed: live TikTok searches until a guard refuses, then reports
    its whole USD budget as spent."""

    def __init__(self):
        self.calls = []

    def __call__(self, ctx, prompt, options, emit, should_stop):
        self.calls.append({"prompt": prompt, "budget": dict(ctx.budget), "routes": options.client.routes,
                           "system": options.system_prompt})
        search_posts(ctx, options.warehouse, "amapiano")
        sql_query(ctx, options.warehouse, COUNT_SQL, purpose="posts, authors and platforms for amapiano",
                  params={"term": "amapiano"})
        for _ in range(500):
            try:
                socialcrawl_call(ctx, options.client, "tiktok", "search/top", {"query": "amapiano"}, max_credits=5)
            except Refused:
                break
        return {"note": "Reading: amapiano, South Africa, this week.", "tokens": {"input": 100, "output": 10},
                "usd": ctx.budget["max_budget_usd"]}


def harness(research=None, model=None, **kwargs):
    h = Harness(research=research or Greedy(), model=model or PlannerModel({"c3": {"verdict": "needs_evidence"}}),
                **kwargs)
    h.ledgers = []
    h.research = h.deps.research

    def socialcrawl(mode, run_id):
        client = LedgerClient(mode, run_id)
        h.ledgers.append(client)
        return client

    h.deps.socialcrawl = socialcrawl
    return h


def draft(h, **request):
    request.setdefault("budget_left", {"credits": ASK_DAILY, "model_usd": ask.model_daily_usd(now=h.deps.now())})
    return investigate.plan_investigation({"question": QUESTION, "market": "ZA", **request}, deps=h.deps)


def start(h, plan, **request):
    body = {"tier": "T3", "plan": plan, "max_credits": plan["max_credits"], "max_model_usd": plan["max_model_usd"],
            "investigation_id": "i_20260928_test", **request}
    return h.run(**body)


# The tier


def test_t3_is_in_the_tiers_with_the_agent_md_and_setup_md_numbers():
    # credits: AGENT.md, "the whole ASK_DAILY share", and ASK_DAILY is SETUP.md's 600. Calls: 15 each for at most 8
    # researchers. Turns: the 20 hard stop per researcher. No USD: SETUP.md sets none for T3, so the plan's does.
    assert TIERS["T3"] == {"credits": ASK_DAILY, "calls": 8 * 15, "max_turns": 20, "max_budget_usd": None}
    assert (investigate.MIN_RESEARCHERS, investigate.MAX_RESEARCHERS) == (5, 8)
    assert investigate.CALLS_EACH == 15


# Drafting a plan


def test_a_request_drafts_a_plan_with_its_estimate_and_a_planner_run():
    h = harness()
    out = draft(h)
    plan, estimate, run = out["plan"], out["estimate"], out["run"]
    assert [q["id"] for q in plan["sub_questions"]] == ["q1", "q2", "q3", "q4", "q5"]
    assert [q["text"] for q in plan["sub_questions"]] == [q["text"] for q in PLANNED]
    assert plan["researchers"] == 5 and plan["gap_round"] is True
    assert plan["max_credits"] == ASK_DAILY
    # every sub-question gets a fair slice of the first round and enrichment; the gap round keeps its 25%
    assert sum(q["credits"] for q in plan["sub_questions"]) == pytest.approx(0.75 * ASK_DAILY)
    assert estimate == investigate.estimate_plan(plan)
    assert estimate["credits"] <= plan["max_credits"] and estimate["model_usd"] <= plan["max_model_usd"]
    assert run["stage"] == "investigation" and run["model_usd"] == pytest.approx(0.004)
    assert run["plan"] == plan and run["tokens"] == {"input": 900, "output": 300}
    assert h.ledgers == []  # drafting spends no SocialCrawl credits
    assert len(h.model.by(investigate.PLAN_SCHEMA)) == 1


def test_the_planner_call_stays_under_its_own_usd_ceiling():
    h = harness()
    draft(h)
    call = h.model.by(investigate.PLAN_SCHEMA)[0]
    price = price_for(call["model"])
    most = (investigate.PLANNER_INPUT_TOKENS * price["input"]
            + reserve_output(call["model"], call["max_tokens"]) * price["output"]) / 1_000_000
    assert investigate.planner_usd() == pytest.approx(most)
    assert most <= investigate.PLANNER_USD == 0.50
    assert len(call["user"]) <= investigate.PLANNER_INPUT_TOKENS  # a token is at least one character


def test_the_longest_request_still_fits_the_planners_input_bound():
    h = harness()
    draft(h, question="amapiano " * 222, angles=["taxi rank speakers and log drums " * 9] * 8)
    call = h.model.by(investigate.PLAN_SCHEMA)[0]
    assert len(investigate.PLANNER_SYSTEM) + len(call["user"]) <= investigate.PLANNER_INPUT_TOKENS


def test_the_plan_never_exceeds_the_budgets_left_when_it_was_made():
    h = harness()
    out = draft(h, budget_left={"credits": 240, "model_usd": 7.5})
    plan = out["plan"]
    assert plan["max_credits"] == 240 and plan["max_model_usd"] <= 7.5
    assert sum(q["credits"] for q in plan["sub_questions"]) == pytest.approx(0.75 * 240)


def test_a_model_budget_too_small_to_write_the_answer_is_refused_before_the_planner_spends():
    h = harness()
    with pytest.raises(ValueError, match="model budget"):
        draft(h, budget_left={"credits": 600, "model_usd": 0.6})
    assert h.model.calls == []


def test_a_failed_planner_gives_the_platform_plan_and_its_spend_still_counts():
    failure = RuntimeError("truncated")
    failure.usage = {"input_tokens": 900, "output_tokens": 2000, "usd": 0.03}
    h = harness(model=PlannerModel(fail=failure))
    out = draft(h)
    plan = out["plan"]
    assert [q["platforms"] for q in plan["sub_questions"]] == [[g] for g in ask.GROUP_ORDER["ZA"][:5]]
    assert all(q["text"] == QUESTION for q in plan["sub_questions"])
    assert out["run"]["model_usd"] == pytest.approx(0.03)


def test_a_failed_planner_without_usage_counts_its_ceiling_without_fake_tokens():
    h = harness(model=PlannerModel(fail=RuntimeError("response lost after provider acceptance")))
    run = draft(h)["run"]

    assert run["model_usd"] == investigate.PLANNER_USD
    assert "tokens" not in run
    assert run["notices"] == ["Planner usage was unavailable; USD 0.50 is counted as spent."]
    assert len(h.model.by(investigate.PLAN_SCHEMA)) == 1


def test_a_known_zero_planner_charge_is_not_replaced_by_the_ceiling():
    failure = RuntimeError("planner returned no plan")
    failure.usage = {"input_tokens": 0, "output_tokens": 0, "usd": 0}
    h = harness(model=PlannerModel(fail=failure))

    run = draft(h)["run"]

    assert run["model_usd"] == 0
    assert run["tokens"] == {"input": 0, "output": 0}
    assert "notices" not in run


def test_billed_planner_usage_survives_a_post_call_plan_validation_error(monkeypatch):
    h = harness()

    def fail_validation(*args, **kwargs):
        raise RuntimeError("plan validation failed")

    monkeypatch.setattr(investigate, "validate_plan", fail_validation)
    with pytest.raises(RuntimeError, match="plan validation failed") as caught:
        draft(h)

    assert caught.value.run["model_usd"] == pytest.approx(h.model.usd)
    assert caught.value.run["tokens"] == {"input": 900, "output": 300}
    assert len(h.model.by(investigate.PLAN_SCHEMA)) == 1


def test_planner_preflight_reserves_the_failure_ceiling():
    h = harness()
    left = investigate.planner_usd(ask.MODEL) + ask.gate_usd(ask.MODEL, 2) + 0.01

    with pytest.raises(ValueError, match="model budget"):
        draft(h, budget_left={"credits": ASK_DAILY, "model_usd": left})

    assert h.model.calls == []


def test_same_question_drafts_at_the_same_clock_get_distinct_planner_run_ids():
    h = harness()

    run_ids = [draft(h)["run"]["run_id"] for _ in range(2)]

    assert len(set(run_ids)) == 2
    assert all(run_id.startswith("r_") for run_id in run_ids)


@pytest.mark.parametrize("planned", [
    [{"text": "What do Gen Z fans say about amapiano?", "platforms": ["tiktok"]}] * 5,  # an age lens (rule 1)
    [{"text": "What do fans say about amapiano?", "platforms": ["google_trends"]}] * 5,  # not a platform group
    PLANNED[:2],  # fewer than five angles
])
def test_planner_output_that_breaks_a_rule_is_dropped_for_the_platform_plan(planned):
    h = harness(model=PlannerModel(planned=planned))
    plan = draft(h)["plan"]
    assert all(q["text"] == QUESTION for q in plan["sub_questions"]) and len(plan["sub_questions"]) == 5
    assert all(not ask._text_breaches(q["text"]) for q in plan["sub_questions"])


def test_angles_the_user_gives_reach_the_planner_fenced_and_a_breaching_angle_is_refused():
    h = harness()
    draft(h, angles=["taxi rank speakers", "Durban dance challenge"])
    user = h.model.by(investigate.PLAN_SCHEMA)[0]["user"]
    assert "<untrusted_content>" in user and "taxi rank speakers" in user
    with pytest.raises(ValueError):
        draft(harness(), angles=["what millennials think"])


@pytest.mark.parametrize("patch", [{"question": "hi"}, {"market": "GH"}, {"angles": "one"},
                                   {"angles": ["a" * 301]}, {"angles": ["taxi"] * 9},
                                   {"budget_left": {"credits": -1, "model_usd": 5}}])
def test_bad_draft_requests_raise(patch):
    with pytest.raises(ValueError):
        draft(harness(), **patch)


def test_a_missing_planner_budget_fails_before_the_model_call():
    h = harness()
    with pytest.raises(ValueError, match="budget_left"):
        investigate.plan_investigation({"question": QUESTION, "market": "ZA"}, deps=h.deps)
    assert h.model.calls == []


@pytest.mark.parametrize("budget", [None, {}, {"credits": 600}, {"credits": 600, "model_usd": True}])
def test_an_invalid_planner_budget_fails_before_the_model_call(budget):
    h = harness()
    with pytest.raises(ValueError, match="budget_left"):
        investigate.plan_investigation({"question": QUESTION, "market": "ZA", "budget_left": budget}, deps=h.deps)
    assert h.model.calls == []


# Estimating and editing a plan


def test_estimate_plan_counts_credits_calls_usd_and_minutes_without_a_model_call():
    h = harness()
    plan = draft(h)["plan"]
    calls_before = len(h.model.calls)
    estimate = investigate.estimate_plan(plan)
    assert len(h.model.calls) == calls_before
    assert set(estimate) == {"credits", "calls", "model_usd", "minutes"}
    assert estimate["credits"] == pytest.approx(ASK_DAILY)  # 75% across the angles plus the 25% gap reserve
    assert estimate["calls"] <= TIERS["T3"]["calls"]
    assert 0 < estimate["model_usd"] <= plan["max_model_usd"] <= ask.model_daily_usd(now=NOW)
    assert estimate["minutes"] > 0


def test_an_edited_plan_gets_a_fresh_smaller_estimate():
    plan = draft(harness())["plan"]
    edited = copy.deepcopy(plan)
    edited["sub_questions"] = edited["sub_questions"][:3]
    for q in edited["sub_questions"]:
        q["credits"] = 50
    edited["gap_round"] = False
    before, after = investigate.estimate_plan(plan), investigate.estimate_plan(edited)
    assert after["credits"] == 150 < before["credits"]
    assert after["model_usd"] < before["model_usd"] and after["minutes"] < before["minutes"]
    assert investigate.validate_plan(edited)["researchers"] == 3  # the count follows the sub-questions


def _plan():
    return draft(harness())["plan"]


@pytest.mark.usefixtures("old_model_cap_schedule")
def test_plan_validation_uses_the_shared_cap_for_its_supplied_decision_time():
    plan = _plan()
    plan["max_model_usd"] = 25
    assert ask.model_daily_usd(now=BEFORE_CAP_CUT) == 80
    assert investigate.validate_plan(plan, now=BEFORE_CAP_CUT)["max_model_usd"] == 25
    assert ask.model_daily_usd(now=AFTER_CAP_CUT) == 20
    with pytest.raises(ValueError, match="max_model_usd"):
        investigate.validate_plan(plan, now=AFTER_CAP_CUT)


def test_validate_plan_rejects_a_two_pass_plan_that_cannot_cover_k4_work(monkeypatch):
    monkeypatch.setattr(critic, "critic_model", lambda: "gemini-3.8-flash")
    plan = _plan()
    plan["max_model_usd"] = 4

    with pytest.raises(ValueError, match="max_model_usd"):
        investigate.validate_plan(plan, model="gemini-3.8-flash", now=NOW)


@pytest.mark.usefixtures("old_model_cap_schedule")
def test_run_revalidates_a_confirmed_plan_after_the_spend_read_at_the_cutoff():
    plan = _plan()
    plan["max_model_usd"] = 25
    spend_reads = []
    h = harness(spent=lambda: spend_reads.append(True) or 0)
    times = iter((BEFORE_CAP_CUT, AFTER_CAP_CUT))
    h.deps.now = lambda: next(times)

    with pytest.raises(ValueError, match="max_model_usd"):
        start(h, plan)

    assert spend_reads == [True]
    assert h.research.calls == []
    assert h.model.calls == []


@pytest.mark.usefixtures("old_model_cap_schedule")
def test_run_ask_revalidates_a_t3_plan_of_usd_30_after_spend_read_crosses_the_sast_cap():
    plan = _plan()
    plan["max_model_usd"] = 30
    assert investigate.validate_plan(plan, now=BEFORE_CAP_CUT)["max_model_usd"] == 30

    spend_reads = []
    h = harness(spent=lambda: spend_reads.append(True) or 0)
    times = iter((BEFORE_CAP_CUT, AFTER_CAP_CUT))
    h.deps.now = lambda: next(times)

    with pytest.raises(ValueError, match="max_model_usd"):
        start(h, plan)

    assert spend_reads == [True]
    assert h.model.calls == []
    assert h.ledgers == []
    assert h.research.calls == []


@pytest.mark.parametrize("edit", [
    lambda p: p["sub_questions"].extend(copy.deepcopy(p["sub_questions"][:4])),        # nine researchers
    lambda p: p["sub_questions"].clear(),
    lambda p: p["sub_questions"][0].update(platforms=["myspace"]),
    lambda p: p["sub_questions"][0].update(platforms=[]),
    lambda p: p["sub_questions"][0].update(text="How do Gen Z shoppers feel about amapiano?"),
    lambda p: p["sub_questions"][0].update(text="x"),
    lambda p: p["sub_questions"][1].update(id="q1"),
    lambda p: p["sub_questions"][0].update(credits=-5),
    lambda p: p["sub_questions"][0].update(credits=400),                                 # past the first round
    lambda p: p.update(max_credits=ASK_DAILY + 1),                                       # no cap is raised here
    lambda p: p.update(max_model_usd=ask.model_daily_usd(now=NOW) + 1),
    lambda p: p.update(max_model_usd=0.2),                                               # cannot write the answer
    lambda p: p.update(gap_round="yes"),
    lambda p: p.update(extra=1),
])
def test_validate_plan_refuses_a_plan_that_breaks_a_rule_or_a_cap(edit):
    plan = _plan()
    edit(plan)
    with pytest.raises(ValueError):
        investigate.validate_plan(plan, now=NOW)


# Running an investigation


def test_one_investigation_completes_within_its_budget():
    """BUILD.md 3.4's check: a whole investigation on fake deps, every researcher spending all it may, stays under
    the plan's credits, T3's calls and the plan's model dollars, with the ledger checked before every call."""
    h = harness()
    plan = draft(h)["plan"]
    out = start(h, plan)
    run, answer = out["run"], out["answer"]
    assert validate_answer(answer) == []
    assert run["tier"] == "T3"
    assert len(h.research.calls) == 6  # five angles and the one gap round
    client = h.ledgers[0]
    assert client.calls and len(client.calls) == sum(1 for op in client.ops if op[0] == "call")
    assert run["credits"] == pytest.approx(3.0 * len(client.calls))
    assert run["credits"] <= plan["max_credits"] <= TIERS["T3"]["credits"]
    assert len(client.calls) <= TIERS["T3"]["calls"]
    assert run["model_usd"] <= plan["max_model_usd"] <= ask.model_daily_usd(now=NOW)
    for lane in h.research.calls[:5]:
        assert lane["budget"]["calls"] <= investigate.CALLS_EACH
        assert lane["budget"]["max_turns"] == TIERS["T3"]["max_turns"]
    assert run["plan"] == investigate.validate_plan(plan)
    assert run["investigation_id"] == "i_20260928_test"


@pytest.mark.parametrize("credits, usd", [(200, 9.0), (90, 8.0), (0, 7.0)])
def test_the_run_stops_at_the_ceilings_the_start_sets(credits, usd):
    h = harness()
    plan = draft(h)["plan"]
    out = start(h, plan, max_credits=credits, max_model_usd=usd)
    assert out["run"]["credits"] <= credits
    assert out["run"]["model_usd"] <= usd
    assert validate_answer(out["answer"]) == []


def test_each_researcher_takes_its_own_angle_and_only_its_platforms():
    h = harness()
    plan = draft(h)["plan"]
    start(h, plan)
    first = h.research.calls[:5]  # the researchers run in threads, so they log in any order
    for sub in plan["sub_questions"]:
        [lane] = [c for c in first if f"Your angle: {sub['text']}\n" in c["prompt"]]
        assert lane["routes"] == sorted({r for g in sub["platforms"] for r in ask.group_routes(g)})
        assert lane["budget"]["credits"] == pytest.approx(sub["credits"])
    # every researcher loop, the gap researcher's included, fits the research share of the plan's dollars
    lanes_usd = sum(c["budget"]["max_budget_usd"] for c in h.research.calls)
    assert lanes_usd + ask.gate_usd(ask.MODEL, 2) <= plan["max_model_usd"] + 1e-9


def test_a_plan_without_the_gap_round_runs_no_gap_round():
    h = harness()
    plan = draft(h)["plan"]
    plan["gap_round"] = False
    out = start(h, plan)
    assert len(h.research.calls) == 5
    assert len(h.model.by(critic.CRITIC_SCHEMA)) == 1
    assert out["run"]["credits"] <= plan["max_credits"]


def test_a_finished_investigation_records_a_notice_for_the_app():
    h = harness()
    out = start(h, draft(h)["plan"])
    notice = out["run"]["notice"]
    assert notice["investigation_id"] == "i_20260928_test"
    assert notice["status"] == out["answer"]["status"]
    assert notice["at"].startswith("2026-09-28T08:15:00")
    assert not re.search(r"\d", notice["text"])  # no figure without its query on Today
    last = [e for e in h.events if e["event"] == "step"][-1]
    assert last["kind"] == "note" and last["text"] == notice["text"]


def test_a_stopped_investigation_records_a_stopped_notice():
    h = harness()
    plan = draft(h)["plan"]
    h.stop_flag["stop"] = True
    out = start(h, plan)
    assert out["run"]["notice"]["status"] == "stopped"


def test_stop_during_the_writer_skips_later_paid_checks_and_records_spend_and_notice():
    stop_flag = {"stop": False}

    class StopsAfterWriter(PlannerModel):
        def complete_json(self, *, system, user, schema, model, max_tokens):
            result = super().complete_json(system=system, user=user, schema=schema, model=model,
                                           max_tokens=max_tokens)
            if schema is WRITER_SCHEMA:
                stop_flag["stop"] = True
            return result

    h = harness(model=StopsAfterWriter(), stop_flag=stop_flag)
    plan = draft(h)["plan"]
    out = start(h, plan)

    assert validate_answer(out["answer"]) == []
    assert out["answer"]["status"] == "insufficient_evidence"
    assert [call["schema"] for call in h.model.calls] == [investigate.PLAN_SCHEMA, WRITER_SCHEMA]
    assert all(call["schema"] not in (SUPPORT_SCHEMA, FIELDS_SCHEMA, critic.CRITIC_SCHEMA) for call in h.model.calls)
    assert h.tables.inserts == []
    assert out["run"]["model_usd"] >= 0.0135
    assert set(out["run"]["notice"]) == {"investigation_id", "status", "text", "at"}
    assert out["run"]["notice"]["status"] == "stopped"
    assert out["run"]["notice"]["text"] == ask.NOTICES["stopped"]
    last = [event for event in h.events if event["event"] == "step"][-1]
    assert last["kind"] == "note" and last["text"] == out["run"]["notice"]["text"]


def test_a_failed_investigation_carries_a_failed_notice_on_its_run():
    def broken(ctx, prompt, options, emit, should_stop):
        raise RuntimeError("research broke")

    h = harness(research=broken)
    plan = draft(h)["plan"]
    with pytest.raises(RuntimeError) as err:
        start(h, plan)
    assert err.value.run["notice"]["status"] == "failed"
    assert err.value.run["model_usd"] <= plan["max_model_usd"]


def test_a_billed_writer_failure_keeps_its_tokens_and_cost_on_the_t3_run():
    failure = RuntimeError("writer response was lost after billing")
    failure.usage = {"input_tokens": 333, "output_tokens": 222, "usd": 0.008}

    class BilledWriterFailure(PlannerModel):
        def complete_json(self, *, system, user, schema, model, max_tokens):
            if schema is WRITER_SCHEMA:
                raise failure
            return super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)

    h = harness(model=BilledWriterFailure())
    plan = draft(h)["plan"]
    with pytest.raises(RuntimeError, match="writer response was lost") as err:
        start(h, plan)

    run = err.value.run
    assert run["tokens"]["input"] >= failure.usage["input_tokens"]
    assert run["tokens"]["output"] >= failure.usage["output_tokens"]
    assert run["model_usd"] >= failure.usage["usd"]
    assert run["model_usd"] <= plan["max_model_usd"]
    assert run["notice"]["status"] == "failed"


def test_the_hold_on_todays_model_budget_is_the_plans_ceiling():
    plan = _plan()
    spent = ask.model_daily_usd(now=NOW) - plan["max_model_usd"] - 0.01
    out = start(harness(spent=lambda: spent), plan)
    assert out["run"]["tier"] == "T3"
    h = harness(spent=lambda: spent + 0.02)
    out = start(h, plan)
    assert out["run"]["tier"] == "T3"
    assert out["run"]["notice"]["status"] == "refused"
    assert h.research.calls == []
    assert h.ledgers and h.ledgers[0].calls == []


def test_t3_refuses_when_its_confirmed_hold_does_not_fit_instead_of_falling_back():
    plan = _plan()
    plan["max_credits"] = 0
    for sub in plan["sub_questions"]:
        sub["credits"] = 0
    plan["max_model_usd"] = 10
    spent = ask.model_daily_usd(now=NOW) - plan["max_model_usd"] + 0.01
    h = harness(spent=lambda: spent)

    out = start(h, plan)

    assert out["run"]["tier"] == "T3"
    assert out["run"]["notice"]["status"] == "refused"
    assert h.research.calls == []
    assert h.ledgers and h.ledgers[0].calls == []


def test_run_ask_refuses_a_t3_plan_under_the_expanded_k4_gate_before_dispatch(monkeypatch):
    monkeypatch.setattr(ask, "MODEL", "gemini-3.8-flash")
    monkeypatch.setattr(critic, "critic_model", lambda: "gemini-3.8-flash")
    plan = _plan()
    plan["max_model_usd"] = 4
    spend_reads = []
    h = harness(spent=lambda: spend_reads.append(True) or 0)

    with pytest.raises(ValueError, match="max_model_usd"):
        start(h, plan)

    assert spend_reads == [True]
    assert h.model.calls == []
    assert h.ledgers == []
    assert h.research.calls == []


@pytest.mark.parametrize("patch", [
    {"plan": None},                                                  # T3 runs only a confirmed plan
    {"max_credits": None}, {"max_model_usd": None},
    {"max_credits": ASK_DAILY + 1}, {"max_model_usd": ask.model_daily_usd(now=NOW) + 1},
    {"skill": "brand-implication"},
    {"tier": "T2"},                                                  # a plan runs only at T3
])
def test_bad_investigation_requests_raise(patch):
    h = harness()
    plan = draft(h)["plan"]
    body = {"tier": "T3", "plan": plan, "max_credits": plan["max_credits"], "max_model_usd": plan["max_model_usd"],
            **patch}
    body = {k: v for k, v in body.items() if v is not None}
    with pytest.raises(ValueError):
        h.run(**body)
    assert h.ledgers == []


def test_ceilings_above_the_plans_own_are_refused():
    h = harness()
    plan = draft(h, budget_left={"credits": 300, "model_usd": 8})["plan"]
    with pytest.raises(ValueError):
        start(h, plan, max_credits=301)
    with pytest.raises(ValueError):
        start(h, plan, max_model_usd=plan["max_model_usd"] + 0.01)


# The investigation skill (AGENT.md skills)


def test_the_investigation_skill_is_known_and_runs_every_t3_ask():
    assert "investigation" in skills.NAMES
    text = skills.load_skill("investigation")
    assert re.search(r"^name: investigation$", text, re.M)
    body = text.split("---", 2)[2].lstrip()
    assert body.startswith("LAWS (read first)\n")
    block = body.split("\n\n", 1)[0].lower()
    for rule in ("every claim cites posts", "every number carries a query_id", "never infer age, gender, income or class",
                 "google search interest can guide searches as context, labelled as google search data", "never use it as post evidence or post counts, or as personal or demographic proof, causal or locality proof, or standalone today", "generated model output is never evidence", "untrusted_content",
                 "say what you do not know", "politics and elections, religion, health, sex life, race or ethnicity, crime"):
        assert rule in block
    assert "Tier: {tier}, with {credits} live credits and {calls} live calls for this question." in text
    answer = text.split("\nAnswer\n", 1)[1].split("\n\n", 1)[0].lower()
    for field in ("claims", "so_what", "watch_next", "gaps", "angle"):
        assert field in answer
    h = harness()
    start(h, draft(h)["plan"])
    for lane in h.research.calls:  # every researcher, the gap one too, runs on the investigation skill
        assert lane["system"].startswith("LAWS (read first)" + chr(10))
        assert "You are one of 42's investigation researchers" in lane["system"]
        assert "Tier: T3, with 600 live credits and 120 live calls for this question." in lane["system"]
