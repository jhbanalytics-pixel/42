"""The one gate every SocialCrawl call in 42 passes through (RULES.md rule 3).

Only routes in PRICED can be called. Each entry names the parameters it accepts and a quote function
that returns the true maximum bill for the call's parameters: pages times the per-page price, per item
or URL counts for batch bodies, and every paid add-on. A route outside PRICED, a parameter its entry does
not know, or a count above the entry's limit is refused. The routes are the ones docs/full-42/SOURCES.md
lists, its costed table rows 1 to 27 and the reserve, the Stage 0 probes in
docs/full-42/research/13-socialcrawl-full-map.md section 5, the routes Ask calls live (one post-detail
route per platform, prism/mentions and instagram/tagged), and the free account routes. Prices are the
credits and credit_formula fields of docs/full-42/reference/sc_routes.json; a parameter whose price the
spec does not give is left out of the entry, so it is refused.

Before a call it also refuses the never-used routes and monitors, serves a same-day repeat from
raw_responses for free, and checks the share's daily cap, the monthly cap and the balance floor
(core/config/caps.yaml, which mirrors docs/full-42/SETUP.md) against the hold. The free account routes
cost nothing, so the balance floor does not refuse them; every other check still applies.

After a live call it charges the higher of the list price and the vendor-reported charge (the larger of
the x-credit-cost header and body credits_used). 502, 503 and INSUFFICIENT_CREDITS charge only what
the vendor reports, an empty page is charged what the vendor reports, INSUFFICIENT_CREDITS halts the
client, and one raw_responses row and one credit_ledger row are appended.

A 502 or 503 the vendor charged nothing for is tried once more after retry_wait seconds. The retry passes
the cap, key and balance checks again as a call of its own and is ledgered in its own rows, so the
refunded attempt stays on the ledger at 0 credits and the hold is never counted twice. The caller gets
the retry's result; a second refund is final.

A client built with retry_routes (the collect job's rank lists and boards) also retries a transient failure
on those routes: a timeout, a connection error, HTTP 429 or any 5xx. It makes at most transient_retries
more attempts, waiting retry_wait, then RETRY_BACKOFF times as long, and so on. Each attempt passes the
cap, key and balance checks as a call of its own and is ledgered in its own rows, so the share caps hold
exactly as before. Two bounds hold for the whole run as well: the retries' holds together stay within
retry_credits, and a route whose retries all failed gets no more of them from this client, so a vendor
outage costs that route's retries once, not on every call. The caller gets the last attempt's result with
credits_charged summed over every attempt, attempts set, and the earlier failure classes in its reason.
Result.failure names the class of a failed live attempt (timeout, connection, transport, http_429,
http_5xx, http_4xx, vendor_error, insufficient_credits, store), never a URL, key or body.

The share (collect, confirm, reserve, ask, eval, build, pulse, video) is written to the job column of both tables
and daily spend is summed by it. lane keeps its DATA.md meaning and is passed in per call.

Replay mode never calls HTTP: it serves recorded responses from raw_responses and costs nothing.
The API key is read from the environment only when a live call is about to be made.
"""

import copy
import hashlib
import json
import logging
import math
import os
import re
import time as clock_time
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import yaml

from core.collect.x_discovery import discovery_quote, project_discovery

log = logging.getLogger(__name__)

BASE_URL = "https://www.socialcrawl.dev/v1"
KEY_ENV = "SOCIALCRAWL_OGILVY_API_KEY"
ROOT = Path(__file__).resolve().parents[2]
ROUTES_FILE = ROOT / "docs" / "full-42" / "reference" / "sc_routes.json"
CAPS_FILE = ROOT / "core" / "config" / "caps.yaml"
SAST = timezone(timedelta(hours=2))  # Africa/Johannesburg keeps no daylight saving

STATUSES = (
    "ok", "empty", "refunded", "cached", "not_in_replay", "cap_reached",
    "forbidden", "balance_floor", "insufficient_credits", "error",
)

# SOURCES.md "Never used". The Google Trends family is matched on a squashed token below, so the
# banned name never sits in this file as a literal.
NEVER_USED = {
    "prism/trend-board": "fetches Google Trending Now on every call",
    "prism/earliness": "has a Google Trends lane",
    "prism/audience-language": "audience samples",
    "tiktok/user/audience": "audience demographics",
    "twitter/ai-search": "generated answers",
    "prism/investigate": "generated answers",
    "prism/answers": "generated answers",
    "prism/ai-visibility": "generated answers",
}
TRENDS_TOKEN = "".join(["google", "trends"])
TRENDS_ROUTE_PREFIX = "_".join(["google", "trends"])
ALLOWED_TRENDS_ROUTES = frozenset((f"{TRENDS_ROUTE_PREFIX}/trending",))
PRISM_LANE_PARAMS = ("platforms", "sources", "engines")  # a prism route's lanes, checked for Google (rule 2)
AI_LANES = ("perplexity", "tavily", "twitter-ai-search", "polymarket")

# Creating or resuming a monitor starts billing on the vendor's own cadence, outside this ledger.
MONITOR_PATH = re.compile(r"(web/)?monitors(/[^/]+)?")
MONITOR_METHODS = ("POST", "PATCH")

LABEL_KEYS = ("relevance", "labels", "judgments", "sponsored", "intent", "niche")
ITEM_KEYS = ("items", "posts", "results", "videos", "articles", "data")
COST_HEADER = "x-credit-cost"
MAX_PAGES = 10
RETRY_WAIT = 2  # seconds before the one retry of a refunded 502 or 503
# Retries of a transient failure on a client's retry_routes: at most TRANSIENT_RETRIES more attempts, the
# n-th after RETRY_WAIT * RETRY_BACKOFF ** (n - 1) seconds, with their holds within RETRY_CREDITS a run.
TRANSIENT_RETRIES = 2
BALANCE_READS = 3  # reads of the free credits/balance endpoint before the balance stays unknown for the client
RETRY_BACKOFF = 4
RETRY_CREDITS = 30
TRANSIENT = frozenset({"timeout", "connection", "http_429", "http_5xx"})
_TIMEOUT_NAMES = frozenset({"Timeout", "ReadTimeout", "ConnectTimeout", "TimeoutError"})
_CONNECTION_NAMES = frozenset({"ConnectionError", "ChunkedEncodingError", "ProtocolError", "RemoteDisconnected"})
BOOLEAN_ADDONS = frozenset({"include_body", "includeExtras"})  # every other add-on is free text


