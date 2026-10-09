"""Known-answer tests for the full series test (DATA.md section 3.5, TRUST.md section 4, task 1.19).

Every expected value is computed here from the rule as written, with scipy, numpy and statsmodels called
directly, then compared with what core/detect/stats.py returns. The last tests run run_stats end to end on
DuckDB fixture tables, which checks the read-only input queries in sql/stats.sql.
"""

import importlib
import logging
import math
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import sqlglot
from google.cloud import bigquery
from scipy import stats as st
from statsmodels.discrete.discrete_model import NegativeBinomial

from .. import sqlrun, stats
from . import duck
from .fixtures import D, at, day, run
from .test_detect_states import SERIES_TEST_COLUMNS, FakeJob, World

SQL = Path(__file__).resolve().parents[1] / "sql"
DATA_MD = Path(__file__).resolve().parents[3] / "docs" / "full-42" / "DATA.md"

PANEL = dict(market="ZA", platform="facebook", series="panel_fb_hub", protocol="p1", lane_class="panel")
COUNTER = dict(market="GLOBAL", platform="tiktok", series="counter_tiktok_hashtag", protocol="p1",
               lane_class="unbiased_counter")
FEED = dict(market="ZA", platform="tiktok", series="feed_tiktok", protocol="p1", lane_class="unbiased_rank")


def on(market, platform, lane_class, switched_on=None):
    return {"market": market, "platform": platform, "lane_class": lane_class,
            "switched_on": switched_on or day(3), "backtest_run_id": "bt-1", "rule_version": "stats-1"}


SW_PANEL = on("ZA", "facebook", "panel")
SW_COUNTER = on("GLOBAL", "tiktok", "unbiased_counter")
SW_FEED = on("ZA", "tiktok", "unbiased_rank")


def sig(item, hist_ys, y, *, kind="hashtag", baseline_state="ok", first_measured=None, trials=None,
        hist_n=None, **where):
    """One tvf_series_signal row; hist_ys runs oldest first and ends on day(1)."""
    w = {**PANEL, **where}
    hist = [{"day": day(len(hist_ys) - i), "y": float(v), "n": hist_n} for i, v in enumerate(hist_ys)]
    return {
        "series_id": f"{item}|{w['market']}|{w['series']}|{w['protocol']}", "item_id": item, **w, "kind": kind,
        "y": None if y is None else float(y), "trials": trials, "hist": hist, "obs_prior": 40, "obs28": len(hist),
        "first_measured": first_measured or day(60), "hist_mean": None, "med": None, "v3": None, "v7": None,
        "peak28": None, "vel": None, "accel": None, "z_display": None, "baseline_state": baseline_state,
    }


def rows_for(signal, switches, totals=(), first_weeks=(), rule_version="r1"):
    out = stats.series_test_rows(signal, D, "stats-t", rule_version, switch_rows=switches, totals=totals,
                                 first_weeks=first_weeks)
    return {r["series_id"]: r for r in out}


def one(rows, item):
    [r] = [r for r in rows.values() if r["item_id"] == item]
    return r


# Independent reference computations


def weighted_median(values, weights):
    pairs = sorted(zip(values, weights))
    total = sum(w for _, w in pairs)
    acc = 0.0
    for i, (v, w) in enumerate(pairs):
        acc += w
        if math.isclose(acc, total / 2):
            return (v + pairs[i + 1][0]) / 2
        if acc > total / 2:
            return v
    raise AssertionError


def farrington_base(zs, alpha):
    """Weighted median with the Noufaily et al. residual r = 1.5 (z^(2/3) - m^(2/3)) / (sqrt(phi) m^(1/6)),
    phi = 1 + alpha m; the plain mean when the median m is 0."""
    m = float(np.median(zs))
    if m == 0:
        return float(np.mean(zs))
    phi = 1 + alpha * m
    weights = []
    for z in zs:
        r = 1.5 * (z ** (2 / 3) - m ** (2 / 3)) / (math.sqrt(phi) * m ** (1 / 6))
        weights.append(r ** -2 if r > 2 else 1.0)
    return weighted_median(zs, weights)


def nb_mid(y, mu, alpha):
    if alpha == 0:
        d = st.poisson(mu)
    else:
        n = 1 / alpha
        d = st.nbinom(n, n / (n + mu))
    return d.sf(y) + 0.5 * d.pmf(y)


def nb2_fit(ys, offsets):
    """(alpha, scale) of an intercept-only NB2 fit with the given offsets; scale is exp(intercept)."""
    res = NegativeBinomial(np.asarray(ys, float), np.ones((len(ys), 1)), loglike_method="nb2",
                           offset=np.asarray(offsets, float)).fit(disp=0)
    assert res.mle_retvals["converged"]
    return float(res.params[-1]), math.exp(float(res.params[0]))


def weekday_list(s, f):
    return [f[h["day"].weekday()] if f else 1.0 for h in s["hist"]]


