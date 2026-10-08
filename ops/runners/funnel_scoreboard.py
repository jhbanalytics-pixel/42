"""Read-only daily funnel scoreboard: how far each moment on the answer key got through 42, stage by stage.

    py -3.13 ops/runners/funnel_scoreboard.py <moments.csv> --cutoff YYYY-MM-DD [options]

The moments file is a CSV with columns id, date (YYYY-MM-DD), market (ZA, NG or KE), moment, any_re, and_re and an
optional class (for example social or news), as ops/runners/benchmark_moments.py reads it. For every moment it
reports the furthest stage reached, in this order, with the evidence for each stage and the first stage it did not
reach:

  COLLECTED  posts matching the moment (any_re, and and_re when set) were observed in the moment's market on or before
             the cutoff, in a lane outside the excluded set
  LOCATED    at least one of them is located in the market (geo_confidence at or above the minimum, from ext_region,
             home_market or place_mention: the rule core/brief/sql/market_scope.sql uses)
  ITEMISED   the posts split into one or more items (post_items); an item belongs to the moment when it is linked to at
             least item_min_posts of the moment's collected posts and those posts are at least item_min_share of the
             item's own posts in the market over the same window. The number of items linked, the number that meet that
             rule, and the posts with no item at all are reported
  STATED     one of those items has an item_state row (a current detect run) on one of the evaluation days
  ELIGIBLE   that row is eligible
  POOLED     the item's SQL rank on that day is inside the candidate pool (core/brief/sql/brief.sql candidates). The
             rank and the scope bucket (0 strict market majority, 1 market, 2 global) are shown. A rank the brief
             recorded itself (not_assessed) is used as recorded; otherwise the rank is recomputed now from the brief's
             own SQL, which is retrospective: later mutations of posts and items can move it
  HELD       the day's brief judged the item and held it; the rule and reason are shown
  CARD       the day's brief published the item as a Today card

Evaluation days are the moment's date to brief_days after it, up to the cutoff. Every stage is decided from rows read,
never assumed: a stage whose evidence could not be read (a failed read, no item_state rows at all that day, no brief
for that day) is put in the unknown bucket with its reason, and the furthest stage is then a lower bound. Moments with
nothing readable are UNKNOWN. The summary gives every denominator: the answer-key moments, those reaching each stage,
those stopping at each stage, and those with an unknown bucket. The counts stopping at each stage plus UNKNOWN add up
to the number of moments, and the run stops with an error if they do not.

Parameters. The cutoff (observations dated on or before it), the post window around each moment, the brief days, the
excluded lanes, the located rule, the item link rule, the pool and the byte cap are all arguments or
recorded defaults, and every run saves them, with the match rule, the rank basis and a digest of the moments file,
in the JSON beside the CSV. The cutoff has no default: a run that does not say where it stops is not repeatable.

Safety: SELECT statements only (checked on the text before each query), as the caller's application default
credentials, in ogilvy-trends-v2, with maximum_bytes_billed of 3 GiB per query by default (at least 10 MiB). It first
dry-runs every statement it could send, which bills nothing, sums the estimates and refuses to run when the sum is over
--max-total-bytes (default 20 GB) or when any statement has no estimate; --dry-run stops after that estimate. It never
calls the bq CLI and writes nothing to BigQuery. The results are saved as new files in --out-dir (default ~/dev/42-readback),
opened in exclusive mode so nothing is overwritten. Prints counts, item titles and rule codes: no handles or post text.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from google.cloud import bigquery

try:
    from ops.runners.benchmark_moments import latest_briefs
except ImportError:  # run as a script from ops/runners
    from benchmark_moments import latest_briefs

PROJECT = "ogilvy-trends-v2"
CORE_Q, AGENT_Q = f"`{PROJECT}.intelligence_42_core`", f"`{PROJECT}.intelligence_42_agent`"
MAX_BYTES = 3 * 1024 ** 3
MIN_BYTES = 10 * 1024 ** 2
DEFAULT_TOTAL_BYTES = 20 * 10 ** 9
FORMAT_VERSION = 2
MARKETS = ("ZA", "NG", "KE")
GEO_SOURCES = ("ext_region", "home_market", "place_mention")
BRIEF_SQL_FILE = Path(__file__).resolve().parents[2] / "core" / "brief" / "sql" / "brief.sql"
STAGES = ("NOT_COLLECTED", "COLLECTED", "LOCATED", "ITEMISED", "STATED", "ELIGIBLE", "POOLED", "HELD", "CARD")
STAGE_INDEX = {name: n for n, name in enumerate(STAGES)}
UNKNOWN = "UNKNOWN"
MATCH_RULE = ("a post matches when its text, transcript, hashtags and enrichment text contain any_re and, when set, "
              "and_re (case-insensitive RE2); it is collected when observed in the moment's market on or before the "
              "cutoff outside the excluded lanes and lane classes; an item belongs to the moment when it is linked to "
              "at least item_min_posts collected matching posts that are at least item_min_share of the item's own "
              "posts in that market over the same window; a brief item is matched to a moment by item id only")
RANK_BASIS = ("sql_rank is the brief's recorded rank when the brief recorded one (not_assessed items), else the rank "
              "recomputed now from the candidates SQL of core/brief/sql/brief.sql with the limit removed, which is "
              "retrospective: later changes to posts, items and scope can move it")
WRITES = re.compile(r"\b(CREATE|DROP|DELETE|TRUNCATE|MERGE|INSERT|UPDATE|ALTER|CALL|EXECUTE|EXPORT|LOAD)\b",
                    re.IGNORECASE)


@dataclass(frozen=True)
class Params:
    cutoff: date
    post_before: int = 2
    post_after: int = 3
    brief_days: int = 3
    exclude_lanes: tuple = ("agent_live", "placebo")
    located_min_confidence: float = 0.7
    item_min_posts: int = 3
    item_min_share: float = 0.25
    item_cap: int = 50
    pool: int = 90
    max_bytes: int = MAX_BYTES

    def __post_init__(self):
        if not isinstance(self.cutoff, date):
            raise ValueError("cutoff must be a date")
        for name in ("post_before", "post_after", "brief_days", "item_min_posts", "item_cap", "pool",
                     "max_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("located_min_confidence", "item_min_share"):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.max_bytes < MIN_BYTES:
            raise ValueError(f"max_bytes must be at least {MIN_BYTES}: BigQuery bills at least 10 MiB a query, and a "
                             "cap of zero must not be sent as if it meant no cap")
        if not all(re.fullmatch(r"[a-z_]+", lane) for lane in self.exclude_lanes):
            raise ValueError("excluded lanes must be lower-case words")

    def record(self):
        out = asdict(self)
        out["cutoff"] = self.cutoff.isoformat()
        out["exclude_lanes"] = sorted(self.exclude_lanes)
        return out


def load_moments(path):
    with open(path, "rb") as f:
        raw = f.read()
    rows = list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
    out, seen = [], set()
    for r in rows:
        m = {k: (r.get(k) or "").strip() for k in ("id", "date", "market", "moment", "any_re", "and_re", "class")}
        if not m["id"] or m["id"] in seen or m["market"] not in MARKETS or not m["any_re"]:
            raise SystemExit(f"bad or repeated moment row: {r}")
        seen.add(m["id"])
        m["d"] = date.fromisoformat(m["date"])
        m["class"] = m["class"] or "unclassified"
        re.compile(m["any_re"])
        if m["and_re"]:
            re.compile(m["and_re"])
        out.append(m)
    if not out:
        raise SystemExit("the moments file has no rows")
    return out, hashlib.sha256(raw).hexdigest()


# SQL. Every statement is a SELECT. Moments arrive as one array parameter, as in benchmark_moments.py.

MOMENTS = "WITH m AS (SELECT * FROM UNNEST(@moments))\n"


def _post_ctes(p):
    lanes = ", ".join(f"'{lane}'" for lane in p.exclude_lanes)
    return f"""