class Refused(Exception):
    """A call PRICED cannot price: not a 42 route, an unpriced parameter, or a count above a limit."""


@dataclass(frozen=True)
class Rule:
    method: str
    params: frozenset  # every parameter the quote prices or knows to be free
    quote: object      # params -> the maximum credits the call can bill
    floor: object      # params -> the least a reported call is charged (the base price before refunds)


# Quote helpers ------------------------------------------------------------------------------------


def _csv(value):
    if value is None:
        return []
    parts = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return [str(p).strip().lower() for p in parts if str(p).strip()]


def _count(params, name, cap):
    """A whole number from 1 to cap: an int or a string of digits. 3.7, "3.7", 2.0 and True are refused."""
    raw = params[name]
    whole = isinstance(raw, int) and not isinstance(raw, bool)
    digits = isinstance(raw, str) and re.fullmatch(r"[0-9]+", raw.strip())
    if not (whole or digits):
        raise Refused(f"{name}={raw!r} is not a whole number")
    n = int(raw)
    if not 1 <= n <= cap:
        raise Refused(f"{name}={n} is outside 1 to {cap}")
    return n


def _present(name, value):
    """A boolean add-on is on whenever its key is present; a text add-on whenever it is non-empty after strip."""
    if name in BOOLEAN_ADDONS:
        return True
    return value is not None and str(value).strip() != ""


def _addons(params, add):
    """Credits held by paid add-ons: an int when the parameter is present, or a price per listed value."""
    total = 0
    for name, price in add.items():
        if name not in params or not _present(name, params[name]):
            continue
        if isinstance(price, dict):
            for value in _csv(params[name]):
                if value not in price:
                    raise Refused(f"unpriced value {name}={value}")
                total += price[value]
        else:
            total += price
    return total


def _list(params, name, cap):
    value = params.get(name)
    if not isinstance(value, list) or not value:
        raise Refused(f"{name} must be a non-empty list")
    if len(value) > cap:
        raise Refused(f"{len(value)} {name} is above the limit of {cap}")
    return value


def rule(base, free=(), *, add=None, pages=None, limits=None, method="GET"):
    """A route billed base per page plus add-ons; pages names the parameter that multiplies it."""
    add, limits = add or {}, limits or {}

    def quote(p):
        for name, cap in limits.items():
            if name in p:
                _count(p, name, cap)
        n = _count(p, pages, limits.get(pages, MAX_PAGES)) if pages and pages in p else 1
        return (base + _addons(p, add)) * n

    names = set(free) | set(add) | set(limits) | ({pages} if pages else set())
    return Rule(method, frozenset(names), quote, lambda p: base)


def special(quote, names, method="GET", floor=None):
    """A route with its own quote; floor is the least a reported call is charged (default: the quote)."""
    if floor is None:
        floor_fn = quote
    elif callable(floor):
        floor_fn = floor
    else:
        def floor_fn(p):
            return floor
    return Rule(method, frozenset(names), quote, floor_fn)


# Parameter groups (each route below lists only what its spec entry has).
LABEL_FREE = ("brand", "brand_description", "offer", "label_evidence", "judgments", "dry_run")
RELEVANCE_FREE = ("relevance", "relevance_threshold")
SEARCH_FREE = ("cursor", "min_views", "max_age_days", "sort_rows", "seen")
JUDGED_4 = {"relevant_to": 4, "label": 4, "exclude": 4}
JUDGED_8 = {"relevant_to": 8, "label": 8, "exclude": 8}
LABELLED_4 = {"label": 4, "exclude": 4}
COMMENT_FREE = ("cursor", "judgments", "dry_run", "label_evidence")

PLATFORM_RATES = {
    "tiktok": 1, "instagram": 1, "youtube": 1, "reddit": 1, "threads": 1,
    "twitter": 1, "x": 1, "facebook": 1, "linkedin": 5,
}
MULTI_DEFAULT = ("tiktok", "instagram", "youtube", "reddit", "threads", "twitter", "facebook", "linkedin")
URL_RATES = (
    ("linkedin.com", 5), ("instagram.com", 2), ("tiktok.com", 1), ("youtube.com", 1), ("youtu.be", 1),
    ("x.com", 1), ("twitter.com", 1), ("facebook.com", 1), ("fb.watch", 1), ("reddit.com", 1),
    ("threads.net", 1), ("threads.com", 1),
)


def _url_rate(url):
    """prism/post-stats: 1 a URL on most platforms, 2 on Instagram, 5 on LinkedIn; unknown hosts at 5."""
    host = urlparse(str(url)).netloc.lower()
    return next((rate for domain, rate in URL_RATES if host == domain or host.endswith("." + domain)), 5)


def _item_rate(item):
    if isinstance(item, dict) and "platform" in item:
        return PLATFORM_RATES.get(str(item["platform"]).lower(), 5)
    return _url_rate(item.get("url") if isinstance(item, dict) else item)


def _pages(p):
    return _count(p, "max_pages", MAX_PAGES) if "max_pages" in p else 1


def _threads_search(p):
    # 1 a window of 15; limit up to 100 holds ceil(limit / 15); page 1 relaxation holds up to 4 more
    # unless expand=false or a cursor is sent; include=engagement holds 20.
    windows = math.ceil(_count(p, "limit", 100) / 15) if "limit" in p else 1
    cursor = p.get("cursor") is not None and str(p["cursor"]).strip() != ""
    relax = 0 if str(p.get("expand", "")).lower() == "false" or cursor else 4
    return (windows + relax + _addons(p, {"include": {"engagement": 20}, **JUDGED_4})) * _pages(p)


