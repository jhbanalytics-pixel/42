"""Backtest of the series test and the switch that turns it on (TRUST.md section 4, DATA.md 3.5, task 1.19).

Each day t of the window ending on as_of is replayed from the rows available before the cutoff of t: the
start of the good stats run of t, or the end of t (UTC) when there is none or it started later. A read counts
when its available_at (or its collect run's ok row, if later) is before the cutoff; collection_health and
item_daily come from the newest good run of their day finished before it; an item needs a cultural_map
version begun before it. The series are rebuilt the way v_series_daily and tvf_series_signal build them.

Every market, platform and lane class K on t is measured as production would test it once switched on: one
stats.series_test_rows run with K and the keys already in force on t switched on, so dispersion pooling and
the Benjamini-Hochberg family are the ones K would get. Only K's series are counted. That run is the placebo
window: each of K's significant series is a false alarm, real surges included. Each of K's tested series then
gets a spike of 1.5, 2, 3 and 5 times its expected value on t, as a whole count rounded up (mu for the negative
binomial; the pulls times the series' own 28-day appearance rate for rank lists), and is detected when it is
significant with the rest of its family as it was. A spike whose whole count is more than 20% away from its
multiple (one appearance where 0.45 was expected is 6.7 times, not 3) or, on a rank list, above the pulls
cannot be injected as asked and is counted as infeasible, outside recall. A test_switch row is in force on t
only when the ok runs row of the backtest that wrote it finished before t's cutoff. Rank keys also report
recall on a spike sustained over t, t - 1 and t - 2, capped at each day's pulls; that figure is reported only
and never used by the switch rule.

Top-10 precision reads item_state: of a market's 10 eligible items with the highest worth_raw shown on t,
the share still Emerging, Rising or Peaking on t + 7.

K switches on when it has 14 observed days of one protocol, at least 60 tested placebo series-days (no alarm
in 60 bounds the rate near 5% by the rule of three), at least 20 feasible injected series-days at 3 times,
a false-alarm rate of at most 0.05 for itself and for its market and platform, and recall of at least 0.8 at
3 times. Its test_switch row is appended once per rule_version, in force from the day after as_of, so
stats.py tests it from then on with no code change. Observed days are counted per market, series and protocol
and given to every market, platform and lane class with a series there, so a curated creator panel or a
multi-platform search, whose collection_health rows carry a NULL platform, counts for its creators' platforms. A
key still with no platform (no series names one) or no lane class is never measured: test_switch holds neither as
NULL, so stats.py could never find its row. It is listed under skipped with its observed days instead.

Which rows are written (W8-DEC-13): switch_rows rebuilds every figure from the counts in a result (alarms and
tested, detected and injected at 3 times), never from the rates, flags or false_alarms block the result states
about itself, and a stay-off flag can only veto. A row is written for a key only where its false-alarm rate and its
market and platform rate are at most 0.05 and its recall at 3 times is at least 0.8, on top of the floors above.
The row cites the run id of the result, which must be well formed and of the as_of day, and a result of the
candidate rule version earns none. write_switch_rows appends only when apply is exactly True. Every series
without a row stays on the untested branch of state.sql.

A row is in force only once the runs row of the run it cites is ok (stats.sql joins it), and a row whose run never
got one is not counted as written, so a failed apply neither switches anything on nor blocks the next apply.

Entry point: python -m core.detect.backtest --as-of YYYY-MM-DD [--days N] [--apply]. The results always go
to one JSON file in core/detect/backtests; only --apply appends the test_switch rows and then the runs row
(stage 'backtest'). Reads BigQuery only, and nothing is ever updated or removed. python -m core.detect.backtest
--report FILE prints, from a saved result file and with no client, the rows that would be written and the keys
that stay off. python -m core.detect.backtest --as-of YYYY-MM-DD --apply FILE writes the rows of that reviewed file
instead of replaying again: the rows are recomputed from the file's counts, the file must be as of the day named
and carry the thresholds in force, and its run id gets the runs row. A run that already has an ok runs row is
refused.
"""

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from google.cloud import bigquery
from scipy import stats as st

from . import runs, sqlrun, stats

PROJECT = "ogilvy-trends-v2"
UTC = timezone.utc
OUT_DIR = Path(__file__).parent / "backtests"
RULE_VERSION = stats.RULE_VERSION

MULTIPLIERS = (1.5, 2, 3, 5)
WINDOW_DAYS = 28
LOOKBACK_DAYS = 118                 # 90 days of first weeks plus 28 days of history before the first replayed day
WARMUP_DAYS, FULL_DAYS = 14, 28     # tvf_series_signal's baseline states
MIN_OBSERVED_DAYS = 14
MIN_TESTED, MIN_INJECTED = 60, 20
NOMINAL_TOL = 0.2                   # a whole-count spike must land within 20% of its multiple
FA_MAX, RECALL_MIN, RECALL_AT = 0.05, 0.8, "3"
THRESHOLDS = {"false_alarm_max": FA_MAX, "recall_min": RECALL_MIN, "recall_at": RECALL_AT,
              "min_observed_days": MIN_OBSERVED_DAYS, "min_tested": MIN_TESTED, "min_injected": MIN_INJECTED}
