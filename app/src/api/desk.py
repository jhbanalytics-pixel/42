"""Signal Desk payload assembly.

Maps the engine's real rows (trend_scores plus trend_analysis plus the voice
pool from enriched_content) onto the Signal Desk data contract, and builds the
live Ask brief. Every number traces to BigQuery; the model writes prose only.
"""

import math
import re
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import timedelta
from functools import partial
from urllib.parse import urlparse

from . import bq, synth

# Same floor as the legacy ask pipeline in main.py: under 12 cleaned items the
# signal is thin and synthesis is skipped.
THIN_FLOOR = 12

# 30 points per series; the client takes the trailing window each view needs.
SERIES_DAYS = 30

RECEIPT_SNIPPET_CHARS = 90
RECEIPT_LIMIT = 3

# One process-wide bound also covers simultaneous market warmups.
_DESK_READ_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="desk-read")

# Mono descriptor domain words for the topic-key prefixes. The label reads
# "Music · South Africa", never a snake_case key.
_DOMAIN_LABELS = {
    "music": "Music",
    "sports": "Sport",
    "politics": "Politics",
    "economy": "Economy",
    "fintech": "Fintech",
    "finance": "Finance",
    "film": "Film",
    "food": "Food",
    "fashion": "Fashion",
    "genz": "Culture",
    "culture": "Culture",
    "diaspora": "Diaspora",
    "education": "Education",
    "transport": "Transport",
    "infra": "Infrastructure",
}

# The engine writes its stats into campaign_angles prose: "128 posts across
# 6 sources", "12 creators tracked". Parse, never guess; 0 when unparsable.
_POSTS_SOURCES_RE = re.compile(
    r"(\d[\d,]*)\s+posts?\s+across\s+(\d[\d,]*)\s+sources?", re.IGNORECASE
)
_CREATORS_RE = re.compile(r"(\d[\d,]*)\s+creators?\s+tracked", re.IGNORECASE)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s")


def _mono_label(query_group: str, market: str) -> str:
    head = (query_group or "").split("_", 1)[0]
    domain = _DOMAIN_LABELS.get(head, head.title() or "Culture")
    return domain + " · " + bq.MARKET_LABELS.get(market, market.upper())


def _why_from_headline(headline: str) -> str:
    """First sentence of the brief headline, capped at 18 words."""
    text = (headline or "").strip()
    if not text:
        return ""
    first = _SENTENCE_SPLIT_RE.split(text)[0].strip()
    words = first.split()
    if len(words) > 18:
        first = " ".join(words[:18]).rstrip(",;:") + "..."
    return first


def parse_angle_counts(angles) -> tuple:
    """(sources, creators, stat_sentence) from the campaign_angles strings."""
    sources = 0
    creators = 0
    stat_sentence = ""
    for angle in angles or []:
        if not isinstance(angle, str):
            continue
        m = _POSTS_SOURCES_RE.search(angle)
        if m and not sources:
            sources = int(m.group(2).replace(",", ""))
            stat_sentence = stat_sentence or angle.strip()
        m = _CREATORS_RE.search(angle)
        if m and not creators:
            creators = int(m.group(1).replace(",", ""))
    return sources, creators, stat_sentence


def _clamp_sentiment(tone, tone_rows=None) -> float | None:
    """Convert the engine's 0..1 media tone to a signed -1..1 value.

    tone_score arrives on a 0..1 scale where 0.5 is neutral: TEV2 normalises
    GDELT AvgTone via (avg_tone + 100) / 200. The desk and every frontend
    sentiment site expect a signed -1..1 value, so recenter on 0.5. When no
    GDELT row contributed tone (tone_rows == 0) there is no media-tone signal
    at all, so return None and let the UI show no pill rather than a false
    neutral, which is the only honest reading of a topic with zero coverage.
    """
    if tone_rows is not None and not tone_rows:
        return None
    if not isinstance(tone, (int, float)):
        return None
    return round(max(-1.0, min(1.0, (float(tone) - 0.5) * 2.0)), 3)


def _velocity_bands(values: list) -> tuple:
    """Quartile boundaries over today's absolute velocity scores."""
    s = sorted(values)
    if not s:
        return 0.0, 0.0, 0.0

    def q(p: float) -> float:
        idx = p * (len(s) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(s) - 1)
        return s[lo] + (s[hi] - s[lo]) * (idx - lo)

    return q(0.25), q(0.5), q(0.75)


def _velocity_word(value: float, bands: tuple) -> str:
    q1, q2, q3 = bands
    if value <= q1:
        return "Low"
    if value <= q2:
        return "Medium"
    if value <= q3:
        return "High"
    return "Very high"


def _series_and_delta(history_rows: list, latest) -> tuple:
    """Per-(market, topic) 30-point series plus the day-over-day delta.

    The series is zero-padded to SERIES_DAYS oldest-to-newest; the delta is
    today minus yesterday, 0.0 unless both days wrote a row."""
    by_key: dict = {}
    for r in history_rows:
        key = (r.get("market"), r.get("query_group"))
        day = r.get("trend_date")
        if key[0] is None or key[1] is None or day is None:
            continue
        by_key.setdefault(key, {})[day] = float(r.get("trend_score") or 0.0)
    dates = [
        latest - timedelta(days=offset) for offset in range(SERIES_DAYS - 1, -1, -1)
    ]
    yesterday = latest - timedelta(days=1)
    series_map: dict = {}
    delta_map: dict = {}
    for key, days in by_key.items():
        series_map[key] = [round(days.get(d, 0.0), 3) for d in dates]
        if latest in days and yesterday in days:
            delta_map[key] = round(days[latest] - days[yesterday], 3)
        else:
            delta_map[key] = 0.0
    return series_map, delta_map


