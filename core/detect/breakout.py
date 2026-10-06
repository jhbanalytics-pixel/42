"""The creator-breakout step of detect (BUILD.md 2.13, SOURCES.md rows 10 and 22).

For run date d it reads each creator's posts in the 180 days to d with their first readings as of d
(sql/breakout.sql, query 1). counter_post_views re-reads are never used: 42 chooses which posts it re-reads and
when, so a re-read measures how long the post has been up and 42's choice, not the creator's audience (DATA.md 3.2).

Breakout post: a post published in the 7 days to d whose first reading with views is at least 3 times its
creator's usual views and at least 1,000. Usual views are the median baseline reading of the creator's earlier
posts on the same platform. An earlier post counts only when its baseline reading, its first reading in a
measured lane (unbiased_rank, panel) or on the creator-profile route of SOURCES.md row 10, was taken at least
as long after publishing as the post's own first reading, so a post read early is never judged against
posts read late. A creator with fewer than 5 such posts is not judged. A post with NULL views never counts.

Breakout signal: an item (a sound or a format) in a market where 3 or more unrelated small creators each have
a breakout post using it, published in the 7 days to d and sighted in that market (query 2) by a creator
located there: the post's geo_market is the market at geo_confidence 0.7 or more, or the creator's
home_market is. Small is nano or micro by creator_tier_at_post, a follower count. Unrelated means a distinct handle, folded, so one person on
two platforms counts once; creators.handle is used where the creators table has it, else the creator_id. An
account with coord_score 1 or more is left out entirely: coord_score counts networks without naming them, so
two flagged accounts cannot be shown to be in different networks. A signal row counts the breakout posts it
left out for that reason in held_flagged. Items with status generic or on the stoplist never make a signal.

run_breakout only reads and returns rows. append_signals, which the detect job runs after state under the
breakout step's own run_id, appends the day's signals to intelligence_42_core.breakout_signals. Nothing is
replaced or removed: the INSERT leaves out a signal whose day, market and item the table already holds, so a
rerun on the same day adds nothing. No model is called, so the step spends nothing.

run_creator_surges reads query 1 for the watch step's creator_surge rule (core/detect/watches.py): per creator,
how many of their posts dated d are breakout posts by the same usual views and is_breakout. It sees the posts
query 1 sees: any platform, but usual views come only from baseline readings in the measured lanes or on the
TikTok creator-profile route, so a creator without 5 such earlier posts on that platform is not judged.
"""

import statistics
import unicodedata
from collections import defaultdict
from pathlib import Path

from . import aggregate, sqlrun
from .items import is_generic
from .sqlrun import AGENT, CORE, query

SQL = Path(__file__).parent / "sql" / "breakout.sql"

# One struct per evidence post, its signal's columns repeated and rank its place in evidence_post_ids; the
# INSERT folds them back into one row per signal. A signal always has MIN_CREATORS or more evidence posts.
ROW_FIELDS = (("metric_date", "DATE"), ("market", "STRING"), ("item_id", "STRING"), ("run_id", "STRING"),
              ("creators", "INT64"), ("posts", "INT64"), ("top_ratio", "FLOAT64"), ("held_flagged", "INT64"),
              ("rule_version", "STRING"), ("post_id", "STRING"), ("rank", "INT64"))

APPEND_SQL = """
INSERT INTO {core}.breakout_signals (metric_date, market, item_id, run_id, creators, posts, evidence_post_ids,
  top_ratio, held_flagged, rule_version)
SELECT n.metric_date, n.market, n.item_id, ANY_VALUE(n.run_id), ANY_VALUE(n.creators), ANY_VALUE(n.posts),
  ARRAY_AGG(n.post_id ORDER BY n.rank), ANY_VALUE(n.top_ratio), ANY_VALUE(n.held_flagged),
  ANY_VALUE(n.rule_version)
FROM UNNEST(@rows) n
WHERE NOT EXISTS (
  SELECT 1 FROM {core}.breakout_signals s
  WHERE s.metric_date = n.metric_date AND s.market = n.market AND s.item_id = n.item_id)
GROUP BY n.metric_date, n.market, n.item_id
"""

APPENDED_SQL = "SELECT COUNT(*) n FROM {core}.breakout_signals s WHERE s.metric_date = @d AND s.run_id = @run_id"

MIN_PRIOR = 5
RATIO = 3
MIN_VIEWS = 1000
MIN_CREATORS = 3
SMALL_TIERS = ("nano", "micro")
GEO_CONFIDENCE = 0.7


