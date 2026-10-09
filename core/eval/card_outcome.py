"""Card and hold outcome measure (METHOD-GAPS section 7, Gap 6). Observation only: nothing here feeds a gate, a
threshold, a card or a hold.

    build_outcomes(briefs, states, detect_days) -> one row per (run date, market, item_id, kind, hold reason)
    summarize(rows, end=None, weeks=4, min_n=30)  -> held rate with a Wilson 95% interval, per market and hold reason
    state_distribution(rows)                       -> the states at t, t + 3, t + 7 and t + 14, printed before any rate
    report_markdown(rows, summaries, end=...)      -> the report text

briefs are rows of agent.briefs (brief_date, market, run_id, published_at, status, payload). Only payload.cards,
payload.more and payload.held_back.items are read, and of those only item_id, rank, state, reason and rule. states
are item_state rows read through v_item_state_current (metric_date, market, item_id, state, base_state,
main_lane_class, signal_lanes), where main_lane_class is the lane class of the row's main series in
v_series_test_current and signal_lanes lists the lane class of every series of the item that day that is significant
or jumping.
detect_days is every date with a good detect run: a day outside it has no item_state at all, so nothing on it is
an absence.

A published card or held item is followed to t + 3, t + 7 and t + 14. On each later day it falls in one class:

    pending      the day has no good detect run yet (or never will)
    unmeasured   an item_state row exists but its main series, or any significant or jumping series of the item
                 that day, is not on a measured lane, so it labels nothing
    confirmed    measured, and Emerging, Rising, Peaking or Mainstream
    unconfirmed  measured, and Spike
    listed       measured, and On the boards or New to 42
    collapsed    measured and Fading, or no item_state row on a day that has a good detect run (absent)
    other        measured, with an empty or unknown state, or a Recurring or Seasonal row with no base state

Held is the product's own active set, the active28 list in state.sql: spike, emerging, rising, peaking, mainstream,
recurring and seasonal. On the boards and New to 42 are not in it, so a topic that is only listed is not held; it
is counted in its own columns. Recurring and Seasonal are an overlay that state.sql puts on top of Rising, Emerging,
Spike or New to 42, and base_state holds the state underneath, so each is classed by its base: a Recurring row over
Spike is a spike, over New to 42 is listed, and with no base state is other (a row written before base_state
existed). The overlays also form the scheduled stratum, so calendar events stay apart from trends.

Three more columns sit beside the product one. TRUST is the meaning of TRUST.md section 7, still rising or peaking at
7 days (its "or new platform" part is not computed). The backtest set is PERSISTING from core/detect/backtest.py,
Emerging, Rising or Peaking. The old column, any listing, is held plus listed, which is what an earlier version
of this module called held.

Only unbiased_rank, unbiased_counter and panel lanes measure (DATA.md 3.2). Posts found by the seed loop or by
search (search_presence, watchlist, legacy) never label an outcome. The lane class comes from the series_test row
the later item_state row points at, a table other than the one being labelled, and is checked here against the
pinned MEASURED_LANES; nothing in the input can declare a row measured. A row without signal_lanes is unmeasured.

The headline is t + 7. A rate is over the rows with a decided class (held, listed, collapsed or other); unmeasured
and pending rows are counted beside it and never in its denominator. A rate is printed only from 30 decided rows.
"""

import json
import re
from datetime import date, datetime, timedelta
from statistics import NormalDist

from core.detect.backtest import PERSISTING

DEFINITION = "active28_by_base_v3"
MEASURED_LANES = frozenset({"unbiased_rank", "unbiased_counter", "panel"})
CONFIRMED_STATES = (*PERSISTING, "mainstream")
UNCONFIRMED_STATES = ("spike",)
HELD_STATES = CONFIRMED_STATES + UNCONFIRMED_STATES
OVERLAY_STATES = ("recurring", "seasonal")
BASE_STATES = ("rising", "emerging", "spike", "new_to_42")
LISTED_STATES = ("on_the_boards", "new_to_42")
TRUST_STATES = ("rising", "peaking")
COLLAPSED_STATES = ("fading",)
SCHEDULED_STATES = OVERLAY_STATES
SKIPPED_STATUSES = ("data_issue",)
HORIZONS = (3, 7, 14)
HEADLINE = 7
WEEKS = 4
MIN_N = 30
NOT_ENOUGH = "not enough data"
DECIDED = ("held", "listed", "collapsed", "other")
WHEN = {0: "t", 3: "t+3", 7: "t+7", 14: "t+14"}
Z95 = NormalDist().inv_cdf(0.975)


