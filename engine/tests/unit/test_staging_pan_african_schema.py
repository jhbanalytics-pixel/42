from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import scripts.setup_bigquery as setup
from scripts.migrations.create_pan_african_table import CREATE_SQL


def test_staging_setup_includes_pan_african_schema_once():
    assert setup.schema_order_for_dataset("trends_v2_staging").count("pan_african_stories.sql") == 1


@pytest.mark.parametrize(
    "dataset",
    [
        "trends_v2",
        "trends_v2_dev",
        "trends_v2_staging_qa",
        "trends_v2_staging_approvals",
        "trends_v2_staging_funded",
    ],
)
def test_other_schema_targets_do_not_gain_pan_african_table(dataset):
    assert "pan_african_stories.sql" not in setup.schema_order_for_dataset(dataset)


def test_staging_pan_african_ddl_uses_existing_migration():
    sql = setup.render_schema_sql(
        "pan_african_stories.sql", "ogilvy-trends-v2", "trends_v2_staging"
    )
    assert sql == CREATE_SQL.format(project="ogilvy-trends-v2", dataset="trends_v2_staging")
    assert "PARTITION BY trend_date" in sql
    assert "CLUSTER BY story_id" in sql


@pytest.mark.parametrize(
    "project,dataset",
    [("ogilvy-trends-v2", "trends_v2"), ("other-project", "trends_v2_staging")],
)
def test_pan_african_setup_refuses_targets_outside_main_staging(project, dataset):
    with pytest.raises(ValueError, match="pan African setup target"):
        setup.render_schema_sql("pan_african_stories.sql", project, dataset)


def test_setup_executes_pan_african_ddl_and_waits_for_completion(monkeypatch):
    monkeypatch.setattr(setup, "PROJECT", "ogilvy-trends-v2")
    monkeypatch.setattr(setup, "DATASET", "trends_v2_staging")
    client = MagicMock()
    setup.deploy_schema(client, "pan_african_stories.sql")
    client.query.assert_called_once_with(
        CREATE_SQL.format(project="ogilvy-trends-v2", dataset="trends_v2_staging")
    )
    client.query.return_value.result.assert_called_once_with()


def test_main_staging_provisions_pan_african_once(monkeypatch):
    monkeypatch.setattr(setup, "PROJECT", "ogilvy-trends-v2")
    monkeypatch.setattr(setup, "DATASET", "trends_v2_staging")
    client = MagicMock()
    client.query.return_value.result.return_value = [SimpleNamespace(n=0)]
    monkeypatch.setattr(setup.bigquery, "Client", lambda **kwargs: client)
    monkeypatch.setattr(setup, "create_dataset", lambda client: None)
    monkeypatch.setattr(
        setup, "reconcile_open_intelligence_staging_dataset_access", lambda client: None
    )
    setup.main()
    queries = [call.args[0] for call in client.query.call_args_list]
    assert (
        queries.count(CREATE_SQL.format(project="ogilvy-trends-v2", dataset="trends_v2_staging"))
        == 1
    )
    assert sum("SELECT COUNT(*)" in sql and "pan_african_stories" in sql for sql in queries) == 1