, p AS (
  SELECT p.post_id, p.creator_id, p.post_date, p.geo_market, p.geo_confidence, p.geo_source,
    LOWER(CONCAT(IFNULL(p.text, ''), ' ', IFNULL(p.transcript, ''), ' ',
      ARRAY_TO_STRING(IFNULL(p.hashtags, []), ' '), ' ', IFNULL(e.screen_text, ''), ' ',
      IFNULL(e.video_notes, ''))) AS t
  FROM {CORE_Q}.posts p
  LEFT JOIN (SELECT post_id, ANY_VALUE(screen_text) AS screen_text, ANY_VALUE(video_notes) AS video_notes
             FROM {CORE_Q}.post_enrichment GROUP BY post_id) e ON e.post_id = p.post_id
  WHERE p.post_date BETWEEN @start AND @end)
, hit AS (
  SELECT m.id, m.market, p.post_id, p.creator_id, p.geo_market, p.geo_confidence, p.geo_source
  FROM m JOIN p
    ON p.post_date BETWEEN DATE_SUB(m.d, INTERVAL {p.post_before} DAY) AND DATE_ADD(m.d, INTERVAL {p.post_after} DAY)
   AND REGEXP_CONTAINS(p.t, CONCAT('(?i)', m.any_re))
   AND (m.and_re = '' OR REGEXP_CONTAINS(p.t, CONCAT('(?i)', m.and_re))))
, o AS (
  SELECT DISTINCT post_id, market
  FROM {CORE_Q}.post_observations
  WHERE observed_date BETWEEN @start AND @cutoff AND lane_class != 'legacy'
    AND IFNULL(lane, '') NOT IN ({lanes}))"""


def cohort_sql(p):
    geo = ", ".join(f"'{s}'" for s in GEO_SOURCES)
    return MOMENTS + _post_ctes(p) + f"""
, linked AS (SELECT DISTINCT post_id FROM {CORE_Q}.post_items WHERE post_id IN (SELECT post_id FROM hit))
, j AS (
  SELECT h.id, h.market, h.post_id, h.creator_id, h.geo_market, h.geo_confidence, h.geo_source,
    o.post_id IS NOT NULL AS collected, l.post_id IS NOT NULL AS itemised
  FROM hit h
  LEFT JOIN o ON o.post_id = h.post_id AND o.market = h.market
  LEFT JOIN linked l ON l.post_id = h.post_id)
