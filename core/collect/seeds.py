"""Seeds for the collect job's expansion phase, the collect side of BUILD.md task 2.4 (SOURCES.md "The
morning seed loop" and "Keeping it open and unbiased"; DATA.md section 5).

read() takes the seed_queue rows of the three markets from the LOOKBACK_DAYS before the run date with one
parameterised SELECT. live() keeps the seeds the run may use: rows with no yield yet, in lane expansion,
exploration or anchor, with a query, no rule 1 word (gdelt.blocked) and a seed_date from the run date back
to ttl_days minus one day before it (ttl 3 on 30 September: the 28th to the 30th). One seed per market
and cluster survives, the newest and then the highest priority. An anchor expires once its last
ANCHOR_MISSES uses found no post. record() lists each market and cluster's past uses, newest first.

A cluster is a query folded to lower-case letters and digits, so "Cyril Ramaphosa" and #cyrilramaphosa
are one cluster. seed_queue carries no cluster id yet; once detection writes one, cluster() should read it.

mix() fills the expansion slots of rows 14 (tiktok/search/top, cost14 each) and 16 (search/multi,
cost16 each) that the job already pays for; seeds take slots, they never add calls. Today's harvest
hashtags, live queue seeds and rotated research terms are taken in turn, each in its own order (harvest
rank, queue priority). A seed with template search/multi goes to row 16, any other to row 14, and an
exploration seed always to row 14. Row 16 fills first, then the main part of row 14, then exploration:
floor(explore_share of row 14) picks from below the cut (harvest left over, queue seeds not taken and the
queue's exploration seeds), chosen by Thompson sampling: each candidate draws from Beta(1 + hits,
1 + misses) over its recorded uses, a hit being a use that found a creator the harvest had not shown, and
the highest draws win, so an untried candidate draws from a flat prior. Exploration keeps that order: the
soft caps below do not reorder it, since exploration is itself the diversity mechanism.

Two caps are hard. Anchors (the old fixed term pools) hold at most ANCHOR_SHARE of the planned expansion
credits. A cluster holds at most MAX_SHARE of the credits of rows 14 and 16, which job.expansion_calls
enforces after the mix. Two are soft, at MAX_SHARE of the planned expansion credits: the platform a
harvest hashtag was mostly seen on, and its source family (the harvest row that saw it: feed, board,
chart, forum, panel, location; queue seeds are family seed_queue, anchors family anchor). A pick that
would pass a soft cap waits while any pick that would not remains; when none remains the slot still
fills, so a pool with little variety spends the same credits and the weekly drift report shows it.

yield_rows() turns one market's run into seed_queue rows, one per cluster and lane used: yield_posts is the
distinct posts its calls returned, yield_new_creators the creators among them that no harvest, panel or
feed call of that market's run had shown, and credits_estimate is NULL. Actual charges remain in the ledger.
Research terms retain their source in seed_key and do not produce seed_queue rows.
ttl_days 0 keeps a yield row out of live() for ever. The job appends them; no seed row is ever changed.
"""

import copy
import math
import re
from dataclasses import dataclass
from datetime import date

from core.collect.writers import _query, table

LOOKBACK_DAYS = 28
LANES = ("expansion", "exploration", "anchor", "placebo")
TEMPLATES = ("search/multi", "tiktok/search/top", "tiktok/search/hashtag", "tiktok/song/videos",
             "instagram/audio/reels", "tiktok/profile/videos", "twitter/user/tweets", "facebook/profile/posts")
PLACEBO_ROW = "23c"
ANCHOR_SHARE = 0.15
MAX_SHARE = 0.25
ANCHOR_MISSES = 3
RESEARCH_SOURCE = "research_r2"
RESEARCH_TERMS_PER_MARKET = 40
RESEARCH_CALLS_PER_MARKET = 1
MULTI = "search/multi"
EXPANSION_ROWS = ("14", "16")
READ_OK = ("ok", "empty", "cached")
ROW_FAMILY = {"1": "feed", "2": "board", "3": "chart", "4": "chart", "6": "chart", "7": "forum", "8": "panel",
              "23": "panel", "23a": "panel", "5": "location", "23b": "x_trends"}
FOLD = re.compile(r"[\W_]+")

