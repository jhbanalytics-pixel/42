"""The early signal as a recorded observation (core/detect/early_signal.py record, hooked into stats.run_stats).

The flag goes to its own table, early_signal. These tests show that with the flag on, everything detect writes
other than that table, the state and rank columns a strategist reads, and the Today payload are byte-identical
to the base commit. The digests below were computed at the base commit 24fc597, before any code of this branch
existed, by running tests/early_identity.py there. They are pinned here, not recomputed from the run under test.
"""

import json
from datetime import timedelta
import logging
from pathlib import Path

import numpy as np
import pytest
from google.cloud import bigquery
from scipy import stats as st

from core.detect import early_signal as es, stats
from core.detect.tests import duck, early_identity as ident
from core.detect.tests.fixtures import D, at, day, run
from core.detect.tests.test_detect_stats import SW_PANEL, StatsClient, panel_world

B0_TABLES = "072ea11c14e4da253514b096cc2ad060a4fa003a3d77e80d559bef5ca13c1fb8"
B0_STATE = "4ee60d59e7d91e47b07c2fc218003db39b79dcb6cc35ddb32c23834d6fada4c5"
# The wave8/api lane changes the Today payload, so the base commit's digest of it (8b5b4670...) no longer holds in a tree
# that has that lane. This is the digest of the same fixture payload at wave8/api 068ee52, which has no early signal:
# the early signal merged on top of it leaves the payload as that lane built it.
API_TODAY = "9f9b44d64a4bfeb205df6c78347b1e19d976529d7f40077451bb36a1097b2ce5"

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def built():
    con = ident.connect()
    rows = ident.build(con)
    yield con, rows
    con.close()


def early(con):
    return {r["series_id"].split("|")[0]: r for r in duck.query(con, "SELECT * FROM {core}.early_signal")}


# Byte identity with the base commit, flag on


def test_every_table_detect_writes_except_early_signal_is_byte_identical_to_the_base_commit(built):
    con, _ = built
    assert ident.digest(ident.dump(con)) == B0_TABLES


def test_state_worth_ranks_and_eligibility_are_byte_identical_to_the_base_commit(built):
    _, rows = built
    blob = json.dumps(ident.state_rows(rows), sort_keys=True, default=ident._plain).encode("utf-8")
    assert ident.digest(blob) == B0_STATE
    assert sum(r["worth_pct"] is not None for r in rows.values()) >= 20       # the rank columns are populated


def test_the_today_payload_is_byte_identical_to_the_one_the_api_lane_built():
    assert ident.today_digest() == API_TODAY


def test_the_identity_run_is_not_vacuous_the_flag_fired_where_no_state_did(built):
    con, rows = built
    flags = early(con)
    tested = duck.query(con, "SELECT COUNT(*) n FROM {core}.series_test WHERE test = 'nb'")[0]["n"]
    assert tested == 66 and len(flags) == tested and all(r["rule_version"] == "early-1" for r in flags.values())
    assert flags["step"]["early"] is True and flags["ramp"]["early"] is True
    assert "step" not in rows and "slow" not in rows        # item_state has no row for either: no state, no rank
    assert rows["ramp"]["state"] == "spike"                  # the ramp is a Spike today and the flag does not change it
    assert sum(r["early"] for k, r in flags.items() if k.startswith("s") and k[1:].isdigit()) <= 2   # stable items


def test_with_the_flag_off_the_other_tables_are_the_same_bytes_as_with_it_on(built, monkeypatch):
    monkeypatch.setattr(es, "record", lambda *a, **k: 0)
    con = ident.connect()
    ident.build(con)
    assert ident.digest(ident.dump(con)) == B0_TABLES
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.early_signal")[0]["n"] == 0
    con.close()


# The recorded values come from the rows detect already wrote, recomputed independently


@pytest.mark.parametrize("item", ["step", "ramp", "s3", "jump"])
def test_the_recorded_cusum_matches_a_recomputation_from_the_raw_counts(built, item):
    con, _ = built
    row = duck.query(con, "SELECT * FROM {core}.series_test WHERE item_id = @i", {"i": item})[0]
    counts = duck.query(con, "SELECT metric_date d, posts FROM {core}.item_daily WHERE item_id = @i "
                             "AND metric_date BETWEEN @a AND @b ORDER BY metric_date",
                        {"i": item, "a": day(13), "b": D})
    assert len(counts) == 14
    n, s = 1 / row["alpha"], 0.0
    for c in counts:
        s = max(0.0, s + st.nbinom.logpmf(c["posts"], n, n / (n + 1.5 * row["mu"]))
                - st.nbinom.logpmf(c["posts"], n, n / (n + row["mu"])))
    got = early(con)[item]
    assert got["cusum"] == pytest.approx(s, abs=1e-9)
    assert got["early"] == (s >= 6.5) and got["y"] == row["y"] and got["mu"] == row["mu"]