SELECT id,
  COUNT(DISTINCT post_id) AS posts_matched,
  COUNT(DISTINCT IF(collected, post_id, NULL)) AS posts_collected,
  COUNT(DISTINCT IF(collected, creator_id, NULL)) AS creators,
  COUNT(DISTINCT IF(collected AND geo_market = market AND IFNULL(geo_confidence, 0) >= {p.located_min_confidence}
    AND geo_source IN ({geo}), post_id, NULL)) AS located,
  COUNT(DISTINCT IF(collected AND NOT itemised, post_id, NULL)) AS unitemised
FROM j GROUP BY id
"""


def items_sql(p):
    return MOMENTS + _post_ctes(p) + f"""
, mine AS (
  SELECT DISTINCT h.id, h.market, h.post_id FROM hit h JOIN o ON o.post_id = h.post_id AND o.market = h.market)
, linked AS (
  SELECT mine.id, mine.market, pi.item_id, COUNT(DISTINCT mine.post_id) AS matched_posts
  FROM mine JOIN {CORE_Q}.post_items pi ON pi.post_id = mine.post_id
  GROUP BY mine.id, mine.market, pi.item_id)
, totals AS (
  SELECT l.item_id, l.market, COUNT(DISTINCT pi.post_id) AS item_posts
  FROM (SELECT DISTINCT item_id, market FROM linked) l
  JOIN {CORE_Q}.post_items pi ON pi.item_id = l.item_id
  JOIN o ON o.post_id = pi.post_id AND o.market = l.market
  JOIN {CORE_Q}.posts ps ON ps.post_id = pi.post_id AND ps.post_date BETWEEN @start AND @end
  GROUP BY l.item_id, l.market)
SELECT linked.id, linked.item_id, linked.matched_posts, totals.item_posts,
  COUNT(*) OVER (PARTITION BY linked.id) AS items_linked
FROM linked JOIN totals ON totals.item_id = linked.item_id AND totals.market = linked.market
WHERE linked.matched_posts > 0
QUALIFY ROW_NUMBER() OVER (PARTITION BY linked.id ORDER BY linked.matched_posts DESC, linked.item_id)
  <= {p.item_cap}
ORDER BY linked.id, linked.matched_posts DESC, linked.item_id
"""


STATE_SQL = f"""
SELECT s.item_id, s.state, s.eligible, s.creators3, s.posts3, s.worth_raw, s.run_id
FROM {CORE_Q}.v_item_state_current s
WHERE s.metric_date = @d AND s.market = @market AND s.item_id IN UNNEST(@item_ids)
ORDER BY s.item_id
"""

DAY_ROWS_SQL = f"""
SELECT COUNT(*) AS n, COUNT(DISTINCT run_id) AS runs
FROM {CORE_Q}.v_item_state_current WHERE metric_date = @d AND market = @market
"""

BRIEFS_SQL = f"""
SELECT brief_date, market, run_id, published_at, status, TO_JSON_STRING(payload) AS payload
FROM {AGENT_Q}.briefs WHERE brief_date BETWEEN @first_day AND @last_day
"""

_SCOPE_ANCHOR = "ms.market_scope _selection_market_scope,"


def rank_sql():
    """The candidates SQL of core/brief/sql/brief.sql up to its ordered rows, with the limit gone and the scope counts
    added, filtered to the given items. The text is read from the brief's own file so a change to the rank order there
    is the order here; if the file no longer has the shape this needs, it stops rather than rank by a stale copy."""
    text = BRIEF_SQL_FILE.read_text(encoding="utf-8")
    block = text.split("-- name: candidates", 1)[1].split("-- name:", 1)[0]
    head, marker, _ = block.partition("selection_snapshot AS (")
    start = head.find("WITH seen AS")
    if not marker or start < 0 or head.count(_SCOPE_ANCHOR) != 1:
        raise SystemExit("core/brief/sql/brief.sql no longer has the candidates shape funnel_scoreboard.py reads")
    head = head[start:].rstrip().rstrip(",")
    head = head.replace(_SCOPE_ANCHOR, _SCOPE_ANCHOR + " ms.total_posts7 _scope_total,"
                        " ms.market_posts7 _scope_market,")
    sql = head + """
SELECT o.item_id, o._selection_sql_rank AS sql_rank, o.eligible, o.state, o._selection_market_scope AS market_scope,
  o._scope_total AS total_posts7, o._scope_market AS market_posts7, o.label, o.canonical_key, o.map_kind