READ_SQL = (
    "SELECT q.seed_date, q.market, q.item_id, q.query, q.kind, q.lane, q.priority, q.template, q.ttl_days,\n"
    "  q.credits_estimate, q.yield_posts, q.yield_new_creators\n"
    "FROM `{table}` q\n"
    "WHERE q.seed_date BETWEEN DATE_SUB(@d, INTERVAL {days} DAY) AND @d AND q.market IN UNNEST(@markets)"
)


@dataclass
class Pick:
    query: str
    lane: str
    family: str
    cluster: str
    platform: str | None = None
    item_id: str | None = None
    kind: str = "hashtag"
    seed_date: date | None = None
    priority: float | None = None
    row: str | None = None
    template: str | None = None
    cost: float | None = None
    source: str | None = None

    @property
    def seed_key(self):
        return f"{self.source}:{self.query}" if self.source == RESEARCH_SOURCE else self.item_id or self.query


def cluster(query):
    return FOLD.sub("", str(query or "").casefold())


def _day(value):
    return date.fromisoformat(value[:10]) if isinstance(value, str) else value


def _queued(row):
    return row.get("yield_posts") is None and row.get("yield_new_creators") is None and \
        row.get("credits_estimate") is not None


def read(bq, run_date, markets):
    from google.cloud import bigquery

    sql = READ_SQL.format(table=table("seed_queue"), days=LOOKBACK_DAYS)
    rows = _query(bq, sql, [bigquery.ScalarQueryParameter("d", "DATE", run_date),
                            bigquery.ArrayQueryParameter("markets", "STRING", list(markets))])
    return [dict(r.items()) for r in rows]


def record(rows):
    """(market, cluster) -> the yield rows of its past uses, newest first."""
    out = {}
    for r in rows:
        if not _queued(r):
            out.setdefault((r["market"], cluster(r["query"])), []).append(r)
    for uses in out.values():
        uses.sort(key=lambda u: _day(u["seed_date"]), reverse=True)
    return out


def _expired(uses):
    recent = [u for u in uses if u.get("yield_posts") is not None][:ANCHOR_MISSES]
    return len(recent) == ANCHOR_MISSES and not any(u["yield_posts"] for u in recent)


def live(rows, run_date, markets=("ZA", "NG", "KE")):
    """market -> the run's seeds, highest priority first."""
    from core.collect.gdelt import blocked

    uses = record(rows)
    best = {}
    for r in rows:
        query, ttl, day = str(r.get("query") or "").strip(), r.get("ttl_days"), _day(r.get("seed_date"))
        if not _queued(r) or r.get("lane") not in LANES or r.get("template") not in TEMPLATES or \
                not query or not ttl or day is None:
            continue
        if not 0 <= (run_date - day).days < ttl or blocked(query) or r.get("market") not in markets:
            continue
        key = (r["market"], r["lane"] == "placebo", cluster(query))
        if r["lane"] == "anchor" and _expired(uses.get((r["market"], cluster(query)), [])):
            continue
        rank = (day, r.get("priority") or 0.0)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, dict(r, query=query, seed_date=day))
    out = {m: [] for m in markets}
    for (market, _, _), (_, s) in best.items():
        out[market].append(s)
    for market in out:
        out[market].sort(key=lambda s: (-(s.get("priority") or 0.0), -s["seed_date"].toordinal(), s["query"]))
    return out


def research_rows(terms, day, markets=("ZA", "NG", "KE")):
    """Build today's rotating research candidates for the existing row 14 queue."""
    rows = []
    for market in markets:
        available = [term for term in terms if term.get("market") == market and term.get("active") is True
                     and term.get("source") == RESEARCH_SOURCE and term.get("location_evidence") is False]
        available = available[:RESEARCH_TERMS_PER_MARKET]
        if not available:
            continue
        start = day.toordinal() % len(available)
        ordered = available[start:] + available[:start]
        for position, term in enumerate(ordered):
            rows.append({"seed_date": day, "market": market, "item_id": None, "query": term["term"],
                         "kind": "topic", "lane": "expansion", "priority": -position,
                         "template": "tiktok/search/top", "ttl_days": 1, "credits_estimate": 1.0,
                         "yield_posts": None, "yield_new_creators": None, "source": RESEARCH_SOURCE})
    return rows


