"""A DuckDB stand-in for the staging warehouse, for the weekly quality and forecast scoring tests. The SQL under
core/eval/sql is BigQuery; execute transpiles it with sqlglot and runs it on in-memory fixture tables named as on
staging. Column lists follow core/schema/agent.sql on origin/full-42-l1."""

import json

import pytest
import sqlglot

PROJECT_DB = '"ogilvy-trends-v2"'
AGENT = f"{PROJECT_DB}.intelligence_42_agent"

TABLES = {
    "forecasts": "forecast_id VARCHAR, item_id VARCHAR, market VARCHAR, target VARCHAR, issue_date DATE, "
                 "horizon BIGINT, rule VARCHAR, prob DOUBLE, predicted_arrival BOOLEAN, "
                 "persistence_arrival BOOLEAN, resolve_date DATE, observed_arrival BOOLEAN",
    "feedback": 'who VARCHAR, what VARCHAR, reason VARCHAR, "at" TIMESTAMP',
    "engine_scorecard": "week_start DATE, week_end DATE, market VARCHAR, run_id VARCHAR, rule_version VARCHAR, "
                        "time_to_detect JSON, lead_time JSON, precision JSON, recall JSON, breadth_platforms JSON, "
                        "expansion_cluster_share JSON, expansion_platform_share JSON, "
                        "expansion_language_share JSON, cost_per_confirmed JSON",
}


class Warehouse:
    def __init__(self, tables):
        import duckdb  # test-only

        self.con = duckdb.connect()
        self.con.execute(f"ATTACH ':memory:' AS {PROJECT_DB}")
        self.con.execute(f"CREATE SCHEMA {AGENT}")
        for name in tables:
            self.con.execute(f"CREATE TABLE {AGENT}.{name} ({TABLES[name]})")
        self.statements = []

    def insert(self, table, rows):
        for row in rows:
            cols = ", ".join(f'"{c}"' for c in row)
            marks = ", ".join("?" for _ in row)
            values = [json.dumps(v) if isinstance(v, dict) else v for v in row.values()]
            self.con.execute(f"INSERT INTO {AGENT}.{table} ({cols}) VALUES ({marks})", values)

    def count(self, table):
        return self.con.execute(f"SELECT COUNT(*) FROM {AGENT}.{table}").fetchone()[0]

    def rows(self, table):
        cur = self.con.execute(f"SELECT * FROM {AGENT}.{table}")
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def exists(self, table):
        return bool(self.con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_catalog = 'ogilvy-trends-v2' "
            "AND table_schema = 'intelligence_42_agent' AND table_name = ?", [table]).fetchone()[0])

    def execute(self, sql, params, max_bytes=None):
        self.statements.append(sql)
        result = None
        for statement in sqlglot.parse(sql, read="bigquery"):
            duck = statement.sql("duckdb")
            result = self.con.execute(duck, {k: v for k, v in params.items() if f"${k}" in duck})
        if result is None or result.description is None:
            return {"rows": []}
        names = [d[0] for d in result.description]
        return {"rows": [dict(zip(names, r)) for r in result.fetchall()]}


@pytest.fixture
def warehouse():
    return lambda *tables: Warehouse(tables or tuple(TABLES))
