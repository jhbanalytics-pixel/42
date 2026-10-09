"""The series test step (DATA.md section 3.5, TRUST.md section 4, task 1.19).

Every tvf_series_signal row becomes one series_test row. A series is tested only when a test_switch row in
force covers its market, platform and lane class; otherwise it is copied with test 'none', every statistic
NULL and significant FALSE, exactly as in warm-up. Panel and counter series get the negative binomial test,
rank lists the beta-binomial, and Benjamini-Hochberg runs per market and day over the tested series. The
rows are appended by a load job; nothing is ever truncated or replaced.

numpy, scipy and statsmodels are imported inside the functions that test, so a day with no switch in force
runs on an image without them.
"""

import datetime
import logging
import math
import warnings
from collections import defaultdict
from pathlib import Path

from google.cloud import bigquery

from . import sqlrun

log = logging.getLogger(__name__)

SIGNAL_SQL = "SELECT * FROM {core}.tvf_series_signal(@d)"
SWITCH_SQL, TOTALS_SQL, FIRST_WEEK_SQL = sqlrun.split(
    (Path(__file__).parent / "sql" / "stats.sql").read_text(encoding="utf-8"))

RULE_VERSION = "stats-1"
SERIES_RULE_VERSION = "stats-2-series"
Q_MAX = 0.05
TESTABLE = ("short", "ok")
NB_LANES = ("unbiased_counter", "panel")
CONTEXT_ONLY = {"board_global_music"}      # DATA.md 3.2: a rank list shown for context, never tested
PRIOR_ITEMS, DISPERSION_SERIES = 20, 30

COLUMNS = [
    "metric_date", "series_id", "item_id", "market", "platform", "series", "protocol", "lane_class", "kind",
    "y", "trials", "obs_prior", "obs28", "first_measured", "baseline_state", "hist_mean", "med", "v3", "v7",
    "peak28", "vel", "accel", "z_display", "test", "mu", "alpha", "weekday_factor", "mu_prior", "ratio",
    "p_mid", "q", "significant", "run_id", "rule_version",
]

UNTESTED = {"test": "none", "mu": None, "alpha": None, "weekday_factor": None, "mu_prior": None,
            "ratio": None, "p_mid": None, "q": None, "significant": False}


def passthrough_rows(signal_rows, d, run_id, rule_version):
    """series_test rows for day d from tvf_series_signal rows, all with test 'none'."""
    return [{"metric_date": d, **{k: v for k, v in row.items() if k != "hist"}, **UNTESTED,
             "run_id": run_id, "rule_version": rule_version} for row in signal_rows]


def in_force(switch_rows, d):
    """(market, platform, lane_class) pairs with a test_switch row switched on by day d. The rows are those
    SWITCH_SQL returns, which only holds rows whose backtest run was accepted."""
    return {(s["market"], s["platform"], s["lane_class"]) for s in switch_rows
            if s.get("switched_on") is not None and s["switched_on"] <= d}


def _key(row):
    return row["market"], row["platform"], row["lane_class"]


def _series_weekday_key(row):
    return row["market"], None, row["lane_class"], row["series"], row["protocol"]


# Negative binomial pieces


def weekday_factors(totals):
    """Factors keyed by market, platform and lane class, plus series and protocol for platformless candidate panels.
    The weekday mean over the mean of all days is shrunk n/(n + 4) towards 1, and is 1 while n is under 4."""
    import numpy as np
    by = defaultdict(list)
    for t in totals:
        own = t["platform"] is None and t["lane_class"] == "panel" and t.get("series") and t.get("protocol")
        key = _series_weekday_key(t) if own else _key(t)
        by[key].append((t["day"].weekday(), float(t["total"])))
    out = {}
    for key, vals in by.items():
        mean_all = float(np.mean([v for _, v in vals]))
        f = {}
        for wd in range(7):
            day_vals = [v for w, v in vals if w == wd]
            n = len(day_vals)
            f[wd] = 1.0 if n < 4 or mean_all == 0 else 1 + n / (n + 4) * (float(np.mean(day_vals)) / mean_all - 1)
        out[key] = f
    return out


def weighted_median(values, weights):
    """The value where the sorted cumulative weight reaches half; the midpoint when it lands exactly on half."""
    import numpy as np
    order = np.argsort(values, kind="stable")
    v, c = np.asarray(values, float)[order], np.cumsum(np.asarray(weights, float)[order])
    half = c[-1] / 2
    i = int(np.argmax(c >= half * (1 - 1e-12)))
    if math.isclose(c[i], half) and i + 1 < len(v):
        return float((v[i] + v[i + 1]) / 2)
    return float(v[i])


