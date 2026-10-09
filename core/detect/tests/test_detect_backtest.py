"""Tests for the backtest and the switch-on step (TRUST.md section 4 "Backtest before use and monthly", DATA.md
section 3.5 and test_switch, task 1.19).

Fixture worlds are loaded into DuckDB and read through sql/backtest.sql, so the as-of filters are checked on
the same SQL that runs on BigQuery. Series values come from the World builder the state tests use; stable
series are drawn from a negative binomial with a fixed seed.
"""

import json
import os
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
import sqlglot
from google.cloud import bigquery
from scipy import stats as st

from .. import backtest, sqlrun, stats
from . import duck
from .fixtures import D, at, day, health, run
from .test_detect_states import World

SQL = Path(__file__).resolve().parents[1] / "sql"
KEY = "ZA|facebook|panel"


class BacktestClient(duck.Client):
    def insert_rows_json(self, table, rows):
        duck.load(self.con, table, rows)
        return []


def connect():
    c = duck.connect()
    c.execute("CREATE TABLE core.test_switch (market VARCHAR NOT NULL, platform VARCHAR NOT NULL, "
              "lane_class VARCHAR NOT NULL, switched_on DATE, backtest_run_id VARCHAR, rule_version VARCHAR)")
    return c


@pytest.fixture
def con():
    c = connect()
    yield c
    c.close()


def stable_panel(w, first, n_items=40, mu=10, alpha=0.1, seed=1, market="ZA", prefix="i"):
    """n_items stable NB panel series on panel_fb_hub from day(first) to D, every day valid."""
    rng = np.random.default_rng(seed)
    w.protocol("panel_fb_hub", first, platform="facebook", lane_class="panel", market=market, k=1.0)
    n = 1 / alpha
    for j in range(n_items):
        ys = rng.negative_binomial(n, n / (n + mu), size=first + 1)
        for i in range(first, -1, -1):
            w.daily(f"{prefix}{j}", i, int(ys[first - i]), platform="facebook", market=market)
    return w


def replay(con, as_of=D, days=7):
    inputs = backtest.load(BacktestClient(con), as_of, days, core="core", agent="agent")
    return backtest.replay(inputs, as_of, days)


# 0. The replayed series are the ones tvf_series_signal reads when everything is available


def mixed_world():
    w = World()
    w.protocol("feed_tiktok", 40)
    w.health[("feed_tiktok", "ZA", "p1", day(6))]["valid"] = False
    for j in range(4):
        for i in range(40, -1, -1):
            if (i + j) % 3 == 0:
                w.counter(f"r{j}", "feed_tiktok", i, float(1 + (i + j) % 3))
    w.protocol("panel_fb_hub", 35, platform="facebook", lane_class="panel", k=2.0)
    w.health[("panel_fb_hub", "ZA", "p1", day(9))]["valid"] = False
    for j in range(3):
        for i in range(35, -1, -1):
            if (i + j) % 4:
                w.daily(f"p{j}", i, 1 + (i * j) % 5, platform="facebook")
    w.protocol("curve_tiktok_hashtag", 20, lane_class="unbiased_counter", market="GLOBAL")
    w.health[("curve_tiktok_hashtag", "GLOBAL", "p1", day(4))]["valid"] = False
    for i in range(30, 21, -1):
        w.counter("c0", "curve_tiktok_hashtag", i, float(i % 7), lane_class="unbiased_counter", unit="delta",
                  market="GLOBAL", source="vendor_history", read_day=day(20))
    for i in range(20, -1, -1):
        w.counter("c0", "curve_tiktok_hashtag", i, float(3 + i % 5), lane_class="unbiased_counter",
                  unit="delta", market="GLOBAL")
    return w


FIELDS = ("item_id", "market", "platform", "series", "protocol", "lane_class", "kind", "y", "trials",
          "obs_prior", "obs28", "first_measured", "baseline_state")


def test_replay_on_the_last_day_matches_tvf_series_signal(con):
    mixed_world().load(con)
    want = {r["series_id"]: r for r in duck.query(con, "SELECT * FROM {core}.tvf_series_signal(@d)", {"d": D})}
    inputs = backtest.load(BacktestClient(con), D, 7, core="core", agent="agent")
    got = {r["series_id"]: r for r in backtest.Store(inputs).signal(D)}
    assert set(got) == set(want) and len(want) == 8
    for sid, w in want.items():
        g = got[sid]
        assert {f: g[f] for f in FIELDS} == {f: w[f] for f in FIELDS}, sid
        assert [(h["day"], h["y"], h["n"]) for h in g["hist"]] == [(h["day"], h["y"], h["n"]) for h in w["hist"]]


