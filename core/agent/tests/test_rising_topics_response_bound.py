from datetime import date, datetime, timezone

import pytest

from core.agent.context import Refused, RunContext, result_hash
from core.agent.tools.sql_query import MAX_BYTES_BILLED
from core.agent.tools.warehouse import rising_topics


class FakeWarehouse:
    def __init__(self, rows, bytes_processed=100):
        self.rows = rows
        self.bytes_processed = bytes_processed
        self.dry_runs = []
        self.runs = []

    def dry_run(self, sql, params):
        self.dry_runs.append((sql, params))
        return {"bytes": self.bytes_processed, "tables": ["intelligence_42_agent.v_items_today"]}

    def run(self, sql, params, max_bytes_billed):
        self.runs.append((sql, params, max_bytes_billed))
        return self.rows


def _ctx():
    return RunContext(
        run_id="r_20261002_topic_bound",
        tier="T1",
        as_of=datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc),
        market="NG",
    )


def _rows(count):
    return [{
        "metric_date": date(2026, 10, 1),
        "market": "NG",
        "item_id": f"sound|synthetic-{i:04d}",
        "label": f"Synthetic topic {i}",
        "kind": "sound",
        "state": "spike",
        "untested": True,
        "worth_pct": float(1000 - i),
        "creators3": 1,
        "posts3": 1,
        "spread_platforms": None,
        "found_platforms": None,
        "markets_hot": 0,
        "lead_market": None,
        "novelty": "new",
        "diffusion": "small_only",
        "authenticity": "not_assessed",
        "share_flags": None,
        "geo_status": "market_unconfirmed",
        "moment": None,
        "run_id": "detect-synthetic",
        "as_of": "2026-10-01T15:24:38+00:00",
    } for i in range(count)]


def test_rising_topics_bounds_response_and_preserves_full_query_result():
    ctx = _ctx()
    rows = _rows(501)
    warehouse = FakeWarehouse(rows)

    out = rising_topics(ctx, warehouse, date="2026-10-01", market="NG", kind="sound", min_platforms=2)

    qid = out["query_id"]
    stored = ctx.queries[qid]
    assert set(out) == {"rows", "truncated", "query_id", "result_hash"}
    assert len(out["rows"]) == 50
    assert out["rows"] == rows[:50]
    assert out["truncated"] is True
    assert stored["rows"] == rows
    assert stored["result_hash"] == result_hash(rows) == out["result_hash"]
    sql, params, max_bytes_billed = warehouse.runs[0]
    assert params == {"date": date(2026, 10, 1), "since": date(2026, 9, 25), "market": "NG",
                     "kind": "sound", "min_platforms": 2}
    assert "t.market = @market" in sql and "t.kind = @kind" in sql
    assert "IFNULL(t.found_platforms, 1) >= @min_platforms" in sql
    assert "LIMIT" not in sql.upper()
    assert max_bytes_billed == MAX_BYTES_BILLED


def test_rising_topics_does_not_mark_fifty_rows_as_truncated():
    ctx = _ctx()
    rows = _rows(50)

    out = rising_topics(ctx, FakeWarehouse(rows), date="2026-10-01")

    assert set(out) == {"rows", "truncated", "query_id", "result_hash"}
    assert out["rows"] == rows
    assert out["truncated"] is False
    assert ctx.queries[out["query_id"]]["rows"] == rows


def test_rising_topics_propagates_upstream_truncation(monkeypatch):
    from core.agent.tools import warehouse as warehouse_tools

    ctx = _ctx()
    rows = _rows(50)
    query_id, digest = ctx.record_query("SELECT item_id FROM intelligence_42_agent.v_items_today", {}, rows,
                                        "rising_topics")
    monkeypatch.setattr(warehouse_tools, "sql_query", lambda *args, **kwargs: {
        "rows": rows, "query_id": query_id, "result_hash": digest, "truncated": True,
    })

    out = warehouse_tools.rising_topics(ctx, FakeWarehouse(rows), date="2026-10-01")

    assert out["truncated"] is True
    assert out["rows"] == rows


def test_rising_topics_keeps_the_query_byte_guard():
    warehouse = FakeWarehouse(_rows(1), bytes_processed=MAX_BYTES_BILLED + 1)

    with pytest.raises(Refused, match="over the .* byte cap"):
        rising_topics(_ctx(), warehouse, date="2026-10-01")

    assert warehouse.dry_runs and not warehouse.runs
