"""A spike ask keeps the answer shape its skill states; the cause-first rule is for a question that asks why.

The server words every spike ask "What made {label} jump in {market} on {day}?", which asks_why reads as a why
question. spike-explain law 2 and the skill's first claim say the jump against its baseline comes first and, when the
jump is not confirmed, no cause follows. So the rule is not added to a spike ask, whatever its wording, and is added
to the same words typed as a plain question."""
from datetime import datetime, timezone

import pytest

from core.agent import writer
from core.agent.context import RunContext
from core.agent.tests.test_ask_skills import L4_SPIKE, Watched
from core.agent.tests.test_writer import FakeModel, draft_output, record

SPIKE_PHRASING = "What made amapiano jump in South Africa on 24 September?"


@pytest.fixture
def ctx():
    c = RunContext(run_id="r_20260928_0500", tier="T1", as_of=datetime(2026, 9, 28, 6, 10, tzinfo=timezone.utc), market="ZA")
    c.evidence = {"tt_1": record("tt_1", "tiktok", "Amapiano Sundays are back in Soweto, everyone at the braai")}
    c.record_query("SELECT day, posts FROM t", {"market": "ZA"}, [{"day": "2026-09-24", "posts": 9}], "daily posts")
    return c


def user_for(question, ctx_, **extra):
    model = FakeModel(draft_output())
    writer.write_answer(model, question=question, as_of="2026-09-28T06:10:00+02:00", market="ZA",
                        window="21 to 27 September 2026", ctx=ctx_, gaps=[], **extra)
    return model.calls[0]["user"]


def test_the_spike_phrasing_asks_why_so_the_exemption_is_the_skill_and_not_the_wording():
    assert writer.asks_why(SPIKE_PHRASING)


def test_a_spike_ask_gets_no_cause_first_rule_on_the_servers_own_phrasing(ctx):
    assert writer.CAUSE_FIRST_RULE not in user_for(SPIKE_PHRASING, ctx, skill="spike-explain")


def test_the_same_words_typed_as_a_plain_question_still_get_the_rule(ctx):
    assert writer.CAUSE_FIRST_RULE in user_for(SPIKE_PHRASING, ctx)
    assert writer.CAUSE_FIRST_RULE in user_for(SPIKE_PHRASING, ctx, skill="culture-read")


def test_a_spike_ask_changes_nothing_else_in_the_writers_message(ctx):
    spike = user_for(SPIKE_PHRASING, ctx, skill="spike-explain")
    plain = user_for("What is happening with amapiano Sundays?", ctx)
    assert spike.replace(SPIKE_PHRASING, "") == plain.replace("What is happening with amapiano Sundays?", "")


def test_a_run_of_a_spike_ask_gives_the_writer_no_cause_first_rule():
    h = Watched()
    h.run(**{**L4_SPIKE, "question": SPIKE_PHRASING})
    users = [c["user"] for c in h.model.calls if c["schema"] is writer.WRITER_SCHEMA]
    assert len(users) == 1 and writer.CAUSE_FIRST_RULE not in users[0]


def test_a_run_of_the_same_words_with_no_spike_gives_the_writer_the_rule_once():
    h = Watched()
    h.run(question=SPIKE_PHRASING)
    users = [c["user"] for c in h.model.calls if c["schema"] is writer.WRITER_SCHEMA]
    assert len(users) == 1 and users[0].count(writer.CAUSE_FIRST_RULE) == 1
