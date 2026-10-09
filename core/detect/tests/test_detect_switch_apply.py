"""W8-DEC-13: a test_switch row is written for a market and platform only where the accepted backtest run shows
placebo false alarms of at most 0.05 and recall at 3 times of at least 0.8, the row cites that run's id, and
every series without a row stays on the untested branch. These tests build backtest result files by hand, so
the rule is read from counts and never from the rates or flags a file states about itself."""

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

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


# 6. A row is in force only when the backtest run it cites has an ok runs row


def switch_con(*run_rows):
    from .test_detect_backtest import connect, duck
    con = connect()
    duck.load(con, "core.test_switch", [GOOD_ROW])
    if run_rows:
        duck.load(con, "agent.runs", list(run_rows))
    return con


def backtest_run(run_id=RUN, status="ok", stage="backtest"):
    from .fixtures import run
    return run(stage, date(2026, 10, 7), run_id=run_id, status=status)


def forced(con, d=date(2026, 10, 8)):
    from .test_detect_backtest import duck
    return stats.in_force(duck.query(con, stats.SWITCH_SQL, {"d": d}), d)


def test_a_row_whose_backtest_run_has_no_runs_row_is_not_in_force():
    con = switch_con()
    try:
        assert forced(con) == set()
    finally:
        con.close()


@pytest.mark.parametrize("change, on", [
    ({}, True),
    ({"status": "error"}, False),
    ({"status": "running"}, False),
    ({"stage": "stats"}, False),
    ({"run_id": "backtest-20261007-ffffffffffff"}, False),
])
def test_only_an_ok_backtest_runs_row_of_the_cited_run_puts_the_row_in_force(change, on):
    con = switch_con(backtest_run(**change))
    try:
        assert forced(con) == ({("ZA", "facebook", "panel")} if on else set())
    finally:
        con.close()


def test_an_ok_runs_row_does_not_bring_the_row_in_force_before_its_day():
    con = switch_con(backtest_run())
    try:
        assert forced(con, date(2026, 10, 7)) == set()
    finally:
        con.close()


class RunsAppendFails:
    """A BacktestClient whose append to agent.runs raises after the test_switch insert has already landed."""

    def __new__(cls, con):
        from .test_detect_backtest import BacktestClient

        class Client(BacktestClient):
            def insert_rows_json(self, table, rows):
                if table.endswith("runs"):
                    raise RuntimeError("runs append failed")
                return super().insert_rows_json(table, rows)
        return Client(con)


def test_an_apply_whose_runs_append_fails_leaves_no_row_in_force_the_next_day(tmp_path):
    from .test_detect_backtest import D, World, connect, duck, stable_panel
    con = connect()
    try:
        stable_panel(World(), 30).load(con)
        with pytest.raises(RuntimeError, match="runs append failed"):
            backtest.run(RunsAppendFails(con), D, apply=True, days=7, out_dir=tmp_path, core="core", agent="agent")
        [row] = duck.query(con, "SELECT * FROM {core}.test_switch")
        assert row["backtest_run_id"].startswith("backtest-")
        assert duck.query(con, "SELECT * FROM {agent}.runs r WHERE r.stage = 'backtest'") == []
        assert forced(con, D + timedelta(days=1)) == set()
        assert forced(con, D + timedelta(days=30)) == set()
    finally:
        con.close()


def test_an_apply_that_completes_puts_the_row_in_force_from_the_next_day(tmp_path):
    from .test_detect_backtest import BacktestClient, D, World, connect, stable_panel
    con = connect()
    try:
        stable_panel(World(), 30).load(con)
        backtest.run(BacktestClient(con), D, apply=True, days=7, out_dir=tmp_path, core="core", agent="agent")
        assert forced(con, D + timedelta(days=1)) == {("ZA", "facebook", "panel")}
        assert forced(con, D) == set()
    finally:
        con.close()


# 7. A failed apply does not block a later one: a row whose run was never accepted is not "already written"


