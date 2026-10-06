"""Binds lane L1's core/collect/socialcrawl_client.py to the agent's SocialCrawlClient protocol.

L1's client checks the share's cap and then calls with no lock, so two asks in flight at once could each pass the
cap check and overshoot it by one quote. Every adapter in the process takes the one module-level LOCK around the
client call, so Ask has one live call in flight at a time on the ask share.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone

from core.agent.context import Refused
from core.agent.tools.socialcrawl import MARKET_PARAMS, MARKETS

LOCK = threading.Lock()
SHARE = "ask"
L1_MODULES = ("core.collect", "core.collect.socialcrawl_client", "core.collect.stores")

# L1 Result.status -> agent status. ok and cached with no items become schema_drift, or empty when L1's own test
# finds a cached body empty; forbidden raises Refused.
STATUS = {
    "ok": "ok",
    "cached": "ok",
    "empty": "empty",
    "not_in_replay": "not_in_replay",
    "cap_reached": "cap_reached",
    "balance_floor": "cap_reached",
    "insufficient_credits": "cap_reached",
    "refunded": "error",
    "error": "error",
}
HTTP_STATUS = {401: "auth_failed", 403: "auth_failed", 429: "rate_limited"}
_URL = re.compile(r"\S*://\S*|\bwww\.\S*", re.IGNORECASE)


def _market(params: dict) -> str | None:
    for key in MARKET_PARAMS:
        value = params.get(key)
        if isinstance(value, str) and value.strip().casefold() in MARKETS:
            return MARKETS[value.strip().casefold()]
    return None


def _next_cursor(body) -> str | None:
    """pagination.next_cursor at the top of the body or under data (SOURCES.md), unless has_more is false."""
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    for page in (body.get("pagination"), data.get("pagination") if isinstance(data, dict) else None):
        if isinstance(page, dict) and page.get("next_cursor") and page.get("has_more") is not False:
            return str(page["next_cursor"])
    return None


def _transcript_rows(route: str, body) -> list | None:
    """data.transcript on a transcript route (the YouTube transcript's shape), or None. L1's ITEM_KEYS has no
    "transcript", and adding it there would change collect-wide parsing, so only the agent path reads it."""
    if not route.rstrip("/").endswith("/transcript") or not isinstance(body, dict):
        return None
    data = body.get("data")
    rows = data.get("transcript") if isinstance(data, dict) else None
    return list(rows) if isinstance(rows, list) else None


class L1Adapter:
    def __init__(self, client, *, lock: threading.Lock):
        self.client = client
        self.lock = lock

    def quote(self, route: str, params: dict) -> float:
        from core.collect.socialcrawl_client import Refused as L1Refused
        from core.collect.socialcrawl_client import quote_for

        try:
            return float(quote_for(route, "GET", dict(params)))
        except L1Refused as e:
            raise Refused(f"{route}: {e}") from None

    def call(self, route: str, params: dict, *, lane: str, run_id: str, max_credits: float) -> dict:
        quote = self.quote(route, params)
        if quote > max_credits:
            raise Refused(f"{route} costs {quote} credits, over max_credits {max_credits}")
        with self.lock:
            result = self.client.call(route, dict(params), market=_market(params), lane=lane)
        reason = _URL.sub("[url]", result.reason or "")
        if result.status == "forbidden":
            raise Refused(reason or f"{route} is forbidden by the SocialCrawl client")
        items = list(result.items or [])
        status = STATUS.get(result.status, "error")
        transcript = _transcript_rows(route, result.body) if status == "ok" and not items else None
        if transcript:
            items = transcript
        elif transcript is not None:
            status = "empty"  # data.transcript arrived as an empty list: no speech, not drift
        elif status == "ok" and not items:
            # L1 says ok only when data had content, so no items means ITEM_KEYS found no list: drift, not silence.
            # A cached body is judged by L1's own emptiness test, since an empty response is cached too.
            from core.collect.socialcrawl_client import _is_empty

            if result.status == "cached" and _is_empty(result.body):
                status = "empty"
            else:
                status, reason = "schema_drift", reason or "the response had data but no item list in a known shape"
        elif result.status == "error":
            status = HTTP_STATUS.get(result.http_status, "error")
        return {
            "items": items,
            "next_cursor": _next_cursor(result.body),
            "credits_charged": float(result.credits_charged or 0),
            "status": status,
            "cache_hit": bool(result.cache_hit),
            "reason": reason,
        }


def make_client(mode: str, *, run_id: str) -> L1Adapter | None:
    """L1's client on the ask share with its BigQuery stores, or None until core/collect is on this branch."""
    try:
        from core.collect.socialcrawl_client import SocialCrawlClient, requests_http
        from core.collect.stores import BigQueryLedgerStore, BigQueryRawStore
    except ModuleNotFoundError as e:
        if e.name not in L1_MODULES:
            raise
        return None
    from google.cloud import bigquery

    from core.agent.tools.sql_query import PROJECT

    bq = bigquery.Client(project=PROJECT)
    client = SocialCrawlClient(
        share=SHARE, run_id=run_id, mode=mode, ledger=BigQueryLedgerStore(bq, PROJECT),
        raw=BigQueryRawStore(bq, PROJECT), http=requests_http, clock=lambda: datetime.now(timezone.utc),
    )
    return L1Adapter(client, lock=LOCK)