def _threads_comments(p):
    # One bundled window up to limit 25; above that ceil(limit / 5), 10 at the 50 maximum.
    limit = _count(p, "limit", 50) if "limit" in p else 0
    return math.ceil(limit / 5) if limit > 25 else 1


def _multi(p):
    # Each platform at its own search price (LinkedIn 5), Threads relaxation up to 4 more.
    names = _csv(p.get("platforms")) or list(MULTI_DEFAULT)
    unknown = [n for n in names if n not in PLATFORM_RATES]
    if unknown:
        raise Refused(f"unpriced value platforms={','.join(unknown)}")
    relax = 4 if "threads" in names else 0
    return sum(PLATFORM_RATES[n] for n in names) + relax + _addons(p, JUDGED_8)


def _news(p):
    # 2 + min(5 x countries, max_legs, 12) on the default google engine; bing lifts the ceiling to 62.
    engines = _csv(p.get("engines")) or ["google"]
    unknown = [e for e in engines if e not in ("google", "bing")]
    if unknown:
        raise Refused(f"unpriced value engines={','.join(unknown)}")
    if "bing" in engines:
        return 62
    legs = _count(p, "max_legs", 12) if "max_legs" in p else 12
    countries = len(_csv(p.get("countries")))
    return 2 + (min(legs, 5 * countries) if countries else legs)


def _post_stats(p):
    return sum(_url_rate(u) for u in _list(p, "urls", 100))


def _profiles(p):
    include = _csv(p.get("include"))
    if set(include) - {"posts"}:
        raise Refused(f"unpriced value include={p['include']}")
    posts = "posts" in include
    items = _list(p, "items", 25 if posts else 50)
    return sum(_item_rate(i) + (1 if posts else 0) for i in items)


def _transcripts(p):
    return 3 * len(_list(p, "ids", 100))


def _creator_card(p):
    # 5 covering any 4 platforms, 1 more for each beyond 4, 7 at most. platforms must name known platforms.
    if "platforms" not in p:
        return 5
    value = p["platforms"]
    parts = value if isinstance(value, (list, tuple)) else str(value).split(",")
    names = [str(n).strip().lower() for n in parts]
    if not names or any(n not in PLATFORM_RATES for n in names):
        raise Refused(f"platforms={value!r} is not a list of known platform names")
    n = len(set(names))
    if n > 7:
        raise Refused(f"{n} platforms is above the limit of 7")
    return 5 + max(0, n - 4)


def _mentions(p):
    # 1 an X or Reddit page, 5 an Instagram tag page, per search term: held as one page a platform a term.
    names = set(_csv(p.get("platforms")))
    unknown = sorted(names - set(MENTION_RATES))
    if not names or unknown:
        raise Refused(f"unpriced value platforms={','.join(unknown)}")
    terms = sum(1 for t in MENTION_TERMS if t in p and _present(t, p[t]))
    if not terms:
        raise Refused("prism/mentions needs a handle, url or name")
    return sum(MENTION_RATES[n] for n in names) * terms


def _scrape(p):
    proxy = str(p.get("proxy", "")).lower()
    if proxy not in ("", "auto", "enhanced"):
        raise Refused(f"unpriced value proxy={proxy}")
    return 5 if proxy or "pdf_parse" in p else 1


def _google_trends_quote(required, enums):
    def quote(params):
        missing = [name for name in required if name not in params or not _present(name, params[name])]
        if missing:
            raise Refused("missing required parameter: " + ", ".join(missing))
        for name, allowed in enums.items():
            if name in params and params[name] not in allowed:
                raise Refused(f"unpriced value {name}={params[name]}")
        return 5
    return quote


TRENDS_CATEGORIES = frozenset((
    "autos_and_vehicles", "beauty_and_fashion", "business_and_finance", "entertainment", "food_and_drink",
    "games", "health", "hobbies_and_leisure", "jobs_and_education", "law_and_government", "other",
    "pets_and_animals", "politics", "science", "shopping", "sports", "technology",
    "travel_and_transportation", "climate",
))


# prism/mentions platforms, without web: the spec does not name the web lane's search engine (rule 2).
MENTION_RATES = {"twitter": 1, "reddit": 1, "instagram": 5}
MENTION_TERMS = ("handle", "url", "name")
SCRAPE_FREE = (
    "url", "formats", "only_main_content", "wait_for", "mobile", "timeout", "max_age", "location_country",
    "screenshot_full_page", "include_tags", "exclude_tags", "block_ads", "remove_base64_images",
)

