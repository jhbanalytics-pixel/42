"""Spike-injection backtest of the early signal against the daily Rising test (METHOD-GAPS Gap 2a).

It reuses the machinery of backtest.py. Backtest.Store replays each day from what was available on it,
stats.series_test_rows tests the day exactly as production would once the market, platform and lane class is
switched on, and the placebo windows are the same replayed days. Where backtest.py injects a one day spike, this
injects a rise that lasts several days into a random share of the key's series, on a copy of the replayed
values, and follows the rise day by day.

For each injected series it records the first day after onset on which each rule fires:

 daily   the series is significant (Benjamini-Hochberg q at most 0.05, with the rest of its market family at their
         placebo values, as backtest._detect does) with a ratio of at least 2: state.sql's sig_ratio_today.
 rising  that, on at least 2 of the last 3 days: state.sql's persistence test (sig_days3 of 2). This is the series
         part of the Rising rule. The other ways into Rising (a second platform or market today) need other series
         and are not modelled, and the creator floors are not modelled either.
 cusum   early_signal.signal alarms.

and reports the median days the CUSUM is earlier than each rule over the series both find, how many only the CUSUM
finds, and a censored median that counts a rule that never fires as horizon + 1 days, which understates the CUSUM's
lead. The false flag rate is taken on the placebo days, with nothing injected: flagged days, alarm episodes (a
flagged day after an unflagged one) and episodes per series-month of 30 days, for all three rules.

An injected rise is a negative binomial draw whose mean is the multiplier for that day times the baseline the
series test fitted on the onset day (mu, scaled by the weekday factors), with the dispersion the test fitted. The
draws use a fixed seed. Series the CUSUM or the daily test already flag on the onset day are left out and counted.

Entry point: python -m core.detect.early_signal_backtest --as-of YYYY-MM-DD [--key MARKET|PLATFORM|LANE]. It reads
BigQuery through backtest.load and writes a markdown report and a JSON file; it writes nothing else.
"""

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from scipy import stats as st

from . import backtest, early_signal, sqlrun, stats

HORIZON_DAYS = 12
PLACEBO_DAYS = 28
REPLICATES = 3
SHARE = 0.25
SEED = 20261008
RISING_DAYS = (2, 3)        # state.sql: sig_days3 >= 2, significant with ratio >= 2 on 2 of the last 3 days
RATIO_MIN = 2.0
MONTH = 30


def step(m):
    """A sustained rise to m times the usual level from the first day."""
    return lambda k: float(m)


def ramp(g, days):
    """Geometric growth of g a day for days days, then held."""
    return lambda k: g ** min(k, days)


SCENARIOS = {
    "ramp 1.3x a day for 5 days": ramp(1.3, 5),
    "ramp 1.15x a day for 5 days": ramp(1.15, 5),
    "step 1.5x": step(1.5),
    "step 2x": step(2.0),
}


def _key_rows(store, t, key):
    """(on, switches) with the keys already in force on t and key switched on, as backtest._replay_day builds."""
    force_rows = [{**r, "rule_version": stats.RULE_VERSION} for r in store.switched_by(store.cutoff(t))]
    switches = [*force_rows, {**dict(zip(backtest.KEY_FIELDS, key)), "switched_on": t,
                              "rule_version": stats.RULE_VERSION}]
    return switches


class Replay:
    """The replayed days of one key, with placebo results cached by day."""

    def __init__(self, store, key, mode="refit"):
        if mode not in early_signal.MODES:
            raise ValueError(f"unknown early signal mode: {mode}")
        self.store, self.key, self.mode = store, key, mode
        self._placebo = {}
        self._earlier = {}
        self.sources = Counter({"frozen": 0, "earliest_in_window": 0, "refit": 0})

    def context(self, t):
        series = self.store.series(t)
        return series, self.store.totals(t), self.store.first_weeks(t, series), _key_rows(self.store, t, self.key)

    def test(self, t, signal, ctx):
        _, totals, weeks, switches = ctx
        return stats.series_test_rows(signal, t, "backtest", stats.RULE_VERSION, switches, totals, weeks)

    def placebo(self, t):
        """(signal rows, series_test rows, totals) of day t with nothing injected."""
        if t not in self._placebo:
            ctx = self.context(t)
            signal = self.store.signal(t, ctx[0])
            self._placebo[t] = (signal, self.test(t, signal, ctx), ctx[1])
        return self._placebo[t]


    def earlier(self, t):
        """{series_id: [earlier series_test rows]} for day t as the frozen mode reads them: the placebo rows of the
        days from the window length to three days before that (no injected rise has reached them, because the
        horizon is shorter than the window)."""
        if t not in self._earlier:
            out = defaultdict(list)
            for k in range(early_signal.WINDOW_DAYS, early_signal.WINDOW_DAYS + early_signal.EARLIER_DAYS + 1):
                _, rows, _ = self.placebo(t - timedelta(days=k))
                for r in rows:
                    if r["test"] == "nb" and r["mu"]:
                        out[r["series_id"]].append(r)
            self._earlier[t] = out
        return self._earlier[t]

    def early(self, t, row, signal_row, totals):
        """The early signal of one series_test row of day t in this replay's mode, counting where its baseline came
        from."""
        baseline, source = None, "refit"
        if self.mode == "frozen":
            baseline, source = early_signal.frozen_baseline(self.earlier(t).get(row["series_id"], []), t)
        self.sources[source] += 1
        factors = stats.weekday_factors(totals)
        return early_signal.signal(row, signal_row["hist"], early_signal.factor_fn(row, factors), baseline=baseline)


