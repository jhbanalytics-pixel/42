"""The weekly detection scorecard (BUILD.md 2.7, ENGINE.md section 6), for the Monday 07:00 learn job.

    run_scorecard(client, week_start, *, run_id=None, reference=REFERENCE, core=CORE, agent=AGENT) -> rows

week_start is a Monday; the week runs to the Sunday after it. One row per market (ZA, NG, KE) with the week,
the run_id and one Figure per metric. A Figure is {value, unit, query_id, run_id, result_hash, n, reason}, like
the brief's pinned numbers (core/brief/evidence.py): query_id names the scorecard.sql query and its params,
result_hash is the sha256 of the rows it returned, and value is counted from exactly those rows. n is how many
items, entries, cards, moments or buckets the value rests on. A metric without data has value None and a
reason, never a 0.

    time_to_detect      median days from first unbiased sighting to first Emerging or Rising, over items
                        whose first Mainstream state falls in the week (an even count takes the mean of the
                        middle two), read over the whole history from DATA_START; items with no measured wait
                        stay in, ranked above every measured wait, and its unit says how many were measured
    lead_time           median days 42 first flagged an item before the held-out reference list
                        (reference/ground_truth.yaml) had it, over entries dated in the week
    precision           share of top-3 cards the weekly random review marks Real (never one-tap feedback)
    recall              share of the week's calendar moments whose matched item surfaced that day or up to
                        RECALL_DAYS after; a moment with no matched item is a miss
    breadth_platforms   share of the week's trends found on 2 or more platforms (above the placebo base)
    expansion_*_share   largest share of the week's expansion credits held by one cluster, platform or
                        language (target: no more than 0.25 each)
    cost_per_confirmed  engine credits over confirmed trends (items on Today on days confirmation ran)

First flagged (time_to_detect, lead_time), surfaced (recall) and trend (breadth_platforms) share one test, the
_trend_rows fragment of scorecard.sql. A row counts when it is eligible (an active map row, neither likely
coordinated nor not_local) and Emerging, Rising, Peaking or Mainstream (time_to_detect takes only Emerging and
Rising), or Seasonal or Recurring and also qualifying as Rising or Emerging. state.sql (DATA.md 3.7) gives
Seasonal and Recurring to anything that qualifies for Rising, Emerging, Spike or New to 42, and writes the
state the row had before that override to item_state.base_state. A Seasonal or Recurring row counts when its
base_state is rising or emerging, and never when it is spike or new_to_42. Rows written before base_state
existed hold NULL there, and only those fall back to a recheck with state.sql's own conditions for that day:
the floors, sig_days3 and novelty stored on the row, and sig_today, sig_ratio_today, sig_platforms_today,
other_market, other_platform_global and obs_days rebuilt from v_series_test_current, posts3_prev from the posts
as tvf_item_window counts them. A later row with NULL base_state takes the same fallback, which does not count
it, since state.sql found neither Rising nor Emerging and the recheck can only undercount. Spike, Fading, On
the boards and New to 42 never count. The recheck reads the tables as they are now, so it is kept to where that
can only undercount. On both paths a Seasonal or Recurring row counts only when its day's good detect run read
the stats runs that are good now: no ok stats run for the 2 days before finished after the detect run started,
at most one for the day itself did (the detect job runs its own stats step inside the detect run,
core/detect/job.py), and none finished after the detect run finished; an unknown time counts as late.
Otherwise the day's Seasonal and Recurring rows never count. posts3_prev counts every post, whatever the
creator's flag and with or without a posts row, so it is at or above what state.sql saw and an untested row can
only fail more often. A row held at Seasonal or Recurring only by the two-day hysteresis never counts, though a
held Emerging or Rising row does.

Bounds: lead_time reads from LEAD_WINDOW days before the week, recall and breadth from the week itself (plus 2
days of series tests and 27 of posts), and time_to_detect reads the whole history from DATA_START, the
earliest date 42 holds data for. Every window the scorecard chooses is a constant here, filled into
scorecard.sql by queries(), so the SQL and the units cannot drift.

time_to_detect keeps items with no measured wait in the median instead of dropping them, since dropping them
removes the longest waits. An item never flagged by its first Mainstream day waited more than the days from
its first sighting to that day; an item with no unbiased sighting before its flag has no measurable wait, and
is counted as longer than every measured one, which can only lengthen the median, never shorten it. Both rank
above every measured wait. When the median falls on one of them, value is a lower bound and the unit says the
true figure is more than it.
Recall covers calendar moments only: the large GDELT events in ENGINE.md section 6 are not included yet
(task 2.5).

It only reads. core/detect/learn.py, the weekly learn job, appends these rows to
intelligence_42_agent.engine_scorecard.
"""

