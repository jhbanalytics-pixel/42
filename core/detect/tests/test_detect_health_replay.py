from core.detect import backtest
from core.detect.tests import duck
from core.detect.tests.fixtures import D, day
from core.detect.tests.test_detect_backtest import BacktestClient, connect
from core.detect.tests.test_detect_states import World


def test_replay_retrospective_zero_yield_matches_live_missing_days():
    con = connect()
    world = World()
    world.protocol("panel", 30, platform="facebook", lane_class="panel", k=1.0)
    for i in range(30, -1, -1):
        world.daily("topic", i, 4, series="panel")
    yesterday = world.health[("panel", "ZA", "p1", day(1))]
    yesterday.update(items=0)
    today = world.health[("panel", "ZA", "p1", D)]
    today.update(items=0, valid=False, invalid_reason="zero_yield")
    world.load(con)
    inputs = backtest.load(BacktestClient(con), D, 1, core="core", agent="agent")
    store = backtest.Store(inputs)
    live = {r["day"]: r["value"] for r in duck.query(con, "SELECT * FROM {core}.v_series_daily")}
    [series] = store.series(D).values()
    assert live[day(1)] is None
    assert series["values"][day(1)][0] is None
    [past] = store.series(day(1)).values()
    assert past["values"][day(1)][0] == 4.0
    con.close()
