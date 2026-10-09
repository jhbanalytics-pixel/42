"""W8-DEC-15: one further attempt at a support or critic call that returned no answer, inside the model cap and the
run's deadline; a second non-return holds the card with 'check did not complete'. Offline: scripted models only, no
network, no spend."""

import json
from datetime import datetime, time
from types import SimpleNamespace

import pytest

from core.brief import explain, job
from core.brief.tests.test_brief_explain import FakeModel, good, run as run_unguarded
from core.brief.tests.test_brief_job import (
    CHECK_KEYS, D, SAST, FakeModel as JobModel, brief, checks, held_items, payload, world,
)
from core.brief.tests.test_brief_written_title import TITLE, titled
from core.trust import retained

INCOMPLETE = "check did not complete"
BLOCKED = "gemini-3.8-flash blocked the request or reply (finish reason SAFETY)"


class RateLimitError(Exception):
    pass


def run(model, **kw):
    """explain_trend with the run's deadline still open, as the job gives it. No retry_guard at all is the case that
    never retries (test_without_a_retry_guard_nothing_is_retried)."""
    kw.setdefault("retry_guard", lambda: True)
    return run_unguarded(model, **kw)


def outcome(kind, usd=0.0):
    """One scripted result of a model call. usd is what a failed call bills."""
    if kind == "ok":
        return None
    if kind == "timeout":
        exc = TimeoutError("model dispatch deadline expired")
    elif kind == "notext":
        exc = RuntimeError("gemini-3.8-flash returned no text (finish reason STOP)")
    elif kind == "blocked":
        exc = RuntimeError(BLOCKED)
    elif kind == "truncated":
        exc = RuntimeError("gemini-3.8-flash hit max_tokens=400; the JSON is truncated")
    elif kind == "rate":
        exc = RateLimitError("429 RESOURCE_EXHAUSTED")
    else:
        return kind
    exc.usd = usd
    return exc


class Scripted(FakeModel):
    """FakeModel with per-phase scripts. script maps writer, support, critic to a list popped one entry per call:
    "ok", an error kind (see outcome), "empty" ({}), or "none" (None). Every dispatch is recorded in order."""

    def __init__(self, drafts=(), script=None, **kw):
        super().__init__(drafts, **kw)
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.events = []

    def complete_json(self, *, system, user, schema, model, max_tokens):
        phase = "critic" if "ruled_out" in schema["properties"] else "support" if "verdict" in schema[
            "properties"] else "writer"
        self.events.append(phase)
        step = (self.script.get(phase) or ["ok"]).pop(0) if self.script.get(phase) else "ok"
        if step in ("empty", "none"):
            self.calls.append({"system": system, "user": user, "support": phase == "support",
                               "critic": phase == "critic", "model": model, "max_tokens": max_tokens,
                               "schema": schema})
            return ({} if step == "empty" else None), dict(self.usage)
        exc = outcome(step)
        if isinstance(exc, Exception):
            raise exc
        return super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)

    def count(self, phase):
        return self.events.count(phase)


def failed_rows(result):
    return [c for c in result["checks"] if c["detail"] == INCOMPLETE]


# A call that returned nothing is tried once more.


@pytest.mark.parametrize("first", ["timeout", "notext", "empty", "none"])
def test_a_support_call_with_no_answer_is_tried_once_more_and_publishes_when_it_answers(first):
    model = Scripted([good()], script={"support": [first, "ok"]})
    result = run(model)
    assert result["reason"] is None and result["numbers_only"] is False
    assert model.count("support") == 5  # three claims, one retried, and the sentence
    assert model.count("critic") == 1 and model.count("writer") == 1
    assert failed_rows(result) == []


@pytest.mark.parametrize("first", ["timeout", "notext", "empty", "none"])
def test_a_critic_call_with_no_answer_is_tried_once_more_and_publishes_when_it_answers(first):
    model = Scripted([good()], script={"critic": [first, "ok"]})
    result = run(model)
    assert result["reason"] is None
    assert model.count("critic") == 2 and model.count("support") == 4