FROM ordered o
WHERE o.item_id IN UNNEST(@item_ids)
ORDER BY o._selection_sql_rank
"""
    return sql.replace("{core}", CORE_Q).replace("{agent}", AGENT_Q)


def read_only(sql):
    """Raise unless the statement is a SELECT: strip comments and quoted text, then refuse any write keyword."""
    bare = re.sub(r"--[^\n]*", " ", sql)
    bare = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`[^`]*`", " ", bare)
    if bare.lstrip().split(None, 1)[0].upper() not in ("SELECT", "WITH") or WRITES.search(bare):
        raise SystemExit("only SELECT statements are run")


def used_parameters(sql, parameters):
    used = set(re.findall(r"@(\w+)", sql))
    return [q for q in parameters if q.name in used]


class Reader:
    """One capped, read-only query at a time. A failed query is recorded by name and returns None: never rows."""

    def __init__(self, client, max_bytes):
        self.client, self.max_bytes, self.errors = client, max_bytes, {}

    def estimate(self, name, sql, parameters):
        """The bytes a dry run says the statement would process, or None (recorded by name) when it cannot say. A dry
        run executes nothing and bills nothing."""
        read_only(sql)
        cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, query_parameters=used_parameters(sql, parameters))
        try:
            size = self.client.query(sql, job_config=cfg).total_bytes_processed
            if type(size) is not int or size < 0:
                raise ValueError(f"the dry run gave no byte count ({size!r})")
            return size
        except Exception as exc:
            self.errors[f"estimate {name}"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            return None

    def rows(self, name, sql, parameters):
        read_only(sql)
        cfg = bigquery.QueryJobConfig(maximum_bytes_billed=self.max_bytes,
                                      query_parameters=used_parameters(sql, parameters))
        try:
            return [dict(r.items()) for r in self.client.query(sql, job_config=cfg).result()]
        except Exception as exc:  # the stage then reads as unknown, with this as its reason
            self.errors[name] = f"{type(exc).__name__}: {str(exc)[:200]}"
            return None


def moment_struct(moments):
    struct = [bigquery.StructQueryParameter(
        None,
        bigquery.ScalarQueryParameter("id", "STRING", m["id"]),
        bigquery.ScalarQueryParameter("d", "DATE", m["d"]),
        bigquery.ScalarQueryParameter("market", "STRING", m["market"]),
        bigquery.ScalarQueryParameter("any_re", "STRING", m["any_re"]),
        bigquery.ScalarQueryParameter("and_re", "STRING", m["and_re"])) for m in moments]
    return bigquery.ArrayQueryParameter("moments", "STRUCT", struct)


def evaluation_days(moment, p):
    return [moment["d"] + timedelta(days=k) for k in range(p.brief_days + 1)
            if moment["d"] + timedelta(days=k) <= p.cutoff]


def counts_for_moment(item, p):
    """Whether a linked item meets the link rule: enough of the moment's posts, and a big enough share of its own."""
    posts, total = item["matched_posts"] or 0, item["item_posts"] or 0
    return posts >= p.item_min_posts and total > 0 and posts / total >= p.item_min_share


def base_parameters(moments, p):
    start = min(m["d"] for m in moments) - timedelta(days=p.post_before)
    end = max(m["d"] for m in moments) + timedelta(days=p.post_after)
    return [moment_struct(moments), bigquery.ScalarQueryParameter("start", "DATE", start),
            bigquery.ScalarQueryParameter("end", "DATE", end),
            bigquery.ScalarQueryParameter("cutoff", "DATE", p.cutoff)]


def day_parameters(day, market, item_ids):
    return [bigquery.ScalarQueryParameter("d", "DATE", day), bigquery.ScalarQueryParameter("market", "STRING", market),
            bigquery.ArrayQueryParameter("item_ids", "STRING", sorted(item_ids))]


def brief_parameters(moments, p):
    return [bigquery.ScalarQueryParameter("first_day", "DATE", min(m["d"] for m in moments)),
            bigquery.ScalarQueryParameter("last_day", "DATE", p.cutoff)]


def plan_queries(moments, p):
    """Every statement a run can send, as (name, sql, parameters). The state and rank reads of a run depend on what the
    first reads return, so they are planned for every market and evaluation day: the plan reads at least what the
    run will, and its total is an upper bound. The item ids are a placeholder, which does not change the bytes."""
    base = base_parameters(moments, p)
    plan = [("cohort", cohort_sql(p), base), ("items", items_sql(p), base)]
    sql_rank = rank_sql()
    for day, market in sorted({(d, m["market"]) for m in moments for d in evaluation_days(m, p)}):
        keyed = day_parameters(day, market, ["placeholder"])
        plan += [(f"state {market} {day}", STATE_SQL, keyed), (f"state rows {market} {day}", DAY_ROWS_SQL, keyed),
                 (f"rank {market} {day}", sql_rank, keyed)]
    plan.append(("briefs", BRIEFS_SQL, brief_parameters(moments, p)))
    return plan


def preflight(reader, plan, cap):
    """Dry-run every planned statement and sum the estimates. The run may go ahead only when every statement has an
    estimate and the sum is within the cap: a statement that cannot be estimated is not assumed to be small."""
    sizes = [(name, reader.estimate(name, sql, parameters)) for name, sql, parameters in plan]
    unestimated = [name for name, size in sizes if size is None]
    total = sum(size for _name, size in sizes if size is not None)
    return {"cap_bytes": cap, "total_bytes": total, "statements": len(sizes), "unestimated": unestimated,
            "largest": sorted(((n, b) for n, b in sizes if b is not None), key=lambda x: (-x[1], x[0]))[:5],
            "ok": not unestimated and total <= cap}


