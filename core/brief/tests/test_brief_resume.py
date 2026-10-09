"""A brief run stopped by Vertex 429 before 06:15 may resume once (W8-DEC-18, Albert, 9 Oct 2026).

The breaker (core/brief/job.py _Breaker) trips when explanations keep running out of tries. Before this decision the
run then published the rest as numbers and posts only. Now, when that stop was a 429 and it came before 06:15 SAST, the
run waits RESUME_WAIT_S (10 minutes) and explains what is left once more, with a fresh breaker, inside the model cap
and the same deadline and time limit. A second stop is final: no third attempt. A card already explained is kept as it
is and never asked for again."""

import json
from datetime import datetime, time, timedelta

import pytest

from core.brief import explain, job
from core.brief.payload import MODEL_REFUSED
from core.brief.tests import test_brief_job as job_tests
from core.brief.tests.test_brief_busy import DEADLINE, Busy, Clock, Refusing, explained, run
from core.brief.tests.test_brief_job import (SAST, EARLY, MARKETS, FakeModel,  # noqa: F401
                                             busy_waits, held_items, market_scope_is_valid_by_default, world)
from core.detect.tests import duck
from core.detect.tests.fixtures import D

TRIP_CALLS = (job.BUSY_RETRIES + 1) * job.BUSY_TRIP_ITEMS  # calls refused before the breaker trips: 3 items, 5 tries
WAIT = 600


def resumes(clock):
    return clock.slept.count(WAIT)


def test_the_wait_is_ten_minutes():
    assert job.RESUME_WAIT_S == 600


def test_a_run_stopped_by_429_before_0615_resumes_once_after_ten_minutes_and_explains_the_rest():
    plain = run(world(n=2), model=FakeModel())
    clock = Clock(EARLY)
    model = Refusing(lambda n: n <= TRIP_CALLS)
    r = run(world(n=2), model=model, clock=clock, sleep=clock.sleep)
    assert resumes(clock) == 1 and clock.slept[-1] == WAIT
    assert len(explained(r)) == len(explained(plain)) == 6
    assert "model" not in r.counts
    assert r.counts["model_resume"] == {"wait_s": WAIT, "stopped_by": "refused", "retried": 6, "tripped_again": False}
    # The refusals are booked as before, and the run is no longer tripped at its end.
    assert r.counts["model_busy"]["refusals"] == TRIP_CALLS and r.counts["model_busy"]["tripped"] is None
    assert r.counts["model_usd"] == pytest.approx(plain.counts["model_usd"])
    for m in MARKETS:
        assert all(i.get("failed_reason") != MODEL_REFUSED for i in held_items(r, m).values())


def test_a_second_stop_is_final_and_there_is_no_third_attempt():
    clock = Clock(EARLY)
    model = Refusing(lambda n: True)
    r = run(world(n=2), model=model, clock=clock, sleep=clock.sleep)
    assert resumes(clock) == 1
    assert len(model.calls) == 2 * TRIP_CALLS
    assert r.counts["model_resume"] == {"wait_s": WAIT, "stopped_by": "refused", "retried": 6, "tripped_again": True}
    assert r.counts["model"]["reason"] == "model_unavailable"
    assert r.counts["model_busy"]["tripped"] == "refused"
    assert r.counts["model_busy"]["gave_up"] == 2 * job.BUSY_TRIP_ITEMS
    for m in MARKETS:
        assert {i["failed_reason"] for i in held_items(r, m).values()} == {MODEL_REFUSED}


class StopsAt(Refusing):
    """Refuses every call; on call number `at` it moves the clock to `when`, as the waits and calls before it would."""

    def __init__(self, clock, at, when):
        super().__init__(lambda n: True)
        self.clock, self.at, self.when = clock, at, when

    def complete_json(self, **kw):
        if len(self.calls) + 1 == self.at:
            self.clock.now = self.when
        return super().complete_json(**kw)


