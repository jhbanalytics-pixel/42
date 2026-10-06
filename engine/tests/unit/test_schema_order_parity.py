"""Guard every schema registry and dataset routing boundary."""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import scripts.setup_bigquery as setup

_ROOT = Path(__file__).resolve().parent.parent.parent
_SCHEMAS_DIR = _ROOT / "infra" / "bigquery_schemas"


BASE_SCHEMA_ORDER = (
    "raw_content.sql",
    "enriched_content.sql",
    "trend_scores.sql",
    "trend_analysis.sql",
    "creator_briefs.sql",
    "trend_cycles.sql",
    "pipeline_runs.sql",
    "ugc_tracking.sql",
    "daily_summary.sql",
    "event_ledger.sql",
    "reconcile_actions.sql",
    "seed_insights.sql",
    "gemini_usage.sql",
    "system_events.sql",
    "seed_graph.sql",
    "seed_candidates.sql",
    "seed_outcomes.sql",
)

OPEN_INTELLIGENCE_SCHEMA_ORDER = (
    "signal_candidates_v2.sql",
    "signal_evidence_v2.sql",
    "signal_membership_v2.sql",
    "signal_lineage_v2.sql",
    "signal_analysis_v2.sql",
    "signal_predictions_v2.sql",
    "signal_outcomes_v2.sql",
    "source_performance_daily_v2.sql",
    "collection_exposure_receipts_v1.sql",
    "open_intelligence_run_receipts_v1.sql",
    "open_intelligence_quality_release_records_v2.sql",
    "open_intelligence_observation_dispositions_v1.sql",
)
# The four v1 ledger files keep their exact order; the eight v2 files that the
# reviewed execution authority successor adds (versioned ledger tables, then the
# origin registry, resource manifest, origin policy and active generation
# registration tables) follow them in migration order.
APPROVAL_SCHEMA_ORDER = (
    "open_intelligence_execution_approvals_v1.sql",
    "open_intelligence_execution_consumptions_v1.sql",
    "open_intelligence_execution_results_v1.sql",
    "open_intelligence_execution_approval_lock_v1.sql",
    "open_intelligence_execution_approvals_v2.sql",
    "open_intelligence_execution_consumptions_v2.sql",
    "open_intelligence_execution_results_v2.sql",
    "open_intelligence_execution_approval_lock_v2.sql",
    "open_intelligence_execution_origin_registries_v1.sql",
    "open_intelligence_execution_resource_manifests_v1.sql",
    "open_intelligence_execution_origin_policies_v1.sql",
    "open_intelligence_execution_active_generation_v1.sql",
    "open_intelligence_execution_derivations_v1.sql",
    "open_intelligence_execution_derivation_tombstones_v1.sql",
)
WAVE1_STAGING_SCHEMA_ORDER = (
    "gdelt_events_wave1_v1.sql",
    "gdelt_event_market_wave1_v1.sql",
    "gdelt_gcam_wave1_v1.sql",
)
RUNTIME_CLOSURE_SCHEMA_ORDER = ("open_intelligence_quality_review_receipts_v1.sql",)

FUNDED_LANE_SCHEMA_ORDER = (
    "socialcrawl_credit_ledger_v1.sql",
    "socialcrawl_funded_control_receipts_v1.sql",
    "socialcrawl_funded_terminal_events_v1.sql",
    "socialcrawl_wave1_source_values_v1.sql",
)
FUNDED_LANE_VIEW_ORDER = (
    "v_socialcrawl_funded_budget_v1.sql",
    "v_socialcrawl_funded_budget_v2.sql",
    "v_socialcrawl_wave1_source_values_v1.sql",
)

QA_SCHEMA_ORDER = ("canary_results_v2.sql",)


def _registries() -> tuple[tuple[str, ...], ...]:
    return (
        tuple(setup.SCHEMA_ORDER),
        tuple(getattr(setup, "OPEN_INTELLIGENCE_SCHEMA_ORDER", ())),
        tuple(getattr(setup, "APPROVAL_SCHEMA_ORDER", ())),
        tuple(getattr(setup, "WAVE1_STAGING_SCHEMA_ORDER", ())),
        tuple(getattr(setup, "RUNTIME_CLOSURE_SCHEMA_ORDER", ())),
        tuple(getattr(setup, "FUNDED_LANE_SCHEMA_ORDER", ())),
        tuple(getattr(setup, "QA_SCHEMA_ORDER", ())),
    )


