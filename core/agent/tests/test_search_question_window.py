from datetime import date, datetime, timezone

import pytest

from core.agent.context import Refused, RunContext
from core.agent.tests.test_warehouse_tools import FakeWarehouse, POST_ROWS
from core.agent.tools.warehouse import search_posts


@pytest.mark.parametrize("dates", [{}, {"since": "2026-08-04"}, {"until": "2026-08-06"}])
def test_search_defaults_missing_dates_to_the_historical_question_window(dates):
    ctx = RunContext(run_id="historical_search", tier="T0", as_of=datetime(2026, 9, 28),
                     window_start=date(2026, 8, 1), window_end=date(2026, 8, 7))
    row = dict(POST_ROWS[0], published_at=datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
               post_date=date(2026, 8, 5))
    warehouse = FakeWarehouse([row])
    result = search_posts(ctx, warehouse, "braai", **dates)
    assert [item["id"] for item in result["evidence"]] == [row["post_id"]]
    for _, params, _ in warehouse.runs:
        assert params["since"] == date.fromisoformat(dates.get("since", "2026-08-01"))
        assert params["until"] == date.fromisoformat(dates.get("until", "2026-08-07"))
    assert len(result["query_ids"]) == 2


def test_explicit_search_dates_still_clip_to_the_question_window():
    ctx = RunContext(run_id="clipped_search", tier="T0", as_of=datetime(2026, 9, 28),
                     window_start=date(2026, 8, 1), window_end=date(2026, 8, 7))
    warehouse = FakeWarehouse([])
    search_posts(ctx, warehouse, "braai", since="2026-07-01", until="2026-09-01")
    assert all(params["since"] == ctx.window_start and params["until"] == ctx.window_end
               for _, params, _ in warehouse.runs)
    with pytest.raises(Refused, match="outside the ask's window"):
        search_posts(ctx, warehouse, "braai", since="2026-09-01", until="2026-09-07")