def median_fit(series, f=None):
    """The pooled fit on plain-median baselines of y/f; a series whose median is 0 carries no offset."""
    ys, offs = [], []
    for s in series:
        fs = weekday_list(s, f)
        m = float(np.median([h["y"] / fh for h, fh in zip(s["hist"], fs)]))
        if m > 0:
            ys += [h["y"] for h in s["hist"]]
            offs += [math.log(m * fh) for fh in fs]
    return nb2_fit(ys, offs)


def final_fit(series, f=None, fixed=None):
    """Stage 1 fits alpha on plain-median baselines for the residual's phi; stage 2 refits on each series' own
    Farrington baseline and gives the alpha and scale the test uses. A series keeps the baseline its own group
    gave it (fixed, by series_id) when it is pooled into another group's fit. Returns (alpha, scale, bases)."""
    alpha1, _ = median_fit(series, f)
    ys, offs, bases = [], [], {}
    for s in series:
        fs = weekday_list(s, f)
        z = [h["y"] / fh for h, fh in zip(s["hist"], fs)]
        b = (fixed or {}).get(s["series_id"])
        bases[s["series_id"]] = b = farrington_base(z, alpha1) if b is None else b
        if float(np.median(z)) > 0 and b > 0:
            ys += [h["y"] for h in s["hist"]]
            offs += [math.log(b * fh) for fh in fs]
    alpha, scale = nb2_fit(ys, offs)
    return alpha, scale, bases


def expected_mu(s, scale, bases, f=None):
    f_today = f[D.weekday()] if f else 1.0
    return bases[s["series_id"]] * max(scale, 1.0) * f_today


def stable(rng, n_series, mu, alpha, prefix, days=28, **where):
    """n_series stable NB2 series with mean mu; today's y is the series' own median."""
    n = 1 / alpha
    out = []
    for j in range(n_series):
        ys = rng.negative_binomial(n, n / (n + mu), size=days)
        out.append(sig(f"{prefix}{j}", ys, float(np.median(ys)), **where))
    return out


# 1. When no test runs


def test_none_rows_are_still_written_and_the_rule_version_stays_when_nothing_ran():
    signal = [
        sig("warm", [5] * 10, 9, baseline_state="warmup"),
        sig("thin", [5] * 12, 9, baseline_state="thin"),
        sig("off", [5] * 28, 9, platform="x", series="panel_x_hub"),
        sig("silent", [0] * 28, 0),
        sig("gone", [5] * 28, None),
        sig("late", [5] * 28, 9, market="NG"),
    ]
    later = on("NG", "facebook", "panel", switched_on=D + (D - day(1)))
    rows = stats.series_test_rows(signal, D, "stats-t", "r1", switch_rows=[SW_PANEL | {"market": "KE"}, later])
    assert len(rows) == len(signal)
    for r in rows:
        assert r["test"] == "none" and r["significant"] is False and r["rule_version"] == "r1"
        assert all(r[k] is None for k in ("mu", "alpha", "weekday_factor", "mu_prior", "ratio", "p_mid", "q"))
    assert stats.series_test_rows(signal, D, "stats-t", "r1") == stats.passthrough_rows(signal, D, "stats-t", "r1")


def test_warmup_thin_and_silent_series_are_untested_even_when_switched_on():
    signal = [
        sig("warm", [5] * 10, 9, baseline_state="warmup"),
        sig("thin", [5] * 12, 9, baseline_state="thin"),
        sig("silent", [0] * 28, 0),
        sig("quiet", [0, 2] * 14, 0),
        sig("music", [0] * 27 + [1], 3, trials=3, hist_n=3, market="GLOBAL", platform="youtube",
            series="board_global_music", lane_class="unbiased_rank"),
    ]
    rows = rows_for(signal, [SW_PANEL, on("GLOBAL", "youtube", "unbiased_rank")])
    assert [one(rows, i)["test"] for i in ("warm", "thin", "silent", "music")] == ["none"] * 4
    quiet = one(rows, "quiet")
    assert quiet["test"] == "nb"
    assert all(r["rule_version"] == stats.RULE_VERSION == "stats-1" for r in rows.values())


# 2. Negative binomial


def test_weekday_factor_is_shrunk_towards_1_and_is_1_until_4_weeks_exist():
    totals = [{"market": "ZA", "platform": "facebook", "lane_class": "panel", "day": day(i),
               "total": 200.0 if day(i).weekday() == 0 else 100.0} for i in range(1, 57)]
    f = stats.weekday_factors(totals)[("ZA", "facebook", "panel")]
    mean_all = np.mean([t["total"] for t in totals])
    for wd in range(7):
        vals = [t["total"] for t in totals if t["day"].weekday() == wd]
        n = len(vals)
        assert n == 8
        assert f[wd] == pytest.approx(1 + n / (n + 4) * (np.mean(vals) / mean_all - 1))
    assert f[0] == pytest.approx(1.5)

    four = stats.weekday_factors(totals[:28])[("ZA", "facebook", "panel")]
    vals = [t["total"] for t in totals[:28] if t["day"].weekday() == 0]
    assert len(vals) == 4
    assert four[0] == pytest.approx(1 + 4 / 8 * (200 / np.mean([t["total"] for t in totals[:28]]) - 1))
    three = stats.weekday_factors(totals[:21])[("ZA", "facebook", "panel")]
    assert all(three[wd] == 1.0 for wd in range(7))


