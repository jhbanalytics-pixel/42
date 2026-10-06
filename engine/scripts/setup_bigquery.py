"""
BigQuery setup script for Trends Engine V2.
Creates the trends_v2 dataset and deploys all 8 table schemas.

Run once:
    python scripts/setup_bigquery.py
"""

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

from google.api_core.exceptions import Conflict, NotFound
from google.cloud import bigquery
from scripts.staging.collect_42_sources import PROJECT as SOURCE_ESTATE_PROJECT
from scripts.staging.collect_42_sources import STAGING_SOURCE_DATASET as SOURCE_ESTATE_DATASET
from src.utils.bigquery import get_dataset

PROJECT = os.getenv("GCP_PROJECT", "ogilvy-trends-v2")
DATASET = get_dataset()  # resolves to trends_v2_dev in dev mode via TRENDS_ENV
REGION = "US"  # multi-region, required for cross-region joins to bigquery-public-data.google_trends

SCHEMAS_DIR = Path(__file__).parent.parent / "infra" / "bigquery_schemas"
VIEWS_DIR = Path(__file__).parent.parent / "infra" / "bigquery_views"

# Deploy in dependency order ,  scored tables after the tables they read from
SCHEMA_ORDER = [
    "raw_content.sql",
    "enriched_content.sql",
    "trend_scores.sql",
    "trend_analysis.sql",
    "creator_briefs.sql",
    "trend_cycles.sql",
    "pipeline_runs.sql",
    "ugc_tracking.sql",
    # The daily pipeline writes and reads daily_summary; a clean setup must
    # create it or Phase 2's cross-trend summary write fails on a fresh dataset.
    "daily_summary.sql",
    # Intelligence Core event-state ledger. Read-only of enriched_content;
    # created here so a fresh-dataset rebuild gets the table.
    "event_ledger.sql",
    # RECONCILE decision ledger (Intelligence Core trust-boundary audit trail).
    "reconcile_actions.sql",
    # LP-only seed intelligence (cron writes; no engine daily read yet).
    "seed_insights.sql",
    # Vertex Gemini token ledger for the stages that persist no table of their
    # own (comment_sentiment, driving_hashtags). Read by vertex_cost_watchdog.py.
    "gemini_usage.sql",
    # Cross-service observability (engine + LP append-only events).
    "system_events.sql",
    # Discovery loop term graph (V3 Track A, dark behind SEED_GRAPH_ENABLED).
    "seed_graph.sql",
    # Discovery loop candidates + outcomes (V3 Track C, dark behind SEED_CANDIDATES_ENABLED).
    "seed_candidates.sql",
    "seed_outcomes.sql",
]

OPEN_INTELLIGENCE_SCHEMA_ORDER = [
    "signal_candidates_v2.sql",
    "signal_evidence_v2.sql",
    "signal_membership_v2.sql",
    "signal_lineage_v2.sql",
    "signal_analysis_v2.sql",
    "signal_predictions_v2.sql",
    "signal_outcomes_v2.sql",
    "source_performance_daily_v2.sql",
    "collection_exposure_receipts_v1.sql",
    # Last, because the run receipt is written last: only after every row family
    # above has been written and read back can a run claim it closed.
    "open_intelligence_run_receipts_v1.sql",
    "open_intelligence_quality_release_records_v2.sql",
    # Additive telemetry projection: the observation disposition sidecar.
    "open_intelligence_observation_dispositions_v1.sql",
]

APPROVAL_SCHEMA_ORDER = [
    "open_intelligence_execution_approvals_v1.sql",
    "open_intelligence_execution_consumptions_v1.sql",
    "open_intelligence_execution_results_v1.sql",
    "open_intelligence_execution_approval_lock_v1.sql",
    # Versioned (v2) execution store, additive to the four v1 ledger tables above.
    # The v2 rows carry the origin registry and resource manifest digests; the
    # configuration tables hold the registered generations and the active pair.
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
]

WAVE1_STAGING_SCHEMA_ORDER = [
    "gdelt_events_wave1_v1.sql",
    "gdelt_event_market_wave1_v1.sql",
    "gdelt_gcam_wave1_v1.sql",
]

RUNTIME_CLOSURE_SCHEMA_ORDER = ["open_intelligence_quality_review_receipts_v1.sql"]

STAGING_MIGRATION_SCHEMA_ORDER = ["pan_african_stories.sql"]

