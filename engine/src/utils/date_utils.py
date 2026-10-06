"""Timezone-aware date utilities for SSA markets."""

from datetime import UTC, datetime, timedelta, timezone

from dateutil import parser as dateutil_parser

# Market timezones
TIMEZONES = {
    "za": timezone(timedelta(hours=2)),  # SAST (UTC+2)
    "ng": timezone(timedelta(hours=1)),  # WAT (UTC+1)
    "ke": timezone(timedelta(hours=3)),  # EAT (UTC+3)
}


def parse_timestamp(value: str | datetime | None) -> datetime | None:
    """Parse a timestamp string to UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        dt = dateutil_parser.parse(str(value))
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def now_utc() -> datetime:
    """Current time in UTC."""
    return datetime.now(UTC)


def now_market(market: str) -> datetime:
    """Current time in a market's local timezone."""
    tz = TIMEZONES.get(market, TIMEZONES["za"])
    return datetime.now(tz)


def is_within_window(dt: datetime | None, days: int = 14) -> bool:
    """Check if a datetime is within the last N days."""
    if dt is None:
        return False
    cutoff = now_utc() - timedelta(days=days)
    dt_utc = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return dt_utc >= cutoff
