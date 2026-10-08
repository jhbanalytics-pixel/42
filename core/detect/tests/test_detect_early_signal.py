"""The negative binomial CUSUM early signal (core/detect/early_signal.py), METHOD-GAPS Gap 2a.

The log likelihood ratio is checked against scipy's own negative binomial mass function, never against the
module's formula. The threshold H is pinned in the module; here the in-control average run length it gives is
simulated with a fixed seed and compared with the stated budget, and the oracle for a run is the textbook
recursion S = max(0, S + llr) written out again below.
"""

import math
from datetime import timedelta

import numpy as np
import pytest
from scipy import stats as st

from core.detect import early_signal as es
from core.detect.tests.fixtures import D, day

ARL0_MIN = 600          # days: 0.05 false flags per series-month of 30 days, METHOD-GAPS G2a acceptance


# The log likelihood ratio


@pytest.mark.parametrize("mu, alpha", [(2.0, 0.05), (10.0, 0.1), (40.0, 0.5), (300.0, 0.02)])
@pytest.mark.parametrize("y", [0, 1, 7, 25, 120])
def test_llr_is_the_log_ratio_of_nb_mass_functions(mu, alpha, y):
    n = 1 / alpha
    p0, p1 = n / (n + mu), n / (n + es.KAPPA * mu)
    want = st.nbinom.logpmf(y, n, p1) - st.nbinom.logpmf(y, n, p0)
    assert es.llr(y, mu, alpha) == pytest.approx(want, abs=1e-9)


@pytest.mark.parametrize("y", [0, 3, 11])
def test_llr_is_poisson_at_zero_dispersion(y):
    want = st.poisson.logpmf(y, es.KAPPA * 6.0) - st.poisson.logpmf(y, 6.0)
    assert es.llr(y, 6.0, 0.0) == pytest.approx(want, abs=1e-12)
    assert es.llr(y, 6.0, 1e-12) == pytest.approx(want, abs=1e-6)


def test_llr_expectation_is_negative_in_control_and_positive_at_the_reference_shift():
    mu, alpha = 20.0, 0.1
    n = 1 / alpha
    ys = np.arange(0, 400)
    for shift, sign in ((1.0, -1), (es.KAPPA, 1)):
        pmf = st.nbinom.pmf(ys, n, n / (n + shift * mu))
        mean = sum(pmf * [es.llr(int(y), mu, alpha) for y in ys])
        assert sign * mean > 0.1


# The recursion


def oracle_path(ys, mus, alpha):
    s, out = 0.0, []
    for y, mu in zip(ys, mus):
        n = 1 / alpha
        s = max(0.0, s + st.nbinom.logpmf(y, n, n / (n + es.KAPPA * mu)) - st.nbinom.logpmf(y, n, n / (n + mu)))
        out.append(s)
    return out


def test_cusum_path_floors_at_zero_and_matches_the_textbook_recursion():
    ys = [3, 0, 0, 0, 14, 22, 9, 0, 31, 5]
    mus = [8.0, 8.0, 9.0, 9.0, 10.0, 10.0, 11.0, 11.0, 12.0, 12.0]
    got = es.cusum(ys, mus, 0.2)
    assert got == pytest.approx(oracle_path(ys, mus, 0.2), abs=1e-9)
    assert min(got) >= 0 and got[1] == 0.0


def test_cusum_resets_to_zero_after_an_alarm_when_asked():
    ys = [60] * 6
    mus = [20.0] * 6
    held = es.cusum(ys, mus, 0.1, h=4.0)
    reset = es.cusum(ys, mus, 0.1, h=4.0, reset_on_alarm=True)
    assert held[0] >= 4.0 and held == sorted(held)          # a sustained rise keeps the evidence up
    assert reset[0] == held[0]                              # the alarm day keeps its value
    assert reset[1] == pytest.approx(reset[0]) and held[1] > 1.9 * held[0]    # the next day starts again from 0


# The in-control average run length (a simulation with a fixed seed)

GRID = [(5.0, 0.05), (5.0, 0.3), (20.0, 0.1), (20.0, 0.5), (100.0, 0.05), (100.0, 0.3)]


@pytest.mark.parametrize("mu, alpha", GRID)
def test_in_control_average_run_length_meets_the_stated_budget(mu, alpha):
    arl = es.average_run_length(mu, alpha, chains=4000, days=500, seed=20261008)
    assert arl >= ARL0_MIN, f"ARL0 {arl:.0f} days at mu {mu}, alpha {alpha}"


