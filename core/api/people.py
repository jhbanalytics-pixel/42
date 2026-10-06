"""Creator pages and communities (core/api/contract.md sections 12.1 to 12.3).

Three rules hold on anything that names a person:
1. a creator gets a page, is named as a community member, or is shown as the author of a card's evidence only at
   tier macro or above as creators.tier stands now (PAGE_TIERS); an author 42 has no creator row for is not named;
2. items in L2's sensitive set never appear beside a name; v_sensitive_items alone is a floor that excludes items but
   switches nothing on, so until v_sensitive_items_complete exists creator pages carry no item list, no recent posts
   and no community, and communities name no one;
3. items held back or flagged in the latest detect run, or flagged likely_coordinated or check_pattern or paid-led in
   any good detect run of the 28 days, are left out, Evidence flags are dropped, and creators.coord_score is never
   read.
A suppressed creator (the suppression view, once it exists) gets no page and is never named: not as a member, not
in example or recent posts, not as the author of a card's evidence.

Counts are over posts first sighted in the market in the 28 days to the latest good detect run, on Compare's
market and day rule. A community is a connected component of creators joined by three or more shared items that
are none of sensitive, on a platform's board, platform-generic or rule 3; components under five are not shown.
"""
import copy
import datetime as dt
import hashlib
import logging
import re
import statistics

from core.api import fast
from core.api.discover import BadRequest, _cards, _flag, _run, figure
from core.api.store import canon_platform, creator_key
from core.api.today import MARKETS, NotFound, hidden_people, without_hidden

log = logging.getLogger(__name__)

__all__ = ["BadRequest", "NotFound", "build_communities", "build_community", "build_creator"]

PAGE_TIERS = ("macro", "mega")  # Albert may lower this to mid (contract.md section 12.1)
WINDOW_DAYS = 28
MIN_MEMBERS = 5
TOP_ITEMS = 5
# An unnamed TikTok sound is labelled by its numeric audio id; a heading never shows an id.
_ID_LIKE = re.compile(r"(?:[a-z_]+:)?(?:\d{6,}|[0-9a-f]{16,}|uc[\w-]{22})", re.I)
UNNAMED_TOPICS = "Topics without a name yet"
_BY_HANDLE = re.compile(r" by @\S+\Z")
LABEL_ITEMS = 3
RECENT_POSTS = 12
EXAMPLE_POSTS = 6
RULE_3_FLAGS = ("likely_coordinated", "check_pattern", "paid_led")
SMALL = "42 shows creators of this size only in totals"
NO_CREATOR = "No creator with that id"
NO_COMMUNITY = "No community with that id"
NO_SENSITIVE_SET = "Topics per creator appear once 42 can keep sensitive topics off named pages"
NO_RECENT_POSTS = "Recent posts appear once 42 can keep sensitive topics off named pages"
INTERACTION = "Grouped by shared topics; interaction not yet measured"
PROFILE_URLS = {"tiktok": "https://tiktok.com/@{}", "instagram": "https://instagram.com/{}",
                "youtube": "https://youtube.com/@{}", "x": "https://x.com/{}", "facebook": "https://facebook.com/{}",
                "reddit": "https://reddit.com/user/{}", "threads": "https://threads.net/@{}"}


def profile_url(platform, handle):
    """The profile link from the handle by the platform's fixed template, else None."""
    template = PROFILE_URLS.get(canon_platform(platform))
    h = (handle or "").strip().lstrip("@")
    if h.startswith("u/"):
        h = h[2:]
    return template.format(h) if template and h else None


def post_evidence(row, keep_flags=False):
    """A post row as the Evidence record; its flags only with keep_flags (never on a named page)."""
    return {"id": row["post_id"], "platform": canon_platform(row.get("platform")), "handle": row.get("handle"),
            "url": row.get("url"), "posted_at": row.get("published_at"), "market": row.get("geo_market"),
            "text": row.get("text"),
            "engagement": {k: row.get(k) for k in ("views", "likes", "comments", "shares")},
            "flags": list(row.get("flags") or []) if keep_flags else [], "thumbnail_url": row.get("thumbnail_url"),
            "duration_s": row.get("duration_s"), "creator_tier": row.get("creator_tier_at_post")}


def _market(market):
    if market not in MARKETS:
        raise BadRequest("market must be ZA, NG or KE.")
    return market


def _window(run):
    end = run["run_date"]
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=WINDOW_DAYS - 1)).isoformat()
    return start, end


