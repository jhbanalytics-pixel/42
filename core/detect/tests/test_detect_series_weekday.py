import json
from datetime import timedelta

import numpy as np
import pytest

from core.detect import backtest, stats
from core.detect.tests import duck
from core.detect.tests.fixtures import D, day
from core.detect.tests.test_detect_backtest import BacktestClient
from core.detect.tests.test_detect_states import World
from core.detect.tests.test_detect_stats import StatsClient

CANDIDATE = "stats-2-series"
KEY = "ZA|facebook|panel"


def desk_world(case="desk", first=56, items=1):
    world = World()
    world.protocol("panel_culture_desk", first, platform=None, lane_class="panel", k=1.0)
    rng = np.random.default_rng(17)
    for item in range(items):
        for i in range(first, -1, -1):
            mean = 12 if day(i).weekday() == D.weekday() else 4
            value = int(rng.negative_binomial(12, 12 / (12 + mean))) if items > 1 else 5
            world.daily(f"desk-{item}", i, value // 2, platform="facebook", series="panel_culture_desk")
            world.daily(f"desk-{item}", i, value - value // 2, platform="tiktok", series="panel_culture_desk")
    if case != "desk":
        world.protocol("panel_culture_desk" if case == "protocol" else "unrelated_panel", first,
                       platform="facebook" if case == "named" else None, lane_class="panel", k=1.0,
                       protocol="p2" if case == "protocol" else "p1")
    for h in world.health.values():
        own = h["series"] == "panel_culture_desk" and h["protocol"] == "p1"
        h["items"] = ((300 if h["day"].weekday() == D.weekday() else 100) if own else
                      ((50 if h["day"].weekday() == D.weekday() else 200) if h["platform"] else
                       (100 if h["day"].weekday() == D.weekday() else 300)))
    return world


def database(world):
    con = duck.connect()
    con.execute("SET threads = 1")
    con.execute("CREATE TABLE core.test_switch (market VARCHAR, platform VARCHAR, lane_class VARCHAR, "
                "switched_on DATE, backtest_run_id VARCHAR, rule_version VARCHAR)")
    world.load(con)
    return con


def switched(version="stats-1", when=None):
    return {"market": "ZA", "platform": "facebook", "lane_class": "panel", "switched_on": when or day(56),
            "backtest_run_id": "accepted-fixture", "rule_version": version}


def live_rows(con, switches):
    duck.load(con, "core.test_switch", switches)
    stats.run_stats(StatsClient(con), D, "fixture", "daily-v1", core="core")
    return duck.query(con, "SELECT * FROM {core}.series_test ORDER BY item_id")


@pytest.mark.parametrize("case", ["desk", "named", "null", "protocol"])
def test_candidate_uses_own_series_and_protocol_for_all_route_isolation_cases(case):
    con = database(desk_world(case))
    [row] = live_rows(con, [switched(CANDIDATE)])
    assert row["weekday_factor"] == pytest.approx(17 / 9)
    assert row["rule_version"] == CANDIDATE
    assert row["platform"] == "facebook" and row["test"] == "nb"
    con.close()


@pytest.mark.parametrize("version", ["stats-1", None, "unrecognised-rule"])
@pytest.mark.parametrize("case, expected", [("desk", 1.0), ("named", 13 / 25), ("null", 1.0)])
def test_existing_missing_and_unknown_versions_keep_current_factors(version, case, expected):
    con = database(desk_world(case))
    [row] = live_rows(con, [switched(version)])
    assert row["weekday_factor"] == pytest.approx(expected)
    assert row["rule_version"] == "stats-1"
    con.close()


def test_future_candidate_does_not_change_an_existing_switch():
    con = database(desk_world())
    [row] = live_rows(con, [switched(), switched(CANDIDATE, D + timedelta(days=1))])
    assert row["weekday_factor"] == 1.0 and row["rule_version"] == "stats-1"
    con.close()


@pytest.mark.parametrize("days, expected", [(10, None), (21, 1.0), (28, 5 / 3), (56, 17 / 9)])
def test_candidate_preserves_warmup_and_minimum_weekday_observations(days, expected):
    con = database(desk_world(first=days))
    [row] = live_rows(con, [switched(CANDIDATE)])
    assert row["weekday_factor"] == pytest.approx(expected) if expected is not None else row["weekday_factor"] is None
    assert row["test"] == ("none" if expected is None else "nb")
    con.close()


@pytest.mark.parametrize("case", ["desk", "named", "null", "protocol"])
def test_sql_and_replay_totals_and_candidate_results_match(case):
    con = database(desk_world(case))
    inputs = backtest.load(BacktestClient(con), D, 7, core="core", agent="agent")
    store = backtest.Store(inputs)
    live_totals = duck.query(con, stats.TOTALS_SQL, {"d": D})

    def totals(rows):
        return {(r["market"], r["platform"], r["lane_class"], r.get("series"), r.get("protocol"), r["day"]): r["total"]
                for r in rows}

    assert totals(store.totals(D)) == totals(live_totals)
    want = stats.series_test_rows(duck.query(con, stats.SIGNAL_SQL, {"d": D}), D, "parity", "daily-v1",
                                  [switched(CANDIDATE)], live_totals)
    got = stats.series_test_rows(store.signal(D), D, "parity", "daily-v1", [switched(CANDIDATE)], store.totals(D))
    shared = ("series_id", "item_id", "market", "platform", "series", "protocol", "lane_class", "kind", "y",
              "trials", "obs_prior", "obs28", "first_measured", "baseline_state", "test", "mu", "alpha",
              "weekday_factor", "mu_prior", "ratio", "p_mid", "q", "significant", "rule_version")
    assert [{k: r[k] for k in shared} for r in got] == [{k: r[k] for k in shared} for r in want]
    assert got[0]["weekday_factor"] == pytest.approx(17 / 9)
    con.close()


def test_candidate_keeps_closed_window_and_own_route_validity():
    world = desk_world(first=70)
    for h in world.health.values():
        if h["day"] == D or h["day"] < day(56):
            h["items"] = 10 ** 9
        if h["day"] == day(7):
            h["valid"] = False
    con = database(world)
    [row] = live_rows(con, [switched(CANDIDATE)])
    assert row["weekday_factor"] == pytest.approx(477 / 253)
    con.close()


def test_candidate_does_not_change_a_named_platform_panel():
    world = desk_world("named")
    for i in range(56, -1, -1):
        world.daily("named", i, 4, platform="facebook", series="unrelated_panel")
    con = database(world)
    rows = {r["item_id"]: r for r in live_rows(con, [switched(CANDIDATE)])}
    assert rows["desk-0"]["weekday_factor"] == pytest.approx(17 / 9)
    assert rows["named"]["weekday_factor"] == pytest.approx(13 / 25)
    assert rows["named"]["rule_version"] == "stats-1"
    con.close()


def test_offline_candidate_replay_isolates_routes_and_tests_injected_spikes(monkeypatch, tmp_path):
    real = stats.series_test_rows
    observed = []

    def capture(*args, **kwargs):
        rows = real(*args, **kwargs)
        observed.extend(rows)
        return rows

    monkeypatch.setattr(stats, "series_test_rows", capture)
    results, captured = [], {}
    for case in ("desk", "named", "null"):
        con = database(desk_world(case, items=12))
        inputs = backtest.load(BacktestClient(con), D, 7, core="core", agent="agent")
        result = backtest.replay(inputs, D, 7, rule_version=CANDIDATE)
        captured[case] = result
        results.append(result["keys"][KEY])
        assert result["rule_version"] == CANDIDATE
        con.close()
    assert results[0] == results[1] == results[2]
    assert results[0]["tested"] == 84
    assert results[0]["recall"]["3"]["injected"] >= 20
    assert results[0]["recall"]["5"]["detected"] > 0
    today = [r for r in observed if r["metric_date"] == D and r["test"] == "nb"]
    assert today and all(r["weekday_factor"] == pytest.approx(17 / 9) for r in today)
    assert all(r["rule_version"] == CANDIDATE for r in today)
    (tmp_path / "candidate-replays.json").write_text(json.dumps(captured, indent=2), encoding="utf-8")


def test_default_replay_and_native_cli_keep_current_rule(tmp_path):
    con = database(desk_world())
    inputs = backtest.load(BacktestClient(con), D, 1, core="core", agent="agent")
    assert backtest.replay(inputs, D, 1)["rule_version"] == "stats-1"
    assert backtest.main(["--as-of", str(D), "--days", "1"], client=BacktestClient(con), out_dir=tmp_path,
                         core="core", agent="agent") == 0
    assert duck.query(con, "SELECT * FROM {core}.test_switch") == []
    [saved] = list(tmp_path.glob("*.json"))
    assert json.loads(saved.read_text(encoding="utf-8"))["rule_version"] == "stats-1"
    con.close()