FUNDED_LANE_SCHEMA_ORDER = [
    "socialcrawl_credit_ledger_v1.sql",
    "socialcrawl_funded_control_receipts_v1.sql",
    "socialcrawl_funded_terminal_events_v1.sql",
    "socialcrawl_wave1_source_values_v1.sql",
]
FUNDED_LANE_VIEW_ORDER = [
    "v_socialcrawl_funded_budget_v1.sql",
    "v_socialcrawl_funded_budget_v2.sql",
    "v_socialcrawl_wave1_source_values_v1.sql",
]

QA_SCHEMA_ORDER = ["canary_results_v2.sql"]

SOURCE_ESTATE_SCHEMA_ORDER = (
    "raw_content.sql",
    "enriched_content.sql",
    "pipeline_runs.sql",
    "system_events.sql",
)

OPEN_INTELLIGENCE_PROJECT = "ogilvy-trends-v2"
OPEN_INTELLIGENCE_STAGING_DATASET = "trends_v2_staging"
OPEN_INTELLIGENCE_QA_DATASET = "trends_v2_staging_qa"
OPEN_INTELLIGENCE_APPROVAL_DATASET = "trends_v2_staging_approvals"
FUNDED_LANE_DATASET = "trends_v2_staging_funded"
# The identity the Wave 1 job runs as: the wave1_pilot service identity the trusted
# origin registry binds and the execution policy requires. The retired v1 identity
# trends-engine-oi-wave1 is not granted.
FUNDED_LANE_WRITER = "intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com"
# The Wave 1 runtime lands its source lab rows through a temporary table in the
# staging dataset, the same way r3_apply lands releases, so it carries the same
# dataset-scoped loader role there (attempt 15 on staging, 4 Sep 2026, closed on
# the ledger and then refused because the temporary table could not be created).
STAGING_TEMPORARY_TABLE_LOADER_ROLE = (
    f"projects/{OPEN_INTELLIGENCE_PROJECT}/roles/oiStagingTemporaryTableLoader"
)


def schema_order_for_dataset(dataset: str) -> list[str]:
    if dataset == SOURCE_ESTATE_DATASET:
        return list(SOURCE_ESTATE_SCHEMA_ORDER)
    if dataset == OPEN_INTELLIGENCE_APPROVAL_DATASET:
        return list(APPROVAL_SCHEMA_ORDER)
    if dataset == OPEN_INTELLIGENCE_QA_DATASET:
        return [*OPEN_INTELLIGENCE_SCHEMA_ORDER, *QA_SCHEMA_ORDER]
    if dataset == FUNDED_LANE_DATASET:
        return list(FUNDED_LANE_SCHEMA_ORDER)
    if dataset == OPEN_INTELLIGENCE_STAGING_DATASET:
        return [
            *SCHEMA_ORDER,
            *OPEN_INTELLIGENCE_SCHEMA_ORDER,
            *WAVE1_STAGING_SCHEMA_ORDER,
            *RUNTIME_CLOSURE_SCHEMA_ORDER,
            *STAGING_MIGRATION_SCHEMA_ORDER,
        ]
    return list(SCHEMA_ORDER)


def view_order_for_dataset(dataset: str) -> list[str]:
    if dataset == FUNDED_LANE_DATASET:
        return list(FUNDED_LANE_VIEW_ORDER)
    return []


def validate_setup_target(project: str, dataset: str) -> None:
    if dataset == SOURCE_ESTATE_DATASET:
        if project != SOURCE_ESTATE_PROJECT:
            raise ValueError(f"unapproved source estate target: {project}.{dataset}")
        return
    if dataset in {
        OPEN_INTELLIGENCE_STAGING_DATASET,
        OPEN_INTELLIGENCE_QA_DATASET,
        OPEN_INTELLIGENCE_APPROVAL_DATASET,
        FUNDED_LANE_DATASET,
    }:
        if project == OPEN_INTELLIGENCE_PROJECT:
            return
        raise ValueError(f"unapproved staging target: {project}.{dataset}")
    if "staging" in dataset:
        raise ValueError(f"unapproved staging target: {project}.{dataset}")


def _with_partition_expiration(sql: str, days: int) -> str:
    marker = "\nOPTIONS (\n"
    before_options, separator, options = sql.rpartition(marker)
    if not separator:
        raise ValueError("schema DDL has no table OPTIONS block")
    if "partition_expiration_days" in options:
        return sql
    return f"{before_options}{separator}  partition_expiration_days = {days},\n{options}"