def _voice_quotes(pool: list) -> list:
    """Three real [platform_label, quote, age] triples from a topic's voice pool.

    Age comes from the post's own publish stamp ("3h", "2d"); "" when the
    source carries no stamp, and the client omits the chip."""
    quotes = bq.build_quotes(bq.clean_items(pool or []), limit=3)
    return [
        [bq._platform_label(q.get("platform")), q["text"], q.get("age") or ""]
        for q in quotes
    ]


def _creators_list(top_creators) -> list:
    """Named creator handles from the engine's "@handle | platform | N" strings."""
    handles = []
    for entry in bq.clean_creator_strings(top_creators):
        head = entry.split("|", 1)[0].strip()
        if head:
            handles.append(head if head.startswith("@") else "@" + head)
        if len(handles) >= 6:
            break
    return handles


def _humanize_count(n) -> str:
    """Compact human count: 1450000 reads 1.5m, 42300 reads 42.3k, 318 reads 318."""
    n = int(n)
    for cut, unit in ((1_000_000, "m"), (1_000, "k")):
        if n >= cut:
            return ("%.1f" % (n / cut)).rstrip("0").rstrip(".") + unit
    return str(n)


def _snippet(text: str) -> str:
    t = " ".join((text or "").split())
    if len(t) <= RECEIPT_SNIPPET_CHARS:
        return t
    return t[: RECEIPT_SNIPPET_CHARS - 1].rstrip() + "…"


def _receipts(pool: list, social_refs) -> list:
    """Top 3 real signal sources for a topic.

    Named voice posts lead (platform, @handle, snippet, humanized engagement,
    age from the post's own publish stamp); the engine's structured social_refs
    fill the remainder (platform, host, title, no metric, no age). Nothing is
    invented: an empty pool and an empty ref list yield an empty list and the
    client drops the section."""
    out = []
    for item in bq.clean_items(pool or []):
        if len(out) >= RECEIPT_LIMIT:
            break
        handle = item.get("handle") or ""
        if not bq.is_real_handle(handle, item.get("platform")):
            continue
        snippet = _snippet(item.get("text"))
        if not snippet:
            continue
        engagement = int(item.get("engagement") or 0)
        metric = _humanize_count(engagement) + " engagement" if engagement > 0 else ""
        out.append(
            [
                bq._platform_label(item.get("platform")),
                handle if handle.startswith("@") else "@" + handle,
                snippet,
                metric,
                bq.age_label(bq.best_stamp(item)),
                item.get("url") or "",
            ]
        )
    if len(out) < RECEIPT_LIMIT:
        for ref in bq.parse_social_refs(social_refs):
            if len(out) >= RECEIPT_LIMIT:
                break
            host = (urlparse(ref["url"]).netloc or "").lower()
            host = host[4:] if host.startswith("www.") else host
            if not host:
                continue
            out.append(
                [
                    bq._platform_label(ref.get("platform")),
                    host,
                    _snippet(ref.get("title") or host),
                    "",
                    "",
                    ref.get("url") or "",
                ]
            )
    return out


def _platforms(display, fallback) -> list:
    """[[label, weight]] from render_payload channels, else the platform list
    at weight 1."""
    channels = (display or {}).get("channels") or []
    if channels:
        return [[bq._platform_label(name), int(weight)] for name, weight in channels]
    return [[bq._platform_label(p), 1] for p in fallback or []]


