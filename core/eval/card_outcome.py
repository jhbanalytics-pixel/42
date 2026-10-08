"""Card and hold outcome measure (METHOD-GAPS section 7, Gap 6). Observation only: nothing here feeds a gate, a
threshold, a card or a hold.

    build_outcomes(briefs, states, detect_days) -> one row per (run date, market, item_id, kind, hold reason)
    summarize(rows, end=None, weeks=4, min_n=30)  -> held rate with a Wilson 95% interval, per market and hold reason
    state_distribution(rows)                       -> the states at t, t + 3, t + 7 and t + 14, printed before any rate
    report_markdown(rows, summaries, end=...)      -> the report text

briefs are rows of agent.briefs (brief_date, market, run_id, published_at, status, payload). Only payload.cards,
payload.more and payload.held_back.items are read, and of those only item_id, rank, state, reason and rule. states
are item_state rows read through v_item_state_current (metric_date, market, item_id, state, main_lane_class), where
main_lane_class is the lane class of the row's main series in v_series_test_current. detect_days is every date with
a good detect run: a day outside it has no item_state at all, so nothing on it is an absence.

A published card or held item is followed to t + 3, t + 7 and t + 14. On each later day it falls in one class:

    pending      the day has no good detect run yet (or never will)
    unmeasured   an item_state row exists but its main series is not on a measured lane, so it labels nothing
    confirmed    measured, and in a state that needs floors, persistence or a qualifying overlay (CONFIRMED_STATES)
    unconfirmed  measured, and in a state the product shows as unconfirmed or as someone else's list
    collapsed    measured and Fading, or no item_state row on a day that has a good detect run (absent)
    other        measured, with a row whose state is empty or unknown to the product vocabulary

Held, in the product vocabulary, is confirmed or unconfirmed: the topic is still in an active state on a measured
lane. The class of each state comes from DATA.md 3.7 and state.sql:

    emerging, rising, peaking   confirmed. Floors met (5 creators, 8 posts in 3 days) plus a significant day (tested)
                                or 5 observed days of a non-falling count (untested); the backtest PERSISTING set.
    mainstream                  confirmed. Needs Rising, Peaking or Mainstream in the last 28 days, then macro creators
                                with news, or Rising on 3 platforms: it is further along than Rising, not gone.
    recurring, seasonal         confirmed. Both are an overlay on an item that qualified for Rising, Emerging, Spike or
                                New to 42 today (state.sql, orders 1 and 2), so the item is active; they only say why.
                                They also form the scheduled stratum, so calendar events stay apart from trends.
    spike                       unconfirmed. One significant day without persistence (tested), or a 3 times jump or a
                                board top 10 twice (untested). The card says so: shown as unconfirmed, with counts.
    new_to_42                   unconfirmed. Untested warm-up only, 3 creators in 3 days or one board entry, counts
                                shown with no growth claim, and the state ends after 14 days of measurement.
    on_the_boards               unconfirmed. Present on a platform's own board today, labelled as the platform's
                                list and not 42's finding.
    fading                      collapsed. At or below 60% of the 28 day peak on each of the last 3 days.

The backtest set (PERSISTING, reused from core/detect/backtest.py) stays as a second column beside the product one,
and a third column counts confirmed alone, so the three can be read against each other.

Only unbiased_rank, unbiased_counter and panel lanes measure (DATA.md 3.2). Posts found by the seed loop or by
search (search_presence, watchlist, legacy) never label an outcome. The lane class comes from the series_test row
the later item_state row points at, a table other than the one being labelled, and is checked here against the
pinned MEASURED_LANES; nothing in the input can declare a row measured.

The headline is t + 7. The held rate is held over the rows with a decided class (held, collapsed or other);
unmeasured and pending rows are counted beside it and never in its denominator. A rate is printed only from 30
decided rows.
"""

import json
import re
from datetime import date, datetime, timedelta
from statistics import NormalDist

from core.detect.backtest import PERSISTING

MEASURED_LANES = frozenset({"unbiased_rank", "unbiased_counter", "panel"})
CONFIRMED_STATES = (*PERSISTING, "mainstream", "recurring", "seasonal")
UNCONFIRMED_STATES = ("spike", "new_to_42", "on_the_boards")
ACTIVE_STATES = CONFIRMED_STATES + UNCONFIRMED_STATES
COLLAPSED_STATES = ("fading",)
SCHEDULED_STATES = ("seasonal", "recurring")
SKIPPED_STATUSES = ("data_issue",)
HORIZONS = (3, 7, 14)
HEADLINE = 7
WEEKS = 4
MIN_N = 30
NOT_ENOUGH = "not enough data"
DECIDED = ("held", "collapsed", "other")
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


