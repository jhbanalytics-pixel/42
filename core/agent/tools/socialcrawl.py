"""The agent's socialcrawl_call and budget_status tools (AGENT.md, Tools and Guardrails).

socialcrawl_call is the only live-spend path in Ask. Before the client is touched it refuses any route that is not a
plain lowercase path, the SOURCES.md "Never used" routes, anything outside ALLOWED_ROUTES, the question budget and
the caller's max_credits; the client enforces ASK_DAILY.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Protocol

from core.agent.context import Refused, RunContext

GOOGLE_TRENDS_ROUTES = frozenset({
    "google_trends/trending",
})
# SOURCES.md "Never used". A trailing slash marks a prefix. The AI-visibility route is prism/ai-visibility.
FORBIDDEN = (
    "google_trends/",
    "prism/trend-board",
    "prism/earliness",
    "prism/audience-language",
    "tiktok/user/audience",
    "twitter/ai-search",
    "prism/investigate",
    "prism/answers",
    "prism/ai-visibility",
)
SEARCH_EVERYWHERE = "search/everywhere"
# What Ask may call at T0 and T1: search, hashtag, comments, transcripts and TikTok screen text (watch_video) on the
# platforms SOURCES.md expands and deepens with. Every one is a GET route in L1's PRICED
# (core/collect/socialcrawl_client.py), which prices no post-detail route, and in docs/full-42/reference/sc_routes.json.
ALLOWED_ROUTES = frozenset({
    "tiktok/search/top", "tiktok/search/hashtag", "tiktok/hashtag", "tiktok/post/comments", "tiktok/post/transcript",
    "tiktok/video/screen-text",
    "instagram/search/reels", "instagram/post/comments", "instagram/media/transcript",
    "youtube/search/advanced", "youtube/video/comments", "youtube/video/transcript",
    "twitter/search/tweets", "twitter/tweet/replies", "twitter/tweet/transcript",
    "reddit/search", "reddit/post/comments", "reddit/post/transcript",
    "threads/search", "threads/post/comments",
    "facebook/search/posts", "facebook/post/comments", "facebook/post/transcript",
    "google_news/search", "search/multi", SEARCH_EVERYWHERE,
    *GOOGLE_TRENDS_ROUTES,
})
EVERYWHERE_EXCLUDE = ("perplexity", "tavily", "twitter-ai-search", "polymarket")
# Generated-answer lanes. No param key or value may name one, except a plain exclude list of names and the string
# value of a free-text search param, since a search about those products does not select them.
ANSWER_LANES = ("perplexity", "tavily", "twitter-ai-search", "grok", "polymarket")
SEARCH_TEXT_PARAMS = frozenset({"q", "query", "keyword", "keywords", "text", "term", "search", "hashtag", "username",
                                "handle", "url", "cursor"})
_LANE_NAMED = re.compile(r"(?:^|-)(" + "|".join(re.escape(lane) for lane in ANSWER_LANES) + r")(?:-|$)")
ITEM_STATUSES = ("ok", "partial")
MARKETS = {"za": "ZA", "south africa": "ZA", "ng": "NG", "nigeria": "NG", "ke": "KE", "kenya": "KE"}
MARKET_PARAMS = ("country", "region", "geo", "gl")
# Stored platform names the answer schema names otherwise (core/eval/answer.schema.json): posts and SocialCrawl
# routes say twitter, the answer says x.
PLATFORM_NAMES = {"twitter": "x"}

_FENCE_TAG = re.compile(r"<([\s\u200b-\u200f\u2060\ufeff]*/?[\s\u200b-\u200f\u2060\ufeff]*untrusted[_-]content[^<>]*)>",
                        re.IGNORECASE)
_SEGMENT = re.compile(r"[a-z0-9_-]+")
_PATH = re.compile(r"[a-z0-9_-]+(/[a-z0-9_-]+)*")


class SocialCrawlClient(Protocol):
    """Mirrors core/collect/socialcrawl_client.py (lane L1), bound in by an adapter at the evening merge.

    That client enforces ASK_DAILY, writes the credit ledger, serves the same-day cache and honours replay mode.
    call returns {"items": [...], "next_cursor", "credits_charged", "status", "cache_hit", "reason"}, where status
    is one of ok, empty, partial, rate_limited, auth_failed, schema_drift, not_in_replay, cap_reached (a daily or
    monthly cap or the balance floor stopped the call) or error, and reason is the client's note on why, with URLs
    already stripped.
    """

    def quote(self, route: str, params: dict) -> float: ...

    def call(self, route: str, params: dict, *, lane: str, run_id: str, max_credits: float) -> dict: ...


def _plain(value, pattern: re.Pattern) -> str | None:
    """Lowercase and nothing else: no decoding, stripping, dot-segment resolution or unicode folding."""
    if not isinstance(value, str) or not value.isascii():
        return None
    value = value.lower()
    return value if pattern.fullmatch(value) else None


def normalise_route(platform: str, endpoint: str) -> str:
    """platform/endpoint lowercased, or Refused when either holds anything but a-z, 0-9, _ and - between slashes."""
    plat, end = _plain(platform, _SEGMENT), _plain(endpoint, _PATH)
    if plat is None or end is None:
        raise Refused(f"platform {str(platform)[:40]!r} and endpoint {str(endpoint)[:80]!r} must be plain route parts: "
                      f"lowercase letters, digits, _ and -, with / only between endpoint parts")
    return f"{plat}/{end}"


def _excludes(params: dict) -> set[str]:
    value = params.get("exclude") or ()
    parts = value.split(",") if isinstance(value, str) else value
    return {str(p).strip().lower() for p in parts if str(p).strip()}


def _lane_named(value) -> str | None:
    """The generated-answer lane a param names, in a string, a comma list, a list or a nested dict's keys or values."""
    if isinstance(value, dict):
        value = [*value.keys(), *value.values()]
    if isinstance(value, (list, tuple, set, frozenset)):
        return next((lane for v in value if (lane := _lane_named(v))), None)
    words = re.sub(r"[^a-z0-9]+", "-", str(value).casefold())
    found = _LANE_NAMED.search(words)
    return found.group(1) if found else None


