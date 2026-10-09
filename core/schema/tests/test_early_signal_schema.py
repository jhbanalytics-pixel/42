"""The early_signal table is defined in core/schema as an additive file, like google_search_signals.sql, and
only the setup runner creates it. The detect module never issues DDL."""

import re
from pathlib import Path

import pytest
from google.api_core.exceptions import NotFound

from core.detect import early_signal as es, stats
from core.schema import apply

SCHEMA_DIR = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[3]
PATH = SCHEMA_DIR / "early_signal.sql"
NAME = "ogilvy-trends-v2.intelligence_42_core.early_signal"
BQ_TYPES = {"metric_date": "DATE", "series_id": "STRING", "item_id": "STRING", "market": "STRING",
            "platform": "STRING", "series": "STRING", "protocol": "STRING", "lane_class": "STRING",
            "kind": "STRING", "y": "FLOAT64", "mu": "FLOAT64", "alpha": "FLOAT64", "cusum": "FLOAT64",
            "early": "BOOL", "run_days": "INT64", "days_used": "INT64", "kappa": "FLOAT64", "h": "FLOAT64",
            "window_days": "INT64", "baseline_mode": "STRING", "baseline_source": "STRING",
            "baseline_date": "DATE", "run_id": "STRING", "rule_version": "STRING"}


def statement():
    [s] = apply.load_statements([PATH])
    return s


def test_the_definition_is_one_plain_additive_table_the_setup_runner_parses():
    s = statement()
    assert apply.kind(s) == "table" and apply.statement_name(s) == NAME
    assert s.startswith("CREATE TABLE IF NOT EXISTS `" + NAME + "`")
    for pattern in (r"\bDROP\b", r"\bTRUNCATE\b", r"\bDELETE\b", r"\bREPLACE\b", r"expir", r"\bOPTIONS\b"):
        assert not re.search(pattern, s, re.I), pattern


def test_the_columns_are_the_ones_the_module_writes_with_their_types():
    body = statement().split("(", 1)[1].rsplit(")", 1)[0]
    cols = re.findall(r"(\w+)\s+(STRING|DATE|FLOAT64|BOOL|INT64)", body)
    assert [c for c, _ in cols] == list(es.COLUMNS)
    assert dict(cols) == BQ_TYPES
    assert "PARTITION BY metric_date" in statement()
    assert "metric_date DATE NOT NULL" in statement() and "series_id STRING NOT NULL" in statement()


def test_it_is_a_new_file_not_an_edit_of_core_sql_and_the_schema_text_rules_cover_it():
    assert "early_signal" not in (SCHEMA_DIR / "core.sql").read_text(encoding="utf-8")
    assert PATH in list(SCHEMA_DIR.rglob("*.sql"))          # the banned text and dash scan globs every .sql here
    text = PATH.read_text(encoding="utf-8")
    assert chr(0x2014) not in text and chr(0x2013) not in text


DDL_TOKENS = ("create_table", "bigquery.Table(", "SchemaField", "TimePartitioning", "CREATE TABLE", "ALTER TABLE",
              "DROP TABLE", "CREATE VIEW", "get_dataset", "create_dataset", "delete_table")


@pytest.mark.parametrize("path", ["core/detect/early_signal.py", "core/detect/stats.py"])
def test_the_detect_module_contains_no_ddl(path):
    text = (REPO / path).read_text(encoding="utf-8")
    assert [t for t in DDL_TOKENS if t in text] == []


def test_record_never_creates_a_missing_table_it_raises_and_the_hook_logs(caplog):
    class Client:
        project = "p"
        created = []
        queries = []

        def get_table(self, ref):
            raise NotFound("no early_signal")

        def create_table(self, *a, **k):
            self.created.append(a)

        def query(self, sql, **k):
            self.queries.append(sql)

    from core.detect.tests.test_detect_early_signal_record import rows_of_the_day

    client = Client()
    signal, rows, totals, d = rows_of_the_day()
    with pytest.raises(NotFound):
        es.record(client, d, "r", signal, rows, totals, "core")
    assert client.created == [] and client.queries == []