def test_mu_item_down_weights_surge_days_farrington_flexible():
    rng = np.random.default_rng(21)
    pool = stable(rng, 29, 47, 0.01, "p")
    hist = [5] * 14 + [90] * 14
    s = sig("a", hist, 6)
    rows = rows_for(pool + [s], [SW_PANEL])
    alpha, scale, bases = final_fit(pool + [s])
    a = rows[s["series_id"]]
    assert a["test"] == "nb" and a["weekday_factor"] == 1.0 and a["mu_prior"] is None
    assert a["alpha"] == pytest.approx(alpha, rel=1e-4)
    assert bases[s["series_id"]] == 5.0
    assert a["mu"] == pytest.approx(5 * max(scale, 1.0), rel=1e-4)
    for p in pool:
        assert rows[p["series_id"]]["mu"] == pytest.approx(expected_mu(p, scale, bases), rel=1e-4)


def test_mu_item_with_weekday_factors_divides_history_and_multiplies_today():
    totals = [{"market": "ZA", "platform": "facebook", "lane_class": "panel", "day": day(i),
               "total": 300.0 if day(i).weekday() == D.weekday() else 100.0} for i in range(1, 57)]
    f = stats.weekday_factors(totals)[("ZA", "facebook", "panel")]
    hist = [12, 8, 9, 30, 11, 10, 7] * 4
    s = sig("a", hist, 20)
    zs = [h["y"] / f[h["day"].weekday()] for h in s["hist"]]
    a = one(rows_for([s], [SW_PANEL], totals=totals), "a")
    alpha, scale, bases = final_fit([s], f)
    alpha1, _ = median_fit([s], f)
    assert bases[s["series_id"]] == pytest.approx(farrington_base(zs, alpha1))
    assert a["weekday_factor"] == pytest.approx(f[D.weekday()])
    assert a["alpha"] == pytest.approx(alpha, rel=1e-4)
    assert a["mu"] == pytest.approx(expected_mu(s, scale, bases, f), rel=1e-4)


def test_nb_p_mid_ratio_and_alpha_for_one_series_known_answer():
    rng = np.random.default_rng(7)
    hist = [int(v) for v in rng.negative_binomial(4, 4 / (4 + 10), size=28)]
    s = sig("a", hist, 24.6)
    a = one(rows_for([s], [SW_PANEL]), "a")
    alpha, scale, bases = final_fit([s])
    mu = expected_mu(s, scale, bases)
    assert a["mu"] == pytest.approx(mu, rel=1e-4)
    assert a["alpha"] == pytest.approx(alpha, rel=1e-4)
    assert a["p_mid"] == pytest.approx(nb_mid(25, mu, alpha), rel=1e-4)
    assert a["ratio"] == pytest.approx((25 + 1) / (mu + 1), rel=1e-4)
    assert a["y"] == 24.6


def test_poisson_when_alpha_is_0():
    assert stats.nb_p_mid(7, 3.2, 0.0) == pytest.approx(st.poisson(3.2).sf(7) + 0.5 * st.poisson(3.2).pmf(7))
    n = 1 / 0.3
    want = st.nbinom(n, n / (n + 3.2)).sf(7) + 0.5 * st.nbinom(n, n / (n + 3.2)).pmf(7)
    assert stats.nb_p_mid(7, 3.2, 0.3) == pytest.approx(want)


def test_negative_counter_delta_is_set_to_0_and_logged(caplog):
    hist = [3, 5, 4, 6, 2, 5, 4] * 4
    s = sig("neg", hist, -40, **COUNTER)
    with caplog.at_level(logging.WARNING, logger=stats.__name__):
        r = one(rows_for([s], [SW_COUNTER]), "neg")
    assert r["test"] == "nb" and r["y"] == -40.0
    assert r["ratio"] == pytest.approx(1 / (r["mu"] + 1))
    assert r["p_mid"] == pytest.approx(nb_mid(0, r["mu"], r["alpha"]))
    assert any("neg|GLOBAL" in m and "-40" in m for m in caplog.messages)


