"""W8-DEC-13: a test_switch row is written for a market and platform only where the accepted backtest run shows
placebo false alarms of at most 0.05 and recall at 3 times of at least 0.8, the row cites that run's id, and
every series without a row stays on the untested branch. These tests build backtest result files by hand, so
the rule is read from counts and never from the rates or flags a file states about itself."""

import json
from datetime import date

import pytest

from .. import backtest, stats

RUN = "backtest-20261007-0123456789ab"
AS_OF = "2026-10-07"


def key(observed=20, tested=100, alarms=2, injected=100, detected=85, **extra):
    return {"observed_days": observed, "tested": tested, "alarms": alarms,
            "recall": {"3": {"injected": injected, "detected": detected}}, **extra}


def result(keys, run_id=RUN, **extra):
    out = {"run_id": run_id, "as_of": AS_OF, "rule_version": stats.RULE_VERSION, "keys": keys, **extra}
    if run_id is None:
        del out["run_id"]
    return out


def written(res, done=frozenset()):
    rows, refused = backtest.switch_rows(res, done)
    return rows, refused


# 1. The two thresholds, at their boundaries


@pytest.mark.parametrize("alarms, tested, on", [
    (5, 100, True),         # 0.05 exactly
    (6, 100, False),        # 0.06
    (501, 10000, False),    # 0.0501
    (500, 10000, True),
    (0, 100, True),
])
def test_placebo_false_alarms_of_at_most_0_05_earn_a_row(alarms, tested, on):
    rows, refused = written(result({"ZA|facebook|panel": key(alarms=alarms, tested=tested)}))
    assert [r["market"] for r in rows] == (["ZA"] if on else [])
    assert bool(refused) is not on


@pytest.mark.parametrize("detected, injected, on", [
    (80, 100, True),        # 0.8 exactly
    (799, 1000, False),     # 0.799
    (800, 1000, True),
    (79, 100, False),
    (20, 20, True),
])
def test_recall_at_3_times_of_at_least_0_8_earns_a_row(detected, injected, on):
    rows, _ = written(result({"ZA|facebook|panel": key(detected=detected, injected=injected)}))
    assert len(rows) == int(on)


def test_the_rates_are_recomputed_from_counts_and_never_read_from_the_file():
    lying = key(alarms=40, tested=100, false_alarm_rate=0.01, market_platform_false_alarm_rate=0.01, switch=True,
                reasons=[])
    rows, refused = written(result({"ZA|facebook|panel": lying}))
    assert rows == [] and "false-alarm rate 0.400" in refused[0]["reasons"][0]
    lying = key(detected=30, injected=100, switch=True)
    lying["recall"]["3"]["recall"] = 0.99
    assert written(result({"ZA|facebook|panel": lying}))[0] == []


def test_a_keys_own_rate_is_recomputed_even_when_its_market_and_platform_rate_is_fine():
    keys = {"ZA|facebook|panel": key(alarms=40, tested=100, false_alarm_rate=0.01),
            "ZA|facebook|unbiased_counter": key(alarms=0, tested=1000)}
    rows, refused = written(result(keys))
    assert [r["lane_class"] for r in rows] == ["unbiased_counter"]
    assert refused[0]["key"] == "ZA|facebook|panel" and "false-alarm rate 0.400" in refused[0]["reasons"][0]


def test_a_file_that_says_a_key_stays_off_cannot_be_overridden_by_good_counts():
    rows, refused = written(result({"ZA|facebook|panel": key(switch=False, reasons=["held back by the run"])}))
    assert rows == [] and "run" in refused[0]["reasons"][0]


def test_market_and_platform_false_alarms_are_summed_over_the_lanes_of_the_pair():
    keys = {"ZA|facebook|panel": key(alarms=1, tested=100),
            "ZA|facebook|unbiased_counter": key(alarms=40, tested=100)}
    rows, refused = written(result(keys))
    assert rows == []
    assert {r["key"] for r in refused} == set(keys)
    assert all("market and platform false-alarm rate" in " ".join(r["reasons"]) for r in refused)


@pytest.mark.parametrize("change", [
    {"tested": 59}, {"observed": 13}, {"injected": 19, "detected": 19},
])
def test_the_existing_floors_still_hold(change):
    assert written(result({"ZA|facebook|panel": key(**change)}))[0] == []


# 2. Per market and platform, never across them


def test_one_market_passing_does_not_switch_another():
    keys = {"ZA|facebook|panel": key(), "NG|facebook|panel": key(alarms=30)}
    rows, refused = written(result(keys))
    assert [(r["market"], r["platform"], r["lane_class"]) for r in rows] == [("ZA", "facebook", "panel")]
    assert [r["key"] for r in refused] == ["NG|facebook|panel"]


def test_one_platform_passing_does_not_switch_another_platform_of_the_same_market():
    keys = {"ZA|facebook|panel": key(), "ZA|tiktok|panel": key(detected=10)}
    rows, refused = written(result(keys))
    assert [r["platform"] for r in rows] == ["facebook"]
    assert [r["key"] for r in refused] == ["ZA|tiktok|panel"]


