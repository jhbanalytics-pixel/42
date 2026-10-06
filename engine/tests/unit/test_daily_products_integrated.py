from types import SimpleNamespace

from src.analysis.open_intelligence.daily_dynamic_meter import DynamicMeter
from src.analysis.open_intelligence.daily_products import DailyProducts
from src.analysis.open_intelligence.daily_stages import _admitted_capture
from src.analysis.open_intelligence.persistence import TARGET_LOCATION
from src.analysis.open_intelligence.run_receipts import build_run_receipt

from tests.unit import test_daily_products_nonempty as nonempty
from tests.unit.daily_product_authority_fixture import funded_product_authority
from tests.unit.test_daily_product_completion import Objects
from tests.unit.test_daily_stages import CUTOFF, Fixture, manifest
from tests.unit.test_open_intelligence_release_profiles import (
    STAGING_RUN_ID,
    staging_receipt_fields,
)


def test_native_factory_installs_shared_dynamic_meter(monkeypatch):
    from tests.unit.test_daily_native_clients import Fixture as NativeFixture

    fx = NativeFixture(monkeypatch)
    meter = fx.clients["products"].dynamic_client
    assert isinstance(meter, DynamicMeter)
    assert meter._client is fx.bigquery


def test_nonempty_producers_reach_bound_completion_with_metered_external_io(tmp_path, monkeypatch):
    from datetime import timedelta

    fx = funded_product_authority(tmp_path, monkeypatch)
    stages = Fixture(monkeypatch)
    stages.run(deny=("compose",))
    entry = _admitted_capture(stages.clients, stages.record("capture"))
    monkeypatch.setattr(nonempty, "DAY", (CUTOFF - timedelta(days=1)).date())
    monkeypatch.setattr(nonempty, "CUTOFF", CUTOFF)
    boundary = nonempty.BoundaryIO()

    class Warehouse(nonempty.Warehouse):
        location = TARGET_LOCATION
        _credentials = SimpleNamespace(
            service_account_email=fx.authority.manifest.service_identity, quota_project_id=None
        )

        def _rows(self, sql, params):
            if sql.strip() == "SELECT 1":
                return []
            if "STRING_AGG(NULLIF(v2persons" in sql:
                return [{"persons": "", "orgs": "", "slang": "newdancewave"}]
            if "brand24_mention_sentiment" in sql:
                return []
            if ".trend_scores`" in sql and "AVG(" in sql:
                return []
            if sql.startswith("SELECT * FROM"):
                table = sql.split("`", 2)[1].rsplit(".", 1)[1]
                return self.io.read_rows(table)
            return super()._rows(sql, params)

    boundary.client = Warehouse(boundary)
    meter = DynamicMeter(boundary.client)
    products = DailyProducts(
        objects=Objects(),
        build={
            "source_sha": fx.authority.manifest.source_sha,
            "image_uri": fx.authority.manifest.image_uri,
        },
        now=lambda: CUTOFF,
        run_id=STAGING_RUN_ID,
        io_factory=lambda **kwargs: boundary,
        dynamic_meter=meter,
    )
    result = products.run(entry=entry, manifest=manifest("compose"), authority_receipt=fx.receipt)
    meter.query("SELECT 1").result()
    complete = products.finish(
        result, dynamic_receipt=build_run_receipt(**staging_receipt_fields())
    )
    assert len(complete["products"]) == 8
    assert complete["products"]["trend_analysis"]["row_count"] == 6
    assert complete["products"]["creator_briefs"]["row_count"] == 6
    assert complete["metering"]["model_calls"] >= 8
    assert complete["metering"]["query_count"] > 1
    assert complete["metering"]["total_bytes_billed"] == 10 * complete["metering"]["query_count"]