def _plain_exclude(value) -> bool:
    parts = value.split(",") if isinstance(value, str) else value
    return isinstance(parts, (list, tuple)) and all(
        isinstance(p, str) and _SEGMENT.fullmatch(p.strip().lower()) for p in parts)


def check_route(route: str, params: dict) -> None:
    """Raise Refused for a route that is not a plain path, is on the "Never used" list or is outside ALLOWED_ROUTES,
    except for the three exact Google Trends routes, for search/everywhere without the generated-answer lanes excluded,
    and for any param key, or any value other than a plain exclude or free-text search string, that names one of those
    lanes."""
    norm = _plain(route, _PATH)
    if norm is None:
        raise Refused(f"route {str(route)[:120]!r} is not a plain route: lowercase letters, digits, _ and - between /")
    for banned in FORBIDDEN:
        if norm == banned.rstrip("/") or norm.startswith(banned.rstrip("/") + "/"):
            if banned == "google_trends/" and norm in GOOGLE_TRENDS_ROUTES:
                continue
            raise Refused(f"route {norm} is on the SOURCES.md Never used list")
    if norm not in ALLOWED_ROUTES:
        raise Refused(f"route {norm} is not one the agent may call. Allowed: {', '.join(sorted(ALLOWED_ROUTES))}")
    if norm == SEARCH_EVERYWHERE and not set(EVERYWHERE_EXCLUDE) <= _excludes(params):
        raise Refused(f"{SEARCH_EVERYWHERE} must exclude {','.join(EVERYWHERE_EXCLUDE)}")
    for key, value in params.items():
        if key == "exclude" and _plain_exclude(value):
            continue  # excluding a lane cannot call it; anything but plain names in it is checked like any value
        searched = key in SEARCH_TEXT_PARAMS and isinstance(value, str)
        lane = _lane_named(key if searched else [key, value])
        if lane:
            raise Refused(f"param {str(key)[:40]!r} names {lane}, a generated-answer lane the agent may not call")


def _fence(text) -> str:
    inner = _FENCE_TAG.sub(r"&lt;\1&gt;", "" if text is None else str(text))
    return f"<untrusted_content>{inner}</untrusted_content>"


_HANDLE = re.compile(r"[\w.@-]{1,64}")


def read_credits(value) -> float | None:
    """A call's credits_charged as a finite, non-negative float, 0.0 when missing, None when it is anything else."""
    if value in (None, ""):
        return 0.0
    if isinstance(value, bool):
        return None
    try:
        credits = float(value)
    except (TypeError, ValueError):
        return None
    return credits if math.isfinite(credits) and credits >= 0 else None


