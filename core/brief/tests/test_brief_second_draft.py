"""The critic-informed second draft (Albert, 4 Oct, D1).

When the critic leaves the simpler explanation ruled out (or passes a news or scheduled event on local reaction) and
holds the card only because the why-now is not shown, the writer gets one more draft with the critic's reason. That
draft goes through every check again in full: the code checks with no further repair round, the support check per
claim and on the sentence, the critic and the specificity rule. Nothing is loosened; a critic that leaves the simpler
explanation standing never gets a second draft, and a second draft that fails is cut as before.
"""

import copy

import pytest

from core.brief import explain, job
from core.brief.tests.test_brief_explain import (CANDIDATE, RULED_OUT, W_END, W_START, FakeModel, good, make_pack,
                                                 outside_fences)

WORDING = dict(RULED_OUT, local_why_now=False, reason="The why-now names no timely cause the cited posts show.")
NOT_RULED_OUT = {"non_cultural_explanation": "a single viral post", "ruled_out": False, "local_why_now": False,
                 "reason": "one creator's clip carries most of the views"}
NEWS_WORDING = {"non_cultural_explanation": "a news event", "ruled_out": False, "news_driven": True,
                "scheduled_event": False, "local_reaction": True, "local_why_now": False, "reason": "no timing shown"}


class Critics(FakeModel):
    """FakeModel whose critic answers come from a list, one per critic call."""

    def __init__(self, drafts, critics, **kw):
        super().__init__(drafts, critic=critics[0], **kw)
        self.critics = [copy.deepcopy(c) for c in critics]

    def complete_json(self, *, system, user, schema, model, max_tokens):
        if "ruled_out" in schema.get("properties", {}):
            self.critic = self.critics.pop(0)
        return super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)


def run(model, pack=None, **kw):
    return explain.explain_trend(CANDIDATE, pack or make_pack(), model=model, spent_today_usd=0, window_start=W_START,
                                 window_end=W_END, market="ZA", second_draft=True, **kw)


def critic_rows(result):
    return [r for r in result["checks"] if r["rule"] == "critic"]


def test_a_wording_only_critic_cut_gets_one_second_draft_that_passes_every_check_again():
    model = Critics([good(), good()], [WORDING, RULED_OUT])
    result = run(model)
    assert result["reason"] is None and result["numbers_only"] is False
    assert len(model.writer_calls()) == 2 and len(model.critic_calls()) == 2
    # Every claim and the sentence are support-checked again on the second draft: 3 claims + 1 sentence, twice.
    assert len(model.support_calls()) == 8
    first, second = critic_rows(result)
    assert first["verdict"] == "cut" and first["detail"].startswith(job.REPAIR)
    assert second["verdict"] == "pass" and not second["detail"].startswith(job.REPAIR)
    # Only the first draft's rows are marked as before the repair, so failed_reason and Today read the final draft.
    split = result["checks"].index(first) + 1
    assert all(r["detail"].startswith(job.REPAIR) for r in result["checks"][:split])
    assert not any(r["detail"].startswith(job.REPAIR) for r in result["checks"][split:])


def test_the_second_draft_prompt_carries_the_critic_reason_and_the_previous_draft_inside_fences():
    model = Critics([good(), good()], [WORDING, RULED_OUT])
    run(model)
    second = model.writer_calls()[1]
    assert second["system"] == explain.WRITER_SYSTEM
    assert second["user"].startswith(model.writer_calls()[0]["user"])
    rest = outside_fences(second["user"])
    assert "The critic held the why-now clause of your previous draft" in rest
    assert WORDING["reason"] not in rest and WORDING["reason"] in second["user"]
    assert good()["explanation"] not in rest and good()["explanation"] in second["user"]


def test_a_second_draft_the_critic_still_holds_is_cut_and_there_is_no_third():
    model = Critics([good(), good()], [WORDING, WORDING])
    result = run(model)
    assert result["reason"] == "failed_checks" and result["numbers_only"] is True
    assert len(model.writer_calls()) == 2 and len(model.critic_calls()) == 2
    assert job.failed_reason({**result, "rests_on": ["c1", "c3"]}) == "Critic: local why-now not shown"


