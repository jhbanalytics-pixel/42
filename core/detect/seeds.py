"""The seed loop, detect side (BUILD.md 2.4, DATA.md section 5, SOURCES.md "The morning seed loop" and
"Keeping it open and unbiased", TRUST.md A2).

run_seeds(client, d, detect_run_id) appends tomorrow's seed_queue rows (seed_date d + 1) in four lanes and returns
counts. It reads today's states from the item_state rows of detect run detect_run_id, which is still open while
seeds runs inside it.
Nothing is replaced or removed: one INSERT adds rows the queue does not hold yet, so a rerun adds nothing.

Budget. B = (MONTHLY minus the month's spend minus the month's reserve) / days left, never above collect's
share of ENGINE_DAILY (all three read from core/config/caps.yaml, which mirrors the SETUP.md caps table). Month
and days left are the seed day's, the seed day included; spend is every credit_ledger charge from the first of
that month to d (the quote where no charge is recorded). The reserve is confirm's share and the reserve share
for each day left, so confirm is never paid from B.
Of B, this step queues: exploration, 10% of the expansion share; expansion, 30% of B, which also pays for the
anchor slice (at most 15% of it) and the placebo expansions (about 5% of its calls). Measured lanes and depth
are collect's own and never queued here. Rows already queued for the seed day count against their lane's share.

Lanes, in the order DATA.md 5 reserves them:
- exploration: Thompson sampling over items ranked p40 to p80. Each item draws from Beta(1 + risen, 1 + not
  risen), counting the exploration seeds of its kind whose yield collect recorded and whose 7-day window has
  closed, plus the item's own; a seed has risen when it found posts or new creators and the item was Rising in
  that market within 7 days of the seed day. Draws are seeded by the seed day.
- anchor: an anchor whose last run yielded posts is queued again; one that yielded nothing expires.
- expansion: items at worth_pct 0.8 or more in New to 42, Emerging (priority x 1.3), Spike, Rising (x 1.0)
  or Peaking (x 0.5), x 1.2 when novelty is new. An item expanded in that market on d or d - 1 is skipped
  unless its main series' acceleration is above 0. Rising items at p90 or more are also probed in the other
  two markets at 0.6 x their priority, where that market has no row for the item today.
- placebo: about 5% of each market's expansion calls (placebo_calls: 5% rounded from 20 calls up, below that
  one call with probability 5% of the calls), on the first items of that market's below-p40 pool shuffled by
  a generator seeded by the seed day and market. Only the never-seed filters apply, not the quota, so a full
  platform cannot bend the draw. The reserve is those items' own credits, taken before expansion; the draw
  stops when expansion credits run out. tvf_placebo_base reads these rows.

Selection, per lane: priority order (ties in a market order that rotates by seed day), each call charged its route's 28-day ledger cost per call (DEFAULT_COST
from the SOURCES.md costed table when the ledger has none), first until each market has used a third of the
lane's credits, then a global pass over the leftovers. Every row from the expansion share and exploration
counts toward one quota: no item, platform or language cluster above 25% of the expansion share. A
search/multi call splits its credits evenly over its platforms. An item with no known language is in cluster
'und', which is not a language and has no language quota; its item and platform quotas still apply. Placebo
rows count toward the quota but are never refused by it.

Never seeded: items with status generic or on the stoplist, ineligible items (item_state.eligible false, so
no worth_pct), items already queued for the seed day in that market, and any row whose template, kind or
query names Google Trends or carries an age or generation term (core/trust/claims.py K6 terms).

drift_report(client, d) gives Herfindahl concentration by kind, platform and language of the week's earned
seeds (credit-weighted) against the unseeded rank feeds (post-weighted), and und_share, the share of seed
credits with no known language. run_seeds calls it on Mondays only.

Contract for the collect job (lane L1), which reads the queue on the seed day:
- Read intelligence_42_core.seed_queue rows with seed_date <= the run day and
  DATE_ADD(seed_date, INTERVAL IFNULL(ttl_days, 1) - 1 DAY) >= the run day, lane in expansion, exploration,
  anchor or placebo, yield_posts and yield_new_creators both NULL and credits_estimate set. Those are the
  queued seeds; the rest are yield rows.
- template is the SocialCrawl route and query its main argument: the hashtag for tiktok/search/hashtag, the
  sound id for tiktok/song/videos and instagram/audio/reels, the handle for tiktok/profile/videos,
  twitter/user/tweets and facebook/profile/posts, and the search text for search/multi, on twitter, threads
  and reddit when kind is brand or event and on MULTI_PLATFORMS otherwise.
- Run each lane in priority order: exploration from row 14's share, expansion, anchor and placebo from the
  expansion share. Run placebo rows in full, as expansion is run, or the TRUST.md A2 base rate is biased.
- Call through socialcrawl_client with the row's market, lane as the lane and item_id (or query when item_id
  is NULL) as seed_key; sightings are written with that lane and lane_class search_presence.
- After a seed runs, APPEND one separate yield row to seed_queue with the seed's seed_date, market, item_id,
  query, kind, lane and template, priority, ttl_days and credits_estimate NULL, and yield_posts (posts the call
  returned that were new to posts) and yield_new_creators (creators new to creators) set. Never update the
  queued row: the queue is append-only. read_trials reads MAX(yield_*) per seed; drift skips rows with no
  credits.
- credit_ledger rows carry route (or endpoint), calls and lane, so the 28-day cost per call is right.
"""

