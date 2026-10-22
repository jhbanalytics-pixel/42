"""Audit F7D-COL-002: a collect run that failed after its counters were appended kept its pull numbers, and a same-day
retry (served from the raw cache) took the next number, so detect's current view, which reads ok collect runs only,
saw pulls 1 and 3 and lost the top-10-twice signal. last_pulls now counts collect runs that finished ok, and rows of
any other stage's run as before. The real LAST_PULLS_SQL runs on DuckDB."""

from datetime import date

import duckdb
import sqlglot

from core.collect import writers

DAY = date(2026, 10, 21)
SERIES = "feed_tiktok"


def last_pulls(counters, runs):
    con = duckdb.connect()
    con.execute("CREATE TABLE counters (obs_date DATE, market VARCHAR, series VARCHAR, protocol VARCHAR, "
                "route VARCHAR, pull_seq BIGINT, run_id VARCHAR)")
    con.execute("CREATE TABLE observations (observed_date DATE, market VARCHAR, series VARCHAR, protocol VARCHAR, "
                "route VARCHAR, lane_class VARCHAR, pull_seq BIGINT, run_id VARCHAR)")
    con.execute("CREATE TABLE runs (run_id VARCHAR, stage VARCHAR, status VARCHAR)")
    for pull, run_id in counters:
        con.execute("INSERT INTO counters VALUES (?, 'ZA', ?, 'p', 'tiktok/trending', ?, ?)", [DAY, SERIES, pull, run_id])
    for row in runs:
        con.execute("INSERT INTO runs VALUES (?, ?, ?)", list(row))
    sql = writers.LAST_PULLS_SQL.format(counters="counters", observations="observations", runs="runs")
    for name, value in (("@day", f"DATE '{DAY.isoformat()}'"), ("@series", f"['{SERIES}']"),
                        ("@public_feed_counter_series", "['none']"), ("@observation_series", "['none']"),
                        ("@public_feed_observation_series", "['none']")):
        sql = sql.replace(name, value)
    rows = con.execute(sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]).fetchall()
    return {(m, s, p): n for m, s, p, n in rows}


def test_a_failed_collect_runs_pulls_do_not_advance_the_next_pull():
    runs = [("c_ok", "collect", "running"), ("c_ok", "collect", "ok"),
            ("c_failed", "collect", "running"), ("c_failed", "collect", "failed")]
    assert last_pulls([(1, "c_ok"), (2, "c_failed")], runs) == {("ZA", SERIES, "p"): 1}


def test_a_collect_run_that_never_finished_does_not_hold_its_pull_either():
    runs = [("c_ok", "collect", "ok"), ("c_dead", "collect", "running")]
    assert last_pulls([(1, "c_ok"), (2, "c_dead")], runs) == {("ZA", SERIES, "p"): 1}


def test_ok_collect_runs_and_other_stages_count_as_before():
    runs = [("c_ok", "collect", "ok"), ("c_ok2", "collect", "ok"), ("pulse_1", "pulse", "failed")]
    assert last_pulls([(1, "c_ok"), (2, "c_ok2"), (3, "pulse_1"), (4, None)], runs) == {("ZA", SERIES, "p"): 4}
    assert last_pulls([(1, "c_ok"), (2, "c_ok2")], runs) == {("ZA", SERIES, "p"): 2}