def read_facts(reader, moments, p):
    """Every row the scoreboard needs. A key is None when its read failed, so assembly can tell failed from empty."""
    base = base_parameters(moments, p)
    facts = {"cohort": None, "items": None, "state": {}, "rank": {}, "briefs": None}
    got = reader.rows("cohort", cohort_sql(p), base)
    if got is not None:
        facts["cohort"] = {r["id"]: r for r in got}
    got = reader.rows("items", items_sql(p), base)
    if got is not None:
        by_id = defaultdict(list)
        for r in got:
            by_id[r["id"]].append(r)
        facts["items"] = by_id
    days = {m["id"]: evaluation_days(m, p) for m in moments}
    wanted = defaultdict(set)
    for m in moments:
        counted = [r["item_id"] for r in (facts["items"] or {}).get(m["id"], []) if counts_for_moment(r, p)]
        for day in days[m["id"]]:
            if counted:
                wanted[(day, m["market"])].update(counted)
    sql_rank = rank_sql()
    for (day, market), ids in sorted(wanted.items()):
        keyed = day_parameters(day, market, ids)
        state = reader.rows(f"state {market} {day}", STATE_SQL, keyed)
        totals = reader.rows(f"state rows {market} {day}", DAY_ROWS_SQL, keyed)
        facts["state"][(day, market)] = None if state is None or totals is None else {
            "rows": {r["item_id"]: r for r in state}, "day_rows": totals[0]["n"] if totals else 0}
        eligible = state is None or any(r.get("eligible") is True for r in state)
        if eligible:
            ranks = reader.rows(f"rank {market} {day}", sql_rank, keyed)
            facts["rank"][(day, market)] = None if ranks is None else {r["item_id"]: r for r in ranks}
        else:
            facts["rank"][(day, market)] = {}
    brief_rows = reader.rows("briefs", BRIEFS_SQL, brief_parameters(moments, p))
    if brief_rows is not None:
        facts["briefs"] = {key: brief_index(payload) for key, payload in latest_briefs(brief_rows).items()}
    return facts


def brief_index(payload):
    """The items a brief judged, keyed by item id: cards and more are cards, held_back items are held, and
    not_assessed items are those the brief says it did not judge, with the rank it recorded."""
    def items(block):
        return [x for x in (block or []) if isinstance(x, dict) and x.get("item_id")]

    held_back = payload.get("held_back") if isinstance(payload.get("held_back"), dict) else {}
    not_assessed = payload.get("not_assessed") if isinstance(payload.get("not_assessed"), dict) else {}
    return {"cards": {x["item_id"]: x for x in items((payload.get("cards") or []) + (payload.get("more") or []))},
            "held": {x["item_id"]: x for x in items(held_back.get("items"))},
            "not_assessed": {x["item_id"]: x for x in items(not_assessed.get("items"))}}


def scope_bucket(market_scope, total, market_posts):
    """0 strict market majority (3+ scoped posts, 2+ in the market), 1 market, 2 global: brief.sql's CASE."""
    if market_scope == "market":
        if isinstance(total, int) and isinstance(market_posts, int):
            return 0 if total >= 3 and market_posts >= 2 else 1
        return None
    return 2


# Assembly. Pure functions of the facts.


def assess_item(moment, item_id, day, facts, p):
    """Everything known about one item on one day, with the stage it reached and what could not be established."""
    market, key = moment["market"], (day, moment["market"])
    ev = {"item_id": item_id, "day": day.isoformat(), "stage": "ITEMISED", "state": None, "eligible": None,
          "sql_rank": None, "rank_source": None, "market_scope": None, "scope_bucket": None, "kind": None,
          "hold_reason": None, "not_assessed_reason": None, "label": None, "unknown": {}, "stopped": None}
    state = facts["state"].get(key)
    row = None
    if state is None:
        ev["unknown"]["STATED"] = "item_state read failed"
    elif item_id in state["rows"] and state["rows"][item_id].get("state") is not None:
        row = state["rows"][item_id]
        ev.update(stage="STATED", state=row["state"], eligible=row.get("eligible"))
        if row.get("eligible") is True:
            ev["stage"] = "ELIGIBLE"
        else:
            ev["stopped"] = f"not eligible (state {row['state']})"
    elif state["day_rows"] == 0:
        ev["unknown"]["STATED"] = "no item_state rows for this market on this day"
    else:
        ev["stopped"] = f"no item_state row for this item on this day ({state['day_rows']} rows that day)"

    brief = None if facts["briefs"] is None else facts["briefs"].get((day.isoformat(), market))
    assessed = (brief or {}).get("not_assessed", {}).get(item_id)
    ranks = facts["rank"].get(key)
    ranked = (ranks or {}).get(item_id) if ranks is not None else None
    if assessed is not None and type(assessed.get("sql_rank")) is int:
        ev.update(sql_rank=assessed["sql_rank"], rank_source="recorded in the brief",
                  market_scope=assessed.get("market_scope"), not_assessed_reason=assessed.get("reason"))
        ev["scope_bucket"] = scope_bucket(assessed.get("market_scope"), None, None)
    elif ranked is not None:
        ev.update(sql_rank=ranked["sql_rank"], rank_source="recomputed retrospectively",
                  market_scope=ranked.get("market_scope"), label=ranked.get("label") or ranked.get("canonical_key"))
        ev["scope_bucket"] = scope_bucket(ranked.get("market_scope"), ranked.get("total_posts7"),
                                          ranked.get("market_posts7"))
    elif ev["eligible"] is True and ranks is None:
        ev["unknown"]["POOLED"] = "rank read failed"
    if ev["eligible"] is True and ev["sql_rank"] is not None:
        if ev["sql_rank"] <= p.pool:
            ev["stage"] = "POOLED"
        else:
            ev["stopped"] = f"SQL rank {ev['sql_rank']} is outside the pool of {p.pool}"
    elif ev["eligible"] is True and "POOLED" not in ev["unknown"]:
        ev["stopped"] = "eligible but not among the ranked candidates"

    could_be_judged = ev["stage"] == "POOLED" or "STATED" in ev["unknown"] or "POOLED" in ev["unknown"]
    if brief is None:
        if could_be_judged:
            ev["unknown"]["HELD"] = ("briefs read failed" if facts["briefs"] is None
                                     else "no brief for this market on this day")
    elif item_id in brief["cards"]:
        ev.update(stage="CARD", kind="card", label=brief["cards"][item_id].get("title") or ev["label"], stopped=None)
    elif item_id in brief["held"]:
        held = brief["held"][item_id]
        ev.update(stage="HELD", kind="held", label=held.get("title") or ev["label"],
                  hold_reason=f"{held.get('rule') or '?'} {held.get('reason') or '?'}", stopped=None)
    elif ev["stage"] == "POOLED":
        ev["stopped"] = (f"in the pool but not judged ({ev['not_assessed_reason']})" if ev["not_assessed_reason"]
                         else "in the pool but the brief records no card or hold for it")
    return ev