def test_production_schema_order_is_unchanged():
    assert tuple(setup.SCHEMA_ORDER) == BASE_SCHEMA_ORDER


def test_schema_registries_are_exact_and_ordered():
    assert tuple(getattr(setup, "OPEN_INTELLIGENCE_SCHEMA_ORDER", ())) == (
        OPEN_INTELLIGENCE_SCHEMA_ORDER
    )
    assert tuple(getattr(setup, "APPROVAL_SCHEMA_ORDER", ())) == APPROVAL_SCHEMA_ORDER
    assert tuple(getattr(setup, "WAVE1_STAGING_SCHEMA_ORDER", ())) == WAVE1_STAGING_SCHEMA_ORDER
    assert tuple(getattr(setup, "RUNTIME_CLOSURE_SCHEMA_ORDER", ())) == (
        RUNTIME_CLOSURE_SCHEMA_ORDER
    )
    assert tuple(getattr(setup, "FUNDED_LANE_SCHEMA_ORDER", ())) == FUNDED_LANE_SCHEMA_ORDER
    assert tuple(getattr(setup, "QA_SCHEMA_ORDER", ())) == QA_SCHEMA_ORDER


def test_every_schema_file_appears_once_across_registries():
    schema_files = sorted(p.name for p in _SCHEMAS_DIR.glob("*.sql"))
    entries = [name for registry in _registries() for name in registry]
    assert sorted(entries) == schema_files
    assert len(entries) == len(set(entries))


def test_every_schema_file_in_schema_order():
    test_every_schema_file_appears_once_across_registries()


def test_schema_registry_entries_exist_on_disk():
    for registry in _registries():
        for name in registry:
            path = _SCHEMAS_DIR / name
            assert path.is_file(), f"schema registry references missing file: {name}"


def test_schema_order_entries_exist_on_disk():
    test_schema_registry_entries_exist_on_disk()


def test_schema_order_for_main_staging_appends_open_intelligence_tables():
    assert tuple(setup.schema_order_for_dataset("trends_v2_staging")) == (
        BASE_SCHEMA_ORDER
        + OPEN_INTELLIGENCE_SCHEMA_ORDER
        + WAVE1_STAGING_SCHEMA_ORDER
        + RUNTIME_CLOSURE_SCHEMA_ORDER
        + ("pan_african_stories.sql",)
    )


def test_funded_lane_dataset_contains_only_the_credit_ledger():
    assert tuple(setup.schema_order_for_dataset("trends_v2_staging_funded")) == (
        FUNDED_LANE_SCHEMA_ORDER
    )


def test_funded_lane_views_are_exact_and_ordered():
    assert tuple(setup.view_order_for_dataset("trends_v2_staging_funded")) == (
        FUNDED_LANE_VIEW_ORDER
    )


def test_isolated_approval_dataset_contains_only_the_twelve_ledger_tables():
    assert tuple(setup.schema_order_for_dataset("trends_v2_staging_approvals")) == (
        APPROVAL_SCHEMA_ORDER
    )


def test_funded_dataset_acl_reconciliation_removes_project_writers_and_pins_runtime_writer():
    dataset = SimpleNamespace(
        location="US",
        access_entries=[
            setup.bigquery.AccessEntry("WRITER", "specialGroup", "projectWriters"),
            setup.bigquery.AccessEntry("OWNER", "specialGroup", "projectOwners"),
        ],
    )
    client = MagicMock()
    client.get_dataset.side_effect = [dataset, dataset]

    setup.reconcile_funded_lane_dataset_access(client)

    expected = {
        ("OWNER", "specialGroup", "projectOwners"),
        (
            "WRITER",
            "userByEmail",
            "intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com",
        ),
    }
    assert {
        (entry.role, entry.entity_type, entry.entity_id)
        for entry in dataset.access_entries
        if entry.entity_type != "view"
    } == expected
    assert {
        tuple(sorted(entry.entity_id.items()))
        for entry in dataset.access_entries
        if entry.entity_type == "view"
    } == {
        (
            ("datasetId", "trends_v2_staging"),
            ("projectId", "ogilvy-trends-v2"),
            ("tableId", view.removesuffix(".sql")),
        )
        for view in FUNDED_LANE_VIEW_ORDER
    }
    client.update_dataset.assert_called_once_with(dataset, ["access_entries"])


