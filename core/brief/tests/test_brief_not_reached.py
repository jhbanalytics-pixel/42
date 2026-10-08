"""An item the explanation loop never reached is held with its own reason (core/brief/job.py _market_payload), not
as an explanation that failed its checks. The hold itself is unchanged: G10, held_back, reason explanation_failed."""

import json

import pytest

from core.brief import job
from core.brief.payload import MODEL_BUSY, MODEL_REFUSED
from core.brief.tests.test_brief_job import (CAP_AFTER_EXPIRY, D, EARLY, LATE, FakeModel, RateLimited, brief, duck,
                                             held_items, payload, pin_brief_model_cap, rate_limit_errors, world)
from core.detect.tests.fixtures import run as run_row

FAILED = "Explanation failed its checks"
NOT_REACHED_TEXT = "Not explained: the run stopped before this topic was reached"


def test_items_the_model_cap_stopped_before_are_held_as_not_reached(monkeypatch):
    pin_brief_model_cap(monkeypatch, CAP_AFTER_EXPIRY)
    con = world(n=3)
    duck.load(con, "agent.runs", [{**run_row("brief", D, run_id="brief-earlier", status="failed"),
                                   "counts": json.dumps({"model_usd": 19.4})}])
    r = brief(con, model=FakeModel(usd=0.1), workers=1)
    za = payload(r, "ZA")
    assert [c["item_id"] for c in za["cards"]] == ["za1"]
    held = held_items(r, "ZA")
    assert set(held) == {"za2", "za3"} and za["held_back"]["count"] == 2
    for item in held.values():
        assert item["reason_text"] == NOT_REACHED_TEXT and item["reason_text"] != FAILED
        assert item["rule"] == "G10" and item["reason"] == "explanation_failed"
        assert item["failed_reason"] is None
    assert za["held_back"]["text"] == "2 held back: not explained before the run stopped"


def test_items_a_slow_run_never_reached_at_the_deadline_are_held_as_not_reached():
    model = FakeModel()
    r = brief(world(n=3), model=model, workers=1,
              clock=lambda: LATE if any(c.get("critic") for c in model.calls) else EARLY)
    held = held_items(r, "ZA")
    assert set(held) == {"za2", "za3"}
    assert {i["reason_text"] for i in held.values()} == {NOT_REACHED_TEXT}
    assert {i["reason"] for i in held.values()} == {"explanation_failed"}


def test_an_item_whose_explanation_ran_and_failed_still_reads_as_failed_checks():
    r = brief(world(n=1), model=FakeModel(ruled_out=False))
    held = held_items(r, "ZA")["za1"]
    assert held["reason_text"] == FAILED and held["reason"] == "explanation_failed"


@pytest.fixture(autouse=True)
def no_busy_wait(monkeypatch):
    monkeypatch.setattr(job, "_sleep", lambda seconds: None)


def test_an_item_a_busy_model_never_started_keeps_the_generic_wording_and_its_busy_reason():
    # A tripped breaker leaves the rest of the topics unstarted, with no result. They carry busy_reason, so the item
    # reads as failed checks with the busy wording as its failed_reason, never as "the run stopped before this topic".
    r = brief(world(n=4), model=RateLimited(rate_limit_errors()[0]))
    items = [i for m in ("ZA", "NG", "KE") for i in held_items(r, m).values()]
    assert len(items) == 12
    assert {i["reason_text"] for i in items} == {FAILED}
    assert {i["failed_reason"] for i in items} <= {MODEL_BUSY, MODEL_REFUSED}
    assert {i["failed_reason"] for i in items}  # at least one busy wording is shown
    assert {i["reason"] for i in items} == {"explanation_failed"} and {i["rule"] for i in items} == {"G10"}