# 1. Replay reads only rows available by the day it replays


def late_world(late):
    w = World()
    w.protocol("feed_tiktok", 30)
    for j in range(5):
        for i in range(30, -1, -1):
            if (i + j) % 3 == 0:
                w.counter(f"r{j}", "feed_tiktok", i, float(1 + (i + j) % 3))
    if late:
        # a restated read of day(21), available on day(2)
        w.counter("r0", "feed_tiktok", 21, 3.0, read_day=day(2))
        # an item the feed first showed on day(2): the list watched it from day(30), but only from day(2) on
        w.counter("r9", "feed_tiktok", 2, 1.0)
        # a vendor curve first read on day(2), carrying days 12 to 2
        w.protocol("curve_tiktok_hashtag", 1, lane_class="unbiased_counter", market="GLOBAL")
        for i in range(12, 1, -1):
            w.counter("c0", "curve_tiktok_hashtag", i, 4.0, lane_class="unbiased_counter", unit="delta",
                      market="GLOBAL", source="vendor_history", read_day=day(2))
        for i in (1, 0):
            w.counter("c0", "curve_tiktok_hashtag", i, 5.0, lane_class="unbiased_counter", unit="delta",
                      market="GLOBAL")
    return w


def rerun_day_8_on_day_3(con):
    """A second collect run for day(8), finished on day(3), that marks the feed invalid that day."""
    rerun = health("feed_tiktok", day(8), valid=False, run_id="collect-rerun-8")
    duck.load(con, "core.collection_health", [rerun])
    duck.load(con, "agent.runs", [{**run("collect", day(8), run_id="collect-rerun-8"),
                                   "started_at": at(day(3), 11), "finished_at": at(day(3), 12)}])


def signal_at(con, t, as_of=D):
    inputs = backtest.load(BacktestClient(con), as_of, 7, core="core", agent="agent")
    return {r["series_id"]: r for r in backtest.Store(inputs).signal(t)}


def test_load_returns_only_rows_available_by_as_of(con):
    late_world(True).load(con)
    rerun_day_8_on_day_3(con)
    inputs = backtest.load(BacktestClient(con), day(5), 7, core="core", agent="agent")
    avail = lambda ts: ts.astimezone(backtest.UTC).date()
    assert inputs["reads"] and all(avail(r["available_at"]) <= day(5) for r in inputs["reads"])
    assert all(avail(r["finished_at"]) <= day(5) for r in inputs["runs"])
    assert "collect-rerun-8" not in {r["run_id"] for r in inputs["health"]}
    assert not [r for r in inputs["reads"] if r["item_id"] == "c0"]
    assert [r["value"] for r in inputs["reads"] if r["item_id"] == "r0" and r["day"] == day(21)] == [1.0]


def test_a_row_available_later_never_influences_a_past_day(con):
    late_world(True).load(con)
    rerun_day_8_on_day_3(con)
    other = connect()
    late_world(False).load(other)
    before = signal_at(other, day(5))
    other.close()

    # as of D everything is loaded; the replay of day(5) still sees only what was available on day(5)
    past = signal_at(con, day(5))
    assert past == before
    assert past == signal_at(con, day(5), as_of=day(5))

    # from day(3) the re-run applies; from day(2) the restated read and the vendor curve do
    r0 = "r0|ZA|feed_tiktok|p1"
    assert day(8) in {h["day"] for h in past[r0]["hist"]}
    three = {h["day"] for h in signal_at(con, day(3))[r0]["hist"]}
    assert day(8) not in three and {day(7), day(9)} <= three
    assert {h["day"]: h["y"] for h in past[r0]["hist"]}[day(21)] == 1.0
    now = signal_at(con, day(1))
    assert {h["day"]: h["y"] for h in now[r0]["hist"]}[day(21)] == 3.0
    c0 = now["c0|GLOBAL|curve_tiktok_hashtag|p1"]
    assert [h["day"] for h in c0["hist"]] == [day(i) for i in range(12, 1, -1)] and c0["y"] == 5.0
    assert "c0|GLOBAL|curve_tiktok_hashtag|p1" not in past
    r9 = now["r9|ZA|feed_tiktok|p1"]
    assert r9["obs_prior"] == 28 and r9["first_measured"] == day(2) and "r9|ZA|feed_tiktok|p1" not in past