TOP_N, LATER_DAYS = 10, 7
RUN_ID = re.compile(r"backtest-(\d{8})-[0-9a-f]{12}")
SWITCH_ROW_FIELDS = ("market", "platform", "lane_class", "switched_on", "backtest_run_id", "rule_version")
PERSISTING = ("emerging", "rising", "peaking")
FIRST_WEEK_LANES = ("panel", "unbiased_counter")

NAMES = ("runs", "health", "reads", "items", "panel", "kinds", "states", "switched")
QUERIES = dict(zip(NAMES, sqlrun.split((Path(__file__).parent / "sql" / "backtest.sql").read_text(encoding="utf-8"))))
READS_SQL = QUERIES["reads"]

# When each test_switch row was written: the ok runs row of the backtest that wrote it lands just after it.
WRITTEN_SQL = """
SELECT r.run_id, r.finished_at FROM {agent}.runs r
WHERE r.stage = 'backtest' AND r.status = 'ok' AND DATE(r.finished_at) <= @as_of
"""


# The backtest runs that were accepted: those with an ok runs row, whenever it landed.
ACCEPTED_SQL = """
SELECT r.run_id FROM {agent}.runs r WHERE r.stage = 'backtest' AND r.status = 'ok'
"""


def params(sql, as_of, days):
    """The @as_of and @start values a statement uses; @start leaves room for the history of the first day."""
    values = {"as_of": as_of, "start": as_of - timedelta(days=days - 1 + LOOKBACK_DAYS)}
    return {k: v for k, v in values.items() if f"@{k}" in sql}