def test_average_run_length_is_the_mean_gap_between_alarms():
    # With h = 0 the statistic alarms the first day it is positive: a stream of constant 30s at mu 5 alarms
    # every day, so the run length is exactly 1 day.
    assert es.average_run_length(5.0, 0.1, h=0.0, chains=10, days=20, seed=1, y_fn=lambda rng, size: np.full(size, 30)) == 1.0


def test_the_budget_is_the_sensitive_end_not_a_loose_one():
    # A threshold half the size must break the budget on the same seed, so the test above can fail.
    assert es.average_run_length(20.0, 0.1, h=es.H / 2, chains=4000, days=500, seed=20261008) < ARL0_MIN


# The daily signal from a series_test row


def row(y, mu=20.0, alpha=0.1, f=1.0, d=D, version="stats-1"):
    return {"metric_date": d, "y": float(y), "mu": mu, "alpha": alpha, "weekday_factor": f, "test": "nb",
            "market": "ZA", "platform": "facebook", "lane_class": "panel", "series": "s", "protocol": "p1",
            "rule_version": version}


def hist_of(values, last=1):
    """History oldest first, the newest value `last` days before D."""
    n = len(values)
    return [{"day": day(last + n - 1 - i), "y": float(v), "n": None} for i, v in enumerate(values)]


def test_a_flat_series_at_its_baseline_gives_no_signal():
    r = es.signal(row(20), hist_of([20] * 13), lambda d: 1.0)
    assert r["alarm"] is False and r["cusum"] < 1.0 and r["days_used"] == 14


def test_a_sustained_rise_to_one_and_a_half_times_alarms_within_ten_days():
    first = None
    for k in range(1, 15):
        r = es.signal(row(30), hist_of([20] * 13 + [30] * (k - 1)), lambda d: 1.0)
        if r["alarm"] and first is None:
            first = k
    assert first is not None and first <= 10


def test_a_rise_older_than_the_window_is_forgotten():
    old = hist_of([60] * 6 + [20] * 20)           # a rise that ended 20 days ago
    r = es.signal(row(20), old, lambda d: 1.0)
    assert r["cusum"] < 1.0 and r["alarm"] is False


def test_history_outside_the_window_is_ignored_and_days_used_counts_observed_days():
    h = hist_of([20] * 30)
    r = es.signal(row(20), h, lambda d: 1.0)
    assert r["days_used"] == es.WINDOW_DAYS
    gap = [x for i, x in enumerate(hist_of([20] * 13)) if i not in (3, 4)]
    assert es.signal(row(20), gap, lambda d: 1.0)["days_used"] == es.WINDOW_DAYS - 2


def test_the_expected_level_of_each_day_follows_the_weekday_factor():
    # A Sunday that runs at twice the weekday level is in control when the factor says so.
    def factor(d):
        return 2.0 if d.weekday() == 6 else 1.0

    d = D - timedelta(days=(D.weekday() + 1) % 7)       # the Sunday on or before D
    ys = [40 if (d - timedelta(days=k)).weekday() == 6 else 20 for k in range(13, 0, -1)]
    h = [{"day": d - timedelta(days=k), "y": float(y), "n": None} for k, y in zip(range(13, 0, -1), ys)]
    sunday = row(40, mu=40.0, f=2.0, d=d)
    assert es.signal(sunday, h, factor)["alarm"] is False
    assert es.signal(sunday, h, lambda x: 1.0)["cusum"] > es.signal(sunday, h, factor)["cusum"]


@pytest.mark.parametrize("bad", [dict(test="none"), dict(mu=None), dict(mu=0.0), dict(alpha=None)])
def test_untested_rows_give_no_signal(bad):
    assert es.signal({**row(20), **bad}, hist_of([20] * 13), lambda d: 1.0) is None


def test_the_signal_does_not_read_a_rank_state_or_significance_flag():
    sig = es.signal(row(30), hist_of([30] * 13), lambda d: 1.0)
    changed = es.signal({**row(30), "significant": True, "q": 1e-9, "ratio": 9.9, "p_mid": 1e-12},
                        hist_of([30] * 13), lambda d: 1.0)
    assert sig == changed and sig["alarm"] is True


# The frozen baseline option


def earlier(offset, mu=20.0, alpha=0.1, f=1.0, sid="s1"):
    return {"series_id": sid, "metric_date": D - timedelta(days=offset), "mu": mu, "alpha": alpha,
            "weekday_factor": f}