def cutoff_world(con, stats_started=None):
    """late_world without restatements, plus a restated read of day(21) available at 15:00 on day(5)."""
    w = late_world(False)
    w.counter("r0", "feed_tiktok", 21, 3.0, read_day=day(5), hour=15)
    w.load(con)
    if stats_started is not None:
        duck.load(con, "agent.runs", [{**run("stats", day(5)), "started_at": stats_started,
                                       "finished_at": stats_started + timedelta(minutes=5)}])


@pytest.mark.parametrize("stats_started, seen", [
    (None, 3.0),                      # no stats run for day(5): the day ends at midnight UTC
    (at(day(5), 13), 1.0),            # the stats run started at 13:00, before the 15:00 read
    (at(day(3), 3), 3.0),             # a re-run days later never reaches past the end of day(5)
])
def test_a_row_counts_only_if_available_before_the_stats_run_of_the_day(con, stats_started, seen):
    cutoff_world(con, stats_started)
    r0 = signal_at(con, day(5))["r0|ZA|feed_tiktok|p1"]
    assert {h["day"]: h["y"] for h in r0["hist"]}[day(21)] == seen


def test_a_row_after_the_end_of_the_day_never_counts_even_after_a_late_stats_rerun(con):
    w = late_world(False)
    w.counter("r0", "feed_tiktok", 21, 3.0, read_day=day(4), hour=1)
    w.load(con)
    duck.load(con, "agent.runs", [{**run("stats", day(5)), "started_at": at(day(3), 3),
                                   "finished_at": at(day(3), 4)}])
    r0 = signal_at(con, day(5))["r0|ZA|feed_tiktok|p1"]
    assert {h["day"]: h["y"] for h in r0["hist"]}[day(21)] == 1.0


def test_items_join_the_cultural_map_on_their_valid_from_day(con):
    w = late_world(False)
    w.load(con)
    con.execute("UPDATE core.cultural_map SET valid_from = ? WHERE item_id = 'r1'", [at(day(3), 2)])
    assert "r1|ZA|feed_tiktok|p1" not in signal_at(con, day(4))
    assert "r1|ZA|feed_tiktok|p1" in signal_at(con, day(3))
    assert "r2|ZA|feed_tiktok|p1" in signal_at(con, day(4))
    sql = (SQL / "backtest.sql").read_text(encoding="utf-8")
    assert "first_seen has no history" in sql


# 2. Recall on injected spikes


def test_injected_spike_recall_per_multiplier_on_stable_series(con):
    stable_panel(World(), 30).load(con)
    res = replay(con)
    assert set(res["recall"]) == {"1.5", "2", "3", "5"}
    for m, r in res["recall"].items():
        assert r["injected"] == 40 * 7
        assert r["recall"] == pytest.approx(r["detected"] / r["injected"])
    rec = {m: res["recall"][m]["recall"] for m in res["recall"]}
    assert rec["3"] >= 0.8 and rec["5"] >= 0.8
    assert rec["1.5"] <= rec["2"] <= rec["3"] <= rec["5"]
    assert res["keys"][KEY]["recall"]["3"] == res["recall"]["3"]


def test_injections_are_whole_counts_and_rank_spikes_that_cannot_fit_are_infeasible():
    hist = [{"day": day(28 - i), "y": 1.0, "n": 3} for i in range(28)]
    row = {"y": 1.0, "trials": 3, "hist": hist}
    base = {"test": "betabinom", "mu": None}
    assert backtest.expected(row, base) == pytest.approx(1.0)
    assert backtest.injected(row, base, 2) == (2, True)
    assert backtest.injected(row, base, 3) == (3, True)
    assert backtest.injected(row, base, 5) == (5, False)            # 5 x a rate of 1/3 is above 1
    assert backtest.injected({**row, "hist": [dict(h, y=0.0) for h in hist]}, base, 3) is None
    assert backtest.injected(row, {"test": "nb", "mu": 4.2}, 3) == (13, True)       # 13 / 4.2 is 3.1 x
    assert backtest.injected(row, {"test": "nb", "mu": 10.0}, 3) == (30, True)
    assert backtest.injected(row, {"test": "nb", "mu": 1.0}, 3) == (3, True)


