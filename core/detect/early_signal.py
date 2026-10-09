"""A negative binomial CUSUM early signal for rising topics (METHOD-GAPS Gap 2a). Information only.

The daily Rising test (state.sql) needs one significant day at 2 times the usual level. A topic that climbs
1.3, 1.6, 1.9 times over four days, or settles at 1.5 times, gives it nothing until the day it crosses 2. A
cumulative sum chart adds up small daily departures from the baseline until together they are too many to be
chance (Page 1954; for count data the negative binomial chart of Hoehle and Paul 2008, glrnb in the R package
surveillance, and Alencar, Ho and Esparza Albarracin 2017).

The baseline is the one the series test already fits. It is not fitted again here: stats._nb_tests sets, for
each tested negative binomial series, mu (the Farrington weighted median baseline, scaled, times the weekday
factor; stats.py lines 197 to 252) and alpha (the pooled negative binomial dispersion, line 250). The signal
reads those two numbers from the series_test row, the weekday factors stats.weekday_factors gives the other
days of the window, and the history stats read from tvf_series_signal.

For a day with mean mu and dispersion alpha (variance mu + alpha mu^2, n = 1 / alpha), the log likelihood
ratio of a mean of KAPPA mu against mu is linear in the count y:

    llr = y log(KAPPA (n + mu) / (n + KAPPA mu)) - n log((n + KAPPA mu) / (n + mu))

and the chart is S = max(0, S + llr), which alarms when S reaches H. KAPPA is 1.5, the shift the chart is
tuned to see. Because llr is a true log likelihood ratio, E[exp(llr)] is 1 under the in-control mean, which
is why a threshold of a few units gives an average run length in the hundreds of days (Siegmund 1985).

Reset rule. S is floored at 0 each day, so evidence that stops accumulating is forgotten. For the run length,
S also returns to 0 after each alarm (the renewal form of Page's scheme). The daily signal has no memory
beyond WINDOW_DAYS days: S restarts from 0 at the first day of that window, so the same series, baselines and
history always give the same answer and nothing carries from yesterday's run. A window can only lower S
against the unbounded chart, so the run length of the unbounded chart, which average_run_length measures, is
a lower bound for the signal's own.

H is 6.5. Known baselines give an in-control average run length of at least 3,700 days at that limit
(simulated in tests/test_detect_early_signal.py with a fixed seed, against a budget of 600 days, which is 0.05
false flags per series in a 30 day month, backtest.py's FA_MAX). The baselines are not known, they are fitted
every day by stats._nb_tests, and that estimation noise makes the real chart alarm more often than the known
baseline theory says. So H was chosen from the replayed placebo windows (early_signal_backtest.py): of the limits
4.5 to 6.5 in steps of 0.5, 6.5 is the first at which alarm episodes stay at or under 0.05 per series-month in all
three synthetic worlds tried (0.0275, 0.025 and 0.04 over 12,000 series-days each; 5.0 gave 0.09, 0.08 and 0.12).

Frozen mode. The refit baseline is fitted afresh every day from the last 28 days, a window that holds a rise
once it has run a few days, so a sustained rise partly raises its own baseline and the chart under-reads it.
signal can instead score the window against an earlier series_test row of the same series (frozen_baseline: the
row fitted just before the window, else the earliest row inside it, stated in baseline_source). That baseline
cannot have absorbed the rise, but it is a single stale estimate held for 14 days, and its estimation error is a
steady drift the chart integrates, so the same limit alarms far more often on stationary series. The limit for
the frozen mode, 9.0, was chosen the same way as H, from the replayed placebo windows: of 5.0 to 11.0 it is the
first at which episodes stay under 0.05 per series-month in all three synthetic worlds with a margin (0.0275,
0.01 and 0.0325; 8.5 gave 0.0325, 0.0175 and 0.045, 6.5 gave 0.0975, 0.035 and 0.12). The refit mode stays the
default; the job does not load earlier series_test rows, so the frozen mode costs one extra read.

Nothing here makes an item Rising, publishes it or ranks it. The recording step (record, further down)
writes to its own table, early_signal, which no gate, state, card, payload or rank reads.
"""

import datetime
import math

import numpy as np

from . import sqlrun

KAPPA = 1.5             # the upward shift in the mean the chart is tuned to see
H = 6.5                 # decision limit, in log likelihood ratio units, against the refit baseline
H_FROZEN = 9.0          # decision limit against a baseline frozen before the window (see below)
WINDOW_DAYS = 14        # the signal looks at the last 14 days, today included
RULE_VERSION = "early-1"
TABLE = "early_signal"        # defined in core/schema/early_signal.sql, created only by the setup runner
MODES = ("refit", "frozen")
DEFAULT_MODE = "refit"
EARLIER_DAYS = 3             # a frozen baseline may be up to 3 days older than the day before the window
COLUMNS = ("metric_date", "series_id", "item_id", "market", "platform", "series", "protocol", "lane_class", "kind",
           "y", "mu", "alpha", "cusum", "early", "run_days", "days_used", "kappa", "h", "window_days",
           "baseline_mode", "baseline_source", "baseline_date", "run_id", "rule_version")
