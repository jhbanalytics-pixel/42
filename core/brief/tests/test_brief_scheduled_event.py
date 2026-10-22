"""Scheduled event reaction alone is held; original critic fields remain auditable."""

import pytest

from core.brief import explain, job
from core.brief.tests.test_brief_explain import (
    FakeModel, assert_numbers_only, critic_row, good, make_pack, run,
)

EVENT_REACTION = {"non_cultural_explanation": "a scheduled event: the Heritage Day long weekend",
                  "ruled_out": False, "news_driven": False, "scheduled_event": True, "local_reaction": True,
                  "local_why_now": True,
                  "reason": "the posts follow the holiday but local creators tell their own braai stories"}


def test_critic_schema_asks_whether_the_simplest_explanation_is_a_scheduled_event():
    props = explain.CRITIC_SCHEMA["properties"]
    assert props["scheduled_event"] == {"type": "boolean"}
    assert "scheduled_event" in explain.CRITIC_SCHEMA["required"]
    assert "scheduled_event" in explain.CRITIC_FIELDS


def test_critic_system_lets_a_scheduled_event_pass_on_local_reaction_like_news():
    system = " ".join(explain.CRITIC_SYSTEM.split())
    assert "Set scheduled_event true only when the simplest explanation you named is a scheduled event" in system
    assert "a release, a match, a holiday or a scheduled cultural moment" in system
    assert "their own reaction to that news or event in their own words" in system
    # The menu still names the event, so an event with no local reaction is still held.
    assert "a news event or scheduled event that alone accounts for the posts" in system


def test_a_scheduled_event_with_only_local_reaction_is_held():
    result = run(FakeModel([good()], critic=EVENT_REACTION))
    assert_numbers_only(result, "failed_checks")
    assert result["news_driven"] is False
    assert critic_row(result)["verdict"] == "cut"
    assert result["critic"]["scheduled_event"] is True


def test_scheduled_and_news_event_reactions_are_both_held():
    for critic in (EVENT_REACTION, dict(EVENT_REACTION, news_driven=True, scheduled_event=False)):
        result = run(FakeModel([good()], critic=critic))
        assert_numbers_only(result, "failed_checks")


@pytest.mark.parametrize("critic", [
    dict(EVENT_REACTION, local_reaction=False), dict(EVENT_REACTION, local_reaction="true"),
    dict(EVENT_REACTION, scheduled_event=False), dict(EVENT_REACTION, scheduled_event="true"),
    dict(EVENT_REACTION, scheduled_event=None), dict(EVENT_REACTION, local_why_now=False),
    {k: v for k, v in EVENT_REACTION.items() if k != "scheduled_event"},
    {k: v for k, v in EVENT_REACTION.items() if k != "local_reaction"},
], ids=["no_reaction", "reaction_string", "not_event", "event_string", "null", "no_why_now", "missing_event",
        "missing_reaction"])
def test_a_scheduled_event_without_a_clear_local_reaction_is_still_held(critic):
    result = run(FakeModel([good()], critic=critic))
    assert_numbers_only(result, "failed_checks")
    assert result["news_driven"] is False
    assert critic_row(result)["verdict"] == "cut"


def test_a_scheduled_event_needs_two_local_creators_among_the_cited_posts():
    pack = make_pack()
    for e in pack["evidence"]:
        e["handle"] = "@thandi_moves"
    result = run(FakeModel([good()], critic=EVENT_REACTION), pack=pack)
    assert_numbers_only(result, "failed_checks")
    assert critic_row(result)["verdict"] == "cut"


# The fixed wording of the critic row (core/brief/job.py)


def event_row(verdict, why_now=True):
    detail = ("critic: simplest non-cultural explanation: a scheduled event: the Heritage Day long weekend; "
              "event-driven with local reaction: local creators tell their own braai stories; "
              f"local why-now {'checked' if why_now else 'not checked'}")
    return {"claim_id": None, "rule": "critic", "verdict": verdict, "checker": "model", "detail": detail}


# 22 Oct 2026: a news or scheduled event no longer passes on local reaction. critic_is_valid rejects ruled_out with
# any event or reaction flag, so explain._critic_row never writes a reaction standing and job.py no longer words one;
# the hand-built rows of that era now read as the not-ruled-out holds they are.
def test_no_critic_answer_gives_a_reaction_standing_or_a_pass_on_reaction():
    import itertools
    for flags in itertools.product([True, False], repeat=5):
        out = dict(zip(("ruled_out", "news_driven", "scheduled_event", "local_reaction", "local_why_now"), flags),
                   non_cultural_explanation="a scheduled event", reason="r")
        row = explain._critic_row(out, 20)
        assert row["standing"] in ("ruled out", "not ruled out", explain.INCONSISTENT)
        if out["news_driven"] or out["scheduled_event"] or out["local_reaction"]:
            assert row["verdict"] == "cut"
    assert explain.STANDINGS == ("ruled out", "not ruled out", explain.INCONSISTENT)
    for name in ("NEWS_PASS", "EVENT_PASS", "REACTION_STANDINGS"):
        assert not hasattr(job, name)


def test_a_hand_built_event_reaction_row_reads_as_a_simpler_explanation_not_ruled_out():
    assert job.check_reason(event_row("cut", why_now=False)) == (
        "Critic: a simpler explanation was not ruled out; local why-now not shown")
    assert job.check_reason(event_row("cut")) == "Critic: a simpler explanation was not ruled out"


def test_the_row_explain_writes_for_an_event_is_the_row_job_reads():
    for count in (1, 2, 20):
        row = explain._critic_row(EVENT_REACTION, reacting_creators=count)
        assert row["verdict"] == "cut"
        assert job.check_reason(row) == "Critic: a simpler explanation was not ruled out: a news or scheduled event"