def test_a_later_apply_writes_the_row_again_when_the_first_one_never_got_its_runs_row(tmp_path):
    from .test_detect_backtest import BacktestClient, D, World, connect, duck, stable_panel
    con = connect()
    try:
        stable_panel(World(), 30).load(con)
        with pytest.raises(RuntimeError, match="runs append failed"):
            backtest.run(RunsAppendFails(con), D, apply=True, days=7, out_dir=tmp_path, core="core", agent="agent")
        again = backtest.run(BacktestClient(con), D, apply=True, days=7, out_dir=tmp_path, core="core", agent="agent")
        assert len(again["switch_on"]) == 1
        cited = {r["backtest_run_id"] for r in duck.query(con, "SELECT * FROM {core}.test_switch")}
        assert again["run_id"] in cited and len(cited) == 2
        assert forced(con, D + timedelta(days=1)) == {("ZA", "facebook", "panel")}
    finally:
        con.close()


def test_an_accepted_row_is_still_not_written_twice(tmp_path):
    from .test_detect_backtest import BacktestClient, D, World, connect, duck, stable_panel
    con = connect()
    try:
        stable_panel(World(), 30).load(con)
        client = BacktestClient(con)
        backtest.run(client, D, apply=True, days=7, out_dir=tmp_path, core="core", agent="agent")
        second = backtest.run(client, D, apply=True, days=7, out_dir=tmp_path, core="core", agent="agent")
        assert second["switch_on"] == []
        assert len(duck.query(con, "SELECT * FROM {core}.test_switch")) == 1
    finally:
        con.close()


# 8. --apply FILE writes what a reviewed result file earns, from its counts, and nothing else


def reviewed_result(keys, **extra):
    return result(keys, thresholds=dict(backtest.THRESHOLDS), applied=False, **extra)


def apply_world():
    from .test_detect_backtest import BacktestClient, connect
    con = connect()
    return con, BacktestClient(con)


TODAY = date(2026, 10, 8)