def queries():
    """The two queries of breakout.sql without their comment lines: the posts query, then the sightings query."""
    lines = SQL.read_text(encoding="utf-8").splitlines()
    return sqlrun.split("\n".join(line for line in lines if not line.startswith("--")))


def usual_views(prior):
    """Median of the earlier posts' views, or None with fewer than MIN_PRIOR of them."""
    return statistics.median(prior) if len(prior) >= MIN_PRIOR else None


def is_breakout(views, usual):
    return views is not None and usual is not None and views >= RATIO * usual and views >= MIN_VIEWS


def _by_creator(posts):
    by_creator = defaultdict(list)
    for p in posts:
        by_creator[(p["platform"], p["creator_id"])].append(p)
    return by_creator


def _usual_for(p, group):
    """Usual views for post p of query 1 against the other posts of its creator and platform (group): the
    median baseline reading of the earlier posts read at least as long after publishing as p's first reading."""
    lag = p["first_read_at"] - p["published_at"]
    return usual_views([o["base_views"] for o in group
                        if o["published_at"] < p["published_at"] and o["base_views"] is not None
                        and o["base_read_at"] - o["published_at"] >= lag])


def _find_breakouts(posts):
    """Breakout rows and eligibility counts for query 1 posts inside its date window."""
    counts = {"input_rows": len(posts), "in_window": 0, "with_first_views": 0,
              "with_five_prior": 0, "at_least_1000_views": 0,
              "at_least_three_times_usual": 0, "breakouts": 0, "small_breakouts": 0,
              "breakout_tiers": {"nano": 0, "micro": 0, "macro": 0, "mega": 0, "other": 0}}
    found = {}
    for group in _by_creator(posts).values():
        for p in group:
            if not p["in_window"]:
                continue
            counts["in_window"] += 1
            if p["first_views"] is None:
                continue
            counts["with_first_views"] += 1
            usual = _usual_for(p, group)
            if usual is None:
                continue
            counts["with_five_prior"] += 1
            if p["first_views"] >= MIN_VIEWS:
                counts["at_least_1000_views"] += 1
            if p["first_views"] >= RATIO * usual:
                counts["at_least_three_times_usual"] += 1
            if is_breakout(p["first_views"], usual):
                found[p["post_id"]] = {"post_id": p["post_id"], "platform": p["platform"],
                                       "creator_id": p["creator_id"], "views": p["first_views"],
                                       "usual_views": usual, "ratio": p["first_views"] / usual if usual else None}
                tier = p.get("tier")
                tier = tier if tier in ("nano", "micro", "macro", "mega") else "other"
                counts["breakout_tiers"][tier] += 1
                if tier in SMALL_TIERS:
                    counts["small_breakouts"] += 1
    counts["breakouts"] = len(found)
    return found, counts


def find_breakouts(posts):
    """Breakout rows for the in-window posts of query 1, keyed by post_id."""
    return _find_breakouts(posts)[0]


def creator_surges(posts, d):
    """{(platform, creator_id): n} from query 1 posts: n is how many of the creator's posts dated d are breakout
    posts (is_breakout against their usual views, as above), 0 when none is. Only creators with a post dated d
    that can be judged (a first reading with views and usual views from 5 or more earlier posts) are listed; any
    other creator's surge is unknown, never 0. Tier, location and co-action do not matter here: this is the
    creator surge of a watch on that creator (contract 10.5), not a breakout signal."""
    out = {}
    for key, group in _by_creator(posts).items():
        for p in group:
            if p.get("post_date") != d or p["first_views"] is None:
                continue
            usual = _usual_for(p, group)
            if usual is not None:
                out[key] = out.get(key, 0) + int(is_breakout(p["first_views"], usual))
    return out


def run_creator_surges(client, d, core=CORE, agent=AGENT):
    """creator_surges for d, read through query 1 of breakout.sql. Read only."""
    return creator_surges(query(client, queries()[0], {"d": d}, core=core, agent=agent), d)


def is_local(p, market):
    located = p["geo_market"] == market and (p["geo_confidence"] or 0) >= GEO_CONFIDENCE
    return located or p["home_market"] == market


def _handle(raw):
    return unicodedata.normalize("NFKC", str(raw).strip().lstrip("@").casefold()).strip()