@pytest.mark.parametrize("stopped_at", [DEADLINE, DEADLINE + timedelta(minutes=1)], ids=["at_0615", "after_0615"])
def test_no_resume_at_or_after_0615(stopped_at):
    clock = Clock(EARLY)
    model = StopsAt(clock, TRIP_CALLS, stopped_at)
    r = run(world(n=2), model=model, clock=clock, sleep=clock.sleep)
    assert resumes(clock) == 0
    assert len(model.calls) == TRIP_CALLS
    assert "model_resume" not in r.counts
    assert r.counts["model_busy"]["tripped"] == "refused"


def test_no_resume_when_the_wait_would_end_at_0615():
    clock = Clock(EARLY)
    model = StopsAt(clock, TRIP_CALLS, DEADLINE - timedelta(seconds=WAIT))  # 06:05:00, the wait ends at 06:15:00
    r = run(world(n=2), model=model, clock=clock, sleep=clock.sleep)
    assert resumes(clock) == 0 and len(model.calls) == TRIP_CALLS
    assert "model_resume" not in r.counts


def test_a_resume_whose_wait_ends_one_second_before_0615_is_allowed():
    clock = Clock(EARLY)
    model = StopsAt(clock, TRIP_CALLS, DEADLINE - timedelta(seconds=WAIT + 1))
    r = run(world(n=2), model=model, clock=clock, sleep=clock.sleep)
    assert resumes(clock) == 1
    assert clock.now < DEADLINE
    assert r.counts["model_resume"]["wait_s"] == WAIT


def test_no_resume_when_the_wait_would_end_past_the_runs_time_limit():
    start = datetime.combine(D, time(3, 0), SAST)
    clock = Clock(start)
    limit = job.collect_chain.TIMEOUTS["brief"] - job.FINISH_MARGIN
    # The stop comes after 45 minutes; 45 + 10 is past the 52 minutes the run may take.
    model = StopsAt(clock, TRIP_CALLS, start + timedelta(minutes=45))
    assert timedelta(minutes=45) + timedelta(seconds=WAIT) >= limit
    r = run(world(n=2), model=model, clock=clock, sleep=clock.sleep)
    assert resumes(clock) == 0 and len(model.calls) == TRIP_CALLS
    assert "model_resume" not in r.counts


def test_no_resume_past_a_recovery_deadline_that_is_earlier_than_the_wait(monkeypatch):
    monkeypatch.setenv("FORCE_RERUN", "1")
    monkeypatch.setenv("RUN_DATE", D.isoformat())
    monkeypatch.setenv("BRIEF_RECOVERY_UNTIL", "05:45")
    clock = Clock(EARLY)
    model = Refusing(lambda n: True)
    r = run(world(n=2), model=model, clock=clock, sleep=clock.sleep)
    # The backoff of the first pass ends by about 05:39, so the 10 minute wait would end after 05:45.
    assert clock.now < EARLY + timedelta(minutes=10)
    assert resumes(clock) == 0 and len(model.calls) == TRIP_CALLS
    assert "model_resume" not in r.counts


def spend_refusing(n_refused):
    """Refuses the first n_refused calls, each reporting 0.25 of spend, which the run books once and does not retry."""
    return Refusing(lambda n: n <= n_refused, error=lambda: Busy(usd=0.25))


@pytest.mark.parametrize("cap", [0.75, 0.7], ids=["spend_equals_cap", "spend_over_cap"])
def test_no_resume_when_the_model_cap_is_reached(monkeypatch, cap):
    monkeypatch.setattr(job, "model_daily_usd", lambda: cap)
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 80.0)
    clock = Clock(EARLY)
    model = spend_refusing(3)
    r = run(world(n=2), model=model, clock=clock, sleep=clock.sleep)
    # Three refusals booked 0.75 and tripped the breaker; that has reached the cap, so nothing is left to resume with.
    assert r.counts["model_usd"] == pytest.approx(0.75)
    assert resumes(clock) == 0 and len(model.calls) == 3
    assert "model_resume" not in r.counts


def test_the_same_run_resumes_when_the_cap_still_has_room(monkeypatch):
    monkeypatch.setattr(job, "model_daily_usd", lambda: 0.8)
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 80.0)
    clock = Clock(EARLY)
    r = run(world(n=2), model=spend_refusing(3), clock=clock, sleep=clock.sleep)
    assert resumes(clock) == 1
    assert r.counts["model_resume"]["tripped_again"] is False


