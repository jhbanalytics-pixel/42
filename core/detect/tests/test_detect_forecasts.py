"""Engine forecasts (DATA.md section 6): known-answer tests on forecasts.py and forecasts.sql through DuckDB.

Issue: six forecasts (three targets by two horizons) for every Emerging and Rising item in ZA, NG and KE plus a
seeded 10% control sample, each with its target's persistence baseline, and never a duplicate on a rerun.
Resolution: one appended row per forecast once its window has closed, read through the latest good runs, and
none while a failed collection day sits in the window. Scoring: forecast_cohort.review_arrival_cohort with Brier
against persistence, never promotion eligible below 200 comparable rows.
"""

import hashlib
import json
import os
import subprocess
import sys
from datetime import timedelta

import pytest

from core.detect import forecasts, sqlrun

from . import duck
from .fixtures import D, at, counter, day, health, rid, run

RUN = "detect-20260920-issue"
MARKETS = ("ZA", "NG", "KE")
TARGETS = ("reach_rising", "cross_market", "persist_50")
I = day(10)                              # issue date of the resolution fixtures; horizon 7 closes on day(3)


class Client(duck.Client):
    """duck.Client that also applies CREATE OR REPLACE VIEW statements as DuckDB views."""

    def query(self, sql, job_config=None):
        text = duck.strip_leading_comments(sql)
        if text.upper().startswith("CREATE OR REPLACE"):
            self.sql.append(sql)
            self.con.execute(duck.create_statement(text))
            return duck._Job([])
        return super().query(sql, job_config)


@pytest.fixture
def con():
    c = duck.connect()
    yield c
    c.close()


def state(item, market, st, d=D, run_id=RUN, markets_hot=1, sig_days3=1, creators3=5, worth_pct=0.5,
          main_series_id=None, authenticity="clear"):
    return {"metric_date": d, "market": market, "item_id": item, "kind": "hashtag", "state_raw": st, "state": st,
            "untested": False, "main_series_id": main_series_id, "sig_days3": sig_days3, "creators3": creators3,
            "markets_hot": markets_hot, "authenticity": authenticity, "eligible": True, "worth_pct": worth_pct,
            "run_id": run_id, "rule_version": "r1"}


def rows(con, where="TRUE", params=None):
    return duck.query(con, f"SELECT * FROM {{agent}}.forecasts f WHERE {where} "
                           "ORDER BY f.item_id, f.market, f.target, f.horizon, f.observed_arrival IS NOT NULL",
                      params or {})


def issue(con, d=D, run_id=RUN):
    client = Client(con)
    return forecasts.run_forecasts(client, d, run_id, core="core", agent="agent"), client


# forecast_id, the control draw and the logistic rule


def test_forecast_id_is_the_sha256_of_item_market_target_issue_date_horizon_and_rule():
    key = "abc|ZA|reach_rising|2026-09-20|7|logistic_v1"
    assert forecasts.forecast_id("abc", "ZA", "reach_rising", D, 7) == hashlib.sha256(key.encode()).hexdigest()
    assert forecasts.RULE == "logistic_v1"
    # the agent's log_forecast rule (ask_v1) gives a different id for the same forecast, so the two never clash
    assert forecasts.forecast_id("abc", "ZA", "reach_rising", D, 7, rule="ask_v1") != \
        forecasts.forecast_id("abc", "ZA", "reach_rising", D, 7)


def test_the_control_draw_is_a_seeded_hash_of_item_and_date_about_one_in_ten():
    ids = [f"item{i}" for i in range(20000)]
    drawn = [i for i in ids if forecasts.in_control(i, D)]
    assert 0.09 <= len(drawn) / len(ids) <= 0.11
    assert drawn == [i for i in ids if forecasts.in_control(i, D)]                 # reproducible
    other = {i for i in ids if forecasts.in_control(i, day(1))}
    assert other != set(drawn)                                                   # redrawn each day
    digest = hashlib.sha256(f"item7|{D.isoformat()}".encode()).hexdigest()
    assert forecasts.in_control("item7", D) == (int(digest[:8], 16) / 16 ** 8 < 0.10)


def test_the_logistic_rule_is_explicit_and_gives_a_probability_per_target_and_horizon():
    assert set(forecasts.COEFFICIENTS) == set(TARGETS)
    for coef in forecasts.COEFFICIENTS.values():
        assert "intercept" in coef and set(coef) - {"intercept"} <= set(forecasts.FEATURES)
    hot = {"state": "rising", "markets_hot": 2, "sig_days3": 3, "creators3": 12, "worth_pct": 0.9, "main_vel": 0.6}
    cold = {"state": "fading", "markets_hot": 0, "sig_days3": 0, "creators3": 1, "worth_pct": 0.1, "main_vel": -0.5}
    for target in TARGETS:
        for horizon in (7, 14):
            p_hot, p_cold = forecasts.prob(target, horizon, hot), forecasts.prob(target, horizon, cold)
            assert 0 < p_cold < p_hot < 1
    # NULL features read as neutral values, never as an error
    blank = {"state": "emerging", "markets_hot": None, "sig_days3": None, "creators3": None, "worth_pct": None,
             "main_vel": None}
    assert 0 < forecasts.prob("reach_rising", 7, blank) < 1