EARLIER_SQL = """
SELECT st.series_id, st.metric_date, st.mu, st.alpha, st.weekday_factor
FROM {core}.v_series_test_current st
WHERE st.test = 'nb' AND st.mu > 0
  AND st.metric_date BETWEEN DATE_SUB(@d, INTERVAL @n DAY) AND DATE_SUB(@d, INTERVAL 1 DAY)
"""


def _terms(mu, alpha, kappa=KAPPA):
    """(a, b) with llr(y) = a y - b for a day of mean mu; the Poisson limit when alpha is under 1e-9."""
    if alpha < 1e-9:
        return math.log(kappa), mu * (kappa - 1)
    a = math.log(kappa) + math.log1p(alpha * mu) - math.log1p(kappa * alpha * mu)
    b = (math.log1p(kappa * alpha * mu) - math.log1p(alpha * mu)) / alpha
    return a, b


def llr(y, mu, alpha, kappa=KAPPA):
    """Log likelihood ratio of a negative binomial mean of kappa mu against mu, for the count y."""
    a, b = _terms(mu, alpha, kappa)
    return a * y - b


def cusum(ys, mus, alpha, kappa=KAPPA, h=H, reset_on_alarm=False):
    """The path of S = max(0, S + llr) over the counts ys with daily baselines mus; with reset_on_alarm, the
    day after S reaches h starts again from 0 (the alarm day keeps its value)."""
    s, path = 0.0, []
    for y, mu in zip(ys, mus):
        s = max(0.0, s + llr(y, mu, alpha, kappa))
        path.append(s)
        if reset_on_alarm and s >= h:
            s = 0.0
    return path


def average_run_length(mu, alpha, h=H, kappa=KAPPA, chains=2000, days=500, seed=0, y_fn=None):
    """Mean days between alarms of the in-control chart with reset after each alarm, from chains x days
    simulated days of a negative binomial series at its baseline mu (the number of days over the number of
    alarms; infinite when none). y_fn(rng, size) replaces the default negative binomial draw."""
    rng = np.random.default_rng(seed)
    a, b = _terms(mu, alpha, kappa)
    s = np.zeros(chains)
    alarms = 0
    for _ in range(days):
        if y_fn is not None:
            y = y_fn(rng, chains)
        elif alpha < 1e-9:
            y = rng.poisson(mu, chains)
        else:
            n = 1 / alpha
            y = rng.negative_binomial(n, n / (n + mu), chains)
        s = np.maximum(0.0, s + a * y - b)
        hit = s >= h
        alarms += int(hit.sum())
        s[hit] = 0.0
    return chains * days / alarms if alarms else math.inf


def signal(test_row, hist, factor, h=None, kappa=KAPPA, window=WINDOW_DAYS, baseline=None):
    """The early signal for one negative binomial series_test row, or None when it has no baseline.

    test_row carries metric_date, y, mu, alpha and weekday_factor as stats.series_test_rows wrote them. hist is
    the series' history as tvf_series_signal gave it (dicts with day and y). factor(day) is the weekday factor
    of that day's group, so day u has baseline mu x factor(u) / weekday_factor. Returns the final S, whether it
    is at or over h, how many days in a row S has been above 0 and how many observed days were used. Only mu,
    alpha, y, the weekday factors and the history are read: not the p-value, q, ratio or significance.

    By default the baseline is today's refit (the refit mode). baseline, a dict with mu, alpha and weekday_factor
    from an earlier series_test row of the same series, replaces it (the frozen mode): a baseline fitted before the
    window cannot have absorbed a rise inside it. The weekday factor of the day it was fitted on rescales it to
    each day of the window."""
    base = baseline if baseline is not None else test_row
    if h is None:
        h = H if baseline is None else H_FROZEN
    mu, alpha = base.get("mu"), base.get("alpha")
    if test_row.get("test") != "nb" or alpha is None or mu is None or not mu > 0 or test_row.get("y") is None:
        return None
    d = test_row["metric_date"]
    f_today = base.get("weekday_factor") or 1.0
    first = d.toordinal() - window + 1
    days = sorted((h_["day"], h_["y"]) for h_ in hist or ()
                  if h_["y"] is not None and first <= h_["day"].toordinal() < d.toordinal())
    days.append((d, test_row["y"]))
    ys = [math.floor(max(float(y), 0.0) + 0.5) for _, y in days]
    mus = [mu * factor(u) / f_today for u, _ in days]
    path = cusum(ys, mus, alpha, kappa, h)
    run = 0
    for s in reversed(path):
        if s <= 0:
            break
        run += 1
    return {"cusum": path[-1], "alarm": path[-1] >= h, "run_days": run, "days_used": len(path), "h": h}