import hashlib
import json
import re
import statistics
from datetime import date, timedelta
from pathlib import Path

import yaml

from core.brief.evidence import result_hash

from . import runs, sqlrun
from .sqlrun import AGENT, CORE

SQL = Path(__file__).parent / "sql" / "scorecard.sql"
REFERENCE = Path(__file__).parent / "reference" / "ground_truth.yaml"
MARKETS = ("ZA", "NG", "KE")
RULE_VERSION = "scorecard-2"      # scorecard-2: every Figure carries the locality regime marker
MIN_PLACEBO = 20                 # DATA.md 3.7: platform counts need at least 20 placebo items
REQUIRED = ("id", "market", "title", "event_date", "match_terms", "added", "source")
NOT_YET = "not yet measured: "
DATA_START = date(2026, 4, 21)   # earliest data 42 holds: legacy memory from 21 April (task 1.7)
LEAD_WINDOW = 28                 # lead_time looks for a flag this many days before a reference entry's date
RECALL_DAYS = 1                  # recall counts an item surfacing up to this many days after its moment (24 hours)
MIN_TERM = 6                     # a reference term shorter than this, spaces removed, never matches


def load_reference(path=REFERENCE):
    """The held-out reference entries, validated as the lifted leadtime_eval.py did: a broken list fails loudly."""
    entries = (yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}).get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: no entries")
    out = []
    for e in entries:
        missing = [f for f in REQUIRED if e.get(f) in (None, "", [])]
        if missing:
            raise ValueError(f"reference entry {e.get('id', '?')!r} is missing {missing}")
        market = str(e["market"]).upper()
        if market not in MARKETS:
            raise ValueError(f"reference entry {e['id']!r}: market {e['market']!r} is not one of {MARKETS}")
        event = e["event_date"]
        event = date.fromisoformat(event) if isinstance(event, str) else event
        if not isinstance(event, date):
            raise ValueError(f"reference entry {e['id']!r}: event_date {event!r} is not a date")
        terms = [str(t).strip().lower() for t in e["match_terms"] if str(t).strip()]
        if not terms:
            raise ValueError(f"reference entry {e['id']!r} has no match terms")
        out.append({"id": str(e["id"]), "market": market, "title": str(e["title"]), "event_date": event,
                    "match_terms": terms, "added": e["added"], "source": str(e["source"])})
    return out


def _quote(text):
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def reference_sql(entries):
    """The entries as a BigQuery ARRAY<STRUCT<ref_id, market, event_date, term>> literal, one element per term."""
    rows = [f"STRUCT({_quote(e['id'])} AS ref_id, {_quote(e['market'])} AS market, "
            f"DATE '{e['event_date'].isoformat()}' AS event_date, {_quote(t)} AS term)"
            for e in entries for t in e["match_terms"]]
    return "[" + ",\n  ".join(rows) + "]"


def queries(entries):
    """name -> SQL of scorecard.sql without its comment header, the reference list and the shared fragments
    (names starting with _) filled in, and dataset names left as placeholders."""
    text = sqlrun.for_authority(SQL.read_text(encoding="utf-8")).replace("{reference}", reference_sql(entries))
    named = {}
    for stmt in sqlrun.split(text):
        name = next(line.split(":", 1)[1].strip() for line in stmt.splitlines() if line.startswith("-- name:"))
        named[name] = sqlrun._strip_leading_comments(stmt)

    def since(kind, extra):
        if kind == "all":
            return f"DATE '{DATA_START.isoformat()}'"
        return f"DATE_SUB(@week_start, INTERVAL {({'week': 0, 'lead': LEAD_WINDOW}[kind]) + extra} DAY)"

    def fill(m):
        kind = m.group(2)
        return (named[m.group(1)].replace("{since}", since(kind, 0)).replace("{since2}", since(kind, 2))
                .replace("{since27}", since(kind, 27)))

    consts = {"{lead_window}": str(LEAD_WINDOW), "{recall_days}": str(RECALL_DAYS), "{min_term}": str(MIN_TERM),
              "{data_start}": f"DATE '{DATA_START.isoformat()}'"}
    out = {}
    for name, sql in named.items():
        if not name.startswith("_"):
            sql = re.sub(r"\{(_\w+)\|(\w+)\}", fill, sql)
            for key, value in consts.items():
                sql = sql.replace(key, value)
            out[name] = sql
    return out


