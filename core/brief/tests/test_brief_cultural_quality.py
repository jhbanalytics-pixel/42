"""Cultural qualification controls, using retained critics and offline models."""
import copy
import json
from pathlib import Path

import pytest

from core.brief import explain
from core.brief.tests.test_brief_explain import FakeModel, good, run, make_pack

PATTERN = dict(non_cultural_explanation="a scheduled holiday alone accounts for the posts",
               ruled_out=True, news_driven=False, scheduled_event=False, local_reaction=False,
               local_why_now=True,
               reason="tt_1 and ig_2 adopt the shaya step dance; ig_2 and tt_3 tie it to braai gatherings.")


@pytest.mark.parametrize("event", [True, False])
def test_ordinary_event_reactions_hold_and_cannot_receive_timing_redraft(event):
    critic = dict(PATTERN, ruled_out=False, scheduled_event=event, news_driven=not event,
                  local_reaction=True, reason="Independent creators give ordinary match opinions.")
    result = run(FakeModel([good()], critic=critic))
    assert result["numbers_only"] is True and result["reason"] == "failed_checks"
    assert result["critic"] == dict(critic, patterns_seen=[])
    assert result["usage_usd"] > 0
    assert explain._wording_only(dict(critic, local_why_now=False), 20) is False


@pytest.mark.parametrize("field", ["ruled_out", "news_driven", "scheduled_event", "local_reaction", "local_why_now"])
@pytest.mark.parametrize("bad", [None, "true", 1, 0, "false"])
def test_malformed_booleans_hold_at_connected_explanation_boundary(field, bad):
    critic = dict(PATTERN, **{field: bad})
    result = run(FakeModel([good()], critic=critic))
    assert result["numbers_only"] is True and result["reason"] == "failed_checks"
    assert explain._wording_only(critic, 2) is False


@pytest.mark.parametrize("field", list(PATTERN))
def test_missing_fields_hold(field):
    from core.brief.quality import critic_is_valid, cultural_critic_passes
    critic = dict(PATTERN)
    del critic[field]
    assert critic_is_valid(critic) is False
    assert cultural_critic_passes(critic) is False
    assert explain._critic_row(critic)["verdict"] == "cut"
    result = run(FakeModel([good()], critic=critic))
    assert result["numbers_only"] is True and result["reason"] == "failed_checks"


@pytest.mark.parametrize("field", ["news_driven", "scheduled_event", "local_reaction"])
def test_ruled_out_with_a_standing_event_or_reaction_is_contradictory(field):
    critic = dict(PATTERN, **{field: True})
    assert explain._critic_row(critic)["verdict"] == "cut"
    assert explain._wording_only(dict(critic, local_why_now=False), 2) is False


@pytest.mark.parametrize("field", ["reason", "non_cultural_explanation"])
@pytest.mark.parametrize("bad", [None, "", "   ", 1])
def test_missing_grounds_hold(field, bad):
    assert explain._critic_row(dict(PATTERN, **{field: bad}))["verdict"] == "cut"


def test_retained_ng_four_critics_hold_without_changing_source_values():
    fixture = json.loads((Path(__file__).parent / "fixtures/ng_four_critics_20261015.json").read_text(encoding="utf-8"))
    before = copy.deepcopy(fixture)
    assert fixture["source_fixture_sha256"] == "3310fd1eeddd61164b11ebaf1caf9c3df60063045d1fab6c28dfd68d4552b7c1"
    assert len(fixture["cards"]) == 4
    for card in fixture["cards"]:
        assert explain._critic_row(card["critic"], 1000)["verdict"] == "cut"
        assert explain._wording_only(dict(card["critic"], local_why_now=False), 1000) is False
    assert fixture == before


def test_supported_dance_adoption_keeps_existing_post_and_claim_evidence():
    pack = make_pack()
    original = copy.deepcopy(pack)
    result = run(FakeModel([good()], critic=PATTERN), pack=pack)
    assert result["numbers_only"] is False
    assert result["critic"] == dict(PATTERN, patterns_seen=[])
    assert result["claims"][0]["quotes"] == [{"evidence_id": "tt_1", "text": "shaya step"}]
    assert pack == original


def test_dance_claim_without_dance_evidence_holds_before_critic():
    pack = make_pack()
    for post in pack["evidence"]:
        post["text"] = "My opinion on the match result."
    model = FakeModel([good(), good()], critic=PATTERN)
    result = run(model, pack=pack)
    assert result["numbers_only"] is True
    assert not model.critic_calls()


@pytest.mark.parametrize("value", [None, [], "true", 1, True])
def test_non_mapping_critics_hold(value):
    from core.brief.quality import critic_is_valid, cultural_critic_passes
    assert critic_is_valid(value) is False
    assert cultural_critic_passes(value) is False
    assert explain._critic_row(value)["verdict"] == "cut"


def test_only_a_valid_pattern_with_false_timing_is_redraft_eligible():
    assert explain._wording_only(dict(PATTERN, local_why_now=False), 0) is True
    assert explain._wording_only(PATTERN, 0) is False


def test_prompt_demands_a_cited_pattern_beyond_event_reaction():
    prompt = " ".join(explain.CRITIC_SYSTEM.split())
    assert "adopted format, practice or emerging behaviour" in prompt
    assert "Ordinary event coverage, independent opinions, jokes, personal stories and event timing alone do not rule it out" in prompt
    assert "Name the pattern and the cited post evidence in reason" in prompt
    # 20 Oct 2026: the evaluate-only line had no scope and could be read as limiting the whole critic.
    assert ("For local_why_now, evaluate only the why-now clause and the local posts cited by its supporting "
            "claims.") in prompt
    assert "Evaluate only the why-now clause" not in prompt