def digest(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return "0" * 64


def replayed_row(run_id, sha, status="replayed"):
    return {**backtest_run(run_id=run_id, status=status), "counts": json.dumps({"sha256": sha})}


def record_replay(client, path, recorded="file"):
    """The runs row a replay leaves for the file at path (recorded "file": the digest of the file on disk)."""
    from .test_detect_backtest import duck
    try:
        run_id = json.loads(Path(path).read_text(encoding="utf-8"))["run_id"]
    except (OSError, ValueError, KeyError, TypeError):
        return
    if not duck.query(client.con, "SELECT * FROM {agent}.runs r WHERE r.run_id = '%s' AND r.status = 'replayed'" % run_id):
        duck.load(client.con, "agent.runs", [replayed_row(run_id, digest(path) if recorded == "file" else recorded)])


def apply_file(path, client, tmp_path, *more, as_of=AS_OF, sha="file", replayed="file", today=TODAY):
    """backtest --apply FILE as the reviewer runs it: with the digest --report printed (sha "file" is the digest
    of the file on disk) and after a replay that recorded a digest (replayed "file" is the digest of the file on
    disk, as the replay itself records it; None is no replay at all)."""
    if replayed is not None:
        record_replay(client, path, replayed)
    args = ["--as-of", as_of, "--apply", str(path)]
    if sha is not None:
        args += ["--sha256", digest(path) if sha == "file" else sha]
    return backtest.main([*args, *more], client=client, out_dir=tmp_path, core="core", agent="agent", today=today)


def stored(con):
    from .test_detect_backtest import duck
    return (duck.query(con, "SELECT * FROM {core}.test_switch"),
            duck.query(con, "SELECT * FROM {agent}.runs r WHERE r.stage = 'backtest' AND r.status <> 'replayed'"))


def test_the_run_result_states_the_thresholds_it_was_judged_by(tmp_path):
    from .test_detect_backtest import BacktestClient, D, World, connect, stable_panel
    con = connect()
    try:
        stable_panel(World(), 30).load(con)
        res = backtest.run(BacktestClient(con), D, apply=False, days=7, out_dir=tmp_path, core="core", agent="agent")
    finally:
        con.close()
    assert res["thresholds"] == backtest.THRESHOLDS == {
        "false_alarm_max": 0.05, "recall_min": 0.8, "recall_at": "3", "min_observed_days": 14,
        "min_tested": 60, "min_injected": 20}
    saved = json.loads((tmp_path / f"{res['run_id']}.json").read_text(encoding="utf-8"))
    assert saved["thresholds"] == res["thresholds"]


def test_apply_file_writes_the_rows_the_files_counts_earn_and_the_files_run_id(tmp_path, capsys):
    con, client = apply_world()
    try:
        liar = key(alarms=40, tested=100, switch=True, false_alarm_rate=0.01)
        res = reviewed_result({"ZA|facebook|panel": key(), "NG|facebook|panel": liar},
                              switch_on=[{**GOOD_ROW, "market": "NG"}])
        assert apply_file(save(tmp_path, res), client, tmp_path) == 0
        rows, run_rows = stored(con)
        assert rows == [{**GOOD_ROW, "switched_on": date(2026, 10, 8)}]
        [run_row] = run_rows
        assert run_row["run_id"] == RUN and run_row["status"] == "ok" and run_row["run_date"] == date(2026, 10, 7)
        assert forced(con) == {("ZA", "facebook", "panel")}
        out = capsys.readouterr().out
        assert "switched on: ZA|facebook|panel from 2026-10-08" in out
        assert not any("item_counter_daily" in s for s in client.sql)
    finally:
        con.close()


def test_apply_file_recomputes_a_file_whose_flags_say_everything_passed(tmp_path):
    con, client = apply_world()
    try:
        res = reviewed_result({"ZA|facebook|panel": key(alarms=40, tested=100, switch=True, reasons=[])})
        assert apply_file(save(tmp_path, res), client, tmp_path) == 0
        assert stored(con) == ([], [])
    finally:
        con.close()


def refuse(tmp_path, res, *, as_of=AS_OF, name=None, capsys=None):
    con, client = apply_world()
    try:
        assert apply_file(save(tmp_path, res, name), client, tmp_path, as_of=as_of) == 1
        assert stored(con) == ([], [])
        assert client.sql == []
    finally:
        con.close()
    return capsys.readouterr().err if capsys else ""


def test_a_file_as_of_another_day_than_the_one_named_is_refused(tmp_path, capsys):
    err = refuse(tmp_path, reviewed_result({"ZA|facebook|panel": key()}), as_of="2026-10-06", capsys=capsys)
    assert "as of 2026-10-07" in err and "2026-10-06" in err


def test_a_file_whose_as_of_is_not_the_day_of_its_run_id_is_refused(tmp_path, capsys):
    res = reviewed_result({"ZA|facebook|panel": key()})
    res["as_of"] = "2026-10-06"
    err = refuse(tmp_path, res, as_of="2026-10-06", capsys=capsys)
    assert "run id" in err


def test_a_file_without_thresholds_is_refused(tmp_path, capsys):
    res = reviewed_result({"ZA|facebook|panel": key()})
    del res["thresholds"]
    assert "thresholds" in refuse(tmp_path, res, capsys=capsys)


@pytest.mark.parametrize("field, value", [
    ("false_alarm_max", 0.1), ("false_alarm_max", 0.04), ("recall_min", 0.7), ("recall_min", 0.9),
    ("recall_at", "2"), ("min_observed_days", 7), ("min_tested", 30), ("min_injected", 10), ("extra", 1)])
def test_a_file_judged_by_other_thresholds_is_refused(tmp_path, capsys, field, value):
    res = reviewed_result({"ZA|facebook|panel": key()})
    res["thresholds"][field] = value
    assert "thresholds" in refuse(tmp_path, res, capsys=capsys)


def test_a_file_missing_one_threshold_is_refused(tmp_path, capsys):
    res = reviewed_result({"ZA|facebook|panel": key()})
    del res["thresholds"]["min_tested"]
    assert "thresholds" in refuse(tmp_path, res, capsys=capsys)


def test_a_file_whose_name_is_not_its_run_id_is_refused(tmp_path, capsys):
    err = refuse(tmp_path, reviewed_result({"ZA|facebook|panel": key()}), name="backtest-20261007-ffffffffffff.json",
                 capsys=capsys)
    assert "run id" in err


def test_a_candidate_rule_file_is_refused(tmp_path, capsys):
    res = reviewed_result({"ZA|facebook|panel": key()}, rule_version=stats.SERIES_RULE_VERSION)
    assert "rule version" in refuse(tmp_path, res, capsys=capsys)


def test_a_file_that_is_not_json_or_not_an_object_is_refused(tmp_path, capsys):
    con, client = apply_world()
    try:
        for text in ("{", "[]", "7"):
            path = tmp_path / f"{RUN}.json"
            path.write_text(text, encoding="utf-8")
            assert apply_file(path, client, tmp_path) == 1
        assert apply_file(tmp_path / "missing.json", client, tmp_path) == 1
        assert stored(con) == ([], []) and client.sql == []
    finally:
        con.close()
    assert capsys.readouterr().err.count("refused:") == 4


def test_a_refused_file_never_builds_a_client(tmp_path, monkeypatch):
    def no_client(*a, **k):
        raise AssertionError("a refused file must not build a BigQuery client")

    monkeypatch.setattr(backtest.bigquery, "Client", no_client)
    res = reviewed_result({"ZA|facebook|panel": key()})
    res["thresholds"]["recall_min"] = 0.5
    path = save(tmp_path, res)
    assert backtest.main(["--as-of", AS_OF, "--apply", str(path), "--sha256", digest(path)], out_dir=tmp_path,
                         today=TODAY) == 1


def test_a_run_that_already_has_an_ok_runs_row_is_refused_and_writes_nothing_more(tmp_path, capsys):
    con, client = apply_world()
    try:
        path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
        assert apply_file(path, client, tmp_path) == 0
        assert apply_file(path, client, tmp_path) == 1
        rows, run_rows = stored(con)
        assert len(rows) == 1 and len(run_rows) == 1
    finally:
        con.close()
    assert "already" in capsys.readouterr().err


def test_a_file_that_earns_nothing_writes_no_rows_and_no_runs_row(tmp_path, capsys):
    con, client = apply_world()
    try:
        path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key(alarms=30)}))
        assert apply_file(path, client, tmp_path) == 0
        assert stored(con) == ([], [])
    finally:
        con.close()
    assert "nothing new to switch on" in capsys.readouterr().out


