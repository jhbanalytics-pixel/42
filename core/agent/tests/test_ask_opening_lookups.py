"""Ask speed, fix 4 (ASK-LATENCY.md): the deterministic opening lookups run in code before the first research turn.

48 of 59 staging Asks opened with a lookup turn of 8 s at p50 (budget_status, rising_topics, resolve_dates and the
rest). The three whose arguments code can know are fetched by code through the same schema check, guard and tool
functions the model's calls go through, and the first research turn receives their results in its prompt.
recall_findings stays the model's: its query is model-written and it feeds the trust path.
"""

import json
import re

import pytest

from core.agent import ask, gemini_research
from core.agent.context import RunContext
from core.agent.tests.test_ask import NOW, FakeWarehouse, Harness
from core.agent.tests.test_ask_timings import research_turns, through_gemini
from core.agent.tests.test_gemini_research_budget import FakeClient, ftext, reply
from core.agent.tests.test_ask_model_budget import configure_gemini
from core.agent.toolset import build_functions

RISING = "Which topics are rising in South Africa right now?"
TRENDING = "What is trending in South Africa right now?"  # the trending board route has its own handling
PLAIN = "What is behind amapiano in South Africa this week?"


class RisingWarehouse(FakeWarehouse):
    """rising_topics' read returns three items; everything else is the Harness warehouse."""

    def __init__(self, fail_rising=False):
        super().__init__()
        self.fail_rising = fail_rising
        self.rising_reads = 0

    def run(self, sql, params, max_bytes_billed):
        if "v_items_today" in sql:
            self.rising_reads += 1
            if self.fail_rising:
                raise RuntimeError("v_items_today is unavailable")
            return [{"metric_date": "2026-09-28", "market": "ZA", "item_id": f"item_{i}", "label": f"Topic {i}",
                     "kind": "topic", "state": "rising", "worth_pct": 90 - i, "creators3": 40 - i, "posts3": 100 - i}
                    for i in range(3)]
        return super().run(sql, params, max_bytes_billed)


def harness(question, *, warehouse=None, prompts=None, **kwargs):
    seen = prompts if prompts is not None else []

    def research(ctx, prompt, options, emit, should_stop):
        seen.append(prompt)
        return {"note": "Reading: done.", "tokens": {"input": 10, "output": 5}, "usd": 0.001}

    h = Harness(research=research, **kwargs)
    if warehouse is not None:
        h.warehouse = warehouse
        h.deps.warehouse = warehouse
    return h, seen


def fetched(prompt):
    """The lookups' results, from the fenced block after the note; "" when the prompt carries none."""
    if "Already fetched" not in prompt:
        return ""
    return prompt.split("Already fetched", 1)[1].split("<untrusted_content>", 1)[1].split("</untrusted_content>")[0]


def what_the_model_would_fetch(warehouse, question, name, args, tier="T1"):
    """The tool's own answer on a fresh context for the same ask: schema check, guard and run_plain, as a model call gets."""
    window = ask._window(question, NOW)
    ctx = RunContext(run_id="r_expected", tier=tier, as_of=NOW, market="ZA", window_start=window[0],
                     window_end=window[1])
    text, is_error = gemini_research._run_call(ctx, build_functions(ctx, warehouse, None, None), name, args)
    assert not is_error, text
    return text


def test_the_first_research_turn_receives_what_the_model_would_have_fetched():
    wh = RisingWarehouse()
    h, prompts = harness(RISING, warehouse=wh)

    h.run(question=RISING)

    prompt = prompts[0]
    # RISING names no window, so the ask reads the default: the last 30 days
    for name, args in (("budget_status", {}), ("resolve_dates", {"expression": "last 30 days"})):
        assert what_the_model_would_fetch(RisingWarehouse(), RISING, name, args) in prompt, name
    # rising_topics records a query, so its id depends on how many the run had made before it (the trending board
    # fallback made one); the rows, the truncation flag and the result hash are what the model would have fetched
    expected = json.loads(what_the_model_would_fetch(RisingWarehouse(), RISING, "rising_topics", {}))
    line = next(l for l in prompt.splitlines() if l.startswith("rising_topics {}: "))
    given = json.loads(line.split(": ", 1)[1])
    assert re.fullmatch(r"q_\d+", given.pop("query_id")) and re.fullmatch(r"q_\d+", expected.pop("query_id"))
    assert given == expected and given["rows"] and given["result_hash"].startswith("sha256:")