def _classify(idx, detect_days, day, market, item_id):
    """(class, state) for the item on a later day."""
    if day not in detect_days:
        return "pending", None
    row = idx.get((day, market, item_id))
    if row is None:
        return "collapsed", "absent"
    state = _state(row.get("state"))
    if row.get("main_lane_class") not in MEASURED_LANES:
        return "unmeasured", state
    if state in CONFIRMED_STATES:
        return "confirmed", state
    if state in UNCONFIRMED_STATES:
        return "unconfirmed", state
    if state in COLLAPSED_STATES:
        return "collapsed", state
    return "other", state


def _row(day, market, item_id, kind, surface, rank, state_t, reason, rule, idx, detect_days):
    out = {"run_date": day, "market": market, "item_id": item_id, "kind": kind, "surface": surface,
           "hold_reason": reason, "rule": rule, "rank": rank, "state_t": state_t,
           "stratum": "scheduled" if state_t in SCHEDULED_STATES else "trend"}
    for h in HORIZONS:
        cls, state = _classify(idx, detect_days, day + timedelta(days=h), market, item_id)
        out[f"state_t{h}"], out[f"class_t{h}"] = state, cls
        out[f"outcome_t{h}"] = "held" if cls in ("confirmed", "unconfirmed") else cls
        out[f"backtest_t{h}"] = cls == "confirmed" and state in PERSISTING
    head = out[f"outcome_t{HEADLINE}"]
    out["measured"] = head in DECIDED
    out["held"] = head == "held"
    out["held_confirmed"] = out[f"class_t{HEADLINE}"] == "confirmed"
    out["held_backtest"] = out[f"backtest_t{HEADLINE}"]
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
    cls, outcome = f"class_t{horizon}", f"outcome_t{horizon}"
    counts = {o: sum(r[outcome] == o for r in sel) for o in (*DECIDED, "unmeasured", "pending")}
    confirmed = sum(r[cls] == "confirmed" for r in sel)
    n = sum(counts[o] for o in DECIDED)
    g = {"horizon": horizon, "kind": kind, "market": market, "hold_reason": reason, "stratum": stratum,
         "window_start": start, "window_end": end, "n": n, **counts, "confirmed": confirmed,
         "unconfirmed": counts["held"] - confirmed, "backtest": sum(bool(r[f"backtest_t{horizon}"]) for r in sel),
         "total": len(sel)}
    for suffix, k in (("", counts["held"]), ("_confirmed", confirmed), ("_backtest", g["backtest"])):
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
    """Counts of items per kind, per moment (t, t+3, t+7, t+14) and state, 'absent', 'pending' and None included, on any lane."""
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


def to_markdown(summary, *, end, horizon=HEADLINE, title=None, persisting_seen=None):
    lines = [f"# {title or f'Card and hold outcomes at {horizon} days'}", "",
             f"Run dates pooled over {WEEKS} weeks ending {_date(end).isoformat()}. Held means still in an active state "
             f"at {horizon} days on a measured lane: confirmed (Emerging, Rising, Peaking, Mainstream, Recurring, "
             "Seasonal) or unconfirmed (Spike, New to 42, On the boards). The backtest column counts only Emerging, "
             "Rising and Peaking. Collapsed means Fading or absent. Unmeasured rows (later state only on a search, "
             "watchlist or legacy lane) and pending rows (no good detect run on the day yet) stay out of every rate. "
             f"A rate is shown only from {MIN_N} decided rows.", ""]
    if persisting_seen == 0:
        lines += ["No Emerging, Rising or Peaking state appears on any of these items at t or later, so the backtest "
                  "column is zero by construction and says nothing about whether cards held up.", ""]
    for kind, head in (("published", "Published cards (cards and more)"), ("held", "Held items, per hold reason")):
        lines += [f"## {head}", "",
                  f"| market | hold reason | stratum | n | held | confirmed | unconfirmed | collapsed | other | unmeasured "
                  f"| pending | held at {horizon} days | confirmed only | backtest set |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for g in summary:
            if g["kind"] == kind:
                lines.append(f"| {g['market']} | {g['hold_reason'] or 'all'} | {g['stratum']} | {g['n']} | {g['held']} | "
                             f"{g['confirmed']} | {g['unconfirmed']} | {g['collapsed']} | {g['other']} | "
                             f"{g['unmeasured']} | {g['pending']} | {g['text']} | {g['text_confirmed']} | "
                             f"{g['text_backtest']} |")
        lines.append("")
    return "\n".join(lines)


def report_markdown(rows, summaries, *, end):
    """The state distribution first, then the rate tables: t + 7 in full, then the other horizons."""
    seen = persisting_seen(rows)
    order = (HEADLINE, *(h for h in HORIZONS if h != HEADLINE))
    return "\n".join([distribution_markdown(rows), *(
        to_markdown(summaries[h], end=end, horizon=h, persisting_seen=seen) for h in order)])