def next_stage(ev):
    """(next stage, status text) for an item's best evidence."""
    if ev["stage"] == "CARD":
        return None, "reached the last stage"
    if ev["stage"] == "HELD":
        return "CARD", f"held {ev['hold_reason']}"
    nxt = STAGES[STAGE_INDEX[ev["stage"]] + 1]
    if nxt in ev["unknown"]:
        return nxt, f"unknown: {ev['unknown'][nxt]}"
    return nxt, f"dropped: {ev['stopped'] or 'not shown by the rows read'}"


def best_evidence(evidence):
    """The item and day that got furthest; ties go to the better SQL rank, then the earlier day."""
    return max(evidence, key=lambda e: (STAGE_INDEX[e["stage"]], -(e["sql_rank"] or 10 ** 9),
                                        -date.fromisoformat(e["day"]).toordinal()))


def tri(known_true, unknown_possible):
    """True when proven, None when the evidence for it was unreadable, else False."""
    return True if known_true else None if unknown_possible else False


def assess_moment(moment, facts, p):
    """One scoreboard row for one moment."""
    cohort = None if facts["cohort"] is None else facts["cohort"].get(moment["id"], {})
    row = {"id": moment["id"], "date": moment["date"], "market": moment["market"], "moment": moment["moment"],
           "class": moment["class"], "posts_matched": None, "posts_collected": None, "creators": None,
           "located": None, "unitemised_posts": None, "items_linked": None, "items_counted": None,
           "best_item": None, "best_item_label": None, "state": None, "eligible": None, "sql_rank": None,
           "rank_source": None, "market_scope": None, "scope_bucket": None, "in_pool": None, "judged": None,
           "hold_reason": None, "card": None, "not_assessed_reason": None, "furthest_stage": UNKNOWN,
           "furthest_day": None, "next_stage": None, "next_stage_status": None, "unknown_reasons": "",
           "evidence": []}
    unknown = Counter()

    def finish(stage):
        row["furthest_stage"] = stage
        row["unknown_reasons"] = "; ".join(f"{k} (x{v})" if v > 1 else k for k, v in unknown.items())
        return row

    if cohort is None:
        unknown["cohort read failed"] += 1
        row["next_stage_status"] = "unknown: cohort read failed"
        return finish(UNKNOWN)
    row.update(posts_matched=cohort.get("posts_matched") or 0, posts_collected=cohort.get("posts_collected") or 0,
               creators=cohort.get("creators") or 0, located=cohort.get("located") or 0,
               unitemised_posts=cohort.get("unitemised") or 0)
    if not row["posts_collected"]:
        row["next_stage"] = "COLLECTED"
        row["next_stage_status"] = (
            f"dropped: {row['posts_matched']} matching posts exist but none was observed in {moment['market']} by "
            f"the cutoff outside the excluded lanes" if row["posts_matched"]
            else "dropped: no post matching the moment was found")
        return finish("NOT_COLLECTED")
    stage = "LOCATED" if row["located"] else "COLLECTED"
    row["next_stage"] = STAGES[STAGE_INDEX[stage] + 1]
    if stage == "COLLECTED":
        row["next_stage_status"] = "dropped: none of the posts is located in the market"
    if facts["items"] is None:
        unknown["ITEMISED: items read failed"] += 1
        row["next_stage"], row["next_stage_status"] = "ITEMISED", "unknown: items read failed"
        return finish(stage)

    items = facts["items"].get(moment["id"], [])
    row["items_linked"] = items[0]["items_linked"] if items else 0
    counted = [r for r in items if counts_for_moment(r, p)]
    row["items_counted"] = len(counted)
    if items and len(items) < row["items_linked"] and (items[-1]["matched_posts"] or 0) >= p.item_min_posts:
        unknown[f"ITEMISED: item list cut at {p.item_cap} of {row['items_linked']} linked items"] += 1
    days = evaluation_days(moment, p)
    evidence = [assess_item(moment, r["item_id"], day, facts, p) for r in counted for day in days]
    if counted and not days:
        unknown["STATED: no evaluation day on or before the cutoff"] += 1
        row["next_stage"], row["next_stage_status"] = "STATED", "unknown: no evaluation day on or before the cutoff"
        return finish("ITEMISED")
    if not evidence:
        best_posts = max((r["matched_posts"] for r in items), default=0)
        if stage == "LOCATED":
            row["next_stage"] = "ITEMISED"
            row["next_stage_status"] = (f"dropped: no item meets the link rule (items linked {row['items_linked']}, "
                                        f"the most matching posts in one is {best_posts}, posts with no item "
                                        f"{row['unitemised_posts']})")
        return finish(stage)

    best = best_evidence(evidence)
    stage = best["stage"]
    row["next_stage"], row["next_stage_status"] = next_stage(best)
    row.update(best_item=best["item_id"], best_item_label=best["label"], state=best["state"],
               eligible=best["eligible"], sql_rank=best["sql_rank"], rank_source=best["rank_source"],
               market_scope=best["market_scope"], scope_bucket=best["scope_bucket"],
               in_pool=tri(STAGE_INDEX[stage] >= STAGE_INDEX["POOLED"], "POOLED" in best["unknown"]
                           or "STATED" in best["unknown"]),
               judged=tri(best["kind"], "HELD" in best["unknown"]), hold_reason=best["hold_reason"],
               card=tri(best["kind"] == "card", "HELD" in best["unknown"]),
               not_assessed_reason=best["not_assessed_reason"], furthest_day=best["day"], evidence=evidence)
    for e in evidence:
        for unknown_stage, reason in e["unknown"].items():
            if STAGE_INDEX[unknown_stage] > STAGE_INDEX[stage]:
                unknown[f"{unknown_stage}: {reason}"] += 1
    return finish(stage)