def _mine(rows, key):
    return {r["series_id"]: i for i, r in enumerate(rows) if r["test"] == "nb" and backtest._key(r) == key}


def _bh_q(ps, pos, p):
    trial = list(ps)
    trial[pos] = p
    return float(st.false_discovery_control(trial, method="bh")[pos])


def _daily_flag(q, ratio):
    return q is not None and q <= stats.Q_MAX and ratio is not None and ratio >= RATIO_MIN


def _rising(flags, k):
    """Whether the daily flags of days k - 2 to k (those that exist) hold at least 2."""
    return sum(flags[max(1, k - 2):k + 1]) >= RISING_DAYS[0]


def _episodes(days):
    """Alarm episodes in a list of flags by consecutive day: flagged days whose previous day was not."""
    return sum(1 for i, f in enumerate(days) if f and (i == 0 or not days[i - 1]))


def _rate(k, n):
    return k / n if n else None


def _placebo_report(replay, as_of, days):
    seqs = defaultdict(lambda: {"cusum": [], "daily": [], "rising": []})
    window = [as_of - timedelta(days=i) for i in range(days - 1, -1, -1)]
    for t in window:
        signal, rows, totals = replay.placebo(t)
        for sid, i in _mine(rows, replay.key).items():
            sig = replay.early(t, rows[i], signal[i], totals)
            if sig is None:
                continue
            s = seqs[sid]
            s["cusum"].append(sig["alarm"])
            s["daily"].append(_daily_flag(rows[i]["q"], rows[i]["ratio"]))
            s["rising"].append(sum(s["daily"][-RISING_DAYS[1]:]) >= RISING_DAYS[0])
    series_days = sum(len(s["cusum"]) for s in seqs.values())
    out = {"days": days, "series": len(seqs), "series_days": series_days}
    for rule in ("cusum", "daily", "rising"):
        flagged = sum(sum(s[rule]) for s in seqs.values())
        episodes = sum(_episodes(s[rule]) for s in seqs.values())
        out[rule] = {"flag_days": flagged, "flag_day_rate": _rate(flagged, series_days), "episodes": episodes,
                     "episodes_per_series_month": _rate(episodes * MONTH, series_days)}
    return out


def _median(values):
    return float(np.median(values)) if values else None


def _lead(a, b, horizon):
    """Days rule a fires before rule b (b - a), over the series both find, with the count only a finds and a
    median that counts a b that never fires as horizon + 1."""
    both = [y - x for x, y in zip(a, b) if x and y]
    only = [(horizon + 1) - x for x, y in zip(a, b) if x and not y]
    return {"pairs": len(both), "median": _median(both), "only_first_finds": len(only),
            "median_censored": _median(both + only), "only_second_finds": sum(1 for x, y in zip(a, b) if y and not x)}


