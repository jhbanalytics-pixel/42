"""The early signal as a recorded observation (core/detect/early_signal.py record, hooked into stats.run_stats).

The flag goes to its own table, early_signal. These tests show that with the flag on, everything detect writes
other than that table, the state and rank columns a strategist reads, and the Today payload are byte-identical
to the base commit. The digests below were computed at the base commit 24fc597, before any code of this branch
existed, by running tests/early_identity.py there. They are pinned here, not recomputed from the run under test.
"""

import json
import logging
from pathlib import Path

import numpy as np
import pytest
from google.api_core.exceptions import NotFound
from google.cloud import bigquery
from scipy import stats as st

from core.detect import early_signal as es, stats
from core.detect.tests import duck, early_identity as ident
from core.detect.tests.fixtures import D, day
from core.detect.tests.test_detect_stats import SW_PANEL, StatsClient, panel_world

B0_TABLES = "072ea11c14e4da253514b096cc2ad060a4fa003a3d77e80d559bef5ca13c1fb8"
B0_STATE = "4ee60d59e7d91e47b07c2fc218003db39b79dcb6cc35ddb32c23834d6fada4c5"
B0_TODAY = "8b5b46701ef7e8c6cf3b663febd7487dc5aa0b4b02d9f8e75321bf86e33edd16"

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


def test_the_today_payload_is_byte_identical_to_the_base_commit():
    assert ident.today_digest() == B0_TODAY


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
    assert [f.name for f in es.SCHEMA] == [
        "metric_date", "series_id", "item_id", "market", "platform", "series", "protocol", "lane_class", "kind",
        "y", "mu", "alpha", "cusum", "early", "run_days", "days_used", "kappa", "h", "window_days", "run_id",
        "rule_version"]


# Nothing reads the table


def test_no_gate_state_card_payload_or_rank_reads_the_table_or_the_module():
    allowed = {"core/detect/early_signal.py", "core/detect/early_signal_backtest.py", "core/detect/stats.py",
               "core/detect/tests/early_identity.py", "core/detect/tests/test_detect_early_signal.py",
               "core/detect/tests/test_detect_early_signal_backtest.py",
               "core/detect/tests/test_detect_early_signal_record.py"}
    hits = []
    for path in ROOT.rglob("*"):
        parts = path.relative_to(ROOT).parts
        if (not path.is_file() or ".git" in parts or "node_modules" in parts or path.suffix not in
                {".py", ".sql", ".js", ".jsx", ".ts", ".tsx", ".yaml", ".yml", ".json", ".html"}):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if ("early_signal" in text or "cusum" in text) and path.relative_to(ROOT).as_posix() not in allowed:
            hits.append(path.relative_to(ROOT).as_posix())
    assert hits == []


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
    yield c
    c.close()


def test_run_stats_records_one_row_per_tested_series_and_returns_the_same_count(con):
    assert stats.run_stats(StatsClient(con), D, "stats-x", "r1", core="core") == 3
    got = duck.query(con, "SELECT * FROM {core}.early_signal ORDER BY item_id")
    assert [r["item_id"] for r in got] == ["p0", "p1", "p2"]
    assert {r["run_id"] for r in got} == {"stats-x"} and {r["metric_date"] for r in got} == {D}


def test_a_failing_record_logs_and_leaves_the_series_test_untouched(con, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("load failed")

    monkeypatch.setattr(es, "record", boom)
    with caplog.at_level(logging.ERROR, logger="core.detect.stats"):
        assert stats.run_stats(StatsClient(con), D, "stats-x", "r1", core="core") == 3
    assert len(duck.query(con, "SELECT * FROM {core}.series_test")) == 3
    assert "early signal" in caplog.text and "load failed" in caplog.text


def test_a_missing_table_is_created_with_the_schema_and_day_partitioning(con):
    class NoTable(StatsClient):
        project = "p"
        created = []

        def get_table(self, ref):
            if ref.endswith(".early_signal") and not self.created:
                raise NotFound("no table")
            return super().get_table(ref)

        def create_table(self, table, exists_ok=False):
            self.created.append(table)
            return bigquery.Table("p." + table.dataset_id + "." + table.table_id, schema=[])

    client = NoTable(con)
    assert stats.run_stats(client, D, "stats-x", "r1", core="core") == 3
    [table] = client.created
    assert table.table_id == "early_signal" and [f.name for f in table.schema] == [f.name for f in es.SCHEMA]
    assert table.time_partitioning.field == "metric_date"
    assert len(duck.query(con, "SELECT * FROM {core}.early_signal")) == 3


def test_no_tested_series_means_no_early_rows_and_no_second_load():
    from core.detect.tests.test_detect_states import SIGNAL, FakeClient

    client = FakeClient([SIGNAL])
    assert stats.run_stats(client, D, "st-1", "r9") == 1
    assert len(client.loads) == 1
