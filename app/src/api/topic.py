"""Topic story payload for 42.

Merges the desk's per-topic brief (synthesis, Prompt Pulse, receipts, voices)
with live aggregates computed straight from enriched_content: the topic's reach,
its share of voice over time, the observed-volume momentum series, the slang it
travels in, and the post wall. Share of voice is a relative per-market %, never
blended; momentum is observed volume, never a forecast.
"""

import re
from concurrent.futures import ThreadPoolExecutor

from src.api import bq

_TAG_RE = re.compile(r"#\w+")
_SLANG_SPLIT_RE = re.compile(r"[,;|]")
SERIES_DAYS = 30


def _by_date(rows, key="n"):
    return {r["day"]: int(r.get(key) or 0) for r in rows if r.get("day") is not None}


def _wall_card(p, primary=""):
    # The other topics this post carries, for a "also: ..." cross-tag chip so a
    # multi-topic post reads as cross-tagged, not misfiled.
    others = [bq.topic_label(t) for t in (p.get("topic_groups") or []) if t and t != primary]
    return {
        "id": p.get("id"),
        "text": p.get("text"),
        "platform": bq._platform_label(p.get("platform")),
        "market": (p.get("market") or "").upper(),
        "engagement": int(p.get("engagement") or 0),
        "age": bq.age_label(bq.best_stamp(p)),
        "handle": p.get("handle") or "",
        "url": p.get("url") or "",
        "also": others[:2],
    }


def _post_age_key(p):
    hours = bq._age_hours(bq.best_stamp(p))
    return hours if hours is not None else 1e12


def _has_brief_content(brief):
    if not isinstance(brief, dict):
        return False
    idea = brief.get("idea")
    values = (brief.get("trend"), brief.get("relevance"), idea.get("text") if isinstance(idea, dict) else None)
    return any(isinstance(value, str) and value.strip() for value in values)


def build_topic_profile(topic_id: str, region: str, base: dict | None = None):
    """One topic's story payload, or None when the topic has no window rows and
    no desk brief. `base` is the desk topic dict (brief substance) when present."""
    market = (region or "all").strip().lower()
    b = base or {}
    tag_market = market if market != "all" else str(b.get("region") or "").lower()
    daily = None
    if base is None:
        daily = bq.fetch_topic_daily(topic_id, market)
        if not daily:
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        daily_read = pool.submit(bq.fetch_topic_daily, topic_id, market) if daily is None else None
        market_daily_read = pool.submit(bq.fetch_market_daily, market)
        voice_read = pool.submit(bq.fetch_voice_pools, [topic_id], market)
        channel_read = pool.submit(bq.fetch_topic_channel_sov, topic_id, market)
        tone_read = pool.submit(bq.fetch_topic_tone_distribution, topic_id, market)
        tags_read = pool.submit(bq.derive_topic_hashtags, tag_market) if tag_market else None

        daily = daily_read.result() if daily_read else daily
        market_daily = market_daily_read.result()
        voice_pools = voice_read.result()
        channel_sov = channel_read.result()
        tone_distribution = tone_read.result()
        hashtags = tags_read.result() if tags_read else {}

    vol_series = bq.zero_filled_series(_by_date(daily, "n"), SERIES_DAYS, "n")
    reach_series = bq.zero_filled_series(_by_date(daily, "reach"), SERIES_DAYS, "reach")
    market_series = bq.zero_filled_series(_by_date(market_daily), SERIES_DAYS, "n")
    sov_series = [
        {
            "date": v["date"],
            "sov_pct": round(100.0 * v["n"] / m["n"], 2) if m["n"] else 0.0,
        }
        for v, m in zip(vol_series, market_series)
    ]
    reach = sum(p["reach"] for p in reach_series)
    mentions = sum(p["n"] for p in vol_series)
    sov = sov_series[-1]["sov_pct"] if sov_series else 0.0

    cleaned = bq.clean_items(voice_pools.get(topic_id, []))
    cleaned.sort(key=_post_age_key)
    wall = [_wall_card(p, topic_id) for p in cleaned]

    slang, seen = [], set()
    for p in cleaned:
        for raw in _SLANG_SPLIT_RE.split(p.get("slang_terms") or ""):
            term = raw.strip().lower()
            if term and term not in seen:
                seen.add(term)
                slang.append(term)
    slang = slang[:8]

    tags = []
    if tag_market:
        tags = [h["tag"] for h in hashtags.get((tag_market, topic_id), [])]

    return {
        "id": topic_id,
        "topic": b.get("topic") or bq.topic_label(topic_id),
        "region": b.get("region") or (market.upper() if market != "all" else ""),
        "label": b.get("label") or "",
        "why": b.get("why") or "",
        "momentum": b.get("momentum") or "steady",
        "score": b.get("score"),
        "seed": b.get("seed"),
        "mentions": mentions or int(b.get("mentions") or 0),
        "reach": reach,
        "sov": sov,
        "sources": b.get("sources"),
        "creators": b.get("creators"),
        "sentiment": b.get("sentiment"),
        "social_mood": b.get("social_mood"),
        "velocity": b.get("velocity"),
        "age": b.get("age") or "",
        "series": b.get("series"),
        "volume_series": vol_series,
        "sov_series": sov_series,
        "sov_by_channel": channel_sov,
        "media_tone_dist": tone_distribution,
        "reach_series": reach_series,
        "platforms": b.get("platforms") or [],
        "creators_list": b.get("creators_list") or [],
        "voices": b.get("voices") or [],
        "brief": b.get("brief"),
        "receipts": b.get("receipts") or [],
        "tags": tags,
        "slang": slang,
        "wall": wall,
        "wall_count": len(wall),
        "has_brief": _has_brief_content(b.get("brief")),
        "generated": False,
    }
