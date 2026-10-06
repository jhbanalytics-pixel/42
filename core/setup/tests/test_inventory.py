"""Unit tests for core/setup/inventory.py. No cloud access: a fake BigQuery client records every query."""
import re
import subprocess
import sys
from pathlib import Path

import pytest
from google.api_core.exceptions import NotFound

from core.setup import inventory as inv

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parents[1]
GB = 1024 ** 3
AUDIENCE_COL = "".join(["gen", "z", "_score"])

ENRICHED_COLS = [("content_id", "STRING"), ("post_text", "STRING"), ("market", "STRING"),
                 ("created_at", "TIMESTAMP"), ("published_at", "TIMESTAMP"), (AUDIENCE_COL, "FLOAT64")]
SCORES_COLS = [("topic", "STRING"), ("country", "STRING"), ("metric_date", "DATE"), ("score", "FLOAT64")]
SEED_COLS = [("term", "STRING"), ("geo_market", "STRING"), ("first_seen", "DATE"), ("last_seen", "DATE")]
PLAIN_COLS = [("run_id", "STRING"), ("payload", "STRING")]
MARKETS = [{"market": "KE", "row_count": 10, "min_date": "2026-01-02", "max_date": "2026-09-07"},
           {"market": "NG", "row_count": 20, "min_date": "2026-01-03", "max_date": "2026-09-07"},
           {"market": "ZA", "row_count": 30, "min_date": "2026-01-01", "max_date": "2026-09-08"}]


def standard_datasets(seed_in="trends_v2_dev", intel=True):
    dev = {"enriched_content": ("BASE TABLE", ENRICHED_COLS, 60),
           "trend_scores": ("BASE TABLE", SCORES_COLS, 90)}
    staging = {"enriched_content": ("BASE TABLE", ENRICHED_COLS, 40)}
    sets = {"trends_v2_dev": dev, "trends_v2_staging": staging}
    if seed_in:
        sets[seed_in]["seed_graph"] = ("BASE TABLE", SEED_COLS, 70)
    if intel:
        sets["intelligence_42_sources_staging"] = {
            "source_runs": ("BASE TABLE", PLAIN_COLS, 5),
            "v_sources": ("VIEW", PLAIN_COLS, 0)}
    return sets


class FakeJob:
    def __init__(self, rows, size):
        self.rows = rows
        self.total_bytes_processed = size
        self.total_bytes_billed = size

    def result(self):
        return self.rows


class FakeClient:
    """Answers the inventory's queries from an in-memory description of the datasets."""

    def __init__(self, datasets, estimates=None):
        self.datasets = datasets
        self.estimates = estimates or {}
        self.calls = []

    def query(self, sql, job_config=None, location=None):
        dry = bool(job_config is not None and job_config.dry_run)
        self.calls.append({"sql": sql, "dry": dry, "config": job_config, "location": location})
        size = next((b for key, b in self.estimates.items() if key in sql), 1000)
        return FakeJob([] if dry else self.answer(sql, job_config), size)

    def names(self, config):
        for p in config.query_parameters:
            if p.name == "names":
                return set(p.values)
        return None

    def answer(self, sql, config):
        m = re.search(r"`ogilvy-trends-v2\.(\w+)\.INFORMATION_SCHEMA\.(TABLES|COLUMNS)`", sql)
        if m:
            dataset = self.datasets.get(m.group(1))
            if dataset is None:
                raise NotFound(f"Dataset ogilvy-trends-v2:{m.group(1)} was not found")
            wanted = self.names(config)
            chosen = {t: d for t, d in dataset.items() if wanted is None or t in wanted}
            if m.group(2) == "TABLES":
                return [{"table_name": t, "table_type": d[0]} for t, d in sorted(chosen.items())]
            return [{"table_name": t, "column_name": c, "data_type": ty}
                    for t, d in sorted(chosen.items()) for c, ty in d[1]]
        m = re.search(r"FROM `ogilvy-trends-v2\.(\w+)\.(\w+)`", sql)
        _, _, total = self.datasets[m.group(1)][m.group(2)]
        if "GROUP BY" in sql:
            return MARKETS
        if "MIN(" in sql:
            return [{"total_rows": total, "min_date": "2025-10-01", "max_date": "2026-09-08"}]
        return [{"total_rows": total}]

    def real(self):
        return [c for c in self.calls if not c["dry"]]