def plain_handle(value) -> str | None:
    """A handle only when it is a plain one, so no scraped text reaches the model unfenced as a handle."""
    return value if isinstance(value, str) and _HANDLE.fullmatch(value) else None


def iso_time(value) -> str | None:
    """ISO time from epoch seconds or an ISO string, in UTC when it has an offset, else None."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            when = datetime.fromtimestamp(value, tz=timezone.utc)
        elif isinstance(value, str):
            when = datetime.fromisoformat(value.strip())
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    return (when.astimezone(timezone.utc) if when.tzinfo else when).isoformat()


def _evidence_id(item: dict, platform: str) -> str:
    if item.get("post_id"):
        return str(item["post_id"])
    native = item.get("id")
    if not native:
        canonical = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
        native = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{platform}_{native}"


def _market(value) -> str | None:
    return MARKETS.get(value.strip().casefold()) if isinstance(value, str) else None


def _evidence_market(item: dict, params: dict, ctx_market: str | None) -> tuple[str | None, bool, str | None]:
    """ZA, NG or KE from the item's market or country, else the call's country, region, geo or gl, else ctx.market.

    Returns (market, assumed, source_market). Only the item's own market locates the post. A market from the call's
    params says which market's feeds the platform searched, so the post is seen in those feeds (source_market) and
    counts one step lower, like an own-feed post. A ctx.market fallback is assumed with no source market."""
    for value in (item.get("market"), item.get("country")):
        if market := _market(value):
            return market, False, None
    for value in (params.get(k) for k in MARKET_PARAMS):
        if market := _market(value):
            return market, True, market
    market = _market(ctx_market)
    return market, market is not None, None


# Which of a live search's creators 42 may not show: a creators row behind the handle is in v_suppressed_creators, or a
# suppression that is not lifted names the handle itself (a handle-only suppression need not have a creators row).
# Handles are keyed the way the view matches them: X as x, trimmed, lower case, no @ or u/. Keys go in as one
# newline-joined string, as the Ask warehouse takes scalar parameters only.
_KEY = ("CONCAT(IF(LOWER(TRIM({t}.platform)) = 'twitter', 'x', LOWER(TRIM({t}.platform))), ':', "
        "LOWER(REGEXP_REPLACE(TRIM({t}.handle), r'^@*(u/)?', '')))")
SUPPRESSED_KEYS_SQL = (
    "WITH s AS (SELECT x.* FROM `ogilvy-trends-v2.intelligence_42_core.suppressions` AS x WHERE TRUE "
    "QUALIFY ROW_NUMBER() OVER (PARTITION BY x.suppression_id "
    "ORDER BY x.status_at DESC, x.status = 'lifted', TO_JSON_STRING(x)) = 1) "
    "SELECT DISTINCT k AS key FROM UNNEST(SPLIT(@keys, '\\n')) AS k "
    f"WHERE k IN (SELECT {_KEY.format(t='s')} FROM s WHERE s.status != 'lifted' AND s.handle IS NOT NULL) "
    f"OR k IN (SELECT {_KEY.format(t='c')} FROM `ogilvy-trends-v2.intelligence_42_core.creators` AS c "
    "WHERE c.creator_id IN (SELECT v.creator_id FROM "
    "`ogilvy-trends-v2.intelligence_42_core.v_suppressed_creators` AS v))")
SUPPRESSED_MAX_BYTES = 2_000_000_000
UNCHECKED_NOTE = ("The suppression list could not be read, so this call's posts are withheld: 42 shows no live post "
                  "it cannot check.")


def creator_key(platform, handle) -> str:
    platform = str(platform or "").strip().lower()
    handle = re.sub(r"^@*(u/)?", "", str(handle or "").strip()).lower()
    return f"{'x' if platform == 'twitter' else platform}:{handle}"


def _suppressed(warehouse, keys: list[str]) -> set[str] | None:
    """The suppressed keys among keys, or None when the list cannot be read (no warehouse or a failed read), so the
    call withholds every post: a post 42 cannot check is never shown."""
    keys = sorted({k for k in keys if "\n" not in k})
    if not keys:
        return set()
    if warehouse is None:
        return None
    try:
        rows = warehouse.run(SUPPRESSED_KEYS_SQL, {"keys": "\n".join(keys)}, SUPPRESSED_MAX_BYTES)
        return {str(dict(r)["key"]) for r in rows}
    except Exception:
        return None


def socialcrawl_call(ctx: RunContext, client: SocialCrawlClient, platform: str, endpoint: str, params: dict,
                     max_credits: float, cursor: str | None = None, warehouse=None) -> dict:
    route = normalise_route(platform, endpoint)
    params = dict(params or {})
    if route == SEARCH_EVERYWHERE:
        params["exclude"] = ",".join(sorted(_excludes(params) | set(EVERYWHERE_EXCLUDE)))
    if cursor is not None:
        params["cursor"] = cursor
    check_route(route, params)

    if ctx.calls_left() <= 0:
        raise Refused(f"no SocialCrawl calls left for this question ({ctx.budget['calls']} allowed)")
    quote = client.quote(route, params)
    if quote > max_credits:
        raise Refused(f"{route} costs {quote} credits, over max_credits {max_credits}")
    if quote > ctx.credits_left():
        raise Refused(f"{route} costs {quote} credits, over the {ctx.credits_left()} left in this question's budget")

    result = client.call(route, params, lane="agent_live", run_id=ctx.run_id,
                         max_credits=min(max_credits, ctx.credits_left()))
    status = result.get("status")
    ctx.calls_made += 1
    ctx.emit("socialcrawl", route=route, status=status, credits=0.0)
    ctx.sc_calls.append({"route": route, "params": dict(params), "status": status})
    charged = read_credits(result.get("credits_charged"))
    if charged is None:  # unreadable: an error that charges the quote, so the budget never undercounts
        charged, status = float(quote), "error"
        ctx.events[-1]["status"] = ctx.sc_calls[-1]["status"] = status
    ctx.events[-1]["credits"] = charged
    ctx.credits_spent += charged

    google_trends_signal = route in GOOGLE_TRENDS_ROUTES
    evidence_ids, items, unplaced, placed = [], [], 0, []
    if status in ITEM_STATUSES and not google_trends_signal:
        for item in result.get("items") or []:
            market, assumed, source = _evidence_market(item, params, ctx.market)
            if market is None:
                unplaced += 1  # an unplaced post cannot be cited, so it never becomes evidence
                continue
            item_platform = item.get("platform") or route.split("/", 1)[0]
            placed.append((item, item_platform, market, assumed, source))
    hidden = _suppressed(warehouse, [creator_key(p, i.get("handle")) for i, p, *_ in placed])
    unchecked = len(placed) if hidden is None else 0
    if hidden is None:
        hidden, placed = set(), []
    for item, item_platform, market, assumed, source in placed:
        if creator_key(item_platform, item.get("handle")) in hidden:
            continue  # a suppressed creator's post never becomes evidence
        eid = _evidence_id(item, item_platform)
        ctx.evidence[eid] = {
            "id": eid,
            "platform": PLATFORM_NAMES.get(item_platform, item_platform),
            "handle": plain_handle(item.get("handle")),
            "url": item.get("url"),
            "posted_at": iso_time(item.get("posted_at")),
            "market": market,
            "text": item.get("text"),
            "engagement": item.get("engagement"),
            "flags": ["market_assumed"] if assumed else [],
            "source_market": source,
        }
        if eid not in evidence_ids:
            evidence_ids.append(eid)
        items.append({**ctx.evidence[eid], "evidence_id": eid, "text": _fence(item.get("text"))})

    output = {
        "evidence_ids": evidence_ids,
        "items": items,
        "unplaced": unplaced,
        "suppressed": sum(creator_key(p, i.get("handle")) in hidden for i, p, *_ in placed),
        "next_cursor": result.get("next_cursor"),
        "credits_spent": charged,
        "status": status,
        "cache_hit": bool(result.get("cache_hit")),
        "reason": str(result.get("reason") or ""),
    }
    if unchecked:
        output["unchecked"] = unchecked
        output["note"] = UNCHECKED_NOTE
    if google_trends_signal:
        output["signals"] = {
            "source": "Google search data",
            "signal_type": "search_interest",
            "route": route,
            "payload": _fence(json.dumps(
                result.get("items") or [] if status in ITEM_STATUSES else [],
                separators=(",", ":"), ensure_ascii=False,
            )),
        }
    return output


def budget_status(ctx: RunContext) -> dict:
    return {"credits_left": ctx.credits_left(), "calls_left": ctx.calls_left()}
