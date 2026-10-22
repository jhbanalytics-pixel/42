"""Audit F7D-ASK-004: the runs row takes the SAST day run_ask's cap decision held the spend against, not the day the
Ask was created, so an Ask created before SAST midnight and admitted after it is counted on the day its hold was
taken and the next Ask that day still sees it once the hold is released."""

from datetime import date

from core.agent import ask
from core.api import agent_app


def test_the_runs_row_is_dated_by_the_cap_decisions_day():
    with ask._SPEND_LOCK:
        ask._UNRECORDED["a_late"] = (date(2026, 10, 22), 2.0)
    try:
        assert agent_app.spend_day("a_late", "2026-10-21T23:59:59+02:00") == "2026-10-22"
    finally:
        ask.recorded("a_late")


def test_an_ask_with_no_held_spend_counts_on_the_sast_day_it_started():
    assert agent_app.spend_day("a_none", "2026-10-21T23:59:59+02:00") == "2026-10-21"
    assert agent_app.spend_day("a_none", "2026-10-21T22:30:00+00:00") == "2026-10-22"