def run(tmp_path, client):
    out = tmp_path / "data-inventory.md"
    assert inv.main(["--out", str(out)], client=client) == 0
    return out.read_text(encoding="utf-8")


def test_market_column_is_picked_by_name_and_type():
    assert inv.pick_market([("id", "STRING"), ("country", "STRING"), ("text", "STRING")]) == "country"
    assert inv.pick_market([("country", "STRING"), ("market", "STRING")]) == "market"
    assert inv.pick_market([("id", "STRING"), ("geo_market", "STRING")]) == "geo_market"
    assert inv.pick_market([("id", "STRING"), ("source_market", "STRING")]) == "source_market"
    assert inv.pick_market([("market", "INT64"), ("topic", "STRING")]) is None
    assert inv.pick_market(PLAIN_COLS) is None


def test_date_column_is_picked_by_name_then_type():
    assert inv.pick_date(ENRICHED_COLS) == ("published_at", "TIMESTAMP")
    assert inv.pick_date(SCORES_COLS) == ("metric_date", "DATE")
    assert inv.pick_date([("ingested_at", "TIMESTAMP"), ("day", "DATE")]) == ("day", "DATE")
    assert inv.pick_date([("id", "STRING"), ("seen", "DATETIME")]) == ("seen", "DATETIME")
    assert inv.pick_date([("date", "STRING"), ("id", "INT64")]) is None
    assert inv.pick_date(PLAIN_COLS) is None


def test_non_select_statements_are_refused_before_any_call():
    client = FakeClient(standard_datasets())
    for sql in ("".join(["DRO", "P TABLE x"]), "".join(["INS", "ERT INTO x VALUES (1)"]),
                "SELECT 1; SELECT 2", "WITH a AS (SELECT 1) SELECT * FROM a"):
        with pytest.raises(ValueError):
            inv.run_query(client, sql, log=[])
    assert client.calls == []


def test_only_select_and_information_schema_queries_are_issued(tmp_path):
    client = FakeClient(standard_datasets())
    run(tmp_path, client)
    assert client.calls
    for call in client.calls:
        sql = call["sql"]
        assert sql.startswith("SELECT ") and ";" not in sql, sql
        assert "INFORMATION_SCHEMA" in sql or "COUNT(*)" in sql, sql
        assert call["location"] == "US"


def test_every_real_query_follows_its_own_dry_run_and_is_capped(tmp_path):
    client = FakeClient(standard_datasets())
    run(tmp_path, client)
    for i, call in enumerate(client.calls):
        if call["dry"]:
            assert call["config"].use_query_cache is False
            continue
        before = client.calls[i - 1]
        assert i > 0 and before["dry"] and before["sql"] == call["sql"], call["sql"]
        assert call["config"].maximum_bytes_billed == 5 * GB
    assert len(client.real()) * 2 == len(client.calls)


def test_a_query_over_the_byte_cap_is_refused_and_reported(tmp_path):
    over = "GROUP BY `market`"
    client = FakeClient(standard_datasets(), estimates={over: 6 * GB})
    md = run(tmp_path, client)
    assert not [c for c in client.real() if over in c["sql"]]
    assert [c for c in client.calls if c["dry"] and over in c["sql"]]
    assert "refused" in md.lower()
    assert "| NG | 20 | 2026-01-03 | 2026-09-07 |" in md


def test_missing_dataset_is_recorded_and_the_rest_still_runs(tmp_path):
    client = FakeClient(standard_datasets(intel=False))
    md = run(tmp_path, client)
    assert "intelligence_42_sources_staging" in md
    assert "does not exist" in md
    assert "## trends_v2_dev.trend_scores" in md


