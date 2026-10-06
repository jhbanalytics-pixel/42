"""Seeds (core/api/contract.md section 19): what 42 will search for next in one market, and what its last searches
found.

Both come from intelligence_42_core.seed_queue (DATA.md section 5), written by the detect step's seed loop
(core/detect/seeds.py) and by the news harvests (core/collect/gdelt.py, core/collect/local_sources.py), and read by
the collect job each morning. Rows are read by store.seed_queue: the queued rows of the latest seed day that has any,
and the yield rows of the latest seed day that has any, MAX of each yield column per seed.

The placebo lane is a hidden control (TRUST.md A2): its rows are never read here, so they are neither listed, labelled
nor counted. A seed that names a creator account (a creator item, a profile route, or a query that is an @handle) is
only ever counted, as a creator account; a suppressed creator is not counted at all (section 12.1). A news topic can
carry a public figure's name, as topic labels do on every page, and is listed like any topic.
"""
import datetime as dt
import re

from core.api.discover import BadRequest, figure
from core.api.store import creator_key
from core.api.today import LABELS, MARKETS, PLATFORM_WORDS

__all__ = ["BadRequest", "build_seeds"]

QUEUE_ID, RESULTS_ID = "q_seeds_queue", "q_seeds_results"
ROW_LIMIT = 300  # rows of each grain read for one market; a seed day holds far fewer
LOOK_BACK, LOOK_AHEAD = 60, 14  # seed days read around today (SAST), so BigQuery reads a bounded set of partitions
SAST = dt.timezone(dt.timedelta(hours=2))
# The platforms each route reads, as core/detect/seeds.py names them (a test keeps the two the same).
MULTI_PLATFORMS = ("instagram", "youtube", "reddit", "twitter", "threads", "facebook")
NEWS_PLATFORMS = ("twitter", "threads", "reddit")
WORDS = dict(PLATFORM_WORDS, threads="Threads")
# Routes whose query is a person's handle. Any route naming a profile, a user, an account or a channel counts too.
PROFILE_ROUTES = frozenset({"tiktok/profile/videos", "twitter/user/tweets", "facebook/profile/posts"})
_PROFILE = re.compile(r"profile|/users?/|/user$|account|channel|handle", re.IGNORECASE)
_HANDLE = re.compile(r"(?<![\w.])@[\w.]")
LANES = (
    ("expansion", "Following what is rising",
     "Topics already rising in 42's counts, searched more widely to see how far they reach."),
    ("exploration", "Trying something new",
     "Topics from the middle of 42's ranking, some picked by chance, so 42 does not only look where it already looks."),
    ("anchor", "Re-checking what worked", "Searches that found posts last time, run again."),
)
LANE_WORDS = {key: words for key, words, _about in LANES}
OTHER = "Other searches"


def _market(value):
    market = (value or "").strip().upper()
    if market not in MARKETS:
        raise BadRequest("market must be ZA, NG or KE.")
    return market


def _platforms(template, kind):
    if template == "search/multi":
        names = NEWS_PLATFORMS if kind in ("brand", "event") else MULTI_PLATFORMS
    else:
        names = ((template or "").split("/")[0],)
    return [WORDS.get(p, p.capitalize()) for p in names if p]


def _names_a_person(row, mapped):
    template = row.get("template") or ""
    return (row.get("kind") == "creator" or (mapped or {}).get("kind") == "creator" or template in PROFILE_ROUTES
            or bool(_PROFILE.search(template)) or bool(_HANDLE.search(row.get("query") or ""))
            or bool(_HANDLE.search((mapped or {}).get("label") or "")))


def _handle_key(row):
    """platform:handle of a creator seed, as store.creator_key matches people, or None."""
    template, query = row.get("template") or "", (row.get("query") or "").strip()
    if not query or " " in query:
        return None
    return creator_key(template.split("/")[0], query)


def _suppressed_keys(store):
    """platform:handle of every suppressed creator 42 can name, or an empty set while the list does not exist."""
    keys = set()
    ids = store.suppressed_creators() or set()
    if ids:
        keys |= {creator_key(c["platform"], c["handle"]) for c in store.creators_by_id(list(ids)) or []}
    for r in store.suppressions() or []:
        if r.get("status") != "lifted" and r.get("platform") and r.get("handle"):
            keys.add(creator_key(r["platform"], r["handle"]))
    return keys - {None}