@pytest.mark.parametrize("e, m, want", [
    (0.15, 3, (1, False)),      # one appearance is 6.7 x, not 3 x
    (0.5, 3, (2, False)),       # two is 4 x
    (0.15, 5, (1, False)),
    (1.0, 1.5, (2, False)),     # two is 2 x, not 1.5 x
    (0.8, 3, (3, False)),       # three is 3.75 x: over 20% out
    (0.9, 3, (3, True)),        # three is 3.33 x: within 20%
])
def test_a_spike_counts_only_when_its_whole_count_is_within_20_percent_of_the_multiplier(e, m, want):
    assert backtest.injected({"y": 0.0, "trials": 3, "hist": []}, {"test": "nb", "mu": e}, m) == want
    # the same expected value on a rank list: 3 pulls today, a 28-day rate of e / 3
    hist = [{"day": day(28 - i), "y": 100 * e / 3, "n": 100} for i in range(28)]
    row = {"y": 0.0, "trials": 3, "hist": hist}
    base = {"test": "betabinom", "mu": None}
    assert backtest.expected(row, base) == pytest.approx(e)
    assert backtest.injected(row, base, m) == want


def sparse_feed_world(first=40, n_items=30):
    """Rank series that each appear in 1 of 3 pulls on every seventh day: an expected 0.14 appearances."""
    w = World()
    w.protocol("feed_tiktok", first)
    for j in range(n_items):
        for i in range(first, -1, -1):
            if (i + j) % 7 == 0 or i == first:
                w.counter(f"s{j}", "feed_tiktok", i, 1.0 if (i + j) % 7 == 0 else 0.0)
    return w


def test_sparse_rank_spikes_are_infeasible_and_never_reach_recall(con):
    sparse_feed_world().load(con)
    res = replay(con)
    k = res["keys"][RANK_KEY]
    assert k["tested"] > 0
    for m in ("1.5", "2", "3", "5"):
        r = k["recall"][m]
        assert r["injected"] == 0 and r["detected"] == 0 and r["recall"] is None
        assert r["infeasible"] == k["tested"]
    assert k["switch"] is False and any("0 feasible" in r for r in k["reasons"])


def feed_world(first=30, n_items=30, seed=3):
    """Rank series on the local feed with appearance rates from 0.05 to 0.6 of 3 pulls."""
    rng = np.random.default_rng(seed)
    w = World()
    w.protocol("feed_tiktok", first)
    for j in range(n_items):
        p = 0.05 + 0.55 * j / (n_items - 1)
        for i in range(first, -1, -1):
            v = rng.binomial(3, p)
            if v or i == first:
                w.counter(f"f{j}", "feed_tiktok", i, float(v))
    return w


RANK_KEY = "ZA|tiktok|unbiased_rank"


def test_rank_recall_leaves_infeasible_spikes_out_and_reports_a_sustained_spike_separately(con):
    feed_world().load(con)
    res = replay(con)
    k = res["keys"][RANK_KEY]
    for m, r in k["recall"].items():
        assert r["injected"] + r["infeasible"] <= k["tested"]
        assert r["recall"] == (pytest.approx(r["detected"] / r["injected"]) if r["injected"] else None)
    assert k["recall"]["5"]["infeasible"] > k["recall"]["1.5"]["infeasible"] >= 0
    assert res["recall"]["5"]["infeasible"] == k["recall"]["5"]["infeasible"]
    assert set(k["sustained_recall"]) == {"1.5", "2", "3", "5"}
    for r in k["sustained_recall"].values():
        assert r["injected"] > 0 and r["recall"] == pytest.approx(r["detected"] / r["injected"])
    assert "sustained_recall" not in replay_panel_keys()


def replay_panel_keys():
    c = connect()
    stable_panel(World(), 30).load(c)
    keys = replay(c)["keys"][KEY]
    c.close()
    return keys


# 3. False alarms on placebo windows


def test_placebo_false_alarms_per_market_and_platform(con):
    w = stable_panel(World(), 30)
    stable_panel(w, 30, market="NG", seed=2, prefix="n")
    w.t["core.item_daily"] = [r for r in w.t["core.item_daily"]
                              if not (r["item_id"] == "i0" and r["market"] == "ZA" and r["metric_date"] == D)]
    w.daily("i0", 0, 400, platform="facebook")
    w.load(con)
    res = replay(con)
    za, ng = res["false_alarms"]["ZA|facebook"], res["false_alarms"]["NG|facebook"]
    assert za["tested"] == ng["tested"] == 40 * 7
    assert za["alarms"] >= 1 and za["rate"] == pytest.approx(za["alarms"] / za["tested"])
    assert ng["rate"] <= 0.05
    assert res["keys"][KEY]["alarms"] == za["alarms"] and res["keys"][KEY]["tested"] == za["tested"]