def test_the_threshold_and_shape_recorded_are_the_ones_pinned_here(built):
    con, _ = built
    r = next(iter(early(con).values()))
    assert (r["kappa"], r["h"], r["window_days"]) == (1.5, 6.5, 14)
    assert list(es.COLUMNS) == [
        "metric_date", "series_id", "item_id", "market", "platform", "series", "protocol", "lane_class", "kind",
        "y", "mu", "alpha", "cusum", "early", "run_days", "days_used", "kappa", "h", "window_days",
        "baseline_mode", "baseline_source", "baseline_date", "run_id", "rule_version"]


# Nothing reads the table


def test_no_gate_state_card_payload_or_rank_reads_the_table_or_the_module():
    allowed = {"core/detect/early_signal.py", "core/detect/early_signal_backtest.py", "core/detect/stats.py",
               "core/detect/tests/early_identity.py", "core/detect/tests/test_detect_early_signal.py",
               "core/detect/tests/test_detect_early_signal_backtest.py",
               "core/detect/tests/test_detect_early_signal_record.py",
               "core/schema/early_signal.sql", "core/schema/tests/test_early_signal_schema.py"}
    named_apart = "core/schema/apply.py"
    hits = []
    for path in ROOT.rglob("*"):
        parts = path.relative_to(ROOT).parts
        if (not path.is_file() or ".git" in parts or "node_modules" in parts or path.suffix not in
                {".py", ".sql", ".js", ".jsx", ".ts", ".tsx", ".yaml", ".yml", ".json", ".html"}):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        rel = path.relative_to(ROOT).as_posix()
        if ("early_signal" in text or "cusum" in text) and rel not in allowed and rel != named_apart:
            hits.append(rel)
    assert hits == []


def test_the_setup_runner_names_the_table_only_in_its_file_list_and_its_docstring():
    """core/schema/apply.py runs DDL for every file it lists and reads no table, so it may name early_signal.sql in
    SQL_FILES and in the module docstring and nowhere else: a reader added to it would be a reader of the table."""
    src = (ROOT / "core/schema/apply.py").read_text(encoding="utf-8")
    _, docstring, code = src.split('"""', 2)
    assert "early_signal" in docstring
    assert code.lower().count("early_signal") == 1 and 'HERE / "early_signal.sql"' in code
    assert "cusum" not in src.lower()
    in_list = [line for line in code.splitlines() if "early_signal" in line]
    assert len(in_list) == 1 and in_list[0].startswith("SQL_FILES = (")


def test_stats_only_calls_record_after_the_series_test_rows_are_loaded():
    src = (ROOT / "core/detect/stats.py").read_text(encoding="utf-8")
    body = src.split("def run_stats", 1)[1]
    assert body.index("load_table_from_json") < body.index("early_signal.record")
    assert src.count("early_signal") == 2                  # the import and the call, nothing else


# Recording never stops the series test


@pytest.fixture
def con():
    c = duck.connect()
    c.execute("CREATE TABLE core.test_switch (market VARCHAR NOT NULL, platform VARCHAR NOT NULL, "
              "lane_class VARCHAR NOT NULL, switched_on DATE, backtest_run_id VARCHAR, rule_version VARCHAR)")
    c.execute(ident.early_signal_ddl())
    panel_world().load(c)
    duck.load(c, "core.test_switch", [SW_PANEL])
    duck.load(c, "agent.runs", [{**run("backtest", day(3), run_id=SW_PANEL["backtest_run_id"]),
                                 "finished_at": at(day(3))}])
    yield c
    c.close()


def test_run_stats_records_one_row_per_tested_series_and_returns_the_same_count(con):
    assert stats.run_stats(StatsClient(con), D, "stats-x", "r1", core="core", agent="agent") == 3
    got = duck.query(con, "SELECT * FROM {core}.early_signal ORDER BY item_id")
    assert [r["item_id"] for r in got] == ["p0", "p1", "p2"]
    assert {r["run_id"] for r in got} == {"stats-x"} and {r["metric_date"] for r in got} == {D}