def factor_fn(row, factors):
    """factor(day) for a series_test row: the weekday factors of the group the series test used for it. A row
    written under the candidate series rule used its own series and protocol factors, every other row the
    market, platform and lane class factors (stats._nb_tests); a missing weekday is 1."""
    from . import stats

    key = stats._series_weekday_key(row) if row.get("rule_version") == stats.SERIES_RULE_VERSION else stats._key(row)
    by_weekday = factors.get(key, {})
    return lambda d: by_weekday.get(d.weekday(), 1.0)



def frozen_baseline(rows, d, window=WINDOW_DAYS):
    """(earlier series_test row, source) for one series' earlier rows (dicts with metric_date, mu, alpha and
    weekday_factor), from day d. 'frozen': the latest row dated up to the day before the window starts and no
    more than EARLIER_DAYS older. 'earliest_in_window': when there is none, the earliest row inside the window,
    which may already carry part of a rise. (None, 'refit'): when there is neither, the signal falls back to
    today's refit baseline."""
    ok = [r for r in rows if r.get("mu") and r["mu"] > 0 and r.get("alpha") is not None]
    cut = d.toordinal() - window
    before = [r for r in ok if cut - EARLIER_DAYS <= r["metric_date"].toordinal() <= cut]
    if before:
        return max(before, key=lambda r: r["metric_date"]), "frozen"
    inside = [r for r in ok if cut < r["metric_date"].toordinal() < d.toordinal()]
    if inside:
        return min(inside, key=lambda r: r["metric_date"]), "earliest_in_window"
    return None, "refit"


def earlier_rows(client, d, core):
    """{series_id: [earlier negative binomial series_test rows]} for the days frozen_baseline may use. One read of
    v_series_test_current. The job does not load these rows for the series test, so only the frozen mode reads
    them."""
    out = {}
    for r in sqlrun.query(client, EARLIER_SQL, {"d": d, "n": WINDOW_DAYS + EARLIER_DAYS}, core=core):
        out.setdefault(r["series_id"], []).append(r)
    return out


def early_rows(signal_rows, series_rows, totals, d, run_id, mode=DEFAULT_MODE, earlier=None):
    """early_signal rows for day d: one per negative binomial series_test row that has a baseline. signal_rows are
    the tvf_series_signal rows the day was tested from and series_rows what stats.series_test_rows made of them, in
    the same order. In the frozen mode earlier maps series_id to its earlier rows; mu and alpha on the output row
    are the baseline the chart was scored against."""
    assert len(signal_rows) == len(series_rows)
    from . import stats

    factors = stats.weekday_factors(totals)
    out = []
    for s, r in zip(signal_rows, series_rows):
        if r["test"] != "nb":
            continue
        base, source = (frozen_baseline((earlier or {}).get(r["series_id"], []), d) if mode == "frozen"
                        else (None, "refit"))
        sig = signal(r, s["hist"], factor_fn(r, factors), baseline=base)
        if sig is None:
            continue
        used = base if base is not None else r
        row = {**{c: r[c] for c in ("series_id", "item_id", "market", "platform", "series", "protocol",
                                    "lane_class", "kind", "y")},
               "mu": used["mu"], "alpha": used["alpha"],
               "metric_date": d, "cusum": sig["cusum"], "early": sig["alarm"], "run_days": sig["run_days"],
               "days_used": sig["days_used"], "kappa": KAPPA, "h": sig["h"], "window_days": WINDOW_DAYS,
               "baseline_mode": mode, "baseline_source": source,
               "baseline_date": base["metric_date"] if base is not None else None,
               "run_id": run_id, "rule_version": RULE_VERSION}
        out.append({c: row[c] for c in COLUMNS})
    return out


def _json(value):
    return value.isoformat() if isinstance(value, (datetime.date, datetime.datetime)) else value


def record(client, d, run_id, signal_rows, series_rows, totals, core, mode=None):
    """Append day d's early_signal rows and return how many. The table is defined in core/schema/early_signal.sql
    and created only by the setup runner: this never issues DDL, and a missing table raises NotFound for the
    caller to log. mode is DEFAULT_MODE (refit) unless given; the frozen mode makes one extra read. Nothing reads the
    table: no gate, state, card, payload or rank."""
    from google.cloud import bigquery

    mode = DEFAULT_MODE if mode is None else mode
    if mode not in MODES:
        raise ValueError(f"unknown early signal mode: {mode}")
    earlier = earlier_rows(client, d, core) if mode == "frozen" else None
    rows = early_rows(signal_rows, series_rows, totals, d, run_id, mode, earlier)
    if not rows:
        return 0
    table = client.get_table(f"{core}.{TABLE}")
    config = bigquery.LoadJobConfig(schema=table.schema, source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                                    write_disposition=bigquery.WriteDisposition.WRITE_APPEND)
    client.load_table_from_json([{k: _json(v) for k, v in r.items()} for r in rows], table,
                                job_config=config).result()
    return len(rows)