def test_spend_already_at_the_cap_at_the_start_neither_calls_nor_resumes():
    con = world(n=2)
    duck.load(con, "agent.runs", [{**job_tests.run("brief", D, run_id="brief-earlier", status="failed"),
                                   "counts": json.dumps({"model_usd": 50.0})}])
    clock = Clock(EARLY)
    model = Refusing(lambda n: True)
    r = run(con, model=model, clock=clock, sleep=clock.sleep)
    assert model.calls == [] and resumes(clock) == 0
    assert "model_resume" not in r.counts


class AuthStop(Exception):
    """Not a 429: a stop the deadline owner reports as unresolved authentication."""

    auth_unresolved = True


def test_a_stop_that_is_not_a_429_does_not_resume():
    clock = Clock(EARLY)
    model = Refusing(lambda n: True, error=AuthStop)
    r = run(world(n=2), model=model, clock=clock, sleep=clock.sleep)
    assert len(model.calls) == 1
    assert resumes(clock) == 0 and clock.slept == []
    assert "model_resume" not in r.counts
    assert r.counts["model"]["reason"] == "model_unavailable"


class Mixed(Refusing):
    """Call 1 fails with a 503, which ends that explanation without a wait; the next TRIP_CALLS calls are 429s."""

    def complete_json(self, **kw):
        n = len(self.calls) + 1
        if n == 1:
            self.calls.append({"user": kw["user"], "support": False, "critic": False, "refused": True})
            raise RuntimeError("503 service unavailable")
        return super().complete_json(**kw)


def test_an_explanation_that_failed_for_another_reason_is_not_retried_on_resume():
    clock = Clock(EARLY)
    model = Mixed(lambda n: 2 <= n <= TRIP_CALLS + 1)
    r = run(world(n=2), model=model, clock=clock, sleep=clock.sleep)
    assert resumes(clock) == 1
    first = model.calls[0]["user"]
    assert sum(1 for c in model.calls if c["user"] == first) == 1  # asked for once, never again
    assert r.counts["model_resume"]["retried"] == 5  # six items, one of them failed for another reason
    assert len(explained(r)) == 5


def test_cards_already_explained_are_not_asked_for_again():
    plain = run(world(n=2), model=FakeModel())
    per = len(plain.model.calls) // 6
    clock = Clock(EARLY)
    # One explanation goes through, then three run out of tries and trip the breaker, then all is well.
    model = Refusing(lambda n: per < n <= per + TRIP_CALLS)
    r = run(world(n=2), model=model, clock=clock, sleep=clock.sleep)
    assert resumes(clock) == 1 and r.counts["model_resume"]["retried"] == 5
    went_through = [c for c in model.calls if not c.get("refused")]
    assert len(went_through) == len(plain.model.calls)
    writers = [c["user"] for c in went_through if not c["support"] and not c["critic"]]
    assert len(writers) == len(set(writers)) == 6
    assert r.counts["model_usd"] == pytest.approx(plain.counts["model_usd"])
    for m in MARKETS:
        for p, q in zip(job_tests.all_cards(job_tests.payload(r, m)), job_tests.all_cards(job_tests.payload(plain, m))):
            assert p == q


@pytest.mark.parametrize("stopped_at", [DEADLINE, DEADLINE + timedelta(minutes=20)], ids=["at_0615", "after_0615"])
def test_a_late_run_with_a_later_recovery_deadline_is_not_resumed_either(monkeypatch, stopped_at):
    # The wait would end inside the recovery deadline and the run's time limit, but the decision resumes only a run
    # stopped before 06:15.
    monkeypatch.setenv("FORCE_RERUN", "1")
    monkeypatch.setenv("RUN_DATE", D.isoformat())
    monkeypatch.setenv("BRIEF_RECOVERY_UNTIL", "08:00")
    clock = Clock(DEADLINE - timedelta(minutes=3))
    model = StopsAt(clock, TRIP_CALLS, stopped_at)
    r = run(world(n=2), model=model, clock=clock, sleep=clock.sleep)
    assert resumes(clock) == 0 and len(model.calls) == TRIP_CALLS
    assert "model_resume" not in r.counts


