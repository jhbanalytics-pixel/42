"""The age the desk sends never crosses the amber edge before its status does.

The shell reads its stale state back out of age_hours from 3 hours, the same
edge refresh_freshness uses for status. Rounding the sent age to two decimals
turned 2.996 hours into 3.0 while the status, decided on the unrounded age,
was still green, so the shell called the check stale up to 18 seconds early.
The sent age is cut to two decimals instead, so it reaches 3.0 only when the
desk says amber.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.api import bq


def _stamp(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


def _agrees(out: dict) -> bool:
    return (out["age_hours"] >= bq.FRESHNESS_AMBER_HOURS) == (out["status"] == "amber")


def test_just_under_three_hours_is_sent_under_three_and_green():
    out = bq.refresh_freshness({"stamp_utc": _stamp(2.996)})
    assert out["status"] == "green"
    assert out["age_hours"] == 2.99
    assert _agrees(out)


def test_the_sent_age_and_the_status_agree_on_both_sides_of_the_edge():
    for hours in (2.9, 2.99, 2.994, 2.995, 2.996, 2.9999, 3.0001, 3.004, 3.5):
        out = bq.refresh_freshness({"stamp_utc": _stamp(hours)})
        assert _agrees(out), (hours, out)


def test_the_sent_age_is_never_ahead_of_the_clock():
    out = bq.refresh_freshness({"stamp_utc": _stamp(6.5)})
    assert 6.49 <= out["age_hours"] <= 6.5


class _FixedClock(datetime):
    NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.NOW


def _at(monkeypatch, hours_ago: float) -> dict:
    monkeypatch.setattr(bq, "datetime", _FixedClock)
    stamp = (_FixedClock.NOW - timedelta(hours=hours_ago)).isoformat()
    return bq.refresh_freshness({"stamp_utc": stamp})


def test_an_exact_hundredth_is_sent_as_itself(monkeypatch):
    # 1.15 * 100 is 114.99999999999999 in binary floating point, so a bare
    # floor sent exactly 1.15 hours as 1.14.
    assert _at(monkeypatch, 1.15)["age_hours"] == 1.15


def test_the_edge_on_a_fixed_clock(monkeypatch):
    out = _at(monkeypatch, 2.996)
    assert (out["age_hours"], out["status"]) == (2.99, "green")
    out = _at(monkeypatch, 3.0)
    assert (out["age_hours"], out["status"]) == (3.0, "amber")
