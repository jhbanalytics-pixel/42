import pytest

from core.detect import backtest
from core.detect.tests import duck
from core.detect.tests.fixtures import D, at, day, run
from core.detect.tests.test_detect_backtest import BacktestClient, connect
from core.detect.tests.test_detect_states import World


@pytest.mark.parametrize("first_platform", ["facebook", "tiktok"])
def test_shared_panel_replay_preserves_live_identity_and_sums_every_platform(first_platform):
    con = connect()
    con.execute("SET threads = 1")
    world = World()
    world.protocol("panel_culture_desk", 30, platform=None, lane_class="panel", k=1.0)
    for i in range(30, -1, -1):
        world.daily("desk", i, 2, platform=first_platform, series="panel_culture_desk")
        if i < 30:
            world.daily("desk", i, 3, platform="instagram", series="panel_culture_desk")
    world.load(con)
    [live] = duck.query(con, "SELECT * FROM {core}.tvf_series_signal(@d)", {"d": D})
    inputs = backtest.load(BacktestClient(con), D, 7, core="core", agent="agent")
    [replayed] = backtest.Store(inputs).signal(D)
    assert live["y"] == 5.0
    assert replayed["y"] == live["y"]
    assert replayed["platform"] == live["platform"] == first_platform
    assert replayed["series_id"] == live["series_id"] == "desk|ZA|panel_culture_desk|p1"
    assert replayed["hist"] == live["hist"]
    con.close()


def test_later_available_platform_cannot_change_past_panel_identity_or_values():
    con = connect()
    con.execute("SET threads = 1")
    world = World()
    world.protocol("panel_culture_desk", 30, platform=None, lane_class="panel", k=1.0)
    for i in range(30, -1, -1):
        world.daily("desk", i, 2, platform="tiktok", series="panel_culture_desk")
    world.daily("desk", 30, 3, platform="instagram", series="panel_culture_desk", run_id="late-panel")
    world.load(con)
    duck.load(con, "agent.runs", [{**run("aggregate", day(30), run_id="late-panel"),
                                    "started_at": at(D, 10), "finished_at": at(D, 11)}])
    inputs = backtest.load(BacktestClient(con), D, 7, core="core", agent="agent")
    store = backtest.Store(inputs)
    [past] = store.signal(day(5))
    assert past["platform"] == "tiktok" and past["y"] == 2.0
    [live] = duck.query(con, "SELECT * FROM {core}.tvf_series_signal(@d)", {"d": D})
    [current] = store.signal(D)
    assert current["platform"] == live["platform"] == "instagram"
    assert current["y"] == live["y"] == 2.0
    con.close()


@pytest.mark.parametrize("first", [30, 140])
def test_replaced_first_day_metadata_cannot_choose_a_superseded_platform(first):
    con = connect()
    con.execute("SET threads = 1")
    world = World()
    world.protocol("panel_culture_desk", first, platform=None, lane_class="panel", k=1.0)
    for i in range(first, -1, -1):
        world.daily("desk", i, 2, platform="facebook", series="panel_culture_desk")
    world.daily("desk", first, 3, platform="tiktok", series="panel_culture_desk", run_id="replacement-first-day")
    world.load(con)
    duck.load(con, "agent.runs", [{**run("aggregate", day(first), run_id="replacement-first-day"),
                                    "started_at": at(D, 10), "finished_at": at(D, 11)}])
    inputs = backtest.load(BacktestClient(con), D, 7, core="core", agent="agent")
    store = backtest.Store(inputs)
    [past] = store.signal(day(5))
    assert past["platform"] == "facebook" and past["y"] == 2.0
    [live] = duck.query(con, "SELECT * FROM {core}.tvf_series_signal(@d)", {"d": D})
    [current] = store.signal(D)
    assert live["platform"] == "tiktok"
    assert current["platform"] == live["platform"]
    assert current["y"] == live["y"] == 2.0
    assert current["series_id"] == live["series_id"] == "desk|ZA|panel_culture_desk|p1"
    con.close()
