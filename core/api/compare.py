"""Compare: two to five subjects side by side in the same units over one window (core/api/contract.md section 11).

The window ends on the latest good aggregate run's date. Counts come from posts, each post once per subject and
market, on the market-local day of its first sighting there (store.compare_counts). Hashtags, sounds and creators
are found through post_items; other kinds through post_items and by whole-word name match, with a note saying so
(visual QA, 5 October 2026: a topic matched by name alone read 0 posts beside its card's 18 creators). Growth, reach and state
come from the item's Discover card at the window's end, in items and markets modes only.
"""
import datetime as dt

from core.api.discover import BadRequest, NotReady, _cards, figure
from core.api.store import cap_refused
from core.api.today import (LABELS, MARKETS, PLATFORM_IDS, PLATFORM_WORDS, STATE_WORDS, hidden_people,
                            without_hidden)

__all__ = ["BadRequest", "NotReady", "build_compare"]

MODES = {"items": (2, 5), "markets": (2, 3), "platforms": (2, 5)}
DAYS = (7, 14, 28)
LINKED_KINDS = ("hashtag", "sound", "creator")
PLATFORMS = tuple(p for p in PLATFORM_WORDS if p not in PLATFORM_IDS)
QUERY_ID = "q_compare"
CAP_NOTE = "This comparison needs more data than one question may read"
KEYWORD_NOTE = "counts the posts 42 linked to it plus posts that name it; name matches may include unrelated uses"
COUNT_WORDS = {
    "posts": "Posts first seen in the window",
    "creators": "Distinct creators in the window",
    "engagement": "Engagement (likes, comments, shares, as each platform counts them)",
    "first_seen": "First seen in 42's collection (all time, not clipped to the window)",
    "platforms": "Platforms seen on",
}
# Visual QA, 5 October 2026 (CP02): #motivation read 34 posts here and 28 on Lexicon over the same dates, with no word
# on why. The basis is stated once under every comparison.
BASIS_NOTE = ("Posts count on the day 42 first saw each one in that market; Lexicon dates a post by the day it was "
              "linked to the term, so its totals for the same dates can differ")
STATE_ROW_WORDS = {"growth": "Growth against its own baseline", "reach": "Creators in the last 3 days",
                   "state": "State"}


def _engagement_note(subject, total):
    """Words for a subject whose posts do not all report likes, comments or shares: its engagement is then a
    subtotal, or null when none of them reports one, never a measured zero."""
    posts, counted = total.get("posts") or 0, total.get("engagement_posts")
    if counted is None or counted >= posts:
        return None
    if counted == 0:
        return f"Engagement for {subject['label']}: none of its {posts} posts report likes, comments or shares"
    return (f"Engagement for {subject['label']} counts {counted} of its {posts} posts; the others report no likes, "
            "comments or shares")


