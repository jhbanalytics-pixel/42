from datetime import date, datetime

import pytest

from core.agent.context import Refused
from core.agent.tools.dates import resolve_dates

AS_OF = datetime.fromisoformat("2026-09-28T06:10:00+02:00")
D = date


@pytest.mark.parametrize("expression,expected", [
    ("today", (D(2026, 9, 28), D(2026, 9, 28))),
    ("Today ", (D(2026, 9, 28), D(2026, 9, 28))),
    ("yesterday", (D(2026, 9, 27), D(2026, 9, 27))),
    ("this week", (D(2026, 9, 22), D(2026, 9, 28))),
    ("last 7 days", (D(2026, 9, 22), D(2026, 9, 28))),
    ("past 14 days", (D(2026, 9, 15), D(2026, 9, 28))),
    ("last 1 day", (D(2026, 9, 28), D(2026, 9, 28))),
    ("the 3 days before as_of", (D(2026, 9, 26), D(2026, 9, 28))),
    ("last week", (D(2026, 9, 22), D(2026, 9, 28))),
    ("last month", (D(2026, 8, 30), D(2026, 9, 28))),
    ("this month", (D(2026, 9, 1), D(2026, 9, 28))),
    ("since 2026-09-01", (D(2026, 9, 1), D(2026, 9, 28))),
    ("2026-08-01 to 2026-08-31", (D(2026, 8, 1), D(2026, 8, 31))),
    ("last 5 hours", (D(2026, 9, 28), D(2026, 9, 28))),
    ("last 6 hours", (D(2026, 9, 28), D(2026, 9, 28))),
    ("last 7 hours", (D(2026, 9, 27), D(2026, 9, 28))),
    ("last 48 hours", (D(2026, 9, 26), D(2026, 9, 28))),
])
def test_expressions(expression, expected):
    assert resolve_dates(expression, AS_OF) == expected


def test_day_boundary_is_johannesburg_not_utc():
    # 23:30 UTC on the 27th is 01:30 SAST on the 28th.
    as_of = datetime.fromisoformat("2026-09-27T23:30:00+00:00")
    assert resolve_dates("today", as_of) == (D(2026, 9, 28), D(2026, 9, 28))
    assert resolve_dates("yesterday", as_of) == (D(2026, 9, 27), D(2026, 9, 27))


def test_lagos_as_of_converted_to_johannesburg():
    # 23:30 WAT (UTC+1) on the 27th is 00:30 SAST on the 28th.
    as_of = datetime.fromisoformat("2026-09-27T23:30:00+01:00")
    assert resolve_dates("today", as_of) == (D(2026, 9, 28), D(2026, 9, 28))


@pytest.mark.parametrize("expression", [
    "",
    "recently",
    "a while ago",
    "last 0 days",
    "since yesterday",
    "since 2026-13-01",
    "2026-09-10 to 2026-09-01",
    "since 2026-10-01",
    "2026-09-01 to 2026-10-05",
    "next week",
])
def test_unresolvable_raises_refused(expression):
    with pytest.raises(Refused, match="cannot resolve dates"):
        resolve_dates(expression, AS_OF)