def test_a_key_already_accepted_for_the_rule_version_is_not_written_again_from_a_file(tmp_path):
    from .test_detect_backtest import duck
    con, client = apply_world()
    try:
        old = "backtest-20261001-aaaaaaaaaaaa"
        duck.load(con, "core.test_switch", [{**GOOD_ROW, "backtest_run_id": old}])
        duck.load(con, "agent.runs", [backtest_run(run_id=old)])
        assert apply_file(save(tmp_path, reviewed_result({"ZA|facebook|panel": key()})), client, tmp_path) == 0
        rows, _ = stored(con)
        assert [r["backtest_run_id"] for r in rows] == [old]
    finally:
        con.close()


def test_a_file_apply_whose_runs_append_fails_leaves_nothing_in_force_and_can_be_repeated(tmp_path):
    from .test_detect_backtest import BacktestClient, connect
    con = connect()
    try:
        path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
        with pytest.raises(RuntimeError, match="runs append failed"):
            apply_file(path, RunsAppendFails(con), tmp_path)
        assert len(stored(con)[0]) == 1 and forced(con) == set()
        assert apply_file(path, BacktestClient(con), tmp_path) == 0
        assert forced(con) == {("ZA", "facebook", "panel")}
    finally:
        con.close()


def test_apply_file_needs_the_day_the_reviewer_read(tmp_path):
    path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
    with pytest.raises(SystemExit):
        backtest.main(["--apply", str(path), "--sha256", digest(path)], out_dir=tmp_path)


def test_apply_file_takes_no_window_length(tmp_path):
    path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
    with pytest.raises(SystemExit):
        backtest.main(["--as-of", AS_OF, "--days", "7", "--apply", str(path), "--sha256", digest(path)],
                      out_dir=tmp_path)


def test_report_still_refuses_apply_with_or_without_a_file(tmp_path):
    path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
    for apply in (["--apply"], ["--apply", str(path)]):
        with pytest.raises(SystemExit):
            backtest.main(["--report", str(path), *apply])


@pytest.mark.parametrize("status", ["error", "failed", "running"])
def test_a_key_whose_only_row_cites_a_run_with_a_not_ok_runs_row_is_written_again(tmp_path, status):
    from .test_detect_backtest import duck
    con, client = apply_world()
    try:
        old = "backtest-20261001-aaaaaaaaaaaa"
        duck.load(con, "core.test_switch", [{**GOOD_ROW, "backtest_run_id": old}])
        duck.load(con, "agent.runs", [backtest_run(run_id=old, status=status)])
        assert apply_file(save(tmp_path, reviewed_result({"ZA|facebook|panel": key()})), client, tmp_path) == 0
        assert sorted(r["backtest_run_id"] for r in stored(con)[0]) == sorted([old, RUN])
        assert forced(con) == {("ZA", "facebook", "panel")}
    finally:
        con.close()