import random
import re
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from . import aggregate, sqlrun
from .items import is_generic
from .sqlrun import AGENT, CORE, query
from core.collect.socialcrawl_client import load_caps
from core.trust.claims import _breach_term

SQL = Path(__file__).parent / "sql" / "seeds.sql"
_NAME = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.MULTILINE)

MARKETS = ("ZA", "NG", "KE")
_CAPS = load_caps()                                           # core/config/caps.yaml, the SETUP.md caps table
MONTHLY = _CAPS["MONTHLY"]["total"]
COLLECT, CONFIRM, RESERVE = (_CAPS["ENGINE_DAILY"][k] for k in ("collect", "confirm", "reserve"))
EXPANSION, EXPLORATION, ANCHOR, PLACEBO, QUOTA = 0.30, 0.10, 0.15, 0.05, 0.25
TTL_DAYS = 1
STATE_WEIGHT = {"new_to_42": 1.3, "emerging": 1.3, "spike": 1.0, "rising": 1.0, "peaking": 0.5}
NEW_WEIGHT, CROSS_MARKET = 1.2, 0.6
EXPAND_AT, CROSS_AT, EXPLORE_FROM, PLACEBO_BELOW = 0.8, 0.9, 0.4, 0.4
EPS = 1e-9

MULTI_PLATFORMS = ("instagram", "youtube", "reddit", "twitter", "threads", "facebook")
NEWS_PLATFORMS = ("twitter", "threads", "reddit")
SOUND_ROUTES = {"tiktok": "tiktok/song/videos", "instagram": "instagram/audio/reels"}
CREATOR_ROUTES = {"tiktok": "tiktok/profile/videos", "twitter": "twitter/user/tweets",
                  "facebook": "facebook/profile/posts"}
DEFAULT_COST = {"search/multi": 5.0, "tiktok/search/hashtag": 1.0, "tiktok/song/videos": 1.0,
                "instagram/audio/reels": 1.0, "tiktok/profile/videos": 1.0, "twitter/user/tweets": 1.0,
                "facebook/profile/posts": 1.0}
FUSED = ("genz", "genalpha", "millennial", "zoomer")          # fused hashtag forms the K6 terms miss
LANG_CLUSTERS = {"sheng": "sw"}

SEED_FIELDS = (("seed_date", "DATE"), ("market", "STRING"), ("item_id", "STRING"), ("query", "STRING"),
               ("kind", "STRING"), ("lane", "STRING"), ("priority", "FLOAT64"), ("template", "STRING"),
               ("ttl_days", "INT64"), ("credits_estimate", "FLOAT64"))