class StopThenSlow(Refusing):
    """Refuses calls up to TRIP_CALLS. The last of them lands at 06:04:00, so the breaker trips then; the first call
    after the wait lands at 06:16:00."""

    def __init__(self, clock):
        super().__init__(lambda n: n <= TRIP_CALLS)
        self.clock = clock

    def complete_json(self, **kw):
        n = len(self.calls) + 1
        if n == TRIP_CALLS:
            self.clock.now = DEADLINE - timedelta(minutes=11)
        if n == TRIP_CALLS + 1:
            self.clock.now = DEADLINE + timedelta(minutes=1)
        return super().complete_json(**kw)


def test_what_the_resume_does_not_reach_keeps_the_wording_of_the_first_stop():
    clock = Clock(EARLY)
    r = run(world(n=2), model=StopThenSlow(clock), clock=clock, sleep=clock.sleep)
    assert resumes(clock) == 1
    # The first call after the wait ends past 06:15, which ends that explanation, and the deadline stops the rest of the
    # resume pass. Those five were not refused in it and keep the wording of the first stop.
    assert r.counts["explanation_stop"]["reason"] == "deadline" and len(r.counts["explanation_stop"]["skipped"]) == 5
    left = [i for m in MARKETS for i in held_items(r, m).values()]
    assert explained(r) == [] and len(left) == 6
    # The sixth card was reached by the resume and ended in a model_error with no wording of its own. It was refused in
    # the first pass, so it says so too rather than showing no reason.
    assert {i["failed_reason"] for i in left} == {MODEL_REFUSED}


class Uncertain(Exception):
    """A call whose usage is unknown: the explainer books the estimate and keeps it as a reservation."""

    reserve_model_estimate = True


class UncertainAt(Refusing):
    """Call numbers in `at` end in an uncertain failure; the next TRIP_CALLS calls after the first of them are 429s."""

    def __init__(self, at):
        super().__init__(lambda n: False)
        self.at = at
        self.uncertain = []

    def complete_json(self, **kw):
        n = len(self.calls) + 1
        if n in self.at or 2 <= n <= TRIP_CALLS + 1:
            self.calls.append({"user": kw["user"], "support": False, "critic": False, "refused": True})
            if n in self.at:
                self.uncertain.append(Uncertain("timed out after send"))
                raise self.uncertain[-1]
            raise Busy()
        return super().complete_json(**kw)


def _reserved(at):
    clock = Clock(EARLY)
    model = UncertainAt(at)
    r = run(world(n=2), model=model, clock=clock, sleep=clock.sleep)
    return r, model


def test_the_reservation_of_an_uncertain_call_in_each_pass_adds_up():
    """Each pass has its own breaker, so the second pass used to replace the first pass's reservation."""
    for at in ({1}, {TRIP_CALLS + 2}, {1, TRIP_CALLS + 2}):
        r, model = _reserved(at)
        assert len(model.uncertain) == len(at) and "model_resume" in r.counts
        booked = sum(exc.reserved_usd for exc in model.uncertain)
        assert booked > 0 and r.counts["model_reserved_usd"] == pytest.approx(booked, abs=1e-6)
        assert r.counts["model_usd"] >= r.counts["model_reserved_usd"]


def _earlier_spend(con, usd):
    duck.load(con, "agent.runs", [{**job_tests.run("brief", D, run_id=f"brief-other-{usd}", status="failed"),
                                   "counts": json.dumps({"model_usd": usd})}])