def test_a_plain_question_gets_the_budget_and_the_dates_but_no_rising_topics_read():
    wh = RisingWarehouse()
    h, prompts = harness(PLAIN, warehouse=wh)

    h.run(question=PLAIN)

    prompt = prompts[0]
    assert what_the_model_would_fetch(wh, PLAIN, "budget_status", {}) in prompt
    assert what_the_model_would_fetch(wh, PLAIN, "resolve_dates", {"expression": "last 7 days"}) in prompt
    assert wh.rising_reads == 0  # the model asks for what is moving only when the question is about it
    assert "rising_topics" not in fetched(prompt)


def test_a_trending_board_question_keeps_its_own_route_and_gets_no_rising_topics_read():
    wh = RisingWarehouse()
    h, prompts = harness(TRENDING, warehouse=wh)

    h.run(question=TRENDING)

    assert wh.rising_reads == 0 and "rising_topics" not in fetched(prompts[0])
    assert "budget_status" in fetched(prompts[0])


def test_the_block_says_it_is_data_and_names_the_tools_that_made_it():
    h, prompts = harness(RISING, warehouse=RisingWarehouse())

    h.run(question=RISING)

    prompt = prompts[0]
    assert "Already fetched" in prompt and "do not call" in prompt.lower()
    assert "<untrusted_content>" in prompt.split("Already fetched", 1)[1]  # fenced as data, like a brief's cards
    block = fetched(prompt)
    assert block.index("budget_status") < block.index("resolve_dates") < block.index("rising_topics")


def test_the_lookups_show_the_same_steps_the_model_driven_calls_do():
    h, _ = harness(RISING, warehouse=RisingWarehouse())

    h.run(question=RISING)

    steps = [e["text"] for e in h.events if e.get("event") == "step"]
    assert "Checking the live search budget" in steps
    assert any(t.startswith("Working out the dates") for t in steps)
    assert "Reading rising topics, South Africa" in steps


def test_a_lookup_that_fails_is_left_out_and_the_ask_goes_on():
    wh = RisingWarehouse(fail_rising=True)
    h, prompts = harness(RISING, warehouse=wh)

    out = h.run(question=RISING)

    assert wh.rising_reads >= 1
    assert "rising_topics" not in fetched(prompts[0]) and "budget_status" in fetched(prompts[0])
    assert out["run"]["tokens"]["input"] >= 10  # research ran and the answer path went on after it


def test_no_lookup_is_run_for_t0_which_makes_no_live_call():
    h, prompts = harness(PLAIN, warehouse=RisingWarehouse(), now=NOW)

    h.run(question=PLAIN, tier="T0")

    assert "budget_status" not in fetched(prompts[0])


def test_a_question_that_inherits_its_window_from_its_parent_gets_no_dates_lookup():
    parent = {"question": PLAIN, "answer": {"short_answer": "x", "claims": []},
              "window": {"from": "2026-09-01", "to": "2026-09-10"}}
    h, prompts = harness("Which creators are driving it?", warehouse=RisingWarehouse())

    h.run(question="Which creators are driving it?", parent=parent)

    assert "resolve_dates" not in fetched(prompts[0])


def test_the_lookups_add_no_model_call(monkeypatch):
    configure_gemini(monkeypatch)
    baseline, _ = harness(RISING, warehouse=RisingWarehouse())
    baseline.run(question=RISING)
    one_turn = FakeClient(reply(ftext("Reading: done.")))
    monkeypatch.setattr(gemini_research, "client_factory", lambda: one_turn)
    h = Harness(research=through_gemini(monkeypatch, one_turn))
    h.warehouse = RisingWarehouse()
    h.deps.warehouse = h.warehouse

    h.run(question=RISING)

    assert len(one_turn.calls) == 1  # the opening lookups are code: a second research call would pop an empty script
    assert [c["schema"] for c in h.model.calls] == [c["schema"] for c in baseline.model.calls]
    first_prompt = one_turn.calls[0]["contents"][0].parts[0].text
    assert "Already fetched" in first_prompt


BOTH = "What is trending and rising in South Africa right now?"


def test_a_question_that_is_both_rising_and_trending_takes_the_trending_route_and_gets_no_rising_topics_read():
    wh = RisingWarehouse()
    h, prompts = harness(BOTH, warehouse=wh)
    assert ask._MOVING.search(BOTH) and ask._current_trending_intent(BOTH)  # the case really is both

    h.run(question=BOTH)

    assert wh.rising_reads == 0 and "rising_topics" not in fetched(prompts[0])


