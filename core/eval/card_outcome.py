"""Card and hold outcome measure (METHOD-GAPS section 7, Gap 6). Observation only: nothing here feeds a gate, a
threshold, a card or a hold.

    build_outcomes(briefs, states, detect_days) -> one row per (run date, market, item_id, kind, hold reason)
    summarize(rows, end=None, weeks=4, min_n=30)  -> held rate with a Wilson 95% interval, per market and hold reason
    to_markdown(summary, end=...)                 -> the report text

briefs are rows of agent.briefs (brief_date, market, run_id, published_at, status, payload). Only payload.cards,
payload.more and payload.held_back.items are read, and of those only item_id, rank, state, reason and rule. states
are item_state rows read through v_item_state_current (metric_date, market, item_id, state, main_lane_class), where
main_lane_class is the lane class of the row's main series in v_series_test_current. detect_days is every date with
a good detect run: a day outside it has no item_state at all, so nothing on it is an absence.

A published card or held item is followed to t + 3, t + 7 and t + 14. On each later day its outcome is one of

    pending     the day has no good detect run yet (or never will)
    unmeasured  an item_state row exists but its main series is not on a measured lane, so it labels nothing
    held        measured, and the state is Emerging, Rising or Peaking (the backtest's PERSISTING set)
    collapsed   measured and Fading, or no item_state row on a day that has a good detect run (absent)
    other       measured and any other state (Mainstream, Spike, Recurring, ...): neither held nor collapsed

Only unbiased_rank, unbiased_counter and panel lanes measure (DATA.md 3.2). Posts found by the seed loop or by
search (search_presence, watchlist, legacy) never label an outcome. The lane class comes from the series_test row
the later item_state row points at, a table other than the one being labelled, and is checked here against the
pinned MEASURED_LANES; nothing in the input can declare a row measured.

The headline outcome is t + 7: measured, held and collapsed on a row are the t + 7 flags. The held rate is held
over the rows with a decided outcome (held, collapsed or other); unmeasured and pending rows are counted beside it
and never in its denominator. Seasonal and Recurring at t form their own stratum, scheduled, so a calendar event
passed off as a trend can be told apart.
"""

import json
import re
from datetime import date, datetime, timedelta
from statistics import NormalDist

from core.detect.backtest import PERSISTING

MEASURED_LANES = frozenset({"unbiased_rank", "unbiased_counter", "panel"})
COLLAPSED_STATES = ("fading",)
SCHEDULED_STATES = ("seasonal", "recurring")
SKIPPED_STATUSES = ("data_issue",)
HORIZONS = (3, 7, 14)
HEADLINE = 7
WEEKS = 4
MIN_N = 30
NOT_ENOUGH = "not enough data"
DECIDED = ("held", "collapsed", "other")
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


def _outcome(idx, detect_days, day, market, item_id):
    """(outcome, state) for the item on a later day."""
    if day not in detect_days:
        return "pending", None
    row = idx.get((day, market, item_id))
    if row is None:
        return "collapsed", "absent"
    state = _state(row.get("state"))
    if row.get("main_lane_class") not in MEASURED_LANES:
        return "unmeasured", state
    if state in PERSISTING:
        return "held", state
    if state in COLLAPSED_STATES:
        return "collapsed", state
    return "other", state


def _row(day, market, item_id, kind, surface, rank, state_t, reason, rule, idx, detect_days):
    out = {"run_date": day, "market": market, "item_id": item_id, "kind": kind, "surface": surface,
           "hold_reason": reason, "rule": rule, "rank": rank, "state_t": state_t,
           "stratum": "scheduled" if state_t in SCHEDULED_STATES else "trend"}
    for h in HORIZONS:
        outcome, state = _outcome(idx, detect_days, day + timedelta(days=h), market, item_id)
        out[f"state_t{h}"], out[f"outcome_t{h}"] = state, outcome
    head = out[f"outcome_t{HEADLINE}"]
    out["measured"] = head in DECIDED
    out["held"] = head == "held"
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


def _group(rows, kind, market, reason, stratum, start, end, min_n, horizon):
    sel = [r for r in rows if r["kind"] == kind and (market == "ALL" or r["market"] == market)
           and (reason is None or r.get("hold_reason") == reason)
           and (stratum == "all" or r.get("stratum") == stratum)]
    col = f"outcome_t{horizon}"
    counts = {o: sum(r[col] == o for r in sel) for o in (*DECIDED, "unmeasured", "pending")}
    n = sum(counts[o] for o in DECIDED)
    g = {"horizon": horizon, "kind": kind, "market": market, "hold_reason": reason, "stratum": stratum,
         "window_start": start, "window_end": end, "n": n, **counts, "total": len(sel),
         "rate": None, "lo": None, "hi": None, "text": NOT_ENOUGH}
    if n >= min_n:
        g["rate"] = counts["held"] / n
        g["lo"], g["hi"] = wilson(counts["held"], n)
        g["text"] = f"{g['rate'] * 100:.1f}% ({g['lo'] * 100:.1f}% to {g['hi'] * 100:.1f}%)"
    return g, bool(sel)


def summarize(rows, *, end=None, weeks=WEEKS, min_n=MIN_N, horizon=HEADLINE):
    """Held rate at t + horizon (7 unless asked) per kind, market (plus ALL), hold reason (plus all reasons) and stratum, pooled over the
    weeks ending on end. A rate and its interval appear only from min_n decided rows."""
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
    """How many state values, at t and at every later horizon, are in PERSISTING, on any lane. Zero means the held
    flag could not be true for anything in these rows."""
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


def split_queries(text):
    """The named queries of card_outcome.sql, each introduced by a line '-- @query name'."""
    parts = re.split(r"^-- @query (\w+)\s*$", text, flags=re.M)
    return {parts[i]: parts[i + 1].strip().rstrip(";").strip() for i in range(1, len(parts), 2)}


def to_markdown(summary, *, end, horizon=HEADLINE, title=None, persisting_seen=None):
    lines = [f"# {title or f'Card and hold outcomes at {horizon} days'}", "",
             f"Run dates pooled over {WEEKS} weeks ending {_date(end).isoformat()}. Held means Emerging, Rising or "
             f"Peaking at {horizon} days on a measured lane. Collapsed means Fading or absent. Unmeasured rows (later state "
             "only on a search, watchlist or legacy lane) and pending rows (no good detect run on the day yet) "
             f"stay out of the rate. A rate is shown only from {MIN_N} decided rows.", ""]
    if persisting_seen == 0:
        lines += ["No Emerging, Rising or Peaking state appears on any of these items at t or later. A held rate "
                  "of 0 here follows from the states available, and is no evidence about whether cards held up.", ""]
    for kind, head in (("published", "Published cards (cards and more)"), ("held", "Held items, per hold reason")):
        lines += [f"## {head}", "",
                  f"| market | hold reason | stratum | n | held | collapsed | other | unmeasured | pending | held at {horizon} days |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for g in summary:
            if g["kind"] == kind:
                lines.append(f"| {g['market']} | {g['hold_reason'] or 'all'} | {g['stratum']} | {g['n']} | {g['held']} | "
                             f"{g['collapsed']} | {g['other']} | {g['unmeasured']} | {g['pending']} | {g['text']} |")
        lines.append("")
    return "\n".join(lines)