def test_a_run_whose_runs_row_is_not_ok_does_not_count_as_already_applied(tmp_path):
    from .test_detect_backtest import duck
    con, client = apply_world()
    try:
        duck.load(con, "agent.runs", [backtest_run(status="error")])
        assert apply_file(save(tmp_path, reviewed_result({"ZA|facebook|panel": key()})), client, tmp_path) == 0
        assert len(stored(con)[0]) == 1
    finally:
        con.close()


# 9. --apply FILE writes only the file that was reviewed (S2-1): the digest --report printed, and a replay that ran


def test_report_prints_the_sha256_of_the_file_bytes(tmp_path, capsys):
    path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
    assert backtest.main(["--report", str(path)]) == 0
    assert f"sha256 {hashlib.sha256(path.read_bytes()).hexdigest()}" in capsys.readouterr().out


def test_apply_file_without_a_sha256_is_a_usage_error(tmp_path):
    path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
    with pytest.raises(SystemExit):
        backtest.main(["--as-of", AS_OF, "--apply", str(path)], out_dir=tmp_path, today=TODAY)


def test_a_sha256_without_apply_file_is_a_usage_error(tmp_path):
    path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
    for argv in (["--report", str(path), "--sha256", digest(path)], ["--as-of", AS_OF, "--sha256", digest(path)]):
        with pytest.raises(SystemExit):
            backtest.main(argv, out_dir=tmp_path, today=TODAY)