def _label(row, mapped):
    if mapped and mapped.get("label"):
        return mapped["label"]
    if row.get("kind") == "sound":
        return "A sound with no name recorded"
    return (row.get("query") or "").strip() or None


def _href(row, label, market):
    if row.get("item_id"):
        return f"#/t/{row['item_id']}?market={market}"
    term = re.sub(r"\s+", " ", (label or "").strip()).lstrip("#").strip()
    if 2 <= len(term) <= 60 and row.get("kind") != "sound":
        from urllib.parse import quote
        return f"#/seedpath/{quote(term, safe='')}?region={market.lower()}"
    return None


def _ordered_lanes(rows):
    known = [key for key, _w, _a in LANES]
    seen = {r.get("lane") for r in rows}
    return [k for k in known if k in seen] + sorted(k for k in seen if k not in known and k is not None) + \
        ([None] if None in seen else [])


def _scope(row, market, person):
    """The row a Figure is hashed over: a person's query and item never enter the hash, so it cannot confirm one."""
    keep = ("seed_date", "lane", "kind", "template", "credits_estimate", "yield_posts", "yield_new_creators")
    out = {k: row.get(k) for k in keep}
    out["market"] = market
    if not person:
        out.update(item_id=row.get("item_id"), query=row.get("query"))
    return out


def _seed(row, mapped, market):
    label = _label(row, mapped)
    lane = row.get("lane")
    return {"lane": lane, "lane_words": LANE_WORDS.get(lane, OTHER), "kind": (mapped or {}).get("kind") or row.get("kind"),
            "label": label, "item_id": row.get("item_id"), "href": _href(row, label, market),
            "platforms": _platforms(row.get("template"), row.get("kind"))}


def _sum(rows, col):
    # Review, 3 October 2026: rows with no value recorded give None, never 0, so the page says Not recorded.
    values = [r.get(col) for r in rows if r.get(col) is not None]
    return sum(values) if values else None


def _queue(rows, people, cmap, market, truncated, today):
    day = rows[0]["seed_date"]
    rows = sorted(rows, key=lambda r: (-(r.get("priority") or 0), r.get("lane") or "", r.get("item_id") or "",
                                       r.get("query") or ""))
    hashed = {id(r): _scope(r, market, id(r) in people) for r in rows}

    def fig(value, unit, part):
        return figure(value, unit, QUEUE_ID, None, [hashed[id(r)] for r in part])

    listed = []
    for r in rows:
        if id(r) in people:
            continue
        s = _seed(r, cmap.get(r.get("item_id")), market)
        s["priority"] = r.get("priority")
        s["credits"] = fig(r["credits_estimate"], "credits, estimated", [r])
        listed.append(s)
    mine = [r for r in rows if id(r) in people]
    lanes = []
    for lane in _ordered_lanes(rows):
        part = [r for r in rows if r.get("lane") == lane]
        lanes.append({"lane": lane, "words": LANE_WORDS.get(lane, OTHER),
                      "seeds": fig(len(part), "searches queued", part),
                      "credits": fig(_sum(part, "credits_estimate"), "credits, estimated", part)})
    # Review, 3 October 2026: the latest queued day can be today, already run at 02:00, or an old day when detect
    # stopped, so the page says will run only for a day still to come.
    day_text = str(day)[:10]
    when = "upcoming" if day_text > today.isoformat() else "today" if day_text == today.isoformat() else "past"
    return {"seed_date": day, "when": when, "seeds": listed, "lanes": lanes,
            "creator_accounts": {"seeds": fig(len(mine), "creator account searches queued", mine),
                                 "credits": fig(_sum(mine, "credits_estimate"), "credits, estimated", mine)}
            if mine else None,
            "seeds_total": fig(len(rows), "searches queued", rows),
            "credits_total": fig(_sum(rows, "credits_estimate"), "credits, estimated", rows),
            "truncated": truncated}


