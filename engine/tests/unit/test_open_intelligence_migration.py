"""Boundary tests for the Open Intelligence staging migration runner."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.migrations.create_open_intelligence_v2 as migration
from google.api_core.exceptions import NotFound
from google.auth import credentials as auth_credentials
from google.auth import impersonated_credentials
from google.auth.compute_engine import credentials as compute_credentials
from google.cloud import bigquery
from google.oauth2 import credentials as user_credentials
from google.oauth2 import service_account
from src.contracts.bigquery_ddl import parse_table_ddl

from tests.unit.test_execution_runtime_v2 import consume as _consume_v2
from tests.unit.test_execution_runtime_v2 import fixture as _fixture_v2
from tests.unit.test_execution_runtime_v2 import load as _load_v2

PROJECT = "ogilvy-trends-v2"
STAGING_DATASET = "trends_v2_staging"
QA_DATASET = "trends_v2_staging_qa"
# The migration_apply binding of the active trusted generation's fresh origin.
STAGING_IDENTITY = "intelligence-42-migration@ogilvy-trends-v2.iam.gserviceaccount.com"
QA_IDENTITY = "trends-engine-canary@ogilvy-trends-v2.iam.gserviceaccount.com"
BASE_TABLE_NAMES = (
    "signal_candidates_v2",
    "signal_evidence_v2",
    "signal_membership_v2",
    "signal_lineage_v2",
    "signal_analysis_v2",
    "signal_predictions_v2",
    "signal_outcomes_v2",
    "source_performance_daily_v2",
    "collection_exposure_receipts_v1",
    # Registered last so the plan creates every row-family table before the
    # receipt that claims they were all written.
    "open_intelligence_run_receipts_v1",
    "open_intelligence_quality_release_records_v2",
    "open_intelligence_observation_dispositions_v1",
)
WAVE1_TABLE_NAMES = (
    "gdelt_events_wave1_v1",
    "gdelt_event_market_wave1_v1",
    "gdelt_gcam_wave1_v1",
)
TABLE_NAMES = (*BASE_TABLE_NAMES, *WAVE1_TABLE_NAMES)
VIEW_NAMES = (
    "v_signal_board_v2",
    "v_signal_evidence_v2",
    "v_source_lab_v2",
    "v_open_intelligence_health_v2",
    "v_desk_dynamic_signals_v1",
    "v_desk_dynamic_signals_v2",
)
_ROOT = Path(__file__).resolve().parent.parent.parent
_EXPECTED_SOURCE_SHA256 = {
    "infra/bigquery_schemas/signal_candidates_v2.sql": "429af89942cee097400c539c988ebdc5b58b2141ed0dfe8c418ffc996fea3fe9",
    "infra/bigquery_schemas/signal_evidence_v2.sql": "3ecded3d430163305aa74230220ff1a0a954c2bfba435a5f40e0261bebf72001",
    "infra/bigquery_schemas/signal_membership_v2.sql": "97019729fedd5317660014c1becec0cce636ec02a34856e8d853dba9fa452bdf",
    "infra/bigquery_schemas/signal_lineage_v2.sql": "b7071f4f2c9628a78f732d77850ecacc9dbc59cc5a95c6aba76a71a55ccf253b",
    "infra/bigquery_schemas/signal_analysis_v2.sql": "6e9eb90fcf4b070153ed805bc1b8852b8e80812f6defbc65460f2c5feaade2e0",
    "infra/bigquery_schemas/signal_predictions_v2.sql": "0ebef04437eb3bf876bfd7fef4374004c67da55d5413225d56e75eb9a344d9de",
    "infra/bigquery_schemas/signal_outcomes_v2.sql": "05c2c2f248a79ea98049f78c4e85c234c7aa019cf07d49879994579c0cdc9c74",
    "infra/bigquery_schemas/source_performance_daily_v2.sql": "1b7ce26d53a72d0b14167503ee07d36738b16fffbed5900330ced0040788fb0c",
    "infra/bigquery_schemas/gdelt_events_wave1_v1.sql": "46ef22ad4b4833a2773417dadcb684a3008c7712eda94463848e4dac3c5716cc",
    "infra/bigquery_schemas/gdelt_event_market_wave1_v1.sql": "6384e95f28ba93bc1b057107124b9864dd2e551d2233519c21c5a905a1bea282",
    "infra/bigquery_schemas/gdelt_gcam_wave1_v1.sql": "70e0e2fa761db27036a974bdbbec0f7e9cc0a3064a88419ddac6a652decc728d",
    "infra/bigquery_schemas/open_intelligence_run_receipts_v1.sql": "3f739ac125c6fe3ddc33355af8b1400a7e604add07027c391d50de1119230346",
    "infra/bigquery_schemas/open_intelligence_observation_dispositions_v1.sql": "5ac207425bd91721b62d090dc0b3e8ac8e6e41649476c710542afc995db93494",
    "infra/bigquery_schemas/canary_results_v2.sql": "32a6401aa9434135a6e276dd01b4dc02037fc25413c86893f349e6adbc427be9",
    "infra/bigquery_views/v_signal_board_v2.sql": "fc62243a048882452e43c9bab37dc9000103ccc405263e5956aeb243f2fd9e01",
    "infra/bigquery_views/v_signal_evidence_v2.sql": "4cb8825be31f5cfcded2d8c9bbdbb28a3c4e134e24ee36f3de0ab0ff6a3bed7d",
    "infra/bigquery_views/v_source_lab_v2.sql": "53aa2c88af66e43e9ee9f6620ee401473ecaa31957f66a8496f72182d5beb8ea",
    "infra/bigquery_views/v_open_intelligence_health_v2.sql": "dd4bc83275e3dcf8ec3ffa1446890ba2844fa68c4b9bc0291a3f2ac454e8e8e6",
    "infra/bigquery_views/v_desk_dynamic_signals_v1.sql": "d9f6eeb764fa29a9fa0fce75ba788590e6397a0fa4656fd00c9308ad05df8e6e",
    "infra/bigquery_views/v_desk_dynamic_signals_v2.sql": "bbd19dac4215b8175ee1720f416bb5fecbf92c87ea97adbc266083f5fd77f824",
}
_MEMBERSHIP_SOURCE_PATH = "infra/bigquery_schemas/signal_membership_v2.sql"
_ACCEPTED_SOURCE_PATHS = (*_EXPECTED_SOURCE_SHA256, _MEMBERSHIP_SOURCE_PATH)
# Re-pinned when the date regex in the bridge policy was tightened to ASCII digits
# (registry e6b95e35 to f23001ed, manifest b7553041 to 592ed45f): the dry run's trusted
# generation line is again the only line that moves, and the plan digest is unchanged.
# Before that, re-pinned when the daily contract revision registered three daily
# operations and bounded exposure (registry 573deeea to e6b95e35, manifest ab7802f4 to
# b7553041): the dry run's
# trusted generation line is again the only line that moves, and the plan digest is
# unchanged. Before that, re-pinned when amendment g added the route A capture's
# artifact bucket to the bridge
# manifest (manifest 1643e4ce to ab7802f4, registry unchanged): the dry run's trusted
# generation line is again the only line that moves, and the plan digest is unchanged.
# Before that, re-pinned when the bridge v3 generation became the active pair (registry d67a0f73 to
# 573deeea, manifest 33ed5155 to 1643e4ce): the dry run's trusted generation line is the
# only line that moves, and the plan digest is unchanged. Before that, re-pinned for
# amendment e, when the resource manifest gained the owner reconciliation
# and v3 source snapshot routine rows, the read only Cloud Build builds collection and
# the three read only v2 ledger tables (manifest ee809a4e to 33ed5155, registry unchanged), which moves the active generation
# pair through the dry run. Before that, re-pinned for amendment d, when the daily origin
# row took the four folded operations
# (registry 3fb8742c to d67a0f73, manifest 52548d3b to ee809a4e): the runtime closure
# now reads r3_apply and r3_release under one identity, so its two read rows collapse to
# one and the active generation pair moves through the dry run. Before that, re-pinned
# when the resource manifest gained the v3 approve and read routine rows (manifest
# 93a66695 to 52548d3b), which moves the active generation pair through the dry run.
# Before that, re-pinned when the registry gained the v2 capture origin row and the manifest its registry
# digest (registry 38d7db4b to 3fb8742c, manifest 99986a35 to 93a66695), which moves the
# active generation pair through the dry run. Before that, re-pinned when the resource
# manifest gained the grant bound source snapshot routine
# row (digest f8ef93da to 99986a35), which moves the dry run through the active generation,
# on top of the open_intelligence_observation_dispositions_v1 sidecar registration (D05).
# Before that, re-pinned when open_intelligence_observation_dispositions_v1 was registered
# as the additive observation disposition sidecar (D05). Before that, re-pinned when the
# reviewed resource manifest was amended (digest f8ef93da), which
# moves the dry run through the active generation. Before that, re-pinned when the
# staging identity, IAM principals and trusted generation moved onto
# the active origin (versioned execution v2). Before that, re-pinned when
# open_intelligence_run_receipts_v1 was registered. This digest is
# the artifact a human reviews before any apply, so it must move whenever the
# plan genuinely changes shape, and never be relaxed into a looser assertion.
_APPROVED_GENERIC_STAGING_PLAN_SHA256 = (
    "191a71c21a8b360cce542fae4e4efa673e2680170ab617ef15ac4368a3d48f18"
)
_APPROVED_GENERIC_STAGING_DRY_RUN_SHA256 = (
    "7dfe8ed846966348796940acbe05eaa687ed0976840a675939d6ae983a93d012"
)


class _Signer:
    key_id = "fixture-key"

    def sign(self, message: bytes) -> bytes:
        return b"fixture-signature"


def _service_credentials(
    email: str, quota_project_id: str | None = None
) -> service_account.Credentials:
    return service_account.Credentials(
        signer=_Signer(),
        service_account_email=email,
        token_uri="https://example.invalid/token",
        project_id=PROJECT,
        quota_project_id=quota_project_id,
    )


def _credential_loader(email: str):
    credentials = _service_credentials(email)
    return lambda: (credentials, PROJECT)


def _api_type(field_type: str) -> str:
    return {
        "INT64": "INTEGER",
        "FLOAT64": "FLOAT",
        "BOOL": "BOOLEAN",
    }.get(field_type, field_type)


def _schema_fields(parsed_fields: tuple) -> list[bigquery.SchemaField]:
    return [
        bigquery.SchemaField(
            name,
            _api_type(field_type),
            mode=mode,
            description=description,
            fields=_schema_fields(nested),
        )
        for name, field_type, mode, description, nested in parsed_fields
    ]


def _partition_field(expression: str | None) -> str | None:
    if expression is None or expression in {"_PARTITIONDATE", "_PARTITIONTIME"}:
        # BigQuery reports ingestion-time partitioning as type DAY with no field.
        return None
    match = re.fullmatch(r"DATE\(([A-Za-z_][A-Za-z0-9_]*)\)", expression)
    return match.group(1) if match else expression


class _QueryJob:
    def result(self):
        return []


TableMutation = Callable[[dict], None]


class StatefulBigQueryClient:
    """Local stateful fake built from installed BigQuery resource classes."""

    def __init__(
        self,
        project: str = PROJECT,
        credentials=None,
        location: str = "US",
        *,
        dataset_project: str = PROJECT,
        dataset_id: str = STAGING_DATASET,
        dataset_location: str = "US",
        default_table_expiration_ms: int | None = None,
        missing_dataset: bool = False,
        table_mutations: dict[str, TableMutation] | None = None,
    ):
        self.project = project
        self.credentials = credentials
        self.location = location
        self.dataset = bigquery.Dataset(f"{dataset_project}.{dataset_id}")
        self.dataset.location = dataset_location
        self.dataset.default_table_expiration_ms = default_table_expiration_ms
        self.missing_dataset = missing_dataset
        self.table_mutations = table_mutations or {}
        self.resources: dict[str, bigquery.Table] = {}
        self.routines: dict[str, str] = {}
        self.rows: dict[str, list[dict[str, str]]] = {}
        self.query_log: list[str] = []
        self.destructive_calls: list[str] = []

    def get_dataset(self, dataset_ref: str) -> bigquery.Dataset:
        if self.missing_dataset:
            raise NotFound(f"missing fixture dataset: {dataset_ref}")
        return self.dataset

    def query(self, sql: str) -> _QueryJob:
        self.query_log.append(sql)
        if not re.match(r"\s*CREATE\s+OR\s+REPLACE\s+PROCEDURE\b", sql, re.I) and re.search(
            r"\b(?:DELETE|DROP|TRUNCATE|MERGE|INSERT|UPDATE)\b", sql, re.I
        ):
            self.destructive_calls.append(sql)

        alter_match = re.fullmatch(
            r"ALTER TABLE `([^`]+)` ADD COLUMN IF NOT EXISTS "
            r"([A-Za-z_][A-Za-z0-9_]*) (STRING|NUMERIC|BOOL|INT64|ARRAY<STRING>) "
            r"OPTIONS\(description = '([^']*)'\)",
            sql,
        )
        if alter_match:
            resource_id, name, field_type, description = alter_match.groups()
            table = self.resources[resource_id]
            if name not in {field.name for field in table.schema}:
                table.schema = (
                    *table.schema,
                    bigquery.SchemaField(
                        name,
                        "STRING" if field_type == "ARRAY<STRING>" else field_type,
                        mode="REPEATED" if field_type == "ARRAY<STRING>" else "NULLABLE",
                        description=description,
                    ),
                )
            return _QueryJob()

        metadata_match = re.fullmatch(
            r"ALTER TABLE `([^`]+)` ALTER COLUMN `?([A-Za-z_][A-Za-z0-9_]*)`? "
            r"SET OPTIONS \(description = '([^']*)'\)",
            sql,
        )
        if metadata_match:
            resource_id, name, description = metadata_match.groups()
            table = self.resources[resource_id]
            table.schema = tuple(
                bigquery.SchemaField(
                    field.name,
                    field.field_type,
                    mode=field.mode,
                    description=description if field.name == name else field.description,
                    fields=field.fields,
                )
                for field in table.schema
            )
            return _QueryJob()

        table_match = re.match(
            r"\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`([^`]+)`",
            sql,
            re.I,
        )
        if table_match:
            resource_id = table_match.group(1)
            if resource_id not in self.resources:
                if resource_id.endswith(".open_intelligence_quality_review_receipts_v1"):
                    fields = tuple(
                        (name, field_type, "REQUIRED", description, ())
                        for name, field_type, description in re.findall(
                            r"^  ([a-z0-9_]+) (STRING|TIMESTAMP) NOT NULL "
                            r"OPTIONS\(description = '([^']+)'\),?$",
                            sql,
                            re.MULTILINE,
                        )
                    )
                    parsed = {
                        "fields": fields,
                        "description": (
                            "One immutable canonical human quality review receipt per "
                            "replacement run. Natural key: run_id."
                        ),
                        "cluster": (),
                        "partition": None,
                        "expiration": None,
                    }
                else:
                    parsed = parse_table_ddl(sql)
                table = bigquery.Table(resource_id, schema=_schema_fields(parsed["fields"]))
                table._properties["type"] = "TABLE"
                table.description = parsed["description"]
                table.clustering_fields = list(parsed["cluster"])
                table.time_partitioning = (
                    None
                    if resource_id.endswith(".open_intelligence_quality_review_receipts_v1")
                    else bigquery.TimePartitioning(
                        field=_partition_field(parsed["partition"]),
                        expiration_ms=(
                            parsed["expiration"] * 86_400_000
                            if parsed["expiration"] is not None
                            else None
                        ),
                    )
                )
                self.resources[resource_id] = table
                self.rows[resource_id] = [{"fixture": "preserved"}]
            return _QueryJob()

        routine_match = re.match(
            r"\s*CREATE\s+OR\s+REPLACE\s+PROCEDURE\s+`([^`]+)`",
            sql,
            re.I,
        )
        if routine_match:
            self.routines[routine_match.group(1)] = sql
            return _QueryJob()

        view_match = re.match(
            r"\s*CREATE\s+OR\s+REPLACE\s+VIEW\s+`([^`]+)`\s+AS\s+(.*)\Z",
            sql,
            re.I | re.S,
        )
        if view_match:
            resource_id, query = view_match.groups()
            view = bigquery.Table(resource_id)
            view._properties["type"] = "VIEW"
            view.view_query = query.rstrip().removesuffix(";")
            self.resources[resource_id] = view
            return _QueryJob()

        raise AssertionError(f"unexpected query fixture: {sql[:80]}")

    def get_table(self, table_ref: str) -> bigquery.Table:
        resource = bigquery.Table.from_api_repr(deepcopy(self.resources[table_ref].to_api_repr()))
        mutation = self.table_mutations.get(resource.table_id)
        if mutation is not None:
            api_repr = resource.to_api_repr()
            mutation(api_repr)
            resource = bigquery.Table.from_api_repr(api_repr)
        return resource

    def get_routine(self, routine_ref: str):
        sql = self.routines[routine_ref]
        return SimpleNamespace(
            routine_id=routine_ref.rsplit(".", 1)[1],
            type_="PROCEDURE",
            body=sql,
        )


def _client_for_target(target: str, **kwargs) -> StatefulBigQueryClient:
    dataset = STAGING_DATASET if target == "staging" else QA_DATASET
    client = StatefulBigQueryClient(dataset_id=dataset, **kwargs)
    if target == "staging":
        plan = migration.build_plan("staging")
        for table in migration.wave1_authority_upgrade.TABLES:
            table_ref = f"{PROJECT}.{dataset}.{table}"
            parsed = migration._wave1_table_contract(table, plan)
            resource = bigquery.Table(
                table_ref,
                schema=[
                    bigquery.SchemaField(
                        name,
                        _api_type(field_type),
                        mode=mode,
                        description=description,
                    )
                    for name, field_type, mode, description in parsed["fields"][:-8]
                ],
            )
            resource._properties["type"] = "TABLE"
            resource.description = parsed["description"]
            resource.clustering_fields = list(parsed["cluster"])
            resource.time_partitioning = bigquery.TimePartitioning(
                field=_partition_field(parsed["partition"]),
                expiration_ms=(
                    parsed["expiration"] * 86_400_000 if parsed["expiration"] is not None else None
                ),
            )
            client.resources[table_ref] = resource
            client.rows[table_ref] = []
    return client


def _apply(target: str, client: StatefulBigQueryClient) -> None:
    identity = STAGING_IDENTITY if target == "staging" else QA_IDENTITY
    migration.apply_plan(
        migration.build_plan(target),
        credential_loader=_credential_loader(identity),
        client_factory=lambda **kwargs: client,
    )


def _apply_source_lab(client: StatefulBigQueryClient) -> None:
    migration.apply_plan(
        migration.build_source_lab_plan(),
        credential_loader=_credential_loader(STAGING_IDENTITY),
        client_factory=lambda **kwargs: client,
    )


def _expected_forward_queries(plan: migration.MigrationPlan) -> tuple[str, ...]:
    queries = list(plan.wave1_authority_upgrade_statements)
    for statement in plan.statements:
        queries.append(statement.sql)
        if statement.name == migration.source_lab_upgrade.TABLE:
            queries.extend(plan.source_lab_upgrade_statements)
        queries.extend(migration.signal_identity_upgrade.statements_for(plan, statement.name))
    queries.extend(statement.sql for statement in plan.runtime_closure_statements)
    return tuple(queries)


def _signal_identity_alters_through(target: str, last_index: int) -> int:
    if target != "staging":
        return 0
    plan = migration.build_plan("staging")
    return sum(
        len(migration.signal_identity_upgrade.statements_for(plan, name))
        for name in TABLE_NAMES[: last_index + 1]
    )


def _generic_staging_plan_payload(plan: migration.MigrationPlan) -> tuple:
    return (
        plan.target,
        plan.project,
        plan.dataset,
        plan.location,
        plan.service_account,
        tuple((statement.name, statement.kind, statement.sql) for statement in plan.statements),
        plan.source_lab_upgrade_statements,
        plan.wave1_authority_upgrade_statements,
        plan.signal_identity_upgrade_statements,
        tuple(
            (statement.name, statement.kind, statement.sql)
            for statement in plan.runtime_closure_statements
        ),
        tuple(
            (item.principal, item.resource, item.role, item.purpose)
            for item in plan.runtime_closure_iam_bindings
        ),
        plan.rollback_statements,
    )


def _assert_generic_staging_compatibility(plan: migration.MigrationPlan) -> None:
    plan_digest = hashlib.sha256(
        repr(_generic_staging_plan_payload(plan)).encode("utf-8")
    ).hexdigest()
    dry_run_digest = hashlib.sha256(migration.render_dry_run(plan).encode("utf-8")).hexdigest()
    assert plan_digest == _APPROVED_GENERIC_STAGING_PLAN_SHA256
    assert dry_run_digest == _APPROVED_GENERIC_STAGING_DRY_RUN_SHA256


def test_generic_staging_plan_and_dry_run_match_approved_base():
    _assert_generic_staging_compatibility(migration.build_plan("staging"))


def test_generic_staging_compatibility_gate_rejects_forward_mutation():
    plan = migration.build_plan("staging")
    mutated_statement = replace(
        plan.statements[0],
        sql=plan.statements[0].sql.replace("Stable signal identity.", "Changed."),
    )
    mutated_plan = replace(plan, statements=(mutated_statement, *plan.statements[1:]))

    with pytest.raises(AssertionError):
        _assert_generic_staging_compatibility(mutated_plan)


def test_generic_staging_compatibility_gate_rejects_rollback_drop_mutation():
    plan = migration.build_plan("staging")
    mutated_plan = replace(
        plan,
        rollback_statements=(
            *plan.rollback_statements,
            "DROP TABLE IF EXISTS `ogilvy-trends-v2.trends_v2_staging.source_performance_daily_v2`;",
        ),
    )

    with pytest.raises(AssertionError):
        _assert_generic_staging_compatibility(mutated_plan)


def test_migration_execution_artifacts_bind_runtime_closure_statements():
    plan = migration.build_plan("staging")
    artifacts = migration._build_migration_execution_artifacts(plan)
    mutated = replace(
        plan,
        runtime_closure_statements=(
            replace(
                plan.runtime_closure_statements[0],
                sql=plan.runtime_closure_statements[0].sql.replace(
                    "CREATE TABLE", "CREATE TABLEX", 1
                ),
            ),
            *plan.runtime_closure_statements[1:],
        ),
    )
    changed = migration._build_migration_execution_artifacts(mutated)

    assert set(artifacts) == {
        "cloud_build",
        "migration_contract",
        "migration_dry_run",
        "migration_plan",
    }
    assert artifacts["migration_plan"] != changed["migration_plan"]
    assert artifacts["migration_dry_run"] != changed["migration_dry_run"]
    assert artifacts["cloud_build"] == (migration._ROOT / "cloudbuild.staging.yaml").read_bytes()
    assert b"Dockerfile.staging" in artifacts["cloud_build"]
    assert b"gcloud " not in artifacts["cloud_build"]
    assert b"images:" not in artifacts["cloud_build"]
    assert hashlib.sha256(artifacts["cloud_build"]).hexdigest() != (
        "00a6c2b143389f0db0ce17d1c64438008fe9f4f765d81e8fa81fcf2181e0664e"
    )


def test_runtime_closure_iam_plan_is_exact_and_never_widens_to_dataset_or_project() -> None:
    plan = migration.build_plan("staging")
    # Since amendment d r3_apply and r3_release run under intelligence-42-orchestration,
    # so the two read rows collapse to one; the first purpose stands for both.
    assert plan.runtime_closure_iam_bindings == (
        migration.RuntimeIamBinding(
            principal="user:albert.meintjes@ogilvy.co.za",
            resource=(
                "projects/ogilvy-trends-v2/datasets/trends_v2_staging/routines/"
                "sp_register_open_intelligence_quality_review_receipt_v1"
            ),
            role="roles/bigquery.dataViewer",
            purpose="human quality-review registration routine invocation",
        ),
        migration.RuntimeIamBinding(
            principal=(
                "serviceAccount:intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
            ),
            resource=(
                "projects/ogilvy-trends-v2/datasets/trends_v2_staging/routines/"
                "sp_read_open_intelligence_quality_review_receipt_v1"
            ),
            role="roles/bigquery.dataViewer",
            purpose="r3 apply quality-review read routine invocation",
        ),
    )
    assert all("/routines/" in item.resource for item in plan.runtime_closure_iam_bindings)
    assert all("production" not in repr(item).lower() for item in plan.runtime_closure_iam_bindings)
    rendered = migration.render_dry_run(plan)
    assert "Runtime closure IAM plan only:" in rendered
    assert all(item.resource in rendered for item in plan.runtime_closure_iam_bindings)


def test_runtime_closure_iam_plan_is_deduplicated_on_principal_resource_and_role() -> None:
    from scripts.migrations import create_open_intelligence_execution_approval_store as store
    from src.analysis.open_intelligence import execution_origins

    registry = execution_origins.load_origin_registry(
        store.REGISTRY_PATH,
        expected_sha256=store.REGISTRATION_DIGESTS["execution_origins_v1.json"],
        contract_root=store.ROOT,
    )
    apply_identity = migration._v2_principal(registry, "r3_apply")
    assert apply_identity == migration._v2_principal(registry, "r3_release")
    rows = migration._runtime_closure_iam_bindings("trends_v2_staging", registry)
    keys = [(item.principal, item.resource, item.role) for item in rows]
    assert len(keys) == len(set(keys))
    assert len(rows) == 2
    assert rows[1].principal == f"serviceAccount:{apply_identity}"
    assert migration._runtime_closure_iam_bindings("trends_v2", registry) == ()


def _consumed_migration_authority():
    fx = _fixture_v2("migration_apply")
    authority = _load_v2(fx)
    return authority, _consume_v2(fx, authority)


def test_exact_apply_wrapper_loads_consumes_applies_and_records_in_order():
    events = []
    authority, consumption = _consumed_migration_authority()
    result = SimpleNamespace(result_id="exr_" + "d" * 64)

    def load(operation, *, mode):
        events.append("load")
        assert operation == "migration_apply"
        assert mode == "new_consume"
        assert migration._execution_approval_artifact_bytes("migration_plan")
        assert (
            migration._execution_approval_artifact_bytes("cloud_build")
            == (migration._ROOT / "cloudbuild.staging.yaml").read_bytes()
        )
        return authority

    payload = migration._execute_durable_migration(
        authority_loader=load,
        authority_consumer=lambda supplied: events.append("consume") or consumption,
        plan_applier=lambda plan: (
            events.append("apply")
            or tuple(
                statement.name for statement in (*plan.statements, *plan.runtime_closure_statements)
            )
        ),
        result_recorder=lambda *args: events.append("result") or result,
    )

    assert events == ["load", "consume", "apply", "result"]
    assert payload["status"] == "succeeded"
    assert payload["execution_approval"] == {
        "manifest_sha256": authority.approval.manifest_sha256,
        "approval_id": authority.approval.approval_id,
        "consumption_id": consumption.consumption_id,
        "result_id": "exr_" + "d" * 64,
    }
    with pytest.raises(migration.MigrationError, match="unavailable"):
        migration._execution_approval_artifact_bytes("migration_plan")


def test_exact_apply_wrapper_records_failed_terminal_result_after_consumption():
    events = []
    authority, consumption = _consumed_migration_authority()

    def fail_apply(_plan):
        events.append("apply")
        raise RuntimeError("private failure detail")

    def record(*args):
        events.append("result")
        assert args[5] == "failed"
        assert '"status":"failed"' in args[3]
        assert "private failure detail" not in args[3]
        return SimpleNamespace(result_id="exr_" + "d" * 64)

    with pytest.raises(RuntimeError, match="private failure detail"):
        migration._execute_durable_migration(
            authority_loader=lambda operation, **kwargs: events.append("load") or authority,
            authority_consumer=lambda supplied: events.append("consume") or consumption,
            plan_applier=fail_apply,
            result_recorder=record,
        )

    assert events == ["load", "consume", "apply", "result"]
    with pytest.raises(migration.MigrationError, match="unavailable"):
        migration._execution_approval_artifact_bytes("migration_plan")


def test_exact_apply_wrapper_records_partial_accepted_objects_on_failure() -> None:
    authority, consumption = _consumed_migration_authority()
    failure = migration.MigrationError("partial migration failure")
    failure.accepted_objects = ("signal_candidates_v2", "signal_membership_v2")
    recorded = []

    def fail_apply(_plan):
        raise failure

    with pytest.raises(migration.MigrationError, match="partial migration failure"):
        migration._execute_durable_migration(
            authority_loader=lambda _operation, **kwargs: authority,
            authority_consumer=lambda _authority: consumption,
            plan_applier=fail_apply,
            result_recorder=lambda *args: (
                recorded.append(args) or SimpleNamespace(result_id="exr_" + "d" * 64)
            ),
        )

    assert len(recorded) == 1
    assert json.loads(recorded[0][3])["accepted_objects"] == [
        "signal_candidates_v2",
        "signal_membership_v2",
    ]


def test_staging_dry_run_is_complete_and_has_no_client_or_query_side_effect(capsys):
    def fail(*args, **kwargs):
        raise AssertionError("dry run crossed an external boundary")

    migration.main(
        ["--target", "staging"],
        credential_loader=fail,
        client_factory=fail,
        request_factory=fail,
    )
    output = capsys.readouterr().out
    plan = migration.build_plan("staging")
    assert "Project: ogilvy-trends-v2" in output
    assert "Dataset: trends_v2_staging" in output
    assert "Location: US" in output
    assert tuple(statement.name for statement in plan.statements) == (
        *TABLE_NAMES,
        *VIEW_NAMES,
    )
    assert all(statement.sql in output for statement in plan.statements)
    assert all(
        output.index(f"{index}. {name}")
        < output.index(f"{index + 1}. {plan.statements[index].name}")
        for index, name in enumerate(
            (statement.name for statement in plan.statements[:-1]), start=1
        )
    )
    assert "Readback checks on apply:" in output
    assert "Rollback plan only:" in output
    assert "separate explicit destructive authorization" in output


def test_qa_dry_run_has_nine_tables_and_no_views(capsys):
    migration.main(
        ["--target", "qa"],
        credential_loader=lambda: pytest.fail("credentials loaded during dry run"),
        client_factory=lambda **kwargs: pytest.fail("client built during dry run"),
        request_factory=lambda: pytest.fail("request built during dry run"),
    )
    output = capsys.readouterr().out
    plan = migration.build_plan("qa")
    assert tuple(statement.name for statement in plan.statements) == (
        *BASE_TABLE_NAMES,
        "canary_results_v2",
    )
    assert all(statement.kind == "table" for statement in plan.statements)
    assert "CREATE OR REPLACE VIEW" not in output
    assert "partition_expiration_days = 90" in output
    assert "DROP VIEW" not in output
    assert all(view_name not in output for view_name in VIEW_NAMES)
    assert plan.rollback_statements == tuple(
        f"DROP TABLE IF EXISTS `{PROJECT}.{QA_DATASET}.{statement.name}`;"
        for statement in reversed(plan.statements)
        if statement.name not in {"signal_membership_v2", "source_performance_daily_v2"}
    )


@pytest.mark.parametrize(
    ("target", "dataset", "objects"),
    [
        (
            "staging",
            STAGING_DATASET,
            (
                *((name, "table") for name in TABLE_NAMES),
                *((name, "view") for name in VIEW_NAMES),
            ),
        ),
        (
            "qa",
            QA_DATASET,
            (*((name, "table") for name in BASE_TABLE_NAMES), ("canary_results_v2", "table")),
        ),
    ],
)
def test_rollback_preserves_membership_and_source_lab_tables(target, dataset, objects):
    plan = migration.build_plan(target)
    expected = tuple(
        f"DROP {kind.upper()} IF EXISTS `{PROJECT}.{dataset}.{name}`;"
        for name, kind in reversed(objects)
        if name
        not in {
            "signal_membership_v2",
            "source_performance_daily_v2",
            *({"v_source_lab_v2"} if target == "staging" else set()),
        }
    )
    if target == "staging":
        expected = (*expected, *migration.source_lab_upgrade.build_rollback(dataset))
    assert plan.rollback_statements == expected
    assert all("signal_membership_v2" not in sql for sql in plan.rollback_statements)
    assert not any(
        re.search(r"\b(?:DROP|DELETE)\b.*source_performance_daily_v2", sql, re.I)
        for sql in plan.rollback_statements
    )


def test_cli_requires_an_exact_target():
    with pytest.raises(SystemExit):
        migration.main([])
    with pytest.raises(SystemExit):
        migration.main(["--target", "production"])


def test_cli_exposes_no_rollback_execution_flag():
    with pytest.raises(SystemExit):
        migration.main(["--target", "staging", "--rollback"])


def test_source_lab_only_exact_dry_run_command_has_no_external_side_effects(capsys):
    def fail(*args, **kwargs):
        raise AssertionError("Source Lab dry run crossed an external boundary")

    migration.main(
        ["--target", "staging", "--source-lab-only"],
        credential_loader=fail,
        client_factory=fail,
        request_factory=fail,
    )

    output = capsys.readouterr().out
    assert "Writer state: disabled" in output
    assert "1. source_performance_daily_v2 (table)" in output
    assert "2. v_source_lab_v2 (view)" in output
    assert "v_signal_board_v2" not in output


def test_source_lab_dry_run_renders_metadata_repairs_with_one_terminator_each():
    output_lines = migration.render_dry_run(migration.build_source_lab_plan()).splitlines()
    route_role_repair = (
        "ALTER TABLE `ogilvy-trends-v2.trends_v2_staging.source_performance_daily_v2` "
        "ALTER COLUMN route_role SET OPTIONS (description = 'Evidence, utility_balance, "
        "utility_catalog, identity_discovery, identity_hygiene, or inventory.');"
    )
    status_repair = (
        "ALTER TABLE `ogilvy-trends-v2.trends_v2_staging.source_performance_daily_v2` "
        "ALTER COLUMN status SET OPTIONS (description = 'Active, pilot, available_unwired, "
        "blocked, permanently_rejected, or inventory_only.');"
    )

    assert output_lines.count(route_role_repair) == 1
    assert output_lines.count(status_repair) == 1
    assert output_lines.index(route_role_repair) < output_lines.index(status_repair)
    assert f"{route_role_repair};" not in output_lines
    assert f"{status_repair};" not in output_lines


def test_source_lab_only_rejects_qa_target(capsys):
    with pytest.raises(SystemExit):
        migration.main(["--target", "qa", "--source-lab-only"])

    assert "only supports --target staging" in capsys.readouterr().err


def test_source_lab_plan_isolated_and_has_a_zero_drop_prior_view_rollback():
    generic_before = migration.build_plan("staging")

    plan = migration.build_source_lab_plan()

    assert plan.target == "staging"
    assert (plan.project, plan.dataset, plan.location, plan.service_account) == (
        PROJECT,
        STAGING_DATASET,
        "US",
        STAGING_IDENTITY,
    )
    assert tuple((statement.name, statement.kind) for statement in plan.statements) == (
        ("source_performance_daily_v2", "table"),
        ("v_source_lab_v2", "view"),
    )
    assert len(plan.source_lab_upgrade_statements) == 20
    assert all("ADD COLUMN IF NOT EXISTS" in sql for sql in plan.source_lab_upgrade_statements[:14])
    assert plan.source_lab_upgrade_statements[14] == (
        "ALTER TABLE `ogilvy-trends-v2.trends_v2_staging.source_performance_daily_v2` "
        "ALTER COLUMN route_role SET OPTIONS (description = 'Evidence, utility_balance, "
        "utility_catalog, identity_discovery, identity_hygiene, or inventory.')"
    )
    assert plan.source_lab_upgrade_statements[15] == (
        "ALTER TABLE `ogilvy-trends-v2.trends_v2_staging.source_performance_daily_v2` "
        "ALTER COLUMN status SET OPTIONS (description = 'Active, pilot, available_unwired, "
        "blocked, permanently_rejected, or inventory_only.')"
    )
    assert plan.source_lab_upgrade_statements[16] == (
        "ALTER TABLE `ogilvy-trends-v2.trends_v2_staging.source_performance_daily_v2` "
        "ALTER COLUMN kill_test_result SET OPTIONS (description = 'Required for permanently_rejected: the kill test reason, equal to blocking_reason.')"
    )
    assert plan.source_lab_upgrade_statements[17] == (
        "ALTER TABLE `ogilvy-trends-v2.trends_v2_staging.source_performance_daily_v2` "
        "ALTER COLUMN review_date SET OPTIONS (description = 'Null. Reserved for a reviewed rejection, which no writer produces.')"
    )
    assert plan.source_lab_upgrade_statements[18] == (
        "ALTER TABLE `ogilvy-trends-v2.trends_v2_staging.source_performance_daily_v2` "
        "ALTER COLUMN `rows` SET OPTIONS (description = 'Distinct items the route returned; present for active and permanently_rejected inventory routes. For identity roles, normalized result count after deterministic normalization and deduplication.')"
    )
    assert plan.source_lab_upgrade_statements[19] == (
        "ALTER TABLE `ogilvy-trends-v2.trends_v2_staging.source_performance_daily_v2` "
        "ALTER COLUMN unique_lift SET OPTIONS (description = 'Marginal downstream row count, candidate terms plus evidence rows only this route produced; present for active and permanently_rejected inventory routes.')"
    )
    assert len(plan.rollback_statements) == 1
    assert "CREATE OR REPLACE VIEW" in plan.rollback_statements[0]
    assert not any(re.search(r"\bDROP\b", sql, re.I) for sql in plan.rollback_statements)
    assert migration.build_plan("staging") == generic_before


def test_source_lab_apply_routes_the_isolated_plan_through_apply_plan(capsys):
    client = _client_for_target("staging")

    migration.main(
        ["--target", "staging", "--source-lab-only", "--apply"],
        credential_loader=_credential_loader(STAGING_IDENTITY),
        client_factory=lambda **kwargs: client,
    )

    plan = migration.build_source_lab_plan()
    assert tuple(client.query_log) == (
        plan.statements[0].sql,
        *plan.source_lab_upgrade_statements,
        plan.statements[1].sql,
    )
    assert client.destructive_calls == []
    assert "Accepted 2 objects in order: source_performance_daily_v2, v_source_lab_v2" in (
        capsys.readouterr().out
    )


def _assert_plan_rejected_before_boundaries(plan, match: str) -> None:
    credential_calls: list[str] = []
    client_calls: list[dict] = []

    def credential_loader():
        credential_calls.append("called")
        return _service_credentials(STAGING_IDENTITY), PROJECT

    def client_factory(**kwargs):
        client_calls.append(kwargs)
        return _client_for_target("staging")

    with pytest.raises(migration.MigrationError, match=match):
        migration.apply_plan(
            plan,
            credential_loader=credential_loader,
            client_factory=client_factory,
        )
    assert credential_calls == []
    assert client_calls == []


def test_quoted_dynamic_drop_statement_is_rejected_before_credentials():
    plan = migration.build_plan("staging")
    view_index = len(TABLE_NAMES)
    quoted_drop = (
        f"EXECUTE IMMEDIATE 'DROP TABLE `{PROJECT}.{STAGING_DATASET}.signal_candidates_v2`';"
    )
    mutated = replace(
        plan.statements[view_index],
        sql=f"{plan.statements[view_index].sql}\n{quoted_drop}",
    )
    _assert_plan_rejected_before_boundaries(
        replace(
            plan,
            statements=(
                *plan.statements[:view_index],
                mutated,
                *plan.statements[view_index + 1 :],
            ),
        ),
        "exactly one terminal semicolon",
    )


def test_second_statement_is_rejected_before_credentials():
    plan = migration.build_plan("staging")
    view_index = len(TABLE_NAMES)
    mutated = replace(
        plan.statements[view_index],
        sql=f"{plan.statements[view_index].sql}\nSELECT 1;",
    )
    _assert_plan_rejected_before_boundaries(
        replace(
            plan,
            statements=(
                *plan.statements[:view_index],
                mutated,
                *plan.statements[view_index + 1 :],
            ),
        ),
        "exactly one terminal semicolon",
    )


@pytest.mark.parametrize("comment", ["-- comment", "# comment", "/* comment */"])
def test_sql_comments_are_rejected_before_credentials(comment):
    plan = migration.build_plan("staging")
    view_index = len(TABLE_NAMES)
    sql = plan.statements[view_index].sql.rstrip().removesuffix(";")
    mutated = replace(
        plan.statements[view_index],
        sql=f"{sql}\n{comment}\n;",
    )
    _assert_plan_rejected_before_boundaries(
        replace(
            plan,
            statements=(
                *plan.statements[:view_index],
                mutated,
                *plan.statements[view_index + 1 :],
            ),
        ),
        "SQL comments",
    )


def test_single_statement_parser_rejects_dynamic_sql():
    with pytest.raises(migration.MigrationError, match="scripting construct"):
        migration.validate_single_statement_sql("EXECUTE IMMEDIATE 'SELECT 1';")


def test_single_statement_parser_preserves_quoted_semicolons():
    migration.validate_single_statement_sql(
        "CREATE VIEW `p.d.v` AS SELECT 'Description keeps; quoted punctuation.' AS description;"
    )


@pytest.mark.parametrize("source_path", _ACCEPTED_SOURCE_PATHS)
def test_source_digest_mismatch_stops_before_credentials(monkeypatch, source_path):
    original_read_bytes = Path.read_bytes
    mutated_path = (_ROOT / source_path).resolve()

    def read_bytes(path: Path) -> bytes:
        content = original_read_bytes(path)
        return content + b"\n" if path.resolve() == mutated_path else content

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    target = "qa" if source_path.endswith("canary_results_v2.sql") else "staging"
    identity = QA_IDENTITY if target == "qa" else STAGING_IDENTITY
    credential_calls: list[str] = []
    client_calls: list[dict] = []

    def credential_loader():
        credential_calls.append("called")
        return _service_credentials(identity), PROJECT

    def client_factory(**kwargs):
        client_calls.append(kwargs)
        return _client_for_target(target)

    with pytest.raises(migration.MigrationError, match="source digest mismatch"):
        migration.main(
            ["--target", target, "--apply"],
            credential_loader=credential_loader,
            client_factory=client_factory,
        )
    assert credential_calls == []
    assert client_calls == []


def test_membership_source_digest_is_full_and_matches_final_schema_bytes():
    source_path = _ROOT / _MEMBERSHIP_SOURCE_PATH
    digest = migration._SOURCE_SHA256.get(_MEMBERSHIP_SOURCE_PATH)
    assert digest is not None
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == digest


def _assert_main_rejected_before_boundaries(target: str, match: str) -> None:
    identity = QA_IDENTITY if target == "qa" else STAGING_IDENTITY
    credential_calls: list[str] = []
    client_calls: list[dict] = []

    def credential_loader():
        credential_calls.append("called")
        return _service_credentials(identity), PROJECT

    def client_factory(**kwargs):
        client_calls.append(kwargs)
        return _client_for_target(target)

    with pytest.raises(migration.MigrationError, match=match):
        migration.main(
            ["--target", target, "--apply"],
            credential_loader=credential_loader,
            client_factory=client_factory,
        )
    assert credential_calls == []
    assert client_calls == []


def test_qa_retention_helper_drift_stops_before_credentials(monkeypatch):
    accepted_helper = migration._with_partition_expiration

    def thirty_day_retention(sql: str, _days: int) -> str:
        return accepted_helper(sql, 30)

    monkeypatch.setattr(migration, "_with_partition_expiration", thirty_day_retention)
    _assert_main_rejected_before_boundaries("qa", "target retention invariant")


def test_staging_expiry_injection_stops_before_credentials(monkeypatch):
    accepted_renderer = migration._render_table_sql

    def expiring_renderer(name, target, dataset, sources):
        sql = accepted_renderer(name, target, dataset, sources)
        if target != "staging":
            return sql
        marker = "\nOPTIONS (\n"
        return sql.replace(
            marker,
            f"{marker}  partition_expiration_days = 1,\n",
            1,
        )

    monkeypatch.setattr(migration, "_render_table_sql", expiring_renderer)
    _assert_main_rejected_before_boundaries("staging", "target retention invariant")


@pytest.mark.parametrize("target", ["staging", "qa"])
def test_nominal_plan_has_literal_target_retention(target):
    plan = migration.build_plan(target)
    table_retention = {
        statement.name: parse_table_ddl(statement.sql)["expiration"]
        for statement in plan.statements
        if statement.kind == "table"
    }
    expected = {
        name: 90 if target == "qa" else 400 if name == "signal_membership_v2" else None
        for name in table_retention
    }
    assert table_retention == expected


def test_default_compute_identity_refreshes_to_exact_staging_identity_before_client(
    monkeypatch,
):
    credentials = compute_credentials.Credentials(
        service_account_email="default",
        quota_project_id=PROJECT,
    )
    request = object()
    refresh_calls: list[object] = []
    client_calls: list[dict] = []

    def refresh(actual_request):
        refresh_calls.append(actual_request)
        credentials._service_account_email = STAGING_IDENTITY

    monkeypatch.setattr(credentials, "refresh", refresh)
    client = _client_for_target("staging")

    def client_factory(**kwargs):
        client_calls.append(kwargs)
        return client

    migration.apply_plan(
        migration.build_plan("staging"),
        credential_loader=lambda: (credentials, PROJECT),
        client_factory=client_factory,
        request_factory=lambda: request,
    )

    assert refresh_calls == [request]
    assert client_calls == [
        {
            "project": PROJECT,
            "credentials": credentials,
            "location": "US",
        }
    ]


@pytest.mark.parametrize(
    ("target", "resolved_identity"),
    [("staging", STAGING_IDENTITY), ("qa", QA_IDENTITY)],
)
@pytest.mark.parametrize(
    "initial_identity",
    ["default", None, ""],
    ids=["default", "missing", "empty"],
)
def test_deferred_compute_identity_refreshes_once_to_exact_approved_email(
    monkeypatch, initial_identity, target, resolved_identity
):
    credentials = compute_credentials.Credentials(
        service_account_email=initial_identity,
        quota_project_id=None,
    )
    request = object()
    refresh_calls: list[object] = []

    def refresh(actual_request):
        refresh_calls.append(actual_request)
        credentials._service_account_email = resolved_identity

    monkeypatch.setattr(credentials, "refresh", refresh)
    accepted = migration._authorized_credentials(
        migration.build_plan(target),
        lambda: (credentials, PROJECT),
        lambda: request,
    )

    assert accepted is credentials
    assert refresh_calls == [request]


@pytest.mark.parametrize(
    ("target", "identity"),
    [("staging", STAGING_IDENTITY), ("qa", QA_IDENTITY)],
)
@pytest.mark.parametrize("quota_project", [None, PROJECT])
def test_explicit_exact_compute_identity_needs_no_refresh(
    monkeypatch, quota_project, target, identity
):
    credentials = compute_credentials.Credentials(
        service_account_email=identity,
        quota_project_id=quota_project,
    )
    monkeypatch.setattr(
        credentials,
        "refresh",
        lambda _request: pytest.fail("explicit compute identity was refreshed"),
    )
    accepted = migration._authorized_credentials(
        migration.build_plan(target),
        lambda: (credentials, PROJECT),
        lambda: pytest.fail("request constructed for explicit identity"),
    )
    assert accepted is credentials


class _SpoofCredentials:
    service_account_email = STAGING_IDENTITY
    quota_project_id = PROJECT

    def refresh(self, _request):
        raise AssertionError("spoof refresh must not run")


class _ServiceCredentialSubclass(service_account.Credentials):
    pass


class _ComputeCredentialSubclass(compute_credentials.Credentials):
    pass


def _service_subclass_credentials():
    return _ServiceCredentialSubclass(
        signer=_Signer(),
        service_account_email=STAGING_IDENTITY,
        token_uri="https://example.invalid/token",
        project_id=PROJECT,
        quota_project_id=PROJECT,
    )


def _impersonated_staging_credentials():
    return impersonated_credentials.Credentials(
        source_credentials=_service_credentials(STAGING_IDENTITY),
        target_principal=STAGING_IDENTITY,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )


@pytest.mark.parametrize(
    "credentials",
    [
        user_credentials.Credentials(token="fixture-user-token"),
        auth_credentials.AnonymousCredentials(),
        _impersonated_staging_credentials(),
        _SpoofCredentials(),
        _service_subclass_credentials(),
        _ComputeCredentialSubclass(
            service_account_email=STAGING_IDENTITY,
            quota_project_id=PROJECT,
        ),
        compute_credentials.Credentials(
            service_account_email="wrong@ogilvy-trends-v2.iam.gserviceaccount.com"
        ),
        _service_credentials("another@ogilvy-trends-v2.iam.gserviceaccount.com"),
    ],
    ids=(
        "user-adc",
        "anonymous",
        "impersonated",
        "generic-spoof",
        "service-subclass",
        "compute-subclass",
        "wrong-compute",
        "wrong-service-account",
    ),
)
def test_identity_rejection_happens_before_client_construction(credentials):
    constructed = False

    def client_factory(**kwargs):
        nonlocal constructed
        constructed = True
        raise AssertionError("client must not be constructed")

    with pytest.raises(migration.MigrationError, match="service account identity"):
        migration.apply_plan(
            migration.build_plan("staging"),
            credential_loader=lambda: (credentials, PROJECT),
            client_factory=client_factory,
        )
    assert constructed is False


def test_default_compute_subclass_is_rejected_without_refresh_or_client(monkeypatch):
    credentials = _ComputeCredentialSubclass(service_account_email="default")
    monkeypatch.setattr(
        credentials,
        "refresh",
        lambda _request: pytest.fail("compute subclass was refreshed"),
    )
    with pytest.raises(migration.MigrationError, match="service account identity"):
        migration.apply_plan(
            migration.build_plan("staging"),
            credential_loader=lambda: (credentials, PROJECT),
            client_factory=lambda **kwargs: pytest.fail("client constructed"),
            request_factory=lambda: pytest.fail("request constructed"),
        )


@pytest.mark.parametrize(
    ("resolved_identity", "refresh_error", "message"),
    [
        ("default", None, "identity mismatch"),
        (None, None, "identity mismatch"),
        (
            "wrong@ogilvy-trends-v2.iam.gserviceaccount.com",
            None,
            "identity mismatch",
        ),
        (None, RuntimeError("sëcret-token-must-not-leak"), "identity refresh failed"),
    ],
    ids=["still-default", "still-missing", "wrong-email", "refresh-failed"],
)
def test_deferred_compute_refresh_failure_stops_before_client_without_raw_error(
    monkeypatch, resolved_identity, refresh_error, message
):
    credentials = compute_credentials.Credentials(service_account_email="default")
    request = object()
    client_calls: list[dict] = []

    def refresh(actual_request):
        assert actual_request is request
        if refresh_error is not None:
            raise refresh_error
        credentials._service_account_email = resolved_identity

    monkeypatch.setattr(credentials, "refresh", refresh)

    with pytest.raises(migration.MigrationError, match=message) as exc_info:
        migration.apply_plan(
            migration.build_plan("staging"),
            credential_loader=lambda: (credentials, PROJECT),
            client_factory=lambda **kwargs: client_calls.append(kwargs),
            request_factory=lambda: request,
        )
    assert "sëcret-token-must-not-leak" not in str(exc_info.value)
    assert client_calls == []


def test_compute_request_factory_failure_is_sanitized_before_client():
    credentials = compute_credentials.Credentials(service_account_email="default")
    client_calls: list[dict] = []

    def request_factory():
        raise RuntimeError("tøkën-from-request-factory")

    with pytest.raises(migration.MigrationError, match="identity refresh failed") as exc_info:
        migration.apply_plan(
            migration.build_plan("staging"),
            credential_loader=lambda: (credentials, PROJECT),
            client_factory=lambda **kwargs: client_calls.append(kwargs),
            request_factory=request_factory,
        )
    assert "tøkën-from-request-factory" not in str(exc_info.value)
    assert client_calls == []


def test_wrong_compute_quota_fails_before_refresh_or_client(monkeypatch):
    credentials = compute_credentials.Credentials(
        service_account_email="default",
        quota_project_id="wrong-quota-project",
    )
    monkeypatch.setattr(
        credentials,
        "refresh",
        lambda _request: pytest.fail("wrong quota reached refresh"),
    )
    with pytest.raises(migration.MigrationError, match="quota project"):
        migration.apply_plan(
            migration.build_plan("staging"),
            credential_loader=lambda: (credentials, PROJECT),
            client_factory=lambda **kwargs: pytest.fail("client constructed"),
            request_factory=lambda: pytest.fail("request constructed"),
        )


def test_wrong_compute_adc_project_fails_before_refresh_or_client(monkeypatch):
    credentials = compute_credentials.Credentials(service_account_email="default")
    monkeypatch.setattr(
        credentials,
        "refresh",
        lambda _request: pytest.fail("wrong ADC project reached refresh"),
    )
    with pytest.raises(migration.MigrationError, match="ADC project"):
        migration.apply_plan(
            migration.build_plan("staging"),
            credential_loader=lambda: (credentials, "wrong-project"),
            client_factory=lambda **kwargs: pytest.fail("client constructed"),
            request_factory=lambda: pytest.fail("request constructed"),
        )


def test_wrong_adc_project_fails_before_client_construction():
    with pytest.raises(migration.MigrationError, match="ADC project"):
        migration.apply_plan(
            migration.build_plan("staging"),
            credential_loader=lambda: (
                _service_credentials(STAGING_IDENTITY),
                "wrong-project",
            ),
            client_factory=lambda **kwargs: pytest.fail("client constructed"),
        )


@pytest.mark.parametrize(
    ("target", "identity"),
    [("staging", STAGING_IDENTITY), ("qa", QA_IDENTITY)],
)
def test_wrong_quota_project_fails_before_client_construction(target, identity):
    client_calls: list[dict] = []

    def client_factory(**kwargs):
        client_calls.append(kwargs)
        return _client_for_target(target)

    with pytest.raises(migration.MigrationError, match="quota project"):
        migration.apply_plan(
            migration.build_plan(target),
            credential_loader=lambda: (
                _service_credentials(identity, quota_project_id="wrong-quota-project"),
                PROJECT,
            ),
            client_factory=client_factory,
        )
    assert client_calls == []


def test_client_factory_receives_exact_authorized_boundary():
    plan = migration.build_plan("staging")
    credentials = _service_credentials(STAGING_IDENTITY)
    client = _client_for_target("staging")
    calls: list[dict] = []

    def client_factory(**kwargs):
        calls.append(kwargs)
        return client

    migration.apply_plan(
        plan,
        credential_loader=lambda: (credentials, PROJECT),
        client_factory=client_factory,
    )

    assert len(calls) == 1
    assert calls[0] == {
        "project": PROJECT,
        "credentials": credentials,
        "location": "US",
    }
    assert calls[0]["credentials"] is credentials


def test_missing_dataset_stops_before_any_query():
    client = _client_for_target("staging", missing_dataset=True)
    with pytest.raises(migration.MigrationError, match="dataset does not exist"):
        _apply("staging", client)
    assert client.query_log == []


@pytest.mark.parametrize(
    ("kwargs", "failed_check"),
    [
        ({"dataset_project": "wrong-project"}, "dataset project"),
        ({"dataset_id": "wrong_dataset"}, "dataset id"),
        ({"dataset_location": "EU"}, "dataset location"),
        ({"default_table_expiration_ms": 86_400_000}, "default table expiry"),
    ],
)
def test_wrong_dataset_metadata_stops_before_any_query(kwargs, failed_check):
    client = StatefulBigQueryClient(**kwargs)
    with pytest.raises(migration.MigrationError, match=failed_check):
        _apply("staging", client)
    assert client.query_log == []


def _mutate_first_field(key: str, value):
    def mutate(resource: dict) -> None:
        resource["schema"]["fields"][0][key] = value

    return mutate


def _mutate_nested_mode(resource: dict) -> None:
    promotion_target = next(
        field for field in resource["schema"]["fields"] if field["name"] == "promotion_target"
    )
    promotion_target["fields"][0]["mode"] = "NULLABLE"


def _mutate_field_order(resource: dict) -> None:
    fields = resource["schema"]["fields"]
    fields[0], fields[1] = fields[1], fields[0]


def _remove_nested_field(resource: dict) -> None:
    promotion_target = next(
        field for field in resource["schema"]["fields"] if field["name"] == "promotion_target"
    )
    promotion_target["fields"].pop()


def _mutate_partition(resource: dict) -> None:
    resource["timePartitioning"]["field"] = "created_at"


def _mutate_retention(resource: dict) -> None:
    resource["timePartitioning"]["expirationMs"] = "86400000"


def _mutate_clustering(resource: dict) -> None:
    resource["clustering"]["fields"].reverse()


def _mutate_table_description(resource: dict) -> None:
    resource["description"] = "Wrong table description."


def _mutate_table_type(resource: dict) -> None:
    resource["type"] = "VIEW"


@pytest.mark.parametrize(
    ("target", "table_name", "mutation", "failed_check"),
    [
        ("staging", "signal_membership_v2", _mutate_first_field("name", "wrong"), "field name"),
        ("staging", "signal_membership_v2", _mutate_first_field("type", "BYTES"), "field type"),
        ("staging", "signal_membership_v2", _mutate_first_field("mode", "NULLABLE"), "field mode"),
        (
            "staging",
            "signal_membership_v2",
            _mutate_first_field("description", "Wrong."),
            "field description",
        ),
        ("staging", "signal_membership_v2", _mutate_field_order, "field name"),
        ("staging", "signal_predictions_v2", _mutate_nested_mode, "field mode"),
        ("staging", "signal_predictions_v2", _remove_nested_field, "nested fields"),
        ("staging", "signal_membership_v2", _mutate_partition, "partition field"),
        ("staging", "signal_membership_v2", _mutate_retention, "partition retention"),
        ("qa", "signal_membership_v2", _mutate_retention, "partition retention"),
        ("staging", "signal_membership_v2", _mutate_clustering, "clustering order"),
        ("staging", "signal_membership_v2", _mutate_table_description, "table description"),
        ("staging", "signal_membership_v2", _mutate_table_type, "table type"),
        ("qa", "signal_membership_v2", _mutate_first_field("name", "wrong"), "field name"),
        ("qa", "signal_membership_v2", _mutate_first_field("type", "BYTES"), "field type"),
        ("qa", "signal_membership_v2", _mutate_first_field("mode", "NULLABLE"), "field mode"),
        (
            "qa",
            "signal_membership_v2",
            _mutate_first_field("description", "Wrong."),
            "field description",
        ),
        ("qa", "signal_membership_v2", _mutate_field_order, "field name"),
        ("qa", "signal_membership_v2", _mutate_partition, "partition field"),
        ("qa", "signal_membership_v2", _mutate_clustering, "clustering order"),
        ("qa", "signal_membership_v2", _mutate_table_description, "table description"),
        ("qa", "signal_membership_v2", _mutate_table_type, "table type"),
    ],
    ids=(
        "field-name",
        "field-type",
        "field-mode",
        "field-description",
        "field-order",
        "nested-mode",
        "nested-fields",
        "partition",
        "staging-retention",
        "qa-retention",
        "clustering",
        "table-description",
        "table-type",
        "qa-field-name",
        "qa-field-type",
        "qa-field-mode",
        "qa-field-description",
        "qa-field-order",
        "qa-partition",
        "qa-clustering",
        "qa-table-description",
        "qa-table-type",
    ),
)
def test_table_readback_rejects_every_schema_mismatch_class(
    target, table_name, mutation, failed_check
):
    client = _client_for_target(target, table_mutations={table_name: mutation})
    with pytest.raises(migration.MigrationError, match=failed_check) as exc_info:
        _apply(target, client)
    failed_index = TABLE_NAMES.index(table_name)
    last_accepted = TABLE_NAMES[failed_index - 1] if failed_index else "dataset metadata"
    assert f"last accepted object: {last_accepted}" in str(exc_info.value)
    assert getattr(exc_info.value, "accepted_objects", None) == TABLE_NAMES[:failed_index]
    upgrade_count = (
        len(migration.build_plan("staging").wave1_authority_upgrade_statements)
        if target == "staging"
        else 0
    )
    assert len(client.query_log) == (
        upgrade_count + failed_index + 1 + _signal_identity_alters_through(target, failed_index)
    )
    assert not any("CREATE OR REPLACE VIEW" in sql for sql in client.query_log)


def _mutate_view_query(resource: dict) -> None:
    resource["view"]["query"] += " WHERE FALSE"


def _mutate_view_type(resource: dict) -> None:
    resource["type"] = "TABLE"


@pytest.mark.parametrize(
    ("mutation", "failed_check"),
    [
        (_mutate_view_query, "view query"),
        (_mutate_view_type, "view type"),
    ],
)
def test_view_readback_rejects_wrong_query_or_type_and_stops(mutation, failed_check):
    client = _client_for_target("staging", table_mutations={VIEW_NAMES[0]: mutation})
    with pytest.raises(migration.MigrationError, match=failed_check) as exc_info:
        _apply("staging", client)
    assert f"last accepted object: {TABLE_NAMES[-1]}" in str(exc_info.value)
    assert len(client.query_log) == (
        len(migration.build_plan("staging").wave1_authority_upgrade_statements)
        + len(migration.build_plan("staging").signal_identity_upgrade_statements)
        + len(TABLE_NAMES)
        + 14
        + 1
    )
    assert not any(VIEW_NAMES[1] in sql for sql in client.query_log)


def test_apply_runs_exact_order_and_reapply_preserves_rows():
    client = _client_for_target("staging")
    _apply("staging", client)
    first_rows = deepcopy(client.rows)
    first_queries = tuple(client.query_log)

    _apply("staging", client)

    plan = migration.build_plan("staging")
    expected = _expected_forward_queries(plan)
    assert first_queries == expected
    assert tuple(client.query_log) == (*expected, *expected)
    assert client.rows == first_rows
    assert client.destructive_calls == []
    assert tuple(client.resources) == tuple(
        f"{PROJECT}.{STAGING_DATASET}.{name}"
        for name in (
            *migration.wave1_authority_upgrade.TABLES,
            *TABLE_NAMES,
            *VIEW_NAMES,
            "open_intelligence_quality_review_receipts_v1",
        )
    )


def test_generic_source_lab_upgrade_uses_eleven_idempotent_additive_alters():
    statements = migration.source_lab_upgrade.build_statements()

    assert len(statements) == 14
    assert all("ALTER TABLE" in sql for sql in statements)
    assert all("ADD COLUMN IF NOT EXISTS" in sql for sql in statements)
    assert all("DROP" not in sql and "DELETE" not in sql for sql in statements)


def test_source_lab_rollback_preserves_existing_table_rows_and_columns():
    rollback = migration.build_plan("staging").rollback_statements

    assert any("CREATE OR REPLACE VIEW" in instruction for instruction in rollback)
    assert not any(
        re.search(r"\b(?:DROP|DELETE)\b.*source_performance_daily_v2", instruction, re.I)
        for instruction in rollback
    )
    assert all(
        "DROP VIEW IF EXISTS `ogilvy-trends-v2.trends_v2_staging.v_source_lab_v2`"
        not in instruction
        for instruction in rollback
    )


def test_source_lab_upgrade_starts_from_legacy_table_and_preserves_rows_on_reapply():
    client = _client_for_target("staging")
    table_ref = f"{PROJECT}.{STAGING_DATASET}.source_performance_daily_v2"
    source_statement = next(
        statement
        for statement in migration.build_plan("staging").statements
        if statement.name == "source_performance_daily_v2"
    )
    parsed = parse_table_ddl(source_statement.sql)
    additive_names = {name for name, _, _ in migration.source_lab_upgrade.COLUMNS}
    legacy_fields = tuple(field for field in parsed["fields"] if field[0] not in additive_names)
    legacy_table = bigquery.Table(
        table_ref,
        schema=_schema_fields(legacy_fields),
    )
    legacy_table._properties["type"] = "TABLE"
    legacy_table.description = parsed["description"]
    legacy_table.clustering_fields = list(parsed["cluster"])
    legacy_table.time_partitioning = bigquery.TimePartitioning(
        field=_partition_field(parsed["partition"]),
        expiration_ms=None,
    )
    client.resources[table_ref] = legacy_table
    client.rows[table_ref] = [{"fixture": "preserved"}]
    before = deepcopy(client.rows[table_ref])

    _apply("staging", client)
    first_schema = tuple(field.name for field in client.get_table(table_ref).schema)
    _apply("staging", client)

    assert client.rows[table_ref] == before
    assert tuple(field.name for field in client.get_table(table_ref).schema) == first_schema
    assert first_schema == tuple(name for name, *_ in parsed["fields"])
    assert first_schema[-14:] == tuple(name for name, _, _ in migration.source_lab_upgrade.COLUMNS)
    plan = migration.build_plan("staging")
    first_apply = tuple(client.query_log[: len(_expected_forward_queries(plan))])
    source_index = first_apply.index(source_statement.sql)
    assert first_apply[source_index + 1 : source_index + 15] == plan.source_lab_upgrade_statements


def test_source_lab_repairs_retired_route_role_description_before_view_readback():
    client = _client_for_target("staging")
    table_ref = f"{PROJECT}.{STAGING_DATASET}.source_performance_daily_v2"
    source_statement = next(
        statement
        for statement in migration.build_plan("staging").statements
        if statement.name == "source_performance_daily_v2"
    )
    parsed = parse_table_ddl(source_statement.sql)
    retired_description = (
        "Evidence, utility_balance, utility_catalog, identity_discovery, or identity_hygiene."
    )
    stale_fields = tuple(
        (*field[:3], retired_description, field[4]) if field[0] == "route_role" else field
        for field in parsed["fields"]
    )
    table = bigquery.Table(table_ref, schema=_schema_fields(stale_fields))
    table._properties["type"] = "TABLE"
    table.description = parsed["description"]
    table.clustering_fields = list(parsed["cluster"])
    table.time_partitioning = bigquery.TimePartitioning(
        field=_partition_field(parsed["partition"]),
        expiration_ms=None,
    )
    client.resources[table_ref] = table
    client.rows[table_ref] = [{"fixture": "preserved"}]

    _apply_source_lab(client)

    expected = (
        "Evidence, utility_balance, utility_catalog, identity_discovery, identity_hygiene, "
        "or inventory."
    )
    route_role = next(
        field for field in client.get_table(table_ref).schema if field.name == "route_role"
    )
    assert route_role.description == expected
    repair = (
        f"ALTER TABLE `{table_ref}` ALTER COLUMN route_role SET OPTIONS "
        f"(description = '{expected}')"
    )
    assert client.query_log.index(repair) < client.query_log.index(
        migration.build_source_lab_plan().statements[1].sql
    )


def test_source_lab_repairs_retired_status_description_before_view_readback():
    client = _client_for_target("staging")
    table_ref = f"{PROJECT}.{STAGING_DATASET}.source_performance_daily_v2"
    source_statement = next(
        statement
        for statement in migration.build_plan("staging").statements
        if statement.name == "source_performance_daily_v2"
    )
    parsed = parse_table_ddl(source_statement.sql)
    retired_description = "Active, pilot, available_unwired, blocked, or rejected."
    stale_fields = tuple(
        (*field[:3], retired_description, field[4]) if field[0] == "status" else field
        for field in parsed["fields"]
    )
    table = bigquery.Table(table_ref, schema=_schema_fields(stale_fields))
    table._properties["type"] = "TABLE"
    table.description = parsed["description"]
    table.clustering_fields = list(parsed["cluster"])
    table.time_partitioning = bigquery.TimePartitioning(
        field=_partition_field(parsed["partition"]),
        expiration_ms=None,
    )
    client.resources[table_ref] = table
    client.rows[table_ref] = [{"fixture": "preserved"}]

    _apply_source_lab(client)

    expected = "Active, pilot, available_unwired, blocked, permanently_rejected, or inventory_only."
    status = next(field for field in client.get_table(table_ref).schema if field.name == "status")
    assert status.description == expected
    repair = (
        f"ALTER TABLE `{table_ref}` ALTER COLUMN status SET OPTIONS (description = '{expected}')"
    )
    assert client.query_log.index(repair) < client.query_log.index(
        migration.build_source_lab_plan().statements[1].sql
    )


def test_qa_apply_creates_no_views():
    client = _client_for_target("qa")
    _apply("qa", client)
    assert len(client.query_log) == len(BASE_TABLE_NAMES) + 1
    assert not any("CREATE OR REPLACE VIEW" in sql for sql in client.query_log)


def test_unquoted_destructive_second_statement_is_rejected_before_credentials():
    plan = migration.build_plan("staging")
    mutated_statement = replace(
        plan.statements[0],
        sql=f"{plan.statements[0].sql}\nDELETE FROM `{PROJECT}.{STAGING_DATASET}.signal_candidates_v2`;",
    )
    mutated_plan = replace(
        plan,
        statements=(mutated_statement, *plan.statements[1:]),
    )
    with pytest.raises(migration.MigrationError, match="exactly one terminal semicolon"):
        migration.apply_plan(
            mutated_plan,
            credential_loader=lambda: pytest.fail("credentials loaded"),
            client_factory=lambda **kwargs: pytest.fail("client constructed"),
        )


def test_apply_rejects_production_fallback_before_credentials():
    plan = replace(migration.build_plan("staging"), dataset="trends_v2")
    with pytest.raises(migration.MigrationError, match="target allowlist"):
        migration.apply_plan(
            plan,
            credential_loader=lambda: pytest.fail("credentials loaded"),
            client_factory=lambda **kwargs: pytest.fail("client constructed"),
        )


def test_apply_rejects_safe_but_unaccepted_ddl_before_credentials():
    plan = migration.build_plan("staging")
    mutated_statement = replace(
        plan.statements[0],
        sql=plan.statements[0].sql.replace(
            "Stable signal identity.",
            "Unaccepted description.",
            1,
        ),
    )
    mutated_plan = replace(
        plan,
        statements=(mutated_statement, *plan.statements[1:]),
    )
    with pytest.raises(migration.MigrationError, match="accepted SQL mismatch"):
        migration.apply_plan(
            mutated_plan,
            credential_loader=lambda: pytest.fail("credentials loaded"),
            client_factory=lambda **kwargs: pytest.fail("client constructed"),
        )


def test_view_query_comparison_normalizes_only_layout():
    query = "SELECT\n  client_scope_id\nFROM `p.d.t`\nWHERE value = 'A B';"
    equivalent = "  SELECT client_scope_id FROM `p.d.t` WHERE value = 'A B'  "
    changed = "SELECT client_scope_id FROM `p.d.t` WHERE value = 'a b'"
    assert migration.normalize_query(query) == migration.normalize_query(equivalent)
    assert migration.normalize_query(query) != migration.normalize_query(changed)


def test_wave1_authority_upgrade_is_exact_staging_only_and_idempotent():
    descriptions = {
        "endpoint": "exact retained vendor route identifier",
        "vendor_family": "independent vendor collection authority",
        "channel_family": "evidence channel authority",
        "source_family": "compatibility alias equal to channel_family for Wave 1 rows",
        "geo_method_id": "retained row-level geo method identifier",
        "geo_receipt_id": "retained row-level geo authority receipt identifier",
        "native_id": "platform-native content identity used before URL deduplication",
        "source_family_map_version": "exact source identity map version",
    }
    statements = migration.wave1_authority_upgrade.build_statements(PROJECT, STAGING_DATASET)
    assert len(statements) == 16
    expected = tuple(
        f"ALTER TABLE `{PROJECT}.{STAGING_DATASET}.{table}` "
        f"ADD COLUMN IF NOT EXISTS {field} STRING "
        f"OPTIONS(description = '{descriptions[field]}')"
        for table in ("raw_content", "enriched_content")
        for field in descriptions
    )
    assert statements == expected
    assert all(
        "DEFAULT" not in statement and "NOT NULL" not in statement for statement in statements
    )
    with pytest.raises(migration.MigrationError, match="staging only"):
        migration.wave1_authority_upgrade.build_statements(PROJECT, "trends_v2")


def test_wave1_authority_upgrade_mutation_changes_the_accepted_definition():
    statements = migration.wave1_authority_upgrade.build_statements(PROJECT, STAGING_DATASET)
    mutated = statements[0].replace(
        "exact retained vendor route identifier",
        "changed",
    )
    assert mutated not in statements


def test_staging_plan_owns_wave1_authority_upgrade_and_qa_does_not():
    staging = migration.build_plan("staging")
    expected = migration.wave1_authority_upgrade.build_statements(PROJECT, STAGING_DATASET)
    assert staging.wave1_authority_upgrade_statements == expected
    assert "Wave 1 authority additive forward SQL:" in migration.render_dry_run(staging)
    assert all(statement in migration.render_dry_run(staging) for statement in expected)
    assert migration.build_plan("qa").wave1_authority_upgrade_statements == ()


def test_wave1_authority_upgrade_applies_idempotently_and_reads_back_exact_schema():
    plan = migration.build_plan("staging")
    client = _client_for_target("staging")
    preserved = {}
    for table in migration.wave1_authority_upgrade.TABLES:
        table_ref = f"{PROJECT}.{STAGING_DATASET}.{table}"
        client.rows[table_ref] = [{"legacy": table}]
        preserved[table_ref] = list(client.rows[table_ref])

    for _ in range(2):
        migration.apply_plan(
            plan,
            credential_loader=_credential_loader(STAGING_IDENTITY),
            client_factory=lambda **_kwargs: client,
        )

    assert tuple(client.query_log[:16]) == plan.wave1_authority_upgrade_statements
    expected_tail = tuple(
        (field, "STRING", "NULLABLE", description)
        for field, description in migration.wave1_authority_upgrade.COLUMNS
    )
    for table in migration.wave1_authority_upgrade.TABLES:
        table_ref = f"{PROJECT}.{STAGING_DATASET}.{table}"
        actual = tuple(
            (field.name, field.field_type, field.mode, field.description)
            for field in client.get_table(table_ref).schema[-8:]
        )
        assert actual == expected_tail
        assert client.rows[table_ref] == preserved[table_ref]


def test_wave1_authority_readback_rejects_description_mutation_before_oi_objects():
    def mutate(api_repr):
        api_repr["schema"]["fields"][-1]["description"] = "changed"

    client = _client_for_target(
        "staging",
        table_mutations={"raw_content": mutate},
    )
    with pytest.raises(migration.MigrationError, match="schema readback mismatch"):
        _apply("staging", client)
    assert len(client.query_log) == 16


def test_runtime_closure_table_and_routines_are_staging_only_and_ordered():
    staging = migration.build_plan("staging")
    assert tuple(statement.name for statement in staging.runtime_closure_statements) == (
        "open_intelligence_quality_review_receipts_v1",
        "sp_register_open_intelligence_quality_review_receipt_v1",
        "sp_read_open_intelligence_quality_review_receipt_v1",
    )
    assert tuple(statement.kind for statement in staging.runtime_closure_statements) == (
        "table",
        "routine",
        "routine",
    )
    rendered = migration.render_dry_run(staging)
    assert "Runtime closure authorized routines:" in rendered
    assert all(statement.sql in rendered for statement in staging.runtime_closure_statements)

    qa = migration.build_plan("qa")
    assert qa.runtime_closure_statements == ()


def test_runtime_closure_routines_are_fixed_templates_without_dynamic_targets():
    plan = migration.build_plan("staging")
    for statement in plan.runtime_closure_statements[1:]:
        assert statement.sql.startswith(
            f"CREATE OR REPLACE PROCEDURE `{PROJECT}.{STAGING_DATASET}.{statement.name}`"
        )
        assert "{project}" not in statement.sql
        assert "{dataset}" not in statement.sql
        assert "EXECUTE IMMEDIATE" not in statement.sql
        assert "production" not in statement.sql.lower()


def test_runtime_closure_objects_apply_after_base_objects_and_read_back_exactly():
    plan = migration.build_plan("staging")
    client = _client_for_target("staging")
    accepted = migration.apply_plan(
        plan,
        credential_loader=_credential_loader(STAGING_IDENTITY),
        client_factory=lambda **_kwargs: client,
    )
    closure_names = tuple(statement.name for statement in plan.runtime_closure_statements)
    assert accepted[-3:] == closure_names
    closure_sql = tuple(statement.sql for statement in plan.runtime_closure_statements)
    positions = tuple(client.query_log.index(sql) for sql in closure_sql)
    assert positions == tuple(sorted(positions))
    assert min(positions) >= len(plan.wave1_authority_upgrade_statements) + len(plan.statements)
    assert tuple(client.routines) == tuple(
        f"{PROJECT}.{STAGING_DATASET}.{name}" for name in closure_names[1:]
    )


def _signal_identity_tables():
    return migration.signal_identity_upgrade.TABLE_COLUMNS


def test_signal_identity_upgrade_uses_six_idempotent_additive_alters():
    statements = migration.signal_identity_upgrade.build_statements(PROJECT, STAGING_DATASET)

    assert len(statements) == 6
    assert all(sql.startswith("ALTER TABLE `") for sql in statements)
    assert all("ADD COLUMN IF NOT EXISTS" in sql for sql in statements)
    assert all("DROP" not in sql and "DELETE" not in sql for sql in statements)
    assert migration.build_plan("staging").signal_identity_upgrade_statements == statements
    assert migration.build_plan("qa").signal_identity_upgrade_statements == ()


def test_signal_identity_columns_close_each_schema_file_as_nullable_fields():
    plan = migration.build_plan("staging")
    for table, columns in _signal_identity_tables():
        statement = next(item for item in plan.statements if item.name == table)
        fields = parse_table_ddl(statement.sql)["fields"]
        tail = tuple(field[:4] for field in fields[-len(columns) :])
        assert tail == tuple(
            (
                name,
                "STRING",
                "REPEATED" if field_type == "ARRAY<STRING>" else "NULLABLE",
                description,
            )
            for name, field_type, description in columns
        )


def test_signal_identity_upgrade_runs_between_create_and_readback_on_legacy_tables():
    client = _client_for_target("staging")
    plan = migration.build_plan("staging")
    refs = {}
    for table, columns in _signal_identity_tables():
        statement = next(item for item in plan.statements if item.name == table)
        parsed = parse_table_ddl(statement.sql)
        added = {name for name, _, _ in columns}
        ref = f"{PROJECT}.{STAGING_DATASET}.{table}"
        legacy = bigquery.Table(
            ref,
            schema=_schema_fields(
                tuple(field for field in parsed["fields"] if field[0] not in added)
            ),
        )
        legacy._properties["type"] = "TABLE"
        legacy.description = parsed["description"]
        legacy.clustering_fields = list(parsed["cluster"])
        retention_days = migration._retention_days("staging", table)
        legacy.time_partitioning = bigquery.TimePartitioning(
            field=_partition_field(parsed["partition"]),
            expiration_ms=None if retention_days is None else retention_days * 86_400_000,
        )
        client.resources[ref] = legacy
        client.rows[ref] = [{"fixture": table}]
        refs[table] = ref
    before = deepcopy(client.rows)

    _apply("staging", client)

    log = tuple(client.query_log)
    for table, columns in _signal_identity_tables():
        schema = tuple(field.name for field in client.get_table(refs[table]).schema)
        assert schema[-len(columns) :] == tuple(name for name, _, _ in columns)
        assert client.rows[refs[table]] == before[refs[table]]
        create_index = next(
            index
            for index, sql in enumerate(log)
            if sql.startswith(f"CREATE TABLE IF NOT EXISTS `{refs[table]}`")
        )
        alters = tuple(
            sql
            for sql in migration.signal_identity_upgrade.build_statements(PROJECT, STAGING_DATASET)
            if f"`{refs[table]}`" in sql
        )
        assert log[create_index + 1 : create_index + 1 + len(alters)] == alters


def test_dry_run_renders_signal_identity_additive_sql():
    text = migration.render_dry_run(migration.build_plan("staging"))

    assert "Signal identity additive forward SQL:" in text
    for sql in migration.signal_identity_upgrade.build_statements(PROJECT, STAGING_DATASET):
        assert f"{sql};" in text


def test_verify_table_models_ingestion_time_partitioning_as_bigquery_reports_it():
    plan = migration.build_plan("staging")
    statement = next(item for item in plan.statements if item.name == "gdelt_event_market_wave1_v1")
    parsed = parse_table_ddl(statement.sql)
    assert parsed["partition"] == "_PARTITIONDATE"
    table = bigquery.Table(
        f"{PROJECT}.{STAGING_DATASET}.gdelt_event_market_wave1_v1",
        schema=_schema_fields(parsed["fields"]),
    )
    table._properties["type"] = "TABLE"
    table.description = parsed["description"]
    table.clustering_fields = list(parsed["cluster"])
    table.time_partitioning = bigquery.TimePartitioning(type_="DAY")

    migration._verify_table(statement, table, "gdelt_events_wave1_v1")

    table.time_partitioning = None
    with pytest.raises(migration.MigrationError, match="partition field"):
        migration._verify_table(statement, table, "gdelt_events_wave1_v1")


def test_durable_result_reference_is_unique_per_consumption() -> None:
    # sp_record refuses a repeated result_reference, so two executions of one plan must not
    # share the reference; execution xdlvd hit exactly that after fq5n6 recorded its failure.
    authority, _consumption = _consumed_migration_authority()
    recorded = []
    for consumption_id in ("exc_" + "c" * 64, "exc_" + "e" * 64):
        consumption = SimpleNamespace(consumption_id=consumption_id)
        migration._execute_durable_migration(
            authority_loader=lambda _operation, **kwargs: authority,
            authority_consumer=lambda _authority, c=consumption: c,
            plan_applier=lambda plan: tuple(
                item.name for item in (*plan.statements, *plan.runtime_closure_statements)
            ),
            result_recorder=lambda *args: (
                recorded.append(args) or SimpleNamespace(result_id="exr_" + "d" * 64)
            ),
        )
    references = [args[2] for args in recorded]
    assert len(set(references)) == 2
    for reference, consumption_id in zip(
        references, ("exc_" + "c" * 64, "exc_" + "e" * 64), strict=True
    ):
        assert reference.startswith("bq://ogilvy-trends-v2.trends_v2_staging#")
        assert reference.endswith("/" + consumption_id)


def test_runtime_closure_routine_readback_accepts_the_body_bigquery_returns():
    # BigQuery returns a procedure body as BEGIN ... END with no trailing semicolon, while the
    # routine file closes with END; execution s92k5 refused the freshly created routine on that.
    plan = migration.build_plan("staging")
    statement = next(
        item
        for item in plan.runtime_closure_statements
        if item.name == "sp_register_open_intelligence_quality_review_receipt_v1"
    )
    body = migration._routine_body(statement.sql)
    assert body.startswith("BEGIN")
    live_body = body.rstrip().rstrip(";")
    assert live_body.endswith("END")

    class _Routine:
        routine_id = statement.name
        type_ = "PROCEDURE"

        def __init__(self, text):
            self.body = text

    class _Client:
        def __init__(self, text):
            self._routine = _Routine(text)

        def get_routine(self, _ref):
            return self._routine

    migration._verify_runtime_closure_statement(_Client(live_body), plan, statement)
    with pytest.raises(migration.MigrationError, match="readback mismatch"):
        migration._verify_runtime_closure_statement(
            _Client(live_body + " SELECT 1;"), plan, statement
        )


def test_view_references_read_only_three_segment_backticked_tokens():
    # 5 Sep 2026: the desk view names STRUCT fields `at`, `end`, `interval` and
    # `window`, which BigQuery reserves; a match that ran between two of those
    # aliases across a decimal literal read as a foreign table reference.
    body = (
        "SELECT STRUCT(x AS `at`, 0.0 AS value) AS point, STRUCT(d AS `end`) AS w "
        "FROM `{project}.{dataset}.signal_evidence_v2` e "
        "JOIN `other-project.other_dataset.foreign_table` f USING (id)"
    )
    assert migration._view_references(body) == {
        "{project}.{dataset}.signal_evidence_v2",
        "other-project.other_dataset.foreign_table",
    }
    assert migration._view_references("SELECT `at`, 1.5, `end` FROM `p.d.t`") == {"p.d.t"}


def test_authority_readback_accepts_a_column_appended_after_the_authority_block():
    # 5 Sep 2026: staging enriched_content gained near_topic and near_cosine by
    # ALTER TABLE after the Wave 1 authority upgrade, so the live order ends with
    # them while the schema file lists them before the authority block. The
    # readback proves the field set, the partition, the clustering and the
    # description; column position is BigQuery's, not the contract's.
    plan = migration.build_plan("staging")
    client = _client_for_target("staging")
    _apply("staging", client)
    table_ref = f"{PROJECT}.{STAGING_DATASET}.enriched_content"
    resource = client.resources[table_ref]
    fields = list(resource.schema)
    moved = [field for field in fields if field.name in {"near_topic", "near_cosine"}]
    assert len(moved) == 2
    resource.schema = [field for field in fields if field not in moved] + moved
    migration._verify_wave1_authority_readback(client, plan)
    resource.schema = [field for field in resource.schema if field.name != "near_cosine"]
    with pytest.raises(migration.MigrationError, match="readback mismatch"):
        migration._verify_wave1_authority_readback(client, plan)


# D05: the observation disposition relation is an additive derived table

DISPOSITIONS_TABLE = "open_intelligence_observation_dispositions_v1"
_DISPOSITIONS_SOURCE_PATH = f"infra/bigquery_schemas/{DISPOSITIONS_TABLE}.sql"


def test_dispositions_schema_is_registered_additively_with_fresh_schema_parity():
    assert migration._SCHEMAS[-1] == DISPOSITIONS_TABLE
    assert BASE_TABLE_NAMES[-1] == DISPOSITIONS_TABLE
    digest = migration._SOURCE_SHA256[_DISPOSITIONS_SOURCE_PATH]
    assert hashlib.sha256((_ROOT / _DISPOSITIONS_SOURCE_PATH).read_bytes()).hexdigest() == digest
    for target in ("staging", "qa"):
        plan = migration.build_plan(target)
        statement = next(item for item in plan.statements if item.name == DISPOSITIONS_TABLE)
        assert statement.kind == "table"
        parsed = parse_table_ddl(statement.sql)
        assert [field[:3] for field in parsed["fields"]] == [
            ("operation_id", "STRING", "REQUIRED"),
            ("observation_key", "STRING", "REQUIRED"),
            ("identity_kind", "STRING", "REQUIRED"),
            ("native_namespace", "STRING", "NULLABLE"),
            ("native_id", "STRING", "NULLABLE"),
            ("collection_event_id", "STRING", "REQUIRED"),
            ("source_row_id", "STRING", "NULLABLE"),
            ("boundary", "STRING", "REQUIRED"),
            ("outcome", "STRING", "REQUIRED"),
            ("reason_code", "STRING", "NULLABLE"),
            ("market", "STRING", "REQUIRED"),
            ("route", "STRING", "REQUIRED"),
            ("observed_at", "TIMESTAMP", "REQUIRED"),
            ("source_binding_digest", "STRING", "REQUIRED"),
        ]
        assert parsed["partition"] == "DATE(observed_at)"
        assert parsed["cluster"] == ("market", "route", "boundary")
        assert parsed["expiration"] == (90 if target == "qa" else None)
        assert f"`{PROJECT}.{migration._TARGET_DATASETS[target]}.{DISPOSITIONS_TABLE}`" in (
            statement.sql
        )
    rollback = migration.build_plan("staging").rollback_statements
    assert f"DROP TABLE IF EXISTS `{PROJECT}.trends_v2_staging.{DISPOSITIONS_TABLE}`;" in rollback