def test_dispersion_is_pooled_by_volume_band_and_pools_up_under_30_series():
    rng = np.random.default_rng(11)
    low = stable(rng, 30, 10, 0.2, "low")
    high = stable(rng, 5, 200, 0.05, "high")
    other = stable(rng, 30, 10, 0.5, "snd", kind="sound")
    rows = rows_for(low + high + other, [SW_PANEL])

    band = {math.floor(math.log2(1 + float(np.median([h["y"] for h in s["hist"]])))) for s in low}
    assert band == {3}
    fit_low = want_low, _, bases_low = final_fit(low)
    fit_high = want_high, _, _ = final_fit(low + high, fixed=bases_low)
    fit_sound = want_sound, _, _ = final_fit(other)
    for group, (want, scale, bases) in ((low, fit_low), (high, fit_high), (other, fit_sound)):
        assert [rows[s["series_id"]]["alpha"] for s in group] == pytest.approx([want] * len(group), rel=1e-4)
        assert [rows[s["series_id"]]["mu"] for s in group] == pytest.approx(
            [expected_mu(s, scale, bases) for s in group], rel=1e-4)
    assert want_low != pytest.approx(want_high)
    assert want_sound == pytest.approx(0.5, abs=0.15) and want_low == pytest.approx(0.2, abs=0.1)


def test_small_market_pools_up_to_the_whole_market():
    rng = np.random.default_rng(5)
    fb = stable(rng, 4, 10, 0.3, "fb")
    tags = stable(rng, 4, 10, 0.3, "tg", kind="sound")
    rows = rows_for(fb + tags, [SW_PANEL])
    alpha, _, _ = final_fit(fb + tags)
    assert [r["alpha"] for r in rows.values()] == pytest.approx([alpha] * 8, rel=1e-4)


def test_method_of_moments_when_the_fit_fails(monkeypatch):
    def fail(*a, **k):
        raise np.linalg.LinAlgError("singular")

    monkeypatch.setattr(stats, "_nb2_fit", fail)
    hist = [4, 9, 2, 15, 6, 3, 11] * 4
    r = one(rows_for([sig("a", hist, 8)], [SW_PANEL]), "a")
    ys = np.array(hist, float)

    def moments(base):
        scale = ys.sum() / base.sum()
        m = scale * base
        return max(0.0, float(np.sum((ys - m) ** 2 - m) / np.sum(m ** 2))), scale

    alpha1, _ = moments(np.full(28, float(np.median(ys))))
    fb = farrington_base(list(ys), alpha1)
    want, scale = moments(np.full(28, fb))
    mu = fb * max(scale, 1.0)
    assert want > 0 and r["alpha"] == pytest.approx(want)
    assert r["mu"] == pytest.approx(mu)
    assert r["p_mid"] == pytest.approx(nb_mid(8, mu, want))


def test_a_scale_below_1_never_lowers_mu():
    hist = [10] * 20 + [0] * 8
    s = sig("a", hist, 12)
    r = one(rows_for([s], [SW_PANEL]), "a")
    alpha, scale, bases = final_fit([s])
    assert scale < 1 and bases[s["series_id"]] == 10.0
    assert r["alpha"] == pytest.approx(alpha, rel=1e-4)
    assert r["mu"] == pytest.approx(10.0)


def test_underdispersed_moments_give_alpha_0_and_a_poisson_test(monkeypatch):
    monkeypatch.setattr(stats, "_nb2_fit", lambda *a, **k: None)
    hist = [10, 10, 11, 9, 10, 10, 10] * 4
    r = one(rows_for([sig("a", hist, 14)], [SW_PANEL]), "a")
    assert r["alpha"] == 0.0
    assert r["p_mid"] == pytest.approx(st.poisson(10).sf(14) + 0.5 * st.poisson(10).pmf(14))


def first_week(item, mean, *, kind="hashtag", market="ZA", platform="facebook", series="panel_fb_hub",
               lane_class="panel"):
    return {"series_id": f"{item}|{market}|{series}|p1", "item_id": item, "market": market, "platform": platform,
            "series": series, "lane_class": lane_class, "kind": kind, "first_week_mean": float(mean)}


PRIOR_WEEKS = [first_week(f"n{j}", j) for j in range(1, 26)]
PRIOR = float(np.percentile(range(1, 26), 90))


def test_a_zero_baseline_is_cold_and_uses_the_prior():
    rows = rows_for([sig("z", [0] * 28, 1, first_measured=day(40))], [SW_PANEL], first_weeks=PRIOR_WEEKS)
    z = one(rows, "z")
    assert z["test"] == "nb" and z["mu_prior"] == pytest.approx(PRIOR) and z["mu"] == pytest.approx(PRIOR)
    assert z["p_mid"] > 0 and z["p_mid"] == pytest.approx(nb_mid(1, PRIOR, z["alpha"]))


def test_a_zero_baseline_without_a_prior_is_untested_and_left_out_of_the_family():
    signal = [sig("z", [0] * 28, 1, first_measured=day(40)), sig("z1", [0] * 27 + [1], 2, first_measured=day(40)),
              sig("ok", [4, 6, 5, 7] * 7, 30)]
    rows = rows_for(signal, [SW_PANEL])
    for item in ("z", "z1"):
        r = one(rows, item)
        assert r["test"] == "none" and r["significant"] is False and r["q"] is None and r["mu"] is None
    assert one(rows, "ok")["test"] == "nb" and one(rows, "ok")["q"] == pytest.approx(one(rows, "ok")["p_mid"])