def test_spend_booked_by_another_stage_during_the_wait_is_seen_before_the_cap_check(monkeypatch):
    monkeypatch.setattr(job, "model_daily_usd", lambda: 1.0)
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 80.0)
    con = world(n=2)
    clock = Clock(EARLY)

    def sleep(seconds):
        clock.sleep(seconds)
        if seconds == WAIT:
            _earlier_spend(con, 5.0)
    model = spend_refusing(3)
    r = run(con, model=model, clock=clock, sleep=sleep)
    assert resumes(clock) == 1 and len(model.calls) == 3  # the wait was made, and nothing was asked after it
    assert "model_resume" not in r.counts and explained(r) == []
    for m in MARKETS:
        assert {i["failed_reason"] for i in held_items(r, m).values()} == {MODEL_REFUSED}


def test_the_resumed_pass_counts_the_spend_booked_during_the_wait_against_the_cap(monkeypatch):
    plain = run(world(n=2), model=FakeModel())
    per_item = plain.counts["model_usd"] / 6
    other = 0.2
    # Three refusals book 0.75. After the wait the day holds `other` more, which still leaves room, and the cap falls
    # inside the second resumed explanation, so the resume stops there.
    monkeypatch.setattr(job, "model_daily_usd", lambda: 0.75 + other + 1.5 * per_item)
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 80.0)
    con = world(n=2)
    clock = Clock(EARLY)

    def sleep(seconds):
        clock.sleep(seconds)
        if seconds == WAIT:
            _earlier_spend(con, other)
    r = run(con, model=spend_refusing(3), clock=clock, sleep=sleep)
    assert resumes(clock) == 1 and r.counts["model_resume"]["retried"] == 6
    assert len(explained(r)) == 2


def test_spend_of_an_earlier_run_counts_against_the_cap_at_the_stop(monkeypatch):
    monkeypatch.setattr(job, "model_daily_usd", lambda: 0.8)
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 80.0)
    con = world(n=2)
    _earlier_spend(con, 0.1)
    clock = Clock(EARLY)
    model = spend_refusing(3)
    r = run(con, model=model, clock=clock, sleep=clock.sleep)
    # 0.1 from the earlier run and 0.75 booked by the three refusals have passed the cap, though 0.75 alone has not.
    assert resumes(clock) == 0 and len(model.calls) == 3 and "model_resume" not in r.counts


def test_a_failed_read_of_the_days_spend_after_the_wait_keeps_the_value_from_the_start(monkeypatch, capsys):
    clock = Clock(EARLY)
    reads = []
    real = job.spent_today

    def spent(client, d, core=job.CORE, agent=job.AGENT):
        reads.append(1)
        if len(reads) > 1:
            raise RuntimeError("BigQuery unreachable")
        return real(client, d, core, agent)
    monkeypatch.setattr(job, "spent_today", spent)
    r = run(world(n=2), model=Refusing(lambda n: n <= TRIP_CALLS), clock=clock, sleep=clock.sleep)
    assert len(reads) == 2 and resumes(clock) == 1
    assert len(explained(r)) == 6 and r.counts["model_resume"]["tripped_again"] is False
    assert "could not be read again" in capsys.readouterr().err


class RefusedThenOutOfTime(Refusing):
    """Refuses every call. The last call of the first pass lands at 06:04:00, where the breaker trips; the first call
    of the resume lands one second before 06:15, so no wait after its refusal can end in time."""

    def __init__(self, clock):
        super().__init__(lambda n: True)
        self.clock = clock

    def complete_json(self, **kw):
        n = len(self.calls) + 1
        if n == TRIP_CALLS:
            self.clock.now = DEADLINE - timedelta(minutes=11)
        if n == TRIP_CALLS + 1:
            self.clock.now = DEADLINE - timedelta(seconds=1)
        return super().complete_json(**kw)


def test_the_resumes_own_wording_is_kept_over_the_first_stops():
    from core.brief.payload import MODEL_BUSY

    clock = Clock(EARLY)
    r = run(world(n=2), model=RefusedThenOutOfTime(clock), clock=clock, sleep=clock.sleep)
    assert resumes(clock) == 1
    left = [i["failed_reason"] for m in MARKETS for i in held_items(r, m).values()]
    assert len(left) == 6 and explained(r) == []
    # The resume ran out of time on its own refusal, and says so for every card it left, over the first stop's wording.
    assert left == [MODEL_BUSY] * 6