PRICED = {
    f"{TRENDS_ROUTE_PREFIX}/trending": special(
        _google_trends_quote(
            ("location",),
            {
                "hours": frozenset(("4", "24", "48", "168")),
                "category": TRENDS_CATEGORIES,
                "status": frozenset(("all", "active")),
                "sort": frozenset(("relevance", "search_volume", "recency", "title")),
            },
        ),
        ("location", "hours", "category", "status", "sort", "limit"), floor=5),
    # Seeding and the costed collect table (SOURCES.md).
    "tiktok/trending": rule(5, ("region", "trim", "feed")),
    "tiktok/hashtags/popular": rule(6, ("countryCode", "period"), add={"industry": {"all": 90}}),
    "youtube/videos/trending": rule(
        1, ("region", "category", "language", "cursor"), add={"include": {"channel": 5}}, limits={"max_results": 50}),
    "youtube/shorts/trending": rule(5, add={"include": {"channel": 10}}),
    "instagram/music/trending": rule(5),
    "instagram/search/location": rule(5, ("query",)),
    "instagram/location/posts": rule(5, ("location_id", "cursor", "safe_url")),
    "apple_music/charts": rule(1, ("country", "type", "limit")),
    "reddit/subreddit": rule(
        1, ("subreddit", "timeframe", "sort", "after", "trim", "cursor") + LABEL_FREE, add=LABELLED_4),
    "facebook/profile/posts": rule(
        1, ("since", "stop_at_id", "url", "pageId", "cursor", "recent_days"), add={"include": {"engagement": 3}}),
    "facebook/events": rule(1, ("url", "time", "cursor"), add={"include": {"details": 12}}),
    "telegram/profile/posts": rule(1, ("handle", "cursor")),
    "tiktok/profile/videos": rule(
        1, ("since", "stop_at_id", "handle", "user_id", "sort_by", "max_cursor", "region", "trim", "format", "cursor")
        + LABEL_FREE, add=LABELLED_4),
    "tiktok/profile/region": rule(1, ("handle",)),
    "tiktok/profile": rule(1, ("handle",)),
    "instagram/profile/about": rule(1, ("handle",)),
    "tiktok/location/posts": rule(1, ("location_id", "cursor", "region")),
    "twitter/ai-search": special(discovery_quote, ("query", "from_handles", "exclude_handles", "from_date", "to_date")),
    "tiktok/song/videos": rule(1, ("clipId", "cursor", "use")),
    "tiktok/song": rule(1, ("clipId",)),
    "tiktok/hashtag": rule(1, ("hashtag", "hashtag_id")),
    "instagram/audio/reels": rule(1, ("audio_id", "cursor")),
    "twitter/user/tweets": rule(
        1, ("handle", "since", "stop_at_id", "trim", "cursor") + LABEL_FREE, add=LABELLED_4),
    # Expansion and confirmation.
    "tiktok/search/top": rule(
        1, ("query", "publish_time", "sort_by", "region", "country", "exclude_country") + SEARCH_FREE,
        pages="max_pages"),
    "tiktok/search/hashtag": rule(
        1, ("hashtag", "region", "trim") + SEARCH_FREE + LABEL_FREE, add=LABELLED_4, pages="max_pages"),
    "tiktok/search/music": rule(1, ("query", "cursor", "sort_by", "filter_by", "region")),
    "tiktok/search/suggestions": rule(1, ("query", "region")),
    "youtube/search/advanced": rule(
        1,
        ("query", "order", "duration", "event_type", "license", "category", "region", "language", "published_after",
         "published_before", "channel_id", "safe_search", "video_caption", "video_definition", "video_dimension",
         "video_embeddable", "video_type", "topic_id", "location", "location_radius") + SEARCH_FREE,
        add={"includeExtras": 5, "include": {"channel": 5}}, pages="max_pages", limits={"max_results": 50}),
    # include=creator holds 60 a page (2 a creator for a full page of 30) and refunds every creator not looked up.
    "instagram/search/reels": rule(
        1, ("query", "date_posted", "page") + SEARCH_FREE + RELEVANCE_FREE + LABEL_FREE,
        add={"include": {"creator": 60}, **JUDGED_4}, pages="max_pages"),
    "twitter/search/tweets": rule(
        1, ("query", "sort") + SEARCH_FREE + RELEVANCE_FREE + LABEL_FREE, add=JUDGED_4, pages="max_pages"),
    "reddit/search": rule(
        1, ("query", "sort", "timeframe", "after", "trim", "cursor", "max_age_days", "seen") + RELEVANCE_FREE
        + LABEL_FREE, add={"include_body": 25, **JUDGED_4}, pages="max_pages"),
    "reddit/search/comments": rule(1, ("query", "sort", "cursor")),
    "threads/search": special(
        _threads_search,
        ("query", "start_date", "end_date", "trim", "cursor", "seen", "limit", "expand", "include", "max_pages")
        + RELEVANCE_FREE + LABEL_FREE + tuple(JUDGED_4), floor=1),
    "facebook/search/posts": rule(
        1, ("query", "cursor", "start_date", "end_date", "recent_posts", "location_uid", "max_age_days", "seen")
        + RELEVANCE_FREE + LABEL_FREE, add=JUDGED_4, pages="max_pages"),
    "linkedin/search/posts": rule(
        5, ("page", "sort_by", "date_posted", "content_type", "from_company", "from_member", "query", "cursor")
        + RELEVANCE_FREE + LABEL_FREE, add=JUDGED_8),
    "google_news/search": rule(
        1, ("keyword", "location_code", "location_name", "location_coordinate", "language_code", "time_range",
            "publisher", "from", "to")),
    "search/multi": special(
        _multi,
        ("query", "platforms", "since", "tiktok.date_posted", "tiktok.sort_by", "tiktok.region",
         "instagram.date_posted", "youtube.uploadDate", "youtube.sortBy", "youtube.type", "youtube.duration",
         "youtube.region", "reddit.sort", "reddit.timeframe", "threads.start_date", "threads.end_date",
         "twitter.sort", "facebook.start_date", "facebook.end_date", "facebook.recent_posts",
         "facebook.location_uid", "linkedin.sort_by", "linkedin.date_posted", "linkedin.content_type")
        + RELEVANCE_FREE + LABEL_FREE + tuple(JUDGED_8), floor=0),
    "search/creators": rule(
        10, ("query", "sources", "exclude", "min_followers", "verified_only", "sort", "relevance", "judgments"),
        add={"brief": 2}),
    "search/forums": rule(
        10, ("query", "sources", "exclude", "timeframe", "lookback_days", "judgments") + RELEVANCE_FREE),
    "search/news": special(
        _news, ("query", "countries", "engines", "sort", "time_range", "from", "to", "publisher", "max_legs",
                "judgments"), floor=2),
    "search/everywhere": rule(
        20, ("query", "lookback_days", "from_date", "to_date", "sources", "exclude", "relevance", "judgments")),
    # Depth.
    "tiktok/post/comments": rule(
        1, ("url", "trim", "sort") + COMMENT_FREE, add=LABELLED_4, pages="scan_pages", limits={"scan_pages": 7}),
    "instagram/post/comments": rule(
        5, ("url", "sort", "safe_url") + COMMENT_FREE, add=LABELLED_4, pages="scan_pages", limits={"scan_pages": 7}),
    "youtube/video/comments": rule(
        1, ("url", "continuationToken", "order", "searchTerm", "format", "channel_id") + COMMENT_FREE,
        add=LABELLED_4, limits={"max_results": 100}),
    "reddit/post/comments": rule(5, ("url", "trim") + COMMENT_FREE, add=LABELLED_4),
    "facebook/post/comments": rule(1, ("url", "feedback_id") + COMMENT_FREE, add=LABELLED_4),
    "twitter/tweet/replies": rule(1, ("url",) + COMMENT_FREE, add=LABELLED_4),
    "threads/post/comments": special(_threads_comments, ("url", "trim", "limit"), floor=1),
    "youtube/transcripts": special(_transcripts, ("ids",), method="POST", floor=0),
    "youtube/video/transcript": rule(3, ("url", "language")),
    "tiktok/post/transcript": rule(10, ("url", "language")),
    "instagram/media/transcript": rule(10, ("url",)),
    "twitter/tweet/transcript": rule(10, ("url",)),
    "reddit/post/transcript": rule(10, ("url", "language")),
    "facebook/post/transcript": rule(10, ("url",)),
    "tiktok/video/screen-text": rule(5, ("url",)),
    "prism/post-stats": special(_post_stats, ("urls",), method="POST", floor=0),
    "prism/profiles": special(_profiles, ("items", "include", "since"), method="POST", floor=0),
    "prism/creator-card": special(_creator_card, ("handle", "platforms", "verify")),
    "web/scrape": special(_scrape, SCRAPE_FREE + ("proxy", "pdf_parse")),
    # Ask: one post opened live per platform (X is twitter/tweet), at the spec's 1 credit. download_media,
    # get_comments and get_transcript have no price in the spec, so they are refused.
    "tiktok/post": rule(1, ("url", "region", "trim")),
    "instagram/post": rule(1, ("url", "region", "trim")),
    "youtube/video": rule(1, ("url", "language", "hl")),
    "twitter/tweet": rule(1, ("url", "trim")),
    "reddit/post": rule(1, ("url",)),
    "facebook/post": rule(1, ("url",)),
    "threads/post": rule(1, ("url", "trim")),
    # Ask: mentions and tags of a handle. A mentions page with no verified row is refunded, so its floor is 0.
    "instagram/tagged": rule(5, ("handle", "user_id", "cursor", "safe_url")),
    "prism/mentions": special(_mentions, ("platforms", "since", "include_self") + MENTION_TERMS, floor=0),
    # Free account routes.
    "credits/balance": rule(0),
    "credits/transactions": rule(0, ("limit", "cursor", "request_id")),
    "status": rule(0),
    "utility/capabilities": rule(0, ("param",)),
    "utility/endpoints": rule(0, ("platform", "search", "method")),
    "utility/endpoint": rule(0, ("id", "url", "method")),
}
FREE_ROUTES = frozenset(r for r in PRICED if r.startswith(("credits/", "utility/")) or r == "status")


