"""Creator profile and voices payloads for 42.

The influence layer. One creator's 30-day footprint, and the reach-ranked board
of voices per market. Reads enriched_content through bq.py and ranks by the
reach a creator commands, never raw post count. Every figure traces to real
rows; a creator with no rows in the window returns None so the API can 404.
"""

import re
from datetime import UTC, datetime

from src.api import bq, synth

_TAG_RE = re.compile(r"#\w+")


def _split_tags(raw) -> list:
    return _TAG_RE.findall(raw or "")[:6]


def _human(n) -> str:
    """Compact reach number for the one prose read line, e.g. 142M, 8.4k."""
    n = int(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1e9:.1f}".rstrip("0").rstrip(".") + "B"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}".rstrip("0").rstrip(".") + "k"
    return str(n)


def _read_line(reach: int, posts: int) -> str:
    """A factual one-liner from the numbers, never a fabricated claim."""
    tail = (
        "High output, and the reach is the signal that places them."
        if posts > 40
        else "Few posts, big rooms. A real voice, not a poster."
    )
    return (
        "Ranks on reach, not volume. "
        + _human(reach)
        + " potential audience across "
        + str(posts)
        + " posts in 30d. "
        + tail
    )


def _post_age_key(p):
    """Sort key for newest-first: smaller is newer, unstamped sinks to the end."""
    hours = bq._age_hours(bq.best_stamp(p))
    return hours if hours is not None else 1e12


def _wall_card(p: dict) -> dict:
    tg = [t for t in (p.get("topic_groups") or []) if t]
    topic_key = tg[0] if tg else None
    return {
        "id": p.get("id"),
        "text": p.get("text"),
        "platform": bq._platform_label(p.get("platform")),
        "market": p.get("market"),
        "engagement": int(p.get("engagement") or 0),
        "age": bq.age_label(bq.best_stamp(p)),
        "url": p.get("url") or None,
        "published_at": p.get("published_at"),
        "collected_at": p.get("collected_at"),
        "hashtags": _split_tags(p.get("hashtags")),
        "topic": topic_key,
        "topic_label": bq.topic_label(topic_key) if topic_key else "",
    }


def build_creator_profile(handle: str) -> dict | None:
    """One creator's profile payload, or None when the handle has no window rows.

    Reach, post count, rank within the home market, by-the-numbers aggregates,
    the topics they drive, the reach-over-time series, the top post, and the
    cleaned post wall newest first."""
    overview = bq.fetch_creator_overview(handle)
    if overview is None:
        return None
    norm = overview["handle"]
    market = overview["market"]

    rank, rank_total = bq.fetch_creator_reach_rank(norm, market) if market else (None, 0)

    # clean_items dedups near-identical text keeping the first row it sees, so
    # feed it a deterministic order with the highest-engagement near-duplicate
    # first: the top post then stops flapping between reposts on repeat loads.
    raw = bq.fetch_creator_posts(handle)
    raw.sort(key=lambda p: (-int(p.get("engagement") or 0), str(p.get("id") or "")))
    cleaned = bq.clean_items(raw)
    peak = max(cleaned, key=lambda p: int(p.get("engagement") or 0), default=None)
    # id is the final tiebreak so equal-age posts hold a stable wall order.
    cleaned.sort(key=lambda p: (_post_age_key(p), str(p.get("id") or "")))
    wall = [_wall_card(p) for p in cleaned]

    top_post = None
    if peak:
        card = _wall_card(peak)
        top_post = {
            "id": card["id"],
            "url": card["url"],
            "published_at": card["published_at"],
            "collected_at": card["collected_at"],
            "text": card["text"],
            "engagement": card["engagement"],
            "platform": card["platform"],
            "market": card["market"],
            "age": card["age"],
            "topic": card["topic"],
            "topic_label": card["topic_label"],
        }

    topics = [{"id": key, "label": bq.topic_label(key)} for key in overview["topic_keys"]]
    posts = overview["posts"]
    reach = overview["reach"]

    return {
        "handle": norm,
        "platform": overview["platform"],
        "market": market,
        "market_label": bq.MARKET_LABELS.get(market, (market or "").upper()),
        "markets": overview["markets"],
        "rank": rank,
        "rank_total": rank_total,
        "reach": reach,
        "posts": posts,
        "total_engagement": reach,
        "avg_engagement": round(reach / posts) if posts else 0,
        "top_engagement": overview["top_engagement"],
        "platforms": overview["platforms"],
        "topics": topics,
        "read": _read_line(reach, posts),
        "angle": (
            "Brief " + topics[0]["label"] + " through this handle; the audience already "
            "follows the sound."
        )
        if topics
        else "",
        "reach_series": bq.fetch_creator_reach_series(handle),
        "top_post": top_post,
        "wall": wall,
        "wall_count": len(wall),
    }