def test_27_zeros_and_one_active_day_uses_the_prior():
    rows = rows_for([sig("z", [0] * 27 + [1], 2, first_measured=day(40))], [SW_PANEL], first_weeks=PRIOR_WEEKS)
    z = one(rows, "z")
    assert z["test"] == "nb" and z["mu_prior"] == pytest.approx(PRIOR) and z["mu"] == pytest.approx(PRIOR)


def test_nb_p_mid_refuses_a_zero_mean():
    with pytest.raises(AssertionError):
        stats.nb_p_mid(1, 0.0, 0.3)


def test_cold_start_prior_is_the_90th_percentile_of_first_weeks_and_mu_is_the_larger():
    weeks = [first_week(f"n{j}", j) for j in range(1, 26)]
    weeks += [first_week("other-kind", 500, kind="sound"), first_week("other-lane", 900, lane_class="unbiased_counter")]
    signal = [
        sig("short", [2] * 16, 3, baseline_state="short"),
        sig("young", [2] * 28, 3, first_measured=day(10)),
        sig("old", [2] * 28, 3, first_measured=day(40)),
        sig("big", [40] * 28, 45, baseline_state="short"),
    ]
    rows = rows_for(signal, [SW_PANEL], first_weeks=weeks)
    prior = float(np.percentile(range(1, 26), 90))
    assert one(rows, "short")["mu_prior"] == pytest.approx(prior) and one(rows, "short")["mu"] == pytest.approx(prior)
    assert one(rows, "young")["mu_prior"] == pytest.approx(prior) and one(rows, "young")["mu"] == pytest.approx(prior)
    assert one(rows, "old")["mu_prior"] is None and one(rows, "old")["mu"] == pytest.approx(2.0)
    assert one(rows, "big")["mu_prior"] == pytest.approx(prior) and one(rows, "big")["mu"] == pytest.approx(40.0)


def test_cold_start_prior_pools_across_platforms_then_markets_under_20_items():
    fb = [first_week(f"f{j}", j) for j in range(10)]
    xs = [first_week(f"x{j}", 100 + j, platform="x", series="panel_x_hub") for j in range(12)]
    rows = rows_for([sig("short", [2] * 16, 3, baseline_state="short")], [SW_PANEL], first_weeks=fb + xs)
    want = float(np.percentile([w["first_week_mean"] for w in fb + xs], 90))
    assert one(rows, "short")["mu_prior"] == pytest.approx(want)

    ng = [first_week(f"g{j}", 1000 + j, market="NG") for j in range(8)]
    rows = rows_for([sig("short", [2] * 16, 3, baseline_state="short")], [SW_PANEL],
                    first_weeks=fb[:5] + xs[:6] + ng)
    want = float(np.percentile([w["first_week_mean"] for w in fb[:5] + xs[:6] + ng], 90))
    assert one(rows, "short")["mu_prior"] == pytest.approx(want)


@pytest.mark.parametrize("mean, alpha", [(2, 0.5), (5, 1.0), (10, 0.3), (50, 0.2)])
def test_null_series_are_calibrated(mean, alpha):
    """2000 stable NB series of 42 days: the last day is today, the 28 before it the history."""
    rng = np.random.default_rng(2026)
    n = 1 / alpha
    signal = []
    for j in range(2000):
        ys = rng.negative_binomial(n, n / (n + mean), size=42)
        signal.append(sig(f"s{j}", ys[-29:-1], float(ys[-1])))
    rows = [r for r in stats.series_test_rows(signal, D, "stats-t", "r1", switch_rows=[SW_PANEL])
            if r["test"] == "nb"]
    assert len(rows) >= 1900
    p = np.array([r["p_mid"] for r in rows])
    assert 0.02 <= float(np.mean(p < 0.05)) <= 0.08
    assert sum(r["significant"] for r in rows) <= 2


# Deployment: the detect image has no numpy, scipy or statsmodels until the test is switched on


BLOCKED = ("numpy", "scipy", "statsmodels")


def block_statistics_libraries(monkeypatch):
    for name in list(sys.modules):
        if name.split(".")[0] in BLOCKED:
            monkeypatch.setitem(sys.modules, name, None)
    for name in BLOCKED:
        monkeypatch.setitem(sys.modules, name, None)


def test_warm_up_and_switch_off_days_run_without_the_statistics_libraries(monkeypatch):
    block_statistics_libraries(monkeypatch)
    try:
        importlib.reload(stats)
        warm = [sig("w", [5] * 10, 9, baseline_state="warmup")]
        client = ScriptedClient(warm, [])
        assert stats.run_stats(client, D, "stats-x", "r1") == 1
        assert client.loads[0][0]["test"] == "none" and client.loads[0][0]["rule_version"] == "r1"
        off = [sig("o", [5] * 28, 9)]
        client = ScriptedClient(off, [])
        assert stats.run_stats(client, D, "stats-x", "r1") == 1
        assert client.loads[0][0]["test"] == "none"
    finally:
        monkeypatch.undo()
        importlib.reload(stats)


