"""Tracker row 15, Ask power item 5: for a why question the answer leads with the real cause.

One writer rule. For a question that asks why, the first claim names the cause the cited posts and comments show, or
says in plain words that the posts do not show why (spike-explain law 5), and the short answer opens with it. The rule
is a paragraph in the writer's user message, added only for a why question, so the system prompt, the schema, every
check and the hold are the ones every other question already has. Nothing here relaxes a check: a cause claim is a
claim and passes the same K and G checks as any other."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from core.agent import writer
from core.agent.context import RunContext
from core.agent.tests.test_writer import FakeModel, draft_output, record


@pytest.fixture
def ctx():
    c = RunContext(run_id="r_20260928_0500", tier="T1", as_of=datetime(2026, 9, 28, 6, 10, tzinfo=timezone.utc), market="ZA")
    c.evidence = {"tt_1": record("tt_1", "tiktok", "Amapiano Sundays are back in Soweto, everyone at the braai"),
                  "rd_2": record("rd_2", "reddit", "The Soweto amapiano Sunday thing is everywhere this week")}
    c.record_query("SELECT day, posts FROM t", {"market": "ZA"}, [{"day": f"2026-09-{d:02d}", "posts": d} for d in range(1, 26)], "daily posts")
    return c


QUESTIONS = Path(__file__).resolve().parents[2] / "eval" / "questions.yaml"


def eval_questions():
    entries = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    return {e["vars"]["id"]: (e["vars"]["family"], " ".join(e["vars"]["question"].split())) for e in entries}


def ask_with(question, ctx_):
    model = FakeModel(draft_output())
    writer.write_answer(model, question=question, as_of="2026-09-28T06:10:00+02:00", market="ZA",
                        window="21 to 27 September 2026", ctx=ctx_, gaps=[])
    return model.calls[0]


WHY_QUESTIONS = [
    "Why did #fixture fail in South Africa?",
    "Why is amapiano back in Soweto?",
    "What is behind #fixture in South Africa?",
    "What's behind the jump in braai posts?",
    "What made amapiano jump in South Africa on 24 September?",
    "What's driving the mood on AFCON posts in Kenya?",
    "What caused the Sunday braai wave?",
    "How come everyone is posting about beetroot?",
    "What’s behind the braai posts?",
    "Load-shedding jokes faded. What replaced them, and why that, why now?",
]
NOT_WHY_QUESTIONS = [
    "What is happening with amapiano Sundays?",
    "What are Kenyan creators riffing on this fortnight?",
    "Who are the mid-sized Nigerian creators driving food content?",
    "Compare football banter in South Africa, Nigeria and Kenya.",
    "Which of these do you expect to still be growing in two weeks, and why?",
    "Predict the AFCON anthem trend for East African TikTok.",
    "How did Heritage Day posts compare with 2024?",
    "",
]


@pytest.mark.parametrize("question", WHY_QUESTIONS)
def test_a_why_question_is_told_to_lead_with_the_cause(question, ctx):
    user = ask_with(question, ctx)["user"]
    assert writer.CAUSE_FIRST_RULE in user
    assert user.index(writer.CAUSE_FIRST_RULE) < user.index("Evidence pack:")  # before the pack, with the question's other parts


@pytest.mark.parametrize("question", NOT_WHY_QUESTIONS)
def test_a_question_that_does_not_ask_why_gets_no_cause_rule(question, ctx):
    call = ask_with(question, ctx)
    assert writer.CAUSE_FIRST_RULE not in call["user"]


def test_the_why_questions_of_the_evaluation_set_all_get_the_rule_and_no_other_family_does():
    got = {qid: writer.asks_why(text) for qid, (family, text) in eval_questions().items()}
    assert {qid for qid, hit in got.items() if hit} == {"WHY-01", "WHY-02", "WHY-03"}
    assert {qid for qid, (family, _) in eval_questions().items() if family == "why"} == {"WHY-01", "WHY-02", "WHY-03"}


def test_the_system_prompt_and_the_schema_are_the_ones_every_question_has(ctx):
    plain = ask_with("What is happening with amapiano Sundays?", ctx)
    why = ask_with("Why is amapiano back in Soweto?", ctx)
    assert why["system"] == plain["system"] and why["schema"] == plain["schema"]
    assert why["model"] == plain["model"] and why["max_tokens"] == plain["max_tokens"]
    assert writer.CAUSE_FIRST_RULE not in why["system"]


def test_the_rule_asks_for_the_first_claim_to_name_the_cause_or_say_the_posts_do_not_show_why():
    rule = writer.CAUSE_FIRST_RULE
    assert "first claim" in rule and "names the cause" in rule and "cites them" in rule
    assert "the posts do not show why" in rule  # spike-explain law 5, in its own words
    assert "short answer" in rule and "opens with" in rule
    assert "guess" in rule  # the gap is never filled with one


def test_the_rule_keeps_a_cause_the_posts_show_apart_from_one_the_writer_infers():
    rule = writer.CAUSE_FIRST_RULE
    assert "inferred" in rule and "after the cause the posts show" in rule


def test_the_rule_is_one_paragraph_with_no_dash_and_no_brace_for_format_to_trip_on():
    rule = writer.CAUSE_FIRST_RULE
    assert "{" not in rule and "}" not in rule
    assert not any(chr(code) in rule for code in (0x2013, 0x2014)) and chr(45) * 2 not in rule
    assert '"' not in rule  # quotation marks are for quotes[] text alone (the writer's own law)


def test_the_rule_does_not_change_what_the_writer_returns(ctx):
    plain, _ = writer.write_answer(FakeModel(draft_output()), question="What is happening with amapiano Sundays?",
                                   as_of="2026-09-28T06:10:00+02:00", market="ZA", window="21 to 27 September 2026",
                                   ctx=ctx, gaps=[])
    why, _ = writer.write_answer(FakeModel(draft_output()), question="Why is amapiano back in Soweto?",
                                 as_of="2026-09-28T06:10:00+02:00", market="ZA", window="21 to 27 September 2026",
                                 ctx=ctx, gaps=[])
    assert why == plain  # the draft is the model's own output run through the same code


# Through a real run: the writer call of run_ask carries the rule for a why question and not for another.

def writer_user(question):
    from core.agent.tests.test_ask import FakeModel as RunModel, Harness
    from core.agent.writer import WRITER_SCHEMA
    model = RunModel()
    Harness(check=None, model=model).run(question=question)
    return [c["user"] for c in model.calls if c["schema"] is WRITER_SCHEMA]


def test_a_run_of_a_why_question_gives_the_writer_the_rule_once():
    users = writer_user("What is behind amapiano in South Africa this week?")
    assert len(users) == 1 and users[0].count(writer.CAUSE_FIRST_RULE) == 1


def test_a_run_of_a_question_that_does_not_ask_why_gives_the_writer_no_rule():
    users = writer_user("Which amapiano sounds are leading in South Africa this week?")
    assert len(users) == 1 and writer.CAUSE_FIRST_RULE not in users[0]
