"""An explanation that ended in a model error is made once more, and pattern bucket 0 goes first (23 Oct 2026).

On 23 Oct the brief ran 04:39 to 05:15 SAST, an hour before its 06:15 deadline, yet KE's top candidate "monaco, the
monaco, nairobi" (pattern bucket 0) and NG's "realtemiday0 oluku" were held "Explanation not run: a model call failed
or ran out of time": a support call of KE's after the repair round, and NG's first writer call, ended in a model error
the per-call retry does not cover (the writer has none; a check retries only a call that gave nothing back, once).
Now the whole explanation is made once more (model-error-retry) under the limits every explanation meets: the day's
model cap with both attempts billed, a whole call (GEMINI_TIMEOUT_S) still fitting before the deadline and the time
limit, the same SAST day and a breaker that has not tripped. A refusal is never asked again, and a busy model stays
the breaker's and the resume's. The explanations of bucket 0 candidates of every market run first (adopted-first).
No cap, timeout or limit is raised. Fakes only: no model, no cloud.
"""

import re
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import pytest

from core.brief import explain, job
from core.brief.payload import NOT_RUN_WORDS
from core.brief.tests.test_brief_check_retry import FlakyJobModel, Scripted, outcome
from core.brief.tests.test_brief_explain import good, run as run_explain
from core.brief.tests.test_brief_job import (  # noqa: F401
    D, EARLY, MARKETS, SAST, FakeChain, FakeModel, brief, busy_waits, held_items, market_scope_is_valid_by_default,
    payload, world,
)


def explained(r, markets=MARKETS):
    return {c["item_id"] for m in markets for c in payload(r, m)["cards"] + payload(r, m)["more"]
            if c["explanation_status"] == "explained"}


class WriterFails(FakeModel):
    """The job's FakeModel whose first `fails` writer calls for each candidate raise the error of `kind`."""

    def __init__(self, fails=1, kind="timeout", **kw):
        super().__init__(**kw)
        self.fails, self.kind, self.writers = fails, kind, {}

    def complete_json(self, *, system, user, schema, model, max_tokens):
        if "claims" in schema["properties"]:
            key = re.search(r"#(\w+)", user).group(1)
            n = self.writers[key] = self.writers.get(key, 0) + 1
            if n <= self.fails:
                self.calls.append({"user": user, "support": False, "critic": False})
                raise outcome(self.kind)
        return super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)


# What is made once more.


@pytest.mark.parametrize("kind", ["timeout", "504", "500", "503"])
def test_a_writer_call_that_failed_is_explained_again_and_publishes(kind):
    model = WriterFails(1, kind)
    r = brief(world(n=1), model=model)
    assert explained(r) == {"za1", "ng1", "ke1"}
    assert sorted(model.writers.values()) == [2, 2, 2]
    assert r.counts["model_retry"] == [{"market": m, "item_id": f"{m.lower()}1", "reason": None} for m in MARKETS]


@pytest.mark.parametrize("kind", ["500", "503", "truncated"])
def test_a_check_call_that_failed_past_its_own_retry_is_explained_again_and_publishes(kind):
    model = FlakyJobModel(1, kind=kind)
    r = brief(world(n=1), model=model)
    assert sorted(model.dispatched.values()) == [2, 2, 2]
    assert explained(r) == {"za1", "ng1", "ke1"}
    for m in MARKETS:
        assert held_items(r, m) == {}


def test_a_504_whose_check_retry_also_failed_is_explained_again():
    # The check's own 504 retry is spent on the first attempt; the whole explanation is then made once more.
    model = FlakyJobModel(2, kind="504")
    r = brief(world(n=1), model=model)
    assert sorted(model.dispatched.values()) == [3, 3, 3]
    assert explained(r) == {"za1", "ng1", "ke1"}


def test_the_explanation_is_made_once_more_at_most_and_then_held_not_run():
    model = FlakyJobModel(9, kind="500")
    r = brief(world(n=1), model=model)
    assert sorted(model.dispatched.values()) == [2, 2, 2]
    for m in MARKETS:
        [item] = held_items(r, m).values()
        assert item["failed_reason"] == NOT_RUN_WORDS["model_error"]
    assert [x["reason"] for x in r.counts["model_retry"]] == ["model_error"] * 3


def test_both_attempts_are_billed_and_the_card_stores_the_second():
    plain = brief(world(n=1, markets=("ZA",)), model=FakeModel())
    r = brief(world(n=1, markets=("ZA",)), model=WriterFails(1, "500"))
    # The failed writer call billed nothing (the fake's error carries usd 0), so the spend is the clean run's.
    assert r.counts["model_usd"] == pytest.approx(plain.counts["model_usd"])
    r = brief(world(n=1, markets=("ZA",)), model=FlakyJobModel(1, kind="500"))
    # The first attempt's writer call is billed beside the whole second attempt.
    assert r.counts["model_usd"] == pytest.approx(plain.counts["model_usd"] + 0.01)
    assert payload(r, "ZA")["cards"][0]["explanation"] == payload(plain, "ZA")["cards"][0]["explanation"]


def test_a_clean_run_records_no_retry():
    assert "model_retry" not in brief(world(n=1), model=FakeModel()).counts


# What is never made again.