def _gate(store, run):
    """The run's clean Discover cards by (market, item_id), and every item rule 3 keeps off named pages: held back
    or flagged in this run, or flagged likely_coordinated, check_pattern or paid-led in any good detect run of the
    28 days. An earlier hold alone does not count, since most items are held while they are still small."""
    cards, _ = _cards(store, run, "all")
    blocked = {r["item_id"] for r, c, held in cards if held or c.get("flag") in RULE_3_FLAGS}
    blocked |= {r["item_id"] for r in store.detect_states(_window(run)[0], run["run_date"]) or []
                if _flag(r) in RULE_3_FLAGS}
    clean = {(r["market"], r["item_id"]): c for r, c, _ in cards if r["item_id"] not in blocked}
    return clean, blocked


def excluded_items(store, run, market, start, end, blocked=None):
    """What never joins two creators: {"sensitive": set or None until L2's set exists, "generic", "board",
    "rule_3"}."""
    if blocked is None:
        blocked = _gate(store, run)[1]
    return {"sensitive": store.sensitive_items(), "generic": set(store.generic_items()),
            "board": set(store.board_items(market, start, end)), "rule_3": set(blocked)}


class PeopleUnavailable(Exception):
    """The suppression list cannot be read, so no page may name anyone (fail closed)."""


def suppressed(store):
    """The suppressed creator ids. A missing view or a failed read raises PeopleUnavailable: without the list 42
    cannot know who asked to be hidden, so it names no one rather than everyone."""
    try:
        ids = store.suppressed_creators()
    except Exception as exc:
        log.warning("the suppression list could not be read (%s); people routes are closed", type(exc).__name__)
        raise PeopleUnavailable() from exc
    if ids is None:
        log.warning("the suppression view does not exist; people routes are closed")
        raise PeopleUnavailable()
    return ids


class _Page:
    """What one market's named pages share: the run, its window, clean cards and the exclusions."""

    def __init__(self, store, market, edges=False):
        self.store, self.market = store, _market(market)
        self.run = _run(store)
        self.start, self.end = _window(self.run)
        self.cards, blocked = _gate(store, self.run)
        self.ex = excluded_items(store, self.run, self.market, self.start, self.end, blocked)
        self.sensitive_known = self.ex["sensitive"] is not None and store.sensitive_complete()
        self.never_named = (self.ex["sensitive"] or set()) | self.ex["rule_3"]
        self.no_edge = set().union(*(s for s in self.ex.values() if s))
        if edges:  # a page that lists communities reads its edges beside the names below
            fast.start(store, "community_edges", self.market, self.start, self.end, self.no_edge)
        self.suppressed = suppressed(store)
        self.nameable = self._nameable_authors()

    def _nameable_authors(self):
        """Creator keys of the market's card evidence authors who may be named: every creators row behind the key is
        at page tier now and none is suppressed."""
        keys = {creator_key(e.get("platform"), e.get("handle")) for (m, _), c in self.cards.items()
                if m == self.market for e in c.get("evidence") or []} - {None}
        rows = (self.store.creators_by_handle(sorted(keys)) or []) if keys else []
        hidden = (self.store.creators_by_id(sorted(self.suppressed)) or []) if self.suppressed else []
        blocked = {creator_key(c.get("platform"), c.get("handle")) for c in hidden}
        by_key = {}
        for c in rows:
            by_key.setdefault(creator_key(c.get("platform"), c.get("handle")), []).append(c)
        return {k for k, cs in by_key.items() if k not in blocked
                and all(c.get("tier") in PAGE_TIERS and c["creator_id"] not in self.suppressed for c in cs)}

    def fig(self, value, unit, query_id, rows):
        return figure(value, unit, query_id, self.run["run_id"], rows)

    def head(self):
        return {"market": self.market, "date": self.end,
                "window": {"from": self.start, "to": self.end, "days": WINDOW_DAYS}}

    def card(self, item_id):
        """The clean card fit to sit beside a name: evidence only from nameable authors, every flag dropped."""
        card = self.cards.get((self.market, item_id))
        if card is None:
            return None
        card = copy.deepcopy(card)
        kept = [dict(e, flags=[]) for e in card.get("evidence") or []
                if creator_key(e.get("platform"), e.get("handle")) in self.nameable]
        ids = {e.get("id") for e in kept}
        card.update(evidence=kept, thumbnails=[t for t in card.get("thumbnails") or [] if t in ids])
        return card