def small_and_large_world(con, in_force=False, written=None):
    """A large calibrated key (ZA facebook panel, 40 series) and a small one (ZA x panel, 12 series)."""
    w = stable_panel(World(), 30)
    w.protocol("panel_x_hub", 30, platform="x", lane_class="panel", k=1.0)
    rng = np.random.default_rng(9)
    for j in range(12):
        ys = rng.negative_binomial(10, 0.5, size=31)
        for i in range(30, -1, -1):
            w.daily(f"x{j}", i, int(ys[30 - i]), platform="x", series="panel_x_hub")
    w.load(con)
    if in_force:
        duck.load(con, "core.test_switch", [{"market": "ZA", "platform": "facebook", "lane_class": "panel",
                                             "switched_on": day(20), "backtest_run_id": "bt-0",
                                             "rule_version": stats.RULE_VERSION}])
    if written is not None:
        duck.load(con, "agent.runs", [{**run("backtest", written.date(), run_id="bt-0"),
                                       "started_at": written - timedelta(minutes=5), "finished_at": written}])


def pile_x_near_zero(monkeypatch):
    """The small key's tested p-values all sit at 0.02: miscalibrated, but hidden in a family of 52."""
    real = stats.series_test_rows

    def skewed(rows, *a, **kw):
        out = real(rows, *a, **kw)
        for r in out:
            if r["platform"] == "x" and r["test"] != "none":
                r["p_mid"] = 0.02
        return out

    monkeypatch.setattr(stats, "series_test_rows", skewed)


def test_a_small_miscalibrated_key_is_measured_alone_and_stays_off(con, monkeypatch):
    small_and_large_world(con)
    pile_x_near_zero(monkeypatch)
    res = replay(con)
    x, fb = res["keys"]["ZA|x|panel"], res["keys"][KEY]
    assert x["tested"] == 12 * 7 and x["alarms"] == 12 * 7 and x["false_alarm_rate"] == 1.0
    assert x["switch"] is False and any("false-alarm" in r for r in x["reasons"])
    assert fb["tested"] == 40 * 7 and fb["false_alarm_rate"] <= 0.05 and fb["switch"] is True
    # BH over all 52 series of the market, as the old family did, would have passed nearly every x p-value
    ps = [0.02] * 12 + list(np.linspace(0.05, 1, 40))
    assert not any(st.false_discovery_control(ps, method="bh")[:12] <= 0.05)


def test_keys_already_in_force_join_the_family_of_the_key_measured(con, monkeypatch):
    small_and_large_world(con, in_force=True, written=at(day(20)))
    pile_x_near_zero(monkeypatch)
    res = replay(con)
    x = res["keys"]["ZA|x|panel"]
    assert x["tested"] == 12 * 7 and x["false_alarm_rate"] < 0.5
    assert res["keys"][KEY]["tested"] == 40 * 7


@pytest.mark.parametrize("written, alone_days", [
    (None, 7),                          # no ok runs row for the backtest that wrote it: never in force here
    (at(D + timedelta(days=1)), 7),     # written after the window, though switched_on is day(20)
    (at(day(3)), 3),                    # written on day(3): x is alone on day(6), day(5) and day(4)
])
def test_a_switch_row_joins_a_family_only_from_when_it_was_written(con, monkeypatch, written, alone_days):
    small_and_large_world(con, in_force=True, written=written)
    pile_x_near_zero(monkeypatch)
    x = replay(con)["keys"]["ZA|x|panel"]
    assert x["tested"] == 12 * 7
    assert x["alarms"] >= 12 * alone_days
    if alone_days < 7:
        assert x["alarms"] < 12 * 7


# 4. The switch rule


GOOD = {"observed_days": 20, "tested": 100, "alarms": 2, "false_alarm_rate": 0.02,
        "market_platform_false_alarm_rate": 0.02,
        "recall": {"3": {"injected": 100, "detected": 85, "recall": 0.85}}}


