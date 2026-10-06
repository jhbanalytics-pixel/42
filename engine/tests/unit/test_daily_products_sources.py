from datetime import UTC, date, datetime

import pandas as pd
import pytest
from src.analysis.open_intelligence.daily_products import (
    REQUIRED_SOURCE_COLUMNS,
    load_product_policy,
    validate_source_rows,
)


def source_row():
    row = dict.fromkeys(REQUIRED_SOURCE_COLUMNS)
    row.update(
        id="row-1",
        market="za",
        collected_at="2026-09-19T12:00:00+00:00",
        published_at="2026-09-19T11:00:00+00:00",
    )
    return row


def test_missing_retained_scoring_column_cannot_silently_become_zero():
    row = source_row()
    del row["genz_score"]
    with pytest.raises(ValueError, match="products_source_columns_missing:genz_score"):
        validate_source_rows(
            pd.DataFrame([row]),
            cutoff=datetime(2026, 9, 20, tzinfo=UTC),
            trend_date=date(2026, 9, 19),
        )


@pytest.mark.parametrize("mutation", ["future", "market", "duplicate"])
def test_source_window_cannot_admit_future_foreign_or_duplicate_rows(mutation):
    row = source_row()
    rows = [row]
    if mutation == "future":
        row["collected_at"] = "2026-09-20T01:00:00+00:00"
    elif mutation == "market":
        row["market"] = "us"
    else:
        rows.append(dict(row))
    with pytest.raises(ValueError, match="products_source_rows_invalid"):
        validate_source_rows(
            pd.DataFrame(rows),
            cutoff=datetime(2026, 9, 20, tzinfo=UTC),
            trend_date=date(2026, 9, 19),
        )


def test_reviewed_policy_has_every_legacy_path_and_exact_flag_set():
    policy = load_product_policy()
    assert policy == {
        "PHASE_2_ENABLED": True,
        "SEED_INTELLIGENCE_ENABLED": True,
        "SEED_GRAPH_ENABLED": True,
        "SEED_CANDIDATES_ENABLED": True,
        "PAN_AFRICAN_ENABLED": True,
    }


@pytest.mark.parametrize(
    "flag",
    [
        "PHASE_2_ENABLED",
        "SEED_INTELLIGENCE_ENABLED",
        "SEED_GRAPH_ENABLED",
        "SEED_CANDIDATES_ENABLED",
        "PAN_AFRICAN_ENABLED",
    ],
)
def test_each_governed_staging_flag_is_enabled_and_cannot_silently_disable_a_route(
    flag, tmp_path, monkeypatch
):
    from src.analysis.open_intelligence import daily_products

    assert load_product_policy()[flag] is True
    changed = tmp_path / "daily_products.env"
    changed.write_text(
        daily_products.POLICY_PATH.read_text(encoding="utf-8").replace(
            f"{flag}=true", f"{flag}=false"
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(daily_products, "POLICY_PATH", changed)
    with pytest.raises(ValueError, match="products_policy_invalid"):
        load_product_policy()


def test_empty_captured_day_runs_real_legacy_paths_without_global_io(monkeypatch):
    from types import SimpleNamespace

    from src.analysis import (
        generate_briefs,
        generate_daily_summary,
        generate_seed_intelligence,
        pan_african,
        seed_candidates,
        seed_graph,
    )
    from src.analysis.open_intelligence.daily_products import run_legacy_products

    def forbidden(*args, **kwargs):
        raise AssertionError("unbound external I/O")

    for module in (
        generate_briefs,
        generate_daily_summary,
        generate_seed_intelligence,
        pan_african,
        seed_candidates,
        seed_graph,
    ):
        for name in ("get_client", "get_dataset", "insert_dataframe", "merge_dataframe"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, forbidden)

    class EmptyWarehouse:
        project = "ogilvy-trends-v2"

        def query(self, sql, **kwargs):
            result = [SimpleNamespace(n=0)] if "COUNT(*)" in sql and "GROUP BY" not in sql else []
            return SimpleNamespace(result=lambda: result, to_dataframe=lambda: pd.DataFrame())

    class IO:
        client = EmptyWarehouse()
        dataset = "trends_v2_staging"

        def __init__(self):
            self.rows = {}

        def sink(self, table):
            def write(rows):
                self.rows.setdefault(table, []).extend(rows)
                return len(rows)

            return write

        def query_runner(self, sql, **kwargs):
            return pd.DataFrame()

        def read_rows(self, table, **kwargs):
            return list(self.rows.get(table, []))

        def outcome(self, table):
            return {"table": table, "count": len(self.rows.get(table, []))}

    io = IO()
    outcomes = run_legacy_products(
        io,
        enriched=pd.DataFrame(columns=REQUIRED_SOURCE_COLUMNS),
        trend_date=date(2026, 9, 19),
        cutoff=datetime(2026, 9, 20, tzinfo=UTC),
        scored_at=datetime(2026, 9, 20, 1, tzinfo=UTC),
        gemini_client=SimpleNamespace(generate_brief=forbidden),
    )
    assert set(outcomes) == {
        "seed_insights",
        "seed_candidates",
        "trend_analysis",
        "daily_summary",
        "v_seed_first_seen",
        "creator_briefs",
        "pan_african_stories",
    }
    assert all(outcome["count"] == 0 for outcome in outcomes.values())