def test_dataset_acl_reconciliation_keeps_the_funded_writer_and_never_grants_the_retired_one():
    # The Wave 1 job runs as the registry's wave1_pilot identity and its preflight reads
    # the funded ledger directly, so a settled funded ACL that grants that identity is
    # left as it is, and neither dataset is given the retired v1 identity.
    funded = "intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com"
    retired = "trends-engine-oi-wave1@ogilvy-trends-v2.iam.gserviceaccount.com"
    views = [
        setup.bigquery.AccessEntry(
            None,
            "view",
            {
                "projectId": "ogilvy-trends-v2",
                "datasetId": "trends_v2_staging",
                "tableId": view.removesuffix(".sql"),
            },
        )
        for view in FUNDED_LANE_VIEW_ORDER
    ]
    settled = SimpleNamespace(
        location="US",
        access_entries=[
            setup.bigquery.AccessEntry("OWNER", "specialGroup", "projectOwners"),
            setup.bigquery.AccessEntry("WRITER", "userByEmail", funded),
            *views,
        ],
    )
    client = MagicMock()
    client.get_dataset.side_effect = [settled, settled]
    setup.reconcile_funded_lane_dataset_access(client)
    client.update_dataset.assert_not_called()
    assert ("WRITER", "userByEmail", funded) in {
        (entry.role, entry.entity_type, entry.entity_id)
        for entry in settled.access_entries
        if entry.entity_type != "view"
    }

    retained = SimpleNamespace(
        location="US",
        access_entries=[
            setup.bigquery.AccessEntry("OWNER", "specialGroup", "projectOwners"),
            setup.bigquery.AccessEntry("WRITER", "userByEmail", retired),
            *views,
        ],
    )
    client = MagicMock()
    client.get_dataset.side_effect = [retained, retained]
    setup.reconcile_funded_lane_dataset_access(client)
    client.update_dataset.assert_called_once_with(retained, ["access_entries"])
    principals = {
        (entry.role, entry.entity_type, entry.entity_id)
        for entry in retained.access_entries
        if entry.entity_type != "view"
    }
    assert principals == {
        ("OWNER", "specialGroup", "projectOwners"),
        ("WRITER", "userByEmail", funded),
    }

    for entries in (
        setup._funded_lane_bootstrap_access_entries(),
        setup._funded_lane_access_entries(),
        setup._staging_temporary_table_loader_entries(),
    ):
        identities = {entry.entity_id for entry in entries if entry.entity_type == "userByEmail"}
        assert identities == {funded}


def test_staging_dataset_acl_reconciliation_adds_the_wave1_loader_and_keeps_the_rest():
    # Attempt 15 on staging, 4 Sep 2026: the Wave 1 runtime closed on the ledger and
    # then could not create its source lab temporary table in trends_v2_staging.
    existing = [
        setup.bigquery.AccessEntry("OWNER", "specialGroup", "projectOwners"),
        setup.bigquery.AccessEntry(
            setup.STAGING_TEMPORARY_TABLE_LOADER_ROLE,
            "userByEmail",
            "trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com",
        ),
    ]
    dataset = SimpleNamespace(location="US", access_entries=list(existing))
    client = MagicMock()
    client.get_dataset.side_effect = [dataset, dataset]

    setup.reconcile_open_intelligence_staging_dataset_access(client)

    assert [(e.role, e.entity_type, e.entity_id) for e in dataset.access_entries] == [
        *[(e.role, e.entity_type, e.entity_id) for e in existing],
        (
            "projects/ogilvy-trends-v2/roles/oiStagingTemporaryTableLoader",
            "userByEmail",
            "intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com",
        ),
    ]
    client.update_dataset.assert_called_once_with(dataset, ["access_entries"])

    settled = SimpleNamespace(location="US", access_entries=list(dataset.access_entries))
    client = MagicMock()
    client.get_dataset.side_effect = [settled, settled]
    setup.reconcile_open_intelligence_staging_dataset_access(client)
    client.update_dataset.assert_not_called()