def _results(rows, people, cmap, market, truncated):
    day = rows[0]["seed_date"]
    rows = sorted(rows, key=lambda r: (-(r.get("yield_posts") or 0), -(r.get("yield_new_creators") or 0),
                                       r.get("lane") or "", r.get("item_id") or "", r.get("query") or ""))
    hashed = {id(r): _scope(r, market, id(r) in people) for r in rows}

    def fig(value, unit, part):
        return figure(value, unit, RESULTS_ID, None, [hashed[id(r)] for r in part])

    def counts(part, what):
        return {"seeds": fig(len(part), what, part),
                "posts": fig(_sum(part, "yield_posts"), "new posts found", part),
                "new_creators": fig(_sum(part, "yield_new_creators"), "new creators found", part)}

    listed = []
    for r in rows:
        if id(r) in people:
            continue
        s = _seed(r, cmap.get(r.get("item_id")), market)
        s["posts"] = fig(r.get("yield_posts"), "new posts found", [r])
        s["new_creators"] = fig(r.get("yield_new_creators"), "new creators found", [r])
        listed.append(s)
    mine = [r for r in rows if id(r) in people]
    lanes = [dict({"lane": lane, "words": LANE_WORDS.get(lane, OTHER)},
                  **counts([r for r in rows if r.get("lane") == lane], "searches run"))
             for lane in _ordered_lanes(rows)]
    return {"seed_date": day, "seeds": listed, "lanes": lanes,
            "creator_accounts": counts(mine, "creator account searches run") if mine else None,
            "totals": counts(rows, "searches run"), "truncated": truncated}


def _body(market, status, message):
    return {"market": market, "market_name": LABELS[market], "status": status, "message": message,
            "query_ids": [QUEUE_ID, RESULTS_ID], "queue": None, "results": None,
            "lanes_about": [{"lane": k, "words": w, "about": a} for k, w, a in LANES], "notes": []}


def build_seeds(store, market, today=None):
    market = _market(market)
    today = today or dt.datetime.now(SAST).date()
    since = (today - dt.timedelta(days=LOOK_BACK)).isoformat()
    until = (today + dt.timedelta(days=LOOK_AHEAD)).isoformat()
    rows = store.seed_queue(market, since, until, ROW_LIMIT + 1)
    where = LABELS[market]
    if rows is None:
        return _body(market, "not_ready", "Seeds fills once 42 has written its first list of searches. 42 writes "
                                          "it each morning after it collects and counts the day's posts.")
    rows = [r for r in rows if r.get("lane") != "placebo"]
    if not rows:
        return _body(market, "empty", f"Nothing is queued for {where} yet. 42 writes the next day's searches each "
                                      "morning once it has collected and counted posts, and adds searches for news "
                                      "topics while it collects.")
    cmap = {r["item_id"]: r for r in store.map_items(sorted({r["item_id"] for r in rows if r.get("item_id")})) or []}
    hidden = _suppressed_keys(store)
    kept, people = [], set()
    for r in rows:
        person = _names_a_person(r, cmap.get(r.get("item_id")))
        if person and _handle_key(r) in hidden:
            continue
        kept.append(r)
        if person:
            people.add(id(r))
    body = _body(market, "ok", None)
    queued = [r for r in kept if r["grain"] == "queued"]
    yielded = [r for r in kept if r["grain"] == "yield"]
    cut_q = sum(r["grain"] == "queued" for r in rows) > ROW_LIMIT
    cut_y = sum(r["grain"] == "yield" for r in rows) > ROW_LIMIT
    if cut_q:
        queued = sorted(queued, key=lambda r: -(r.get("priority") or 0))[:ROW_LIMIT]
    if cut_y:
        yielded = sorted(yielded, key=lambda r: -(r.get("yield_posts") or 0))[:ROW_LIMIT]
    if queued:
        body["queue"] = _queue(queued, people, cmap, market, cut_q, today)
    if yielded:
        body["results"] = _results(yielded, people, cmap, market, cut_y)
    if body["queue"] is None and body["results"] is None:
        body["status"], body["message"] = "empty", f"Nothing is queued for {where} yet."
    if body["queue"] is None and body["results"] is not None:
        body["notes"].append(f"Nothing is queued for {where} yet; 42 writes the next list each morning.")
    if cut_q or cut_y:
        body["notes"].append(f"Only the first {ROW_LIMIT} searches of a day are read.")
    return body