def build_voices(region: str) -> dict:
    """The reach-ranked board of named voices for a region.

    Single market reads the engine's reach-ordered top authors directly; "ALL"
    merges the three markets and re-ranks by reach (share of voice never blends
    across markets, so the merged board ranks by reach and says so). Each row
    carries a real 30-day reach sparkline from one batched query."""
    reg = (region or "all").strip().lower()
    # The board reads the rolling 30-day window, refreshed by the daily 00:30 UTC
    # cron, so the payload is static for the UTC day. Cache it (multiple author +
    # sparkline queries assemble it) keyed by region and UTC date; the key rolls
    # at midnight so a new day always recomputes. In-process on the warm
    # instance, GCS-backed for new ones.
    cache_key = ("voices", reg, datetime.now(UTC).date().isoformat())
    cached = synth.cache_get(cache_key)
    if cached is not None:
        return cached
    if reg in bq.MARKET_LABELS:
        markets = (reg,)
        label = bq.MARKET_LABELS[reg]
    else:
        markets = ("za", "ng", "ke")
        label = "All markets"

    # Build each handle's reach sparkline per market and key it by (market,
    # handle). On the ALL board a handle that posts in two markets gets one row
    # per market (its single-market reach), so its series must reflect only the
    # row's own market: a blended "all" series summed ZA+NG+KE and overstated
    # those rows, never matching the headline reach beside it. Keying by (market,
    # handle) rather than handle alone also avoids handle_norm collisions across
    # markets on this merged path. Single-market boards keep one market, so the
    # za/ng/ke output is unchanged.
    merged: list = []
    series_by_mh: dict = {}
    for mk in markets:
        authors_mk = [dict(a, market=mk) for a in bq.fetch_top_authors(mk, limit=40)]
        merged.extend(authors_mk)
        for h, s in bq.fetch_voices_series([a.get("name") for a in authors_mk], mk).items():
            series_by_mh[(mk, h)] = s

    if len(markets) > 1:
        merged.sort(key=lambda a: int(a.get("reach") or 0), reverse=True)
        authors = merged[:40]
    else:
        authors = merged

    creators = []
    for a in authors:
        norm = bq.handle_norm(a.get("name"))
        market = a.get("market") or reg
        creators.append(
            {
                "handle": a.get("name"),
                "platform": a.get("platform"),
                "market": market.upper(),
                "reach": int(a.get("reach") or 0),
                "posts": int(a.get("mentions") or 0),
                "topics": a.get("topics") or [],
                "series": series_by_mh.get((market, norm), []),
            }
        )

    trap = None
    if creators:
        leader = creators[0]
        poster = max(creators, key=lambda c: c["posts"])
        if poster["handle"] != leader["handle"]:
            poster_rank = next(
                (i + 1 for i, c in enumerate(creators) if c["handle"] == poster["handle"]), None
            )
            trap = {
                "poster": poster["handle"],
                "poster_posts": poster["posts"],
                "poster_reach": poster["reach"],
                "poster_rank": poster_rank,
                "leader": leader["handle"],
                "leader_reach": leader["reach"],
                "leader_posts": leader["posts"],
            }

    result = {"market_label": label, "region": reg, "creators": creators, "trap": trap}
    synth.cache_set(cache_key, result)
    return result