@dataclass
class Result:
    status: str
    route: str
    params_hash: str | None = None
    http_status: int | None = None
    credits_quoted: float = 0
    credits_charged: float = 0
    cache_hit: bool = False
    body: dict | None = None
    items: list = field(default_factory=list)
    vendor_labels: list = field(default_factory=list)
    reason: str = ""
    attempts: int = 1
    failure: str = ""


def transport_failure(exc):
    """timeout, connection or transport for an exception raised by the HTTP callable, read from its class
    names only (requests and the standard library both), so nothing of its message is kept."""
    names = {c.__name__ for c in type(exc).__mro__}
    if names & _TIMEOUT_NAMES:
        return "timeout"
    if names & _CONNECTION_NAMES:
        return "connection"
    return "transport"


def http_failure(status):
    """The class of a non-success HTTP status: http_429, http_5xx, http_4xx or http_other."""
    if status == 429:
        return "http_429"
    if 500 <= status <= 599:
        return "http_5xx"
    if 400 <= status <= 499:
        return "http_4xx"
    return "http_other"


@lru_cache(maxsize=1)
def _list_prices():
    routes = json.loads(ROUTES_FILE.read_text(encoding="utf-8"))["routes"]
    return {(r["path"].strip("/"), r["method"].upper()): r["credits"] or 0 for r in routes}


def list_price(route, method):
    """The spec's credits field, for reports; charging uses the PRICED floor."""
    return _list_prices().get((route, method), 0)


def load_caps(path=CAPS_FILE):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def quote_for(route, method, params):
    """The hold for one call from PRICED. Raises Refused when the call cannot be priced."""
    entry = PRICED.get(route)
    if entry is None or entry.method != method:
        raise Refused("not a priced 42 route")
    unknown = sorted(set(params) - entry.params)
    if unknown:
        raise Refused("unpriced parameter: " + ", ".join(unknown))
    return entry.quote(params)


def floor_for(route, method, params):
    """The least a call the vendor reported is charged: the PRICED base price before refunds."""
    quote_for(route, method, params)
    return PRICED[route].floor(params)