@pytest.mark.parametrize("critic", [NOT_RULED_OUT, dict(NOT_RULED_OUT, local_why_now=True)],
                         ids=["neither", "why_now_only"])
def test_a_simpler_explanation_left_standing_never_gets_a_second_draft(critic):
    model = Critics([good(), good()], [critic, RULED_OUT])
    result = run(model)
    assert result["reason"] == "failed_checks"
    assert len(model.writer_calls()) == 1 and len(model.critic_calls()) == 1


def test_a_news_event_with_local_reaction_held_only_on_the_why_now_gets_a_second_draft():
    model = Critics([good(), good()], [NEWS_WORDING, RULED_OUT])
    result = run(model)
    assert result["reason"] is None
    assert len(model.writer_calls()) == 2


def test_the_second_draft_gets_no_code_repair_round_and_a_code_fault_cuts_it_with_its_reason():
    bad = good()
    bad["claims"][0]["quotes"] = [{"evidence_id": "tt_1", "text": "shaya step forever"}]
    bad["claims"][0]["text"] = 'Creators post the "shaya step forever" dance, 31 creators in three days.'
    model = Critics([good(), bad, good()], [WORDING, RULED_OUT])
    result = run(model)
    assert result["reason"] == "failed_checks"
    assert len(model.writer_calls()) == 2 and len(model.critic_calls()) == 1
    final = [r for r in result["checks"] if not r["detail"].startswith(job.REPAIR)]
    assert any(r["claim_id"] == "c1" and r["rule"] == "K1" and r["verdict"] == "cut" for r in final)
    assert job.failed_reason({**result, "rests_on": ["c1", "c3"]}) != job.NO_REST


def test_a_second_draft_with_a_place_fault_is_cut_with_the_place_row():
    placed = good()
    placed["explanation"] = placed["explanation"].replace("likely because", "in Nigeria, likely because")
    model = Critics([good(), placed], [WORDING, RULED_OUT])
    result = run(model)
    assert result["reason"] == "failed_checks" and len(model.critic_calls()) == 1
    final = [r for r in result["checks"] if not r["detail"].startswith(job.REPAIR)]
    assert any(r["claim_id"] is None and r["rule"] == "K3" and r["verdict"] == "cut" for r in final)


def test_a_second_draft_claim_the_support_check_rejects_is_cut_as_on_the_first():
    second = good()
    second["claims"][2]["text"] = "It likely took off over the Heritage Day weekend braais at home."
    model = Critics([good(), second], [WORDING, RULED_OUT], verdicts={"braais at home": "unsupported"})
    result = run(model)
    assert result["reason"] == "failed_checks"
    assert len(model.critic_calls()) == 1


def test_the_second_draft_is_capped_like_every_call(monkeypatch):
    estimate, writers = explain._estimate_usd, []

    def second_writer_over_the_cap(system, user, max_tokens, model_id):
        if system == explain.WRITER_SYSTEM:
            writers.append(user)
            if len(writers) == 2:
                return 1e9
        return estimate(system, user, max_tokens, model_id)

    monkeypatch.setattr(explain, "_estimate_usd", second_writer_over_the_cap)
    model = Critics([good(), good()], [WORDING, RULED_OUT])
    result = run(model)
    assert result["reason"] == "model_cap"
    assert len(model.writer_calls()) == 1


def test_without_second_draft_the_first_cut_stands():
    model = Critics([good(), good()], [WORDING, RULED_OUT])
    result = explain.explain_trend(CANDIDATE, make_pack(), model=model, spent_today_usd=0, window_start=W_START,
                                   window_end=W_END, market="ZA")
    assert result["reason"] == "failed_checks" and len(model.writer_calls()) == 1


def test_the_brief_job_asks_for_the_second_draft(monkeypatch):
    seen = {}

    def fake_explain(*args, **kw):
        seen.update(kw)
        raise RuntimeError("stop")

    monkeypatch.setattr(job, "explain_trend", fake_explain)
    cand = {"row": {"item_id": "i1"}, "pack": {"evidence": []}, "market": "ZA", "rerun": None}
    from datetime import date

    job._explain_one(cand, model=FakeModel(), spent_before=0.0, d=date(2026, 10, 5), model_call_guard=None)
    assert seen.get("second_draft") is True
