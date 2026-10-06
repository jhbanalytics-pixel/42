"""A scheduled event with local creators reacting in their own words passes like news does (Albert, 3 Oct).

The writer is told to name a timely cause such as a release, a match or a holiday, and the critic named "a news or
scheduled event that alone accounts for the posts" as a reason to hold, while its event pass covered news only. Now
a scheduled event (a release, match, holiday or scheduled cultural moment) the posts do not rule out passes exactly
as a news event does: at least two different local creators reacting in their own words among the cited posts, a
local why-now, and every claim one confidence step lower, labelled news/event-driven on the card. A scheduled event
with no local reaction, or with reaction from fewer than two local creators, is still held.
"""

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


def test_a_scheduled_event_with_local_reaction_publishes_one_step_lower():
    result = run(FakeModel([good()], critic=EVENT_REACTION))
    assert result["numbers_only"] is False and result["reason"] is None
    assert result["news_driven"] is True
    row = critic_row(result)
    assert row["verdict"] == "pass" and "event-driven with local reaction" in row["detail"]
    assert {c["id"]: c["label"] for c in result["claims"]} == {"c1": "single_source", "c2": "inferred",
                                                              "c3": "inferred"}
    assert result["critic"]["scheduled_event"] is True


def test_a_scheduled_event_and_news_event_pass_alike():
    news = run(FakeModel([good()], critic=dict(EVENT_REACTION, news_driven=True, scheduled_event=False)))
    event = run(FakeModel([good()], critic=EVENT_REACTION))
    assert news["claims"] == event["claims"] and news["news_driven"] is event["news_driven"] is True


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


def test_an_event_pass_is_worded_as_event_driven_with_local_reaction():
    assert job.check_reason(event_row("pass")) == f"{job.EVENT_PASS}: a news or scheduled event"
    assert job.EVENT_PASS == "Critic: event-driven, local creators react in their own words"


def test_an_event_reading_without_a_local_why_now_is_held_for_the_why_now_only():
    assert job.check_reason(event_row("cut", why_now=False)) == "Critic: local why-now not shown"


def test_the_row_explain_writes_for_an_event_is_the_row_job_reads():
    row = explain._critic_row(EVENT_REACTION, reacting_creators=2)
    assert row["verdict"] == "pass"
    assert job.check_reason(row) == f"{job.EVENT_PASS}: a news or scheduled event"
    held = explain._critic_row(EVENT_REACTION, reacting_creators=1)
    assert held["verdict"] == "cut"
    assert job.check_reason(held) == "Critic: a simpler explanation was not ruled out: a news or scheduled event"