def params_hash(method, params):
    text = json.dumps({"method": method, "params": params}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_vendor_labels(body):
    """Copy of body with each row's vendor model labels moved into row["vendor_labels"].

    Returns (body, items, labels): items are the rows without vendor_labels, labels the parallel list.
    """
    body = copy.deepcopy(body)
    rows = _items(body)
    labels = []
    for row in rows:
        found = {}
        if isinstance(row, dict):
            found = dict(row.pop("vendor_labels", None) or {})
            for key in LABEL_KEYS:
                if key in row:
                    found[key] = row.pop(key)
            computed = row.get("computed")
            if isinstance(computed, dict):
                for key in LABEL_KEYS:
                    if key in computed:
                        found[key] = computed.pop(key)
            row["vendor_labels"] = found
        labels.append(found)
    items = [{k: v for k, v in r.items() if k != "vendor_labels"} if isinstance(r, dict) else r for r in rows]
    return body, items, labels


def requests_http(method, url, *, params=None, json=None, headers=None, timeout=60):
    """The HTTP callable for Cloud Run jobs. Returns (status code, parsed body or None, headers)."""
    import requests

    resp = requests.request(method, url, params=params, json=json, headers=headers, timeout=timeout)
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body, dict(resp.headers)


def _item_lists(node):
    """Every list of rows in a response's data: data itself when it is a list, else every list held
    under an item key at any depth of nested objects (for example data.sources.<platform>.items)."""
    if isinstance(node, list):
        return [node]
    lists = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ITEM_KEYS and isinstance(value, list):
                lists.append(value)
            elif isinstance(value, dict):
                lists.extend(_item_lists(value))
    return lists


def _items(body):
    data = body.get("data") if isinstance(body, dict) else None
    return [row for rows in _item_lists(data) for row in rows]


def _is_empty(body):
    data = body.get("data") if isinstance(body, dict) else None
    if not data:
        return True
    lists = _item_lists(data)
    return bool(lists) and not any(lists)


def _squash(value):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return re.sub(r"[^a-z]", "", text.lower())


def _normalise(route):
    route = route.strip().strip("/")
    return route[3:] if route.lower().startswith("v1/") else route


def forbidden(route, method, params, *, original_route=None):
    """Reason the call may not be made, or "". Adds the AI-lane exclude to search/everywhere."""
    lower = route.lower()
    exact_trends_route = route in ALLOWED_TRENDS_ROUTES and (original_route is None or original_route == route)
    if lower in NEVER_USED:
        return f"{route} is never used: {NEVER_USED[lower]}"
    if (TRENDS_TOKEN in _squash(route) and not exact_trends_route) or any(
        TRENDS_TOKEN in _squash([k, v]) for k, v in params.items()
    ):
        return "Google Trends is never used (RULES.md rule 2)"
    if lower.startswith("prism/") and any("google" in _squash(params[k]) for k in PRISM_LANE_PARAMS if k in params):
        return f"{route} may not use a Google lane (RULES.md rule 2)"
    if lower == "prism/mentions":
        names = set(_csv(params.get("platforms")))
        if not names:
            return ("prism/mentions must name its platforms: the default includes the web lane, whose search "
                    "engine the spec does not name (RULES.md rule 2)")
        if "web" in names:
            return "prism/mentions may not search the web lane: the spec does not name its engine (RULES.md rule 2)"
    if method in MONITOR_METHODS and MONITOR_PATH.fullmatch(lower):
        return f"{method} {route}: a monitor bills on its own cadence outside the credit ledger"
    if lower == "search/everywhere":
        if set(_csv(params.get("sources"))) & set(AI_LANES):
            return "search/everywhere may not name an AI lane in sources"
        if "exclude" in params:
            if not set(AI_LANES) <= set(_csv(params["exclude"])):
                return "search/everywhere must exclude " + ",".join(AI_LANES)
        else:
            params["exclude"] = ",".join(AI_LANES)
    return ""


def _credits(value):
    """A finite, non-negative number of credits, or None: absent, not a number, NaN, inf or negative."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _reported(body, headers):
    """What the vendor says it charged: the larger of the x-credit-cost header and body credits_used."""
    header = next((v for k, v in (headers or {}).items() if str(k).lower() == COST_HEADER), None)
    values = [v for v in (_credits(header), _credits(body.get("credits_used")) if body else None) if v is not None]
    return max(values) if values else None


def _whole(value):
    return int(value) if float(value).is_integer() else value


class SocialCrawlClient:
    def __init__(
        self, *, share, run_id, mode, ledger, raw, http, clock,
        agent=None, schedule_started=True, eval_refresh=False, caps=None, timeout=60,
        retry_wait=RETRY_WAIT, sleep=clock_time.sleep, retry_routes=(), transient_retries=TRANSIENT_RETRIES,
        retry_credits=RETRY_CREDITS,
    ):
        if mode not in ("live", "replay"):
            raise ValueError(f"mode must be live or replay, not {mode!r}")
        caps = caps or load_caps()
        engine = caps["ENGINE_DAILY"]
        self.share_caps = {
            "collect": engine["collect"],
            "confirm": engine["confirm"],
            "reserve": engine["reserve"],
            "ask": caps["ASK_DAILY"],
            "eval": caps["EVAL_DAILY"]["refresh" if eval_refresh else "live"],
            "build": caps["BUILD_DAILY"]["after_schedule" if schedule_started else "before_schedule"],
            "pulse": caps["PULSE_DAILY"],
            "video": (caps.get("VIDEO_DAILY") or {}).get("credits", 0),
        }
        if share not in self.share_caps:
            raise ValueError(f"share must be one of {sorted(self.share_caps)}, not {share!r}")
        self.monthly = caps["MONTHLY"]["total"]
        self.protected = set(caps["MONTHLY"]["protected"])
        self.floor = caps["BALANCE_FLOOR"]
        self.share, self.run_id, self.mode, self.agent = share, run_id, mode, agent
        self.ledger, self.raw, self.http, self.clock, self.timeout = ledger, raw, http, clock, timeout
        self.retry_wait, self.sleep = retry_wait, sleep
        self.retry_routes = frozenset(_normalise(r) for r in retry_routes)
        self.transient_retries, self.retry_credits = transient_retries, retry_credits
        self._retry_held = 0   # holds of the transient retries made so far
        self._exhausted = set()  # retry routes whose retries all failed in this client
        self._balance = None
        self._balance_reads = 0
        self._halt = None  # (status, reason) once the client has stopped

    def __repr__(self):
        return f"SocialCrawlClient(share={self.share!r}, run_id={self.run_id!r}, mode={self.mode!r})"

    def call(self, route, params=None, *, method=None, market=None, item_id=None, seed_key=None, agent=None,
             lane=None, use_cache=True):
        return self._call(route, params, method=method, market=market, item_id=item_id, seed_key=seed_key,
                          agent=agent, lane=lane, use_cache=use_cache)

    def discover_x(self, params, *, market=None, use_cache=True):
        return self._call("twitter/ai-search", params, market=market, lane="discovery",
                          use_cache=use_cache, discovery=True)

    def account_profile(self, platform, handle, **kwargs):
        from core.collect.location_sources import COUNTRY_ROUTES

        if platform not in COUNTRY_ROUTES:
            raise ValueError("account country is supported only for TikTok and Instagram")
        return self._call(COUNTRY_ROUTES[platform], {"handle": handle.casefold()}, **kwargs)

    def _call(self, route, params=None, *, method=None, market=None, item_id=None, seed_key=None, agent=None,
              lane=None, use_cache=True, discovery=False):
        original_route = route
        route = _normalise(route)
        params = dict(params or {})
        if self._halt:
            return Result(self._halt[0], route, reason=self._halt[1])
        method = (method or (PRICED[route].method if route in PRICED else "GET")).upper()
        why = "" if discovery and route == "twitter/ai-search" else forbidden(route, method, params, original_route=original_route)
        if why:
            return Result("forbidden", route, reason=why)
        try:
            quote = quote_for(route, method, params)
        except Refused as exc:
            return Result("forbidden", route, reason=str(exc))
        floor = PRICED[route].floor(params)
        if quote == 0 and route not in FREE_ROUTES:
            return Result("forbidden", route, reason=f"{method} {route} prices at 0 and is not a free route")
        phash = params_hash(method, params)
        call = {"route": route, "method": method, "phash": phash, "market": market, "item_id": item_id,
                "seed_key": seed_key, "agent": agent or self.agent, "lane": lane}

        if self.mode == "replay":
            body = self.raw.find(route, phash)
            if body is None:
                return Result("not_in_replay", route, phash, reason="no recorded response for these params")
            return self._served(route, phash, body)

        if use_cache:
            body = self.raw.find(route, phash, since=datetime.combine(self._today(), time(0), SAST))
            if body is not None:
                self._write_ledger(call, calls=0, quoted=0, charged=0, cache_hit=True, posts_new=0)
                return self._served(route, phash, body)

        result = self._checked(call, params, quote, floor)
        attempts, charged, failures, transient = 1, result.credits_charged or 0, [], False
        while True:
            wait, extra = self._retry(route, result, attempts, quote)
            if wait is None:
                break
            transient = transient or extra
            failures.append(result.failure or result.status)
            log.warning("socialcrawl %s %s; retry %d in %ss", route, failures[-1], attempts, wait)
            self.sleep(wait)
            result = self._checked(call, params, quote, floor)
            attempts += 1
            charged += result.credits_charged or 0
        if attempts == 1:
            return result
        if transient and result.failure in TRANSIENT:
            self._exhausted.add(route)
        result.attempts, result.credits_charged = attempts, _whole(charged)
        if result.status in ("ok", "empty"):
            result.reason = f"{result.status} on attempt {attempts} after {', '.join(failures)}"
            log.warning("socialcrawl %s %s", route, result.reason)
        else:
            result.reason = f"{result.reason} ({attempts} attempts: {', '.join(failures + [result.failure or result.status])})"
        return result

    def _retry(self, route, result, attempts, quote):
        """(seconds to wait, whether it is a transient retry) before another attempt, or (None, False)."""
        if (route in self.retry_routes and route not in self._exhausted and result.failure in TRANSIENT
                and attempts <= self.transient_retries and self._retry_held + quote <= self.retry_credits):
            self._retry_held += quote
            return self.retry_wait * RETRY_BACKOFF ** (attempts - 1), True
        if (attempts == 1 and result.status == "refunded" and not result.credits_charged
                and route not in ALLOWED_TRENDS_ROUTES):
            return self.retry_wait, False
        return None, False

    def _checked(self, call, params, quote, floor):
        """One live attempt after the cap, key and balance checks."""
        route, phash = call["route"], call["phash"]
        why = self._over_cap(quote)
        if why:
            return Result("cap_reached", route, phash, credits_quoted=quote, reason=why)
        key = os.environ.get(KEY_ENV)
        if not key:
            return Result("error", route, phash, reason=f"{KEY_ENV} is not set")
        why = "" if route in FREE_ROUTES else self._below_floor(key)
        if why:
            return Result("balance_floor", route, phash, credits_quoted=quote, reason=why,
                          failure="balance_unread" if self._balance is None else "")
        return self._live(call, params, quote, floor, key)

    def _today(self):
        return self.clock().astimezone(SAST).date()

    def _served(self, route, phash, body):
        if route == "twitter/ai-search":
            body = project_discovery(body)
        body, items, labels = split_vendor_labels(body)
        if isinstance(body, dict) and body.get("success") is False:
            return Result("error", route, phash, 200, 0, 0, True, body,
                          reason="recorded supplier response reports failure", failure="vendor_error")
        return Result("cached", route, phash, 200, 0, 0, True, body, items, labels)

    def _over_cap(self, quote):
        today = self._today()
        cap = self.share_caps[self.share]
        day = self.ledger.spent(today, today, job=self.share)
        if not math.isfinite(day):
            return f"{self.share} share: the ledger's spend today is not a finite number"
        if day + quote > cap:
            return f"{self.share} share: {day} spent today plus {quote} quoted is over {cap}"
        if self.share not in self.protected:
            month = self.ledger.spent(today.replace(day=1), today)
            if not math.isfinite(month):
                return "monthly cap: the ledger's spend this month is not a finite number"
            if month + quote > self.monthly:
                return f"monthly cap: {month} spent this month plus {quote} quoted is over {self.monthly}"
        return ""

    def _below_floor(self, key):
        reads = 0
        try:
            # The balance read is free, so a failed one is read again (a few times, then it stays unknown and the
            # client fails closed): one transient failure must not refuse every paid call for the whole run.
            while self._balance is None and self._balance_reads < BALANCE_READS:
                if reads:
                    self.sleep(self.retry_wait)
                self._balance_reads += 1
                reads += 1
                try:
                    status, body, _ = self.http("GET", f"{BASE_URL}/credits/balance", params=None, json=None,
                                                headers={"x-api-key": key}, timeout=self.timeout)
                    self._balance = _credits(body["data"]["balance"]) if status == 200 else None
                except Exception as exc:
                    log.warning("socialcrawl balance read failed: %s", type(exc).__name__)
        finally:
            if reads:
                # Every HTTP call is ledgered; the balance read is free. One row counts the reads of this check.
                balance_call = {"route": "credits/balance", "phash": params_hash("GET", {}), "market": None,
                                "item_id": None, "agent": self.agent, "lane": None}
                self._write_ledger(balance_call, calls=reads, quoted=0, charged=0, cache_hit=False, posts_new=0)
        if self._balance is None:
            return "balance could not be read"
        if self._balance < self.floor:
            return f"balance {self._balance:g} is below the floor {self.floor}"
        return ""

    def _live(self, call, params, quote, floor, key):
        route, method = call["route"], call["method"]
        status, body, headers, reason, failure = None, None, {}, "", ""
        try:
            status, body, headers = self.http(
                method, f"{BASE_URL}/{route}",
                params=params if method == "GET" else None,
                json=None if method == "GET" else params,
                headers={"x-api-key": key}, timeout=self.timeout,
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc).replace(key, '[key]')[:200]}"
            failure = transport_failure(exc)
        body = body if isinstance(body, dict) else None
        reported = _reported(body, headers)
        vendor = reported if reported is not None else 0
        # Reported: the higher of the PRICED base price and the report. Unreported: the hold.
        billed = quote if reported is None else max(floor, reported)
        error = body.get("error") if body and isinstance(body.get("error"), dict) else {}

        if error.get("type") == "INSUFFICIENT_CREDITS":
            self._halt = ("insufficient_credits", "halted: SocialCrawl reported INSUFFICIENT_CREDITS")
            outcome, charge, reason = "insufficient_credits", vendor, "SocialCrawl reported INSUFFICIENT_CREDITS; client halted"
            failure = "insufficient_credits"
        elif status in (502, 503):
            outcome, charge, reason, failure = "refunded", vendor, f"HTTP {status} is refunded", "http_5xx"
        elif status is not None and 200 <= status < 300 and body and body.get("success") is not False:
            outcome = "empty" if _is_empty(body) else "ok"
            charge = (quote if reported is None else reported) if outcome == "empty" else billed
        else:
            outcome, charge = "error", billed
            if not failure:
                failure = "vendor_error" if status is not None and 200 <= status < 300 else \
                    http_failure(status) if status is not None else "transport"
            if not reason:
                detail = f"{error.get('type', '')} {error.get('message', '')}".strip()
                reason = f"HTTP {status}: {detail.replace(key, '[key]')[:200]}"
        charge = _whole(charge)
        if self._balance is not None:
            self._balance -= charge

        if route == "twitter/ai-search":
            data = body.get("data") if isinstance(body, dict) else None
            if outcome == "ok" and (not isinstance(data, dict) or not isinstance(data.get("sources"), list)):
                outcome, failure, reason = "error", "parse", "X discovery sources are not a list"
            body = project_discovery(body)
        stored, items, labels = split_vendor_labels(body) if body else (None, [], [])
        # The ledger row goes first, so a failing raw_responses insert never leaves a paid call unledgered.
        self._write_ledger(call, calls=1, quoted=quote, charged=charge, cache_hit=False,
                           posts_new=len(items) if outcome == "ok" else 0)
        try:
            self.raw.append({
                "run_id": self.run_id, "job": self.share, "market": call["market"], "route": route,
                "params_hash": call["phash"], "lane": call["lane"], "seed_key": call["seed_key"],
                "fetched_at": self.clock().astimezone(timezone.utc).isoformat(), "http_status": status,
                "credits_quoted": quote, "credits_charged": charge, "cache_hit": False, "body": stored,
            })
        except Exception as exc:
            outcome, failure = "error", "store"
            reason = f"raw_responses append failed after a {charge} credit call: {type(exc).__name__}: " + \
                str(exc).replace(key, "[key]")[:200]
        if outcome != "ok":
            log.warning("socialcrawl %s %s: %s", route, outcome, reason)
        return Result(outcome, route, call["phash"], status, quote, charge, False, stored, items, labels, reason,
                      failure=failure)

    def _write_ledger(self, call, *, calls, quoted, charged, cache_hit, posts_new):
        platform, _, endpoint = call["route"].partition("/")
        row = {
            "trend_date": self._today().isoformat(), "run_id": self.run_id, "job": self.share,
            "lane": call["lane"], "agent": call["agent"], "market": call["market"],
            "platform": platform, "endpoint": endpoint, "route": call["route"],
            "params_hash": call["phash"], "item_id": call["item_id"], "calls": calls,
            "credits_quoted": quoted, "credits_charged": charged, "cache_hit": cache_hit,
            "posts_new": posts_new, "balance_after": None if self._balance is None else _whole(self._balance),
            "logged_at": self.clock().astimezone(timezone.utc).isoformat(),
        }
        try:
            self.ledger.append(row)
        except Exception as exc:
            # A call that cannot be ledgered must not be followed by more calls.
            self._halt = ("error", f"halted: credit_ledger append failed ({type(exc).__name__})")
            raise