def test_the_sentence_support_call_gets_the_same_second_attempt():
    model = Scripted([good()], script={"support": ["ok", "ok", "ok", "timeout", "ok"]})
    result = run(model)
    assert result["reason"] is None and model.count("support") == 5


# A second non-return holds the card.


def test_a_second_non_return_on_a_claim_holds_the_card_with_the_reason():
    model = Scripted([good()], script={"support": ["timeout", "timeout"]})
    result = run(model)
    assert result["reason"] == "check_incomplete" and result["numbers_only"] is True
    assert result["explanation"] is None and result["claims"] == []
    assert model.count("support") == 2 and model.count("critic") == 0 and model.count("writer") == 1
    [row] = failed_rows(result)
    assert (row["claim_id"], row["rule"], row["verdict"], row["checker"]) == ("c1", "K4", "cut", "model")
    assert row["span_sha256"] == "80fc228ba150a910acc3aad57abf08607197f399e1fc7b5bd805c9bdeacfec44"


def test_a_second_non_return_on_the_sentence_holds_the_card():
    model = Scripted([good()], script={"support": ["ok", "ok", "ok", "timeout", "empty"]})
    result = run(model)
    assert result["reason"] == "check_incomplete"
    assert model.count("support") == 5 and model.count("critic") == 0
    [row] = failed_rows(result)
    assert (row["claim_id"], row["rule"]) == (None, "K4")


def test_a_second_non_return_from_the_critic_holds_the_card():
    model = Scripted([good()], script={"critic": ["timeout", "timeout"]})
    result = run(model)
    assert result["reason"] == "check_incomplete"
    assert model.count("critic") == 2
    [row] = failed_rows(result)
    assert (row["claim_id"], row["rule"], row["verdict"]) == (None, "critic", "cut")


def test_never_more_than_one_further_attempt_per_call():
    model = Scripted([good()], script={"support": ["timeout"] * 5})
    result = run(model)
    assert result["reason"] == "check_incomplete"
    assert model.count("support") == 2


def test_a_check_that_did_not_complete_has_its_fixed_wording_and_no_repair_round():
    model = Scripted([good(), good()], script={"support": ["timeout", "timeout"]})
    result = run(model)
    assert model.count("writer") == 1
    assert job.failed_reason(result) == "Check did not complete"
    [row] = failed_rows(result)
    assert job.check_reason(row) == "Support check: check did not complete"


# What is never tried again.


@pytest.mark.parametrize("kind", ["blocked", "truncated", "rate"])
def test_a_refusal_or_a_cut_off_reply_is_never_tried_again(kind):
    model = Scripted([good()], script={"support": [kind]})
    result = run(model)
    assert result["reason"] == "model_error"
    assert model.count("support") == 1
    assert failed_rows(result) == []


@pytest.mark.parametrize("kind", ["blocked", "rate"])
def test_a_critic_refusal_is_never_tried_again(kind):
    model = Scripted([good()], script={"critic": [kind]})
    result = run(model)
    assert result["reason"] == "model_error"
    assert model.count("critic") == 1


def test_a_failing_verdict_is_never_tried_again():
    model = Scripted([good(), good()], verdicts={"Creators post": "unsupported"})
    result = run(model)
    assert result["reason"] == "failed_checks"
    assert model.count("support") == 6  # three claims, the one repair round, three claims again
    assert failed_rows(result) == []


def test_a_critic_that_does_not_rule_the_rival_out_is_never_tried_again():
    critic = {"non_cultural_explanation": "a paid campaign", "ruled_out": False, "local_why_now": True,
              "reason": "fake"}
    model = Scripted([good()], critic=critic)
    result = run(model)
    assert result["reason"] == "failed_checks"
    assert model.count("critic") == 1


def test_a_writer_call_with_no_answer_is_not_tried_again():
    model = Scripted([good()], script={"writer": ["timeout"]})
    result = run(model)
    assert result["reason"] == "model_error"
    assert model.count("writer") == 1


def test_a_title_check_with_no_answer_is_not_tried_again_and_never_holds_the_card():
    model = Scripted([titled()], script={"support": ["ok", "ok", "ok", "ok", "timeout", "timeout"]})
    result = run(model)
    assert result["reason"] is None and result["title_written"] is None
    assert model.count("support") == 5
    assert failed_rows(result) == []