def summarize(rows):
    """Every denominator: moments, those reaching each stage, those stopping at each stage, and the unknown bucket."""
    def block(selected):
        if any(r["furthest_stage"] not in (*STAGES, UNKNOWN) for r in selected):
            raise SystemExit("scoreboard denominators do not add up: a moment has no known stage")
        stopped = Counter(r["furthest_stage"] for r in selected)
        reached = {s: sum(1 for r in selected if r["furthest_stage"] != UNKNOWN
                          and STAGE_INDEX[r["furthest_stage"]] >= STAGE_INDEX[s]) for s in STAGES[1:]}
        out = {"moments": len(selected), "stopped_at": {s: stopped.get(s, 0) for s in (*STAGES, UNKNOWN)},
               "reached_at_least": reached,
               "with_unknown_bucket": sum(1 for r in selected if r["unknown_reasons"])}
        if sum(out["stopped_at"].values()) != len(selected):
            raise SystemExit("scoreboard denominators do not add up")
        return out

    groups = {"all": rows}
    for market in MARKETS:
        groups[f"market {market}"] = [r for r in rows if r["market"] == market]
    for klass in sorted({r["class"] for r in rows}):
        groups[f"class {klass}"] = [r for r in rows if r["class"] == klass]
    return {name: block(selected) for name, selected in groups.items() if selected}


CSV_COLUMNS = ("id", "date", "market", "moment", "class", "posts_matched", "posts_collected", "creators", "located",
               "unitemised_posts", "items_linked", "items_counted", "best_item", "best_item_label", "state",
               "eligible", "sql_rank", "rank_source", "market_scope", "scope_bucket", "in_pool", "judged",
               "hold_reason", "card", "not_assessed_reason", "furthest_stage", "furthest_day", "next_stage",
               "next_stage_status", "unknown_reasons")


def render(rows, summary, p, out=print):
    def show(value):
        return "?" if value is None else value

    for market in MARKETS:
        mine = [r for r in rows if r["market"] == market]
        if not mine:
            continue
        out(f"\n{market}: {len(mine)} moments")
        for r in mine:
            tail = f" ({r['hold_reason']})" if r["hold_reason"] else ""
            out(f"  {r['id']} {r['date']} {r['furthest_stage']}{tail} [{r['class']}]: {r['moment']}")
            out(f"     posts matched {show(r['posts_matched'])}, collected {show(r['posts_collected'])}, located "
                f"{show(r['located'])}, creators {show(r['creators'])}; items linked {show(r['items_linked'])}, "
                f"meeting the link rule {show(r['items_counted'])}, posts with no item {show(r['unitemised_posts'])}")
            if r["best_item"]:
                out(f"     best item {r['best_item'][:8]} {r['best_item_label'] or ''} on {r['furthest_day']}: state "
                    f"{show(r['state'])}, eligible {show(r['eligible'])}, SQL rank {show(r['sql_rank'])} "
                    f"({r['rank_source'] or 'no rank'}), scope {show(r['market_scope'])} bucket "
                    f"{show(r['scope_bucket'])}, judged {show(r['judged'])}")
            out(f"     next stage {show(r['next_stage'])}: {r['next_stage_status']}")
            if r["unknown_reasons"]:
                out(f"     unknown bucket (the furthest stage is a lower bound): {r['unknown_reasons']}")
    out("\nSummary (moments stopping at each stage; reached-at-least counts follow)")
    for name, block in summary.items():
        stops = ", ".join(f"{s} {n}" for s, n in block["stopped_at"].items())
        out(f"  {name}: {block['moments']} moments | stopped at: {stops} | with an unknown bucket: "
            f"{block['with_unknown_bucket']}")
        out("     reached at least: " + ", ".join(f"{s} {n}" for s, n in block["reached_at_least"].items()))
    out(f"\nCutoff {p.cutoff}. Match rule: {MATCH_RULE}.\nRank basis: {RANK_BASIS}.")


