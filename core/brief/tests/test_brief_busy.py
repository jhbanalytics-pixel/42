"""A busy model slows the brief down, it does not empty it (core/brief/job.py _Breaker and _explain_all). On 6 Oct
2026 Vertex refused calls on and off with 429 RESOURCE_EXHAUSTED; the first refusal tripped the breaker, so 25 of 30
topics were never tried and the brief had no cards. A refused call is now waited out and made again, the breaker trips
only on sustained refusal or when a wait would end past the deadline, and what stays unexplained says why."""

from datetime import datetime, time, timedelta
from types import SimpleNamespace

import pytest

from core.brief import job
from core.brief.payload import MODEL_BUSY, MODEL_REFUSED
# busy_waits and market_scope_is_valid_by_default are test_brief_job's autouse fixtures, imported to apply here too.
from core.brief.tests.test_brief_job import (EARLY, MARKETS, SAST, Client, FakeChain, FakeConfirm, FakeCtx,  # noqa: F401
                                             FakeModel, all_cards, busy_waits, held_items,
                                             market_scope_is_valid_by_default, payload, world)
from core.detect.tests.fixtures import D

DEADLINE = datetime.combine(D, time(6, 15), SAST)


class Busy(Exception):
    """Vertex's refusal as google-genai raises it: code 429 and RESOURCE_EXHAUSTED in the text."""

    code = 429

    def __init__(self, usd=None):
        super().__init__("429 RESOURCE_EXHAUSTED. Resource exhausted. Please try again later.")
        if usd is not None:
            self.usd = usd


class Refusing(FakeModel):
    """FakeModel that refuses the calls whose 1-based numbers refuse(n) is true for; refused calls are recorded too."""

    def __init__(self, refuse, usd=0.01, error=Busy):
        super().__init__(usd=usd)
        self.refuse = refuse
        self.error = error
        self.refused = 0

    def complete_json(self, **kw):
        n = len(self.calls) + 1
        if self.refuse(n):
            self.calls.append({"user": kw["user"], "support": False, "critic": False, "refused": True})
            self.refused += 1
            raise self.error()
        return super().complete_json(**kw)


class Clock:
    """A clock that the waits move on, so a test sees where each wait would have ended."""

    def __init__(self, now):
        self.now = now
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def run(con, *, model, clock=None, sleep=None):
    client = Client(con)
    chain = FakeChain()
    clock = clock or (lambda: EARLY)
    counts = job.run(client, D, chain=chain, model=model, make_sc=lambda run_id: object(), clock=clock,
                     build_ctx=FakeCtx(), confirm=FakeConfirm(), campaign_hashtags=[],
                     political_terms={m: ["election"] for m in MARKETS}, workers=1, core="core", agent="agent",
                     sleep=sleep)
    return SimpleNamespace(client=client, chain=chain, model=model, counts=counts)


def explained(r):
    return [c for m in MARKETS for c in all_cards(payload(r, m)) if c["explanation_status"] == "explained"]


def test_a_refused_call_is_made_again_after_a_wait_and_every_card_is_explained():
    plain = run(world(n=2), model=FakeModel())
    calls, usd = len(plain.model.calls), plain.counts["model_usd"]
    waits = []
    model = Refusing(lambda n: n in (1, 4))
    r = run(world(n=2), model=model, sleep=waits.append)
    assert len(explained(r)) == len(explained(plain)) == 6
    assert len(model.calls) == calls + 2
    # Each refused call waited once, the first wait of the backoff, and went through on its next try.
    assert len(waits) == 2 and all(job.BUSY_WAIT_S <= w <= job.BUSY_WAIT_S * (1 + job.BUSY_JITTER) for w in waits)
    # A refusal bills nothing: the run books exactly what the calls that went through cost.
    assert r.counts["model_usd"] == pytest.approx(usd)
    assert r.counts["model_busy"] == {"refusals": 2, "gave_up": 0, "waited_s": round(sum(waits), 1),
                                      "tripped": None}
    assert "model" not in r.counts