def _build_topic(
    row: dict, series_map: dict, delta_map: dict, bands: tuple, pools: dict, age: str
) -> dict:
    market = row.get("market") or ""
    query_group = row.get("query_group") or ""
    key = (market, query_group)
    score = round(float(row.get("trend_score") or 0.0), 3)
    velocity = float(row.get("velocity_score") or 0.0)
    sources, creators, stat_sentence = parse_angle_counts(row.get("campaign_angles"))
    payload = bq.parse_render_payload(row.get("render_payload"))
    display = payload["display"]
    nano = row.get("nano_banana_prompt") or ""
    lyria = row.get("lyria_prompt") or ""
    synthesis = row.get("trend_synthesis") or ""
    context = row.get("cultural_context") or ""

    brief = {
        "trend": synthesis,
        "relevance": bq.clean_sentiment(row.get("sentiment_summary")) or stat_sentence,
        "idea": {"tool": "Editorial response", "text": context},
        "prompt": {"nano": nano, "lyria": lyria},
    }
    # The opportunity lens is generated lazily by /api/desk/opportunity, one
    # topic at a time, so the desk build never waits on the model. When a
    # prior request already generated and cached it, the brief carries it.
    trend_date = row.get("trend_date")
    opportunity = synth.cache_get(
        (
            "opp",
            market + ":" + query_group,
            trend_date.isoformat() if trend_date is not None else "",
        )
    )
    if opportunity:
        brief["opportunity"] = opportunity

    voice_pool = [
        item
        for item in pools.get(query_group, [])
        if market and item.get("market") == market
    ]
    receipts = _receipts(voice_pool, row.get("social_refs"))

    # Momentum reads the engine's stored momentum_label when Wave 1 has written
    # it; otherwise it falls back to the velocity-window read, then to today's
    # velocity+score rail. Every branch yields one of rising/building/steady/
    # cooling, so the existing pill renders unchanged when the new fields are
    # absent.
    momentum = (
        bq.clean_momentum_label(row.get("momentum_label"))
        or bq.momentum_label_from_windows(
            row.get("velocity_score_7d"), row.get("velocity_score_30d")
        )
        or bq.momentum_from_velocity(velocity, score).lower()
    )

    out = {
        "id": query_group,
        "region": market.upper(),
        "topic": bq.topic_label(query_group),
        "label": _mono_label(query_group, market),
        "momentum": momentum,
        "score": score,
        # Seed score (0..1): how worth-seeding the trend is for driving
        # Nanobanana/Lyria, independent of the trend score. None when the engine
        # has not scored it yet, so the chip stays hidden rather than reading 0.
        "seed": (
            round(float(row.get("seed_score")), 3)
            if row.get("seed_score") is not None
            else None
        ),
        "delta": delta_map.get(key, 0.0),
        # mentions, reach and sov are attached in build_desk_payload from the
        # 30-day enriched_content count, not the engine's single-run item_count.
        "sources": sources,
        "creators": creators,
        "sentiment": _clamp_sentiment(row.get("tone_score"), row.get("tone_rows")),
        "social_mood": (
            bq.mood_label(row.get("b24_sentiment_trajectory"))
            if (row.get("b24_sentiment_trajectory") or "").strip()
            else None
        ),
        "velocity": _velocity_word(abs(velocity), bands),
        "age": age,
        "why": _why_from_headline(row.get("headline")),
        "series": series_map.get(key, [0.0] * SERIES_DAYS),
        "platforms": _platforms(display, row.get("platforms")),
        "creators_list": _creators_list(row.get("top_creators")),
        "voices": _voice_quotes(voice_pool),
        "brief": brief,
    }
    if receipts:
        out["receipts"] = receipts
    # Forecast outlook (heating/steady/cooling) only rides when the engine has
    # the FORECAST_ENABLED block on and persisted it into render_payload. Absent
    # on every row today, so the key stays off the topic and the chip no-ops.
    if payload.get("forecast_outlook"):
        out["outlook"] = payload["forecast_outlook"]
    # Wave 1 lifecycle and continuity badges. Both columns are NULL until the
    # engine's LIFECYCLE_ENABLED / CONTINUITY_BADGES_ENABLED flags flip, so the
    # key stays off the topic and the badge renders nothing today.
    lifecycle = bq.clean_lifecycle_phase(row.get("lifecycle_phase"))
    if lifecycle:
        out["lifecycle"] = lifecycle
    continuity = bq.clean_continuity_state(row.get("continuity_state"))
    if continuity:
        out["continuity"] = continuity
    return out


# A bridge needs real presence on both sides: at least this many posts in
# each scene that do NOT carry the other scene's tag. Double-tagged posts are
# multi-label classification, not a creator carrying culture across scenes.
BRIDGE_MIN_EXCLUSIVE = 2
BRIDGE_LIMIT = 3

# Lexicon floor: a term must clear this many archive rows before it surfaces.
LEXICON_MIN_COUNT = 25
LEXICON_LIMIT = 24


def build_bridges(rows: list) -> list:
    """Creators active in two distinct topic scenes, from real 14-day posts.

    rows come from fetch_bridge_rows: one row per (handle, market, post) with
    the post's topic list. A handle qualifies when some pair of topics from
    different domains each has BRIDGE_MIN_EXCLUSIVE posts not tagged with the
    other topic. Anon ids and outlet handles drop via is_real_handle. Every
    number on the card is counted from these rows; nothing is modeled."""
    by_handle: dict = {}
    for r in rows:
        handle = r.get("handle") or ""
        market = r.get("market") or ""
        topics = [t for t in (r.get("topics") or []) if t]
        if not handle or not market or not topics:
            continue
        if not bq.is_real_handle(handle, r.get("platform")):
            continue
        entry = by_handle.setdefault((handle, market), {"topic_ids": {}, "eng": {}})
        post_id = r.get("id")
        entry["eng"][post_id] = float(r.get("engagement") or 0.0)
        for t in topics:
            entry["topic_ids"].setdefault(t, set()).add(post_id)

    cards = []
    for (handle, market), entry in by_handle.items():
        topic_ids = entry["topic_ids"]
        best = None
        names = sorted(topic_ids)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                if a.split("_", 1)[0] == b.split("_", 1)[0]:
                    continue
                excl_a = len(topic_ids[a] - topic_ids[b])
                excl_b = len(topic_ids[b] - topic_ids[a])
                if min(excl_a, excl_b) < BRIDGE_MIN_EXCLUSIVE:
                    continue
                weight = sum(
                    entry["eng"].get(p, 0.0) for p in topic_ids[a] | topic_ids[b]
                )
                key = (min(excl_a, excl_b), weight)
                if best is None or key > best[0]:
                    best = (key, a, b, weight)
        if best is None:
            continue
        _key, a, b, weight = best
        ordered = sorted([a, b], key=lambda t: -len(topic_ids[t]))
        from_t, to_t = ordered[0], ordered[1]
        note = (
            str(len(topic_ids[from_t]))
            + " posts in "
            + bq.topic_label(from_t)
            + " and "
            + str(len(topic_ids[to_t]))
            + " in "
            + bq.topic_label(to_t)
            + " over the last 14 days. One creator, two scenes."
        )
        cards.append(
            {
                "h": handle if handle.startswith("@") else "@" + handle,
                "from": bq.topic_label(from_t),
                "to": bq.topic_label(to_t),
                "mk": market.upper(),
                "eng": _humanize_count(weight),
                "note": note,
                "weight": weight,
            }
        )
    cards.sort(key=lambda c: -c["weight"])
    for c in cards:
        del c["weight"]
    return cards[:BRIDGE_LIMIT]