def figure_windows(week_start):
    """{figure: (since, until)}: the days of item_state each Figure reads, the windows scorecard.sql fills in. The regime
    marker of a Figure (C4 v3 section 11.4) is read over its own window and over the same window a week earlier. The
    Figures that read credits or reviews only (precision, expansion, cost) are the week itself."""
    start, end = week_start, week_start + timedelta(days=6)
    week = (start, end)
    windows = dict.fromkeys(FIGURE_NAMES, week)
    windows["time_to_detect"] = (DATA_START, end)
    windows["lead_time"] = (start - timedelta(days=LEAD_WINDOW), end)
    windows["recall"] = (start, end + timedelta(days=RECALL_DAYS))
    return windows


def params(market, week_start):
    year, week, _ = week_start.isocalendar()
    return {"market": market, "week_start": week_start, "week_end": week_start + timedelta(days=6),
            "iso_week": f"{year}-W{week:02d}"}


def _query_id(name, sql, query_params):
    key = json.dumps({"query": name, "sql": sql, "params": query_params}, sort_keys=True, default=str)
    return f"q_{name}_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}"


def _figure(value, unit, trace, n, reason=None):
    return {"value": value, "unit": unit, **trace, "n": n, "reason": None if value is not None else reason}


def locality_regime(rows):
    """The regime marker of a week (C4 v3 section 11.4) from the locality_regime rows: the rule that wrote
    item_state.eligible in the week ("v1", "locality_v2.1" or "mixed" when the week straddles the switch, None with
    no state rows), the rule of the week before, the days under each rule, and whether the two weeks may be compared
    as one series (the same single rule in both, never a mixed week)."""
    def basis(this_week):
        found = sorted({r["locality_basis"] for r in rows if bool(r["this_week"]) is this_week})
        return None if not found else found[0] if len(found) == 1 else "mixed"

    this, before = basis(True), basis(False)
    days = {}
    for r in rows:
        if r["this_week"]:
            days[r["locality_basis"]] = days.get(r["locality_basis"], 0) + r["days"]
    return {"locality_basis": this, "previous_week_basis": before, "days_by_basis": days,
            "comparable_with_previous_week": this is not None and this == before != "mixed"}


def _median(values):
    return statistics.median(values) if values else None


def time_to_detect(rows, trace):
    """Median wait over every item first Mainstream in the week. Items without a measured wait rank above every
    measured one, at a lower bound: the days from first sighting to first Mainstream for an item never flagged,
    and never below the longest measured wait. value is a lower bound when the median falls on one of them."""
    measured = sorted(r["days"] for r in rows if r["days"] is not None)
    top = measured[-1] if measured else None
    bounds = []
    for r in (r for r in rows if r["days"] is None):
        own = ((r["mainstream_on"] - r["first_seen"]).days
               if r["flagged_on"] is None and r["first_seen"] is not None else None)
        known = [x for x in (own, top) if x is not None]
        bounds.append(max(known) if known else None)
    k, n = len(measured), len(rows)
    unit = (f"days from first unbiased sighting to first Emerging or Rising, median over {n} items first "
            f"Mainstream in the week, {k} of {n} measured; the rest (never flagged by their first Mainstream day, "
            "or with no unbiased sighting before the flag) rank above every measured wait")
    if not rows or None in bounds:
        reason = ("no item reached Mainstream in this market in the week" if not rows else
                  f"{n - k} of {n} items that reached Mainstream have no measured wait and nothing to bound it")
        return _figure(None, unit, trace, 0, reason)
    more_than = n // 2 >= k
    if more_than:
        unit += "; the median falls on one of them, so the true figure is more than this"
    return _figure(statistics.median(measured + sorted(bounds)), unit, trace, n)


def lead_time(rows, trace):
    leads = [r["lead_days"] for r in rows if r["lead_days"] is not None]
    reason = ("no reference entries dated in this week" if not rows else
              f"none of {len(rows)} reference entries dated in this week matched an item 42 flagged")
    return _figure(_median(leads), f"days early, median over {len(leads)} of {len(rows)} reference entries "
                   "flagged", trace, len(leads), reason)


def precision(rows, trace):
    value = sum(bool(r["is_real"]) for r in rows) / len(rows) if rows else None
    return _figure(value, "share of top-3 cards marked Real in the weekly random review", trace, len(rows),
                   NOT_YET + "no weekly random review of top-3 cards for this market and week")