def placeholders(market, day):
    """Stand-in seeds for --plan: two news seeds for row 16, a topic for row 14, exploration and anchors."""
    def fake(n, lane, template):
        return {"seed_date": day, "market": market, "item_id": f"topic|<{market} {lane} seed {n}>",
                "query": f"<{market} {lane} seed {n}>", "kind": "topic", "lane": lane, "priority": 1.0 / n,
                "template": template, "ttl_days": 3, "credits_estimate": 5.0 if template == MULTI else 1.0,
                "yield_posts": None,
                "yield_new_creators": None}

    return [fake(1, "expansion", MULTI), fake(2, "expansion", MULTI), fake(3, "expansion", "tiktok/search/top"),
            fake(1, "exploration", MULTI), fake(1, "anchor", "tiktok/search/top"),
            fake(2, "anchor", "tiktok/search/top")]


def _interleave(*pools):
    out = []
    for i in range(max((len(p) for p in pools), default=0)):
        out += [p[i] for p in pools if i < len(p)]
    return out


def _placed(pick, row, lane=None, cost=None):
    pick = copy.copy(pick)
    pick.row = row
    if lane and pick.template is None:
        pick.lane = lane
    if cost is not None:
        pick.cost = cost
    return pick


def mix(ranked, queue, uses, rng, *, slots14, slots16, cost14, cost16, explore_share=0.15, hold=None):
    """Pick expansion, exploration, anchor and placebo calls within the row 14 and 16 credit holds."""
    from core.collect.gdelt import blocked

    hold = hold or (lambda row: cost16 if row.get("template") == MULTI else cost14)
    origin = ranked.get("origin", {})
    harvest = []
    for tag in ranked.get("hashtag", []):
        if blocked(tag):
            continue
        platform, row = origin.get(tag, (None, None))
        harvest.append(Pick(tag, "expansion", ROW_FAMILY.get(row, "harvest"), cluster(tag), platform))
    by = {(row, lane): [] for row in ("14", "16") for lane in ("expansion", "anchor", "exploration")}
    placebo_pool = []
    for s in queue:
        if blocked(s["query"]):
            continue
        lane = s["lane"]
        cost = hold(s)
        pick = Pick(s["query"], lane, "anchor" if lane == "anchor" else "seed_queue", cluster(s["query"]), None,
                    s.get("item_id"), s.get("kind") or "topic", _day(s.get("seed_date")), s.get("priority"),
                    template=s.get("template"), cost=cost, source=s.get("source"))
        if lane == "placebo":
            placebo_pool.append(pick)
            continue
        row = "16" if pick.template == MULTI and lane != "exploration" else "14"
        by[(row, lane)].append(pick)
    pool16 = _interleave(harvest, by[("16", "expansion")], by[("16", "anchor")])
    pool14 = _interleave(harvest, by[("14", "expansion")], by[("14", "anchor")])
    explore_pool = by[("14", "exploration")]
    n16 = min(slots16, len({p.cluster for p in pool16}))
    normal14 = {p.cluster for p in pool14 + explore_pool if p.source != RESEARCH_SOURCE}
    research14 = {p.cluster for p in pool14 + explore_pool if p.source == RESEARCH_SOURCE} - normal14
    n14 = min(slots14, len(normal14) + min(RESEARCH_CALLS_PER_MARKET, len(research14)))
    pool14 = [p for p in pool14 if p.source != RESEARCH_SOURCE or p.cluster not in normal14]
    explore_pool = [p for p in explore_pool if p.source != RESEARCH_SOURCE or p.cluster not in normal14]
    explore = math.floor(explore_share * n14)
    budget = n14 * cost14 + n16 * cost16
    spent = {}
    research_selected = 0

    def price(pick, row):
        return pick.cost if pick.template is not None and pick.cost is not None else \
            (cost16 if row == "16" else cost14)

    def paid(group, cost):
        return spent.get(group, 0) + cost

    def hard(p, cost, taken):
        return p.cluster not in taken and (p.source != RESEARCH_SOURCE or
                                           research_selected < RESEARCH_CALLS_PER_MARKET) and \
            (p.lane != "anchor" or paid("anchor", cost) <= ANCHOR_SHARE * budget)

    def soft(p, cost):
        return (p.platform is None or paid(("platform", p.platform), cost) <= MAX_SHARE * budget) and \
            paid(("family", p.family), cost) <= MAX_SHARE * budget

    def fill(pool, n, row, cost, taken, lane=None, credits=None):
        nonlocal research_selected
        out, pool, used = [], list(pool), 0.0
        limit = n * cost if credits is None else credits
        while len(out) < n:
            free = [p for p in pool if used + price(p, row) <= limit and hard(p, price(p, row), taken)]
            if not free:
                break
            pick = free[0] if lane else next((p for p in free if soft(p, price(p, row))), free[0])
            cost = price(pick, row)
            pool.remove(pick)
            taken.add(pick.cluster)
            for group in ("anchor" if pick.lane == "anchor" else None, ("platform", pick.platform),
                          ("family", pick.family)):
                spent[group] = paid(group, cost)
            out.append(_placed(pick, row, lane, cost))
            used += cost
            research_selected += int(pick.source == RESEARCH_SOURCE)
        return out, used

    share14, share16 = slots14 * cost14, slots16 * cost16
    room = {"14": share14, "16": share16}
    placebo, cut = [], []
    for pick in placebo_pool:
        cost = price(pick, "16" if pick.template == MULTI else "14")
        if cost > room["14"] + room["16"]:
            cut.append((_placed(pick, PLACEBO_ROW, cost=cost),
                        f"the expansion share of {share14 + share16:g} credits cannot take its {cost:g} hold"))
            continue
        first, other = ("16", "14") if pick.template == MULTI else ("14", "16")
        from_first = min(cost, room[first])
        room[first] -= from_first
        room[other] -= cost - from_first
        placebo.append(_placed(pick, PLACEBO_ROW, cost=cost))

    row16, _ = fill(pool16, n16, "16", cost16, set(), credits=room["16"])
    taken14 = set()
    main14, used14 = fill(pool14, n14 - explore, "14", cost14, taken14, credits=room["14"])
    left = [p for p in pool14 if p.cluster not in taken14 and p.lane != "anchor" and
            p.source != RESEARCH_SOURCE] + [p for p in explore_pool if p.source != RESEARCH_SOURCE]
    draws = []
    for i, p in enumerate(left):
        past = uses.get(p.cluster, [])
        hits = sum(1 for u in past if (u.get("yield_new_creators") or 0) > 0)
        draws.append((-rng.betavariate(1 + hits, 1 + len(past) - hits), i, p))
    ordered = [p for _, _, p in sorted(draws, key=lambda d: (d[0], d[1]))]
    explored, _ = fill(ordered, explore, "14", cost14, taken14, lane="exploration",
                       credits=max(0.0, room["14"] - used14))
    return {"14": main14 + explored, "16": row16, "placebo": placebo, "cut": cut}


