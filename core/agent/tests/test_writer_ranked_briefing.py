"""A question about which stories lead is answered as a ranked briefing; the rule is the writer's, the checks are not.

One paragraph in the writer's user message, added only for a ranked story question, like the cause-first rule: the
system prompt, the schema, every check and the hold are the ones every question has. The paragraph asks for a one line
lead that names the stories, then a group of claims per story that starts with the story's name. Whether a part of a
story is there is the pack's to decide: the rule says to leave a part out rather than fill it."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from core.agent import writer
from core.agent.context import RunContext
from core.agent.tests.test_ask_skills import L4_SPIKE, Watched
from core.agent.tests.test_writer import FakeModel, draft_output, record

QUESTIONS = Path(__file__).resolve().parents[2] / "eval" / "questions.yaml"


@pytest.fixture
def ctx():
    c = RunContext(run_id="r_20260928_0500", tier="T1", as_of=datetime(2026, 9, 28, 6, 10, tzinfo=timezone.utc), market="ZA")
    c.evidence = {"tt_1": record("tt_1", "tiktok", "Amapiano Sundays are back in Soweto, everyone at the braai"),
                  "rd_2": record("rd_2", "reddit", "The Soweto amapiano Sunday thing is everywhere this week")}
    c.record_query("SELECT day, posts FROM t", {"market": "ZA"}, [{"day": f"2026-09-{d:02d}", "posts": d} for d in range(1, 26)], "daily posts")
    return c


def eval_questions():
    entries = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    return {e["vars"]["id"]: " ".join(e["vars"]["question"].split()) for e in entries}


def call_for(question, ctx_, **extra):
    model = FakeModel(draft_output())
    writer.write_answer(model, question=question, as_of="2026-09-28T06:10:00+02:00", market="ZA",
                        window="21 to 27 September 2026", ctx=ctx_, gaps=[], **extra)
    return model.calls[0]


RANKED = [
    "What are people in South Africa posting about most this week?",
    "What are the top conversations in Nigeria this week?",
    "What's trending in Kenya right now?",
    "What is the biggest story in South Africa today?",
    "What are Kenyans talking about the most this week?",
    "Which stories are leading Nigerian social right now?",
    "Give me the three conversations with the most momentum in South Africa this week.",
    "What’s South African social talking about most?",
    "What are the main conversations in Kenya this week?",
    "What stories dominate Kenyan social this week?",
    "What are the biggest moments on South African social this week?",
    "What are the top stories this week and what should brands do?",
]
NOT_RANKED = [
    "What is happening with amapiano Sundays?",
    "Which sounds are trending in South Africa this week?",
    "What are the most used hashtags in Nigeria?",
    "Who are the top creators in Kenyan food content?",
    "Which brands are most talked about in South African telecoms?",
    "Why is amapiano the top story in Soweto?",
    "What caused the biggest jump in braai posts?",
    "Which of these do you expect to be trending in two weeks?",
    "Which Soweto brand is most likely to win the festive season?",
    "Compare football banter in South Africa, Nigeria and Kenya.",
    "Take the biggest Nigerian song or sound on TikTok in the last month.",
    "Is amapiano trending in South Africa?",
    "Is #gqomchallenge trending in Soweto this week?",
    "What are people posting most about Checkers?",
    "What is the top story about Bafana Bafana?",
    "How should Castle Lager play the trending Springbok conversation?",
    "Which TikTok trend is Nando's using most?",
    "Is the loadshedding conversation trending up or down?",
    "What are people saying about the top 3 banks?",
    "Show me the most viral post about the Durban July.",
    "How is the Springbok tour playing on social?",
    "A brief for a data bundle aimed at students in Nigeria.",
    "",
]


@pytest.mark.parametrize("question", RANKED)
def test_a_ranked_story_question_is_told_to_write_a_ranked_briefing(question, ctx):
    assert writer.asks_ranked(question)
    user = call_for(question, ctx)["user"]
    assert user.count(writer.RANKED_BRIEFING_RULE) == 1
    assert user.index(writer.RANKED_BRIEFING_RULE) < user.index("Evidence pack:")


@pytest.mark.parametrize("question", NOT_RANKED)
def test_any_other_question_gets_no_ranked_briefing_rule(question, ctx):
    assert not writer.asks_ranked(question)
    assert writer.RANKED_BRIEFING_RULE not in call_for(question, ctx)["user"]


def test_of_the_benchmark_questions_only_the_most_momentum_one_asks_for_a_ranked_briefing():
    got = {qid for qid, text in eval_questions().items() if writer.asks_ranked(text)}
    assert got == {"NOW-01"}


RANKED_SKILLS = ["culture-read", None]  # the default skill, and a call that names none
OTHER_SKILLS = ["brand-lens", "context-pack", "creator-fit", "investigation", "spike-explain"]


@pytest.mark.parametrize("skill", RANKED_SKILLS)
def test_the_skills_that_answer_ranked_story_questions_get_the_rule(skill, ctx):
    assert writer.RANKED_BRIEFING_RULE in call_for(RANKED[0], ctx, skill=skill)["user"]


@pytest.mark.parametrize("skill", OTHER_SKILLS)
def test_every_other_skill_keeps_its_own_answer_shape_and_gets_no_ranked_rule(skill, ctx):
    assert writer.RANKED_BRIEFING_RULE not in call_for(RANKED[0], ctx, skill=skill)["user"]


def test_the_skills_that_get_the_rule_are_listed_by_name_and_are_real_skills():
    from core.agent import skills
    assert set(writer.RANKED_RULE_SKILLS) == {None, skills.DEFAULT}
    assert set(OTHER_SKILLS) | {skills.DEFAULT} == {p.name for p in (Path(skills.__file__).resolve().parents[1] / "skills").iterdir() if p.is_dir()}


def test_a_numeric_repair_call_carries_the_same_skill_rule(ctx):
    for skill, wanted in (("context-pack", False), ("culture-read", True)):
        model = FakeModel(draft_output())
        writer.repair_answer_numbers(model, draft=draft_output(), issues=[], question=RANKED[0],
                                     as_of="2026-09-28T06:10:00+02:00", market="ZA", window="21 to 27 September 2026",
                                     ctx=ctx, skill=skill)
        assert (writer.RANKED_BRIEFING_RULE in model.calls[0]["user"]) is wanted


def test_a_spike_ask_gets_neither_the_ranked_nor_the_cause_first_rule(ctx):
    user = call_for("What made the top story jump in South Africa on 24 September?", ctx, skill="spike-explain")["user"]
    assert writer.RANKED_BRIEFING_RULE not in user and writer.CAUSE_FIRST_RULE not in user


def test_a_why_question_that_names_a_ranking_gets_the_cause_rule_and_not_the_briefing(ctx):
    user = call_for("Why are these the top conversations in Nigeria this week?", ctx)["user"]
    assert writer.CAUSE_FIRST_RULE in user and writer.RANKED_BRIEFING_RULE not in user


def test_the_system_prompt_and_the_schema_are_the_ones_every_question_has(ctx):
    plain = call_for("What is happening with amapiano Sundays?", ctx)
    ranked = call_for(RANKED[0], ctx)
    assert ranked["system"] == plain["system"] and ranked["schema"] == plain["schema"]
    assert ranked["model"] == plain["model"] and ranked["max_tokens"] == plain["max_tokens"]
    assert writer.RANKED_BRIEFING_RULE not in ranked["system"]


def test_the_rule_asks_for_a_one_line_lead_that_names_the_stories_and_no_figure():
    rule = writer.RANKED_BRIEFING_RULE
    assert "short answer" in rule and "one line" in rule and "names the stories" in rule
    assert "no other figure" in rule


def test_the_rule_asks_for_the_story_name_then_a_colon_on_every_claim_of_a_story():
    rule = writer.RANKED_BRIEFING_RULE
    assert "story's name" in rule and "colon" in rule and "same name" in rule


def test_the_rule_takes_a_stories_size_from_one_query_row_and_ranks_by_the_whole_store_rows():
    rule = writer.RANKED_BRIEFING_RULE
    assert "same query row" in rule and "posts" in rule and "creators" in rule
    assert "most creators first" in rule


def test_the_rule_leaves_a_part_out_when_the_pack_does_not_support_it():
    rule = writer.RANKED_BRIEFING_RULE
    assert "per day" in rule and "peak" in rule and "still rising" in rule
    assert "leave" in rule and "out" in rule
    assert "cited posts" in rule and "reason" in rule
    assert "never fill" in rule


def test_the_rule_asks_for_one_so_what_line_per_story_worded_as_interpretation():
    rule = writer.RANKED_BRIEFING_RULE
    assert "so_what" in rule and "claim ids" in rule and "interpretation" in rule


def test_the_rule_keeps_every_other_rule():
    assert "Every other rule still holds" in writer.RANKED_BRIEFING_RULE


def test_the_rule_is_one_paragraph_with_no_dash_and_no_brace_for_format_to_trip_on():
    rule = writer.RANKED_BRIEFING_RULE
    assert "{" not in rule and "}" not in rule and "\n" not in rule
    assert not any(chr(code) in rule for code in (0x2013, 0x2014)) and chr(45) * 2 not in rule
    assert '"' not in rule  # quotation marks are for quotes[] text alone (the writer's own law)


def test_the_rule_does_not_change_what_the_writer_returns(ctx):
    plain, _ = writer.write_answer(FakeModel(draft_output()), question="What is happening with amapiano Sundays?",
                                   as_of="2026-09-28T06:10:00+02:00", market="ZA", window="21 to 27 September 2026",
                                   ctx=ctx, gaps=[])
    ranked, _ = writer.write_answer(FakeModel(draft_output()), question=RANKED[0], as_of="2026-09-28T06:10:00+02:00",
                                    market="ZA", window="21 to 27 September 2026", ctx=ctx, gaps=[])
    assert ranked == plain  # the draft is the model's own output run through the same code


# Through a real run: the writer call of run_ask carries the rule for a ranked story question and not for another.

def writer_users(h):
    return [c["user"] for c in h.model.calls if c["schema"] is writer.WRITER_SCHEMA]


def test_a_run_of_a_ranked_question_gives_the_writer_the_rule_once():
    h = Watched()
    h.run(question=RANKED[0])
    users = writer_users(h)
    assert len(users) == 1 and users[0].count(writer.RANKED_BRIEFING_RULE) == 1


def test_a_run_of_a_question_that_does_not_ask_for_a_ranking_gives_the_writer_no_rule():
    h = Watched()
    h.run(question="What is happening with amapiano Sundays in South Africa this week?")
    assert writer.RANKED_BRIEFING_RULE not in writer_users(h)[0]


def test_a_run_of_a_spike_ask_gives_the_writer_no_ranked_rule():
    h = Watched()
    h.run(**{**L4_SPIKE, "question": "What made the top story jump in South Africa on 24 September?"})
    assert writer.RANKED_BRIEFING_RULE not in writer_users(h)[0]