def _split(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _one_each(values, what):
    if len(set(values)) != len(values):
        raise BadRequest(f"Name each {what} once.")
    return values


def _market(value):
    market = (value or "").strip().upper()
    if market not in MARKETS:
        raise BadRequest("market must be ZA, NG or KE.")
    return market


def _scopes(mode, item_ids, market, markets, platforms):
    """(item_id, market, platform) per subject, in the order asked."""
    low, high = MODES[mode]
    if mode == "items":
        if not low <= len(item_ids) <= high:
            raise BadRequest(f"mode=items compares {low} to {high} items.")
        m = _market(market)
        return [(i, m, None) for i in _one_each(item_ids, "item")]
    if len(item_ids) != 1:
        raise BadRequest(f"mode={mode} compares one item.")
    if mode == "markets":
        wanted = [v.upper() for v in _split(markets)]
        if not low <= len(wanted) <= high or any(m not in MARKETS for m in wanted):
            raise BadRequest("markets must be two or three of ZA, NG and KE.")
        return [(item_ids[0], m, None) for m in _one_each(wanted, "market")]
    wanted = [PLATFORM_IDS.get(p.lower(), p.lower()) for p in _split(platforms)]
    if not low <= len(wanted) <= high:
        raise BadRequest(f"mode=platforms compares {low} to {high} platforms.")
    unknown = [p for p in wanted if p not in PLATFORMS]
    if unknown:
        raise BadRequest(f"{unknown[0]} is not a platform 42 collects.")
    m = _market(market)
    return [(item_ids[0], m, p) for p in _one_each(wanted, "platform")]


def _terms(row):
    """The label, canonical key and aliases, each once whatever its case."""
    out, seen = [], set()
    for t in [row.get("label"), row.get("canonical_key")] + list(row.get("aliases") or []):
        t = (t or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _terms_or_refuse(row):
    """A keyword subject needs words to match; with none it would match nearly every post."""
    terms = _terms(row)
    if not terms:
        raise BadRequest(f"{row.get('item_id')} has no name 42 can match in post text.")
    return terms


def _scope_label(market, platform):
    return LABELS[market] + (f" {PLATFORM_WORDS.get(platform, platform)}" if platform else "")


def _coverage_note(scopes, health, start, end):
    """Usable collection_health series-days per compared market (or market and platform), always stated."""
    parts, shares = [], set()
    for market, platform in dict.fromkeys((m, p) for _, m, p in scopes):
        rows = [h for h in health if h["market"] == market and start <= str(h["day"]) <= end
                and (platform is None or PLATFORM_IDS.get(h.get("platform"), h.get("platform")) == platform)]
        usable = sum(1 for h in rows if h.get("valid"))
        label = _scope_label(market, platform)
        parts.append(f"{usable} of {len(rows)} {label} series-days usable" if rows else f"no {label} series reported")
        shares.add(usable / len(rows) if rows else None)
    if len(shares) > 1:
        return f"Coverage differs: {', '.join(parts)} over the window; read differences with care"
    return f"Coverage: {', '.join(parts)} over the window"


def _valid_days(health, market, platform):
    return {str(h["day"]) for h in health if h["market"] == market and h.get("valid")
            and (platform is None or PLATFORM_IDS.get(h.get("platform"), h.get("platform")) == platform)}


def _state_figure(row, card):
    """The card's state as a Figure-shaped value with its word, from the item_state row that holds it."""
    if not card.get("state"):
        return None
    f = figure(card["state"], "state", "q_discover_items", row["run_id"],
               [{"item_id": row["item_id"], "market": row["market"], "metric_date": row["metric_date"],
                 "run_id": row["run_id"], "state": row.get("state")}])
    return dict(f, word=card.get("state_word") or STATE_WORDS.get(card["state"]))


def build_compare(store, mode, items=None, market=None, markets=None, platforms=None, days=28):
    if mode not in MODES:
        raise BadRequest("mode must be items, markets or platforms.")
    if days not in DAYS:
        raise BadRequest("days must be 7, 14 or 28.")
    scopes = _scopes(mode, _split(items), market, markets, platforms)
    run = store.latest_aggregate_run()
    if run is None:
        raise NotReady("No aggregate run has finished yet")
    end = run["run_date"]
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=days - 1)).isoformat()
    item_ids = list(dict.fromkeys(i for i, _, _ in scopes))
    found = store.map_items(item_ids)
    if found is None:
        raise NotReady("42's map is not built yet")
    by_id = {r["item_id"]: r for r in found}
    missing = [i for i in item_ids if i not in by_id]
    if missing:
        raise BadRequest("That trend is not tracked by 42. Pick another one.")

    subjects = []
    for n, (item_id, m, p) in enumerate(scopes, 1):
        row = by_id[item_id]
        linked = row.get("kind") in LINKED_KINDS
        subjects.append({"key": f"s{n}", "label": row.get("label") or row.get("canonical_key") or item_id,
                         "item_id": item_id, "market": m, "platform": p,
                         "matched_by": "linked" if linked else "keyword", "card": None,
                         "_terms": None if linked else _terms_or_refuse(row)})
    body = {"mode": mode, "window": {"from": start, "to": end, "days": days}, "subjects": subjects,
            "series": [], "rows": [], "notes": []}
    hidden = hidden_people(store)  # read once; every card and the body go through it

    try:
        states = {}
        detect = store.latest_detect_run(until=end)
        if detect is not None and detect["run_date"] == end:
            wanted = {s["market"] for s in subjects}
            cards, _ = _cards(store, detect, wanted.pop() if len(wanted) == 1 else "all", hidden)
            states = {(r["item_id"], r["market"]): (r, c) for r, c, _ in cards}
        for s in subjects:
            s["card"] = states.get((s["item_id"], s["market"]), (None, None))[1]
        counts = store.compare_counts(
            [{"key": s["key"], "item_id": s["item_id"], "market": s["market"], "platform": s["platform"],
              "terms": s["_terms"]} for s in subjects], start, end)
        if counts is None:
            raise NotReady("Posts are not collected yet")
        health = store.health_range(start, end, sorted({s["market"] for s in subjects})) or []
    except Exception as exc:
        if not cap_refused(exc):
            raise
        for s in subjects:
            s.pop("_terms")
        body["notes"] = [CAP_NOTE]
        return without_hidden(body, hidden)

    window = [(dt.date.fromisoformat(start) + dt.timedelta(days=n)).isoformat() for n in range(days)]
    totals = {r["key"]: r for r in counts if r["grain"] == "total"}
    daily = {(r["key"], str(r["day"])): r["posts"] for r in counts if r["grain"] == "day"}
    units = {"posts": f"posts in {days} days", "creators": f"creators in {days} days",
             "engagement": f"likes, comments and shares in {days} days", "first_seen": "first sighting day",
             "platforms": f"platforms in {days} days"}

    def count_figure(s, metric):
        r = totals.get(s["key"]) or {}
        hashed = dict(r, item_id=s["item_id"], market=s["market"], platform=s["platform"], start=start, end=end)
        return figure(r.get(metric), units[metric], QUERY_ID, run["run_id"], [hashed])

    for s in subjects:
        valid = _valid_days(health, s["market"], s["platform"])
        body["series"].append({"key": s["key"], "unit": "posts a day",
                               "points": [{"date": d, "value": daily.get((s["key"], d), 0) if d in valid else None}
                                          for d in window]})
    metrics = ["posts", "creators", "engagement", "first_seen"]
    for metric in metrics:
        body["rows"].append({"metric": metric, "words": COUNT_WORDS[metric],
                             "values": {s["key"]: count_figure(s, metric) for s in subjects}})
    if mode != "platforms":
        for metric in ("growth", "reach", "state"):
            values = {}
            for s in subjects:
                row, card = states.get((s["item_id"], s["market"]), (None, None))
                values[s["key"]] = None if card is None else (
                    _state_figure(row, card) if metric == "state" else card.get(metric))
            body["rows"].append({"metric": metric, "words": STATE_ROW_WORDS[metric], "values": values})
        body["rows"].append({"metric": "platforms", "words": COUNT_WORDS["platforms"],
                             "values": {s["key"]: count_figure(s, "platforms") for s in subjects}})

    body["notes"].append(_coverage_note(scopes, health, start, end))
    body["notes"].append(BASIS_NOTE)
    for s in subjects:
        note = f"{s['label']} {KEYWORD_NOTE}"
        if s["matched_by"] == "keyword" and note not in body["notes"]:
            body["notes"].append(note)
        s.pop("_terms")
    for s in subjects:
        note = _engagement_note(s, totals.get(s["key"]) or {})
        if note and note not in body["notes"]:
            body["notes"].append(note)
    return without_hidden(body, hidden)