def farrington_base(zs, alpha):
    """Weekday-adjusted level before scaling: the median m of zs, then the weighted median with days whose
    Anscombe residual r = 1.5 (z^(2/3) - m^(2/3)) / (sqrt(phi) m^(1/6)), phi = 1 + alpha m, exceeds 2
    weighted r^-2 (Farrington flexible, Noufaily et al. 2013). With m = 0 the residual is undefined and the
    plain mean is used."""
    import numpy as np
    z = np.asarray(zs, float)
    m = float(np.median(z))
    if m == 0:
        return float(z.mean())
    r = 1.5 * (z ** (2 / 3) - m ** (2 / 3)) / (math.sqrt(1 + alpha * m) * m ** (1 / 6))
    return weighted_median(z, np.where(r > 2, np.maximum(r, 2) ** -2.0, 1.0))


def _prior_keys(r):
    return [(r["lane_class"], r["kind"], r["market"], r["platform"], r["series"]),
            (r["lane_class"], r["kind"], r["market"]),
            (r["lane_class"], r["kind"])]


def cold_start_prior(first_weeks):
    """A function from a series row to its mu_prior: the 90th percentile of first-week means of its kind,
    market, platform and series, pooling across platforms and then markets while under 20 items."""
    import numpy as np
    levels = [defaultdict(list) for _ in range(3)]
    for w in first_weeks:
        for level, key in zip(levels, _prior_keys(w)):
            level[key].append(float(w["first_week_mean"]))

    def prior(row):
        vals = []
        for level, key in zip(levels, _prior_keys(row)):
            vals = level.get(key, [])
            if len(vals) >= PRIOR_ITEMS:
                break
        return float(np.percentile(vals, 90)) if vals else None

    return prior


def _nb2_fit(ys, offsets):
    """(alpha, scale) of an intercept-only NB2 model with offset log(baseline), scale = exp(intercept);
    None when the fit does not converge."""
    import numpy as np
    from statsmodels.discrete.discrete_model import NegativeBinomial

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = NegativeBinomial(ys, np.ones((len(ys), 1)), loglike_method="nb2", offset=offsets).fit(disp=0)
    const, alpha = float(res.params[0]), float(res.params[-1])
    if res.mle_retvals["converged"] and math.isfinite(alpha) and math.isfinite(const) and alpha >= 0:
        return alpha, math.exp(const)
    return None


def pooled_fit(ys, offsets):
    """(alpha, scale) over pooled histories. The scale is the fit's own intercept: how far the pooled mean sits
    from the median baselines in the offsets. By moments, alpha floored at 0, when the NB2 fit fails."""
    import numpy as np
    if not ys:
        return 0.0, 1.0
    ys, offsets = np.asarray(ys, float), np.asarray(offsets, float)
    try:
        fit = _nb2_fit(ys, offsets)
    except Exception:
        fit = None
    if fit is None:
        base = np.exp(offsets)
        scale = float(ys.sum() / base.sum())
        m = base * scale
        fit = max(0.0, float(np.sum((ys - m) ** 2 - m) / np.sum(m ** 2))), scale
    return fit


def nb_p_mid(y, mu, alpha):
    """P(Y > y) + 0.5 P(Y = y) for Y negative binomial with mean mu and dispersion alpha; Poisson at alpha 0."""
    assert mu > 0, "a zero mean is never tested"
    from scipy import stats as st

    if alpha == 0:
        dist = st.poisson(mu)
    else:
        n = 1 / alpha
        dist = st.nbinom(n, n / (n + mu))
    return float(dist.sf(y) + 0.5 * dist.pmf(y))


def _dispersion_keys(t):
    return [(t["market"], t["platform"], t["kind"], t["band"]), (t["market"], t["platform"], t["kind"]),
            (t["market"], t["platform"]), (t["market"],)]