def build_lexicon(rows: list) -> list:
    """The living slang index from the engine's own slang_terms detections.

    Aggregates per term across markets (dominant market wins the chip), keeps
    terms clearing LEXICON_MIN_COUNT, and adds the week-over-week inflection
    when the prior week has volume. Definitions live client-side in the
    human-reviewed glossary; the client drops terms without one."""
    by_term: dict = {}
    for r in rows:
        term = (r.get("term") or "").strip()
        market = (r.get("market") or "").strip()
        n = int(r.get("n") or 0)
        if not term or not market or n <= 0:
            continue
        agg = by_term.setdefault(term, {"n": 0, "n7": 0, "n_prev": 0, "markets": {}})
        agg["n"] += n
        agg["n7"] += int(r.get("n7") or 0)
        agg["n_prev"] += int(r.get("n_prev") or 0)
        agg["markets"][market] = agg["markets"].get(market, 0) + n

    out = []
    for term, agg in by_term.items():
        if agg["n"] < LEXICON_MIN_COUNT:
            continue
        market = max(agg["markets"], key=agg["markets"].get)
        entry = {"term": term, "market": market.upper(), "n": agg["n"]}
        if agg["n_prev"] > 0:
            entry["wow"] = int(
                round(100.0 * (agg["n7"] - agg["n_prev"]) / agg["n_prev"])
            )
        out.append(entry)
    out.sort(key=lambda e: -e["n"])
    return out[:LEXICON_LIMIT]


# Platform heat-map cap: the most-active platforms across the day's board.
HEATMAP_LIMIT = 10


def build_platform_heat(topics: list) -> list:
    """Daily platform heat-map: which platform drove the desk today.

    Aggregates each topic's existing per-platform breakdown (the [label, weight]
    pairs already on the topic from render_payload channels) across the whole
    board into one rollup, with each platform's share of the total weight. No
    new query and no new column: it is a fold over data the desk already holds.
    Empty when no topic carried a platform breakdown, so the panel hides."""
    totals: dict = {}
    for t in topics or []:
        for pair in t.get("platforms") or []:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            name = pair[0]
            weight = pair[1]
            if not name or not isinstance(weight, (int, float)) or weight <= 0:
                continue
            totals[name] = totals.get(name, 0.0) + float(weight)
    if not totals:
        return []
    grand = sum(totals.values()) or 1.0
    rows = [
        {
            "platform": name,
            "weight": round(weight, 2),
            "share": round(100.0 * weight / grand, 1),
        }
        for name, weight in totals.items()
    ]
    rows.sort(key=lambda r: -r["weight"])
    return rows[:HEATMAP_LIMIT]


def build_tone_split(counts: dict) -> dict:
    """Shape the lexicon tone counts into a render-ready split, or empty.

    counts come from bq.fetch_tone_split: the positive / neutral / negative row
    counts plus the scored total over the 14-day window. Returns {} when nothing
    is scored (the column is NULL across the feed until the engine's lexicon flag
    flips, so the tone bar hides today). Otherwise each bucket carries its count
    and its rounded share of the scored total, the three shares summing to 100."""
    scored = int((counts or {}).get("scored") or 0)
    if scored <= 0:
        return {}
    pos = int(counts.get("positive") or 0)
    neg = int(counts.get("negative") or 0)
    neu = max(0, scored - pos - neg)
    pos_share = round(100.0 * pos / scored, 1)
    neg_share = round(100.0 * neg / scored, 1)
    neu_share = round(max(0.0, 100.0 - pos_share - neg_share), 1)
    return {
        "scored": scored,
        "positive": {"n": pos, "share": pos_share},
        "neutral": {"n": neu, "share": neu_share},
        "negative": {"n": neg, "share": neg_share},
    }


# Pan-African cap: the strongest cross-market stories on the desk.
PAN_AFRICAN_LIMIT = 6