def render_schema_sql(sql_file: str, project: str, dataset: str) -> str:
    if dataset == SOURCE_ESTATE_DATASET:
        validate_setup_target(project, dataset)
        if sql_file not in SOURCE_ESTATE_SCHEMA_ORDER:
            raise ValueError(f"unapproved source estate schema: {sql_file}")
    if sql_file in STAGING_MIGRATION_SCHEMA_ORDER:
        if (project, dataset) != (
            OPEN_INTELLIGENCE_PROJECT,
            OPEN_INTELLIGENCE_STAGING_DATASET,
        ):
            raise ValueError("unapproved pan African setup target")
        from scripts.migrations.create_pan_african_table import CREATE_SQL

        return CREATE_SQL.format(project=project, dataset=dataset)
    sql = (SCHEMAS_DIR / sql_file).read_text(encoding="utf-8")
    if sql_file in OPEN_INTELLIGENCE_SCHEMA_ORDER:
        if (project, dataset) == (
            OPEN_INTELLIGENCE_PROJECT,
            OPEN_INTELLIGENCE_QA_DATASET,
        ):
            sql = _with_partition_expiration(sql, 90)
        elif (project, dataset, sql_file) == (
            OPEN_INTELLIGENCE_PROJECT,
            OPEN_INTELLIGENCE_STAGING_DATASET,
            "signal_membership_v2.sql",
        ):
            sql = _with_partition_expiration(sql, 400)
    return sql.replace("{project}", project).replace("{dataset}", dataset)


def render_source_estate_ddl(project: str, dataset: str) -> dict[str, str]:
    if (project, dataset) != (SOURCE_ESTATE_PROJECT, SOURCE_ESTATE_DATASET):
        raise ValueError(f"unapproved source estate target: {project}.{dataset}")
    return {
        filename: render_schema_sql(filename, project, dataset)
        for filename in SOURCE_ESTATE_SCHEMA_ORDER
    }


def create_dataset(client: bigquery.Client) -> None:
    dataset_ref = bigquery.Dataset(f"{PROJECT}.{DATASET}")
    dataset_ref.location = REGION
    dataset_ref.description = (
        "Trends Engine V2 ,  SSA cultural trend pipeline for Google Gemini campaign"
    )
    if DATASET == FUNDED_LANE_DATASET:
        dataset_ref.description = "Dedicated staging ledger for the funded SocialCrawl lane."
        dataset_ref.access_entries = _funded_lane_bootstrap_access_entries()
    try:
        client.create_dataset(dataset_ref, timeout=30)
        print(f"  Created dataset {PROJECT}.{DATASET} in {REGION}")
    except Conflict:
        print(f"  Dataset {PROJECT}.{DATASET} already exists ,  skipping")


def _funded_lane_bootstrap_access_entries() -> list[bigquery.AccessEntry]:
    return [
        bigquery.AccessEntry("OWNER", "specialGroup", "projectOwners"),
        bigquery.AccessEntry("WRITER", "userByEmail", FUNDED_LANE_WRITER),
    ]


def _funded_lane_access_entries() -> list[bigquery.AccessEntry]:
    return [
        *_funded_lane_bootstrap_access_entries(),
        *(
            bigquery.AccessEntry(
                None,
                "view",
                {
                    "projectId": OPEN_INTELLIGENCE_PROJECT,
                    "datasetId": OPEN_INTELLIGENCE_STAGING_DATASET,
                    "tableId": sql_file.removesuffix(".sql"),
                },
            )
            for sql_file in FUNDED_LANE_VIEW_ORDER
        ),
    ]


def _access_identity(entry: bigquery.AccessEntry) -> tuple[object, ...]:
    entity_id = entry.entity_id
    if isinstance(entity_id, Mapping):
        return (entry.role, entry.entity_type, tuple(sorted(entity_id.items())))
    return (entry.role, entry.entity_type, entity_id)


def reconcile_funded_lane_dataset_access(client: bigquery.Client) -> None:
    dataset = client.get_dataset(f"{OPEN_INTELLIGENCE_PROJECT}.{FUNDED_LANE_DATASET}")
    if dataset.location != REGION:
        raise ValueError(f"funded lane dataset must be in {REGION}")
    desired = _funded_lane_access_entries()
    desired_ids = {_access_identity(entry) for entry in desired}
    current_ids = {_access_identity(entry) for entry in dataset.access_entries}
    if current_ids != desired_ids:
        dataset.access_entries = desired
        client.update_dataset(dataset, ["access_entries"])
    readback = client.get_dataset(f"{OPEN_INTELLIGENCE_PROJECT}.{FUNDED_LANE_DATASET}")
    if {_access_identity(entry) for entry in readback.access_entries} != desired_ids:
        raise ValueError("funded lane dataset access readback does not match the approved ACL")