def yield_rows(day, market, done, known=frozenset()):
    """One seed_queue row per cluster and lane used in one market's run. done holds (call, result, parsed)
    in run order; a call carries its Pick as call.seed (None outside rows 14 and 16)."""
    shown = {p.get("creator_id") for call, _, parsed in done if getattr(call, "seed", None) is None
             for p in (parsed or {}).get("posts", [])}
    groups = {}
    for call, result, parsed in done:
        pick = getattr(call, "seed", None)
        if pick is None or pick.source == RESEARCH_SOURCE or result.status not in READ_OK:
            continue
        g = groups.setdefault((pick.cluster, pick.lane), {"pick": pick, "routes": set(), "posts": set(),
                                                           "creators": set()})
        g["routes"].add(call.route)
        for post in (parsed or {}).get("posts", []):
            g["posts"].add(post["post_id"])
            g["creators"].add(post.get("creator_id"))
    rows = []
    for g in groups.values():
        pick = g["pick"]
        new_posts = None if known is None else len(g["posts"] - set(known))
        rows.append({"seed_date": day.isoformat(), "market": market, "item_id": pick.item_id, "query": pick.query,
                     "kind": pick.kind, "lane": pick.lane, "priority": pick.priority,
                     "template": ",".join(sorted(g["routes"])), "ttl_days": 0, "credits_estimate": None,
                     "yield_posts": new_posts, "yield_new_creators": len(g["creators"] - shown - {None})})
    return rows