def statements():
    """The named statements of seeds.sql, dataset placeholders left in."""
    out = {}
    for piece in sqlrun.split(sqlrun.for_authority(SQL.read_text(encoding="utf-8"))):
        m = _NAME.search(piece)
        out[m.group(1)] = sqlrun._strip_leading_comments(piece[m.end():])
    return out


def budget(spent, seed_date):
    month_end = (seed_date.replace(day=28) + timedelta(days=4)).replace(day=1)
    days_left = (month_end - seed_date).days
    b = (MONTHLY - spent - days_left * (CONFIRM + RESERVE)) / days_left
    return max(0.0, min(float(COLLECT), b))


def shares(b):
    e = EXPANSION * b
    return {"budget": b, "expansion": e, "exploration": EXPLORATION * e, "anchor": ANCHOR * e, "quota": QUOTA * e}


def template(kind, key, label, keywords=None, local_terms=None):
    """(route, query) for an item, or None when no route reads its kind on its platform."""
    if kind == "hashtag":
        return "tiktok/search/hashtag", key
    if kind in ("sound", "creator"):
        platform, _, ident = key.partition(":")
        route = (SOUND_ROUTES if kind == "sound" else CREATOR_ROUTES).get(platform)
        return (route, ident) if route and ident else None
    if kind in ("brand", "event"):
        return "search/multi", label or key
    terms = []
    for t in list(keywords or [])[:3] + list(local_terms or [])[:2]:
        if t and t not in terms:
            terms.append(t)
    return "search/multi", " ".join(terms) if terms else (label or key)


def platforms(route, kind):
    if route == "search/multi":
        return NEWS_PLATFORMS if kind in ("brand", "event") else MULTI_PLATFORMS
    return (route.split("/")[0],)


def lang_cluster(code):
    c = (code or "").strip().casefold()
    return LANG_CLUSTERS.get(c, c) or "und"


def allowed(row):
    """False when the template, kind or query names Google Trends or carries an age or generation term."""
    for text in (row.get("template"), row.get("kind"), row.get("query")):
        text = (text or "").replace("_", " ")
        if _breach_term(text, frozenset()):
            return False
        folded = re.sub(r"\W+", "", text.casefold())
        if any(f in folded for f in FUSED):
            return False
    return True


class Quota:
    """Credits taken per item, platform and language cluster, each held to one limit."""

    def __init__(self, limit):
        self.limit = limit
        self.used = defaultdict(float)

    def copy(self):
        other = Quota(self.limit)
        other.used.update(self.used)
        return other

    @staticmethod
    def _parts(row):
        c, ps = row["credits_estimate"], row["platforms"]
        parts = [(("item", row["item_id"] or row["query"]), c)] + [(("platform", p), c / len(ps)) for p in ps]
        return parts + ([] if row["lang"] == "und" else [(("language", row["lang"]), c)])

    def fits(self, row):
        return all(self.used[k] + v <= self.limit + EPS for k, v in self._parts(row))

    def take(self, row):
        for k, v in self._parts(row):
            self.used[k] += v


def market_order(seed_date):
    """MARKETS rotated by the seed day, so no market always claims shared quotas first."""
    k = seed_date.toordinal() % len(MARKETS) if seed_date else 0
    return MARKETS[k:] + MARKETS[:k]


def greedy(rows, credits, quota, seed_date=None):
    """Rows in priority order (ties in the seed day's market order), each market up to a third of credits,
    then a global pass up to credits."""
    rank = {m: i for i, m in enumerate(market_order(seed_date))}
    order = sorted(rows, key=lambda r: (-r["priority"], rank.get(r["market"], len(rank)), r["item_id"] or "",
                                        r["query"]))
    third = credits / len(MARKETS)
    spent, total, picked = defaultdict(float), 0.0, {}
    for global_pass in (False, True):
        for i, r in enumerate(order):
            c = r["credits_estimate"]
            room = total + c <= credits + EPS if global_pass else spent[r["market"]] + c <= third + EPS
            if i in picked or not room or not quota.fits(r):
                continue
            quota.take(r)
            spent[r["market"]] += c
            total += c
            picked[i] = r
    return list(picked.values())


