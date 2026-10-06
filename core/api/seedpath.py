"""Seed path (core/api/contract.md section 18): where a word, hashtag or slang term was recorded in one market.

Posts are found by store.seed_path_posts: a whole-word, case-insensitive match of the term in post text or
transcript, or the term among the post's hashtags, each post on the market-local day of its first sighting there
(legacy and agent_live sightings aside, as Compare counts). The window is the 28 days ending on the latest good
aggregate run. Every number is a Figure with the query id q_seed_path, or sits in a block that names it.
"""
import datetime as dt
import re
from collections import Counter

from core.api.discover import BadRequest, _cards, figure
from core.api.store import cap_refused
from core.api.today import LABELS, MARKETS, PLATFORM_WORDS

__all__ = ["BadRequest", "build_seed_path"]

QUERY_ID = "q_seed_path"
DAYS = 28
ROW_LIMIT = 5000  # matching posts read inside the window; the bytes cap guards the scan itself
MIN_SUPPORT = 2  # a side word must appear in at least this many matching posts
SIDE_WORDS = 12
TOPICS = 8
HIDDEN_KINDS = ("creator",)  # a creator item names a person; Seed path lists none (section 12.1)
STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before being below between both
but by can could did do does doing down during each even ever every few for from further get gets got had has have
having he her here hers herself him himself his how i if in into is it its itself just let like me more most much my
myself no nor not now of off on once only or other our ours ourselves out over own really same see she should so some
still such than that the their theirs them themselves then there these they this those through to too under until up
us very via was we were what when where which while who whom why will with would yes yet you your yours yourself
yourselves im ive dont cant wont thats its youre lol rt amp new one two day today time way go going know make
""".split())
_MENTION = re.compile(r"(?<![\w@])@[\w.]+")
_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_TOKEN = re.compile(r"#?\w[\w']*")


def _keyword(value):
    """The term as it is matched: trimmed, a leading # dropped, inner spaces collapsed, lower case."""
    term = re.sub(r"\s+", " ", (value or "").strip()).lstrip("#").strip().lower()
    if not 2 <= len(term) <= 60 or not re.search(r"\w", term):
        raise BadRequest("keyword must be 2 to 60 characters with at least one letter or number.")
    return term


def _market(value):
    market = (value or "").strip().upper()
    if market not in MARKETS:
        raise BadRequest("market must be ZA, NG or KE.")
    return market


def _junk(token):
    """A long token with no vowel or with three or more letter and digit runs is an id, not a word."""
    bare = token.lstrip("#")
    return len(bare) > 15 and (not re.search(r"[aeiou]", bare) or len(re.findall(r"[a-z]+|[0-9]+", bare)) >= 3)


def _words(post, term):
    """The distinct words of one post beside the term: no mentions, links, stopwords, numbers or the term itself."""
    text = " ".join(t for t in (post.get("text"), post.get("transcript")) if t)
    text = _URL.sub(" ", _MENTION.sub(" ", text.lower()))
    own = set(term.split()) | {"#" + w for w in term.split()} | {"#" + term.replace(" ", "")}
    out = set()
    for raw in _TOKEN.findall(text):
        token = re.sub(r"'s$", "", raw).strip("'")
        bare = token.lstrip("#")
        if (not bare or token in own or bare in own or bare in STOPWORDS or bare.isdigit()
                or (len(bare) < 3 and not token.startswith("#")) or _junk(token)):
            continue
        out.add(token)
    for tag in post.get("hashtags") or []:
        tag = "#" + (tag or "").lstrip("#").lower()
        if len(tag) > 1 and tag not in own and tag[1:] not in own and not _junk(tag):
            out.add(tag)
    # A hashtag and the same bare word in one post count once, as the hashtag.
    return {w for w in out if w.startswith("#") or "#" + w not in out}


def _empty(term, market, status, message, run=None, start=None, end=None):
    return {"keyword": term, "market": market, "market_name": LABELS[market], "status": status, "message": message,
            "query_id": QUERY_ID, "run_id": run["run_id"] if run else None,
            "window": {"from": start, "to": end, "days": DAYS} if start else None,
            "matched_posts": None, "platforms": [], "side_words": [], "side_words_min_posts": MIN_SUPPORT,
            "topics": [], "truncated": False, "notes": []}


