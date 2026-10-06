"""The spike object an ask may carry (core/api/contract.md sections 6 and 14.1), checked before it is stored."""
import re
from datetime import date

KEYS = ("item_id", "market", "date", "series")
ITEM_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
SERIES_RE = re.compile(r"[A-Za-z0-9_]{1,80}")


def _real_date(value):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def check_spike(value):
    """A copy of value holding only the spike keys, or ValueError with a plain message."""
    if not isinstance(value, dict):
        raise ValueError("spike must be an object or null.")
    extra = sorted(str(k)[:40] for k in value if k not in KEYS)[:5]
    if extra:
        raise ValueError(f"spike takes only item_id, market, date and series, not {', '.join(extra)}.")
    if any(k not in value for k in KEYS[:3]):
        raise ValueError("spike needs item_id, market and date.")
    if not isinstance(value["item_id"], str) or not ITEM_ID_RE.fullmatch(value["item_id"]):
        raise ValueError("spike item_id must be an item id.")
    if value["market"] not in ("ZA", "NG", "KE"):
        raise ValueError("spike market must be ZA, NG or KE.")
    if not _real_date(value["date"]):
        raise ValueError("spike date must be a real date written YYYY-MM-DD.")
    series = value.get("series")
    if series is not None and not (isinstance(series, str) and SERIES_RE.fullmatch(series)):
        raise ValueError("spike series must be 1 to 80 letters, digits or _, or left out.")
    return {k: value[k] for k in KEYS if k in value}