def placebo_calls(n, seed_date, market):
    """Placebo calls for a market with n expansion calls: 5% of them, rounded, from 20 calls up; below 20, one
    call with probability 5% of n from a generator seeded by the seed day and market, else none."""
    if n >= 20:
        return int(PLACEBO * n + 0.5)
    return int(random.Random(f"placebo-n|{seed_date.isoformat()}|{market}").random() < PLACEBO * n)


def posterior(trials):
    """Beta parameters on "Rising within 7 days": per kind from Beta(1, 1), and each item's own record."""
    kind, item = defaultdict(lambda: [1, 1]), defaultdict(lambda: [0, 0])
    for t in trials:
        i = 0 if t["success"] else 1
        kind[t["kind"]][i] += 1
        item[(t["market"], t["item_id"])][i] += 1
    return {"kind": {k: tuple(v) for k, v in kind.items()}, "item": {k: tuple(v) for k, v in item.items()}}


def thompson(pool, post, seed_date):
    """One Beta draw per (market, item_id), repeatable for a seed day."""
    rng = random.Random(f"exploration|{seed_date.isoformat()}")
    out = {}
    for c in sorted(pool, key=lambda c: (c["market"], c["item_id"])):
        a, b = post["kind"].get(c["kind"], (1, 1))
        s, f = post["item"].get((c["market"], c["item_id"]), (0, 0))
        out[(c["market"], c["item_id"])] = rng.betavariate(a + s, b + f)
    return out


def _row(c, lane, priority, seed_date):
    return {"seed_date": seed_date, "market": c["market"], "item_id": c["item_id"], "query": c["query"],
            "kind": c["kind"], "lane": lane, "priority": priority, "template": c["template"],
            "ttl_days": TTL_DAYS, "credits_estimate": c["credits_estimate"], "platforms": c["platforms"],
            "lang": c["lang"]}


def _annotate(c, cost):
    """The candidate with its template, query, credits, platforms and language cluster, or None if never seeded."""
    if is_generic(c["kind"], c["canonical_key"]):
        return None
    t = template(c["kind"], c["canonical_key"], c["label"], c.get("keywords"), c.get("local_terms"))
    if t is None or not cost.get(t[0]):
        return None
    out = {**c, "template": t[0], "query": t[1], "credits_estimate": cost[t[0]],
           "platforms": platforms(t[0], c["kind"]), "lang": lang_cluster(c.get("lang"))}
    return out if allowed(out) else None


def _expansion(items, recent, queued, present, seed_date):
    own = {}
    for c in items:
        if c["worth_pct"] < EXPAND_AT or c["state"] not in STATE_WEIGHT:
            continue
        if (c["market"], c["item_id"]) in recent and not (c["accel"] or 0) > 0:
            continue
        p = c["worth_pct"] * STATE_WEIGHT[c["state"]] * (NEW_WEIGHT if c["novelty"] == "new" else 1.0)
        own[(c["market"], c["item_id"])] = _row(c, "expansion", p, seed_date)
    probes = {}
    for c in items:
        if (c["market"], c["item_id"]) not in own or c["state"] != "rising" or c["worth_pct"] < CROSS_AT:
            continue
        p = CROSS_MARKET * own[(c["market"], c["item_id"])]["priority"]
        for m in MARKETS:
            key = (m, c["item_id"])
            if key in present or key in queued or (key in recent and not (c["accel"] or 0) > 0):
                continue
            if key not in probes or probes[key]["priority"] < p:
                probes[key] = _row({**c, "market": m}, "expansion", p, seed_date)
    return list(own.values()) + list(probes.values())