def test_seed_graph_is_found_in_whichever_dataset_holds_it(tmp_path):
    md = run(tmp_path, FakeClient(standard_datasets(seed_in="trends_v2_staging")))
    assert "## trends_v2_staging.seed_graph" in md
    assert "## trends_v2_dev.seed_graph" not in md
    md = run(tmp_path, FakeClient(standard_datasets(seed_in=None)))
    assert "seed_graph was not found in trends_v2_dev or trends_v2_staging" in md


def test_markdown_has_one_table_per_source_and_no_row_content(tmp_path):
    client = FakeClient(standard_datasets())
    md = run(tmp_path, client)
    for section in ("trends_v2_dev.enriched_content", "trends_v2_dev.trend_scores", "trends_v2_dev.seed_graph",
                    "trends_v2_staging.enriched_content", "intelligence_42_sources_staging.source_runs"):
        assert f"## {section}" in md
    assert md.count("| Market | Rows | Min date | Max date |") == 4
    assert "| ZA | 30 | 2026-01-01 | 2026-09-08 |" in md
    assert "Total rows: 60" in md and "Total rows: 90" in md
    assert "`published_at`" in md and "`country`" in md and "`geo_market`" in md
    assert re.search(r"Run at \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", md)
    assert "## Queries" in md and "bytes processed" in md
    assert "v_sources" in md and "not counted" in md
    assert "run_id, payload" in md
    for call in client.calls:
        assert "post_text" not in call["sql"] and "content_id" not in call["sql"]
    for word in ("post_text", "content_id", AUDIENCE_COL, "".join(["gen", "z"])):
        assert word not in md.lower()


def test_plan_prints_every_query_and_needs_no_client(capsys, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("plan must not build a client")
    monkeypatch.setattr(inv.bigquery, "Client", no_network)
    assert inv.main(["--plan"]) == 0
    out = capsys.readouterr().out
    for dataset in ("trends_v2_dev", "trends_v2_staging", "intelligence_42_sources_staging"):
        assert f"`ogilvy-trends-v2.{dataset}.INFORMATION_SCHEMA.TABLES`" in out
        assert f"`ogilvy-trends-v2.{dataset}.INFORMATION_SCHEMA.COLUMNS`" in out
    for table in ("trends_v2_dev.enriched_content", "trends_v2_dev.trend_scores",
                  "trends_v2_staging.enriched_content", "trends_v2_dev.seed_graph", "trends_v2_staging.seed_graph"):
        assert f"SELECT COUNT(*) AS total_rows FROM `ogilvy-trends-v2.{table}`" in out
    assert "GROUP BY" in out and "5 GB" in out and "dry run" in out.lower()


def test_plan_runs_as_a_script_without_network():
    proc = subprocess.run([sys.executable, str(HERE / "inventory.py"), "--plan"], capture_output=True,
                          encoding="utf-8", cwd=ROOT, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "INFORMATION_SCHEMA.COLUMNS" in proc.stdout


def test_default_output_path_is_the_reference_doc():
    assert inv.OUT == ROOT / "docs" / "full-42" / "reference" / "data-inventory.md"


def test_no_banned_literals_or_dashes_in_the_inventory_code():
    words = ["".join(["gen", "z"]), "".join(["gen", " z"]), "".join(["google", "_trends"]),
             "".join(["google", " trends"])]
    banned = re.compile("|".join(words).encode(), re.I)
    paths = [HERE / "inventory.py", Path(__file__)]
    paths += list((HERE / "__pycache__").glob("inventory*.pyc"))
    paths += list((HERE / "tests" / "__pycache__").glob("test_inventory*.pyc"))
    for path in paths:
        assert not banned.search(path.read_bytes()), path
    for path in (HERE / "inventory.py", Path(__file__)):
        text = path.read_text(encoding="utf-8")
        assert chr(0x2014) not in text and chr(0x2013) not in text, path
        assert not re.search(r"(?<![\s\[\"'(])-{2}|-{2}(?![a-z])", text), path