def build_pan_african(rows: list) -> list:
    """The pan-African stories that genuinely span two or more markets.

    rows come from bq.fetch_pan_african_rows: one row per cross-market story. A
    row with fewer than two distinct markets, or no label, drops (the 2-plus-market
    rule), so a malformed or single-market row never shows as a continent story.
    Empty until the pan-African stage writes rows, so the view hides today. Sorted
    by momentum_composite then item count, the engine's cross-market strength."""
    out: list = []
    for r in rows or []:
        markets: list = []
        for m in r.get("markets") or []:
            slug = str(m or "").strip().lower()
            if slug and slug not in markets:
                markets.append(slug)
        label = str(r.get("story_label") or "").strip()
        if len(markets) < 2 or not label:
            continue
        topics = [str(t).strip() for t in (r.get("topic_keys") or []) if str(t).strip()]
        try:
            momentum = float(r.get("momentum_composite") or 0.0)
        except (TypeError, ValueError):
            momentum = 0.0
        try:
            items = int(r.get("total_item_count") or 0)
        except (TypeError, ValueError):
            items = 0
        out.append(
            {
                "id": str(r.get("story_id") or label),
                "label": label,
                "markets": [m.upper() for m in markets],
                "topics": [bq.topic_label(t) for t in topics],
                "items": items,
                "momentum": round(momentum, 3),
            }
        )
    out.sort(key=lambda s: (-s["momentum"], -s["items"], s["label"]))
    return out[:PAN_AFRICAN_LIMIT]


# Why the stories surface is not ready. surface_missing is the table not being
# in the dataset at all, which no retry changes; dependency_unavailable is a
# probe or read BigQuery refused, retryable unless the refusal was a bad query
# or a refused permission.
# retryable is also what decides whether the desk payload is cached for the
# day (main._desk_cached), so a transient refusal is not pinned until midnight.
_PAN_AFRICAN_ERRORS = {
    "surface_missing": (
        "The pan-African stories surface is not provisioned in this dataset.",
        False,
    ),
    "dependency_unavailable": (
        "The pan-African stories surface could not be read.",
        True,
    ),
}


def _pan_african_unavailable(code: str, retryable: bool | None = None) -> dict:
    message, default_retryable = _PAN_AFRICAN_ERRORS[code]
    if retryable is None:
        retryable = default_retryable
    return {
        "status": "unavailable",
        "error": {"code": code, "message": message, "retryable": retryable},
    }


def build_pan_african_surface(region: str) -> tuple[list, dict]:
    """The desk's stories list and the honest status of the surface behind it.

    ready carries stories; no_stories is the table present and the stage
    having written nothing for the day; unavailable is the table absent or
    unreadable. An empty list is never presented as a completed run on its
    own, because on staging the table has never existed and the earlier read
    swallowed that into the same [] a quiet day produces.
    """
    try:
        rows = bq.fetch_pan_african_rows(region)
    except bq.PanAfricanSurfaceMissing:
        return [], _pan_african_unavailable("surface_missing")
    except bq.PanAfricanSurfaceUnavailable as exc:
        return [], _pan_african_unavailable(
            "dependency_unavailable", retryable=exc.retryable
        )
    stories = build_pan_african(rows)
    return stories, {"status": "ready" if stories else "no_stories", "error": None}


def age_label(age_hours) -> str:
    """The compact "3h" chip value for a data age. Empty when age is unknown."""
    if age_hours is None:
        return ""
    return str(int(round(age_hours))) + "h"


DYNAMIC_CONTRACT_VERSION = "desk_dynamic_signal_v2"
DYNAMIC_MARKETS = ("za", "ng", "ke", "all")

# Bounded, strategist-facing, and deliberately free of any dataset, table,
# query, identity or credential detail.
_DYNAMIC_ERRORS = {
    "no_released_closed_run": (
        "Open discovery is unavailable until a released closed engine run can be proved.",
        True,
    ),
    "dependency_unavailable": (
        "Open discovery is unavailable because its engine source could not be read.",
        True,
    ),
    "unsupported_contract_version": (
        "Open discovery is unavailable because its contract version is unsupported.",
        False,
    ),
    "run_integrity_failed": (
        "Open discovery is unavailable because the released run could not be verified.",
        False,
    ),
    "signal_contract_violation": (
        "Open discovery is unavailable because a released signal failed its contract.",
        False,
    ),
}


# The engine measures six qualities per candidate signal. A strategist reads a
# band; a number invites a precision the measurement does not carry and leaks
# the engine's own vocabulary onto the desk. Bands are published here, once, so
# every surface says the same word for the same measurement.
SIGNAL_QUALITY_FIELDS = (
    "velocity",
    "novelty",
    "breadth",
    "independence",
    "history",
    "geo_confidence",
)

_QUALITY_SOURCE = {
    "velocity": "velocity_score",
    "novelty": "novelty_score",
    "breadth": "breadth_score",
    "independence": "independence_score",
    "history": "historical_similarity",
    "geo_confidence": "geo_confidence",
}

QUALITY_BANDS = {field: ("low", "moderate", "high") for field in SIGNAL_QUALITY_FIELDS}

_QUALITY_MODERATE_FLOOR = 0.34
_QUALITY_HIGH_FLOOR = 0.67


def quality_band(field: str, value) -> str:
    """One measurement as a word, or unmeasured when there is no measurement.

    A missing score is not a low score: reporting it as the bottom band would
    state a finding the run never made.
    """
    if field not in QUALITY_BANDS:
        raise KeyError(field)
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "unmeasured"
    if score != score:  # NaN carries no measurement either
        return "unmeasured"
    if score >= _QUALITY_HIGH_FLOOR:
        return "high"
    if score >= _QUALITY_MODERATE_FLOOR:
        return "moderate"
    return "low"