def test_probe_a_a_file_edited_after_the_report_is_refused_whatever_it_would_earn(tmp_path, capsys):
    """The report says NG stays off; the same file with the alarms edited is not the file that was reviewed."""
    res = reviewed_result({"ZA|facebook|panel": key(), "NG|facebook|panel": key(alarms=30)})
    path = save(tmp_path, res)
    assert backtest.main(["--report", str(path)]) == 0
    printed = capsys.readouterr().out
    reported = printed.split("sha256 ")[1].split()[0]
    assert "NG|facebook|panel stays off" in printed and reported == digest(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["keys"]["NG|facebook|panel"]["alarms"] = 2
    path.write_text(json.dumps(data), encoding="utf-8")
    con, client = apply_world()
    try:
        assert apply_file(path, client, tmp_path, sha=reported) == 1
        assert stored(con) == ([], []) and client.sql == []
    finally:
        con.close()
    assert "sha256" in capsys.readouterr().err


@pytest.mark.parametrize("sha", ["", "abc", "z" * 64, "0" * 64])
def test_a_wrong_or_malformed_sha256_is_refused_before_a_client_is_built(tmp_path, monkeypatch, capsys, sha):
    def no_client(*a, **k):
        raise AssertionError("a refused file must not build a BigQuery client")

    monkeypatch.setattr(backtest.bigquery, "Client", no_client)
    path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
    assert backtest.main(["--as-of", AS_OF, "--apply", str(path), "--sha256", sha], out_dir=tmp_path, today=TODAY) == 1
    assert "sha256" in capsys.readouterr().err


def test_an_upper_case_sha256_of_the_right_file_is_accepted(tmp_path):
    con, client = apply_world()
    try:
        path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
        assert apply_file(path, client, tmp_path, sha=digest(path).upper()) == 0
        assert forced(con) == {("ZA", "facebook", "panel")}
    finally:
        con.close()


def test_probe_a2_a_file_no_replay_produced_is_refused_even_with_its_own_digest(tmp_path, capsys):
    """A hand written file with a correct digest still has no replayed runs row, so it earns no accepted run."""
    res = reviewed_result({"KE|tiktok|panel": key(observed=99, tested=1000, alarms=0, injected=500, detected=500)},
                          run_id="backtest-20261007-abcdefabcdef")
    con, client = apply_world()
    try:
        assert apply_file(save(tmp_path, res), client, tmp_path, replayed=None) == 1
        assert stored(con) == ([], []) and forced(con) == set()
    finally:
        con.close()
    assert "replay" in capsys.readouterr().err


def test_a_replay_that_recorded_another_digest_does_not_vouch_for_the_file(tmp_path, capsys):
    con, client = apply_world()
    try:
        path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
        assert apply_file(path, client, tmp_path, replayed="f" * 64) == 1
        assert stored(con) == ([], [])
    finally:
        con.close()
    assert "replay" in capsys.readouterr().err


@pytest.mark.parametrize("status", ["ok", "error", "running"])
def test_only_a_replayed_runs_row_vouches_for_the_file(tmp_path, status):
    from .test_detect_backtest import duck
    con, client = apply_world()
    try:
        path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
        duck.load(con, "agent.runs", [replayed_row(RUN, digest(path), status=status)])
        assert apply_file(path, client, tmp_path, replayed=None) == 1
        assert stored(con)[0] == []
    finally:
        con.close()


def test_a_replayed_runs_row_of_another_stage_does_not_vouch_for_the_file(tmp_path):
    from .test_detect_backtest import duck
    con, client = apply_world()
    try:
        path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
        duck.load(con, "agent.runs", [{**replayed_row(RUN, digest(path)), "stage": "stats"}])
        assert apply_file(path, client, tmp_path, replayed=None) == 1
        assert stored(con)[0] == []
    finally:
        con.close()


def test_a_replay_without_apply_appends_a_replayed_runs_row_with_the_digest_of_the_file(tmp_path):
    from .test_detect_backtest import BacktestClient, D, World, connect, duck, stable_panel
    con = connect()
    try:
        stable_panel(World(), 30).load(con)
        res = backtest.run(BacktestClient(con), D, apply=False, days=7, out_dir=tmp_path, core="core", agent="agent")
        [row] = duck.query(con, "SELECT * FROM {agent}.runs r WHERE r.stage = 'backtest'")
        assert row["run_id"] == res["run_id"] and row["status"] == "replayed"
        assert json.loads(row["counts"])["sha256"] == digest(tmp_path / f"{res['run_id']}.json")
        assert duck.query(con, "SELECT * FROM {core}.test_switch") == []
    finally:
        con.close()


def test_a_replayed_runs_row_neither_puts_a_row_in_force_nor_counts_as_accepted():
    from .test_detect_backtest import duck
    con = switch_con(replayed_row(RUN, "a" * 64))
    try:
        assert forced(con) == set()
        assert duck.query(con, backtest.ACCEPTED_SQL) == []
    finally:
        con.close()


def test_a_replay_with_apply_appends_the_ok_row_and_no_replayed_row(tmp_path):
    from .test_detect_backtest import BacktestClient, D, World, connect, duck, stable_panel
    con = connect()
    try:
        stable_panel(World(), 30).load(con)
        backtest.run(BacktestClient(con), D, apply=True, days=7, out_dir=tmp_path, core="core", agent="agent")
        got = duck.query(con, "SELECT * FROM {agent}.runs r WHERE r.stage = 'backtest'")
        assert [r["status"] for r in got] == ["ok"]
    finally:
        con.close()


def test_replay_then_report_then_apply_file_with_the_reported_digest_writes_the_rows(tmp_path, capsys):
    from .test_detect_backtest import BacktestClient, D, World, connect, stable_panel
    con = connect()
    try:
        stable_panel(World(), 30).load(con)
        client = BacktestClient(con)
        res = backtest.run(client, D, apply=False, days=7, out_dir=tmp_path, core="core", agent="agent")
        path = tmp_path / f"{res['run_id']}.json"
        assert backtest.main(["--report", str(path)]) == 0
        reported = capsys.readouterr().out.split("sha256 ")[1].split()[0]
        argv = ["--as-of", D.isoformat(), "--apply", str(path), "--sha256", reported]
        assert backtest.main(argv, client=client, out_dir=tmp_path, core="core", agent="agent", today=D) == 0
        assert forced(con, D + timedelta(days=1)) == {("ZA", "facebook", "panel")}
    finally:
        con.close()


# 10. A row that cites the run but is not about to be written blocks the apply (S2-2)


def test_probe_b_a_stray_row_citing_the_run_that_the_file_does_not_earn_blocks_the_apply(tmp_path, capsys):
    from .test_detect_backtest import duck
    con, client = apply_world()
    try:
        duck.load(con, "core.test_switch", [{**GOOD_ROW, "market": "NG"}])
        path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key(), "NG|facebook|panel": key(alarms=30)}))
        assert apply_file(path, client, tmp_path) == 1
        rows, run_rows = stored(con)
        assert [r["market"] for r in rows] == ["NG"] and run_rows == [] and forced(con) == set()
    finally:
        con.close()
    assert "NG|facebook|panel" in capsys.readouterr().err