# (a), (b) and (e): issuing


def issue_world(con, others=300):
    base = [state("em-za", "ZA", "emerging"), state("ri-za", "ZA", "rising", markets_hot=2),
            state("em-ng", "NG", "emerging", markets_hot=3), state("ri-ke", "KE", "rising"),
            state("ri-gl", "GLOBAL", "rising", markets_hot=2), state("em-gl", "GLOBAL", "emerging")]
    kinds = ("fading", "spike", "peaking", "new_to_42", "mainstream", "seasonal")
    other = [state(f"o{i}", MARKETS[i % 3], kinds[i % len(kinds)], markets_hot=i % 4) for i in range(others)]
    # an older detect run for D holds an item the pinned run does not: it is never read
    stray = [state("stray", "ZA", "rising", run_id="detect-20260920-old")]
    duck.load(con, "core.item_state", base + other + stray)
    return {r["item_id"]: r for r in base + other}


def test_emerging_and_rising_items_get_six_forecasts_and_the_control_sample_about_ten_percent(con):
    world = issue_world(con)
    counts, _ = issue(con)
    got = rows(con)
    by_item = {}
    for r in got:
        by_item.setdefault(r["item_id"], []).append(r)
    for item in ("em-za", "ri-za", "em-ng", "ri-ke"):
        assert sorted((r["target"], r["horizon"]) for r in by_item[item]) == sorted(
            (t, h) for t in TARGETS for h in (7, 14))
    control = {i for i in world if i.startswith("o") and forecasts.in_control(i, D)}
    assert 0.07 * 300 <= len(control) <= 0.13 * 300
    assert {i for i in by_item if i.startswith("o")} == control
    assert all(len(by_item[i]) == 6 for i in control)
    assert "stray" not in by_item
    assert counts["watched"] == 4 and counts["control"] == len(control)
    assert counts["issued"] == len(got) == 6 * (4 + len(control))
    for r in got:
        assert r["issue_date"] == D and r["rule"] == "logistic_v1" and r["observed_arrival"] is None
        assert r["resolve_date"] == D + timedelta(days=r["horizon"])
        assert 0 < r["prob"] < 1 and r["predicted_arrival"] == (r["prob"] >= 0.5)
        assert r["forecast_id"] == forecasts.forecast_id(r["item_id"], r["market"], r["target"], D, r["horizon"])


def test_global_never_gets_a_forecast(con):
    issue_world(con)
    issue(con)
    assert {r["market"] for r in rows(con)} <= set(MARKETS)
    assert rows(con, "f.market = 'GLOBAL'") == []
    assert rows(con, "f.item_id IN ('ri-gl', 'em-gl')") == []


def test_persistence_follows_each_targets_definition(con):
    world = issue_world(con)
    issue(con)
    got = {(r["item_id"], r["target"], r["horizon"]): r["persistence_arrival"] for r in rows(con)}
    for h in (7, 14):
        # reach_rising: Rising or Peaking now
        assert got[("ri-za", "reach_rising", h)] is True and got[("em-za", "reach_rising", h)] is False
        # cross_market: markets_hot >= 2 now
        assert got[("ri-za", "cross_market", h)] is True and got[("em-ng", "cross_market", h)] is True
        assert got[("em-za", "cross_market", h)] is False and got[("ri-ke", "cross_market", h)] is False
    # persist_50: always
    assert {v for (_, t, _), v in got.items() if t == "persist_50"} == {True}
    for (item, target, _), v in got.items():
        if item.startswith("o"):
            if target == "reach_rising":
                assert v == (world[item]["state"] in ("rising", "peaking"))
            if target == "cross_market":
                assert v == (world[item]["markets_hot"] >= 2)
    assert any(world[i]["state"] == "peaking" and got[(i, "reach_rising", 7)] for i, _, _ in got if i in world)


def test_a_seasonal_or_recurring_row_resting_on_emerging_or_rising_is_watched(con):
    """base_state carries the state before the Seasonal or Recurring override; NULL on rows written before it."""
    base = {"seasonal-rising": ("ZA", "seasonal", "rising"), "recurring-emerging": ("NG", "recurring", "emerging"),
            "seasonal-spike": ("KE", "seasonal", "spike"), "seasonal-new": ("ZA", "seasonal", "new_to_42"),
            "seasonal-old": ("ZA", "seasonal", None)}
    assert not any(forecasts.in_control(i, D) for i in base)       # so only watching can issue them
    duck.load(con, "core.item_state", [{**state(i, m, st), "base_state": b} for i, (m, st, b) in base.items()])
    counts, _ = issue(con)
    got = {}
    for r in rows(con):
        got.setdefault(r["item_id"], []).append(r)
    assert set(got) == {"seasonal-rising", "recurring-emerging"} and all(len(v) == 6 for v in got.values())
    assert (counts["watched"], counts["control"]) == (2, 0)