def _staging_temporary_table_loader_entries() -> list[bigquery.AccessEntry]:
    return [
        bigquery.AccessEntry(
            STAGING_TEMPORARY_TABLE_LOADER_ROLE, "userByEmail", FUNDED_LANE_WRITER
        ),
    ]


def reconcile_open_intelligence_staging_dataset_access(client: bigquery.Client) -> None:
    """Add the Wave 1 loader entry to the staging dataset ACL without touching the rest."""

    name = f"{OPEN_INTELLIGENCE_PROJECT}.{OPEN_INTELLIGENCE_STAGING_DATASET}"
    dataset = client.get_dataset(name)
    if dataset.location != REGION:
        raise ValueError(f"staging dataset must be in {REGION}")
    current = list(dataset.access_entries)
    current_ids = {_access_identity(entry) for entry in current}
    missing = [
        entry
        for entry in _staging_temporary_table_loader_entries()
        if _access_identity(entry) not in current_ids
    ]
    if missing:
        dataset.access_entries = [*current, *missing]
        client.update_dataset(dataset, ["access_entries"])
    readback = client.get_dataset(name)
    readback_ids = {_access_identity(entry) for entry in readback.access_entries}
    if any(
        _access_identity(entry) not in readback_ids
        for entry in _staging_temporary_table_loader_entries()
    ):
        raise ValueError("staging dataset access readback lacks the Wave 1 loader entry")


def deploy_schema(client: bigquery.Client, sql_file: str) -> None:
    path = SCHEMAS_DIR / sql_file
    if sql_file not in STAGING_MIGRATION_SCHEMA_ORDER and not path.exists():
        print(f"  WARN: {sql_file} not found ,  skipping")
        return

    sql = render_schema_sql(sql_file, PROJECT, DATASET)

    try:
        client.query(sql).result()
        print(f"  OK: {sql_file}")
    except Exception as e:
        print(f"  ERROR: {sql_file} ,  {e}")
        raise


def deploy_view(client: bigquery.Client, sql_file: str) -> None:
    path = VIEWS_DIR / sql_file
    if not path.exists():
        raise FileNotFoundError(f"funded lane view is missing: {sql_file}")
    sql = path.read_text(encoding="utf-8")
    sql = (
        sql.replace("{project}", PROJECT)
        .replace("{view_dataset}", OPEN_INTELLIGENCE_STAGING_DATASET)
        .replace("{source_dataset}", FUNDED_LANE_DATASET)
    )
    client.query(sql).result()
    print(f"  OK: {sql_file}")


def main() -> None:
    print("\nTrends Engine V2 ,  BigQuery Setup")
    print(f"Project : {PROJECT}")
    print(f"Dataset : {DATASET}")
    print(f"Region  : {REGION}")
    print()

    validate_setup_target(PROJECT, DATASET)
    schema_order = schema_order_for_dataset(DATASET)
    view_order = view_order_for_dataset(DATASET)
    client = bigquery.Client(project=PROJECT)

    print("Step 1: Create dataset")
    create_dataset(client)

    print("\nStep 2: Deploy table schemas")
    for sql_file in schema_order:
        deploy_schema(client, sql_file)

    if view_order:
        print("\nStep 3: Deploy read views")
        for sql_file in view_order:
            deploy_view(client, sql_file)
    if DATASET == FUNDED_LANE_DATASET:
        reconcile_funded_lane_dataset_access(client)
    if DATASET == OPEN_INTELLIGENCE_STAGING_DATASET:
        reconcile_open_intelligence_staging_dataset_access(client)

    print("\nDone. Verifying row counts:")
    tables = [s.replace(".sql", "") for s in schema_order]
    for table in tables:
        # table value is derived from SCHEMA_ORDER (a hardcoded module-level
        # list of .sql filenames), so it is not user-controlled. Validate
        # anyway to silence bandit B608 and harden against future drift.
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table):
            raise ValueError(f"unsafe table name: {table!r}")
        try:
            result = client.query(
                f"SELECT COUNT(*) as n FROM `{PROJECT}.{DATASET}.{table}`"
            ).result()
            n = list(result)[0].n
            print(f"  {table}: {n} rows")
        except NotFound:
            print(f"  {table}: NOT FOUND ,  deploy may have failed")

    print("\nBigQuery setup complete.")


if __name__ == "__main__":
    main()
