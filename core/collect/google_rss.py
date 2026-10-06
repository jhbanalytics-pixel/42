"""Google's free daily trending searches feed for ZA, NG and KE (https://trends.google.com/trending/rss?geo=ZA),
read once a run at the start of collect. Albert said yes to adding it on 4 October 2026.

Each market is one anonymous HTTPS GET through the public feed reader's transport
(public_feed_collect.https_transport: a total deadline, the 2 MiB body limit, no redirects, trust_env off), after
one robots.txt read for trends.google.com through the same reader's robots check. A body with a document type
or entity declaration is refused unparsed. Nothing here charges SocialCrawl credits, and a failure is recorded
as that market's state and never raised.

The rows go to google_search_signals as source google_rss, kind daily, rank the item's place in the feed, and the
item's approximate traffic and first three news headlines kept in raw_payload. They are Google search interest
only (RULES.md rule 2): the terms may steer which queries row 14 searches (google_trends.queue_seeds), which is
query triage, and they are never post evidence, location proof, why-now evidence or Today support.
"""

from __future__ import annotations

import email.utils
import re
from datetime import date, datetime, timezone
from types import SimpleNamespace
from xml.etree import ElementTree

from core.collect.google_trends import MARKETS, SearchSignal, SearchSignalBatch, SourceState, _utc, _utc_text
from core.public_feeds import reader

URL = "https://trends.google.com/trending/rss?geo={}"
SOURCE = "google_rss"
KIND = "daily"
HT = "{https://trends.google.com/trending/rss}"
MAX_NEWS = 3
DECLARATION = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def _published(text, fetched_at):
    try:
        moment = email.utils.parsedate_to_datetime(text.strip())
    except (TypeError, ValueError, AttributeError):
        return _utc_text(fetched_at)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return _utc_text(moment.astimezone(timezone.utc))


def parse(body, market, fetched_at):
    """(signals, state) for one market's feed body, in feed order."""
    fetched_at = _utc(fetched_at)
    if not isinstance(body, bytes) or len(body) > reader.MAX_RESPONSE_BYTES:
        return [], SourceState("unavailable", "body_over_limit")
    if DECLARATION.search(body):
        return [], SourceState("unavailable", "doctype_refused")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return [], SourceState("unavailable", "malformed_feed")
    signals = []
    for item in root.iter("item"):
        term = (item.findtext("title") or "").strip()
        if not term:
            continue
        news = []
        for block in item.findall(f"{HT}news_item")[:MAX_NEWS]:
            news.append({"title": (block.findtext(f"{HT}news_item_title") or "").strip(),
                         "url": (block.findtext(f"{HT}news_item_url") or "").strip(),
                         "source": (block.findtext(f"{HT}news_item_source") or "").strip()})
        pub_date = (item.findtext("pubDate") or "").strip()
        signals.append(SearchSignal(
            market=market, term=term, source=SOURCE, kind=KIND, rank=len(signals) + 1, fetched_at=fetched_at,
            refreshed_at=_published(pub_date, fetched_at), refresh_date=None,
            raw_payload={"geo": market, "approx_traffic": (item.findtext(f"{HT}approx_traffic") or "").strip(),
                         "pub_date": pub_date, "news": news}))
    if not signals:
        return [], SourceState("empty", "no_items")
    return signals, SourceState("ok")


def read_signals(transport, *, run_id, run_date, clock):
    """One read a market through transport(url, timeout=, max_bytes=), after robots.txt allows it. Never raises
    for a failed read: the market's state says why it has no rows."""
    from core.collect.public_feed_collect import _Robots

    if not isinstance(run_date, date) or isinstance(run_date, datetime):
        raise ValueError("run_date must be a date")
    robots = _Robots()
    signals, states = [], {}
    for market in MARKETS:
        url = URL.format(market)
        target = SimpleNamespace(url=url)
        if not robots.allowed(target, transport):
            states[market, SOURCE] = SourceState("not_made", robots.reason(target))
            continue
        fetched_at = clock()
        try:
            status, _, body = transport(url, timeout=reader.TIMEOUT_SECONDS, max_bytes=reader.MAX_RESPONSE_BYTES)
        except Exception as exc:
            states[market, SOURCE] = SourceState("unknown", f"transport_{type(exc).__name__}")
            continue
        if status != 200:
            states[market, SOURCE] = SourceState("unavailable", f"http_{status}")
            continue
        found, states[market, SOURCE] = parse(body, market, fetched_at)
        signals.extend(found)
    return SearchSignalBatch(run_id, run_date, tuple(signals), states)