def build_seed_path(store, keyword, market):
    term, market = _keyword(keyword), _market(market)
    run = store.latest_aggregate_run()
    if run is None:
        return _empty(term, market, "not_ready", "Seed path fills after 42's first daily count of posts has finished.")
    end = run["run_date"]
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=DAYS - 1)).isoformat()
    try:
        rows = store.seed_path_posts(market, [term], start, end, ROW_LIMIT + 1)
    except Exception as exc:
        if not cap_refused(exc):
            raise
        return _empty(term, market, "too_large", "This term matches more posts than one search may read. "
                      "Try a longer or more specific term.", run, start, end)
    if rows is None:
        return _empty(term, market, "not_ready", "Seed path fills once 42 has collected posts.", run, start, end)

    firsts = sorted((r for r in rows if r["grain"] == "first"), key=lambda r: (str(r["day"]), r["platform"]))
    posts = [r for r in rows if r["grain"] == "post"]
    truncated = len(posts) > ROW_LIMIT
    posts = posts[:ROW_LIMIT]
    body = _empty(term, market, "ok" if firsts else "no_match", None, run, start, end)
    if not firsts:
        body["message"] = (f"No post 42 collected in {LABELS[market]} uses “{term}” as a word or hashtag.")
        return body
    rid = run["run_id"]

    def fig(value, unit, hashed):
        return figure(value, unit, QUERY_ID, rid, hashed)

    scope = {"keyword": term, "market": market, "start": start, "end": end}
    body["matched_posts"] = fig(len(posts), f"matching posts in {DAYS} days",
                                [dict(scope, post_ids=sorted(p["post_id"] for p in posts))])
    days = [(dt.date.fromisoformat(start) + dt.timedelta(days=n)).isoformat() for n in range(DAYS)]
    for f in firsts:
        mine = [p for p in posts if p["platform"] == f["platform"]]
        per_day = Counter(str(p["day"]) for p in mine)
        hashed = [dict(scope, platform=f["platform"], first_day=str(f["day"]), posts=f["posts"])]
        body["platforms"].append({
            "platform": f["platform"], "name": PLATFORM_WORDS.get(f["platform"], f["platform"].capitalize()),
            "first_seen": fig(str(f["day"]), "first sighting day", hashed),
            "before_window": str(f["day"]) < start,
            "posts_all_time": fig(f["posts"], "matching posts, all time", hashed),
            "posts_in_window": fig(len(mine), f"matching posts in {DAYS} days",
                                   [dict(scope, platform=f["platform"], post_ids=sorted(p["post_id"] for p in mine))]),
            "daily": {"unit": "matching posts a day", "query_id": QUERY_ID, "run_id": rid,
                      "points": [{"date": d, "posts": per_day.get(d, 0)} for d in days]},
        })

    seen = {}
    for p in posts:
        for w in _words(p, term):
            seen.setdefault(w, []).append(p["post_id"])
    ranked = sorted(((w, ids) for w, ids in seen.items() if len(ids) >= MIN_SUPPORT), key=lambda x: (-len(x[1]), x[0]))
    body["side_words"] = [{"word": w, "posts": fig(len(ids), "matching posts with this word",
                                                   [dict(scope, word=w, post_ids=sorted(ids))])}
                          for w, ids in ranked[:SIDE_WORDS]]

    linked = {}
    for p in posts:
        for item_id in p.get("item_ids") or []:
            linked.setdefault(item_id, []).append(p["post_id"])
    detect = store.latest_detect_run(until=end) if linked else None
    if detect is not None:
        cards = {r["item_id"]: (r, c, h) for r, c, h in _cards(store, detect, market)[0] if r["market"] == market}
        topics = []
        for item_id, ids in linked.items():
            if item_id not in cards or cards[item_id][0].get("kind") in HIDDEN_KINDS:
                continue
            row, card, held = cards[item_id]
            topics.append({"item_id": item_id, "title": card["title"], "kind": row.get("kind"),
                           "state_word": card.get("state_word"), "held_back": held is not None,
                           "href": f"#/t/{item_id}?market={market}",
                           "posts": fig(len(ids), "matching posts linked to this item",
                                        [dict(scope, item_id=item_id, post_ids=sorted(ids))])})
        body["topics"] = sorted(topics, key=lambda t: (-t["posts"]["value"], t["title"]))[:TOPICS]

    body["truncated"] = truncated
    body["notes"] = ["Matched as a whole word or hashtag in post text and video transcripts, so it may include "
                     "unrelated uses of the same word."]
    if any(p["before_window"] for p in body["platforms"]):
        body["notes"].append(f"Some first sightings are older than the {DAYS} days the daily counts cover.")
    if truncated:
        body["notes"].append(f"Only the first {ROW_LIMIT} matching posts in the window were read for the daily "
                             "counts, side words and topics.")
    return body
