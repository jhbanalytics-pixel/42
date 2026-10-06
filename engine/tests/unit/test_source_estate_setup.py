from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import scripts.setup_bigquery as setup
from scripts.staging.collect_42_sources import PROJECT, STAGING_SOURCE_DATASET

EXPECTED = (
    "raw_content.sql",
    "enriched_content.sql",
    "pipeline_runs.sql",
    "system_events.sql",
)


def test_source_estate_schema_order_is_exact_collection_write_closure():
    assert tuple(setup.schema_order_for_dataset(STAGING_SOURCE_DATASET)) == EXPECTED
    assert setup.view_order_for_dataset(STAGING_SOURCE_DATASET) == []


def test_exact_source_estate_target_is_admitted():
    assert (PROJECT, STAGING_SOURCE_DATASET) == (
        "ogilvy-trends-v2",
        "intelligence_42_sources_staging",
    )
    setup.validate_setup_target(PROJECT, STAGING_SOURCE_DATASET)


@pytest.mark.parametrize(
    "project,dataset",
    [
        ("another-project", STAGING_SOURCE_DATASET),
        (PROJECT, "intelligence_42_sources_staging_backup"),
        (PROJECT, "backup_intelligence_42_sources_staging"),
        (PROJECT, "trends_v2_staging"),
        (PROJECT, "trends_v2"),
        (PROJECT, "intelligence_42_sources_prod"),
    ],
)
def test_source_estate_renderer_refuses_other_targets(project, dataset):
    with pytest.raises(ValueError, match="source estate target"):
        setup.render_source_estate_ddl(project, dataset)


def test_source_estate_renderer_reuses_complete_canonical_ddl():
    rendered = setup.render_source_estate_ddl(PROJECT, STAGING_SOURCE_DATASET)
    assert tuple(rendered) == EXPECTED
    for filename, sql in rendered.items():
        canonical = (setup.SCHEMAS_DIR / filename).read_text(encoding="utf-8")
        assert sql == canonical.replace("{project}", PROJECT).replace(
            "{dataset}", STAGING_SOURCE_DATASET
        )
        assert f"`{PROJECT}.{STAGING_SOURCE_DATASET}.{filename[:-4]}`" in sql
        assert "{project}" not in sql
        assert "{dataset}" not in sql
    assert "PARTITION BY DATE(collected_at)" in rendered["raw_content.sql"]
    assert "PARTITION BY DATE(collected_at)" in rendered["enriched_content.sql"]


def test_source_estate_rendering_never_constructs_a_cloud_client(monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr(setup.bigquery, "Client", factory)
    assert tuple(setup.render_source_estate_ddl(PROJECT, STAGING_SOURCE_DATASET)) == EXPECTED
    factory.assert_not_called()


@pytest.mark.parametrize(
    "filename",
    ["trend_scores.sql", "seed_graph.sql", "event_ledger.sql", "gemini_usage.sql"],
)
def test_generic_renderer_cannot_put_product_or_usage_tables_in_source_estate(filename):
    with pytest.raises(ValueError, match="source estate schema"):
        setup.render_schema_sql(filename, PROJECT, STAGING_SOURCE_DATASET)


def test_generic_source_estate_renderer_refuses_other_project():
    with pytest.raises(ValueError, match="source estate target"):
        setup.render_schema_sql("raw_content.sql", "another-project", STAGING_SOURCE_DATASET)


def test_main_provisions_only_the_source_closure_without_route_acls(monkeypatch):
    client = MagicMock()
    client.query.return_value.result.return_value = [SimpleNamespace(n=0)]
    monkeypatch.setattr(setup, "PROJECT", PROJECT)
    monkeypatch.setattr(setup, "DATASET", STAGING_SOURCE_DATASET)
    monkeypatch.setattr(setup.bigquery, "Client", lambda **kwargs: client)
    routes_acl = MagicMock()
    funded_acl = MagicMock()
    monkeypatch.setattr(setup, "reconcile_open_intelligence_staging_dataset_access", routes_acl)
    monkeypatch.setattr(setup, "reconcile_funded_lane_dataset_access", funded_acl)

    setup.main()

    dataset = client.create_dataset.call_args.args[0]
    assert (dataset.project, dataset.dataset_id) == (PROJECT, STAGING_SOURCE_DATASET)
    assert dataset.location == "US"
    statements = [call.args[0] for call in client.query.call_args_list]
    ddl = [sql for sql in statements if not sql.startswith("SELECT COUNT(*)")]
    assert ddl == list(setup.render_source_estate_ddl(PROJECT, STAGING_SOURCE_DATASET).values())
    assert len(statements) == 2 * len(EXPECTED)
    routes_acl.assert_not_called()
    funded_acl.assert_not_called()


@pytest.mark.parametrize(
    "project,dataset",
    [
        ("another-project", STAGING_SOURCE_DATASET),
        (PROJECT, "intelligence_42_sources_staging_backup"),
        (PROJECT, "backup_intelligence_42_sources_staging"),
    ],
)
def test_main_refuses_unapproved_targets_before_client_creation(monkeypatch, project, dataset):
    factory = MagicMock()
    monkeypatch.setattr(setup, "PROJECT", project)
    monkeypatch.setattr(setup, "DATASET", dataset)
    monkeypatch.setattr(setup.bigquery, "Client", factory)
    with pytest.raises(ValueError, match="target"):
        setup.main()
    factory.assert_not_called()


def test_collection_loader_keeps_create_never():
    source = Path(setup.__file__).resolve().parents[1] / "src" / "utils" / "bigquery.py"
    assert "create_disposition=bigquery.CreateDisposition.CREATE_NEVER" in source.read_text(
        encoding="utf-8"
    )