def test_a_rise_the_refit_baseline_has_absorbed_alarms_against_the_frozen_one():
    # The whole window ran at 1.6 times the old level of 20, so the refit baseline has crept up to 26.
    h = hist_of([32] * 13)
    refit = es.signal(row(32, mu=26.0), h, lambda d: 1.0)
    frozen = es.signal(row(32, mu=26.0), h, lambda d: 1.0, baseline={"mu": 20.0, "alpha": 0.1, "weekday_factor": 1.0})
    assert refit["alarm"] is False and frozen["alarm"] is True and frozen["cusum"] > refit["cusum"] + 2


def test_no_baseline_argument_is_the_refit_mode_unchanged():
    h = hist_of([32] * 13)
    assert es.signal(row(32, mu=26.0), h, lambda d: 1.0) == es.signal(row(32, mu=26.0), h, lambda d: 1.0, baseline=None)


def test_the_frozen_baseline_follows_the_weekday_factor_of_the_day_it_was_fitted_on():
    h = hist_of([20] * 13)
    plain = es.signal(row(20, mu=20.0), h, lambda d: 1.0, baseline={"mu": 20.0, "alpha": 0.1, "weekday_factor": 1.0})
    scaled = es.signal(row(20, mu=20.0), h, lambda d: 1.0, baseline={"mu": 40.0, "alpha": 0.1, "weekday_factor": 2.0})
    assert scaled["cusum"] == pytest.approx(plain["cusum"])


def test_the_frozen_baseline_is_the_row_just_before_the_window_else_the_earliest_in_it_else_none():
    w = es.WINDOW_DAYS
    rows = [earlier(w), earlier(w - 3, mu=99.0), earlier(w + 2, mu=7.0), earlier(2, mu=55.0)]
    got, source = es.frozen_baseline(rows, D)
    assert (got["mu"], source, got["metric_date"]) == (20.0, "frozen", D - timedelta(days=w))
    got, source = es.frozen_baseline([earlier(w + 2, mu=7.0), earlier(w + 1, mu=8.0)], D)
    assert (got["mu"], source) == (8.0, "frozen")                     # the latest one up to the day before the window
    got, source = es.frozen_baseline([earlier(w - 3, mu=99.0), earlier(2, mu=55.0)], D)
    assert (got["mu"], source) == (99.0, "earliest_in_window")
    assert es.frozen_baseline([earlier(w + 9)], D) == (None, "refit")
    assert es.frozen_baseline([], D) == (None, "refit")
    assert es.frozen_baseline([{**earlier(w), "mu": 0.0}, {**earlier(w), "alpha": None}], D) == (None, "refit")


def test_earlier_rows_reads_the_current_series_test_view_once():
    class Job:
        def result(self):
            return [{"series_id": "s1", "metric_date": D - timedelta(days=14), "mu": 5.0, "alpha": 0.1,
                     "weekday_factor": 1.0}]

    class Client:
        def __init__(self):
            self.sql = []

        def query(self, sql, job_config=None):
            self.sql.append((sql, job_config))
            return Job()

    c = Client()
    got = es.earlier_rows(c, D, "core")
    assert list(got) == ["s1"] and got["s1"][0]["mu"] == 5.0 and len(c.sql) == 1
    sql = c.sql[0][0]
    assert "core.v_series_test_current" in sql and "test = 'nb'" in sql and "@d" in sql


def test_each_mode_alarms_at_its_own_calibrated_limit():
    h = hist_of([32] * 13)
    base = {"mu": 20.0, "alpha": 0.1, "weekday_factor": 1.0}
    refit = es.signal(row(32, mu=20.0), h, lambda d: 1.0)
    frozen = es.signal(row(32, mu=20.0), h, lambda d: 1.0, baseline=base)
    assert refit["h"] == es.H == 6.5 and frozen["h"] == es.H_FROZEN == 9.0
    assert refit["cusum"] == pytest.approx(frozen["cusum"])          # the same baseline gives the same chart
    mid = es.signal(row(32, mu=20.0), hist_of([20] * 5 + [32] * 8), lambda d: 1.0, baseline=base)
    assert es.H <= mid["cusum"] < es.H_FROZEN and mid["alarm"] is False        # over the refit limit, under the frozen one
    assert es.signal(row(32, mu=20.0), hist_of([20] * 5 + [32] * 8), lambda d: 1.0)["alarm"] is True
    assert es.signal(row(32, mu=20.0), h, lambda d: 1.0, baseline=base, h=3.0)["h"] == 3.0