def test_the_rule_reads_the_main_series_velocity_through_the_current_stats_run(con):
    duck.load(con, "core.item_state", [state("fast", "ZA", "rising", main_series_id="s-fast"),
                                       state("slow", "ZA", "rising", main_series_id="s-slow"),
                                       state("gone", "ZA", "rising", main_series_id="s-gone")])
    duck.load(con, "core.series_test", [
        {"metric_date": D, "series_id": "s-fast", "item_id": "fast", "market": "ZA", "vel": 1.0,
         "run_id": "stats-ok"},
        {"metric_date": D, "series_id": "s-slow", "item_id": "slow", "market": "ZA", "vel": -1.0,
         "run_id": "stats-ok"},
        {"metric_date": D, "series_id": "s-slow", "item_id": "slow", "market": "ZA", "vel": 5.0,
         "run_id": "stats-failed"},
        {"metric_date": D, "series_id": "s-gone", "item_id": "gone", "market": "ZA", "vel": 5.0,
         "run_id": "stats-failed"}])
    duck.load(con, "agent.runs", [run("stats", D, "stats-ok"), run("stats", D, "stats-failed", "failed", hour=14)])
    issue(con)
    assert len(rows(con, "f.item_id = 'slow'")) == 6                          # the failed run adds no second row
    p = {r["item_id"]: r["prob"] for r in rows(con, "f.target = 'reach_rising' AND f.horizon = 7")}
    assert p["fast"] > p["slow"]
    assert p["slow"] == pytest.approx(forecasts.prob("reach_rising", 7, {
        "state": "rising", "markets_hot": 1, "sig_days3": 1, "creators3": 5, "worth_pct": 0.5, "main_vel": -1.0}))
    # a velocity only a failed stats run holds is unknown, read as 0
    assert p["gone"] == pytest.approx(forecasts.prob("reach_rising", 7, {
        "state": "rising", "markets_hot": 1, "sig_days3": 1, "creators3": 5, "worth_pct": 0.5, "main_vel": None}))


# (d): a rerun on the same day writes no duplicate forecast_ids


def test_a_rerun_on_the_same_day_writes_no_duplicate_forecast_ids(con):
    issue_world(con)
    first, _ = issue(con)
    n = len(rows(con))
    second, _ = issue(con)
    # a second detect run for D writes the same states under its own run_id
    duck.load(con, "core.item_state", [{**r, "run_id": "detect-20260920-again"} for r in duck.query(
        con, "SELECT * FROM {core}.item_state s WHERE s.run_id = @r", {"r": RUN})])
    third, _ = issue(con, run_id="detect-20260920-again")
    assert first["issued"] == n > 0 and second["issued"] == 0 and third["issued"] == 0
    assert len(rows(con)) == n
    assert duck.query(con, "SELECT f.forecast_id FROM {agent}.forecasts f GROUP BY 1 HAVING COUNT(*) > 1") == []


def test_the_append_statement_itself_skips_an_issued_forecast_and_a_second_resolution(con):
    issue_world(con, others=0)
    cands = duck.query(con, "SELECT s.item_id, s.market, s.state, s.markets_hot FROM {core}.item_state s "
                            "WHERE s.run_id = @r", {"r": RUN})
    new = forecasts.issue_rows(D, cands)
    client = Client(con)
    forecasts._append(client, new, "core", "agent")
    forecasts._append(client, new, "core", "agent")
    assert len(rows(con)) == len(new) == 24
    done = [{**r, "observed_arrival": True} for r in new]
    forecasts._append(client, done, "core", "agent")
    forecasts._append(client, [{**r, "observed_arrival": False} for r in new], "core", "agent")
    assert len(rows(con)) == 48 and {r["observed_arrival"] for r in rows(con)} == {None, True}