def test_stats_and_the_detect_job_import_without_the_statistics_libraries():
    code = ("import sys\n"
            f"for n in {BLOCKED!r}: sys.modules[n] = None\n"
            "import core.detect.stats, core.detect.job\n")
    root = Path(__file__).resolve().parents[3]
    done = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr


# 3. Beta-binomial


def test_betabinom_known_answer():
    patterns = {"a": [0, 1, 0, 0] * 7, "b": [1, 1, 0, 2] * 7, "c": [0] * 27 + [1], "d": [3, 2, 3, 3] * 7,
                "e": [0, 0, 1, 0] * 7}
    signal = [sig(k, v, 0 if k != "e" else 3, trials=3, hist_n=3, **FEED) for k, v in patterns.items()]
    signal += [sig("snd", [3] * 28, 3, trials=3, hist_n=3, kind="sound", **FEED),
               sig("ng", [3] * 28, 3, trials=3, hist_n=3, **{**FEED, "market": "NG"})]
    rows = rows_for(signal, [SW_FEED, on("NG", "tiktok", "unbiased_rank")])

    rates = [sum(v) / (3 * len(v)) for v in patterns.values()]
    m, v = float(np.mean(rates)), float(np.var(rates))
    c = m * (1 - m) / v - 1
    a0, b0 = m * c, (1 - m) * c
    hist = patterns["e"]
    a, b = a0 + sum(hist), b0 + sum(3 - y for y in hist)
    x, n = 3 + hist[-1] + hist[-2], 9
    e = one(rows, "e")
    assert e["test"] == "betabinom"
    assert e["p_mid"] == pytest.approx(st.betabinom(n, a, b).sf(x) + 0.5 * st.betabinom(n, a, b).pmf(x))
    assert e["ratio"] == pytest.approx((x / n) / (a / (a + b)))
    assert e["mu"] is None and e["alpha"] is None and e["weekday_factor"] is None
    assert one(rows, "c")["test"] == "betabinom"


def test_betabinom_prior_falls_back_to_beta_1_1_when_moments_do_not_fit():
    signal = [sig(k, [1] * 28, 3, trials=3, hist_n=3, **FEED) for k in ("a", "b", "c")]
    a = one(rows_for(signal, [SW_FEED]), "a")
    pa, pb = 1 + 28, 1 + 56
    x = 3 + 1 + 1
    assert a["p_mid"] == pytest.approx(st.betabinom(9, pa, pb).sf(x) + 0.5 * st.betabinom(9, pa, pb).pmf(x))


def test_betabinom_clamps_x_to_n_and_logs(caplog):
    signal = [sig("a", [3] * 27 + [4], 5, trials=3, hist_n=3, **FEED),
              sig("b", [1] * 28, 1, trials=3, hist_n=3, **FEED),
              sig("c", [0, 1] * 14, 1, trials=3, hist_n=3, **FEED)]
    with caplog.at_level(logging.WARNING, logger=stats.__name__):
        a = one(rows_for(signal, [SW_FEED]), "a")
    rates = [1.0, 1 / 3, 1 / 6]
    m, v = float(np.mean(rates)), float(np.var(rates))
    c = m * (1 - m) / v - 1
    pa, pb = m * c + 84, (1 - m) * c
    assert a["test"] == "betabinom"
    assert a["p_mid"] == pytest.approx(0.5 * st.betabinom(9, pa, pb).pmf(9))
    assert a["ratio"] == pytest.approx(1 / (pa / (pa + pb)))
    assert any("a|ZA" in msg and "12" in msg for msg in caplog.messages)


def test_betabinom_without_trials_is_untested():
    signal = [sig("zero", [1] * 28, 1, trials=0, hist_n=3, **FEED),
              sig("null", [1] * 28, 1, trials=None, hist_n=3, **FEED),
              sig("gap", [1] * 28, 1, trials=3, hist_n=None, **FEED),
              sig("ok", [0, 1] * 14, 1, trials=3, hist_n=3, **FEED)]
    rows = rows_for(signal, [SW_FEED])
    assert [one(rows, i)["test"] for i in ("zero", "null", "gap", "ok")] == ["none", "none", "none", "betabinom"]


class ScriptedClient:
    """Answers the signal query, then the switch query, then empty totals and first weeks."""

    def __init__(self, signal, switches):
        self.answers = [signal, switches, [], []]
        self.loads = []

    def query(self, sql, job_config=None):
        return FakeJob(self.answers.pop(0))

    def get_table(self, ref):
        return bigquery.Table("p." + ref, schema=[])

    def load_table_from_json(self, rows, table, job_config=None):
        self.loads.append(rows)
        return FakeJob([])


def test_run_stats_survives_bad_rank_trials():
    signal = [sig("zero", [1] * 28, 1, trials=0, hist_n=3, **FEED),
              sig("gap", [1] * 28, 1, trials=3, hist_n=None, **FEED),
              sig("big", [3] * 28, 7, trials=3, hist_n=3, **FEED)]
    client = ScriptedClient(signal, [SW_FEED])
    assert stats.run_stats(client, D, "stats-x", "r1") == 3
    assert [r["test"] for r in client.loads[0]] == ["none", "none", "betabinom"]