def main(argv=None, client=None, out=print):
    ap = argparse.ArgumentParser(prog="funnel_scoreboard.py", description="Read-only daily funnel scoreboard.")
    ap.add_argument("moments", help="moments CSV: id, date, market, moment, any_re, and_re[, class]")
    ap.add_argument("--cutoff", required=True, type=date.fromisoformat, metavar="YYYY-MM-DD",
                    help="observations and briefs on or before this day are read (required)")
    ap.add_argument("--post-before", type=int, default=2)
    ap.add_argument("--post-after", type=int, default=3)
    ap.add_argument("--brief-days", type=int, default=3, help="evaluation days after the moment's date")
    ap.add_argument("--exclude-lane", action="append", dest="exclude_lanes", default=None)
    ap.add_argument("--located-min-confidence", type=float, default=0.7)
    ap.add_argument("--item-min-posts", type=int, default=3)
    ap.add_argument("--item-min-share", type=float, default=0.25)
    ap.add_argument("--pool", type=int, default=90)
    ap.add_argument("--max-bytes", type=int, default=MAX_BYTES, help="most bytes one statement may bill")
    ap.add_argument("--max-total-bytes", type=int, default=DEFAULT_TOTAL_BYTES,
                    help="the run is refused when the dry-run estimates of all its statements add up to more")
    ap.add_argument("--dry-run", action="store_true", help="estimate every statement, print the total, run nothing")
    ap.add_argument("--out-dir", default="~/dev/42-readback")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    p = Params(cutoff=args.cutoff, post_before=args.post_before, post_after=args.post_after,
               brief_days=args.brief_days,
               exclude_lanes=tuple(sorted(args.exclude_lanes)) if args.exclude_lanes else ("agent_live", "placebo"),
               located_min_confidence=args.located_min_confidence, item_min_posts=args.item_min_posts,
               item_min_share=args.item_min_share, pool=args.pool, max_bytes=args.max_bytes)
    if args.max_total_bytes <= 0:
        raise ValueError("max_total_bytes must be a positive integer")
    moments, digest = load_moments(args.moments)
    client = client or bigquery.Client(project=PROJECT)
    reader = Reader(client, p.max_bytes)
    out(f"42 funnel scoreboard, {args.moments}, {len(moments)} moments, cutoff {p.cutoff}. SELECT only.")
    check = preflight(reader, plan_queries(moments, p), args.max_total_bytes)
    out(f"Dry run: {check['statements']} statements, an estimated {check['total_bytes']:,} bytes "
        f"({check['total_bytes'] / 1024 ** 3:.2f} GiB) against a total cap of {check['cap_bytes']:,}; largest "
        + ", ".join(f"{n} {b:,}" for n, b in check["largest"]))
    if not check["ok"]:
        why = (f"{len(check['unestimated'])} statements could not be estimated ({', '.join(check['unestimated'][:5])})"
               if check["unestimated"] else "the estimate is over the total cap")
        out(f"Refused: {why}. Nothing was run. Raise --max-total-bytes only after reading the estimate.")
        for name, message in reader.errors.items():
            out(f"ERROR {name}: {message}")
        return 2
    if args.dry_run:
        out("Dry run only: nothing was run and nothing was written.")
        return 0
    facts = read_facts(reader, moments, p)
    rows = [assess_moment(m, facts, p) for m in moments]
    summary = summarize(rows)
    render(rows, summary, p, out)
    for name, message in reader.errors.items():
        out(f"ERROR {name} read failed: {message}")

    folder = os.path.expanduser(args.out_dir)
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"funnel-scoreboard-{os.path.splitext(os.path.basename(args.moments))[0]}-{stamp}"
    with open(os.path.join(folder, stem + ".csv"), "x", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    detail = {"format_version": FORMAT_VERSION, "preflight": check, "parameters": p.record(), "match_rule": MATCH_RULE, "rank_basis": RANK_BASIS,
              "moments_file": os.path.abspath(args.moments), "moments_sha256": digest, "run_at_utc": stamp,
              "read_errors": reader.errors, "summary": summary,
              "rows": [{k: v for k, v in r.items()} for r in rows]}
    with open(os.path.join(folder, stem + ".json"), "x", encoding="utf-8", newline="") as f:
        json.dump(detail, f, indent=2, sort_keys=True, default=str)
        f.write("\n")
    out(f"\nsaved {os.path.join(folder, stem)}.csv and .json")
    return 1 if reader.errors else 0


if __name__ == "__main__":
    sys.exit(main())
