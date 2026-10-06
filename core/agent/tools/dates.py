"""resolve_dates: the server turns a date expression into an inclusive window. The model never computes dates."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from core.agent.context import Refused

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    SAST = ZoneInfo("Africa/Johannesburg")
except (ImportError, ZoneInfoNotFoundError):
    # Windows without the tzdata package has no IANA database. SAST has no daylight saving, so +02:00 is exact.
    SAST = timezone(timedelta(hours=2), "SAST")

_ISO = r"(\d{4}-\d{2}-\d{2})"


def _refuse(expression: str, why: str) -> Refused:
    return Refused(f"cannot resolve dates: {expression!r} ({why})")


def _iso(text: str, expression: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise _refuse(expression, f"{text} is not a valid date") from None


def resolve_dates(expression: str, as_of: datetime) -> tuple[date, date]:
    """Return (from_date, to_date), both inclusive, in Africa/Johannesburg local time."""
    local = as_of.astimezone(SAST) if as_of.tzinfo else as_of.replace(tzinfo=SAST)
    today = local.date()
    text = " ".join((expression or "").lower().split())

    def window(days: int) -> tuple[date, date]:
        return today - timedelta(days=days - 1), today

    try:
        start, end = _match(text, expression, local, today, window)
    except OverflowError:
        raise _refuse(expression, "the window reaches past the calendar") from None
    if start > end:
        raise _refuse(expression, "the start is after the end")
    if end > today:
        raise _refuse(expression, f"the window ends after as_of ({today.isoformat()})")
    return start, end


def _match(text, expression, local, today, window):
    if text == "today":
        result = (today, today)
    elif text == "yesterday":
        result = (today - timedelta(days=1),) * 2
    elif text in ("this week", "last week"):
        result = window(7)
    elif text == "last month":
        result = window(30)
    elif text == "this month":
        result = (today.replace(day=1), today)
    elif m := re.fullmatch(r"(?:last|past) (\d+) days?|the (\d+) days? before as_of", text):
        n = int(m.group(1) or m.group(2))
        if n < 1:
            raise _refuse(expression, "the number of days must be at least 1")
        result = window(n)
    elif m := re.fullmatch(r"(?:last|past) (\d+) hours?", text):
        n = int(m.group(1))
        if n < 1:
            raise _refuse(expression, "the number of hours must be at least 1")
        result = ((local - timedelta(hours=n)).date(), today)
    elif m := re.fullmatch(rf"since {_ISO}", text):
        result = (_iso(m.group(1), expression), today)
    elif m := re.fullmatch(rf"{_ISO} to {_ISO}", text):
        result = (_iso(m.group(1), expression), _iso(m.group(2), expression))
    else:
        raise _refuse(expression, "supported: today, yesterday, this week, last week, last N days, last N hours, "
                                  "last month, this month, since YYYY-MM-DD, YYYY-MM-DD to YYYY-MM-DD")
    return result