@pytest.mark.parametrize("change, on", [
    ({}, True),
    ({"observed_days": 13}, False),
    ({"false_alarm_rate": 0.06}, False),
    ({"market_platform_false_alarm_rate": 0.051}, False),
    ({"recall": {"3": {"injected": 100, "detected": 79, "recall": 0.79}}}, False),
    ({"tested": 59}, False),
    ({"tested": 60}, True),
    ({"tested": 60, "alarms": 0, "false_alarm_rate": 0.0}, True),
    ({"recall": {"3": {"injected": 19, "detected": 19, "recall": 1.0}}}, False),
    ({"recall": {"3": {"injected": 20, "detected": 20, "recall": 1.0}}}, True),
    ({"recall": {"3": {"injected": 10, "detected": 10, "infeasible": 90, "recall": 1.0}}}, False),
    ({"recall": {"3": {"injected": 100, "detected": 50, "recall": 0.5}},
      "sustained_recall": {"3": {"injected": 100, "detected": 100, "recall": 1.0}}}, False),
    ({"false_alarm_rate": 0.05, "recall": {"3": {"injected": 100, "detected": 80, "recall": 0.8}}}, True),
])
def test_switch_rule(change, on):
    got, reasons = backtest.decide({**GOOD, **change})
    assert got is on and (reasons == []) is on


def test_switch_row_is_appended_once_per_key_and_rule_version(con, tmp_path):
    stable_panel(World(), 30).load(con)
    client = BacktestClient(con)
    first = backtest.run(client, D, apply=True, days=7, out_dir=tmp_path, core="core", agent="agent")
    second = backtest.run(client, D, apply=True, days=7, out_dir=tmp_path, core="core", agent="agent")
    rows = duck.query(con, "SELECT * FROM {core}.test_switch")
    assert rows == [{"market": "ZA", "platform": "facebook", "lane_class": "panel",
                     "switched_on": D + timedelta(days=1), "backtest_run_id": first["run_id"],
                     "rule_version": stats.RULE_VERSION}]
    assert first["keys"][KEY]["switch"] and second["keys"][KEY]["switch"]
    assert len(first["switch_on"]) == 1 and second["switch_on"] == []
    assert ("ZA", "facebook", "panel") in stats.in_force(rows, D + timedelta(days=1))
    assert not stats.in_force(rows, D)


# 5. Nothing switches on before 14 observed days, and the first switch needs a tested day after them


@pytest.mark.parametrize("first, n_items, tested, on", [
    (12, 60, 0, False), (13, 60, 0, False), (14, 60, 60, True),     # 15 observed days and 60 series: one tested day
    (14, 40, 40, False), (15, 40, 80, True),                        # 40 series need a second tested day
])
def test_observed_days_before_the_first_switch(con, tmp_path, first, n_items, tested, on):
    stable_panel(World(), first, n_items=n_items).load(con)
    res = backtest.run(BacktestClient(con), D, apply=True, days=7, out_dir=tmp_path, core="core", agent="agent")
    k = res["keys"][KEY]
    assert k["observed_days"] == first + 1
    assert k["tested"] == tested
    assert k["switch"] is on
    assert len(duck.query(con, "SELECT * FROM {core}.test_switch")) == (1 if on else 0)


class FailingSwitchClient(BacktestClient):
    def insert_rows_json(self, table, rows):
        if table.endswith("test_switch"):
            return [{"index": 0, "errors": ["refused"]}]
        return super().insert_rows_json(table, rows)


def test_a_failed_switch_insert_leaves_no_ok_backtest_row(con, tmp_path):
    stable_panel(World(), 30).load(con)
    with pytest.raises(RuntimeError, match="test_switch"):
        backtest.run(FailingSwitchClient(con), D, apply=True, days=7, out_dir=tmp_path, core="core", agent="agent")
    assert duck.query(con, "SELECT * FROM {agent}.runs r WHERE r.stage = 'backtest'") == []


# 6. Results as one JSON file and one runs row


def test_results_go_to_one_json_file_and_a_backtest_runs_row(con, tmp_path):
    stable_panel(World(), 30).load(con)
    res = backtest.run(BacktestClient(con), D, apply=True, days=7, out_dir=tmp_path, core="core", agent="agent")
    [path] = list(tmp_path.iterdir())
    assert path.name == f"{res['run_id']}.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == res
    for field in ("recall", "false_alarms", "series", "keys", "top10_precision", "switch_on"):
        assert field in saved
    assert saved["series"]["replayed"] == saved["series"]["tested"] == 40 * 7
    [row] = duck.query(con, "SELECT * FROM {agent}.runs r WHERE r.stage = 'backtest'")
    assert row["run_id"] == res["run_id"] and row["status"] == "ok" and row["run_date"] == D
    counts = json.loads(row["counts"])
    assert counts["recall"] == saved["recall"] and counts["false_alarms"] == saved["false_alarms"]
    assert counts["series"] == saved["series"]


