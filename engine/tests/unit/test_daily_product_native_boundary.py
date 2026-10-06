"""The native history I/O factory the daily products run over, and compose isolation."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import daily_native_clients as native
from src.analysis.open_intelligence import daily_product_boundary as boundary
from src.analysis.open_intelligence.daily_dynamic_meter import DynamicMeter
from src.analysis.open_intelligence.daily_product_io import ProductBudget
from src.analysis.open_intelligence.daily_products import REQUIRED_SOURCE_COLUMNS
from src.analysis.open_intelligence.production_snapshot_tables import (
    snapshot_destination_table,
)

CUTOFF = datetime(2026, 9, 20, tzinfo=UTC)
DAY = CUTOFF.date().replace(day=19)
ENRICHED = snapshot_destination_table(DAY, "enriched_content")


def _row(**overrides):
    row = {column: None for column in REQUIRED_SOURCE_COLUMNS if column != "engagement_weighted"}
    row.update(
        id="za:1",
        market="za",
        platform="tiktok",
        source="socialcrawl",
        content_type="video",
        engagement_total=1000,
        published_at=datetime(2026, 9, 19, 10, tzinfo=UTC),
        collected_at=datetime(2026, 9, 19, 12, tzinfo=UTC),
    )
    row.update(overrides)
    return row


class Warehouse:
    project, location = "ogilvy-trends-v2", "US"

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.queries, self.loads = [], []

    def query(self, sql, **kwargs):
        self.queries.append((sql, kwargs))
        rows = self.rows
        return SimpleNamespace(
            result=lambda **options: iter(rows),
            errors=None,
            total_bytes_billed=10,
        )

    def load_table_from_json(self, rows, table, **kwargs):
        self.loads.append((list(rows), table, kwargs))
        return SimpleNamespace(
            result=lambda **options: None, errors=None, output_rows=len(list(rows))
        )


def _meter(warehouse, calls=1):
    meter = DynamicMeter(warehouse)
    meter.bind(
        ProductBudget(
            {
                "max_model_calls": calls,
                "max_bytes_billed": 1000,
                "max_rows_written": 100,
                "max_credits": 0,
            }
        )
    )
    return meter


def _admission(calls=1):
    return SimpleNamespace(
        manifest=SimpleNamespace(
            project="ogilvy-trends-v2",
            datasets=("trends_v2_staging",),
            limits={"max_model_calls": calls},
        )
    )


def test_factory_reads_the_captures_own_enriched_snapshot_through_the_bound_meter():
    warehouse = Warehouse([_row()])
    meter = _meter(warehouse)
    sdk_calls = []

    def sdk(**kwargs):
        sdk_calls.append(kwargs)
        return SimpleNamespace(models="vertex-models")

    factory = boundary.native_product_io_factory(warehouse=warehouse, meter=meter, model_sdk=sdk)
    built = factory(
        admission=_admission(),
        capture={"snapshot_tables": (ENRICHED, "other")},
        cutoff=CUTOFF,
    )
    ((sql, kwargs),) = warehouse.queries
    assert f"`ogilvy-trends-v2.trends_v2_staging.{ENRICHED}`" in sql
    assert kwargs["retry"] is None
    assert kwargs["job_retry"] is None
    assert meter._budget.query_count == 1
    assert meter._budget.total_bytes_billed == 10
    assert list(built.enriched["id"]) == ["za:1"]
    assert built.enriched["engagement_weighted"].iloc[0] > 0
    assert built.models == "vertex-models"
    assert sdk_calls == [{"project": "ogilvy-trends-v2", "location": "global"}]
    assert built.dataset == "trends_v2_staging"


def test_engagement_is_damped_at_the_cutoff_so_a_retry_computes_the_same_rows():
    first = boundary.captured_source_rows(
        _meter(Warehouse([_row()])), capture={"snapshot_tables": (ENRICHED,)}, cutoff=CUTOFF
    )
    again = boundary.captured_source_rows(
        _meter(Warehouse([_row()])), capture={"snapshot_tables": (ENRICHED,)}, cutoff=CUTOFF
    )
    assert first.equals(again)


def test_a_snapshot_the_capture_did_not_declare_is_never_read():
    warehouse = Warehouse([_row()])
    factory = boundary.native_product_io_factory(warehouse=warehouse, meter=_meter(warehouse))
    with pytest.raises(ValueError, match="products_capture_source_undeclared"):
        factory(admission=_admission(), capture={"snapshot_tables": ("other",)}, cutoff=CUTOFF)
    assert warehouse.queries == []


def test_zero_model_origin_never_builds_the_model_sdk():
    warehouse = Warehouse([])

    def sdk(**kwargs):
        raise AssertionError("model sdk built")

    factory = boundary.native_product_io_factory(
        warehouse=warehouse, meter=_meter(warehouse, 0), model_sdk=sdk
    )
    built = factory(
        admission=_admission(0), capture={"snapshot_tables": (ENRICHED,)}, cutoff=CUTOFF
    )
    assert set(REQUIRED_SOURCE_COLUMNS) <= set(built.enriched.columns)
    with pytest.raises(ValueError, match="products_model_budget_unfunded"):
        built.models.generate_content(model="m", contents="x", config={})


def test_sink_appends_through_one_unretried_load_and_returns_the_reported_count():
    warehouse = Warehouse()
    built = boundary.NativeProductBoundary(warehouse=warehouse, enriched=None, models=None)
    assert built.sink("trend_scores")([{"a": 1}, {"a": 2}]) == 2
    assert built.sink("trend_scores")([]) == 0
    ((rows, table, kwargs),) = warehouse.loads
    assert rows == [{"a": 1}, {"a": 2}]
    assert table == "ogilvy-trends-v2.trends_v2_staging.trend_scores"
    assert kwargs["num_retries"] == 0
    assert kwargs["job_config"].write_disposition == "WRITE_APPEND"


def test_native_factory_supplies_the_history_factory(monkeypatch):
    from tests.unit.test_daily_native_clients import Fixture

    products = Fixture(monkeypatch).clients["products"]
    assert products.pre_dispatch_refusal() is None
    assert products._io_factory is not None


def test_certified_rules_without_an_approval_record_leave_compose_unbound(monkeypatch):
    from scripts.staging import replay_open_intelligence as replay

    monkeypatch.setattr(replay, "load_composition_rules_v3", lambda *args, **kwargs: {})
    assert native.native_compose_binding() is None


@pytest.mark.parametrize("error", [ValueError("rules_invalid"), OSError("unreadable")])
def test_an_unreadable_rule_set_leaves_compose_unbound_instead_of_aborting(monkeypatch, error):
    from scripts.staging import replay_open_intelligence as replay

    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(replay, "load_composition_rules_v3", refuse)
    assert native.native_compose_binding() is None


def test_sink_sends_dates_and_instants_as_json_text():
    import json
    from datetime import date

    warehouse = Warehouse()
    built = boundary.NativeProductBoundary(warehouse=warehouse, enriched=None, models=None)
    row = {"trend_date": date(2026, 9, 19), "scored_at": CUTOFF, "terms": ["a"], "n": 1}
    assert built.sink("trend_scores")([row]) == 1
    ((rows, _table, _kwargs),) = warehouse.loads
    assert json.loads(json.dumps(rows)) == [
        {"trend_date": "2026-09-19", "scored_at": CUTOFF.isoformat(), "terms": ["a"], "n": 1}
    ]
