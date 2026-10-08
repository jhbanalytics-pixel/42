"""Read the full-window evidence used to choose market or global Today scope."""

from collections.abc import Mapping, Sequence
from datetime import datetime, time, timedelta, timezone
from numbers import Integral
from pathlib import Path
import re

from core.brief.evidence import OFFSETS, WINDOW_DAYS
from core.detect import sqlrun
from core.detect.sqlrun import AGENT, CORE

SQL = Path(__file__).parent / "sql" / "market_scope.sql"
_NAME = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.MULTILINE)
QUERIES = {_NAME.search(stmt).group(1): stmt for stmt in sqlrun.split(SQL.read_text(encoding="utf-8"))}


def read_market_scope(client, row, d, market, *, core=CORE, agent=AGENT):
    """Return the share of distinct eligible item posts located in or sourced from market."""
    tz = timezone(timedelta(hours=OFFSETS[market]))
    start = datetime.combine(d - timedelta(days=WINDOW_DAYS - 1), time(), tz)
    end = start + timedelta(days=WINDOW_DAYS)
    rows = sqlrun.query(client, QUERIES["market_scope"],
                        {"item_id": row["item_id"], "market": market, "d": d, "start": start, "end": end},
                        core=core, agent=agent)
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or len(rows) != 1:
        raise ValueError("market scope query must return exactly one row")
    counts = rows[0]
    if not isinstance(counts, Mapping):
        raise ValueError("market scope query row must contain count fields")
    if "total_posts7" not in counts or "market_posts7" not in counts:
        raise ValueError("market scope query row is missing required count fields")
    total_count, market_count = counts["total_posts7"], counts["market_posts7"]
    if any(not isinstance(value, Integral) or isinstance(value, bool) for value in (total_count, market_count)):
        raise ValueError("market scope query counts must be finite non-boolean integers")
    total_posts7, market_posts7 = int(total_count), int(market_count)
    if total_posts7 < 0 or market_posts7 < 0 or market_posts7 > total_posts7:
        raise ValueError("market scope query counts must be between zero and total")
    market_share7 = market_posts7 / total_posts7 if total_posts7 else None
    if "news_posts7" not in counts:
        raise ValueError("market scope query row is missing required count fields")
    news_count = counts["news_posts7"]
    if not isinstance(news_count, Integral) or isinstance(news_count, bool):
        raise ValueError("market scope query counts must be finite non-boolean integers")
    news_posts7 = int(news_count)
    if news_posts7 < 0 or news_posts7 > total_posts7:
        raise ValueError("market scope query counts must be between zero and total")
    result = {"market_scope": "market" if market_share7 is not None and market_share7 > 0.5 else "global",
              "market_posts7": market_posts7, "total_posts7": total_posts7, "market_share7": market_share7}
    # News public-feed posts count toward scope as feed evidence and never alone (W8-DEC-12): a set that is all
    # news is Market unconfirmed. Detect's own not_local verdict is not softened.
    if total_posts7 and news_posts7 == total_posts7 and row.get("geo_status") != "not_local":
        result["geo_status"] = "market_unconfirmed"
    return result
