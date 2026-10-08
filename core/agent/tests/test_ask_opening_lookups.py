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
