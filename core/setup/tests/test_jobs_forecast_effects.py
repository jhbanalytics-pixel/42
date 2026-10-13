from pathlib import Path

import pytest

from core.setup.release import jobs_effects as je
from core.setup.release import jobs_source as js
from core.detect import sqlrun


ROOT = Path(__file__).resolve().parents[3]


def forecast_statements():
    return [s for s in js.schema_statements(js.tree_from_disk(ROOT))
            if "`ogilvy-trends-v2.intelligence_42_agent.forecasts`" in s]


def test_registered_lineage_consumers_are_read_by_the_expanded_locality_sql():
    source = (ROOT / "core/detect/sql/locality_views.sql").read_text(encoding="utf-8")
    assert "{counted_links}" in source
    body = sqlrun.render(source)
    for table in ("post_item_lineage", "post_item_end"):
        path, access = je.CONSUMERS[f"intelligence_42_core.{table}"]
        assert path == "core/detect/sql/counted_links.sql" and access == "read"
        assert table in (ROOT / path).read_text(encoding="utf-8")
        assert f"intelligence_42_core.{table}" in body


def test_current_forecast_create_and_nullable_resolution_column_have_distinct_effects():
    create, alter = forecast_statements()
    assert je.classify(create) == ("E-FORECASTS-DDL", "create_table", "intelligence_42_agent.forecasts")
    assert je.classify(alter) == ("E-FORECASTS-RESOLUTION", "add_column", "intelligence_42_agent.forecasts")


@pytest.mark.parametrize("change", [
    lambda s: s.replace("resolution_rule_version STRING", "resolution_rule_version INT64"),
    lambda s: s.replace("resolution_rule_version STRING", "resolution_rule_version STRING NOT NULL"),
    lambda s: s.replace("resolution_rule_version", "unknown_field"),
    lambda s: s.replace("IF NOT EXISTS ", ""),
    lambda s: s + "; DROP TABLE `ogilvy-trends-v2.intelligence_42_agent.forecasts`",
])
@pytest.mark.parametrize("index", [0, 1])
def test_forecast_effect_registration_refuses_unapproved_shapes(index, change):
    with pytest.raises(ValueError):
        je.classify(change(forecast_statements()[index]))


def test_forecast_registration_keeps_unknown_routines_and_destructive_ddl_refused():
    for statement in (
        "DROP TABLE `ogilvy-trends-v2.intelligence_42_agent.forecasts`",
        "ALTER TABLE `ogilvy-trends-v2.intelligence_42_agent.forecasts` DROP COLUMN rule",
        "CREATE OR REPLACE TABLE FUNCTION `ogilvy-trends-v2.intelligence_42_agent.forecasts`() AS (SELECT 1)",
    ):
        with pytest.raises(ValueError):
            je.classify(statement)