# 3. The row cites the run


def test_the_row_carries_the_run_id_the_day_after_as_of_and_the_rule_version():
    [row], _ = written(result({"ZA|facebook|panel": key()}))
    assert row == {"market": "ZA", "platform": "facebook", "lane_class": "panel", "switched_on": "2026-10-08",
                   "backtest_run_id": RUN, "rule_version": stats.RULE_VERSION}


@pytest.mark.parametrize("run_id", [None, "", "   ", 7, "backtest", "backtest-20261007-xyz",
                                    "backtest-20261006-0123456789ab", "stats-20261007-0123456789ab"])
def test_a_result_without_a_usable_run_id_is_refused_whole(run_id):
    with pytest.raises(ValueError, match="run id"):
        written(result({"ZA|facebook|panel": key()}, run_id=run_id))


def test_a_candidate_rule_replay_never_earns_a_row():
    with pytest.raises(ValueError, match="rule version"):
        written(result({"ZA|facebook|panel": key()}, rule_version=stats.SERIES_RULE_VERSION))


@pytest.mark.parametrize("name", ["ZA|-|panel", "-|facebook|panel", "ZA|facebook", "ZA|facebook|panel|x", "||"])
def test_a_key_test_switch_cannot_hold_earns_nothing(name):
    rows, refused = written(result({name: key()}))
    assert rows == [] and refused[0]["key"] == name


def test_a_key_with_malformed_counts_earns_nothing():
    for bad in ({"alarms": 101}, {"alarms": -1}, {"tested": True}, {"detected": 101}, {"alarms": 1.5}):
        rows, refused = written(result({"ZA|facebook|panel": key(**bad)}))
        assert rows == [] and refused, bad


def test_a_boolean_is_not_a_count():
    rows, refused = written(result({"ZA|facebook|panel": key(alarms=False)}))
    assert rows == [] and refused[0]["reasons"] == ["counts are missing or inconsistent"]


def test_a_key_with_unusable_counts_refuses_the_other_keys_of_its_market_and_platform():
    keys = {"ZA|facebook|panel": key(alarms=500, tested=100), "ZA|facebook|unbiased_counter": key(),
            "ZA|tiktok|panel": key()}
    rows, refused = written(result(keys))
    assert [r["platform"] for r in rows] == ["tiktok"]
    assert {r["key"] for r in refused} == {"ZA|facebook|panel", "ZA|facebook|unbiased_counter"}


def test_a_key_already_switched_on_for_the_rule_version_is_not_written_again():
    done = {("ZA", "facebook", "panel", stats.RULE_VERSION)}
    assert written(result({"ZA|facebook|panel": key()}), done)[0] == []


# 4. Nothing is written without the apply flag, and nothing malformed is written with it


class Client:
    def __init__(self):
        self.inserted = []

    def insert_rows_json(self, table, rows):
        self.inserted.append((table, rows))
        return []


GOOD_ROW = {"market": "ZA", "platform": "facebook", "lane_class": "panel", "switched_on": "2026-10-08",
            "backtest_run_id": RUN, "rule_version": stats.RULE_VERSION}


@pytest.mark.parametrize("apply", [False, None, 0, 1, "yes"])
def test_nothing_is_written_unless_apply_is_exactly_true(apply):
    client = Client()
    with pytest.raises(ValueError, match="apply"):
        backtest.write_switch_rows(client, "core", [GOOD_ROW], apply)
    assert client.inserted == []


def test_apply_true_writes_the_rows_to_test_switch():
    client = Client()
    backtest.write_switch_rows(client, "core", [GOOD_ROW], True)
    assert client.inserted == [("core.test_switch", [GOOD_ROW])]


@pytest.mark.parametrize("field, value", [("backtest_run_id", None), ("backtest_run_id", ""),
                                          ("backtest_run_id", " "), ("market", ""), ("platform", None),
                                          ("lane_class", ""), ("switched_on", None), ("rule_version", "")])
def test_a_row_missing_any_field_is_refused_even_with_apply(field, value):
    client = Client()
    with pytest.raises(ValueError, match=field):
        backtest.write_switch_rows(client, "core", [GOOD_ROW, {**GOOD_ROW, field: value}], True)
    assert client.inserted == []


@pytest.mark.parametrize("field, value", [("backtest_run_id", "backtest"), ("backtest_run_id", "run-1"),
                                          ("backtest_run_id", "backtest-20261007-0123456789abX"),
                                          ("rule_version", stats.SERIES_RULE_VERSION),
                                          ("rule_version", "stats-9")])
def test_a_row_citing_no_backtest_run_or_a_candidate_rule_is_refused_even_with_apply(field, value):
    client = Client()
    with pytest.raises(ValueError, match=field):
        backtest.write_switch_rows(client, "core", [{**GOOD_ROW, field: value}], True)
    assert client.inserted == []