def test_the_wait_doubles_on_each_refusal_up_to_its_cap(monkeypatch):
    waits = []
    model = Refusing(lambda n: n <= 4)
    r = run(world(n=1, markets=("ZA",)), model=model, sleep=waits.append)
    assert len(explained(r)) == 1
    for w, base in zip(waits, (10, 20, 40, 80)):
        assert base <= w <= base * (1 + job.BUSY_JITTER)
    assert len(waits) == 4

    monkeypatch.setenv("BRIEF_BUSY_WAIT_MAX_S", "25")
    monkeypatch.setenv("BRIEF_BUSY_JITTER", "0")
    waits = []
    run(world(n=1, markets=("ZA",)), model=Refusing(lambda n: n <= 4), sleep=waits.append)
    assert waits == [10, 20, 25, 25]


def test_sustained_refusal_trips_the_breaker_and_every_held_item_says_the_model_was_busy():
    waits = []
    model = Refusing(lambda n: True)
    r = run(world(n=2), model=model, sleep=waits.append)
    tries = job.BUSY_RETRIES + 1
    # Stopped at 05:30, before 06:15, the run waits RESUME_WAIT_S and is stopped the same way once more (W8-DEC-18).
    assert len(model.calls) == 2 * tries * job.BUSY_TRIP_ITEMS
    assert len(waits) == 2 * job.BUSY_RETRIES * job.BUSY_TRIP_ITEMS + 1 and waits.count(job.RESUME_WAIT_S) == 1
    assert r.counts["model"]["reason"] == "model_unavailable"
    assert r.counts["model_busy"]["tripped"] == "refused"
    assert r.counts["model_busy"]["gave_up"] == 2 * job.BUSY_TRIP_ITEMS
    assert r.counts["model_usd"] == 0
    for m in MARKETS:
        p = payload(r, m)
        assert p["cards"] == [] and p["more"] == []
        held = held_items(r, m)
        assert len(held) == 2
        # The gate's hold is unchanged (G10); only the reason it gives is new.
        assert all(i["rule"] == "G10" and i["reason"] == "explanation_failed" for i in held.values())
        assert {i["failed_reason"] for i in held.values()} == {MODEL_REFUSED}


def test_a_call_that_goes_through_resets_the_count_of_explanations_that_ran_out_of_tries():
    tries = job.BUSY_RETRIES + 1
    model = Refusing(lambda n: n <= tries)  # the first explanation's first call runs out of tries, then all is well
    r = run(world(n=2), model=model, sleep=lambda s: None)
    assert "model" not in r.counts
    assert r.counts["model_busy"]["gave_up"] == 1 and r.counts["model_busy"]["tripped"] is None
    assert len(explained(r)) == 5
    za = held_items(r, "ZA")
    assert za["za1"]["failed_reason"] == MODEL_REFUSED and za["za1"]["rule"] == "G10"


def test_no_wait_ends_past_the_deadline_and_what_is_left_says_so():
    clock = Clock(DEADLINE - timedelta(minutes=2))
    model = Refusing(lambda n: True)
    r = run(world(n=2), model=model, clock=clock, sleep=clock.sleep)
    assert clock.slept, "the first waits fit before 06:15"
    assert clock.now < DEADLINE
    # The wait that would have passed 06:15 is not taken: the breaker trips at once instead.
    assert r.counts["model_busy"]["tripped"] == "deadline"
    assert r.counts["model"]["reason"] == "model_unavailable"
    assert len(model.calls) == len(clock.slept) + 1
    for m in MARKETS:
        assert {i["failed_reason"] for i in held_items(r, m).values()} == {MODEL_BUSY}