def test_a_row_citing_the_run_for_a_key_the_file_writes_again_does_not_block_the_repeat(tmp_path):
    from .test_detect_backtest import duck
    con, client = apply_world()
    try:
        duck.load(con, "core.test_switch", [GOOD_ROW])
        path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
        assert apply_file(path, client, tmp_path) == 0
        assert forced(con) == {("ZA", "facebook", "panel")}
    finally:
        con.close()


def test_a_row_of_another_run_for_a_key_the_file_does_not_earn_does_not_block(tmp_path):
    from .test_detect_backtest import duck
    con, client = apply_world()
    try:
        other = "backtest-20261001-aaaaaaaaaaaa"
        duck.load(con, "core.test_switch", [{**GOOD_ROW, "market": "NG", "backtest_run_id": other}])
        path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key(), "NG|facebook|panel": key(alarms=30)}))
        assert apply_file(path, client, tmp_path) == 0
    finally:
        con.close()


# 11. A file that is too old is refused (S2-3, lead ruling: at most 3 days before today)


def aged(as_of):
    run_id = f"backtest-{as_of.replace('-', '')}-0123456789ab"
    return reviewed_result({"ZA|facebook|panel": key()}, run_id=run_id, as_of=as_of)


@pytest.mark.parametrize("as_of, ok", [("2026-10-08", True), ("2026-10-05", True), ("2026-10-04", False),
                                       ("2026-08-08", False)])
def test_a_file_more_than_three_days_before_today_is_refused(tmp_path, capsys, as_of, ok):
    con, client = apply_world()
    try:
        assert apply_file(save(tmp_path, aged(as_of)), client, tmp_path, as_of=as_of) == (0 if ok else 1)
        assert bool(stored(con)[0]) is ok
        assert ok or client.sql == []
    finally:
        con.close()
    assert ok or "days old" in capsys.readouterr().err


def test_the_age_is_read_from_the_clock_given_not_the_real_one(tmp_path):
    path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
    for today, code in ((date(2026, 10, 10), 0), (date(2026, 10, 11), 1)):
        con, client = apply_world()
        try:
            assert apply_file(path, client, tmp_path, today=today) == code
        finally:
            con.close()


# 12. Two near-equivalent mutants pinned (S2-6)


def test_a_non_backtest_ok_runs_row_with_the_run_id_is_not_an_accepted_run(tmp_path):
    from .test_detect_backtest import duck
    con, client = apply_world()
    try:
        path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
        duck.load(con, "agent.runs", [{**backtest_run(), "stage": "stats"}])
        assert duck.query(con, backtest.ACCEPTED_SQL) == []
        assert apply_file(path, client, tmp_path) == 0
        assert forced(con) == {("ZA", "facebook", "panel")}
    finally:
        con.close()


def test_a_key_accepted_for_the_candidate_rule_does_not_block_the_current_rule(tmp_path):
    from .test_detect_backtest import duck
    con, client = apply_world()
    try:
        old = "backtest-20261001-aaaaaaaaaaaa"
        duck.load(con, "core.test_switch", [{**GOOD_ROW, "backtest_run_id": old,
                                             "rule_version": stats.SERIES_RULE_VERSION}])
        duck.load(con, "agent.runs", [backtest_run(run_id=old)])
        assert apply_file(save(tmp_path, reviewed_result({"ZA|facebook|panel": key()})), client, tmp_path) == 0
        assert sorted(r["rule_version"] for r in stored(con)[0]) == sorted(
            [stats.SERIES_RULE_VERSION, stats.RULE_VERSION])
    finally:
        con.close()


def test_a_replayed_runs_row_of_another_run_does_not_vouch_for_the_file(tmp_path, capsys):
    """The digest is the file's own, but the replay that recorded it was of a different run id."""
    from .test_detect_backtest import duck
    con, client = apply_world()
    try:
        path = save(tmp_path, reviewed_result({"ZA|facebook|panel": key()}))
        duck.load(con, "agent.runs", [replayed_row("backtest-20261001-aaaaaaaaaaaa", digest(path))])
        assert apply_file(path, client, tmp_path, replayed=None) == 1
        assert stored(con) == ([], [])
    finally:
        con.close()
    assert "replay" in capsys.readouterr().err