def signal_qualities(row: dict) -> dict | None:
    """The candidate row's six measurements plus its optional taxonomy tags.

    Tags label a discovery; they never decide what may exist, so a malformed
    tag value yields no tags rather than an invented one.
    """
    source = row if isinstance(row, dict) else {}
    qualities = {}
    for field in SIGNAL_QUALITY_FIELDS:
        value = source.get(_QUALITY_SOURCE[field])
        if field == "history" and value is None:
            qualities[field] = "unmeasured"
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        score = float(value)
        if (
            score != score
            or score in (float("inf"), float("-inf"))
            or not 0 <= score <= 1
        ):
            return None
        qualities[field] = quality_band(field, score)
    tags = source.get("topic_tags")
    if tags is None:
        tags = []
    if not isinstance(tags, list):
        return None
    if any(
        not isinstance(tag, str) or not tag.strip() or tag != tag.strip()
        for tag in tags
    ):
        return None
    if len(set(tags)) != len(tags):
        return None
    qualities["topic_tags"] = list(tags)
    return qualities


def _dynamic_unavailable(market: str, code: str) -> dict:
    message, retryable = _DYNAMIC_ERRORS[code]
    return {
        "contract_version": DYNAMIC_CONTRACT_VERSION,
        "status": "unavailable",
        "requested_market": market,
        "run": None,
        "signals": [],
        "error": {"code": code, "message": message, "retryable": retryable},
    }


_SIGNAL_ID_PATTERN = re.compile(r"sig_[0-9a-f]{64}\Z")
_DISCOVERY_MODES = frozenset({"phrase", "hashtag", "sound", "creator", "entity"})
_EVIDENCE_STATES = frozenset({"ready", "thin", "contradictory", "unchecked"})
_RECEIPT_COUNT_FIELDS = (
    "candidate_count",
    "evidence_count",
    "membership_count",
    "lineage_count",
    "analysis_count",
    "prediction_count",
)


def _iso_date(value: object) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _dynamic_run_object(receipt: dict) -> dict | None:
    completed_at = receipt.get("completed_at")
    closed_at = (
        completed_at.isoformat().replace("+00:00", "Z")
        if hasattr(completed_at, "isoformat")
        else None
    )
    run = {
        "run_id": receipt.get("run_id"),
        "signal_date": _iso_date(receipt.get("signal_date")),
        "market_scope": list(receipt.get("market_scope") or []),
        "observation_start": _iso_date(receipt.get("observation_start")),
        "observation_end": _iso_date(receipt.get("observation_end")),
        "observation_method": receipt.get("observation_method"),
        "closed_at": closed_at,
    }
    required = (
        "run_id",
        "signal_date",
        "observation_start",
        "observation_end",
        "observation_method",
        "closed_at",
    )
    if any(not run[field] for field in required) or not run["market_scope"]:
        return None
    return run


def _instrument_members(row: dict) -> dict | None:
    """The engine's evidence_summary and ribbon_series, passed through untouched.

    The engine view derives both members from the released rows (5 Sep 2026)
    and the frontend validates them against the design system contract, so
    this layer neither builds nor repairs them. A row without the members is a
    view that predates them and is served as before; a member present in the
    wrong shape poisons the signal, the same as a wrong quality.
    """
    members = {}
    if "evidence_summary" in row:
        summary = row.get("evidence_summary")
        if summary is not None:
            if (
                not isinstance(summary, dict)
                or summary.get("contractVersion") != "1.0.0"
            ):
                return None
            members["evidence_summary"] = summary
    if "ribbon_series" in row:
        series = row.get("ribbon_series")
        if series is not None and not isinstance(series, list):
            return None
        members["ribbon_series"] = series
    return members


def _admitted_signal(row: dict, run: dict) -> dict | None:
    """One view row as a contract signal object, or None when it violates.

    Any violation poisons the whole run: the producer never returns a partial
    ready run and never turns invalid rows into a valid result.
    """
    if row.get("contract_version") != DYNAMIC_CONTRACT_VERSION:
        return None
    signal_id = row.get("signal_id")
    name = (row.get("signal_name") or "").strip()
    if not isinstance(signal_id, str) or not _SIGNAL_ID_PATTERN.fullmatch(signal_id):
        return None
    if not name or len(name) > 240:
        return None
    if row.get("discovery_mode") not in _DISCOVERY_MODES:
        return None
    if row.get("evidence_state") not in _EVIDENCE_STATES:
        return None
    if row.get("market") not in run["market_scope"]:
        return None
    if row.get("run_id") != run["run_id"]:
        return None
    if _iso_date(row.get("signal_date")) != run["signal_date"]:
        return None
    receipts = row.get("receipts")
    if not isinstance(receipts, list):
        return None
    qualities = signal_qualities(row)
    if qualities is None:
        return None
    instrument = _instrument_members(row)
    if instrument is None:
        return None
    return {
        "signal": {
            "contract_version": DYNAMIC_CONTRACT_VERSION,
            "run_id": run["run_id"],
            "signal_id": signal_id,
            "signal_date": run["signal_date"],
            "market": row.get("market"),
            "signal_name": name,
            "discovery_mode": row.get("discovery_mode"),
            "evidence_state": row.get("evidence_state"),
            "why_now": row.get("why_now"),
            "possible_response": row.get("possible_response"),
            "observation_start": run["observation_start"],
            "observation_end": run["observation_end"],
            "observation_method": run["observation_method"],
            "receipts": receipts,
            "qualities": qualities,
            **instrument,
        }
    }