def test_backtests_directory_is_kept_in_the_repository():
    assert backtest.OUT_DIR == Path(backtest.__file__).parent / "backtests"
    assert (backtest.OUT_DIR / ".gitkeep").exists()


def test_dry_run_prints_what_would_switch_on_and_writes_nothing_to_bigquery(con, tmp_path, capsys):
    stable_panel(World(), 30).load(con)
    client = BacktestClient(con)
    assert backtest.main(["--as-of", D.isoformat(), "--days", "7"], client=client, out_dir=tmp_path,
                         core="core", agent="agent") == 0
    out = capsys.readouterr().out
    assert "would switch on" in out and KEY in out
    assert duck.query(con, "SELECT * FROM {core}.test_switch") == []
    # a replay leaves one runs row, status replayed, which puts nothing in force and is never an accepted run
    assert [r["status"] for r in duck.query(con, "SELECT * FROM {agent}.runs r WHERE r.stage = 'backtest'")] == [
        "replayed"]
    assert backtest.main(["--as-of", D.isoformat(), "--days", "7", "--apply"], client=client, out_dir=tmp_path,
                         core="core", agent="agent") == 0
    assert "switched on" in capsys.readouterr().out
    assert len(duck.query(con, "SELECT * FROM {core}.test_switch")) == 1


# 6b. A key with no platform (a curated creator panel's collection_health rows) is skipped, not measured


def curated_world(first=30, items=True):
    """The stable facebook panel plus a curated creator panel whose collection_health rows have no platform;
    its item_daily rows carry the creators' platform, as item_daily's NOT NULL platform requires. With items
    False the curated panel has health rows but no item series, so nothing names a platform for its days."""
    w = stable_panel(World(), first)
    w.protocol("panel_creators_curated", first, platform=None, lane_class="panel", k=1.0)
    rng = np.random.default_rng(7)
    for j in range(10 if items else 0):
        ys = rng.negative_binomial(10, 0.5, size=first + 1)
        for i in range(first, -1, -1):
            w.daily(f"c{j}", i, int(ys[first - i]), platform="instagram", series="panel_creators_curated")
    return w


def test_a_key_with_no_platform_and_no_series_to_name_one_is_skipped_and_the_rest_is_measured_as_before(con):
    curated_world(items=False).load(con)
    res = replay(con)
    assert all("-" not in name.split("|") for name in res["keys"])
    assert res["skipped"] == [{"key": "ZA|-|panel", "market": "ZA", "platform": None, "lane_class": "panel",
                               "observed_days": 31, "replayed": 0, "reason": "no platform"}]
    assert list(res["false_alarms"]) == ["ZA|facebook"]
    alone = connect()
    try:
        stable_panel(World(), 30).load(alone)
        assert res["keys"][KEY] == replay(alone)["keys"][KEY]
    finally:
        alone.close()


def test_dry_run_and_apply_with_a_no_platform_key(con, tmp_path, capsys):
    curated_world(items=False).load(con)
    client = BacktestClient(con)
    assert backtest.main(["--as-of", D.isoformat(), "--days", "7"], client=client, out_dir=tmp_path,
                         core="core", agent="agent") == 0
    out = capsys.readouterr().out
    assert "ZA|-|panel: skipped: no platform (31 observed days, 0 series-days replayed)" in out
    assert "None" not in out
    assert f"would switch on: {KEY}" in out
    assert backtest.main(["--as-of", D.isoformat(), "--days", "7", "--apply"], client=client, out_dir=tmp_path,
                         core="core", agent="agent") == 0
    rows = duck.query(con, "SELECT * FROM {core}.test_switch")
    assert [(r["market"], r["platform"], r["lane_class"]) for r in rows] == [("ZA", "facebook", "panel")]


# 6c. A panel's collection_health rows carry no platform (the collect writer sets it NULL for prism/profiles and
# search/multi), so its valid days are counted per market, series and protocol and given to every market, platform
# and lane class with a series on that series and protocol, the way Store.series matches health.


def test_a_null_platform_panel_health_row_gives_its_series_platforms_their_observed_days(con):
    w = World()
    w.protocol("panel_creators_curated", 19, platform=None, lane_class="panel", k=1.0)
    w.health[("panel_creators_curated", "ZA", "p1", day(3))]["valid"] = False
    for i in range(19, -1, -1):
        w.daily("t0", i, 2, platform="tiktok", series="panel_creators_curated")
        w.daily("g0", i, 1, platform="instagram", series="panel_creators_curated")
    w.load(con)
    store = backtest.Store(backtest.load(BacktestClient(con), D, 7, core="core", agent="agent"))
    assert dict(store.observed_days(D)) == {("ZA", "tiktok", "panel"): 19, ("ZA", "instagram", "panel"): 19}