# 4. Benjamini-Hochberg per market and day


def test_benjamini_hochberg_per_market_with_global_its_own_family():
    rng = np.random.default_rng(3)
    za = stable(rng, 12, 8, 0.2, "za")
    za[0]["y"], za[1]["y"] = 30.0, 22.0
    glob = stable(rng, 6, 8, 0.2, "gl", **COUNTER)
    glob[0]["y"] = 40.0
    quiet = sig("warm", [5] * 10, 90, baseline_state="warmup")
    rows = rows_for(za + glob + [quiet], [SW_PANEL, SW_COUNTER])
    for family in (za, glob):
        ids = [s["series_id"] for s in family]
        q = st.false_discovery_control([rows[i]["p_mid"] for i in ids], method="bh")
        for i, qi in zip(ids, q):
            assert rows[i]["q"] == pytest.approx(qi)
            assert rows[i]["significant"] is bool(qi <= 0.05)
    assert one(rows, "warm")["q"] is None and one(rows, "warm")["significant"] is False
    pooled = st.false_discovery_control([rows[s["series_id"]]["p_mid"] for s in za + glob], method="bh")
    assert list(pooled[:12]) != pytest.approx([rows[s["series_id"]]["q"] for s in za])


# 5. Injected spikes


def test_injected_spikes_at_3_and_5_times_mu_are_significant_and_the_median_is_not():
    rng = np.random.default_rng(42)
    series = stable(rng, 40, 10, 0.1, "s")
    rows = rows_for(series, [SW_PANEL])
    mu3 = rows[series[0]["series_id"]]["mu"]
    mu5 = rows[series[1]["series_id"]]["mu"]
    series[0]["y"], series[1]["y"] = 3 * mu3, 5 * mu5
    rows = rows_for(series, [SW_PANEL])
    assert rows[series[0]["series_id"]]["significant"] is True
    assert rows[series[1]["series_id"]]["significant"] is True
    assert rows[series[1]["series_id"]]["ratio"] >= 2
    assert not any(rows[s["series_id"]]["significant"] for s in series[2:])
    assert all(rows[s["series_id"]]["p_mid"] > 0.2 for s in series[2:])


# 6. Output columns


def ddl_columns(text):
    body = text.split("intelligence_42_core.series_test (", 1)[1].split("PARTITION BY", 1)[0]
    body = re.sub(r"--[^\n]*", "", body)
    return [c.split()[0] for c in body.replace("\n", " ").rstrip(" )").split(",") if c.strip()]


def test_output_columns_are_the_series_test_ddl():
    cols = ddl_columns(DATA_MD.read_text(encoding="utf-8"))
    assert cols == SERIES_TEST_COLUMNS
    rows = stats.series_test_rows([sig("a", [4, 6, 5, 7] * 7, 9), sig("w", [1] * 5, 1, baseline_state="warmup")],
                                  D, "stats-t", "r1", switch_rows=[SW_PANEL])
    for r in rows:
        assert list(r) == cols
        assert r["metric_date"] == D and r["run_id"] == "stats-t" and r["rule_version"] == "stats-1"
        for k in ("mu", "alpha", "weekday_factor", "ratio", "p_mid", "q"):
            assert r[k] is None or type(r[k]) is float
        assert type(r["significant"]) is bool


# run_stats and sql/stats.sql on DuckDB


def stats_sql():
    return sqlrun.split(sqlrun.render((SQL / "stats.sql").read_text(encoding="utf-8")))


def test_stats_sql_is_three_bigquery_selects():
    stmts = stats_sql()
    assert len(stmts) == 3 == len({stats.SWITCH_SQL, stats.TOTALS_SQL, stats.FIRST_WEEK_SQL})
    for s in stmts:
        tree = sqlglot.parse_one(s, read="bigquery")
        assert isinstance(tree, sqlglot.exp.Select)


class StatsClient(duck.Client):
    def get_table(self, ref):
        return bigquery.Table("p." + ref, schema=[])

    def load_table_from_json(self, rows, table, job_config=None):
        duck.load(self.con, f"{table.dataset_id}.{table.table_id}", rows)
        return duck._Job([])


@pytest.fixture
def con():
    c = duck.connect()
    c.execute("CREATE TABLE core.test_switch (market VARCHAR NOT NULL, platform VARCHAR NOT NULL, "
              "lane_class VARCHAR NOT NULL, switched_on DATE, backtest_run_id VARCHAR, rule_version VARCHAR)")
    yield c
    c.close()