def load(client, as_of, days=WINDOW_DAYS, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Every input as rows, read only up to what was available on as_of."""
    return {name: sqlrun.query(client, sql, params(sql, as_of, days), core=core, agent=agent)
            for name, sql in {**QUERIES, "written": WRITTEN_SQL, "accepted": ACCEPTED_SQL}.items()}


def end_of(t):
    return datetime.combine(t + timedelta(days=1), time(0), UTC)


def _sid(item, market, series, protocol):
    return f"{item}|{market}|{series}|{protocol}"


def _key(r):
    return r["market"], r["platform"], r["lane_class"]


KEY_FIELDS = ("market", "platform", "lane_class")


def _order(key):
    """A sort key for (market, platform, lane_class) tuples that puts a missing part first instead of failing."""
    return tuple("" if v is None else v for v in key)


def _label(key):
    """market|platform|lane_class, with '-' for a missing part."""
    return "|".join("-" if v is None else str(v) for v in key)


def _missing(key):
    """The parts of a key test_switch cannot hold: its market, platform and lane_class are NOT NULL, so stats.py
    can never find a switch row for a key with a missing part."""
    return [f.replace("_", " ") for f, v in zip(KEY_FIELDS, key) if v is None]


def _whole(x):
    """x rounded up to a whole count, ignoring float noise such as 3 x 0.1."""
    return math.ceil(x - 1e-9)


class Store:
    """The loaded rows, indexed so that any day can be rebuilt from what was available on it."""

    def __init__(self, inputs):
        self.runs = defaultdict(list)
        for r in inputs["runs"]:
            self.runs[(r["stage"], r["run_date"])].append(r)
        self.health = defaultdict(list)
        for h in inputs["health"]:
            self.health[h["day"]].append(h)
        self.reads = defaultdict(lambda: defaultdict(list))
        for r in inputs["reads"]:
            key = (r["lane_class"], r["item_id"], r["market"], r["platform"], r["series"], r["protocol"])
            self.reads[key][r["day"]].append(r)
        self.items = inputs["items"]
        self.posts = defaultdict(float)
        for r in inputs["panel"]:
            self.posts[(r["run_id"], r["item_id"], r["market"], r["series"], r["protocol"])] += r["posts"] or 0
        self.versions = defaultdict(list)
        for r in inputs["kinds"]:
            self.versions[r["item_id"]].append(r)
        self.states = inputs["states"]
        self.switched = inputs["switched"]
        self.written = {r["run_id"]: r["finished_at"] for r in inputs["written"]}
        self._health, self._cut = {}, {}

    def good(self, stage, d, cut):
        """The newest good run of stage for day d finished before cut, or None."""
        done = [r for r in self.runs.get((stage, d), ()) if r["finished_at"] < cut]
        return max(done, key=lambda r: r["finished_at"])["run_id"] if done else None

    def cutoff(self, t):
        """The start of the good stats run of t, or the end of t when there is none or it started later."""
        if t not in self._cut:
            done = self.runs.get(("stats", t), ())
            run = max(done, key=lambda r: r["finished_at"]) if done else None
            self._cut[t] = end_of(t) if run is None else min(run["started_at"], end_of(t))
        return self._cut[t]

    def switched_by(self, cut):
        """test_switch rows whose backtest's ok runs row finished before cut; a row with none is never read."""
        return [s for s in self.switched
                if self.written.get(s["backtest_run_id"]) is not None and self.written[s["backtest_run_id"]] < cut]

    def kind(self, item, cut):
        """(kind, first_seen) of the cultural_map version of item in force at cut, or None."""
        live = [v for v in self.versions.get(item, ())
                if v["valid_from"] is not None and v["valid_from"] < cut
                and (v["valid_to"] is None or v["valid_to"] >= cut)]
        if not live:
            return None
        v = max(live, key=lambda v: v["valid_from"])
        return v["kind"], v["first_seen"]

    def health_at(self, t):
        """{(market, series, protocol): {day: row}} for days up to t, from the good collect run at t's cutoff."""
        if t not in self._health:
            cut = self.cutoff(t)
            out = defaultdict(dict)
            for d, rows in self.health.items():
                rid = self.good("collect", d, cut) if d <= t else None
                for h in rows:
                    if rid is not None and h["run_id"] == rid:
                        out[(h["market"], h["series"], h["protocol"])][d] = h
            self._health[t] = out
        return self._health[t]

    @staticmethod
    def _newest(reads, cut):
        seen = [r for r in reads if r["available_at"] < cut]
        return max(seen, key=lambda r: r["available_at"]) if seen else None

    def series(self, t):
        """{series_id: series} as v_series_daily gave it on t; values maps day to (value, trials)."""
        cut = self.cutoff(t)
        health = self.health_at(t)
        aggregate = {d: self.good("aggregate", d, cut) for days in health.values() for d in days}
        out = {}
        watched, panels = [], {}
        for it in self.items:
            if it["available_at"] >= cut:
                continue
            if it["lane_class"] == "panel":
                first_day = it["first_day"]
                if first_day not in aggregate:
                    aggregate[first_day] = self.good("aggregate", first_day, cut)
                if it["run_id"] != aggregate[first_day]:
                    continue
                key = _sid(it["item_id"], it["market"], it["series"], it["protocol"])
                first = (it["first_day"], it["platform"] is None, it["platform"] or "")
                old = panels.get(key)
                if old is None or first < (old["first_day"], old["platform"] is None, old["platform"] or ""):
                    panels[key] = it
            else:
                watched.append(it)
        for it in watched + list(panels.values()):
            item, market, platform, series, protocol = (it[k] for k in ("item_id", "market", "platform", "series",
                                                                         "protocol"))
            rank = it["lane_class"] == "unbiased_rank"
            reads = self.reads.get(("unbiased_rank", item, market, platform, series, protocol), {})
            values = {}
            for d, h in health.get((market, series, protocol), {}).items():
                if rank:                    # zero only on a valid day
                    r = self._newest(reads.get(d, ()), cut)
                    v = r["value"] if r is not None and r["value"] is not None else 0.0
                    values[d] = (v if h["valid"] else None, h["units_ok"])
                else:
                    posts = self.posts.get((aggregate[d], item, market, series, protocol)) or 0
                    values[d] = (posts * h["k"] if h["valid"] and h["k"] is not None else None, None)
            out[_sid(item, market, series, protocol)] = {"item_id": item, "market": market, "platform": platform,
                                                         "series": series, "protocol": protocol,
                                                         "lane_class": it["lane_class"], "values": values}
        for (lane, item, market, platform, series, protocol), by_day in self.reads.items():
            if lane != "unbiased_counter":
                continue
            values = {}
            for d, reads in by_day.items():
                r = self._newest(reads, cut) if d <= t else None
                if r is None:
                    continue            # no row before the first read
                h = health.get((market, series, protocol), {}).get(d)
                ok = r["source"] == "vendor_history" or (h is not None and h["valid"])
                values[d] = (r["value"] if ok else None, None)
            if values:
                out[_sid(item, market, series, protocol)] = {"item_id": item, "market": market, "platform": platform,
                                                             "series": series, "protocol": protocol,
                                                             "lane_class": lane, "values": values}
        return out

    def signal(self, t, series=None):
        """tvf_series_signal rows for day t, with the columns stats.py reads; display features are NULL."""
        series = self.series(t) if series is None else series
        cut = self.cutoff(t)
        first = {}
        for s in series.values():
            for d, (v, _) in s["values"].items():
                k = (s["item_id"], s["market"])
                if v is not None and v > 0 and d <= t and (k not in first or d < first[k]):
                    first[k] = d
        rows = []
        for sid, s in sorted(series.items()):
            kind = self.kind(s["item_id"], cut)
            if t not in s["values"] or kind is None:
                continue
            y, trials = s["values"][t]
            prior = sorted((d, v, n) for d, (v, n) in s["values"].items() if d < t and v is not None)
            hist = [{"day": d, "y": v, "n": n} for d, v, n in prior if d >= t - timedelta(days=28)]
            obs_prior, obs28 = len(prior), len(hist)
            state = ("warmup" if obs_prior < WARMUP_DAYS else "thin" if obs28 < WARMUP_DAYS
                     else "short" if obs_prior < FULL_DAYS else "ok")
            rows.append({"series_id": sid, **{k: s[k] for k in ("item_id", "market", "platform", "series",
                                                                "protocol", "lane_class")},
                         "kind": kind[0], "y": y, "trials": trials, "hist": hist,
                         "obs_prior": obs_prior, "obs28": obs28, "first_measured": first.get((s["item_id"], s["market"])),
                         "hist_mean": None, "med": None, "v3": None, "v7": None, "peak28": None, "vel": None,
                         "accel": None, "z_display": None, "baseline_state": state})
        return rows

    def totals(self, t):
        """sql/stats.sql weekday totals as of t: route items per market, platform, lane class and day over the
        8 weeks before t, for days on which every route of that group was valid. Platformless panels also carry
        separate totals per market, series and protocol for candidate evaluation."""
        by = defaultdict(lambda: [0.0, []])
        own = defaultdict(lambda: [0.0, []])
        for days in self.health_at(t).values():
            for d, h in days.items():
                if t - timedelta(days=56) <= d <= t - timedelta(days=1):
                    g = by[(h["market"], h["platform"], h["lane_class"], d)]
                    g[0] += h["items"] or 0
                    g[1].append(h["valid"])
                    if h["platform"] is None and h["lane_class"] == "panel":
                        g = own[(h["market"], h["series"], h["protocol"], h["lane_class"], d)]
                        g[0] += h["items"] or 0
                        g[1].append(h["valid"])
        rows = [{"market": m, "platform": p, "lane_class": lc, "day": d, "total": total}
                for (m, p, lc, d), (total, valid) in by.items()
                if any(v is not None for v in valid) and all(v for v in valid if v is not None)]
        rows += [{"market": m, "platform": None, "lane_class": lc, "series": s, "protocol": p,
                  "day": d, "total": total}
                 for (m, s, p, lc, d), (total, valid) in own.items()
                 if any(v is not None for v in valid) and all(v for v in valid if v is not None)]
        return rows

    def first_weeks(self, t, series):
        """sql/stats.sql first-week means as of t, from the replayed series."""
        lo, hi = t - timedelta(days=90), t - timedelta(days=1)
        cut = self.cutoff(t)
        out = []
        for sid, s in series.items():
            kind = self.kind(s["item_id"], cut)
            if s["lane_class"] not in FIRST_WEEK_LANES or kind is None or kind[1] is None or not lo <= kind[1] <= hi:
                continue
            vals = sorted((d, v) for d, (v, _) in s["values"].items() if lo <= d <= hi and v is not None)
            start = next((i for i, (_, v) in enumerate(vals) if v > 0), None)
            if start is None or len(vals) - start < 7:
                continue
            week = [max(v, 0) for _, v in vals[start:start + 7]]
            out.append({"series_id": sid, **{k: s[k] for k in ("item_id", "market", "platform", "series",
                                                               "lane_class")},
                        "kind": kind[0], "first_week_mean": sum(week) / 7})
        return out

    def observed_days(self, as_of):
        """{(market, platform, lane_class): the most valid days of any one series protocol} as of as_of.

        Valid days are counted per (market, series, protocol), the way series() matches health, and given to every
        (market, platform, lane_class) with a series on that series and protocol, as well as to the health rows'
        own key when it has a platform. A curated panel's or a multi-platform search's collection_health rows carry
        a NULL platform, so keying by the health platform alone left their series' keys at 0 days. Only when no
        series names a platform does the NULL key stay, to be listed under skipped."""
        named = defaultdict(set)
        for s in self.series(as_of).values():
            named[(s["market"], s["series"], s["protocol"])].add((s["market"], s["platform"], s["lane_class"]))
        out = defaultdict(int)
        for (market, series, protocol), days in self.health_at(as_of).items():
            valid = [h for h in days.values() if h["valid"]]
            attributed = named.get((market, series, protocol), set())
            own = defaultdict(int)
            for h in valid:
                own[(market, h["platform"], h["lane_class"])] += 1
            for key, n in own.items():
                if key[1] is not None or not attributed:
                    out[key] = max(out[key], n)
            for key in attributed:
                if valid:
                    out[key] = max(out[key], len(valid))
        return out


def _own_rate(row):
    pulls = sum(h["n"] for h in row["hist"])
    return sum(min(h["y"], h["n"]) for h in row["hist"]) / pulls if pulls else 0.0


def expected(row, base):
    """The expected value of today's y: mu for the negative binomial, the pulls times the series' own 28-day
    appearance rate for rank lists."""
    return base["mu"] if base["test"] == "nb" else row["trials"] * _own_rate(row)


def injected(row, base, multiplier):
    """(today's y with the spike as a whole count rounded up, whether it is the spike asked for), or None when
    the expected value is 0. It is not when the whole count is more than 20% away from multiplier times the
    expected value, or when a rank spike is above the day's pulls."""
    e = expected(row, base)
    if not e or e <= 0:
        return None
    y = _whole(multiplier * e)
    nominal = abs(y / e - multiplier) <= NOMINAL_TOL * multiplier
    return y, nominal and (base["test"] != "betabinom" or y <= row["trials"])


def sustained(row, multiplier):
    """The rank row with a spike on today and the two history days the 3-day count reads, each a whole count of
    appearances rounded up and capped at that day's pulls; None when the series' own rate is 0."""
    rate = _own_rate(row)
    if rate <= 0:
        return None
    hist = sorted(row["hist"], key=lambda h: h["day"])

    def spike(n):
        return float(min(_whole(multiplier * n * rate), n))

    return dict(row, y=spike(row["trials"]), hist=hist[:-2] + [dict(h, y=spike(h["n"])) for h in hist[-2:]])


def _name(m):
    return f"{m:g}"


def _entry(tally, key):
    if key not in tally:
        tally[key] = {"replayed": 0, "tested": 0, "alarms": 0,
                      "recall": {_name(m): {"injected": 0, "detected": 0, "infeasible": 0} for m in MULTIPLIERS}}
        if key[2] == "unbiased_rank":
            tally[key]["sustained_recall"] = {_name(m): {"injected": 0, "detected": 0} for m in MULTIPLIERS}
    return tally[key]


def _rate(k, n):
    return k / n if n else None


def _bh(ps):
    return st.false_discovery_control(ps, method="bh")


def _detect(test, signal, base, families, spiked, rec):
    """Count each spiked series as injected, and as detected when its spiked p-value is significant under
    Benjamini-Hochberg with the rest of its family at their placebo values."""
    if not spiked:
        return
    out = test([spiked.get(i, s) for i, s in enumerate(signal)])
    for idx in families.values():
        ps = [base[j]["p_mid"] for j in idx]
        for pos, i in enumerate(idx):
            if i not in spiked:
                continue
            rec["injected"] += 1
            p = out[i]["p_mid"]
            if p is not None:
                trial = list(ps)
                trial[pos] = p
                rec["detected"] += int(_bh(trial)[pos] <= stats.Q_MAX)


def _replay_day(store, t, tally, rule_version=RULE_VERSION):
    series = store.series(t)
    signal = store.signal(t, series)
    if not signal:
        return
    totals, weeks = store.totals(t), store.first_weeks(t, series)
    force_rows = store.switched_by(store.cutoff(t))
    force = stats.in_force(force_rows, t)
    if rule_version == RULE_VERSION:
        force_rows = [{**row, "rule_version": RULE_VERSION} for row in force_rows]
    for s in signal:
        _entry(tally, _key(s))["replayed"] += 1

    placebo = {}
    for key in sorted({_key(s) for s in signal}, key=_order):
        if _missing(key):
            continue                # no test_switch row can cover it; replay() reports it as skipped
        on = frozenset(force | {key})
        switches = [*force_rows, {**dict(zip(KEY_FIELDS, key)), "switched_on": t, "rule_version": rule_version}]

        def test(rows, switches=switches):
            return stats.series_test_rows(rows, t, "backtest", rule_version, switches, totals, weeks)

        cache_key = (on, key) if rule_version == stats.SERIES_RULE_VERSION else on
        if cache_key not in placebo:
            placebo[cache_key] = test(signal)
        base = placebo[cache_key]
        families = defaultdict(list)
        for i, r in enumerate(base):
            if r["test"] != "none":
                families[r["market"]].append(i)
        mine = [i for idx in families.values() for i in idx if _key(base[i]) == key]
        if not mine:
            continue
        q = {}
        for idx in families.values():
            q.update(zip(idx, _bh([base[j]["p_mid"] for j in idx])))
        k = _entry(tally, key)
        k["tested"] += len(mine)
        k["alarms"] += sum(int(q[i] <= stats.Q_MAX) for i in mine)

        for m in MULTIPLIERS:
            rec, spiked = k["recall"][_name(m)], {}
            for i in mine:
                inj = injected(signal[i], base[i], m)
                if inj is None:
                    continue
                if inj[1]:
                    spiked[i] = dict(signal[i], y=float(inj[0]))
                else:
                    rec["infeasible"] += 1
            _detect(test, signal, base, families, spiked, rec)
            if "sustained_recall" in k:
                held = {i: sustained(signal[i], m) for i in mine if base[i]["test"] == "betabinom"}
                _detect(test, signal, base, families, {i: r for i, r in held.items() if r is not None},
                        k["sustained_recall"][_name(m)])


def decide(k):
    """(switch on, reasons it does not) for one market, platform and lane class. Sustained rank recall is
    reported only and never read here."""
    reasons = []
    if k["observed_days"] < MIN_OBSERVED_DAYS:
        reasons.append(f"{k['observed_days']} observed days of one protocol, under {MIN_OBSERVED_DAYS}")
    if k["tested"] < MIN_TESTED:
        reasons.append(f"{k['tested']} tested placebo series-days, under {MIN_TESTED}")
    elif k["false_alarm_rate"] > FA_MAX:
        reasons.append(f"false-alarm rate {k['false_alarm_rate']:.3f}, over {FA_MAX}")
    mp = k["market_platform_false_alarm_rate"]
    if mp is not None and mp > FA_MAX:
        reasons.append(f"market and platform false-alarm rate {mp:.3f}, over {FA_MAX}")
    r = k["recall"][RECALL_AT]
    if r["injected"] < MIN_INJECTED:
        reasons.append(f"{r['injected']} feasible series-days injected at {RECALL_AT}x, under {MIN_INJECTED}")
    elif r["recall"] < RECALL_MIN:
        reasons.append(f"recall {r['recall']:.3f} at {RECALL_AT}x, under {RECALL_MIN}")
    return not reasons, reasons


def _count(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _run_id(results):
    """The run id of a result, which must look like one this module mints and carry the day it was run as of."""
    run_id = results.get("run_id")
    m = RUN_ID.fullmatch(run_id) if isinstance(run_id, str) else None
    if m is None:
        raise ValueError(f"no usable backtest run id: {run_id!r}")
    if m.group(1) != str(results.get("as_of", "")).replace("-", ""):
        raise ValueError(f"backtest run id {run_id} is not of the as_of day {results.get('as_of')!r}")
    return run_id


def _counted(name, k):
    """(market, platform, lane_class) and the reason the counts of one key of a result cannot be used, if any."""
    parts = tuple(name.split("|")) if isinstance(name, str) else ()
    if len(parts) != 3 or any(not p.strip() or p == "-" for p in parts):
        return None, f"{name!r} is not a market, platform and lane class that test_switch can hold"
    rec = ((k.get("recall") or {}).get(RECALL_AT) or {}) if isinstance(k, dict) else {}
    fields = [k.get("observed_days"), k.get("tested"), k.get("alarms"), rec.get("injected"), rec.get("detected")] \
        if isinstance(k, dict) else [None]
    if not all(_count(v) for v in fields) or fields[2] > fields[1] or fields[4] > fields[3]:
        return parts, "counts are missing or inconsistent"
    return parts, None


def switch_rows(results, done=frozenset()):
    """(rows, refused) a backtest result earns. Every figure the rule reads is recomputed from the counts in the
    result: its own rates, its switch flags and its false_alarms block are never trusted, and a flag that says
    a key stays off can only veto. The row cites the result's run id, which must be well formed and of the as_of
    day, or the whole result is refused with ValueError. A candidate rule version earns nothing. Keys already in
    done as (market, platform, lane_class, rule_version) are neither written nor refused."""
    if results.get("rule_version") != RULE_VERSION:
        raise ValueError(f"backtest rule version {results.get('rule_version')!r} cannot write {RULE_VERSION} rows")
    run_id = _run_id(results)
    switched_on = (date.fromisoformat(results["as_of"]) + timedelta(days=1)).isoformat()
    keys = results.get("keys") or {}
    counted = {name: _counted(name, k) for name, k in keys.items()}
    pair, tainted = defaultdict(lambda: [0, 0]), set()
    for name, (parts, problem) in counted.items():
        if parts is None:
            continue
        if problem:
            tainted.add(parts[:2])
        else:
            pair[parts[:2]][0] += keys[name]["tested"]
            pair[parts[:2]][1] += keys[name]["alarms"]
    rows, refused = [], []
    for name, k in keys.items():
        parts, problem = counted[name]
        if problem is None and parts[:2] in tainted:
            problem = "another key of its market and platform has missing or inconsistent counts"
        if problem:
            refused.append({"key": name, "reasons": [problem]})
            continue
        rec = k["recall"][RECALL_AT]
        tested, alarms = pair[parts[:2]]
        on, reasons = decide({
            "observed_days": k["observed_days"], "tested": k["tested"], "alarms": k["alarms"],
            "false_alarm_rate": _rate(k["alarms"], k["tested"]),
            "market_platform_false_alarm_rate": _rate(alarms, tested),
            "recall": {RECALL_AT: {"injected": rec["injected"], "detected": rec["detected"],
                                   "recall": _rate(rec["detected"], rec["injected"])}}})
        if on and k.get("switch") is False:
            on, reasons = False, ["the backtest run recorded it as staying off"]
        if not on:
            refused.append({"key": name, "reasons": reasons})
        elif (*parts, RULE_VERSION) not in done:
            rows.append({"market": parts[0], "platform": parts[1], "lane_class": parts[2],
                         "switched_on": switched_on, "backtest_run_id": run_id, "rule_version": RULE_VERSION})
    return rows, refused


def write_switch_rows(client, core, rows, apply):
    """Append rows to test_switch. Refuses unless apply is exactly True and every row names every field,
    cites a well formed backtest run id (one run for all rows) and carries the rule version in force."""
    if apply is not True:
        raise ValueError("test_switch rows are written only with apply set to True")
    for row in rows:
        for field in SWITCH_ROW_FIELDS:
            v = row.get(field)
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"test_switch row has no {field}")
        if RUN_ID.fullmatch(row["backtest_run_id"]) is None:
            raise ValueError(f"test_switch row cites no usable backtest_run_id: {row['backtest_run_id']!r}")
        if row["rule_version"] != RULE_VERSION:
            raise ValueError(f"test_switch row has rule_version {row['rule_version']!r}, not {RULE_VERSION}")
    if len({row["backtest_run_id"] for row in rows}) > 1:
        raise ValueError("test_switch rows cite more than one backtest run")
    if rows:
        errors = client.insert_rows_json(f"{core}.test_switch", rows)
        if errors:
            raise RuntimeError(f"append to {core}.test_switch failed: {errors}")


def accepted_keys(switched, accepted):
    """(market, platform, lane_class, rule_version) of the test_switch rows whose backtest run has an ok runs row.
    A row whose run never got one is not in force, so it must not stop a later apply from writing its key."""
    ok = {r["run_id"] for r in accepted}
    return {(s["market"], s["platform"], s["lane_class"], s["rule_version"]) for s in switched
            if s["backtest_run_id"] in ok}


def reviewed(path, as_of):
    """The result in the saved file at path, once it is shown to be the one reviewed: named by its own run id,
    as of the day the caller names (not the day it states about itself), and judged by the thresholds in force.
    Raises ValueError otherwise."""
    path = Path(path)
    results = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(results, dict):
        raise ValueError(f"{path.name} is not a backtest result")
    if path.stem != results.get("run_id"):
        raise ValueError(f"file name {path.name} is not its backtest run id {results.get('run_id')!r}")
    if results.get("as_of") != as_of.isoformat():
        raise ValueError(f"{path.name} is as of {results.get('as_of')}, not {as_of.isoformat()}")
    if results.get("thresholds") != THRESHOLDS:
        raise ValueError(f"{path.name} was not judged by the thresholds in force: {results.get('thresholds')!r}")
    _run_id(results)
    return results


def apply_file(path, as_of, client=None, core=sqlrun.CORE, agent=sqlrun.AGENT, out=print):
    """Write the test_switch rows a reviewed result file earns and then its runs row, with no new replay. The rows
    are recomputed from the counts in the file, the file's own switch_on list is ignored, and a key already
    accepted for the rule version is not written again. Returns 0, or 1 when the file is refused before anything
    is read or written; a client is built only once the file is accepted."""
    try:
        results = reviewed(path, as_of)
        if results.get("rule_version") != RULE_VERSION:
            raise ValueError(f"backtest rule version {results.get('rule_version')!r} cannot write {RULE_VERSION} rows")
    except (ValueError, OSError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    if client is None:
        client = bigquery.Client(project=PROJECT)
    accepted = sqlrun.query(client, ACCEPTED_SQL, core=core, agent=agent)
    if results["run_id"] in {r["run_id"] for r in accepted}:
        print(f"refused: backtest run {results['run_id']} already has an ok runs row", file=sys.stderr)
        return 1
    switched = sqlrun.query(client, QUERIES["switched"], core=core, agent=agent)
    rows, _ = switch_rows(results, accepted_keys(switched, accepted))
    if rows:
        started = runs.now()
        write_switch_rows(client, core, rows, True)
        runs.append(client, results["run_id"], "backtest", date.fromisoformat(results["as_of"]), "ok", started,
                    runs.now(), {**results, "applied": True, "switch_on": rows}, agent=agent)
    for r in rows:
        out(f"switched on: {r['market']}|{r['platform']}|{r['lane_class']} from {r['switched_on']}")
    if not rows:
        out("nothing new to switch on")
    return 0


def report(path, out=print):
    """Print, from a saved backtest result file, the test_switch rows that would be written and the keys that stay
    off, reading nothing else and writing nothing. The file name must be its run id. Returns 0, or 1 when the
    file is refused."""
    path = Path(path)
    try:
        results = json.loads(path.read_text(encoding="utf-8"))
        if path.stem != results.get("run_id"):
            raise ValueError(f"file name {path.name} is not its backtest run id {results.get('run_id')!r}")
        rows, refused = switch_rows(results)
    except (ValueError, OSError, AttributeError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    out(f"dry run of {path.name}: nothing is written")
    out(f"would write {len(rows)} test_switch row{'' if len(rows) == 1 else 's'} from backtest run {results['run_id']}")
    for r in rows:
        out(f"  {r['market']}|{r['platform']}|{r['lane_class']} from {r['switched_on']} cites {r['backtest_run_id']}")
    for r in refused:
        out(f"  {r['key']} stays off: {'; '.join(r['reasons'])}")
    return 0


def top10_precision(store, window, as_of, markets):
    """Per market: of the top 10 eligible items by worth_raw shown on each day t with t + 7 on or before as_of,
    the share still Emerging, Rising or Peaking on t + 7; 'not yet measurable' when no such day exists."""
    by_run = defaultdict(list)
    for r in store.states:
        by_run[(r["metric_date"], r["run_id"])].append(r)

    def shown(d):
        rid = store.good("detect", d, end_of(d))
        return None if rid is None else by_run.get((d, rid), [])

    out = {}
    for market in sorted(set(markets) | {r["market"] for r in store.states}):
        days = items = persisted = 0
        for t in window:
            later = t + timedelta(days=LATER_DAYS)
            if later > as_of:
                continue
            now, then = shown(t), shown(later)
            if now is None or then is None:
                continue
            top = sorted((r for r in now if r["market"] == market and r["eligible"] and r["worth_raw"] is not None),
                         key=lambda r: (-r["worth_raw"], r["item_id"]))[:TOP_N]
            if not top:
                continue
            state = {r["item_id"]: r["state"] for r in then if r["market"] == market}
            days += 1
            items += len(top)
            persisted += sum(state.get(r["item_id"]) in PERSISTING for r in top)
        out[market] = ({"days": days, "items": items, "persisted": persisted, "precision": persisted / items}
                       if items else {"status": "not yet measurable"})
    return out


def _with_rates(recs):
    return {m: {**r, "recall": _rate(r["detected"], r["injected"])} for m, r in recs.items()}


def _summed(keys, field):
    out = {}
    for m in (_name(m) for m in MULTIPLIERS):
        parts = [k[field][m] for k in keys.values() if field in k]
        sums = {c: sum(p[c] for p in parts) for c in (parts[0] if parts else {"injected": 0, "detected": 0})
                if c != "recall"}
        out[m] = {**sums, "recall": _rate(sums["detected"], sums["injected"])}
    return out


def replay(inputs, as_of, days=WINDOW_DAYS, *, rule_version=RULE_VERSION):
    """The backtest results for the days days ending on as_of, as plain JSON types. The candidate rule requires an
    explicit rule_version argument; normal run and CLI calls keep RULE_VERSION."""
    if rule_version not in (RULE_VERSION, stats.SERIES_RULE_VERSION):
        raise ValueError("unsupported backtest rule version")
    store = Store(inputs)
    window = [as_of - timedelta(days=i) for i in range(days - 1, -1, -1)]
    tally = {}
    for t in window:
        _replay_day(store, t, tally, rule_version)
    observed = store.observed_days(as_of)
    for key in observed:
        _entry(tally, key)
    skipped = [{"key": _label(key), **dict(zip(KEY_FIELDS, key)), "observed_days": observed.get(key, 0),
                "replayed": tally[key]["replayed"], "reason": "no " + " and no ".join(_missing(key))}
               for key in sorted(tally, key=_order) if _missing(key)]
    markets = {key[0] for key in tally if key[0] is not None} - {"GLOBAL"}
    tally = {key: k for key, k in tally.items() if not _missing(key)}

    mp = defaultdict(lambda: [0, 0])
    for (m, p, _), k in tally.items():
        mp[(m, p)][0] += k["tested"]
        mp[(m, p)][1] += k["alarms"]
    false_alarms = {f"{m}|{p}": {"tested": n, "alarms": a, "rate": _rate(a, n)}
                    for (m, p), (n, a) in sorted(mp.items(), key=lambda kv: _order(kv[0]))}

    keys = {}
    for key in sorted(tally, key=_order):
        k = tally[key]
        metrics = {"observed_days": observed.get(key, 0), "replayed": k["replayed"], "tested": k["tested"],
                   "alarms": k["alarms"], "false_alarm_rate": _rate(k["alarms"], k["tested"]),
                   "market_platform_false_alarm_rate": false_alarms.get(f"{key[0]}|{key[1]}", {}).get("rate"),
                   "recall": _with_rates(k["recall"])}
        if "sustained_recall" in k:
            metrics["sustained_recall"] = _with_rates(k["sustained_recall"])
        on, reasons = decide(metrics)
        keys[_label(key)] = {**metrics, "switch": on, "reasons": reasons}

    return {"as_of": as_of.isoformat(), "days": days, "rule_version": rule_version,
            "multipliers": [_name(m) for m in MULTIPLIERS],
            "series": {"replayed": sum(k["replayed"] for k in keys.values()),
                       "tested": sum(k["tested"] for k in keys.values()), "keys": len(keys)},
            "recall": _summed(keys, "recall"), "sustained_recall": _summed(keys, "sustained_recall"),
            "false_alarms": false_alarms, "keys": keys, "skipped": skipped,
            "top10_precision": top10_precision(store, window, as_of, markets)}


def run(client, as_of, *, apply=False, days=WINDOW_DAYS, out_dir=OUT_DIR, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Backtest as of as_of and write the JSON file; with apply, append any new test_switch rows and then the
    runs row, so a failed insert leaves no ok backtest row. Returns the results."""
    run_id = runs.new_run_id("backtest", as_of)
    started = runs.now()
    inputs = load(client, as_of, days, core, agent)
    results = {"run_id": run_id, "applied": apply, "thresholds": dict(THRESHOLDS), **replay(inputs, as_of, days)}
    rows, _ = switch_rows(results, accepted_keys(inputs["switched"], inputs["accepted"]))
    results["switch_on"] = rows
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{run_id}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    if apply:
        write_switch_rows(client, core, rows, apply)
        runs.append(client, run_id, "backtest", as_of, "ok", started, runs.now(), results, agent=agent)
    return results


def main(argv=None, client=None, out_dir=OUT_DIR, core=sqlrun.CORE, agent=sqlrun.AGENT):
    ap = argparse.ArgumentParser(prog="python -m core.detect.backtest",
                                 description="Backtest the series test and switch it on where it passes.")
    ap.add_argument("--as-of", type=date.fromisoformat, help="last replayed day, YYYY-MM-DD")
    ap.add_argument("--report", help="dry run from a saved backtest result file: print the rows it would write")
    ap.add_argument("--days", type=int, help=f"days replayed, ending on --as-of (default {WINDOW_DAYS})")
    ap.add_argument("--apply", nargs="?", const=True, default=False, metavar="FILE",
                    help="append the test_switch rows and the runs row; with FILE, write the rows of that reviewed "
                         "result file (it must be as of --as-of) instead of replaying again")
    a = ap.parse_args(argv)
    if a.report:
        if a.apply or a.as_of:
            ap.error("--report reads a saved result file and takes neither --apply nor --as-of")
        return report(a.report)
    if a.as_of is None:
        ap.error("--as-of is required")
    if isinstance(a.apply, str):
        if a.days is not None:
            ap.error("--apply FILE writes a reviewed result and takes no --days")
        return apply_file(a.apply, a.as_of, client, core, agent)
    if client is None:
        client = bigquery.Client(project=PROJECT)
    res = run(client, a.as_of, apply=a.apply, days=WINDOW_DAYS if a.days is None else a.days, out_dir=out_dir,
              core=core, agent=agent)
    print(f"backtest {res['run_id']} as of {res['as_of']}: {res['series']['tested']} of "
          f"{res['series']['replayed']} series-days tested over {res['days']} days")
    print("recall: " + ", ".join(f"{m}x {r['recall'] if r['recall'] is None else round(r['recall'], 3)} "
                                 f"of {r['injected']} ({r['infeasible']} infeasible)" for m, r in res["recall"].items()))
    for name, k in res["keys"].items():
        print(f"  {name}: {'passes' if k['switch'] else 'stays off: ' + '; '.join(k['reasons'])}")
    for k in res["skipped"]:
        print(f"  {k['key']}: skipped: {k['reason']} ({k['observed_days']} observed days, "
              f"{k['replayed']} series-days replayed)")
    verb = "switched on" if a.apply else "would switch on"
    for r in res["switch_on"]:
        print(f"{verb}: {r['market']}|{r['platform']}|{r['lane_class']} from {r['switched_on']}")
    if not res["switch_on"]:
        print("nothing new to switch on")
    print(f"results: {out_dir / (res['run_id'] + '.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