def _anchors(queue, seed_date, cost, queued):
    runs = defaultdict(list)
    for q in queue:
        if q["lane"] == "anchor":
            runs[(q["market"], q["item_id"], q["query"])].append(q)
    out = []
    for (market, iid, text), rs in runs.items():
        last = max(r["seed_date"] for r in rs)
        at_last = [r for r in rs if r["seed_date"] == last]
        ttl = max(r["ttl_days"] or 1 for r in at_last)
        if max(r["yield_posts"] or 0 for r in at_last) <= 0 or last + timedelta(days=ttl) > seed_date:
            continue
        if (market, iid or text) in queued:
            continue
        route = next((r["template"] for r in at_last if r["template"]), None)
        kind = next((r["kind"] for r in at_last if r["kind"]), None)
        if not cost.get(route):
            continue
        row = {"seed_date": seed_date, "market": market, "item_id": iid, "query": text, "kind": kind,
               "lane": "anchor", "priority": next((r["priority"] for r in at_last if r["priority"] is not None), 0.0),
               "template": route, "ttl_days": TTL_DAYS, "credits_estimate": cost[route],
               "platforms": platforms(route, kind), "lang": "und"}
        if allowed(row):
            out.append(row)
    return out


def plan(d, cands, queue=(), trials=(), cost=None, spent=0.0):
    """Tomorrow's seed rows and the shares they were chosen within. cands are read_candidates rows, queue the
    queue statement's rows, trials read_trials rows, cost credits per call by route, spent the month's spend."""
    seed_date = d + timedelta(days=1)
    share = shares(budget(spent, seed_date))
    cost = {**DEFAULT_COST, **{k: v for k, v in (cost or {}).items() if v and v > 0}}
    quota = Quota(share["quota"])
    lang_of = {(c["market"], c["item_id"]): lang_cluster(c.get("lang")) for c in cands}
    used = defaultdict(float)
    queued = set()
    for q in queue:
        if q["seed_date"] != seed_date:
            continue
        queued.add((q["market"], q["item_id"] or q["query"]))
        if q["credits_estimate"]:
            used[q["lane"]] += q["credits_estimate"]
            quota.take({"item_id": q["item_id"], "query": q["query"], "credits_estimate": q["credits_estimate"],
                        "platforms": platforms(q["template"], q["kind"]) if q["template"] else ("unknown",),
                        "lang": lang_of.get((q["market"], q["item_id"]), "und")})
    recent = {(q["market"], q["item_id"]) for q in queue
              if q["lane"] == "expansion" and d - timedelta(days=1) <= q["seed_date"] <= d}
    present = {(c["market"], c["item_id"]) for c in cands}
    items = [a for a in (_annotate(c, cost) for c in cands if (c["market"], c["item_id"]) not in queued) if a]

    pool = [c for c in items if EXPLORE_FROM <= c["worth_pct"] < EXPAND_AT]
    theta = thompson(pool, posterior(trials), seed_date)
    rows = greedy([_row(c, "exploration", theta[(c["market"], c["item_id"])], seed_date) for c in pool],
                  max(0.0, share["exploration"] - used["exploration"]), quota, seed_date)

    left = share["expansion"] - used["expansion"] - used["placebo"] - used["anchor"]
    anchors = greedy(_anchors(queue, seed_date, cost, queued), max(0.0, min(share["anchor"], left)), quota,
                     seed_date)
    left -= sum(r["credits_estimate"] for r in anchors)

    # Placebo: size the reserve from the calls a trial expansion pass implies and the drawn items' own costs,
    # then take the first k items of each market's shuffled below-p40 pool. Only the never-seed filters apply
    # (in _annotate); no quota test, so a full platform cannot bend the draw. Stops when the credits run out.
    wanted = _expansion(items, recent, queued, present, seed_date)
    trial = greedy(wanted, max(0.0, left), quota.copy(), seed_date)
    calls = Counter(r["market"] for r in trial)
    placebo, reserve = [], 0.0
    for m in market_order(seed_date):
        prio = sorted(r["priority"] for r in trial if r["market"] == m)
        low = sorted((c for c in items if c["market"] == m and c["worth_pct"] < PLACEBO_BELOW),
                     key=lambda c: c["item_id"])
        random.Random(f"placebo|{seed_date.isoformat()}|{m}").shuffle(low)
        for c in low[:placebo_calls(calls[m], seed_date, m)]:
            if reserve + c["credits_estimate"] > left + EPS:
                break
            placebo.append(_row(c, "placebo", prio[len(prio) // 2], seed_date))
            reserve += c["credits_estimate"]
        else:
            continue
        break
    for r in placebo:
        quota.take(r)
    expansion = greedy(wanted, max(0.0, left - reserve), quota, seed_date)
    rows += anchors + expansion + placebo
    return {"rows": rows, **{k: share[k] for k in ("budget", "expansion", "exploration")}}


def read_cost(client, d, core=CORE, agent=AGENT):
    return {r["route"]: r["cost_per_call"] for r in query(client, statements()["cost"], {"d": d}, core, agent)}


def read_candidates(client, d, run_id, core=CORE, agent=AGENT):
    """d's candidates from the item_state rows detect run run_id wrote, read while that run is still open."""
    return query(client, statements()["candidates"], {"d": d, "run_id": run_id}, core, agent)


def read_trials(client, d, core=CORE, agent=AGENT):
    return query(client, statements()["trials"], {"d": d}, core, agent)


def hhi(weights):
    total = sum(weights.values())
    return sum((w / total) ** 2 for w in weights.values()) if total > 0 else None


def drift(seed_rows, feed_rows):
    """Herfindahl index by kind, platform and language of seeds (credits) and feeds (posts), and the gap."""
    seeds_w = {dim: defaultdict(float) for dim in ("kind", "platform", "language")}
    feeds_w = {dim: defaultdict(float) for dim in ("kind", "platform", "language")}
    for r in seed_rows:
        c = r["credits_estimate"] or 0.0
        seeds_w["kind"][r["kind"] or "unknown"] += c
        seeds_w["language"][lang_cluster(r["lang"])] += c
        ps = platforms(r["template"], r["kind"]) if r["template"] else ("unknown",)
        for p in ps:
            seeds_w["platform"][p] += c / len(ps)
    for r in feed_rows:
        cat = lang_cluster(r["cat"]) if r["dim"] == "language" else (r["cat"] or "unknown")
        feeds_w[r["dim"]][cat] += r["n"]
    total = sum(seeds_w["language"].values())
    out = {"und_share": seeds_w["language"].get("und", 0.0) / total if total > 0 else None}
    for dim in seeds_w:
        s, f = hhi(seeds_w[dim]), hhi(feeds_w[dim])
        out[dim] = {"seeds": s, "feeds": f, "gap": None if s is None or f is None else s - f}
    return out


def drift_report(client, d, core=CORE, agent=AGENT):
    """The weekly drift report for the seven seed days to d."""
    st = statements()
    seed_rows = query(client, st["drift_seeds"], {"d": d}, core, agent)
    feed_rows = query(client, st["drift_feeds"], {"d": d}, core, agent)
    return {"from": (d - timedelta(days=6)).isoformat(), "to": d.isoformat(), **drift(seed_rows, feed_rows)}


def run_seeds(client, d, detect_run_id, core=CORE, agent=AGENT):
    """Append tomorrow's seeds for run date d from detect run detect_run_id's states and return counts; on
    Mondays also the weekly drift report."""
    st = statements()
    seed_date = d + timedelta(days=1)
    spent = query(client, st["spend"], {"d": d, "month_start": seed_date.replace(day=1)}, core, agent)[0]["spent"]
    out = plan(d, read_candidates(client, d, detect_run_id, core, agent),
               queue=query(client, st["queue"], {"d": d}, core, agent),
               trials=read_trials(client, d, core, agent), cost=read_cost(client, d, core, agent), spent=spent or 0.0)
    rows = out["rows"]
    if rows:
        aggregate._run(client, st["append"], [aggregate._struct_array("rows", rows, SEED_FIELDS)], core, agent)
    credits = defaultdict(float)
    for r in rows:
        credits[r["lane"]] += r["credits_estimate"]
    counts = {"budget": out["budget"], "expansion": out["expansion"], "exploration": out["exploration"],
              "appended": len(rows), "credits": dict(credits)}
    if d.weekday() == 0:
        counts["drift"] = drift_report(client, d, core, agent)
    return counts
