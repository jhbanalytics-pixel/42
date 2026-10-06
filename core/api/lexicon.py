"""Lexicon (core/api/contract.md section 20): the words and hashtags 42 is recording in one market.

Page port, 3 October 2026. The old Lexicon read the desk's slang index, which f42-api never serves. This reads 42's
own items instead: open cultural_map rows of kind meme (legacy slang lands there, core/detect/legacy.py) and
hashtag, never creator, never a generic or stoplisted hashtag, with their posts in the market counted per day as
store.item_days counts them. Topic items are left out: their labels are 42's names for a theme, not words people
write. The window is the 28 days ending on the latest good aggregate run, as Seed path reads; the week change
compares its last 7 days with the 7 before. Every number is a Figure with the query id q_lexicon.
"""
import datetime as dt
import re

from core.api.discover import BadRequest, figure
from core.api.seedpath import _keyword, _market
from core.api.today import LABELS
from core.detect.items import is_generic

__all__ = ["BadRequest", "NotReady", "build_lexicon"]

QUERY_ID = "q_lexicon"
DAYS = 28
WEEK = 7
TERM_LIMIT = 100
KINDS = ("meme", "hashtag")
_ID = re.compile(r"^[0-9a-f]{40,}$")


class NotReady(Exception):
    """No good aggregate run exists yet, so there is no window to count in."""


def _day(value, days):
    return (dt.date.fromisoformat(value) + dt.timedelta(days=days)).isoformat()


def _label(row):
    """The item's label, else its canonical key; never an id."""
    for value in (row.get("label"), row.get("canonical_key")):
        text = (value or "").strip()
        if text and not _ID.match(text):
            return text
    return None


def _seed_term(label):
    """The label as Seed path matches it, or None when Seed path would refuse it."""
    try:
        return _keyword(label)
    except BadRequest:
        return None


def _change(week, prior, warming):
    if warming:
        return {"percent": None, "reason": "warming_up",
                "text": "42 has not collected for two full weeks yet, so there is no earlier week to compare with."}
    if not prior:
        return {"percent": None, "reason": "no_earlier_posts",
                "text": "No posts in the week before, so there is no change to work out."}
    percent = round((week - prior) * 100 / prior)
    if percent == 0:
        return {"percent": 0, "reason": None, "text": "Same as the week before"}
    return {"percent": percent, "reason": None,
            "text": f"{'Up' if percent > 0 else 'Down'} {abs(percent)}% on the week before"}


def _shown(row):
    kind = row.get("kind")
    if kind not in KINDS or row.get("status") != "active":
        return False
    return not is_generic(kind, row.get("canonical_key") or "") and not is_generic(kind, row.get("label") or "")


def build_lexicon(store, market):
    market = _market(market)
    run = store.latest_aggregate_run()
    if run is None:
        raise NotReady("no aggregate run has finished yet")
    end = run["run_date"]
    start, week_from, prior_from = _day(end, 1 - DAYS), _day(end, 1 - WEEK), _day(end, 1 - 2 * WEEK)
    body = {"market": market, "market_name": LABELS[market], "status": "ok", "message": None,
            "query_id": QUERY_ID, "run_id": run["run_id"],
            "window": {"from": start, "to": end, "days": DAYS,
                       "week": {"from": week_from, "to": end},
                       "prior_week": {"from": prior_from, "to": _day(week_from, -1)}},
            "kinds": list(KINDS), "terms": [], "truncated": False, "notes": []}
    # Rows the builder drops (generic, stoplisted, unlabelled) can sit inside the read, so it reads twice the
    # list and says the list is cut only when the terms it keeps run past it, or the read itself was cut.
    read_limit = 2 * TERM_LIMIT + 1
    rows = store.lexicon_terms(market, KINDS, start, end, week_from, prior_from, read_limit)
    if rows is None:
        body.update(status="not_ready", message="The lexicon fills once 42 has counted posts for the words and "
                                                "hashtags it records.")
        return body
    first_collect = store.first_ok_collect_date()
    warming = first_collect is None or str(first_collect) > prior_from
    rid = run["run_id"]
    terms = []
    for r in rows:
        label = _label(r)
        if not _shown(r) or label is None or not r.get("posts_window"):
            continue
        scope = {"item_id": r["item_id"], "market": market}
        hashed = [dict(scope, start=start, end=end, posts=r["posts_window"])]
        terms.append({
            "item_id": r["item_id"], "label": label, "kind": r["kind"],
            "kind_word": "Hashtag" if r["kind"] == "hashtag" else "Word or phrase",
            "seed_term": _seed_term(label),
            "topic_href": f"#/t/{r['item_id']}?market={market}",
            "first_seen": str(r["first_seen"]) if r.get("first_seen") else None,
            "posts": figure(r["posts_window"], f"posts in {DAYS} days", QUERY_ID, rid, hashed),
            "week_posts": figure(r["posts_week"] or 0, f"posts in the last {WEEK} days", QUERY_ID, rid,
                                 [dict(scope, start=week_from, end=end, posts=r["posts_week"] or 0)]),
            "prior_week_posts": figure(r["posts_prior_week"] or 0, f"posts in the {WEEK} days before", QUERY_ID, rid,
                                       [dict(scope, start=prior_from, end=_day(week_from, -1),
                                             posts=r["posts_prior_week"] or 0)]),
            "change": _change(r["posts_week"] or 0, r["posts_prior_week"] or 0, warming),
        })
    terms.sort(key=lambda t: (-t["posts"]["value"], t["label"].lower(), t["item_id"]))
    body["truncated"] = len(terms) > TERM_LIMIT or len(rows) >= read_limit
    body["terms"] = terms[:TERM_LIMIT]
    if not body["terms"]:
        body.update(status="empty", message=f"42 has not recorded posts for any word or hashtag in "
                                            f"{LABELS[market]} in the last {DAYS} days.")
        return body
    body["notes"].append("Posts are counted on the days 42 linked them to the word or hashtag, as the topic pages "
                         "count them. First seen is the day 42 first recorded the item in any market.")
    if warming:
        body["notes"].append("42 has not collected for two full weeks yet, so no week-on-week change is shown.")
    if body["truncated"]:
        body["notes"].append(f"Only the {TERM_LIMIT} most-posted words and hashtags are listed.")
    return body