def build_dynamic_discovery(region: str) -> dict:
    """The additive Open Discover member for one desk response.

    A qualifying released run is verified before it is believed: the receipt's
    claimed counts must match the measured per-family counts in SQL, and every
    released candidate must pass the signal contract. Zero admitted signals on
    a verified run is no_discovery, the one path to the strategist copy that
    says the completed run produced no discovery. Anything unprovable is
    unavailable, never downgraded to an empty success.
    """
    market = (region or "").strip().lower() or "all"
    if market not in DYNAMIC_MARKETS:
        market = "all"
    try:
        rows = bq.fetch_dynamic_run_receipt(market)
    except bq.DynamicDependencyUnavailable:
        return _dynamic_unavailable(market, "dependency_unavailable")
    if not rows:
        return _dynamic_unavailable(market, "no_released_closed_run")
    receipt = dict(rows[0])
    run = _dynamic_run_object(receipt)
    if run is None:
        return _dynamic_unavailable(market, "run_integrity_failed")
    try:
        measured = bq.fetch_dynamic_run_row_counts(run["run_id"])
        view_rows = bq.fetch_dynamic_run_signals(run["run_id"])
    except bq.DynamicDependencyUnavailable:
        return _dynamic_unavailable(market, "dependency_unavailable")
    for field in _RECEIPT_COUNT_FIELDS:
        if receipt.get(field) != measured.get(field):
            return _dynamic_unavailable(market, "run_integrity_failed")
    signals = []
    seen_signal_ids: set[str] = set()
    for row in view_rows:
        if row.get("contract_version") != DYNAMIC_CONTRACT_VERSION:
            return _dynamic_unavailable(market, "unsupported_contract_version")
        admitted = _admitted_signal(dict(row), run)
        if admitted is None:
            return _dynamic_unavailable(market, "signal_contract_violation")
        signal_id = admitted["signal"]["signal_id"]
        if signal_id in seen_signal_ids:
            return _dynamic_unavailable(market, "signal_contract_violation")
        seen_signal_ids.add(signal_id)
        # The requested market bounds the response. A multi-market run's
        # other-market signals are out of request scope, not a violation.
        if market != "all" and admitted["signal"]["market"] != market:
            continue
        signals.append(admitted)
    return {
        "contract_version": DYNAMIC_CONTRACT_VERSION,
        "status": "ready" if signals else "no_discovery",
        "requested_market": market,
        "run": run,
        "signals": signals,
        "error": None,
    }


def build_desk_payload(region: str) -> dict:
    """Assemble one region from bounded independent reads after its rows load."""
    freshness = bq._freshness()
    rows = bq.fetch_desk_rows(region)
    if not rows:
        pan_african, pan_african_surface = build_pan_african_surface(region)
        return {
            "updated": None,
            "freshness": freshness,
            "digest": None,
            "topics": [],
            "bridges": [],
            "lexicon": [],
            "platform_heat": [],
            "tone_split": {},
            "pan_african": pan_african,
            "pan_african_surface": pan_african_surface,
            "dynamic_discovery": build_dynamic_discovery(region),
        }
    latest = rows[0].get("trend_date")
    calls = [
        (bq.fetch_digest, latest),
        (bq.fetch_score_history, latest, region),
        (
            partial(bq.fetch_voice_pools, per_market=True),
            [r.get("query_group") for r in rows if r.get("query_group")],
            region,
        ),
        (bq.fetch_topics_reach, region),
        (bq.fetch_bridge_rows, region),
        (bq.fetch_lexicon_rows, region),
        (bq.fetch_tone_split, region),
        (build_pan_african_surface, region),
        (build_dynamic_discovery, region),
    ]
    pending = []
    try:
        for function, *args in calls:
            pending.append(_DESK_READ_POOL.submit(function, *args))
        history = pending[1].result()
        pools = pending[2].result()
        series_map, delta_map = _series_and_delta(history, latest)
        bands = _velocity_bands(
            [abs(float(r.get("velocity_score") or 0.0)) for r in rows]
        )
        age = age_label(freshness.get("age_hours"))
        for row in rows:
            pending.append(
                _DESK_READ_POOL.submit(
                    _build_topic, row, series_map, delta_map, bands, pools, age
                )
            )
        topics = [task.result() for task in pending[len(calls) :]]
        topics.sort(key=lambda t: t["score"], reverse=True)
        # Reach and mentions retain their per-market, 30-day measurement.
        reach_map = pending[3].result()
        for t in topics:
            rd = reach_map.get((str(t["region"]).lower(), t["id"]), {})
            t["reach"] = int(rd.get("reach", 0))
            t["mentions"] = int(rd.get("mentions", 0))
        market_totals: dict = {}
        for t in topics:
            market_totals[t["region"]] = market_totals.get(t["region"], 0) + int(
                t.get("mentions") or 0
            )
        for t in topics:
            base = market_totals.get(t["region"], 0) or 1
            t["sov"] = round(100.0 * int(t.get("mentions") or 0) / base, 2)
        pan_african, pan_african_surface = pending[7].result()
        return {
            "updated": latest.isoformat(),
            "freshness": freshness,
            "digest": pending[0].result(),
            "topics": topics,
            "bridges": build_bridges(pending[4].result()),
            "lexicon": build_lexicon(pending[5].result()),
            "platform_heat": build_platform_heat(topics),
            "tone_split": build_tone_split(pending[6].result()),
            "pan_african": pan_african,
            "pan_african_surface": pan_african_surface,
            "dynamic_discovery": pending[8].result(),
        }
    finally:
        for task in pending:
            task.cancel()
        wait(pending)