def recall(rows, trace):
    matched = [r for r in rows if r["matched"]]
    value = sum(bool(r["surfaced"]) for r in rows) / len(rows) if matched else None
    reason = ("no calendar moments in this market in the week" if not rows else
              f"{NOT_YET}none of {len(rows)} calendar moments has matched items yet")
    return _figure(value, "share of calendar moments whose matched item surfaced as a trend that day or up to "
                   f"{RECALL_DAYS} day after (moments with no matched item count as misses; calendar moments "
                   "only, GDELT events not yet)", trace, len(rows), reason)


def breadth_platforms(rows, trace):
    placebo = rows[0]["placebo_items"] if rows else None
    ready = rows and (placebo or 0) >= MIN_PLACEBO
    value = sum(bool(r["multi"]) for r in rows) / len(rows) if ready else None
    reason = ("no trends in this market in the week" if not rows else
              f"{NOT_YET}{placebo or 0} placebo items at the week's end; platform counts need {MIN_PLACEBO}")
    return _figure(value, "share of the week's trends found on 2 or more platforms", trace,
                   len(rows) if ready else 0, reason)


def expansion_share(rows, dimension, trace):
    buckets = [r for r in rows if r["dimension"] == dimension]
    total = sum(r["credits"] or 0 for r in buckets)
    value = max(r["credits"] or 0 for r in buckets) / total if total else None
    noun = "an item" if dimension == "cluster" else "a platform"
    unit = f"largest share of expansion credits held by one {dimension} (target 0.25 or less)"
    if dimension == "cluster":
        unit += "; credits with no item are left out of the denominator"
    return _figure(value, unit, trace, len(buckets),
                   f"no expansion credits charged to {noun} in this market in the week")


def cost_per_confirmed(rows, trace):
    credits, trends = (rows[0]["credits"], rows[0]["trends"]) if rows else (None, 0)
    value = credits / trends if trends and credits is not None else None
    reason = ("no confirmed trends in this market in the week" if not trends else
              "no engine credits in credit_ledger for this market in the week")
    return _figure(value, "credits per confirmed trend (engine credits charged to this market)", trace,
                   trends or 0, reason)


FIGURE_NAMES = ("time_to_detect", "lead_time", "precision", "recall", "breadth_platforms", "expansion_cluster_share",
                "expansion_platform_share", "expansion_language_share", "cost_per_confirmed")

NO_LANGUAGE = {"value": None, "unit": "largest share of expansion credits held by one language",
               "query_id": None, "result_hash": None, "n": None,
               "reason": NOT_YET + "credit_ledger and seed_queue rows carry no language"}


def run_scorecard(client, week_start, *, run_id=None, reference=REFERENCE, core=CORE, agent=AGENT):
    """See the module docstring. Returns one row per market in MARKETS order and writes nothing."""
    if week_start.weekday() != 0:
        raise ValueError(f"week_start {week_start} is not a Monday")
    run_id = run_id or runs.new_run_id("learn", week_start)
    sql = queries(load_reference(reference))
    rows = []
    for market in MARKETS:
        p = params(market, week_start)

        def run(name, extra=None):
            query_params = {**p, **(extra or {})}
            result = sqlrun.query(client, sql[name], query_params, core=core, agent=agent)
            trace = {"query_id": _query_id(name, sql[name], query_params), "run_id": run_id,
                     "result_hash": result_hash(result)}
            return result, trace

        expansion, exp_trace = run("expansion_share")
        windows, markers = figure_windows(week_start), {}
        for window in sorted(set(windows.values())):
            regime_rows, regime_trace = run("locality_regime", {"since": window[0], "until": window[1]})
            markers[window] = {**locality_regime(regime_rows),
                               "window": {"since": window[0].isoformat(), "until": window[1].isoformat()},
                               **regime_trace}
        row = {
            "week_start": week_start, "week_end": p["week_end"], "market": market, "run_id": run_id,
            "rule_version": RULE_VERSION,
            "time_to_detect": time_to_detect(*run("time_to_detect")),
            "lead_time": lead_time(*run("lead_time")),
            "precision": precision(*run("precision")),
            "recall": recall(*run("recall")),
            "breadth_platforms": breadth_platforms(*run("breadth_platforms")),
            "expansion_cluster_share": expansion_share(expansion, "cluster", exp_trace),
            "expansion_platform_share": expansion_share(expansion, "platform", exp_trace),
            "expansion_language_share": {**NO_LANGUAGE, "run_id": run_id},
            "cost_per_confirmed": cost_per_confirmed(*run("cost_per_confirmed")),
        }
        for name in FIGURE_NAMES:
            row[name] = {**row[name], "regime": markers[windows[name]]}
        rows.append(row)
    return rows