def test_a_trending_board_fallback_window_gets_no_dates_lookup_because_the_window_is_not_the_questions(monkeypatch):
    from core.agent.tests.test_ask import TrendingFallbackWarehouse, trending_snapshot

    monkeypatch.setattr(ask, "get_trending_fallback_snapshot",
                        lambda ctx, warehouse, market: trending_snapshot(market, as_of=ctx.as_of), raising=False)
    h, prompts = harness(TRENDING, warehouse=TrendingFallbackWarehouse("ZA"))

    out = h.run(question=TRENDING, market=None)

    assert "Today's board is empty, so this uses the last 7 days" in out["run"]["notices"]  # the fallback ran
    assert "budget_status" in fetched(prompts[0])
    assert "resolve_dates" not in fetched(prompts[0])  # a date lookup for the question's words would name another window


def test_a_trending_question_with_a_board_still_gets_its_dates_lookup(monkeypatch):
    from core.agent.tests.test_ask import TrendingFallbackWarehouse, trending_snapshot

    monkeypatch.setattr(ask, "get_trending_fallback_snapshot",
                        lambda ctx, warehouse, market: trending_snapshot(market, as_of=ctx.as_of, available=True),
                        raising=False)
    h, prompts = harness(TRENDING, warehouse=TrendingFallbackWarehouse("ZA"))

    h.run(question=TRENDING, market=None)

    assert "resolve_dates" in fetched(prompts[0])


def test_t2_researchers_get_no_opening_lookups_and_run_no_lookup_tools():
    from core.agent.tests.test_ask_t2 import CriticModel, Lanes

    lanes = Lanes()
    h = Harness(research=lanes, model=CriticModel())
    h.warehouse = RisingWarehouse()
    h.deps.warehouse = h.warehouse

    h.run(question=RISING, tier="T2")

    assert lanes.calls and all("Already fetched" not in call["prompt"] for call in lanes.calls)
    assert h.warehouse.rising_reads == 0
    steps = [e["text"] for e in h.events if e.get("event") == "step"]
    assert "Checking the live search budget" not in steps and "Reading rising topics, South Africa" not in steps


def test_t3_research_gets_no_opening_lookups_and_runs_no_lookup_tools():
    from core.agent.tests.test_ask import K4IntegrationModel

    plan = {"sub_questions": [{"id": "q1", "text": "What do TikTok posts say about amapiano this week?",
                               "platforms": ["tiktok"], "credits": 20}],
            "gap_round": False, "max_credits": 100, "max_model_usd": 10.0}
    seen = []

    def research(ctx, prompt, options, emit, should_stop):
        seen.append(prompt)
        return {"note": "Reading: done.", "tokens": {"input": 10, "output": 5}, "usd": 0.001}

    h = Harness(research=research, check=None, model=K4IntegrationModel())
    h.warehouse = RisingWarehouse()
    h.deps.warehouse = h.warehouse

    h.run(question=RISING, tier="T3", plan=plan, max_credits=100, max_model_usd=10.0,
          investigation_id="i_20260928_test")

    assert seen and all("Already fetched" not in prompt for prompt in seen)
    assert h.warehouse.rising_reads == 0


def test_the_note_names_the_default_window_rising_topics_ran_with_and_does_not_claim_the_questions_window():
    import inspect

    from core.agent.tools.warehouse import rising_topics

    default_days = inspect.signature(rising_topics).parameters["window_days"].default
    window = ask._window(RISING, NOW)
    asked_days = (window[1] - window[0]).days + 1
    assert asked_days != default_days  # the case the note exists for: the question's window is not the default

    note = ask.OPENING_NOTE

    assert f"{default_days} days" in note and "window_days" in note
    assert "the same tools and arguments you would have used" not in note
    h, prompts = harness(RISING, warehouse=RisingWarehouse())
    h.run(question=RISING)
    assert note in prompts[0]
    assert f"rising_topics {{}}" in prompts[0]  # the arguments each lookup ran with are printed on its line


def test_a_failed_opening_lookup_shows_a_failure_step_and_the_ask_goes_on():
    h, prompts = harness(RISING, warehouse=RisingWarehouse(fail_rising=True))

    out = h.run(question=RISING)

    steps = [e["text"] for e in h.events if e.get("event") == "step"]
    assert "Reading rising topics did not run" in steps  # the same line the model-driven path writes
    assert steps.index("Reading rising topics, South Africa") < steps.index("Reading rising topics did not run")
    assert out["run"]["tokens"]["input"] >= 10  # research ran after the failed lookup
    assert not any("v_items_today" in t for t in steps)  # the error text stays in the log, not in the step