def _nb_tests(nb, d, factors, prior, series_on=()):
    """Fills weekday_factor, mu_prior, alpha, mu, ratio and p_mid on each nb work dict; returns the ones with
    mu above 0, the rest stay untested. The plain median baseline sets the volume band and the pooled fit
    (alpha for the residual's phi), then the Farrington weighted median with that alpha, then a refit on those
    Farrington baselines gives the alpha and the scale the test uses; the scale only ever raises mu. A median of 0
    makes a series cold: it needs the prior."""
    import numpy as np

    for t in nb:
        s = t["row"]
        own = _series_weekday_key(s)
        use_series = _key(s) in series_on and s["lane_class"] == "panel" and own in factors
        f = factors.get(own if use_series else _key(s), {})
        t["weekday_rule_version"] = SERIES_RULE_VERSION if use_series else RULE_VERSION
        t["f"] = f.get(d.weekday(), 1.0)
        hist = sorted(s["hist"] or [], key=lambda h: h["day"])
        t["hist_y"] = [max(float(h["y"]), 0.0) for h in hist]
        t["hist_f"] = [f.get(h["day"].weekday(), 1.0) for h in hist]
        t["z"] = [y / fh for y, fh in zip(t["hist_y"], t["hist_f"])]
        t["m"] = float(np.median(t["z"]))
        cold = t["m"] == 0 or s["baseline_state"] == "short" or (
            s["first_measured"] is not None and (d - s["first_measured"]).days < 28)
        t["mu_prior"] = prior(s) if cold else None
        t["market"], t["platform"], t["kind"] = s["market"], s["platform"], s["kind"]
        t["band"] = math.floor(math.log2(1 + max(t["m"] * t["f"], t["mu_prior"] or 0)))
    nb = [t for t in nb if t["m"] > 0 or t["mu_prior"]]

    groups = [defaultdict(list) for _ in range(4)]
    for t in nb:
        for level, key in zip(groups, _dispersion_keys(t)):
            level[key].append(t)

    def fit(members, baseline):
        ys, offsets = [], []
        for m in members:
            if m["m"] > 0 and m[baseline] > 0:
                ys += [math.floor(y + 0.5) for y in m["hist_y"]]
                offsets += [math.log(m[baseline] * fh) for fh in m["hist_f"]]
        return pooled_fit(ys, offsets)

    chosen = {}
    for t in nb:
        for i, key in enumerate(_dispersion_keys(t)):
            members = groups[i][key]
            if len(members) >= DISPERSION_SERIES:
                break
        t["group"] = (i, key)
        chosen[(i, key)] = members
    first = {g: fit(members, "m")[0] for g, members in chosen.items()}
    for t in nb:
        t["base"] = farrington_base(t["z"], first[t["group"]])
    final = {g: fit(members, "base") for g, members in chosen.items()}
    for t in nb:
        t["alpha"], scale = final[t["group"]]
        mu_item = t["base"] * max(scale, 1.0) * t["f"]
        t["mu"] = mu_item if t["mu_prior"] is None else max(mu_item, t["mu_prior"])

    kept = [t for t in nb if t["mu"] > 0]
    for t in kept:
        t["p_mid"] = nb_p_mid(t["y"], t["mu"], t["alpha"])
        t["ratio"] = (t["y"] + 1) / (t["mu"] + 1)
    return kept


# Beta-binomial pieces


def beta_prior(rates):
    """Beta(a0, b0) by moments from appearance rates; Beta(1, 1) when the moments admit no beta."""
    import numpy as np
    m, v = float(np.mean(rates)), float(np.var(rates))
    if 0 < m < 1 and 0 < v < m * (1 - m):
        c = m * (1 - m) / v - 1
        return m * c, (1 - m) * c
    return 1.0, 1.0


def _list_key(s):
    return s["market"], s["series"], s["protocol"], s["kind"]


def _counted(hist):
    """Every hist day carries its pull count."""
    return all(h["n"] is not None for h in hist)


def _betabinom_tests(bb, signal_rows):
    from scipy import stats as st

    rates = defaultdict(list)
    for s in signal_rows:
        hist = s["hist"] or []
        if s["lane_class"] == "unbiased_rank" and hist and _counted(hist):
            n = sum(h["n"] for h in hist)
            if n > 0:
                rates[_list_key(s)].append(sum(min(h["y"], h["n"]) for h in hist) / n)
    priors = {}
    for t in bb:
        s = t["row"]
        key = _list_key(s)
        if key not in priors:
            priors[key] = beta_prior(rates[key]) if rates[key] else (1.0, 1.0)
        a0, b0 = priors[key]
        hist = sorted(s["hist"], key=lambda h: h["day"])
        a = a0 + sum(min(h["y"], h["n"]) for h in hist)
        b = b0 + sum(h["n"] - min(h["y"], h["n"]) for h in hist)
        x = int(round(t["y"] + sum(h["y"] for h in hist[-2:])))
        n = int(s["trials"] + sum(h["n"] for h in hist[-2:]))
        if x > n:
            log.warning("appearances above pulls set to the pulls: %s had %s in %s pulls", s["series_id"], x, n)
            x = n
        dist = st.betabinom(n, a, b)
        t["p_mid"] = float(dist.sf(x) + 0.5 * dist.pmf(x))
        t["ratio"] = (x / n) / (a / (a + b))


# The step