def test_a_row_with_a_field_missing_altogether_is_refused():
    row = {k: v for k, v in GOOD_ROW.items() if k != "backtest_run_id"}
    with pytest.raises(ValueError, match="backtest_run_id"):
        backtest.write_switch_rows(Client(), "core", [row], True)


def test_rows_citing_two_different_runs_are_refused():
    with pytest.raises(ValueError, match="one backtest run"):
        backtest.write_switch_rows(Client(), "core", [GOOD_ROW, {**GOOD_ROW, "platform": "tiktok",
                                                                   "backtest_run_id": "backtest-20261007-aaaaaaaaaaaa"}],
                                   True)


def test_no_rows_means_no_insert_call():
    client = Client()
    backtest.write_switch_rows(client, "core", [], True)
    assert client.inserted == []


# 5. Dry-run report from a result file


def save(tmp_path, res, name=None):
    path = tmp_path / (name or f"{res.get('run_id', 'none')}.json")
    path.write_text(json.dumps(res), encoding="utf-8")
    return path


def test_report_prints_the_rows_that_would_be_written_and_the_keys_that_stay_off(tmp_path, capsys):
    path = save(tmp_path, result({"ZA|facebook|panel": key(), "NG|facebook|panel": key(alarms=30)}))
    assert backtest.main(["--report", str(path)]) == 0
    out = capsys.readouterr().out
    assert f"would write 1 test_switch row from backtest run {RUN}" in out
    assert f"ZA|facebook|panel from 2026-10-08 cites {RUN}" in out
    assert "NG|facebook|panel stays off: false-alarm rate 0.300, over 0.05" in out
    assert "dry run" in out


def test_report_needs_no_client_and_refuses_apply(tmp_path, capsys, monkeypatch):
    def no_client(*a, **k):
        raise AssertionError("report must not build a BigQuery client")

    monkeypatch.setattr(backtest.bigquery, "Client", no_client)
    path = save(tmp_path, result({"ZA|facebook|panel": key()}))
    assert backtest.main(["--report", str(path)]) == 0
    with pytest.raises(SystemExit):
        backtest.main(["--report", str(path), "--apply"])


def test_report_on_a_file_without_a_run_id_writes_nothing_and_fails(tmp_path, capsys):
    path = save(tmp_path, result({"ZA|facebook|panel": key()}, run_id=None), name="backtest-20261007-0123456789ab.json")
    assert backtest.main(["--report", str(path)]) == 1
    captured = capsys.readouterr()
    assert "run id" in captured.err and "would write" not in captured.out


def test_report_refuses_a_file_whose_name_is_not_its_run_id(tmp_path, capsys):
    path = save(tmp_path, result({"ZA|facebook|panel": key()}), name="backtest-20261007-ffffffffffff.json")
    assert backtest.main(["--report", str(path)]) == 1
    assert "run id" in capsys.readouterr().err


def test_report_says_so_when_nothing_is_earned(tmp_path, capsys):
    path = save(tmp_path, result({"ZA|facebook|panel": key(alarms=30)}))
    assert backtest.main(["--report", str(path)]) == 0
    assert "would write 0 test_switch rows" in capsys.readouterr().out


def test_report_reads_a_real_backtest_result_file_of_the_current_shape(tmp_path, capsys):
    from .test_detect_backtest import BacktestClient, D, World, connect, stable_panel
    con = connect()
    try:
        stable_panel(World(), 30).load(con)
        res = backtest.run(BacktestClient(con), D, apply=False, days=7, out_dir=tmp_path, core="core", agent="agent")
    finally:
        con.close()
    assert backtest.main(["--report", str(tmp_path / f"{res['run_id']}.json")]) == 0
    out = capsys.readouterr().out
    assert [r["market"] for r in res["switch_on"]] == ["ZA"]
    assert f"would write 1 test_switch row from backtest run {res['run_id']}" in out


def test_run_without_apply_never_calls_the_writer(monkeypatch, tmp_path):
    from .test_detect_backtest import BacktestClient, D, World, connect, stable_panel
    seen = []
    monkeypatch.setattr(backtest, "write_switch_rows", lambda *a, **k: seen.append(a))
    con = connect()
    try:
        stable_panel(World(), 30).load(con)
        backtest.run(BacktestClient(con), D, apply=False, days=7, out_dir=tmp_path, core="core", agent="agent")
    finally:
        con.close()
    assert seen == []
    assert isinstance(D, date)


def test_run_with_a_truthy_apply_that_is_not_true_writes_nothing(tmp_path):
    from .test_detect_backtest import BacktestClient, D, World, connect, duck, stable_panel
    con = connect()
    try:
        stable_panel(World(), 30).load(con)
        with pytest.raises(ValueError, match="apply"):
            backtest.run(BacktestClient(con), D, apply="yes", days=7, out_dir=tmp_path, core="core", agent="agent")
        assert duck.query(con, "SELECT * FROM {core}.test_switch") == []
        assert duck.query(con, "SELECT * FROM {agent}.runs r WHERE r.stage = 'backtest'") == []
    finally:
        con.close()
