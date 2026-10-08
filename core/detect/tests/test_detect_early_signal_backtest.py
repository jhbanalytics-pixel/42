"""The spike-injection backtest of the early signal (core/detect/early_signal_backtest.py).

The world is the stable negative binomial panel the main backtest tests use. The backtest replays real days
through backtest.Store, injects rises into a share of the series on a copy of the replayed values, runs
stats.series_test_rows on them and reads both the daily Rising test and the CUSUM.
"""

import copy
import json

import pytest

from core.detect import backtest, early_signal as es, early_signal_backtest as eb
from core.detect.tests import duck
from core.detect.tests.fixtures import D
from core.detect.tests.test_detect_backtest import BacktestClient, connect, stable_panel
from core.detect.tests.test_detect_states import World

KEY = ("ZA", "facebook", "panel")
SMALL = dict(horizon=8, placebo_days=3, replicates=1, share=0.25, seed=7,
             scenarios={"step 2x": eb.step(2.0)})


@pytest.fixture(scope="module")
def inputs():
    con = connect()
    stable_panel(World(), 90, mu=10, alpha=0.1, seed=3).load(con)
    inputs = backtest.load(BacktestClient(con), D, 28, core="core", agent="agent")
    con.close()
    return inputs


@pytest.fixture(scope="module")
def result(inputs):
    return eb.run(inputs, D, KEY, **SMALL)


def test_scenarios_grow_the_way_they_are_named():
    ramp = eb.SCENARIOS["ramp 1.3x a day for 5 days"]
    assert [round(ramp(k), 3) for k in (1, 2, 3, 5, 6, 12)] == [1.3, 1.69, 2.197, 3.713, 3.713, 3.713]
    assert [eb.step(1.5)(k) for k in (1, 7)] == [1.5, 1.5]
    assert [round(eb.SCENARIOS["ramp 1.15x a day for 5 days"](5), 3)] == [2.011]


def test_the_result_counts_what_was_injected_and_what_was_left_alone(result):
    s = result["scenarios"]["step 2x"]
    assert result["rule"]["kappa"] == es.KAPPA and result["rule"]["h"] == es.H
    assert result["placebo"]["series_days"] > 0 and result["placebo"]["days"] == 3
    assert s["injected"] + s["already_flagged_at_onset"] == 10 and s["horizon"] == 8   # 25% of the 40 stable series
    for rule in ("cusum", "daily", "rising"):
        assert 0 <= s[rule]["detected"] <= s["injected"]


def test_a_step_to_twice_the_level_is_found_by_the_cusum_in_the_horizon(result):
    s = result["scenarios"]["step 2x"]
    # at twice the level the log likelihood ratio adds about 1.2 a day against a limit of 6.5: day 5 or 6 on average
    assert s["cusum"]["detected"] >= 0.7 * s["injected"] and s["cusum"]["median_day"] <= 7


def test_the_same_seed_gives_the_same_report(inputs, result):
    assert json.dumps(eb.run(inputs, D, KEY, **SMALL), sort_keys=True) == json.dumps(result, sort_keys=True)


def test_a_different_seed_injects_a_different_set(inputs, result):
    other = eb.run(inputs, D, KEY, **{**SMALL, "seed": 8})
    assert other["scenarios"]["step 2x"]["series"] != result["scenarios"]["step 2x"]["series"]


def test_days_earlier_is_the_rising_day_minus_the_cusum_day_over_pairs_both_found(result):
    s = result["scenarios"]["step 2x"]
    pairs = [(c, r) for c, r in zip(s["first_day"]["cusum"], s["first_day"]["rising"]) if c and r]
    earlier = sorted(r - c for c, r in pairs)
    assert s["days_earlier_than_rising"]["pairs"] == len(pairs)
    if earlier:
        mid = len(earlier) // 2
        want = earlier[mid] if len(earlier) % 2 else (earlier[mid - 1] + earlier[mid]) / 2
        assert s["days_earlier_than_rising"]["median"] == want


def test_injection_leaves_the_replayed_inputs_untouched(inputs):
    before = copy.deepcopy(inputs)
    eb.run(inputs, D, KEY, **SMALL)
    assert inputs == before


def test_the_markdown_report_states_method_and_headline_numbers(result):
    md = eb.markdown(result)
    assert "Median days earlier" in md and "False flags" in md and "step 2x" in md
    assert "synthetic" in md.lower() and "information only" in md.lower()
    prose = [ln for ln in md.splitlines() if not set(ln) <= set("|-: ")]
    for bad in (chr(0x2014), chr(0x2013), "-" * 2):
        assert not any(bad in ln for ln in prose)


def test_the_frozen_mode_is_labelled_deterministic_and_says_where_each_baseline_came_from(inputs, result):
    a = eb.run(inputs, D, KEY, **SMALL, mode="frozen")
    assert a["mode"] == "frozen" and result["mode"] == "refit"
    assert json.dumps(a, sort_keys=True) == json.dumps(eb.run(inputs, D, KEY, **SMALL, mode="frozen"), sort_keys=True)
    src = a["baseline_source"]
    assert src["frozen"] > 0 and set(src) == {"frozen", "earliest_in_window", "refit"}
    assert "frozen" in eb.markdown(a).lower()


def test_an_unknown_mode_is_refused(inputs):
    with pytest.raises(ValueError):
        eb.run(inputs, D, KEY, **SMALL, mode="stale")