def test_a_curated_panel_with_series_is_measured_under_its_creators_platform_not_skipped(con):
    curated_world().load(con)
    res = replay(con)
    assert res["skipped"] == []
    k = res["keys"]["ZA|instagram|panel"]
    assert k["observed_days"] == 31 and k["replayed"] == 70
    assert not [r for r in k["reasons"] if "observed days" in r]
    assert list(res["false_alarms"]) == ["ZA|facebook", "ZA|instagram"]
    alone = connect()
    try:
        stable_panel(World(), 30).load(alone)
        assert res["keys"][KEY] == replay(alone)["keys"][KEY]
    finally:
        alone.close()


def test_key_order_and_labels_take_a_missing_part():
    keys = [("ZA", "tiktok", "panel"), ("ZA", None, "panel"), ("GLOBAL", "tiktok", None), ("ZA", "facebook", "panel")]
    assert sorted(keys, key=backtest._order) == [("GLOBAL", "tiktok", None), ("ZA", None, "panel"),
                                                 ("ZA", "facebook", "panel"), ("ZA", "tiktok", "panel")]
    assert backtest._label(("ZA", None, "panel")) == "ZA|-|panel"
    assert backtest._missing(("ZA", None, "panel")) == ["platform"]
    assert backtest._missing(("ZA", None, None)) == ["platform", "lane class"]
    assert backtest._missing(("ZA", "tiktok", "panel")) == []


# 7. Top-10 precision against later persistence


def state_rows(t, market, states, worth=None, eligible=True):
    return [{"metric_date": t, "market": market, "item_id": f"{market}{j}", "state": s, "eligible": eligible,
             "worth_raw": (worth or (lambda j: 1.0 - j / 100))(j), "run_id": f"detect-{t:%Y%m%d}"}
            for j, s in enumerate(states)]


def precision_world(con):
    rows, runs = [], []
    for i in range(13, -1, -1):
        t = day(i)
        runs.append(run("detect", t))
        # twelve eligible items: the top ten are items 0 to 9, of which 0 to 5 persist; plus an ineligible leader
        rows += state_rows(t, "ZA", ["rising"] * 6 + ["fading"] * 4 + ["rising"] * 2)
        rows += [{"metric_date": t, "market": "ZA", "item_id": "lead", "state": "fading", "eligible": False,
                  "worth_raw": 9.0, "run_id": f"detect-{t:%Y%m%d}"}]
        if i <= 1:
            rows += state_rows(t, "NG", ["emerging"] * 3)
    duck.load(con, "core.item_state", rows)
    duck.load(con, "agent.runs", runs)


def test_top10_precision_when_7_later_days_exist(con):
    precision_world(con)
    res = replay(con, days=14)["top10_precision"]
    assert res["ZA"] == {"days": 7, "items": 70, "persisted": 42, "precision": pytest.approx(0.6)}
    assert res["NG"] == {"status": "not yet measurable"}


def test_top10_precision_is_not_yet_measurable_without_7_later_days(con):
    precision_world(con)
    res = replay(con, days=7)["top10_precision"]
    assert res == {"ZA": {"status": "not yet measurable"}, "NG": {"status": "not yet measurable"}}


# sql/backtest.sql


def test_backtest_sql_is_bigquery_selects_that_read_by_available_at():
    stmts = sqlrun.split(sqlrun.render((SQL / "backtest.sql").read_text(encoding="utf-8")))
    assert len(stmts) == len(backtest.QUERIES) == 8
    for s in stmts:
        assert isinstance(sqlglot.parse_one(s, read="bigquery"), (sqlglot.exp.Select, sqlglot.exp.Union))
        assert not any(w in s.upper() for w in ("INSERT", "DELETE", "MERGE", "TRUNCATE", "DROP", "UPDATE"))
    assert "available_at" in backtest.READS_SQL and "@as_of" in backtest.READS_SQL


@pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")
def test_backtest_sql_dry_runs_on_bigquery():
    from .test_detect_bigquery import PROJECT, dry_run
    client = bigquery.Client(project=PROJECT)
    for sql in [*backtest.QUERIES.values(), backtest.WRITTEN_SQL]:
        dry_run(client, sqlrun.render(sql), backtest.params(sql, D, 7))