def test_a_failing_record_logs_and_leaves_the_series_test_untouched(con, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("load failed")

    monkeypatch.setattr(es, "record", boom)
    with caplog.at_level(logging.ERROR, logger="core.detect.stats"):
        assert stats.run_stats(StatsClient(con), D, "stats-x", "r1", core="core", agent="agent") == 3
    assert len(duck.query(con, "SELECT * FROM {core}.series_test")) == 3
    assert "early signal" in caplog.text and "load failed" in caplog.text


def rows_of_the_day():
    """(tvf_series_signal rows, series_test rows, weekday totals, day) for the three-series panel world."""
    c = duck.connect()
    panel_world().load(c)
    signal = duck.query(c, stats.SIGNAL_SQL, {"d": D})
    totals = duck.query(c, stats.TOTALS_SQL, {"d": D})
    weeks = duck.query(c, stats.FIRST_WEEK_SQL, {"d": D})
    rows = stats.series_test_rows(signal, D, "x", "r1", [SW_PANEL], totals, weeks)
    c.close()
    return signal, rows, totals, D


class Capture:
    """A client that records loads and answers the earlier series_test read with the rows it is given."""
    project = "p"

    def __init__(self, earlier=None):
        self.loads, self.earlier = [], earlier

    def query(self, sql, job_config=None):
        if self.earlier is None:
            raise AssertionError("refit mode must not query")
        rows = self.earlier

        class Job:
            def result(self):
                return rows

        return Job()

    def get_table(self, ref):
        return bigquery.Table("p." + ref, schema=[])

    def load_table_from_json(self, rows, table, job_config=None):
        self.loads.append(rows)
        return duck._Job([])


def test_the_default_mode_is_the_refit_baseline_and_reads_nothing_extra():
    assert es.DEFAULT_MODE == "refit"
    signal, rows, totals, d = rows_of_the_day()
    c = Capture()
    assert es.record(c, d, "r", signal, rows, totals, "core") == 3
    assert {r["baseline_mode"] for r in c.loads[0]} == {"refit"} and {r["h"] for r in c.loads[0]} == {6.5}
    assert {r["baseline_source"] for r in c.loads[0]} == {"refit"} and {r["baseline_date"] for r in c.loads[0]} == {None}


def test_frozen_mode_scores_against_the_earlier_baseline_and_says_which_source_each_row_used():
    signal, rows, totals, d = rows_of_the_day()
    sids = [r["series_id"] for r in rows]
    earlier = [{"series_id": sids[0], "metric_date": d - timedelta(days=14), "mu": 1.0, "alpha": 0.1,
                "weekday_factor": 1.0},
               {"series_id": sids[1], "metric_date": d - timedelta(days=3), "mu": 2.0, "alpha": 0.1,
                "weekday_factor": 1.0}]
    c = Capture(earlier)
    assert es.record(c, d, "r", signal, rows, totals, "core", mode="frozen") == 3
    got = {r["series_id"]: r for r in c.loads[0]}
    assert (got[sids[0]]["baseline_source"], got[sids[0]]["baseline_date"], got[sids[0]]["mu"]) == (
        "frozen", (d - timedelta(days=14)).isoformat(), 1.0)
    assert (got[sids[1]]["baseline_source"], got[sids[1]]["mu"]) == ("earliest_in_window", 2.0)
    assert (got[sids[2]]["baseline_source"], got[sids[2]]["baseline_date"]) == ("refit", None)
    assert {r["baseline_mode"] for r in c.loads[0]} == {"frozen"}
    assert got[sids[0]]["h"] == 9.0 and got[sids[1]]["h"] == 9.0 and got[sids[2]]["h"] == 6.5   # refit fallback keeps its own limit
    assert got[sids[0]]["early"] is True                     # mu 1 against counts of 3 to 6 a day


def test_an_unknown_mode_is_refused():
    signal, rows, totals, d = rows_of_the_day()
    with pytest.raises(ValueError):
        es.record(Capture(), d, "r", signal, rows, totals, "core", mode="stale")


def test_no_tested_series_means_no_early_rows_and_no_second_load():
    from core.detect.tests.test_detect_states import SIGNAL, FakeClient

    client = FakeClient([SIGNAL])
    assert stats.run_stats(client, D, "st-1", "r9") == 1
    assert len(client.loads) == 1