def _date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _state(v):
    return v.lower() if isinstance(v, str) else None


def _payload(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


def _latest_briefs(briefs):
    """The newest usable brief of each (date, market), by published_at then run_id; data_issue briefs are skipped."""
    best = {}
    for b in briefs:
        if b.get("status") in SKIPPED_STATUSES:
            continue
        key = (_date(b["brief_date"]), b["market"])
        stamp = (str(b.get("published_at") or ""), str(b.get("run_id") or ""))
        if key not in best or stamp > best[key][0]:
            best[key] = (stamp, b)
    return [best[k][1] for k in sorted(best)]


def _index_states(states):
    idx = {}
    for s in states:
        key = (_date(s["metric_date"]), s["market"], s["item_id"])
        if key in idx:
            raise ValueError(f"two item_state rows for {key}")
        idx[key] = s
    return idx


def _measured(row):
    """The main series and every significant or jumping series of the item that day are on measured lanes. A row
    that does not carry signal_lanes cannot show that, so it never measures."""
    signals = row.get("signal_lanes")
    return (row.get("main_lane_class") in MEASURED_LANES and isinstance(signals, (list, tuple))
            and all(lane in MEASURED_LANES for lane in signals))


def _classify(idx, detect_days, day, market, item_id):
    """(class, state shown, state underneath any overlay) for the item on a later day."""
    if day not in detect_days:
        return "pending", None, None
    row = idx.get((day, market, item_id))
    if row is None:
        return "collapsed", "absent", "absent"
    state = _state(row.get("state"))
    base = _state(row.get("base_state"))
    eff = (base if base in BASE_STATES else None) if state in OVERLAY_STATES else state
    if not _measured(row):
        return "unmeasured", state, eff
    if eff in CONFIRMED_STATES:
        return "confirmed", state, eff
    if eff in UNCONFIRMED_STATES:
        return "unconfirmed", state, eff
    if eff in LISTED_STATES:
        return "listed", state, eff
    if eff in COLLAPSED_STATES:
        return "collapsed", state, eff
    return "other", state, eff


def _row(day, market, item_id, kind, surface, rank, state_t, reason, rule, idx, detect_days):
    out = {"definition": DEFINITION, "run_date": day, "market": market, "item_id": item_id, "kind": kind,
           "surface": surface, "hold_reason": reason, "rule": rule, "rank": rank, "state_t": state_t,
           "stratum": "scheduled" if state_t in SCHEDULED_STATES else "trend"}
    for h in HORIZONS:
        cls, state, eff = _classify(idx, detect_days, day + timedelta(days=h), market, item_id)
        measured = cls in ("confirmed", "unconfirmed", "listed", "collapsed", "other")
        out[f"state_t{h}"], out[f"class_t{h}"], out[f"eff_state_t{h}"] = state, cls, eff
        out[f"outcome_t{h}"] = "held" if cls in ("confirmed", "unconfirmed") else cls
        out[f"trust_t{h}"] = measured and eff in TRUST_STATES
        out[f"backtest_t{h}"] = measured and eff in PERSISTING
        out[f"any_t{h}"] = cls in ("confirmed", "unconfirmed", "listed")
    head = out[f"outcome_t{HEADLINE}"]
    out["measured"] = head in DECIDED
    out["held"] = head == "held"
    out["held_confirmed"] = out[f"class_t{HEADLINE}"] == "confirmed"
    out["held_trust"] = out[f"trust_t{HEADLINE}"]
    out["held_backtest"] = out[f"backtest_t{HEADLINE}"]
    out["held_any"] = out[f"any_t{HEADLINE}"]
    out["collapsed"] = head == "collapsed"
    return out


def build_outcomes(briefs, states, detect_days):
    idx = _index_states(states)
    detect_days = {_date(d) for d in detect_days}
    rows = []
    for b in _latest_briefs(briefs):
        day, market = _date(b["brief_date"]), b["market"]
        payload = _payload(b.get("payload"))
        for surface in ("cards", "more"):
            for c in payload.get(surface) or []:
                rows.append(_row(day, market, c["item_id"], "published", surface, c.get("rank"),
                                 _state(c.get("state")), None, None, idx, detect_days))
        for h in (payload.get("held_back") or {}).get("items") or []:
            at_t = idx.get((day, market, h["item_id"]))
            rows.append(_row(day, market, h["item_id"], "held", None, None,
                             _state(at_t.get("state")) if at_t else None, h.get("reason") or "unknown",
                             h.get("rule"), idx, detect_days))
    rows.sort(key=lambda r: (r["run_date"], r["market"], r["kind"], r["rank"] or 99, r["item_id"]))
    return rows


def wilson(k, n, z=Z95):
    """Wilson score interval for k of n, clamped to [0, 1]."""
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _rate(k, n, min_n):
    """(rate, lo, hi, text): nothing but the words 'not enough data' below min_n decided rows."""
    if n < min_n:
        return None, None, None, NOT_ENOUGH
    lo, hi = wilson(k, n)
    return k / n, lo, hi, f"{k / n * 100:.1f}% ({lo * 100:.1f}% to {hi * 100:.1f}%)"


def _group(rows, kind, market, reason, stratum, start, end, min_n, horizon):
    sel = [r for r in rows if r["kind"] == kind and (market == "ALL" or r["market"] == market)
           and (reason is None or r.get("hold_reason") == reason)
           and (stratum == "all" or r.get("stratum") == stratum)]
    cls, outcome, eff = f"class_t{horizon}", f"outcome_t{horizon}", f"eff_state_t{horizon}"
    counts = {o: sum(r[outcome] == o for r in sel) for o in (*DECIDED, "unmeasured", "pending")}
    confirmed = sum(r[cls] == "confirmed" for r in sel)
    n = sum(counts[o] for o in DECIDED)
    g = {"horizon": horizon, "kind": kind, "market": market, "hold_reason": reason, "stratum": stratum,
         "window_start": start, "window_end": end, "n": n, **counts, "confirmed": confirmed,
         "unconfirmed": counts["held"] - confirmed,
         "on_the_boards": sum(r[cls] == "listed" and r[eff] == "on_the_boards" for r in sel),
         "new_to_42": sum(r[cls] == "listed" and r[eff] == "new_to_42" for r in sel),
         "trust": sum(bool(r[f"trust_t{horizon}"]) for r in sel),
         "backtest": sum(bool(r[f"backtest_t{horizon}"]) for r in sel),
         "any": counts["held"] + counts["listed"], "total": len(sel)}
    for suffix, k in (("", counts["held"]), ("_trust", g["trust"]), ("_backtest", g["backtest"]), ("_any", g["any"])):
        g[f"rate{suffix}"], g[f"lo{suffix}"], g[f"hi{suffix}"], g[f"text{suffix}"] = _rate(k, n, min_n)
    return g, bool(sel)


def summarize(rows, *, end=None, weeks=WEEKS, min_n=MIN_N, horizon=HEADLINE):
    """Held rate at t + horizon (7 unless asked) per kind, market (plus ALL), hold reason (plus all reasons) and
    stratum, pooled over the weeks ending on end. A rate and its interval appear only from min_n decided rows."""
    if not rows:
        return []
    end = _date(end) if end is not None else max(_date(r["run_date"]) for r in rows)
    start = end - timedelta(days=7 * weeks - 1)
    pool = [r for r in rows if start <= _date(r["run_date"]) <= end]
    out = []
    for kind in ("published", "held"):
        of_kind = [r for r in pool if r["kind"] == kind]
        markets = sorted({r["market"] for r in of_kind}) + ["ALL"]
        reasons = [None] + (sorted({r["hold_reason"] for r in of_kind}) if kind == "held" else [])
        for market in markets:
            for reason in reasons:
                for stratum in ("all", "trend", "scheduled"):
                    g, any_rows = _group(pool, kind, market, reason, stratum, start, end, min_n, horizon)
                    if any_rows:
                        out.append(g)
    return out


def persisting_seen(rows):
    """How many state values, at t and at every later horizon, are in PERSISTING, on any lane. Zero means the
    backtest column could not be true for anything in these rows."""
    fields = ("state_t", *(f"state_t{h}" for h in HORIZONS))
    return sum(r.get(f) in PERSISTING for r in rows for f in fields)


def state_mix(rows, *, horizon=HEADLINE):
    """Counts of (kind, state at t, state at t + horizon, outcome): which states the rates are made of."""
    counts = {}
    for r in rows:
        key = (r["kind"], r["state_t"], r[f"state_t{horizon}"], r[f"outcome_t{horizon}"])
        counts[key] = counts.get(key, 0) + 1
    return [{"kind": k, "state_t": t, "state_later": later, "outcome": o, "n": n}
            for (k, t, later, o), n in sorted(counts.items(), key=str)]


def state_distribution(rows):
    """Counts of items per kind, per moment (t, t+3, t+7, t+14) and state, 'absent', 'pending' and None included,
    on any lane."""
    counts = {}
    for r in rows:
        for h, when in WHEN.items():
            pending = h and r[f"class_t{h}"] == "pending"
            key = (r["kind"], when, "pending" if pending else r["state_t"] if h == 0 else r[f"state_t{h}"])
            counts[key] = counts.get(key, 0) + 1
    return [{"kind": k, "when": w, "state": s, "n": n} for (k, w, s), n in sorted(counts.items(), key=str)]


def split_queries(text):
    """The named queries of card_outcome.sql, each introduced by a line '-- @query name'."""
    parts = re.split(r"^-- @query (\w+)\s*$", text, flags=re.M)
    return {parts[i]: parts[i + 1].strip().rstrip(";").strip() for i in range(1, len(parts), 2)}


def distribution_markdown(rows):
    dist = state_distribution(rows)
    states = sorted({d["state"] for d in dist}, key=lambda s: (s is None, str(s)))
    lines = ["## State at t, t+3, t+7 and t+14 (every row, any lane, before any rate)", ""]
    for kind, head in (("published", "Published cards"), ("held", "Held items")):
        counts = {(d["when"], d["state"]): d["n"] for d in dist if d["kind"] == kind}
        lines += [f"{head}, number of items in each state.", "",
                  "| state | " + " | ".join(WHEN.values()) + " |", "|---" * (len(WHEN) + 1) + "|"]
        for s in states:
            lines.append(f"| {s if s is not None else 'no state'} | "
                         + " | ".join(str(counts.get((w, s), 0)) for w in WHEN.values()) + " |")
        lines.append("")
    return "\n".join(lines)


def to_markdown(summary, *, end, horizon=HEADLINE, title=None, persisting_seen=None, dates=None):
    lines = [f"# {title or f'Card and hold outcomes at {horizon} days'}", "",
             f"Run dates pooled over a window of up to {WEEKS} weeks ending {_date(end).isoformat()}. "
             + (f"Run dates present: {dates[0]} to {dates[1]} ({dates[2]} dates). " if dates else "")
             + f"Held means the product's own active set at {horizon} days on a measured lane (state.sql active28): "
             "Spike, Emerging, Rising, Peaking, Mainstream, and Recurring or Seasonal classed by the state underneath. "
             "On the boards and New to 42 are not in that set and have their own columns. Rising or peaking is "
             "TRUST.md section 7's meaning without its new platform clause. The backtest set is Emerging, Rising and "
             "Peaking. The last column is the earlier definition, held plus listed. Collapsed means Fading or "
             "absent. Unmeasured rows (later state only on a search, watchlist or legacy lane) and pending rows (no "
             f"good detect run on the day yet) stay out of every rate. A rate is shown only from {MIN_N} decided rows.",
             ""]
    if persisting_seen == 0:
        lines += ["No Emerging, Rising or Peaking state appears on any of these items at t or later, so the backtest "
                  "column is zero by construction and says nothing about whether cards held up.", ""]
    for kind, head in (("published", "Published cards (cards and more)"), ("held", "Held items, per hold reason")):
        lines += [f"## {head}", "",
                  f"| market | hold reason | stratum | n | held | confirmed | spike | on_the_boards | new_to_42 | collapsed "
                  f"| other | unmeasured | pending | held at {horizon} days | rising or peaking | backtest set "
                  f"| any listing (old) |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for g in summary:
            if g["kind"] == kind:
                lines.append(f"| {g['market']} | {g['hold_reason'] or 'all'} | {g['stratum']} | {g['n']} | {g['held']} | "
                             f"{g['confirmed']} | {g['unconfirmed']} | {g['on_the_boards']} | {g['new_to_42']} | "
                             f"{g['collapsed']} | {g['other']} | {g['unmeasured']} | {g['pending']} | {g['text']} | "
                             f"{g['text_trust']} | {g['text_backtest']} | {g['text_any']} |")
        lines.append("")
    return "\n".join(lines)


def report_markdown(rows, summaries, *, end):
    """The state distribution first, then the rate tables: t + 7 in full, then the other horizons."""
    seen = persisting_seen(rows)
    days = sorted({_date(r["run_date"]) for r in rows})
    dates = (days[0].isoformat(), days[-1].isoformat(), len(days)) if days else None
    order = (HEADLINE, *(h for h in HORIZONS if h != HEADLINE))
    return "\n".join([distribution_markdown(rows), *(
        to_markdown(summaries[h], end=end, horizon=h, persisting_seen=seen, dates=dates) for h in order)])