def test_staging_dataset_acl_reconciliation_refuses_wrong_location_or_lost_entry():
    client = MagicMock()
    client.get_dataset.return_value = SimpleNamespace(location="EU", access_entries=[])
    with pytest.raises(ValueError, match="US"):
        setup.reconcile_open_intelligence_staging_dataset_access(client)
    client.update_dataset.assert_not_called()

    client = MagicMock()
    client.get_dataset.side_effect = [
        SimpleNamespace(location="US", access_entries=[]),
        SimpleNamespace(location="US", access_entries=[]),
    ]
    with pytest.raises(ValueError, match="Wave 1 loader"):
        setup.reconcile_open_intelligence_staging_dataset_access(client)


def test_funded_dataset_acl_reconciliation_refuses_wrong_location():
    client = MagicMock()
    client.get_dataset.return_value = SimpleNamespace(location="EU", access_entries=[])

    with pytest.raises(ValueError, match="US"):
        setup.reconcile_funded_lane_dataset_access(client)

    client.update_dataset.assert_not_called()


def test_schema_order_for_qa_excludes_base_tables():
    assert tuple(setup.schema_order_for_dataset("trends_v2_staging_qa")) == (
        OPEN_INTELLIGENCE_SCHEMA_ORDER + QA_SCHEMA_ORDER
    )


def test_schema_order_for_dataset_is_a_one_argument_pure_boundary():
    assert tuple(inspect.signature(setup.schema_order_for_dataset).parameters) == ("dataset",)


def test_schema_order_for_every_other_dataset_is_production_only():
    for dataset in (
        "trends_v2",
        "trends_v2_dev",
        "trends_v2_prod",
        "backup_trends_v2_staging",
        "trends_v2_backup_staging",
        "trends_v2_staging_backup",
        "backup_trends_v2_staging_qa",
        "trends_v2_backup_staging_qa",
        "trends_v2_staging_qa_backup",
    ):
        assert tuple(setup.schema_order_for_dataset(dataset)) == BASE_SCHEMA_ORDER


@pytest.mark.parametrize("dataset", ["trends_v2_staging", "trends_v2_staging_qa"])
def test_validate_setup_target_accepts_exact_approved_staging_targets(dataset: str):
    setup.validate_setup_target("ogilvy-trends-v2", dataset)


@pytest.mark.parametrize(
    ("project", "dataset"),
    [
        ("other-project", "trends_v2_staging"),
        ("other-project", "trends_v2_staging_qa"),
        ("ogilvy-trends-v2", "backup_trends_v2_staging"),
        ("ogilvy-trends-v2", "trends_v2_backup_staging"),
        ("ogilvy-trends-v2", "trends_v2_staging_backup"),
        ("ogilvy-trends-v2", "backup_trends_v2_staging_qa"),
        ("ogilvy-trends-v2", "trends_v2_backup_staging_qa"),
        ("ogilvy-trends-v2", "trends_v2_staging_qa_backup"),
    ],
)
def test_validate_setup_target_rejects_wrong_project_and_lookalikes(project: str, dataset: str):
    with pytest.raises(ValueError, match="unapproved staging target"):
        setup.validate_setup_target(project, dataset)


@pytest.mark.parametrize(
    ("project", "dataset"),
    [
        ("other-project", "trends_v2_staging"),
        ("ogilvy-trends-v2", "trends_v2_backup_staging"),
        ("ogilvy-trends-v2", "trends_v2_staging_qa_backup"),
    ],
)
def test_main_rejects_staging_lookalikes_before_any_client_or_dataset_call(
    monkeypatch, project: str, dataset: str
):
    calls: list[str] = []
    monkeypatch.setattr(setup, "PROJECT", project)
    monkeypatch.setattr(setup, "DATASET", dataset)
    monkeypatch.setattr(setup.bigquery, "Client", lambda project: calls.append("client"))
    monkeypatch.setattr(setup, "create_dataset", lambda client: calls.append("create_dataset"))
    monkeypatch.setattr(setup, "deploy_schema", lambda client, name: calls.append("deploy_schema"))

    with pytest.raises(ValueError, match="unapproved staging target"):
        setup.main()

    assert calls == []