# A timeout the auth or cleanup machinery reports is not "no answer" and is never tried again.


@pytest.mark.parametrize("flag", ["auth_unresolved", "request_cleanup_failed"])
def test_a_timeout_with_an_unresolved_auth_or_cleanup_flag_is_not_retried(flag):
    exc = TimeoutError("model dispatch deadline expired")
    exc.usd = 0.0
    setattr(exc, flag, True)
    model = Scripted([good()], script={"support": [exc, "ok"]})
    result = run(model)
    assert result["reason"] == "model_error"
    assert model.count("support") == 1 and failed_rows(result) == []
    plain = TimeoutError("model dispatch deadline expired")
    plain.usd = 0.0
    assert run(Scripted([good()], script={"support": [plain, "ok"]}))["reason"] is None


# The retry is inside the budget guard and the deadline.


def test_the_retry_goes_through_the_day_guard_and_the_cap_before_it_is_sent():
    model = Scripted([good()], script={"support": ["timeout", "ok"]})
    seen = []
    guard_model = SimpleNamespace(complete_json=lambda **kw: (seen.append("dispatch"), model.complete_json(**kw))[1])

    def guard():
        seen.append("guard")
        return True

    run(guard_model, model_call_guard=guard)
    first = seen.index("dispatch", seen.index("dispatch") + 1)  # writer is dispatch 1, the failed support call is 2
    assert seen[first - 1] == "guard" and seen[first + 1:first + 3] == ["guard", "dispatch"]


def test_a_retry_the_day_guard_refuses_is_not_sent():
    model = Scripted([good()], script={"support": ["timeout", "ok"]})
    answers = iter([True, True, False])  # writer, first support call, then the retry

    result = run(model, model_call_guard=lambda: next(answers, False))
    assert result["reason"] == "model_error"
    assert model.count("support") == 1


def test_cap_reached_means_no_retry(monkeypatch):
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 1.0)
    model = Scripted([good()], script={"support": [outcome("timeout", usd=0.995)]})
    result = run(model)
    assert result["reason"] == "model_error"
    assert model.count("support") == 1
    assert result["usage_usd"] == pytest.approx(0.01 + 0.995)


def test_the_cap_counts_the_failed_attempts_bill_before_the_retry_is_reserved(monkeypatch):
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 1.0)
    # Room for the retry: 0.9 billed leaves 0.1, more than one estimate.
    model = Scripted([good()], script={"support": [outcome("timeout", usd=0.5), "ok"]})
    result = run(model)
    assert result["reason"] is None and model.count("support") == 5


def test_without_a_retry_guard_nothing_is_retried():
    model = Scripted([good()], script={"support": ["timeout", "ok"]})
    result = run_unguarded(model)
    assert result["reason"] == "model_error"
    assert model.count("support") == 1 and failed_rows(result) == []
    model = Scripted([good()], script={"critic": ["timeout", "ok"]})
    result = run_unguarded(model)
    assert result["reason"] == "model_error" and model.count("critic") == 1


def test_deadline_passed_means_no_retry():
    model = Scripted([good()], script={"support": ["timeout", "ok"]})
    result = run(model, retry_guard=lambda: False)
    assert result["reason"] == "model_error"
    assert model.count("support") == 1 and failed_rows(result) == []


def test_the_deadline_is_asked_once_for_each_retry_and_never_for_a_clean_call():
    asked = []
    model = Scripted([good()], script={"support": ["timeout", "ok"], "critic": ["timeout", "ok"]})
    result = run(model, retry_guard=lambda: asked.append(1) or True)
    assert result["reason"] is None
    assert len(asked) == 2
    clean = []
    run(Scripted([good()]), retry_guard=lambda: clean.append(1) or True)
    assert clean == []


def test_both_attempts_are_billed():
    model = Scripted([good()], script={"support": [outcome("timeout", usd=0.02), "ok"]})
    result = run(model)
    assert result["reason"] is None
    assert result["usage_usd"] == pytest.approx(0.01 + 0.02 + 0.01 * 5)


# The job: the reason shown, the stored row, the dispatch count and the deadline.