def test_no_wait_ends_past_the_runs_time_limit():
    by_market = job._candidates(Client(world(n=1)), D, build_ctx=FakeCtx(), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")
    tasks = job._in_rank_order(by_market)
    limit = job.collect_chain.TIMEOUTS["brief"] - job.FINISH_MARGIN
    clock = Clock(EARLY)
    started = EARLY - limit + timedelta(seconds=30)
    spend, busy = {"usd": 0.0}, {}
    results, unavailable, stop = job._explain_all(tasks, model=Refusing(lambda n: True), base_usd=0.0, spend=spend,
                                                  clock=clock, chain=FakeChain(), d=D, workers=1, started=started,
                                                  sleep=clock.sleep, busy=busy)
    assert clock.now - started < limit
    assert len(clock.slept) == 1  # 10 to 12.5 s fits in 30 s, the next 20 to 25 s does not
    assert busy["tripped"] == "deadline" and unavailable
    assert {c.get("busy_reason") for c in tasks} == {MODEL_BUSY}
    assert spend["usd"] == 0.0


def test_no_wait_ends_past_the_recovery_deadline(monkeypatch):
    monkeypatch.setenv("FORCE_RERUN", "1")
    monkeypatch.setenv("RUN_DATE", D.isoformat())
    monkeypatch.setenv("BRIEF_RECOVERY_UNTIL", "07:00")
    until = datetime.combine(D, time(7, 0), SAST)
    by_market = job._candidates(Client(world(n=1)), D, build_ctx=FakeCtx(), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")
    tasks = job._in_rank_order(by_market)
    clock = Clock(until - timedelta(seconds=25))
    busy = {}
    job._explain_all(tasks, model=Refusing(lambda n: True), base_usd=0.0, spend={"usd": 0.0}, clock=clock,
                     chain=FakeChain(), d=D, workers=1, started=clock.now, sleep=clock.sleep, busy=busy)
    assert clock.now < until
    assert len(clock.slept) == 1 and busy["tripped"] == "deadline"


def test_a_deadline_stop_after_waiting_on_the_model_names_the_busy_model():
    # The wait and the calls ate the time: the deadline stops the run before the breaker trips, and the explanation
    # never started says the model was busy.
    clock = Clock(DEADLINE - timedelta(minutes=3))

    class Slow(Refusing):
        def complete_json(self, **kw):
            clock.now += timedelta(seconds=15)
            return super().complete_json(**kw)

    r = run(world(n=3, markets=("ZA",)), model=Slow(lambda n: n == 1), clock=clock, sleep=clock.sleep)
    stop = r.counts["explanation_stop"]
    assert stop["reason"] == "deadline" and stop["skipped"]
    assert "model" not in r.counts
    za = held_items(r, "ZA")
    skipped = {s["item_id"] for s in stop["skipped"]}
    assert skipped and {za[i]["failed_reason"] for i in skipped} == {MODEL_BUSY}
    assert all(c["failed_reason"] is None for c in all_cards(payload(r, "ZA")))


def test_a_slow_run_with_no_refusals_keeps_failed_reason_unset_at_the_deadline():
    model = FakeModel()
    r = run(world(n=3), model=model, clock=lambda: datetime.combine(D, time(6, 20), SAST) if model.writer_calls()
            else EARLY, sleep=lambda s: pytest.fail("no wait without a refusal"))
    assert "model_busy" not in r.counts
    held = held_items(r, "ZA")
    assert held and all(i["failed_reason"] is None for i in held.values())


def test_a_refusal_that_reports_spend_is_not_made_again_and_its_spend_is_booked_once():
    model = Refusing(lambda n: n == 1, error=lambda: Busy(usd=0.02))
    waits = []
    r = run(world(n=1, markets=("ZA",)), model=model, sleep=waits.append)
    assert waits == []
    assert len(model.calls) == 1
    # The refused explanation ends there (it ran out of tries), booking its 0.02 once; nothing else was called for it.
    assert r.counts["model_usd"] == pytest.approx(0.02)
    assert r.counts["model_busy"]["gave_up"] == 1
    assert held_items(r, "ZA")["za1"]["failed_reason"] == MODEL_REFUSED


def test_settings_come_from_the_environment_and_bad_values_keep_the_defaults(monkeypatch):
    monkeypatch.setenv("BRIEF_BUSY_RETRIES", "1")
    monkeypatch.setenv("BRIEF_BUSY_TRIP_ITEMS", "1")
    model = Refusing(lambda n: True)
    r = run(world(n=1), model=model, sleep=lambda s: None)
    assert len(model.calls) == 4 and r.counts["model_busy"]["tripped"] == "refused"  # two passes of two
    for name, value in (("BUSY_RETRIES", "-1"), ("BUSY_RETRIES", "many"), ("BUSY_WAIT_S", "nan"),
                        ("BUSY_TRIP_ITEMS", "0"), ("PACE_S", "-2")):
        monkeypatch.setenv(f"BRIEF_{name}", value)
        assert job._setting(name, getattr(job, name), 1 if name == "BUSY_TRIP_ITEMS" else 0) == getattr(job, name)


def test_a_pace_gap_goes_before_each_explanation_after_the_first(monkeypatch):
    monkeypatch.setenv("BRIEF_PACE_S", "2")
    waits = []
    r = run(world(n=2), model=FakeModel(), sleep=waits.append)
    assert len(explained(r)) == 6
    assert waits == [2.0] * 5