def _injected(replay, as_of, scenarios, horizon, replicates, share, seed):
    t0 = as_of - timedelta(days=horizon)
    signal0, rows0, totals0 = replay.placebo(t0)
    factors0 = stats.weekday_factors(totals0)
    mine0 = _mine(rows0, replay.key)
    flagged0 = {sid for sid, i in mine0.items()
                if (replay.early(t0, rows0[i], signal0[i], totals0) or {}).get("alarm")
                or _daily_flag(rows0[i]["q"], rows0[i]["ratio"])}
    candidates = sorted(sid for sid, i in mine0.items() if rows0[i]["mu"] >= 1)
    out = {}
    for si, (name, mult) in enumerate(scenarios.items()):
        res = {"horizon": horizon, "series": [], "first_day": {"cusum": [], "daily": [], "rising": []},
               "already_flagged_at_onset": 0, "non_injected": {"series_days": 0, "cusum_flag_days": 0}}
        for rep in range(replicates):
            pick = np.random.default_rng([seed, rep]).choice(
                len(candidates), size=round(share * len(candidates)), replace=False)
            chosen = [candidates[j] for j in sorted(pick)]
            res["already_flagged_at_onset"] += sum(sid in flagged0 for sid in chosen)
            chosen = [sid for sid in chosen if sid not in flagged0]
            draws = np.random.default_rng([seed, rep, si + 1])
            counts = {}
            for sid in chosen:
                row = rows0[mine0[sid]]
                mu0, alpha, f0 = row["mu"], row["alpha"], row["weekday_factor"] or 1.0
                f = early_signal.factor_fn(row, factors0)
                counts[sid] = {}
                for k in range(1, horizon + 1):
                    u = t0 + timedelta(days=k)
                    mean = mult(k) * mu0 * f(u) / f0
                    n = 1 / alpha if alpha > 1e-9 else None
                    counts[sid][u] = (float(draws.poisson(mean)) if n is None
                                      else float(draws.negative_binomial(n, n / (n + mean))))
            flags = {sid: {"cusum": [None] * (horizon + 1), "daily": [False] * (horizon + 1)} for sid in chosen}
            for k in range(1, horizon + 1):
                t = t0 + timedelta(days=k)
                signal_p, rows_p, _ = replay.placebo(t)
                ctx = replay.context(t)
                series = ctx[0]
                for sid in chosen:
                    values = series[sid]["values"]
                    for u, y in counts[sid].items():
                        if u <= t:
                            values[u] = (y, None)
                signal = replay.store.signal(t, series)
                rows = replay.test(t, signal, ctx)
                tested = [i for i, r in enumerate(rows_p) if r["test"] != "none" and r["market"] == replay.key[0]]
                pos = {rows_p[i]["series_id"]: j for j, i in enumerate(tested)}
                ps = [rows_p[i]["p_mid"] for i in tested]
                at = {r["series_id"]: i for i, r in enumerate(rows)}
                for sid in chosen:
                    i = at.get(sid)
                    if i is None or rows[i]["test"] != "nb" or sid not in pos:
                        continue
                    sig = replay.early(t, rows[i], signal[i], ctx[1])
                    flags[sid]["cusum"][k] = sig["alarm"] if sig else None
                    q = _bh_q(ps, pos[sid], rows[i]["p_mid"])
                    flags[sid]["daily"][k] = _daily_flag(q, rows[i]["ratio"])
                for sid, i in _mine(rows, replay.key).items():
                    if sid in counts:
                        continue
                    sig = replay.early(t, rows[i], signal[i], ctx[1])
                    if sig is not None:
                        res["non_injected"]["series_days"] += 1
                        res["non_injected"]["cusum_flag_days"] += int(sig["alarm"])
            for sid in chosen:
                f = flags[sid]
                first = lambda seq: next((k for k in range(1, horizon + 1) if seq[k]), None)
                res["series"].append(f"{rep}:{sid}")
                res["first_day"]["cusum"].append(first(f["cusum"]))
                res["first_day"]["daily"].append(first(f["daily"]))
                res["first_day"]["rising"].append(next((k for k in range(1, horizon + 1)
                                                        if _rising(f["daily"], k)), None))
        first_days = res["first_day"]
        res["injected"] = len(res["series"])
        for rule in ("cusum", "daily", "rising"):
            found = [x for x in first_days[rule] if x]
            res[rule] = {"detected": len(found), "share": _rate(len(found), res["injected"]),
                         "median_day": _median(found)}
        res["days_earlier_than_daily"] = _lead(first_days["cusum"], first_days["daily"], horizon)
        res["days_earlier_than_rising"] = _lead(first_days["cusum"], first_days["rising"], horizon)
        out[name] = res
    return out


def run(inputs, as_of, key, *, horizon=HORIZON_DAYS, placebo_days=PLACEBO_DAYS, replicates=REPLICATES,
        share=SHARE, seed=SEED, scenarios=SCENARIOS, mode="refit"):
    """The backtest of the early signal for one market, platform and lane class key, as plain JSON types. mode is
    'refit' (scored against the baseline the series test fitted that day) or 'frozen' (against the baseline fitted
    before the window; see early_signal.frozen_baseline). The horizon must be shorter than the window."""
    if horizon >= early_signal.WINDOW_DAYS:
        raise ValueError("the horizon must be shorter than the window")
    replay = Replay(backtest.Store(inputs), key, mode)
    out = {"as_of": as_of.isoformat(), "key": "|".join(key), "mode": mode,
            "rule": {"kappa": early_signal.KAPPA, "h": early_signal.H, "h_frozen": early_signal.H_FROZEN,
                     "window_days": early_signal.WINDOW_DAYS,
                     "rule_version": early_signal.RULE_VERSION},
            "design": {"horizon": horizon, "placebo_days": placebo_days, "replicates": replicates,
                       "share": share, "seed": seed},
            "placebo": _placebo_report(replay, as_of, placebo_days),
            "scenarios": _injected(replay, as_of, scenarios, horizon, replicates, share, seed)}
    out["baseline_source"] = dict(replay.sources)
    return out