def test_the_step_only_appends(con):
    issue_world(con)
    _, client = issue(con)
    for sql in client.sql:
        upper = duck.strip_leading_comments(sql).upper()
        for word in ("UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "MERGE"):
            assert word not in upper
        assert not upper.startswith("CREATE OR REPLACE TABLE")


# (c): resolution


ROUTES = {"tiktok": "feed_tiktok", "reddit": "list_reddit"}   # one route per platform in the fixtures


class ResolveWorld:
    """Every stage ok for day(20) to D, feed_tiktok (tiktok) and list_reddit (reddit) valid in all three markets,
    and the fixture forecasts. failed = (stage, day) makes that stage's only run for that day a failed one."""

    def __init__(self, con, failed=None):
        self.con = con
        days = [day(i) for i in range(20, -1, -1)]
        duck.load(con, "agent.runs", [run(s, d, status="failed" if (s, d) == failed else "ok")
                                      for s in ("collect", "detect", "stats") for d in days])
        duck.load(con, "core.collection_health", [health(r, d, market=m, platform=p)
                                                  for d in days for m in MARKETS for p, r in ROUTES.items()])

    def fc(self, item, target, market="ZA", horizon=7, issue_date=I, rule="logistic_v1", prob=0.6,
           persistence=False, issue_state="emerging", main="tiktok"):
        """One issued forecast, with the item's state on the issue day and, unless main is None, its main series
        on that platform's route, tested on the issue day."""
        row = {"forecast_id": forecasts.forecast_id(item, market, target, issue_date, horizon, rule=rule),
               "item_id": item, "market": market, "target": target, "issue_date": issue_date, "horizon": horizon,
               "rule": rule, "prob": prob, "predicted_arrival": prob >= 0.5, "persistence_arrival": persistence,
               "resolve_date": issue_date + timedelta(days=horizon), "observed_arrival": None}
        duck.load(self.con, "agent.forecasts", [row])
        series_id = f"{item}|{market}|{ROUTES[main]}|p1" if main else None
        self.state(item, issue_state, issue_date, market=market, main_series_id=series_id)
        if main:
            self.test(item, market, issue_date, significant=False, platform=main)
        return row["forecast_id"]

    def state(self, item, st, d, market="ZA", run_id=None, **kw):
        duck.load(self.con, "core.item_state", [state(item, market, st, d=d, run_id=run_id or rid("detect", d), **kw)])

    def test(self, item, market, d, significant=True, run_id=None, platform="tiktok"):
        duck.load(self.con, "core.series_test", [{
            "metric_date": d, "series_id": f"{item}|{market}|{ROUTES[platform]}|p1", "item_id": item,
            "market": market, "platform": platform, "significant": significant, "q": 0.01 if significant else 0.5,
            "run_id": run_id or rid("stats", d)}])

    def collect_again(self, d, invalid=(), extra=()):
        """A later ok collect run for day d that supersedes the first: every fixture route again, valid except
        the (market, route) pairs in invalid, plus the extra health rows."""
        later = f"collect-{d:%Y%m%d}-later"
        rows = [health(r, d, market=m, platform=p, valid=(m, r) not in invalid, run_id=later)
                for m in MARKETS for p, r in ROUTES.items()]
        rows += [{**h, "run_id": later} for h in extra]
        duck.load(self.con, "core.collection_health", rows)
        duck.load(self.con, "agent.runs", [run("collect", d, later, hour=14)])

    def series(self, item, values):
        """Daily appearances on feed_tiktok in ZA; values maps days-before-D to a count."""
        duck.load(self.con, "core.item_counter_daily", [counter(item, "feed_tiktok", day(i), v)
                                                        for i, v in values.items()])
        return f"{item}|ZA|feed_tiktok|p1"


def resolve(con, d=D):
    return forecasts.run_forecasts(Client(con), d, f"detect-{d:%Y%m%d}-none", core="core", agent="agent")


def resolved(con):
    return {r["forecast_id"]: r["observed_arrival"]
            for r in rows(con, "f.observed_arrival IS NOT NULL")}


def reach_world(con, failed=None):
    w = ResolveWorld(con, failed)
    ids = {
        "up": w.fc("up", "reach_rising"),
        "flat": w.fc("flat", "reach_rising", persistence=True, issue_state="rising"),
        "ghost": w.fc("ghost", "reach_rising"),
        "stale": w.fc("stale", "reach_rising"),
        "agent": w.fc("agent", "reach_rising", rule="ask_v1"),
        "late": w.fc("late", "reach_rising", horizon=14),
        "today": w.fc("today", "reach_rising", issue_date=day(7)),
        "ngup": w.fc("ngup", "reach_rising", market="NG"),
    }
    w.state("up", "rising", day(5))
    w.state("agent", "peaking", day(6))
    w.state("flat", "fading", day(6))
    # flat is rising on I, but the issue day is not in the window
    w.state("flat", "rising", day(2))                              # nor is a day after resolve_date
    duck.load(con, "agent.runs", [run("detect", day(5), "detect-bad", "failed", hour=14)])
    w.state("ghost", "rising", day(5), run_id="detect-bad")       # only in a failed run: never read
    duck.load(con, "agent.runs", [run("detect", day(4), "detect-late", hour=14)])
    w.state("stale", "rising", day(4))                             # superseded by the later ok run
    w.state("stale", "fading", day(4), run_id="detect-late")
    w.state("late", "rising", day(5))
    w.state("today", "rising", day(1))
    w.state("ngup", "rising", day(5), market="NG")
    return w, ids


def test_resolution_appends_observed_arrival_read_through_the_latest_good_runs(con):
    _, ids = reach_world(con)
    # a youtube route failed in NG on day(8): no fixture item has its main series on youtube, so nothing waits
    duck.load(con, "core.collection_health", [health("board_youtube", day(8), market="NG", platform="youtube",
                                                     valid=False)])
    before = len(rows(con))
    counts = resolve(con)
    got = resolved(con)
    assert got == {ids["up"]: True, ids["flat"]: False, ids["ghost"]: False, ids["stale"]: False,
                   ids["agent"]: True, ids["ngup"]: True}
    assert counts["resolved"] == 6 and len(rows(con)) == before + 6
    # the resolution copies the issue row and sets observed_arrival; the issue row stays as it was
    pair = rows(con, "f.forecast_id = @f", {"f": ids["up"]})
    assert [r["observed_arrival"] for r in pair] == [None, True]
    assert {k: v for k, v in pair[0].items() if k != "observed_arrival"} == \
        {k: v for k, v in pair[1].items() if k != "observed_arrival"}


def test_resolution_waits_until_after_resolve_date(con):
    _, ids = reach_world(con)
    assert resolve(con, d=day(3))["resolved"] == 0                # day(3) is the 7-day resolve_date of I
    assert resolved(con) == {}
    resolve(con)
    got = resolved(con)
    assert ids["late"] not in got and ids["today"] not in got     # windows still open on D


def test_resolution_is_recorded_once_and_the_current_view_shows_it(con):
    _, ids = reach_world(con)
    resolve(con)
    n = len(rows(con))
    assert resolve(con)["resolved"] == 0 and len(rows(con)) == n
    current = {r["forecast_id"]: r for r in duck.query(con, "SELECT * FROM {core}.v_forecasts_current")}
    assert len(current) == len({r["forecast_id"] for r in rows(con)})
    assert current[ids["up"]]["observed_arrival"] is True
    assert current[ids["late"]]["observed_arrival"] is None


def test_a_failed_collection_day_in_the_window_leaves_the_forecast_unresolved(con):
    w, ids = reach_world(con)
    # day(8)'s later collect run: NG's tiktok route invalid, so NG's tiktok forecasts wait and ZA's resolve
    w.collect_again(day(8), {("NG", "feed_tiktok")})
    resolve(con)
    got = resolved(con)
    assert ids["ngup"] not in got and ids["up"] in got


def test_the_items_own_platform_failing_holds_it_even_when_another_platform_was_valid(con):
    w, ids = reach_world(con)
    rup = w.fc("rup", "reach_rising", main="reddit")                # main series on reddit
    w.state("rup", "rising", day(5))
    # day(8) in ZA: the tiktok route failed and the reddit route was valid (the reviewer's probe)
    w.collect_again(day(8), {("ZA", "feed_tiktok")})
    resolve(con)
    got = resolved(con)
    assert ids["up"] not in got and ids["flat"] not in got and ids["stale"] not in got
    assert got[rup] is True and got[ids["ngup"]] is True


def test_one_failed_route_on_the_items_platform_holds_it_even_when_another_route_there_was_valid(con):
    w, ids = reach_world(con)
    # day(8) in ZA: feed_tiktok valid, but a second tiktok route, the hashtag board, failed
    w.collect_again(day(8), extra=[health("board_tiktok_hashtag", day(8), market="ZA", valid=False)])
    resolve(con)
    got = resolved(con)
    assert ids["up"] not in got and ids["flat"] not in got and got[ids["ngup"]] is True


def test_a_failed_collect_run_in_the_window_leaves_every_market_unresolved(con):
    reach_world(con, failed=("collect", day(8)))         # day(8) has no ok collect run
    resolve(con)
    assert resolved(con) == {}


def test_a_day_without_a_good_detect_run_leaves_the_forecast_unresolved(con):
    reach_world(con, failed=("detect", day(9)))          # day(9) has no ok detect run
    resolve(con)
    assert resolved(con) == {}


def test_cross_market_counts_another_market_significant_and_not_held_as_likely_coordinated(con):
    w = ResolveWorld(con)
    ids = {k: w.fc(k, "cross_market") for k in ("xm", "xc", "xg", "xo", "xf")}
    w.test("xm", "KE", day(7))                                     # another market, clear
    w.test("xc", "NG", day(7))
    w.state("xc", "rising", day(7), market="NG", authenticity="likely_coordinated")
    w.test("xg", "GLOBAL", day(7))                                 # GLOBAL is never another market
    w.test("xg", "ZA", day(7))                                     # nor is its own market
    w.test("xo", "NG", day(7), significant=False)
    w.test("xf", "KE", day(6), run_id="stats-bad")                 # only in a failed stats run
    duck.load(con, "agent.runs", [run("stats", day(6), "stats-bad", "failed", hour=14)])
    resolve(con)
    assert resolved(con) == {ids["xm"]: True, ids["xc"]: False, ids["xg"]: False, ids["xo"]: False,
                             ids["xf"]: False}


def test_a_failed_secondary_platform_in_the_forecasts_own_market_holds_every_target(con):
    # case A: state.sql reads every series the item has in the market, so a failed reddit day could have hidden
    # the significant day that made the item Rising
    w = ResolveWorld(con)
    ids = {t: w.fc(f"sec-{t}", t) for t in TARGETS}
    for t in TARGETS:
        for i in range(10, 2, -1):
            w.test(f"sec-{t}", "ZA", day(i), significant=False, platform="reddit")
    solo = w.fc("solo", "reach_rising")                             # tiktok only in ZA
    w.series("sec-persist_50", {i: 2 for i in range(20, -1, -1)})
    w.collect_again(day(8), {("ZA", "list_reddit")})
    resolve(con)
    got = resolved(con)
    assert not set(ids.values()) & set(got)
    assert got[solo] is False


def test_a_platform_the_item_has_no_series_on_in_the_own_market_does_not_hold_it(con):
    w = ResolveWorld(con)
    up = w.fc("up", "reach_rising")
    w.state("up", "rising", day(5))
    w.collect_again(day(8), {("ZA", "list_reddit")})
    resolve(con)
    assert resolved(con) == {up: True}


def apple_music(w, item, target):
    """A forecast whose main series is ZA's Apple Music chart, a platform collected in ZA only."""
    fid = w.fc(item, target, main=None)
    sid = f"{item}|ZA|board_apple_music|p1"
    duck.load(w.con, "core.item_state", [state(item, "ZA", "emerging", d=I, run_id="detect-20260910-am",
                                               main_series_id=sid)])
    duck.load(w.con, "agent.runs", [run("detect", I, "detect-20260910-am", hour=14)])
    duck.load(w.con, "core.series_test", [{"metric_date": I, "series_id": sid, "item_id": item, "market": "ZA",
                                           "platform": "apple_music", "significant": False, "q": 0.5,
                                           "run_id": rid("stats", I)}])
    return fid


def test_cross_market_resolves_when_the_main_platform_is_not_collected_in_the_other_markets(con):
    # case B: board_apple_music is ZA only, so NG and KE have no apple_music health rows to wait for
    w = ResolveWorld(con)
    duck.load(con, "core.collection_health", [health("board_apple_music", day(i), market="ZA",
                                                     platform="apple_music") for i in range(20, -1, -1)])
    cm, rr = apple_music(w, "am", "cross_market"), apple_music(w, "am2", "reach_rising")
    resolve(con)
    assert resolved(con) == {cm: False, rr: False}


def test_cross_market_still_waits_for_a_platform_the_item_has_a_series_on_in_the_other_market(con):
    w = ResolveWorld(con)
    duck.load(con, "core.collection_health", [health("board_apple_music", day(i), market="ZA",
                                                     platform="apple_music") for i in range(20, -1, -1)])
    cm = apple_music(w, "am", "cross_market")
    w.test("am", "KE", day(6), significant=False, platform="reddit")
    w.collect_again(day(8), {("KE", "list_reddit")})
    resolve(con)
    assert cm not in resolved(con)


def test_only_measured_lane_routes_judge_a_platform_day(con):
    # (b): search, placebo and watchlist routes on tiktok failing in ZA on day(8) hold nothing; a failed panel
    # route there does
    w, ids = reach_world(con)
    extra = [health(r, day(8), market="ZA", lane_class=lc, valid=False)
             for r, lc in (("search_tiktok", "search_presence"), ("placebo_tiktok", "search_presence"),
                           ("watch_tiktok", "watchlist"))]
    w.collect_again(day(8), extra=extra)
    resolve(con)
    assert resolved(con)[ids["up"]] is True
    con2 = duck.connect()
    w2, ids2 = reach_world(con2)
    w2.collect_again(day(8), extra=[health("panel_tiktok", day(8), market="ZA", lane_class="panel", valid=False)])
    resolve(con2)
    assert ids2["up"] not in resolved(con2)
    con2.close()


def test_cross_market_waits_while_the_items_platform_failed_in_a_market_it_reads(con):
    w = ResolveWorld(con)
    xa, xm = w.fc("xa", "cross_market"), w.fc("xm", "cross_market")
    reach = w.fc("za", "reach_rising")
    w.test("xm", "KE", day(7))                                     # significant in KE
    w.collect_again(day(8), {("NG", "feed_tiktok")})              # NG's tiktok route failed on day(8)
    resolve(con)
    got = resolved(con)
    assert xa not in got and xm not in got                         # NG was not read on day(8), either way
    assert got[reach] is False                                     # a ZA reach target does not read NG


def test_cross_market_waits_while_any_platform_the_item_was_tested_on_there_failed(con):
    w = ResolveWorld(con)
    xr, xs = w.fc("xr", "cross_market"), w.fc("xs", "cross_market")
    w.test("xr", "NG", day(6), significant=False, platform="reddit")  # xr has a reddit series in NG
    w.collect_again(day(8), {("NG", "list_reddit")})              # NG's reddit route failed on day(8)
    resolve(con)
    got = resolved(con)
    assert xr not in got and got[xs] is False


def test_persist_50_holds_while_v7_of_the_main_series_stays_at_half_its_level_or_more(con):
    w = ResolveWorld(con)
    ids = {k: w.fc(k, "persist_50", persistence=True, issue_state="rising") for k in ("hold", "drop", "dip")}
    ids["none"] = w.fc("none", "persist_50", persistence=True, issue_state="rising", main=None)
    w.series("hold", {i: 2 for i in range(20, 2, -1)})
    # v7 at I is 14; zero from day(8) on takes v7 on day(3) to 0
    w.series("drop", {i: 2 for i in range(20, 8, -1)})
    # v7 dips to 6 on day(6) (three twos in its seven days), under half of 14, and is back at 9 on day(3):
    # it did not stay at half its level
    w.series("dip", {**{i: 2 for i in range(20, 9, -1)}, **{i: 3 for i in (5, 4, 3)}})
    # none has no main series: it cannot be read and stays open
    resolve(con)
    assert resolved(con) == {ids["hold"]: True, ids["drop"]: False, ids["dip"]: False}


def counter_world(con, values, health_days=range(20, -1, -1)):
    """A persist_50 forecast whose main series is a ZA hashtag counter (a tiktok route) with daily deltas values,
    the counter's route valid on health_days."""
    w = ResolveWorld(con)
    fid = w.fc("cnt", "persist_50", persistence=True, issue_state="rising", main=None)
    sid = "cnt|ZA|curve_tiktok_hashtag|p1"
    duck.load(con, "agent.runs", [run("detect", I, "detect-20260910-main", hour=14)])
    duck.load(con, "core.item_state", [state("cnt", "ZA", "rising", d=I, run_id="detect-20260910-main",
                                             main_series_id=sid)])
    duck.load(con, "core.series_test", [{"metric_date": I, "series_id": sid, "item_id": "cnt", "market": "ZA",
                                         "platform": "tiktok", "run_id": rid("stats", I)}])
    duck.load(con, "core.collection_health", [
        health("curve_tiktok_hashtag", day(i), lane_class="unbiased_counter") for i in health_days])
    duck.load(con, "core.item_counter_daily", [
        counter("cnt", "curve_tiktok_hashtag", day(i), v, lane_class="unbiased_counter", unit="delta")
        for i, v in values.items()])
    return fid


def test_persist_50_reads_a_complete_counter_series(con):
    fid = counter_world(con, {i: 2 for i in range(20, -1, -1)})
    resolve(con)
    assert resolved(con) == {fid: True}


def test_a_missing_day_of_the_main_series_leaves_persist_50_unresolved_and_never_lowers_v7(con):
    # no read on day(6), inside the window: the 7-day sum would silently lose a day
    fid = counter_world(con, {i: 2 for i in range(20, -1, -1) if i != 6})
    resolve(con)
    assert fid not in resolved(con)


def test_a_missing_day_before_the_issue_day_leaves_persist_50_unresolved(con):
    # no read on day(13): the issue day's own v7 would be short a day
    fid = counter_world(con, {i: 2 for i in range(20, -1, -1) if i != 13})
    resolve(con)
    assert fid not in resolved(con)


def test_a_null_day_of_the_main_series_leaves_persist_50_unresolved(con):
    # the counter's route has no health row on day(5), so its value that day is NULL
    fid = counter_world(con, {i: 2 for i in range(20, -1, -1)},
                        health_days=[i for i in range(20, -1, -1) if i != 5])
    resolve(con)
    assert fid not in resolved(con)


# (f): weekly scoring


def cohort(n, target="reach_rising", horizon=7, rule="logistic_v1"):
    """n resolved forecasts the rule gets right and persistence gets wrong."""
    return [{"forecast_id": f"{target}{horizon}{i}", "item_id": f"i{i}", "market": "ZA", "target": target,
             "issue_date": day(30), "horizon": horizon, "rule": rule, "prob": 0.8, "predicted_arrival": True,
             "persistence_arrival": False, "resolve_date": day(30) + timedelta(days=horizon),
             "observed_arrival": True} for i in range(n)]


def test_scoring_is_never_promotion_eligible_below_200_comparable_rows():
    small = forecasts.score(cohort(199))
    c = small["cohorts"]["reach_rising/7"]
    assert c["resolved"] == 199 and c["beats_persistence"] is True
    assert c["brier"] == pytest.approx(0.04) and c["persistence_brier"] == pytest.approx(1.0)
    assert c["minimum_comparable_sample"] == 200 and c["comparable_sample_met"] is False
    assert c["promotion_eligible"] is False and small["promotion_eligible"] is False
    big = forecasts.score(cohort(200))
    assert big["cohorts"]["reach_rising/7"]["promotion_eligible"] is True and big["promotion_eligible"] is True


def test_scoring_needs_every_cohort_to_beat_persistence_on_errors_and_on_brier():
    tie = [{**r, "persistence_arrival": True} for r in cohort(300, "persist_50", 14)]
    out = forecasts.score(cohort(300) + tie)
    assert out["cohorts"]["reach_rising/7"]["promotion_eligible"] is True
    assert out["cohorts"]["persist_50/14"]["beats_persistence"] is False
    assert out["cohorts"]["persist_50/14"]["promotion_eligible"] is False
    assert out["promotion_eligible"] is False
    assert forecasts.score([])["promotion_eligible"] is False


def test_scoring_needs_a_brier_score_below_persistences_as_well_as_fewer_errors():
    # 300 arrivals. Persistence misses 100 (error and Brier 1/3). The rule misses 50 at prob 0 and gets 250 right
    # at prob 0.5: fewer errors (1/6) but a worse Brier score (0.375)
    rs = cohort(300)
    rs = [{**r, "persistence_arrival": i >= 100, "prob": 0.0 if i < 50 else 0.5,
           "predicted_arrival": i >= 50} for i, r in enumerate(rs)]
    c = forecasts.score(rs)["cohorts"]["reach_rising/7"]
    assert c["beats_persistence"] is True and c["comparable_sample_met"] is True
    assert c["brier"] == pytest.approx(0.375) and c["persistence_brier"] == pytest.approx(1 / 3)
    assert c["promotion_eligible"] is False


def test_scoring_counts_unresolved_rows_without_scoring_them():
    rs = cohort(10) + [{**r, "forecast_id": r["forecast_id"] + "u", "observed_arrival": None} for r in cohort(5)]
    c = forecasts.score(rs)["cohorts"]["reach_rising/7"]
    assert c["resolved"] == 10 and c["unresolved"] == 5 and c["brier"] == pytest.approx(0.04)


def test_scoring_works_when_ops_imports_are_blocked():
    script = '''
import json
import sys

class BlockOps:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "ops" or fullname.startswith("ops."):
            raise ModuleNotFoundError("ops unavailable")
        return None

sys.meta_path.insert(0, BlockOps())
from core.detect.tests import test_detect_forecasts as t
from core.detect.forecasts import score

out = score(t.cohort(200))
cohort_result = out["cohorts"]["reach_rising/7"]
print(json.dumps({
    "resolved": cohort_result["resolved"],
    "brier": cohort_result["brier"],
    "persistence_brier": cohort_result["persistence_brier"],
}))
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")),
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    scored = json.loads(result.stdout)
    assert scored["resolved"] == 200
    assert scored["brier"] == pytest.approx(0.04)
    assert scored["persistence_brier"] == pytest.approx(1.0)


def test_weekly_score_reads_the_current_engine_forecasts_only(con):
    w = ResolveWorld(con)
    w.fc("a", "reach_rising", prob=0.9)
    w.fc("b", "reach_rising", prob=0.2)
    w.fc("c", "reach_rising", rule="ask_v1")
    w.state("a", "rising", day(5))
    w.state("c", "rising", day(5))
    resolve(con)
    out = forecasts.weekly_score(Client(con), D, core="core", agent="agent")
    c = out["cohorts"]["reach_rising/7"]
    assert c["eligible"] == 2 and c["resolved"] == 2                 # one row per forecast, agent rows left out
    assert c["brier"] == pytest.approx(((0.9 - 1) ** 2 + (0.2 - 0) ** 2) / 2)
    assert out["promotion_eligible"] is False


# The SQL file


def test_forecasts_sql_names_its_statements_and_renders_datasets():
    st = forecasts.statements()
    assert set(st) == {"current_view", "candidates", "issued", "resolve", "append", "scoring"}
    view = sqlrun.render(st["current_view"])
    assert sqlrun.object_name(view) == "intelligence_42_core.v_forecasts_current"
    assert "intelligence_42_agent.forecasts" in view
    assert "agent_views" not in str(forecasts.SQL)


BQ = pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")


@pytest.fixture(scope="module")
def bq():
    from google.cloud import bigquery
    return bigquery.Client(project="ogilvy-trends-v2")


def dry_run(bq, sql, params):
    from google.cloud import bigquery
    config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, query_parameters=params)
    job = bq.query(sql, job_config=config)
    assert job.dry_run
    return job


@BQ
def test_the_forecasts_table_exists_on_staging_with_the_l1_columns(bq):
    table = bq.get_table("ogilvy-trends-v2.intelligence_42_agent.forecasts")
    assert [(f.name, f.field_type) for f in table.schema] == [
        (n, {"INT64": "INTEGER", "FLOAT64": "FLOAT", "BOOL": "BOOLEAN"}.get(t, t)) for n, t in forecasts.FORECAST_FIELDS]
    assert table.time_partitioning.field == "issue_date"


@BQ
@pytest.mark.parametrize("name", ["current_view", "candidates", "issued", "resolve", "scoring"])
def test_bigquery_dry_run(bq, name):
    st = forecasts.statements()
    params = {"d": D, "run_id": RUN, "since": D - timedelta(days=forecasts.LOOKBACK_DAYS), "rule": forecasts.RULE}
    view = duck.split_create(sqlrun.render(st["current_view"]))[3]
    sql = sqlrun.render(st[name])
    if name == "current_view":
        sql = view                        # the body: a dry run never creates the view
    sql = sql.replace("intelligence_42_core.v_forecasts_current", f"({view})")
    dry_run(bq, sql, [sqlrun._param(k, v) for k, v in params.items() if f"@{k}" in sql])


@BQ
def test_bigquery_dry_run_append(bq):
    row = forecasts.issue_rows(D, [{"item_id": "x", "market": "ZA", "state": "rising", "markets_hot": 1,
                                    "sig_days3": 1, "creators3": 5, "worth_pct": 0.5, "main_vel": 0.1}])[0]
    append = sqlrun.render(forecasts.statements()["append"])
    dry_run(bq, append, [forecasts.rows_param([row, {**row, "observed_arrival": True}])])


def _prism_health(con, bad_day):
    # collect writes the culture desk panel's health row (prism/profiles) with no platform
    duck.load(con, "core.collection_health", [health("panel_culture_desk", day(i), market="ZA", platform=None,
              lane_class="panel", valid=(day(i) != bad_day)) for i in range(20, -1, -1)])


def _panel_test(con, item, d, platform):
    duck.load(con, "core.series_test", [{"metric_date": d, "series_id": f"{item}|ZA|panel_culture_desk|p1",
              "item_id": item, "market": "ZA", "platform": platform, "significant": False, "q": 0.5,
              "run_id": rid("stats", d)}])


def test_a_failed_route_with_no_platform_holds_the_forecasts_that_read_its_series():
    con = duck.connect()
    w = ResolveWorld(con)
    sid = "pc|ZA|panel_culture_desk|p1"
    main_fid = w.fc("pc", "reach_rising", main=None)
    duck.load(con, "core.item_state", [state("pc", "ZA", "emerging", d=I, run_id=rid("detect", I), main_series_id=sid)])
    for i in range(10, 2, -1):
        _panel_test(con, "pc", day(i), "tiktok")
    second_fid = w.fc("pc2", "reach_rising")
    for i in range(9, 2, -1):
        _panel_test(con, "pc2", day(i), "tiktok")
    _prism_health(con, day(8))
    resolve(con)
    got = resolved(con)
    assert main_fid not in got and second_fid not in got