def panel_world(items=("p0", "p1", "p2"), first=45):
    w = World()
    w.protocol("panel_fb_hub", first, platform="facebook", lane_class="panel", k=1.0)
    for key, h in w.health.items():
        h["items"] = 120 if h["day"].weekday() == D.weekday() else 60
    w.health[("panel_fb_hub", "ZA", "p1", day(5))]["valid"] = False
    for j, item in enumerate(items):
        for i in range(first, -1, -1):
            w.daily(item, i, 3 + (i + j) % 4, platform="facebook")
    return w


def test_weekday_totals_query_sums_valid_route_days_over_8_weeks(con):
    w = panel_world()
    w.protocol("panel_fb_hub", 70, platform="facebook", lane_class="panel", market="NG", k=1.0)
    w.health[("panel_fb_hub", "NG", "p1", day(9))]["valid"] = False
    w.load(con)
    got = duck.query(con, stats.TOTALS_SQL, {"d": D})
    za = {r["day"]: r["total"] for r in got if r["market"] == "ZA"}
    assert set(za) == {day(i) for i in range(1, 46)} - {day(5)}
    assert all(za[d] == (120 if d.weekday() == D.weekday() else 60) for d in za)
    ng = {r["day"] for r in got if r["market"] == "NG"}
    assert ng == {day(i) for i in range(1, 57)} - {day(9)}
    assert {(r["platform"], r["lane_class"]) for r in got} == {("facebook", "panel")}


def test_first_week_query_takes_7_observed_days_of_series_first_seen_in_90_days(con):
    w = World()
    w.protocol("panel_fb_hub", 120, platform="facebook", lane_class="panel", k=2.0)
    w.health[("panel_fb_hub", "ZA", "p1", day(37))]["valid"] = False
    for i in range(40, -1, -1):
        w.daily("new", i, i % 3)
    for i in range(110, -1, -1):
        w.daily("old", i, 1)
    for i in range(4, -1, -1):
        w.daily("fresh", i, 2)
    w.protocol("feed_tiktok", 60)
    for i in range(50, -1, -1):
        w.counter("rank", "feed_tiktok", i, 1.0)
    w.load(con)
    for item, i in (("new", 40), ("old", 110), ("fresh", 4), ("rank", 50)):
        con.execute("UPDATE core.cultural_map SET first_seen = ? WHERE item_id = ?", [day(i), item])
    got = {r["item_id"]: r for r in duck.query(con, stats.FIRST_WEEK_SQL, {"d": D})}
    assert set(got) == {"new"}
    # first value above 0 on day(40): observed days 40, 39, 38, 36, 35, 34, 33 (37 invalid)
    want = np.mean([2 * (i % 3) for i in (40, 39, 38, 36, 35, 34, 33)])
    assert got["new"]["first_week_mean"] == pytest.approx(want)
    assert got["new"]["kind"] == "hashtag" and got["new"]["series"] == "panel_fb_hub"
    assert got["new"]["lane_class"] == "panel" and got["new"]["platform"] == "facebook"


def test_run_stats_without_a_switch_writes_passthrough_rows_after_two_reads(con):
    w = panel_world()
    w.load(con)
    client = StatsClient(con)
    assert stats.run_stats(client, D, "stats-x", "r1", core="core", agent="agent") == 3
    assert len(client.sql) == 2
    got = duck.query(con, "SELECT * FROM {core}.series_test")
    assert {r["test"] for r in got} == {"none"} and {r["rule_version"] for r in got} == {"r1"}


def test_run_stats_end_to_end_with_a_switch_row(con):
    w = panel_world()
    w.load(con)
    duck.load(con, "core.test_switch", [SW_PANEL])
    duck.load(con, "agent.runs", [{**run("backtest", day(3), run_id=SW_PANEL["backtest_run_id"]),
                                   "finished_at": at(day(3))}])
    client = StatsClient(con)
    assert stats.run_stats(client, D, "stats-x", "r1", core="core", agent="agent") == 3
    assert len(client.sql) == 4
    got = duck.query(con, "SELECT * FROM {core}.series_test ORDER BY item_id")
    assert [r["test"] for r in got] == ["nb"] * 3
    assert {r["rule_version"] for r in got} == {"stats-1"} and {r["run_id"] for r in got} == {"stats-x"}
    totals = {day(i): (120 if day(i).weekday() == D.weekday() else 60) for i in range(1, 46) if i != 5}
    mean_all = np.mean(list(totals.values()))
    today = [t for d, t in totals.items() if d.weekday() == D.weekday()]
    want_f = 1 + len(today) / (len(today) + 4) * (np.mean(today) / mean_all - 1)
    assert [r["weekday_factor"] for r in got] == pytest.approx([want_f] * 3)
    q = st.false_discovery_control([r["p_mid"] for r in got], method="bh")
    assert [r["q"] for r in got] == pytest.approx(list(q))


@pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")
def test_stats_sql_dry_runs_on_bigquery():
    from .test_detect_bigquery import PROJECT, dry_run
    client = bigquery.Client(project=PROJECT)
    for sql in (stats.SWITCH_SQL, stats.TOTALS_SQL, stats.FIRST_WEEK_SQL):
        dry_run(client, sqlrun.render(sql), {"d": D})
