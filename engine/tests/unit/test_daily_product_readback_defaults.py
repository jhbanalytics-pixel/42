from datetime import date
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence.daily_product_completion import CompletionStore
from src.analysis.open_intelligence.daily_product_io import BoundedProductIO, ProductBudget
from src.analysis.open_intelligence.persistence import TARGET_LOCATION

from tests.unit.test_daily_product_completion import Objects


@pytest.mark.parametrize("tampered", [False, True])
def test_readback_checks_written_fields_without_rejecting_server_defaults(tampered):
    rows = []
    identity = "intelligence-42-apply@ogilvy-trends-v2.iam.gserviceaccount.com"
    client = SimpleNamespace(
        project="ogilvy-trends-v2",
        location=TARGET_LOCATION,
        _credentials=SimpleNamespace(service_account_email=identity, quota_project_id=None),
        query=lambda *args, **kwargs: SimpleNamespace(
            total_bytes_billed=0,
            result=lambda **kwargs: [
                {
                    **row,
                    "created_with_gemini_tag": None,
                    "trend_name": "changed" if tampered else row["trend_name"],
                }
                for row in rows
            ],
        ),
    )
    boundary = SimpleNamespace(
        client=client,
        dataset="trends_v2_staging",
        sink=lambda table: lambda batch: rows.extend(batch) or len(batch),
    )
    io = BoundedProductIO(
        boundary,
        admission=SimpleNamespace(
            manifest=SimpleNamespace(project=client.project, service_identity=identity)
        ),
        budget=ProductBudget({"max_bytes_billed": 100, "max_rows_written": 10}),
        trend_date=date(2026, 9, 19),
        ledger=CompletionStore(Objects()),
        operation_id="op-1",
    )
    row = {
        "brief_id": "brief-1",
        "trend_date": date(2026, 9, 19),
        "market": "za",
        "trend_name": "music",
    }
    if tampered:
        with pytest.raises(ValueError, match="products_readback_differs"):
            io.sink("creator_briefs")([row])
    else:
        assert io.sink("creator_briefs")([row]) == 1
        assert io.outcome("creator_briefs")["row_count"] == 1