def _last_activity_age(series_30: list) -> str:
    """Hours since the last day with volume, from the real 30-day series."""
    for offset, point in enumerate(reversed(series_30)):
        if point["n"] > 0:
            return str(offset * 24) + "h"
    return ""


def build_ask_brief(query: str, region: str):
    """The live Ask brief: retrieval first, then one Gemini call.

    Thin signal returns real aggregates only and never calls the model. On a
    rich query the model writes the prose; the cited voices are replaced with
    real posts and every numeric field comes from the retrieval aggregates.
    Returns None when the model fails so the caller can surface a 502."""
    raw = bq.search_content(query, region)
    items = bq.clean_items(raw["items"])[:40]
    series_30 = bq.zero_filled_series(raw["daily_counts"])
    quotes = bq.build_quotes(items)
    named = [
        (handle, platform, int(n))
        for handle, platform, n in raw["creators"]
        if bq.is_real_handle(handle, platform)
    ]
    platform_split = [{"platform": p, "n": int(n)} for p, n in raw["platform_split"]]
    market_split = [{"market": m, "n": int(n)} for m, n in raw["market_split"]]

    if len(items) < THIN_FLOOR:
        return {
            "query": query,
            "region": region,
            "thin": True,
            "broad": bool(raw.get("broad")),
            "total_matches": int(raw["total_matches"]),
            "volume_series": series_30,
            "platform_split": platform_split,
            "market_split": market_split,
            "quotes": quotes,
            "creators": [{"handle": h, "mentions": n} for h, _p, n in named][:8],
        }

    j = synth.synthesize_brief(query, region)
    if j is None:
        return None

    counts = [p["n"] for p in series_30]
    tail = counts[-SERIES_DAYS:]
    peak = max(tail)
    series = [round(n / peak, 3) for n in tail] if peak else [0.0] * SERIES_DAYS
    momentum = j["momentum"] or synth.compute_momentum(counts).lower()
    total = int(raw["total_matches"])

    return {
        "id": "ask_" + re.sub(r"[^a-z0-9]+", "_", (query + "_" + region)).strip("_"),
        "generated": True,
        "query": query,
        "thin": False,
        "broad": bool(raw.get("broad")),
        "region": j["region"],
        # The display name the panel renders; its absence failed the client's
        # readability check and broke every generated brief.
        "topic": query.strip().title(),
        "label": j["label"],
        "momentum": momentum,
        # Log-scaled volume: separates 10 from 100k without flatlining every
        # busy query at the cap the way total/1000 did.
        "score": round(min(0.99, math.log10(total + 1) / 5.0), 3),
        "delta": round(series[-1] - series[-2], 3),
        "has_data": peak > 0,
        "mentions": total,
        "sources": len(platform_split),
        "creators": len(named),
        "sentiment": j["sentiment"],
        "velocity": j["velocity"],
        "age": _last_activity_age(series_30),
        "why": j["why"],
        "series": series,
        "platforms": [
            [bq._platform_label(p["platform"]), p["n"]] for p in platform_split
        ],
        "creators_list": [h if h.startswith("@") else "@" + h for h, _p, _n in named][
            :6
        ],
        "voices": [
            [bq._platform_label(q.get("platform")), q["text"], q.get("age") or ""]
            for q in quotes[:3]
        ],
        "market_split": market_split,
        "brief": {
            "trend": j["trend"],
            "relevance": j["relevance"],
            "opportunity": j["opportunity"],
            "idea": {"tool": j["ideaTool"], "text": j["idea"]},
            "prompt": {"nano": j["promptNano"], "lyria": j["promptLyria"]},
        },
    }


def refine_ask_brief(topic: dict, instruction: str):
    """Apply a refinement instruction to a generated brief, prose only.

    The model rewrites the prose fields the instruction touches. Every number,
    the series, the receipts, and the real cited voices carry over untouched
    from the incoming topic: refinement never re-invents evidence. Returns
    None when the model fails so the caller can surface a 502."""
    j = synth.refine_brief_prose(topic, instruction)
    if j is None:
        return None
    brief = topic.get("brief") if isinstance(topic.get("brief"), dict) else {}
    idea = brief.get("idea") if isinstance(brief.get("idea"), dict) else {}
    prompt = brief.get("prompt") if isinstance(brief.get("prompt"), dict) else {}
    out = dict(topic)
    out["refined"] = int(topic.get("refined") or 0) + 1
    if j["why"]:
        out["why"] = j["why"]
    if j["label"]:
        out["label"] = j["label"]
    out["brief"] = {
        "trend": j["trend"] or brief.get("trend") or "",
        "relevance": j["relevance"] or brief.get("relevance") or "",
        "opportunity": j["opportunity"] or brief.get("opportunity") or "",
        "idea": {"tool": j["ideaTool"], "text": j["idea"] or idea.get("text") or ""},
        "prompt": {
            "nano": j["promptNano"] or prompt.get("nano") or "",
            "lyria": j["promptLyria"] or prompt.get("lyria") or "",
        },
    }
    return out