def build_creator(store, creator_id, market):
    _market(market)
    hidden = suppressed(store)
    creator = store.creator(creator_id)
    if creator is None or creator_id in hidden:
        raise NotFound(NO_CREATOR)
    if creator.get("tier") not in PAGE_TIERS:
        raise NotFound(SMALL)
    page = _Page(store, market)
    posts = _one_per_post(store.window_posts([creator_id], page.market, page.start, page.end) or [])
    recent = None
    if page.sensitive_known:
        recent = sorted((p for p in posts if not set(p["item_ids"]) & page.never_named),
                        key=lambda p: (p.get("published_at") or "", p["post_id"]), reverse=True)[:RECENT_POSTS]

    items = None
    if page.sensitive_known:
        counts = {}
        for p in posts:
            for i in set(p["item_ids"]) - page.never_named:
                counts[i] = counts.get(i, 0) + 1
        items = []
        for i, n in counts.items():
            card = page.card(i)
            if card is not None:
                rows = [{"creator_id": creator_id, "item_id": i, "market": page.market, "start": page.start,
                         "end": page.end, "post_ids": sorted(p["post_id"] for p in posts if i in p["item_ids"])}]
                items.append({"card": card, "posts": page.fig(n, f"posts in {WINDOW_DAYS} days", "q_creator_items",
                                                              rows)})
        items.sort(key=lambda x: (-x["posts"]["value"], x["card"].get("title") or "", x["card"]["item_id"]))

    formats = {}
    for p in posts:
        for f in p.get("formats") or []:
            formats[f] = formats.get(f, 0) + 1
    views = [p["views"] for p in posts if p.get("views") is not None]
    reach_rows = [{"creator_id": creator_id, "market": page.market, "start": page.start, "end": page.end,
                   "posts": sorted((p["post_id"], p.get("views")) for p in posts)}]
    community = None
    if page.sensitive_known:
        community = next(({"label": body["label"], "community_id": body["community_id"]}
                          for members, body in _communities(page) if creator_id in members), None)
    return {
        "creator": {"creator_id": creator["creator_id"], "platform": canon_platform(creator.get("platform")),
                    "handle": creator.get("handle"), "tier": creator.get("tier"),
                    "followers": page.fig(creator.get("followers"), "followers", "q_creator", [creator]),
                    "home_market": creator.get("home_market"),
                    "profile_url": profile_url(creator.get("platform"), creator.get("handle"))},
        **page.head(),
        "recent_posts": None if recent is None else [post_evidence(p) for p in recent],
        "recent_posts_note": None if page.sensitive_known else NO_RECENT_POSTS,
        "items": items,
        "formats": [{"format": f, "posts": page.fig(n, f"posts in {WINDOW_DAYS} days", "q_creator_formats",
                                                    [{"creator_id": creator_id, "format": f, "posts": n,
                                                      "start": page.start, "end": page.end}])}
                    for f, n in sorted(formats.items(), key=lambda fn: (-fn[1], fn[0]))] or None,
        "reach": {"median_views": page.fig(statistics.median(views) if views else None, "views on the middle post",
                                           "q_creator_reach", reach_rows),
                  "posts_28d": page.fig(len(posts), f"posts in {WINDOW_DAYS} days", "q_creator_reach", reach_rows)},
        "community": community,
        "note": None if page.sensitive_known else NO_SENSITIVE_SET,
    }


def _components(edges):
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        a, b = find(e["creator_a"]), find(e["creator_b"])
        if a != b:
            parent[max(a, b)] = min(a, b)
    groups, shared = {}, {}
    for x in list(parent):
        groups.setdefault(find(x), set()).add(x)
    for e in edges:
        items = shared.setdefault(find(e["creator_a"]), {})
        for i in e["items"]:
            items.setdefault(i, set()).update((e["creator_a"], e["creator_b"]))
    return [(groups[root], shared.get(root, {})) for root in groups]