def _test_for(s, d, on):
    """(test, y as tested) for one series; test 'none' when DATA.md 3.5 rule 1 applies."""
    if s["baseline_state"] not in TESTABLE or s["y"] is None or s["series"] in CONTEXT_ONLY or _key(s) not in on:
        return "none", None
    if s["lane_class"] in NB_LANES:
        test, y = "nb", float(math.floor(max(s["y"], 0) + 0.5))
        if s["y"] < 0:
            log.warning("negative counter delta set to 0: %s on %s was %s", s["series_id"], d, s["y"])
    elif s["lane_class"] == "unbiased_rank":
        if not s["trials"] or not _counted(s["hist"] or []):
            return "none", None
        test, y = "betabinom", float(s["y"])
    else:
        return "none", None
    if y == 0 and not any(h["y"] > 0 for h in s["hist"] or []):
        return "none", None
    return test, y


def series_test_rows(signal_rows, d, run_id, rule_version, switch_rows=(), totals=(), first_weeks=()):
    """series_test rows for day d, in the series_test column order. Series not covered by a test_switch row
    in force are untested. Tested runs carry RULE_VERSION, except NB rows using the explicit SERIES_RULE_VERSION
    weekday exposure."""
    on = in_force(switch_rows, d)
    series_on = in_force([s for s in switch_rows if s.get("rule_version") == SERIES_RULE_VERSION], d)
    work = []
    for i, s in enumerate(signal_rows):
        test, y = _test_for(s, d, on)
        if test != "none":
            work.append({"i": i, "row": s, "test": test, "y": y})
    nb = [t for t in work if t["test"] == "nb"]
    if nb:
        kept = {t["i"] for t in _nb_tests(nb, d, weekday_factors(totals), cold_start_prior(first_weeks), series_on)}
        work = [t for t in work if t["test"] != "nb" or t["i"] in kept]
    bb = [t for t in work if t["test"] == "betabinom"]
    if bb:
        _betabinom_tests(bb, signal_rows)

    families = defaultdict(list)
    for t in work:
        families[t["row"]["market"]].append(t)
    if families:
        from scipy import stats as st
    for family in families.values():
        qs = st.false_discovery_control([t["p_mid"] for t in family], method="bh")
        for t, q in zip(family, qs):
            t["q"] = float(q)

    tested = {t["i"]: t for t in work}
    version = RULE_VERSION if work else rule_version
    out = []
    for i, s in enumerate(signal_rows):
        row = {"metric_date": d, **s, **UNTESTED, "run_id": run_id, "rule_version": version}
        t = tested.get(i)
        if t:
            nbt = t["test"] == "nb"
            row.update(test=t["test"], ratio=float(t["ratio"]), p_mid=t["p_mid"], q=t["q"],
                       significant=bool(t["q"] <= Q_MAX),
                       mu=float(t["mu"]) if nbt else None, alpha=float(t["alpha"]) if nbt else None,
                       weekday_factor=float(t["f"]) if nbt else None,
                       mu_prior=t["mu_prior"] if nbt else None)
            if nbt:
                row["rule_version"] = t["weekday_rule_version"]
        out.append({c: row[c] for c in COLUMNS})
    return out


def _json(value):
    return value.isoformat() if isinstance(value, (datetime.date, datetime.datetime)) else value


def run_stats(client, d, run_id, rule_version, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Read tvf_series_signal for day d, test the series switched on, append series_test. Returns the row count.
    The switch, weekday totals and first weeks are read only when some series could be tested. The switch rows
    come from sql/stats.sql, which keeps only those whose backtest run has an ok runs row in agent."""
    signal = sqlrun.query(client, SIGNAL_SQL, {"d": d}, core=core, agent=agent)
    switches, totals, first_weeks = [], [], []
    if any(r["baseline_state"] in TESTABLE for r in signal):
        switches = sqlrun.query(client, SWITCH_SQL, {"d": d}, core=core, agent=agent)
        on = in_force(switches, d)
        if any(_key(r) in on for r in signal):
            totals = sqlrun.query(client, TOTALS_SQL, {"d": d}, core=core, agent=agent)
            first_weeks = sqlrun.query(client, FIRST_WEEK_SQL, {"d": d}, core=core, agent=agent)
    rows = series_test_rows(signal, d, run_id, rule_version, switches, totals, first_weeks)
    if rows:
        table = client.get_table(f"{core}.series_test")
        config = bigquery.LoadJobConfig(schema=table.schema, source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                                        write_disposition=bigquery.WriteDisposition.WRITE_APPEND)
        client.load_table_from_json([{k: _json(v) for k, v in r.items()} for r in rows], table,
                                    job_config=config).result()
    return len(rows)
