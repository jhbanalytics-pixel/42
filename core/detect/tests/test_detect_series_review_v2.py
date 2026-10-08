import json
import pytest

from core.detect import backtest, stats
from core.detect.tests import duck
from core.detect.tests.fixtures import D, at, day, run
from core.detect.tests.test_detect_backtest import BacktestClient
from core.detect.tests.test_detect_series_weekday import CANDIDATE, database, desk_world, switched


@pytest.mark.parametrize("versions", [(CANDIDATE,), ("stats-1", CANDIDATE), (None,),
                                      ("unknown-rule",), (None, "unknown-rule", CANDIDATE)])
@pytest.mark.parametrize("requested", [None, CANDIDATE])
def test_requested_replay_version_matches_effective_weekday_model(monkeypatch, versions, requested):
    con = database(desk_world())
    inputs = backtest.load(BacktestClient(con), D, 1, core="core", agent="agent")
    inputs["switched"].extend(switched(version) for version in versions)
    inputs["written"].append({"run_id": "accepted-fixture", "finished_at": at(day(57))})
    observed = []
    real = stats.series_test_rows

    def capture(*args, **kwargs):
        rows = real(*args, **kwargs)
        observed.extend(r for r in rows if r["test"] == "nb")
        return rows

    monkeypatch.setattr(stats, "series_test_rows", capture)
    result = backtest.replay(inputs, D, 1, **({"rule_version": requested} if requested else {}))
    expected = requested or "stats-1"
    factor = 17 / 9 if requested else 1.0
    assert result["rule_version"] == expected
    assert observed and all(r["rule_version"] == expected for r in observed)
    assert all(r["weekday_factor"] == pytest.approx(factor) for r in observed)
    assert result["keys"]["ZA|facebook|panel"]["tested"] == 1
    con.close()


def test_default_replay_keeps_mixed_force_keys_eligible_without_using_candidate_factors(monkeypatch):
    world = desk_world("null")
    for i in range(56, -1, -1):
        world.daily("other", i, 4, platform="instagram", series="unrelated_panel")
    con = database(world)
    inputs = backtest.load(BacktestClient(con), D, 1, core="core", agent="agent")
    inputs["switched"].extend([switched(CANDIDATE), {**switched(None), "platform": "instagram"},
                                {**switched("unknown-rule"), "platform": "instagram"}])
    inputs["written"].append({"run_id": "accepted-fixture", "finished_at": at(day(57))})
    seen = []
    real = stats.series_test_rows

    def capture(*args, **kwargs):
        rows = real(*args, **kwargs)
        seen.extend(r for r in rows if r["test"] == "nb")
        return rows

    monkeypatch.setattr(stats, "series_test_rows", capture)
    result = backtest.replay(inputs, D, 1)
    assert set(result["keys"]) == {"ZA|facebook|panel", "ZA|instagram|panel"}
    assert all(k["tested"] == 1 for k in result["keys"].values())
    assert seen and all(r["rule_version"] == result["rule_version"] == "stats-1" for r in seen)
    assert all(r["weekday_factor"] == 1.0 for r in seen)
    con.close()


def test_default_cli_with_retained_candidate_switch_uses_and_reports_legacy(monkeypatch, tmp_path):
    con = database(desk_world())
    duck.load(con, "core.test_switch", [switched(CANDIDATE)])
    duck.load(con, "agent.runs", [{**run("backtest", day(57), run_id="accepted-fixture"),
                                    "finished_at": at(day(57))}])
    real, seen = stats.series_test_rows, []

    def capture(*args, **kwargs):
        rows = real(*args, **kwargs)
        seen.extend(r for r in rows if r["test"] == "nb")
        return rows

    monkeypatch.setattr(stats, "series_test_rows", capture)
    assert backtest.main(["--as-of", str(D), "--days", "1"], client=BacktestClient(con), out_dir=tmp_path,
                         core="core", agent="agent") == 0
    [path] = list(tmp_path.glob("*.json"))
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["rule_version"] == "stats-1" and report["applied"] is False
    assert seen and all(r["rule_version"] == "stats-1" and r["weekday_factor"] == 1.0 for r in seen)
    assert len(duck.query(con, "SELECT * FROM {core}.test_switch")) == 1
    con.close()