def _communities(page):
    """[(every member id, community body)] for the market, largest first."""
    store = page.store
    edges = store.community_edges(page.market, page.start, page.end, page.no_edge) or []
    found = [(members, shared) for members, shared in _components(edges) if len(members) >= MIN_MEMBERS]
    if not found:
        return []
    everyone = sorted(set().union(*(m for m, _ in found)))
    tops = {}
    for n, (_, shared) in enumerate(found):
        tops[n] = [i for i, _ in sorted(shared.items(), key=lambda kv: (-len(kv[1]), kv[0]))][:TOP_ITEMS]
    top_ids = sorted({i for t in tops.values() for i in t})
    # The members, their posts, the top items' map rows and the suppression list, read together.
    fast.start(store, "creators_by_id", everyone)
    fast.start(store, "window_posts", everyone, page.market, page.start, page.end)
    fast.start(store, "map_items", top_ids)
    fast.start(store, "suppressed_creators")
    creators = {c["creator_id"]: c for c in store.creators_by_id(everyone) or []}
    posts = _one_per_post(store.window_posts(everyone, page.market, page.start, page.end) or [])
    # Read through the suppression list: a suppressed creator's own item is never named by its label or key.
    labels = {r["item_id"]: r.get("label") or r.get("canonical_key")
              for r in without_hidden(store.map_items(top_ids) or [], hidden_people(store))}

    out = []
    for n, (members, shared) in enumerate(found):
        top = tops[n]
        cid = hashlib.sha256((page.market + ":" + ",".join(sorted(top))).encode()).hexdigest()
        cards = {i: page.card(i) for i in top}
        if not page.sensitive_known:
            cards = {i: c and _unnamed(c) for i, c in cards.items()}
        names = [n for n in ((cards[i] or {}).get("title") or labels.get(i) or i for i in top[:LABEL_ITEMS])
                 if not _ID_LIKE.fullmatch(str(n))] or [UNNAMED_TOPICS]
        mine = [p for p in posts if p["creator_id"] in members]
        langs = {}
        for p in mine:
            for lang in p.get("langs") or []:
                langs[lang] = langs.get(lang, 0) + 1
        named = [creators[c] for c in members if c in creators and creators[c].get("tier") in PAGE_TIERS
                 and c not in page.suppressed]
        named.sort(key=lambda c: (-(c.get("followers") or 0), c["creator_id"]))
        examples = sorted((p for p in mine if p["creator_id"] in {c["creator_id"] for c in named}
                           and not set(p["item_ids"]) & page.never_named),
                          key=lambda p: (p.get("published_at") or "", p["post_id"]), reverse=True)[:EXAMPLE_POSTS]
        body = {
            "community_id": cid, "method": "shared_items", "label": ", ".join(names),
            "creators": page.fig(len(members), "creators", "q_communities",
                                 [{"market": page.market, "start": page.start, "end": page.end,
                                   "members": sorted(members), "top_items": top}]),
            "members": [{"creator_id": c["creator_id"], "platform": canon_platform(c.get("platform")),
                         "handle": c.get("handle"), "tier": c.get("tier"),
                         "profile_url": profile_url(c.get("platform"), c.get("handle"))} for c in named]
            if page.sensitive_known else None,
            "top_items": [cards[i] for i in top if cards[i] is not None],
            "platforms": sorted({canon_platform(p["platform"]) for p in mine if p.get("platform")}),
            "languages": [{"lang": lang, "posts": page.fig(k, f"posts in {WINDOW_DAYS} days", "q_community_languages",
                                                           [{"community_id": cid, "lang": lang, "posts": k}])}
                          for lang, k in sorted(langs.items(), key=lambda lk: (-lk[1], lk[0]))] or None,
            "example_posts": [post_evidence(p) for p in examples] if page.sensitive_known else None,
        }
        out.append((members, body))
    return sorted(out, key=lambda mb: (-len(mb[0]), mb[1]["community_id"]))


def _unnamed(card):
    """Contract 12.1: before the sensitive set is complete a community names no one, so a topic card keeps no
    evidence or thumbnails, and a title built from a post's author ("TikTok post by @x") drops the author."""
    title = card.get("title")
    if isinstance(title, str):
        title = _BY_HANDLE.sub("", title)
    return dict(card, title=title, evidence=[], thumbnails=[])


def _one_per_post(rows):
    """The post read joins enrichment, which can hold more than one row a post (an embedding row beside the
    language and format row); each post counts once, with the languages and formats of all its rows."""
    out = {}
    for r in rows:
        kept = out.get(r["post_id"])
        if kept is None:
            out[r["post_id"]] = dict(r)
            continue
        for key in ("langs", "formats"):
            extra = [v for v in r.get(key) or [] if v not in (kept.get(key) or [])]
            if extra:
                kept[key] = list(kept.get(key) or []) + extra
    return list(out.values())


def _list_head(page):
    return {**page.head(), "method": "shared_items", "interaction": INTERACTION,
            "note": None if page.sensitive_known else NO_SENSITIVE_SET}


def build_communities(store, market):
    page = _Page(store, market, edges=True)
    return {**_list_head(page), "communities": [body for _, body in _communities(page)]}


def build_community(store, community_id, market=None):
    for m in [_market(market)] if market is not None else MARKETS:
        page = _Page(store, m)
        for _, body in _communities(page):
            if body["community_id"] == community_id:
                return {**_list_head(page), "community": body}
    raise NotFound(NO_COMMUNITY)