class FlakyJobModel(JobModel):
    """The job's FakeModel whose support check of the claim "Local creators are posting" fails its first `fails`
    attempts for each distinct prompt with `kind`. dispatched counts the attempts per prompt."""

    def __init__(self, fails, kind="timeout", on_fail=None, **kw):
        super().__init__(**kw)
        self.fails, self.kind, self.on_fail = fails, kind, on_fail
        self.dispatched = {}

    def complete_json(self, *, system, user, schema, model, max_tokens):
        if "verdict" in schema["properties"] and "Local creators are posting videos with this tag." in user.split(
                "Label:", 1)[0]:
            n = self.dispatched[user] = self.dispatched.get(user, 0) + 1
            if n <= self.fails:
                if self.on_fail:
                    self.on_fail()
                exc = outcome(self.kind)
                if isinstance(exc, Exception):
                    raise exc
                return {}, {"input_tokens": 1, "output_tokens": 1, "usd": 0.0}
        return super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)


def test_one_non_return_in_the_job_publishes_and_sends_the_retry_once():
    model = FlakyJobModel(1)
    r = brief(world(n=1), model=model)
    assert sorted(model.dispatched.values()) == [2, 2, 2]
    assert not [c for c in checks(r) if "did not complete" in c["reason"]]
    assert any(c["explained"] for m in ("ZA", "NG", "KE") for c in payload(r, m)["cards"])


def test_a_second_non_return_in_the_job_holds_each_card_with_the_reason_shown():
    model = FlakyJobModel(2)
    r = brief(world(n=1), model=model)
    assert sorted(model.dispatched.values()) == [2, 2, 2]
    for m in ("ZA", "NG", "KE"):
        [item] = held_items(r, m).values()
        assert item["reason"] == "explanation_failed"
        assert item["failed_reason"] == "Check did not complete"
    rows = [c for c in checks(r) if "did not complete" in c["reason"]]
    assert len(rows) == 3
    for c in rows:
        assert c["reason"] == "Support check: check did not complete"
        assert c["reason_code"] == "check_incomplete" and retained.is_digest(c["span_sha256"])
        assert set(c) == CHECK_KEYS | {"span_sha256", "reason_code"}


def test_a_refusal_in_the_job_is_never_retried_and_shows_no_incomplete_reason():
    model = FlakyJobModel(1, kind="blocked")
    r = brief(world(n=1), model=model)
    assert sorted(model.dispatched.values()) == [1, 1, 1]
    assert not [c for c in checks(r) if "did not complete" in c["reason"]]
    assert "Check did not complete" not in json.dumps([payload(r, m) for m in ("ZA", "NG", "KE")])


def test_the_job_gives_the_retry_the_runs_deadline():
    now = {"t": datetime.combine(D, time(5, 30), SAST)}
    model = FlakyJobModel(1, on_fail=lambda: now.update(t=datetime.combine(D, time(6, 15), SAST)))
    brief(world(n=1), model=model, clock=lambda: now["t"])
    assert list(model.dispatched.values()) == [1]


def test_check_incomplete_is_one_of_the_failed_check_reasons():
    assert "check_incomplete" in job.FAILED_CHECKS
    assert "check_incomplete" in retained.REASON_CODES


def test_the_job_hands_each_explanation_a_retry_guard_that_follows_the_deadline(monkeypatch):
    from core.brief.tests.test_brief_job import FakeChain

    seen = []

    def fake_explain(row, pack, **kw):
        seen.append(kw["retry_guard"])
        return {"reason": None, "usage_usd": 0.0, "checks": [], "numbers_only": False, "explanation": "x"}

    monkeypatch.setattr(job, "explain_trend", fake_explain)
    now = {"t": datetime.combine(D, time(5, 30), SAST)}
    cand = {"market": "ZA", "row": {"item_id": "i1"}, "pack": {}, "rerun": None}
    job._explain_all([cand], model=object(), base_usd=0.0, spend={"usd": 0.0}, clock=lambda: now["t"],
                     chain=FakeChain(), d=D, workers=1, started=now["t"])
    [guard] = seen
    assert guard() is True
    now["t"] = datetime.combine(D, time(6, 15), SAST)
    assert guard() is False