def test_a_refusal_is_never_asked_again():
    model = WriterFails(1, "blocked")
    r = brief(world(n=1), model=model)
    assert sorted(model.writers.values()) == [1, 1, 1]
    assert explained(r) == set() and "model_retry" not in r.counts
    result = run_explain(Scripted([good()], script={"writer": ["blocked"]}))
    assert result["reason"] == "model_error" and result["model_refused"] is True
    assert "model_refused" not in run_explain(Scripted([good()], script={"writer": ["500"]}))


def test_no_retry_once_a_whole_call_no_longer_fits_before_the_deadline():
    near = datetime.combine(D, time(6, 14, 30), SAST)  # 30 s left, under GEMINI_TIMEOUT_S
    model = FlakyJobModel(1, kind="500")
    r = brief(world(n=1), model=model, clock=lambda: near)
    assert sorted(model.dispatched.values()) == [1, 1, 1]
    assert explained(r) == set() and "model_retry" not in r.counts


def test_no_retry_once_the_days_model_cap_is_reached(monkeypatch):
    # The first attempt's writer call brings the day to the cap, so the second attempt is never started.
    monkeypatch.setattr(explain, "_estimate_usd", lambda *args: 0.0)
    monkeypatch.setattr(job, "model_daily_usd", lambda: 0.01)
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 0.01)
    model = FlakyJobModel(1, kind="500")
    r = brief(world(n=1, markets=("ZA",)), model=model)
    assert list(model.dispatched.values()) == [1]
    assert "model_retry" not in r.counts and r.counts["model_usd"] == pytest.approx(0.01)


def test_retry_admission_reads_the_same_limits_as_any_call():
    chain = FakeChain()
    ok = {"reason": "model_error", "usage_usd": 0.0}
    breaker = SimpleNamespace(tripped=None)
    admits = lambda result=ok, cand=None, b=breaker, spent=0.0, now=EARLY, started=None: job._retry_admits(
        result, cand or {}, b, spent, now, started, D, chain)
    assert admits()
    assert not admits(result={"reason": "failed_checks", "usage_usd": 0.0})
    assert not admits(result={"reason": "check_incomplete", "usage_usd": 0.0})
    assert not admits(result={"reason": "model_cap", "usage_usd": 0.0})
    assert not admits(result={"reason": "job_error", "usage_usd": 0.0})
    assert not admits(result={**ok, "model_refused": True})
    assert not admits(cand={"busy_reason": "busy"})
    assert not admits(b=SimpleNamespace(tripped="ModelUnavailable"))
    assert not admits(spent=10_000.0)
    assert not admits(now=datetime.combine(D, time(6, 15), SAST))
    assert not admits(now=datetime.combine(D + timedelta(days=1), time(5), SAST))
    deadline = datetime.combine(D, time(6, 15), SAST)
    assert admits(now=deadline - timedelta(seconds=job.GEMINI_TIMEOUT_S))
    assert not admits(now=deadline - timedelta(seconds=job.GEMINI_TIMEOUT_S - 1))
    # The task's time limit counts as the deadline does.
    limit = job.collect_chain.TIMEOUTS["brief"] - job.FINISH_MARGIN
    assert not admits(started=EARLY - limit + timedelta(seconds=30))
    assert admits(started=EARLY - limit + timedelta(seconds=job.GEMINI_TIMEOUT_S))


# Bucket 0 first.


def cand(market, n, bucket):
    return {"market": market, "row": {"item_id": f"{market.lower()}{n}", "market_scope": "market",
                                      "_selection_pattern": bucket},
            "decision": SimpleNamespace(publish=True, where="today")}


def test_bucket_0_candidates_of_every_market_are_explained_before_the_rest_each_part_in_rank_order():
    by_market = {"ZA": [cand("ZA", 1, 1), cand("ZA", 2, 0), cand("ZA", 3, 1)],
                 "NG": [cand("NG", 1, 0), cand("NG", 2, 0)],
                 "KE": [cand("KE", 1, 2), cand("KE", 2, 1), cand("KE", 3, 0)]}
    order = [c["row"]["item_id"] for c in job._in_rank_order(by_market)]
    assert order == ["za2", "ng1", "ke3", "ng2", "za1", "ke1", "za3", "ke2"]


def test_without_bucket_0_the_order_is_rank_order_across_markets_as_before():
    by_market = {m: [cand(m, n, None) for n in (1, 2)] for m in MARKETS}
    order = [c["row"]["item_id"] for c in job._in_rank_order(by_market)]
    assert order == ["za1", "ng1", "ke1", "za2", "ng2", "ke2"]


def test_a_late_failure_never_costs_the_bucket_0_candidate(monkeypatch):
    # KE's third candidate is bucket 0, so it is explained first and has its retry long before the deadline stops the
    # run; the rank 1 candidates of ZA and NG wait behind it.
    original = job._candidates

    def with_buckets(*args, **kwargs):
        by_market = original(*args, **kwargs)
        for m, cands in by_market.items():
            for c in cands:
                c["row"]["_selection_pattern"] = 0 if c["row"]["item_id"] == "ke3" else 1
        return by_market

    monkeypatch.setattr(job, "_candidates", with_buckets)
    model = WriterFails(1, "timeout")
    order = []
    real = model.complete_json

    def recorded(**kw):
        if "claims" in kw["schema"]["properties"]:
            order.append(re.search(r"#(\w+)", kw["user"]).group(1))
        return real(**kw)

    model.complete_json = recorded
    r = brief(world(n=3), model=model)
    assert order[:2] == ["ke3", "ke3"]
    assert "ke3" in explained(r)
