from copy import deepcopy
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.daily_product_completion import CompletionStore
from src.analysis.open_intelligence.daily_product_io import BoundedProductIO, ProductBudget
from src.analysis.open_intelligence.daily_products import DailyProducts, admit_execution
from src.analysis.open_intelligence.daily_stages import _admitted_capture
from src.analysis.open_intelligence.persistence import TARGET_LOCATION
from src.analysis.open_intelligence.run_receipts import build_run_receipt

from tests.unit.daily_product_authority_fixture import funded_product_authority
from tests.unit.test_daily_product_completion import Objects, completion
from tests.unit.test_daily_stages import CUTOFF, Fixture
from tests.unit.test_open_intelligence_release_profiles import (
    STAGING_RUN_ID,
    staging_receipt_fields,
)


@pytest.mark.parametrize("defect", ["tamper", "wrong_run", "wrong_day"])
def test_finish_authenticates_retained_result_and_dynamic_identity(tmp_path, monkeypatch, defect):
    fx = funded_product_authority(tmp_path, monkeypatch)
    stages = Fixture(monkeypatch)
    stages.run(deny=("compose",))
    capture = _admitted_capture(stages.clients, stages.record("capture"))
    products = DailyProducts(
        objects=Objects(),
        build=None,
        now=lambda: CUTOFF,
        run_id=STAGING_RUN_ID,
        dynamic_meter=SimpleNamespace(require_complete=lambda budget: budget.require_complete()),
    )
    result = {
        "operation_id": "op-1",
        "products": {
            name: outcome
            for name, outcome in completion()["products"].items()
            if name != "v_desk_dynamic_signals_v2"
        },
    }
    products._active["op-1"] = {
        "result_digest": canonical_digest(result),
        "capture": capture,
        "cutoff": CUTOFF,
        "receipt": fx.receipt,
        "admission": admit_execution(fx.receipt),
        "budget": ProductBudget(fx.authority.manifest.limits),
    }
    receipt = build_run_receipt(**staging_receipt_fields())
    changed = deepcopy(result)
    if defect == "tamper":
        changed["products"]["trend_analysis"]["row_count"] = 1
        changed["products"]["trend_analysis"]["state"] = "completed"
    elif defect == "wrong_run":
        receipt = replace(receipt, run_id="run_other_staging_daily_v1")
    else:
        receipt = replace(receipt, signal_date=date(2026, 9, 9))
    with pytest.raises(
        ValueError, match=r"products_result_differs|products_dynamic_identity_differs"
    ):
        products.finish(changed, dynamic_receipt=receipt)


@pytest.mark.parametrize(
    "row",
    [
        {"trend_date": date(2026, 9, 9), "market": "za"},
        {"trend_date": date(2026, 9, 10), "market": "us"},
    ],
)
def test_wrong_day_or_market_never_reaches_the_external_sink(tmp_path, monkeypatch, row):
    fx = funded_product_authority(tmp_path, monkeypatch)
    admission = admit_execution(fx.receipt)
    writes = []
    boundary = SimpleNamespace(
        dataset="trends_v2_staging",
        client=SimpleNamespace(
            project="ogilvy-trends-v2",
            location=TARGET_LOCATION,
            _credentials=SimpleNamespace(
                service_account_email=admission.manifest.service_identity, quota_project_id=None
            ),
        ),
        sink=lambda table: lambda rows: writes.extend(rows) or len(rows),
    )
    io = BoundedProductIO(
        boundary,
        admission=admission,
        budget=ProductBudget(admission.manifest.limits),
        trend_date=date(2026, 9, 10),
        ledger=CompletionStore(Objects()),
        operation_id="op-1",
    )
    with pytest.raises(ValueError, match="products_row_scope_invalid"):
        io.sink("trend_scores")([row])
    assert writes == []