def _fmt(x, digits=2):
    return "none" if x is None else (f"{x:.{digits}f}".rstrip("0").rstrip(".") if isinstance(x, float) else str(x))


def markdown(res, source="a synthetic negative binomial panel"):
    p, d = res["placebo"], res["design"]
    lines = [f"# Early signal backtest, {res['key']}, as of {res['as_of']}, {res['mode']} baseline", "",
             f"Source: {source}. The CUSUM is information only: it changes no state, gate, rank or payload.",
             f"Rule {res['rule']['rule_version']}: negative binomial CUSUM tuned to a {res['rule']['kappa']} times shift, "
             f"decision limit {res['rule']['h_frozen'] if res['mode'] == 'frozen' else res['rule']['h']}, window {res['rule']['window_days']} days. Injection: "
             f"{d['replicates']} replicates of {round(d['share'] * 100)}% of the key's series, "
             f"{d['horizon']} days after onset, seed {d['seed']}. Baseline mode: {res['mode']}"
             + (" (scored against the series test row fitted just before the window; "
                f"sources {res['baseline_source']})." if res["mode"] == "frozen" else
                " (the baseline the series test refit that day)."), "",
             "## False flags on non-injected series", "",
             f"{p['series']} series over {p['days']} placebo days, {p['series_days']} series-days.", "",
             "| Rule | Flag days | Flag-day rate | Alarm episodes | Episodes per series-month |",
             "|---|---|---|---|---|"]
    for rule, label in (("cusum", "CUSUM early signal"), ("daily", "Daily test (significant, ratio 2 or more)"),
                        ("rising", "Rising series part (2 of the last 3 days)")):
        r = p[rule]
        lines.append(f"| {label} | {r['flag_days']} | {_fmt(r['flag_day_rate'], 4)} | {r['episodes']} | "
                     f"{_fmt(r['episodes_per_series_month'], 4)} |")
    lines += ["", "The budget is 0.05 episodes per series-month.", "", "## Injected rises", ""]
    for name, s in res["scenarios"].items():
        e, r = s["days_earlier_than_daily"], s["days_earlier_than_rising"]
        lines += [f"### {name}", "",
                  f"{s['injected']} series injected, {s['already_flagged_at_onset']} left out as flagged on the onset day. "
                  f"Non-injected series during the horizon: {s['non_injected']['cusum_flag_days']} CUSUM flag days "
                  f"in {s['non_injected']['series_days']} series-days.", "",
                  "| Rule | Found | Share | Median first day |", "|---|---|---|---|"]
        for rule, label in (("cusum", "CUSUM"), ("daily", "Daily test"), ("rising", "Rising series part")):
            lines.append(f"| {label} | {s[rule]['detected']} | {_fmt(s[rule]['share'])} | {_fmt(s[rule]['median_day'])} |")
        lines += ["", "| Against | Pairs found by both | Median days earlier | Only the CUSUM finds | "
                  "Median earlier, never counted as horizon + 1 | Only that rule finds |", "|---|---|---|---|---|---|",
                  f"| Daily test | {e['pairs']} | {_fmt(e['median'])} | {e['only_first_finds']} | "
                  f"{_fmt(e['median_censored'])} | {e['only_second_finds']} |",
                  f"| Rising series part | {r['pairs']} | {_fmt(r['median'])} | {r['only_first_finds']} | "
                  f"{_fmt(r['median_censored'])} | {r['only_second_finds']} |", ""]
    lines += ["Median days earlier is the other rule's first day minus the CUSUM's, so a negative number means "
              "the CUSUM is later. Median days earlier counts only series both rules find.", ""]
    return "\n".join(lines)


def main(argv=None, client=None, out_dir=None):
    ap = argparse.ArgumentParser(prog="python -m core.detect.early_signal_backtest",
                                 description="Backtest the early signal against the daily Rising test.")
    ap.add_argument("--as-of", required=True, type=date.fromisoformat)
    ap.add_argument("--key", default="ZA|facebook|panel", help="market|platform|lane_class")
    ap.add_argument("--out", type=Path, default=Path("early_signal_backtest"), help="path stem for .md and .json")
    a = ap.parse_args(argv)
    if client is None:
        from google.cloud import bigquery
        client = bigquery.Client(project=backtest.PROJECT)
    days = PLACEBO_DAYS
    inputs = backtest.load(client, a.as_of, days, sqlrun.CORE, sqlrun.AGENT)
    res = run(inputs, a.as_of, tuple(a.key.split("|")))
    a.out.with_suffix(".json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    a.out.with_suffix(".md").write_text(markdown(res, "BigQuery replay"), encoding="utf-8")
    print(f"wrote {a.out.with_suffix('.md')} and {a.out.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