def _signals_with_counts(found, posts, sightings, d, run_id, rule_version):
    by_post = {p["post_id"]: p for p in posts}
    groups = defaultdict(dict)
    held = defaultdict(set)
    counts = {"input_rows": len(sightings), "for_breakouts": 0, "non_generic": 0,
              "small": 0, "local": 0, "unflagged_local": 0, "flagged_local": 0,
              "item_groups": 0, "max_distinct_unflagged_creators": 0,
              "qualifying_item_groups": 0}
    for s in sightings:
        b = found.get(s["post_id"])
        if b is None:
            continue
        counts["for_breakouts"] += 1
        if is_generic(s["kind"], s["canonical_key"]):
            continue
        counts["non_generic"] += 1
        p = by_post[s["post_id"]]
        if p["tier"] not in SMALL_TIERS:
            continue
        counts["small"] += 1
        if not is_local(p, s["market"]):
            continue
        counts["local"] += 1
        key = (s["market"], s["item_id"])
        if p["coord_score"] > 0:
            held[key].add(b["post_id"])
            counts["flagged_local"] += 1
        else:
            groups[key][b["post_id"]] = _handle(p["handle"] or p["creator_id"])
            counts["unflagged_local"] += 1
    rows = []
    counts["item_groups"] = len(set(groups) | set(held))
    for (market, item), handles in sorted(groups.items()):
        creators = len(set(handles.values()))
        counts["max_distinct_unflagged_creators"] = max(
            counts["max_distinct_unflagged_creators"], creators)
        if creators < MIN_CREATORS:
            continue
        counts["qualifying_item_groups"] += 1
        ratios = {pid: found[pid]["ratio"] for pid in handles}
        rows.append({"metric_date": d, "market": market, "item_id": item, "run_id": run_id,
                     "creators": creators, "posts": len(handles),
                     "evidence_post_ids": sorted(handles, key=lambda pid: (-(ratios[pid] or 0), pid)),
                     "top_ratio": max((r for r in ratios.values() if r is not None), default=None),
                     "held_flagged": len(held[(market, item)]), "rule_version": rule_version})
    return rows, counts


def signals(found, posts, sightings, d, run_id, rule_version):
    return _signals_with_counts(found, posts, sightings, d, run_id, rule_version)[0]


def _empty_reason(post_counts, sighting_counts, signal_rows):
    if signal_rows:
        return None
    if post_counts["breakouts"] == 0:
        return "no_breakout_posts"
    if sighting_counts["input_rows"] == 0:
        return "no_sound_format_sightings"
    if sighting_counts["for_breakouts"] == 0:
        return "no_breakout_sightings"
    if sighting_counts["non_generic"] == 0:
        return "only_generic_items"
    if sighting_counts["small"] == 0:
        return "no_small_creator_breakouts"
    if sighting_counts["local"] == 0:
        return "no_local_sightings"
    if sighting_counts["unflagged_local"] == 0:
        return "only_coordinated_creators"
    return "insufficient_unrelated_creators"


def run_breakout(client, d, run_id, rule_version, core=CORE, agent=AGENT):
    """Read breakout posts, item signals, and eligibility counts for d without writing."""
    posts_sql, sightings_sql = queries()
    posts = query(client, posts_sql, {"d": d}, core=core, agent=agent)
    found, post_counts = _find_breakouts(posts)
    sightings = query(client, sightings_sql, {"d": d}, core=core, agent=agent) if found else []
    signal_rows, sighting_counts = _signals_with_counts(found, posts, sightings, d, run_id, rule_version)
    eligibility = {"posts": post_counts, "sightings": sighting_counts,
                   "empty_reason": _empty_reason(post_counts, sighting_counts, signal_rows)}
    return {"breakouts": sorted(found.values(), key=lambda b: b["post_id"]),
            "signals": signal_rows, "eligibility": eligibility}


def rows_param(signal_rows):
    """The @rows parameter of APPEND_SQL: one struct per evidence post of each signal."""
    return aggregate._struct_array("rows", [{**s, "post_id": pid, "rank": rank} for s in signal_rows
                                            for rank, pid in enumerate(s["evidence_post_ids"])], ROW_FIELDS)


def append_signals(client, d, run_id, rule_version, core=CORE, agent=AGENT):
    """Append d's signals under run_id and return counts with eligibility diagnostics."""
    out = run_breakout(client, d, run_id, rule_version, core, agent)
    counts = {"breakouts": len(out["breakouts"]), "signals": len(out["signals"]), "appended": 0,
              "eligibility": out["eligibility"]}
    if out["signals"]:
        aggregate._run(client, APPEND_SQL, [rows_param(out["signals"])], core, agent)
        counts["appended"] = query(client, APPENDED_SQL, {"d": d, "run_id": run_id}, core, agent)[0]["n"]
    return counts