def test_main_uses_routed_order_for_deploy_and_row_count_verification(monkeypatch):
    routed_order = ("first_fixture.sql", "second_fixture.sql")
    boundary_calls: list[tuple[str, ...]] = []
    deployed: list[str] = []
    count_queries: list[str] = []

    class QueryJob:
        def __init__(self, sql: str):
            self.sql = sql

        def result(self):
            count_queries.append(self.sql)
            return [SimpleNamespace(n=0)]

    class Client:
        def query(self, sql: str):
            return QueryJob(sql)

    def route(dataset: str):
        boundary_calls.append(("route", dataset))
        return routed_order

    def validate(project: str, dataset: str):
        boundary_calls.append(("validate", project, dataset))

    client = Client()
    monkeypatch.setattr(
        setup,
        "validate_setup_target",
        validate,
        raising=False,
    )
    monkeypatch.setattr(setup, "schema_order_for_dataset", route, raising=False)
    monkeypatch.setattr(setup, "create_dataset", lambda _client: None)
    monkeypatch.setattr(
        setup,
        "deploy_schema",
        lambda actual_client, name: deployed.append(name) if actual_client is client else None,
    )
    monkeypatch.setattr(setup.bigquery, "Client", lambda project: client)

    setup.main()

    assert boundary_calls == [
        ("validate", setup.PROJECT, setup.DATASET),
        ("route", setup.DATASET),
    ]
    assert deployed == list(routed_order)
    assert count_queries == [
        f"SELECT COUNT(*) as n FROM `{setup.PROJECT}.{setup.DATASET}.first_fixture`",
        f"SELECT COUNT(*) as n FROM `{setup.PROJECT}.{setup.DATASET}.second_fixture`",
    ]


def test_deploy_schema_queries_the_dataset_aware_render(monkeypatch):
    rendered = "CREATE TABLE rendered_fixture"
    calls: list[object] = []

    class QueryJob:
        def result(self):
            calls.append("result")

    class Client:
        def query(self, sql: str):
            calls.append(sql)
            return QueryJob()

    monkeypatch.setattr(setup, "PROJECT", "ogilvy-trends-v2")
    monkeypatch.setattr(setup, "DATASET", "trends_v2_staging_qa")
    monkeypatch.setattr(
        setup,
        "render_schema_sql",
        lambda name, project, dataset: (
            rendered
            if (name, project, dataset)
            == (
                "signal_candidates_v2.sql",
                "ogilvy-trends-v2",
                "trends_v2_staging_qa",
            )
            else None
        ),
        raising=False,
    )

    setup.deploy_schema(Client(), "signal_candidates_v2.sql")

    assert calls == [rendered, "result"]


def test_all_expected_schema_files_are_owned_by_a_registry():
    for name in (
        BASE_SCHEMA_ORDER
        + OPEN_INTELLIGENCE_SCHEMA_ORDER
        + APPROVAL_SCHEMA_ORDER
        + WAVE1_STAGING_SCHEMA_ORDER
        + QA_SCHEMA_ORDER
    ):
        path = _SCHEMAS_DIR / name
        assert path.is_file(), f"expected schema file missing: {name}"


def test_wave1_authority_tail_order_matches_raw_and_enriched_schemas():
    expected = (
        "endpoint",
        "vendor_family",
        "channel_family",
        "source_family",
        "geo_method_id",
        "geo_receipt_id",
        "native_id",
        "source_family_map_version",
    )
    for filename in ("raw_content.sql", "enriched_content.sql"):
        sql = setup.render_schema_sql(filename, setup.PROJECT, "trends_v2_staging")
        column_block = sql.split(")\nPARTITION BY", 1)[0]
        fields = tuple(re.findall(r"^  ([a-z_]+) ", column_block, re.M))
        assert fields[-8:] == expected
