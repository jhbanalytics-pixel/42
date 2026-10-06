"""Fail-closed Open Intelligence v2 staging and QA migration runner."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if __name__ == "__main__":
    sys.modules["scripts.migrations.create_open_intelligence_v2"] = sys.modules[__name__]

import google.auth
from google.api_core.exceptions import NotFound
from google.auth.compute_engine import credentials as compute_credentials
from google.auth.transport import requests as google_auth_requests
from google.cloud import bigquery
from google.oauth2 import service_account
from scripts.setup_bigquery import _with_partition_expiration
from src.analysis.open_intelligence import execution_approval, execution_generations
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.execution_origins import OriginRefusal, select_origin
from src.contracts.bigquery_ddl import Field, parse_table_ddl

PROJECT = "ogilvy-trends-v2"
LOCATION = "US"
_SCHEMAS = (
    "signal_candidates_v2",
    "signal_evidence_v2",
    "signal_membership_v2",
    "signal_lineage_v2",
    "signal_analysis_v2",
    "signal_predictions_v2",
    "signal_outcomes_v2",
    "source_performance_daily_v2",
    "collection_exposure_receipts_v1",
    # Last. The receipt is the commit marker for a run, so its table is created
    # after every row-family table it will vouch for.
    "open_intelligence_run_receipts_v1",
    "open_intelligence_quality_release_records_v2",
    # Additive telemetry projection: the observation disposition sidecar.
    "open_intelligence_observation_dispositions_v1",
)
_WAVE1_SCHEMAS = (
    "gdelt_events_wave1_v1",
    "gdelt_event_market_wave1_v1",
    "gdelt_gcam_wave1_v1",
)
_RUNTIME_CLOSURE_SCHEMAS = ("open_intelligence_quality_review_receipts_v1",)
_RUNTIME_CLOSURE_ROUTINES = (
    "sp_register_open_intelligence_quality_review_receipt_v1",
    "sp_read_open_intelligence_quality_review_receipt_v1",
)
_VIEWS = (
    "v_signal_board_v2",
    "v_signal_evidence_v2",
    "v_source_lab_v2",
    "v_open_intelligence_health_v2",
    # Last. It reads the run receipt, so it is created after that table.
    "v_desk_dynamic_signals_v1",
    "v_desk_dynamic_signals_v2",
)
_SOURCE_SHA256 = {
    "infra/bigquery_schemas/raw_content.sql": "6fdcb62cefedce79c199063e9775e79466f7a08f80009146e371e5c5f2497c0c",
    "infra/bigquery_schemas/enriched_content.sql": "9756a6a451fa188575c4bf760b2bea72947f4f1bcf9cbcbf61ac777893bd32db",
    "infra/bigquery_schemas/signal_candidates_v2.sql": "5b072f6d65ffd4b475c372094e0485b65e20500ebb860c5abcdd4d08ea8db6f5",
    "infra/bigquery_schemas/signal_evidence_v2.sql": "f3b71761d64b89bbca36295bd43556c996d641a18e4f14bbc999469f6b8ffc6c",
    "infra/bigquery_schemas/signal_membership_v2.sql": "97019729fedd5317660014c1becec0cce636ec02a34856e8d853dba9fa452bdf",
    "infra/bigquery_schemas/signal_lineage_v2.sql": "b7071f4f2c9628a78f732d77850ecacc9dbc59cc5a95c6aba76a71a55ccf253b",
    "infra/bigquery_schemas/signal_analysis_v2.sql": "6e9eb90fcf4b070153ed805bc1b8852b8e80812f6defbc65460f2c5feaade2e0",
    "infra/bigquery_schemas/signal_predictions_v2.sql": "0ebef04437eb3bf876bfd7fef4374004c67da55d5413225d56e75eb9a344d9de",
    "infra/bigquery_schemas/signal_outcomes_v2.sql": "05c2c2f248a79ea98049f78c4e85c234c7aa019cf07d49879994579c0cdc9c74",
    "infra/bigquery_schemas/source_performance_daily_v2.sql": "0c81861a715514e3afb45587be9afe323057ee9c0f139e7a1878e287c274c275",
    "infra/bigquery_schemas/gdelt_events_wave1_v1.sql": "46ef22ad4b4833a2773417dadcb684a3008c7712eda94463848e4dac3c5716cc",
    "infra/bigquery_schemas/gdelt_event_market_wave1_v1.sql": "6384e95f28ba93bc1b057107124b9864dd2e551d2233519c21c5a905a1bea282",
    "infra/bigquery_schemas/gdelt_gcam_wave1_v1.sql": "70e0e2fa761db27036a974bdbbec0f7e9cc0a3064a88419ddac6a652decc728d",
    "infra/bigquery_schemas/collection_exposure_receipts_v1.sql": "3ba336c9720981300f3fa83875962fed382908e5b6698ad472502609c81a7561",
    "infra/bigquery_schemas/open_intelligence_run_receipts_v1.sql": "3f739ac125c6fe3ddc33355af8b1400a7e604add07027c391d50de1119230346",
    "infra/bigquery_schemas/open_intelligence_quality_release_records_v2.sql": "aada06bf3ac8b2b309ab9927a4cc39e610ebbc64fa22af046eaaf8e5d88fbeb5",
    "infra/bigquery_schemas/open_intelligence_quality_review_receipts_v1.sql": "9434abc2e6d515c6d4c336cf564d43a40c4f6df9a2d73a33eeb2f6f77e74378d",
    "infra/bigquery_schemas/open_intelligence_observation_dispositions_v1.sql": "5ac207425bd91721b62d090dc0b3e8ac8e6e41649476c710542afc995db93494",
    "infra/bigquery_schemas/canary_results_v2.sql": "32a6401aa9434135a6e276dd01b4dc02037fc25413c86893f349e6adbc427be9",
    "infra/bigquery_views/v_signal_board_v2.sql": "fc62243a048882452e43c9bab37dc9000103ccc405263e5956aeb243f2fd9e01",
    "infra/bigquery_views/v_signal_evidence_v2.sql": "4cb8825be31f5cfcded2d8c9bbdbb28a3c4e134e24ee36f3de0ab0ff6a3bed7d",
    "infra/bigquery_views/v_source_lab_v2.sql": "53aa2c88af66e43e9ee9f6620ee401473ecaa31957f66a8496f72182d5beb8ea",
    "infra/bigquery_views/v_open_intelligence_health_v2.sql": "dd4bc83275e3dcf8ec3ffa1446890ba2844fa68c4b9bc0291a3f2ac454e8e8e6",
    "infra/bigquery_views/v_desk_dynamic_signals_v1.sql": "d9f6eeb764fa29a9fa0fce75ba788590e6397a0fa4656fd00c9308ad05df8e6e",
    "infra/bigquery_views/v_desk_dynamic_signals_v2.sql": "bbd19dac4215b8175ee1720f416bb5fecbf92c87ea97adbc266083f5fd77f824",
    "infra/bigquery_routines/sp_register_open_intelligence_quality_review_receipt_v1.sql": "c5323fd739fe554a3710fc3ddf3d73390f2f9b464d6d843454a3010d336b771d",
    "infra/bigquery_routines/sp_read_open_intelligence_quality_review_receipt_v1.sql": "a58d6a17ce36370c6bc3ce9ee8c7b595f63b4faad2b781078c4d3f782fe82e50",
}
_TARGET_DATASETS = {
    "staging": "trends_v2_staging",
    "qa": "trends_v2_staging_qa",
}
# The staging identity is never spelled here: it is the migration_apply binding of the
# active trusted generation's fresh origin. QA has no versioned execution origin and keeps
# its canary identity as a retained literal.
_QA_IDENTITY = "trends-engine-canary@ogilvy-trends-v2.iam.gserviceaccount.com"
_MANIFEST_VERSION_V2 = "open_intelligence_execution_manifest_v2"
_FORBIDDEN_FORWARD = re.compile(r"\b(?:ALTER|DELETE|DROP|INSERT|MERGE|TRUNCATE|UPDATE)\b", re.I)
_SCRIPTING_FORWARD = re.compile(
    r"\b(?:BEGIN|CALL|DECLARE|LOOP|REPEAT|SET|WHILE)\b|"
    r"\bEXECUTE\s+IMMEDIATE\b",
    re.I,
)
_TABLE_HEADER = re.compile(
    r"\ACREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`(?P<ref>[^`]+)`",
    re.I,
)
_VIEW_HEADER = re.compile(
    r"\ACREATE\s+OR\s+REPLACE\s+VIEW\s+`(?P<ref>[^`]+)`\s+AS\s+",
    re.I | re.S,
)
_TYPE_ALIASES = {
    "BOOLEAN": "BOOL",
    "FLOAT": "FLOAT64",
    "INTEGER": "INT64",
}


class MigrationError(RuntimeError):
    """Raised when an allowlist or resource readback check fails."""


_DURABLE_ARTIFACT_CONTEXT: dict[str, bytes] | None = None


@dataclass(frozen=True)
class Statement:
    name: str
    kind: str
    sql: str


@dataclass(frozen=True)
class RuntimeIamBinding:
    principal: str
    resource: str
    role: str
    purpose: str


@dataclass(frozen=True)
class MigrationPlan:
    target: str
    project: str
    dataset: str
    location: str
    service_account: str
    statements: tuple[Statement, ...]
    source_lab_upgrade_statements: tuple[str, ...]
    rollback_statements: tuple[str, ...]
    wave1_authority_upgrade_statements: tuple[str, ...] = ()
    signal_identity_upgrade_statements: tuple[str, ...] = ()
    runtime_closure_statements: tuple[Statement, ...] = ()
    runtime_closure_iam_bindings: tuple[RuntimeIamBinding, ...] = ()
    origin_registry_sha256: str | None = None
    resource_manifest_sha256: str | None = None
    connected_repo: str | None = None
    image_repository: str | None = None


def _v2_origin(registry, operation: str):
    """The one fresh origin binding an operation, admitted for the plan render.

    The plan and its dry run are the operator's preparation artifacts, so the origin is
    selected under new_approval; the durable route re-selects it under new_consume in
    _require_plan_authority against the issued authority.
    """
    rows = [
        origin
        for origin in registry.values()
        if origin.manifest_version == _MANIFEST_VERSION_V2
        and operation in origin.operation_bindings
    ]
    if len(rows) != 1:
        raise MigrationError(f"no single fresh origin binds {operation}")
    try:
        return select_origin(
            manifest_version=rows[0].manifest_version,
            contract_sha256=rows[0].contract_sha256,
            mode="new_approval",
            registry=registry,
        )
    except OriginRefusal as error:
        raise MigrationError(f"fresh origin for {operation} is not admitted: {error}") from error


def _v2_principal(registry, operation: str) -> str:
    return _v2_origin(registry, operation).operation_bindings[operation].service_identity


def _runtime_closure_iam_bindings(dataset: str, registry) -> tuple[RuntimeIamBinding, ...]:
    if dataset != "trends_v2_staging":
        return ()
    routine_root = f"projects/{PROJECT}/datasets/{dataset}/routines"
    register = f"{routine_root}/sp_register_open_intelligence_quality_review_receipt_v1"
    read = f"{routine_root}/sp_read_open_intelligence_quality_review_receipt_v1"
    rows = (
        RuntimeIamBinding(
            principal="user:albert.meintjes@ogilvy.co.za",
            resource=register,
            role="roles/bigquery.dataViewer",
            purpose="human quality-review registration routine invocation",
        ),
        RuntimeIamBinding(
            principal=f"serviceAccount:{_v2_principal(registry, 'r3_apply')}",
            resource=read,
            role="roles/bigquery.dataViewer",
            purpose="r3 apply quality-review read routine invocation",
        ),
        RuntimeIamBinding(
            principal=f"serviceAccount:{_v2_principal(registry, 'r3_release')}",
            resource=read,
            role="roles/bigquery.dataViewer",
            purpose="r3 release quality-review read routine invocation",
        ),
    )
    # r3_apply and r3_release share one identity since amendment d, so the closure is
    # deduplicated on principal, resource and role; the first row's purpose stands.
    seen: set[tuple[str, str, str]] = set()
    distinct = []
    for row in rows:
        key = (row.principal, row.resource, row.role)
        if key in seen:
            continue
        seen.add(key)
        distinct.append(row)
    return tuple(distinct)


class _SourceLabUpgrade:
    COLUMNS = (
        ("platform", "STRING", "Canonical vendor platform for inventory rows."),
        ("resource", "STRING", "Canonical vendor resource for inventory rows."),
        ("catalog_digest", "STRING", "Approved catalog snapshot digest for inventory rows."),
        (
            "official_credits",
            "NUMERIC",
            "Vendor documented credit quantity for the inventory route.",
        ),
        (
            "official_credits_label",
            "STRING",
            "Vendor documented credit label for the inventory route.",
        ),
        ("official_archetype", "STRING", "Vendor documented route archetype."),
        ("catalog_paginated", "BOOL", "Vendor catalog pagination flag."),
        ("catalog_metered", "BOOL", "Vendor catalog metering flag."),
        ("cache_ttl_seconds", "INT64", "Vendor catalog cache lifetime in seconds."),
        ("delivery", "STRING", "Vendor documented delivery mode."),
        ("docs_url", "STRING", "Sanitized official documentation URL."),
        ("vendor_family", "STRING", "Canonical supplier family."),
        (
            "channel_family",
            "STRING",
            "Canonical channel family. Equals source_family compatibility alias.",
        ),
        (
            "funded_lane",
            "STRING",
            "Funded execution lane when a route is piloted under a bounded stage.",
        ),
    )
    TABLE = "source_performance_daily_v2"
    VIEW = "v_source_lab_v2"

    @classmethod
    def build_statements(
        cls, project: str = PROJECT, dataset: str = "trends_v2_staging"
    ) -> tuple[str, ...]:
        table_ref = f"{project}.{dataset}.{cls.TABLE}"
        return tuple(
            f"ALTER TABLE `{table_ref}` ADD COLUMN IF NOT EXISTS {name} {field_type} "
            f"OPTIONS(description = {description!r})"
            for name, field_type, description in cls.COLUMNS
        )

    @classmethod
    def build_rollback(cls, dataset: str) -> tuple[str, ...]:
        source = (_ROOT / "infra" / "bigquery_views" / f"{cls.VIEW}.sql").read_text(
            encoding="utf-8"
        )
        for name, _, _ in cls.COLUMNS[:11]:
            source, removed = re.subn(
                rf"^\s+{re.escape(name)},\r?\n",
                "",
                source,
                count=2,
                flags=re.M,
            )
            if removed != 2:
                raise MigrationError(f"cannot render prior Source Lab view field: {name}")
        return (source.replace("{project}", PROJECT).replace("{dataset}", dataset),)

    @classmethod
    def verify_columns(cls, table: Any) -> None:
        fields = {field.name: field for field in table.schema}
        for name, expected_type, expected_description in cls.COLUMNS:
            field = fields.get(name)
            actual_type = _resource_type(field.field_type) if field is not None else None
            actual_mode = (field.mode or "NULLABLE").upper() if field is not None else None
            actual_description = field.description if field is not None else None
            if (
                actual_type != expected_type
                or actual_mode != "NULLABLE"
                or actual_description != expected_description
            ):
                raise MigrationError(
                    f"Source Lab additive column mismatch for {name}: "
                    f"expected {expected_type} NULLABLE {expected_description!r}, "
                    f"got {actual_type} {actual_mode} {actual_description!r}"
                )

    @classmethod
    def apply_upgrade(
        cls,
        client: Any,
        project: str = PROJECT,
        dataset: str = "trends_v2_staging",
    ) -> None:
        for statement in cls.build_statements(project, dataset):
            client.query(statement).result()
        cls.verify_columns(client.get_table(f"{project}.{dataset}.{cls.TABLE}"))


source_lab_upgrade = _SourceLabUpgrade()


class _Wave1AuthorityUpgrade:
    TABLES = ("raw_content", "enriched_content")
    COLUMNS = (
        ("endpoint", "exact retained vendor route identifier"),
        ("vendor_family", "independent vendor collection authority"),
        ("channel_family", "evidence channel authority"),
        (
            "source_family",
            "compatibility alias equal to channel_family for Wave 1 rows",
        ),
        ("geo_method_id", "retained row-level geo method identifier"),
        (
            "geo_receipt_id",
            "retained row-level geo authority receipt identifier",
        ),
        (
            "native_id",
            "platform-native content identity used before URL deduplication",
        ),
        ("source_family_map_version", "exact source identity map version"),
    )

    @classmethod
    def build_statements(cls, project: str, dataset: str) -> tuple[str, ...]:
        if project != PROJECT or dataset != "trends_v2_staging":
            raise MigrationError("Wave 1 authority upgrade is staging only")
        return tuple(
            f"ALTER TABLE `{project}.{dataset}.{table}` "
            f"ADD COLUMN IF NOT EXISTS {field} STRING "
            f"OPTIONS(description = '{description}')"
            for table in cls.TABLES
            for field, description in cls.COLUMNS
        )


wave1_authority_upgrade = _Wave1AuthorityUpgrade()


class _SignalIdentityUpgrade:
    """Additive columns the quality-gated R3 and Wave 1 contracts added to the signal tables.

    Live staging tables predate them, and an additive ALTER appends at the end, so the schema
    files list these columns last and nullable.
    """

    TABLE_COLUMNS = (
        (
            "signal_candidates_v2",
            (
                (
                    "label_member_identity",
                    "STRING",
                    "Exact winning observation identity that owns the candidate label.",
                ),
            ),
        ),
        (
            "signal_evidence_v2",
            (
                (
                    "vendor_family",
                    "STRING",
                    "Canonical data supplier family. Distinct from channel family.",
                ),
                (
                    "channel_family",
                    "STRING",
                    "Canonical evidence channel. Equals source_family compatibility alias.",
                ),
            ),
        ),
        (
            "signal_membership_v2",
            (
                (
                    "vendor_families",
                    "ARRAY<STRING>",
                    "Complete sorted data supplier families for this member.",
                ),
                (
                    "channel_families",
                    "ARRAY<STRING>",
                    "Complete sorted channel families. Equals source_families compatibility aliases.",
                ),
                (
                    "source_provenance_json",
                    "STRING",
                    "Canonical sampled source provenance envelope for hybrid_graph_v3. Null for legacy rows.",
                ),
            ),
        ),
    )

    @classmethod
    def build_statements(cls, project: str, dataset: str) -> tuple[str, ...]:
        if project != PROJECT or dataset != "trends_v2_staging":
            raise MigrationError("signal identity upgrade is staging only")
        return tuple(
            f"ALTER TABLE `{project}.{dataset}.{table}` "
            f"ADD COLUMN IF NOT EXISTS {name} {field_type} "
            f"OPTIONS(description = '{description}')"
            for table, columns in cls.TABLE_COLUMNS
            for name, field_type, description in columns
        )

    @classmethod
    def statements_for(cls, plan: MigrationPlan, table: str) -> tuple[str, ...]:
        marker = f"`{plan.project}.{plan.dataset}.{table}` "
        return tuple(sql for sql in plan.signal_identity_upgrade_statements if marker in sql)


signal_identity_upgrade = _SignalIdentityUpgrade()


def _retention_days(target: str, name: str) -> int | None:
    if target == "qa":
        return 90
    if target == "staging" and name == "signal_membership_v2":
        return 400
    return None


def _accepted_source_texts() -> dict[str, str]:
    sources: dict[str, str] = {}
    for source_path, expected_digest in _SOURCE_SHA256.items():
        path = _ROOT / source_path
        try:
            source_bytes = path.read_bytes()
        except OSError as exc:
            raise MigrationError(f"accepted source unreadable: {source_path}") from exc
        actual_digest = hashlib.sha256(source_bytes).hexdigest()
        if actual_digest != expected_digest:
            raise MigrationError(
                f"source digest mismatch for {source_path}: "
                f"expected {expected_digest}, got {actual_digest}"
            )
        try:
            sources[source_path] = source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError(f"accepted source is not UTF-8: {source_path}") from exc
    return sources


def _render_table_sql(
    name: str,
    target: str,
    dataset: str,
    sources: dict[str, str],
) -> str:
    source_path = f"infra/bigquery_schemas/{name}.sql"
    sql = sources[source_path]
    retention_days = _retention_days(target, name)
    if retention_days is not None:
        sql = _with_partition_expiration(sql, retention_days)
    return sql.replace("{project}", PROJECT).replace("{dataset}", dataset)


def _render_view_sql(
    name: str,
    dataset: str,
    sources: dict[str, str],
) -> str:
    source_path = f"infra/bigquery_views/{name}.sql"
    return sources[source_path].replace("{project}", PROJECT).replace("{dataset}", dataset)


def _render_runtime_closure_routine_sql(
    name: str,
    dataset: str,
    sources: dict[str, str],
) -> str:
    source_path = f"infra/bigquery_routines/{name}.sql"
    sql = sources[source_path].replace("{project}", PROJECT).replace("{dataset}", dataset)
    expected = f"CREATE OR REPLACE PROCEDURE `{PROJECT}.{dataset}.{name}`"
    if (
        not sql.startswith(expected)
        or "{project}" in sql
        or "{dataset}" in sql
        or "EXECUTE IMMEDIATE" in sql
        or "production" in sql.lower()
    ):
        raise MigrationError(f"runtime closure routine target mismatch for {name}")
    return sql


def _rollback_statements(dataset: str, statements: Sequence[Statement]) -> tuple[str, ...]:
    return tuple(
        f"DROP {statement.kind.upper()} IF EXISTS `{PROJECT}.{dataset}.{statement.name}`;"
        for statement in reversed(statements)
        if statement.name != "signal_membership_v2"
    )


def _validate_target_retention(target: str, statements: Sequence[Statement]) -> None:
    for statement in statements:
        if statement.kind != "table":
            continue
        expected_days = _retention_days(target, statement.name)
        actual_days = parse_table_ddl(statement.sql)["expiration"]
        if actual_days != expected_days:
            raise MigrationError(
                f"target retention invariant failed for {statement.name}: "
                f"{target} requires {expected_days!r} days, got {actual_days!r}"
            )


def build_plan(target: str) -> MigrationPlan:
    """Build the exact local plan without credentials or a BigQuery client."""

    if target not in _TARGET_DATASETS:
        raise MigrationError(f"unsupported migration target: {target}")
    sources = _accepted_source_texts()
    dataset = _TARGET_DATASETS[target]
    generation = origin = None
    identity = _QA_IDENTITY
    if target == "staging":
        generation = execution_generations.active_generation()
        origin = _v2_origin(generation.registry, "migration_apply")
        identity = origin.operation_bindings["migration_apply"].service_identity
    table_names = (
        (*_SCHEMAS, "canary_results_v2") if target == "qa" else (*_SCHEMAS, *_WAVE1_SCHEMAS)
    )
    statements = [
        Statement(
            name=name,
            kind="table",
            sql=_render_table_sql(name, target, dataset, sources),
        )
        for name in table_names
    ]
    if target == "staging":
        statements.extend(
            Statement(
                name=name,
                kind="view",
                sql=_render_view_sql(name, dataset, sources),
            )
            for name in _VIEWS
        )
    source_lab_upgrade_statements = (
        source_lab_upgrade.build_statements(PROJECT, dataset) if target == "staging" else ()
    )
    wave1_authority_upgrade_statements = (
        wave1_authority_upgrade.build_statements(PROJECT, dataset) if target == "staging" else ()
    )
    signal_identity_upgrade_statements = (
        signal_identity_upgrade.build_statements(PROJECT, dataset) if target == "staging" else ()
    )
    runtime_closure_statements = (
        (
            Statement(
                name=_RUNTIME_CLOSURE_SCHEMAS[0],
                kind="table",
                sql=_render_table_sql(_RUNTIME_CLOSURE_SCHEMAS[0], target, dataset, sources),
            ),
            *(
                Statement(
                    name=name,
                    kind="routine",
                    sql=_render_runtime_closure_routine_sql(name, dataset, sources),
                )
                for name in _RUNTIME_CLOSURE_ROUTINES
            ),
        )
        if target == "staging"
        else ()
    )
    for statement in statements:
        validate_single_statement_sql(statement.sql)
    _validate_target_retention(target, statements)
    return MigrationPlan(
        target=target,
        project=PROJECT,
        dataset=dataset,
        location=LOCATION,
        service_account=identity,
        statements=tuple(statements),
        source_lab_upgrade_statements=source_lab_upgrade_statements,
        rollback_statements=(
            *_rollback_statements(
                dataset,
                tuple(
                    statement
                    for statement in statements
                    if statement.name not in {source_lab_upgrade.TABLE, source_lab_upgrade.VIEW}
                ),
            ),
            *(source_lab_upgrade.build_rollback(dataset) if target == "staging" else ()),
        ),
        wave1_authority_upgrade_statements=wave1_authority_upgrade_statements,
        signal_identity_upgrade_statements=signal_identity_upgrade_statements,
        runtime_closure_statements=runtime_closure_statements,
        runtime_closure_iam_bindings=(
            ()
            if generation is None
            else _runtime_closure_iam_bindings(dataset, generation.registry)
        ),
        origin_registry_sha256=None if generation is None else generation.origin_registry_sha256,
        resource_manifest_sha256=(
            None if generation is None else generation.resource_manifest_sha256
        ),
        connected_repo=None if origin is None else origin.connected_repo,
        image_repository=None if origin is None else origin.image_repository,
    )


def build_source_lab_plan() -> MigrationPlan:
    """Build the isolated staging-only Source Lab migration plan."""

    staging_plan = build_plan("staging")
    table_ref = f"{staging_plan.project}.{staging_plan.dataset}.{source_lab_upgrade.TABLE}"
    statements = tuple(
        statement
        for statement in staging_plan.statements
        if statement.name in {source_lab_upgrade.TABLE, source_lab_upgrade.VIEW}
    )
    if tuple((statement.name, statement.kind) for statement in statements) != (
        (source_lab_upgrade.TABLE, "table"),
        (source_lab_upgrade.VIEW, "view"),
    ):
        raise MigrationError("Source Lab migration members are not available")
    return MigrationPlan(
        target=staging_plan.target,
        project=staging_plan.project,
        dataset=staging_plan.dataset,
        location=staging_plan.location,
        service_account=staging_plan.service_account,
        statements=statements,
        source_lab_upgrade_statements=(
            *staging_plan.source_lab_upgrade_statements,
            f"ALTER TABLE `{table_ref}` ALTER COLUMN route_role SET OPTIONS "
            "(description = 'Evidence, utility_balance, utility_catalog, identity_discovery, "
            "identity_hygiene, or inventory.')",
            f"ALTER TABLE `{table_ref}` ALTER COLUMN status SET OPTIONS "
            "(description = 'Active, pilot, available_unwired, blocked, permanently_rejected, "
            "or inventory_only.')",
            # Ruled 5 Sep 2026: the reviewed rejection was named by every contract
            # layer and produced by nothing; the measured kill verdict replaces it.
            f"ALTER TABLE `{table_ref}` ALTER COLUMN kill_test_result SET OPTIONS "
            "(description = 'Required for permanently_rejected: the kill test reason, equal to blocking_reason.')",
            f"ALTER TABLE `{table_ref}` ALTER COLUMN review_date SET OPTIONS "
            "(description = 'Null. Reserved for a reviewed rejection, which no writer produces.')",
            # 5 Sep 2026: the writer stores counts here, not a 0 to 1 ratio; the
            # listening post read the old sentence and modelled the columns wrong.
            f"ALTER TABLE `{table_ref}` ALTER COLUMN `rows` SET OPTIONS "
            "(description = 'Distinct items the route returned; present for active and permanently_rejected inventory routes. For identity roles, normalized result count after deterministic normalization and deduplication.')",
            f"ALTER TABLE `{table_ref}` ALTER COLUMN unique_lift SET OPTIONS "
            "(description = 'Marginal downstream row count, candidate terms plus evidence rows only this route produced; present for active and permanently_rejected inventory routes.')",
        ),
        rollback_statements=source_lab_upgrade.build_rollback(staging_plan.dataset),
        wave1_authority_upgrade_statements=(),
        runtime_closure_statements=(),
        origin_registry_sha256=staging_plan.origin_registry_sha256,
        resource_manifest_sha256=staging_plan.resource_manifest_sha256,
        connected_repo=staging_plan.connected_repo,
        image_repository=staging_plan.image_repository,
    )


def _is_source_lab_plan(plan: MigrationPlan) -> bool:
    return plan.target == "staging" and tuple(
        (statement.name, statement.kind) for statement in plan.statements
    ) == (
        (source_lab_upgrade.TABLE, "table"),
        (source_lab_upgrade.VIEW, "view"),
    )


def _statement_code(sql: str) -> str:
    output: list[str] = []
    quoted = False
    backticked = False
    semicolons: list[int] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if quoted:
            output.append(" ")
            if char == "'" and index + 1 < len(sql) and sql[index + 1] == "'":
                output.append(" ")
                index += 2
                continue
            if char == "'":
                quoted = False
        elif backticked:
            output.append(" ")
            if char == "`":
                backticked = False
        else:
            if sql.startswith("--", index) or sql.startswith("/*", index) or char == "#":
                raise MigrationError("SQL comments are not accepted")
            if char == "'":
                quoted = True
                output.append(" ")
            elif char == "`":
                backticked = True
                output.append(" ")
            else:
                output.append(char)
                if char == ";":
                    semicolons.append(index)
        index += 1
    if quoted or backticked:
        raise MigrationError("unterminated quoted SQL text")
    terminal_index = len(sql.rstrip()) - 1
    if semicolons != [terminal_index]:
        raise MigrationError("SQL must contain exactly one terminal semicolon")
    code = "".join(output)
    if _SCRIPTING_FORWARD.search(code):
        raise MigrationError("SQL scripting construct is not accepted")
    return code


def validate_single_statement_sql(sql: str) -> None:
    """Reject comments, scripts, and any nonterminal statement delimiter."""

    _statement_code(sql)


def _view_references(body: str) -> set[str]:
    """Every `project.dataset.table` reference in a view body.

    A reference is one backticked token of three dot-separated segments. The
    earlier pattern let a match run from one backtick to the next, so two
    backticked reserved-word aliases with a decimal between them (`at` ... 0.0
    ... `end`) read as a foreign reference and the desk view failed the
    allowlist (5 Sep 2026).
    """

    return set(re.findall(r"`([^`.]+\.[^`.]+\.[^`.]+)`", body))


def _validate_forward_statement(plan: MigrationPlan, statement: Statement) -> None:
    stripped = statement.sql.strip()
    searchable = _statement_code(stripped)
    if _FORBIDDEN_FORWARD.search(searchable):
        raise MigrationError(f"forbidden forward SQL in {statement.name}")
    if statement.kind == "table":
        parse_table_ddl(stripped)
        match = _TABLE_HEADER.match(stripped)
    elif statement.kind == "view" and plan.target == "staging":
        match = _VIEW_HEADER.match(stripped)
        if match is None or not stripped.endswith(";"):
            raise MigrationError(f"unsupported view DDL grammar in {statement.name}")
        allowed_refs = {f"{plan.project}.{plan.dataset}.{name}" for name in _SCHEMAS}
        body = stripped[match.end() :]
        refs = _view_references(body)
        if not refs.issubset(allowed_refs):
            raise MigrationError(f"view {statement.name} crosses the dataset allowlist")
    else:
        raise MigrationError(f"unsupported forward object kind: {statement.kind}")
    expected_ref = f"{plan.project}.{plan.dataset}.{statement.name}"
    if match is None or match.group("ref") != expected_ref:
        raise MigrationError(f"forward object target mismatch for {statement.name}")


def _validate_plan(plan: MigrationPlan) -> None:
    expected = build_source_lab_plan() if _is_source_lab_plan(plan) else build_plan(plan.target)
    if (
        plan.project,
        plan.dataset,
        plan.location,
        plan.service_account,
        plan.origin_registry_sha256,
        plan.resource_manifest_sha256,
        plan.connected_repo,
        plan.image_repository,
    ) != (
        expected.project,
        expected.dataset,
        expected.location,
        expected.service_account,
        expected.origin_registry_sha256,
        expected.resource_manifest_sha256,
        expected.connected_repo,
        expected.image_repository,
    ):
        raise MigrationError("migration target allowlist mismatch")
    expected_shape = tuple((statement.name, statement.kind) for statement in expected.statements)
    observed_shape = tuple((statement.name, statement.kind) for statement in plan.statements)
    if observed_shape != expected_shape:
        raise MigrationError("migration object order allowlist mismatch")
    for statement in plan.statements:
        _validate_forward_statement(plan, statement)
    for statement, accepted_statement in zip(plan.statements, expected.statements, strict=True):
        if statement.sql != accepted_statement.sql:
            raise MigrationError(f"accepted SQL mismatch for {statement.name}")
    if plan.source_lab_upgrade_statements != expected.source_lab_upgrade_statements:
        raise MigrationError("Source Lab additive upgrade allowlist mismatch")
    if plan.wave1_authority_upgrade_statements != expected.wave1_authority_upgrade_statements:
        raise MigrationError("Wave 1 authority additive upgrade allowlist mismatch")
    if plan.signal_identity_upgrade_statements != expected.signal_identity_upgrade_statements:
        raise MigrationError("signal identity additive upgrade allowlist mismatch")
    if tuple(
        (statement.name, statement.kind, statement.sql)
        for statement in plan.runtime_closure_statements
    ) != tuple(
        (statement.name, statement.kind, statement.sql)
        for statement in expected.runtime_closure_statements
    ):
        raise MigrationError("runtime closure routine allowlist mismatch")


def _authorized_credentials(
    plan: MigrationPlan,
    credential_loader: Callable[[], tuple[Any, str | None]],
    request_factory: Callable[[], Any],
) -> Any:
    credentials, adc_project = credential_loader()
    if adc_project != plan.project:
        raise MigrationError(f"ADC project mismatch: expected {plan.project}, got {adc_project!r}")
    credential_type = type(credentials)
    if credential_type not in (
        service_account.Credentials,
        compute_credentials.Credentials,
    ):
        raise MigrationError("active ADC is not an accepted service account identity")
    quota_project = credentials.quota_project_id
    if quota_project is not None and quota_project != plan.project:
        raise MigrationError(
            f"quota project mismatch: expected {plan.project}, got {quota_project!r}"
        )
    identity = credentials.service_account_email
    if credential_type is compute_credentials.Credentials and identity in (
        None,
        "",
        "default",
    ):
        try:
            credentials.refresh(request_factory())
        except Exception:
            raise MigrationError("compute service account identity refresh failed") from None
        identity = credentials.service_account_email
    if identity != plan.service_account:
        raise MigrationError(
            f"service account identity mismatch: expected {plan.service_account}, got {identity!r}"
        )
    return credentials


def _failure(
    object_name: str,
    check: str,
    expected: object,
    actual: object,
    last_accepted: str,
) -> MigrationError:
    return MigrationError(
        f"{object_name}: failed check {check}; expected {expected!r}, "
        f"got {actual!r}; last accepted object: {last_accepted}"
    )


def _verify_dataset(plan: MigrationPlan, dataset: bigquery.Dataset) -> None:
    checks = (
        ("dataset project", plan.project, dataset.project),
        ("dataset id", plan.dataset, dataset.dataset_id),
        ("dataset location", plan.location, dataset.location),
        ("default table expiry", None, dataset.default_table_expiration_ms),
    )
    for check, expected, actual in checks:
        if actual != expected:
            raise _failure(
                plan.dataset,
                check,
                expected,
                actual,
                "service identity",
            )


def _resource_type(field_type: str) -> str:
    normalized = field_type.upper()
    return _TYPE_ALIASES.get(normalized, normalized)


def _verify_fields(
    object_name: str,
    expected: tuple[Field, ...],
    actual: Sequence[bigquery.SchemaField],
    last_accepted: str,
    path: str = "",
) -> None:
    if len(actual) != len(expected):
        check = "nested fields" if path else "top-level fields"
        raise _failure(object_name, check, len(expected), len(actual), last_accepted)
    for expected_field, actual_field in zip(expected, actual, strict=True):
        name, field_type, mode, description, nested = expected_field
        field_path = f"{path}.{name}" if path else name
        checks = (
            ("field name", name, actual_field.name),
            ("field type", field_type, _resource_type(actual_field.field_type)),
            ("field mode", mode, (actual_field.mode or "NULLABLE").upper()),
            ("field description", description, actual_field.description),
        )
        for check, expected_value, actual_value in checks:
            if actual_value != expected_value:
                raise _failure(
                    object_name,
                    f"{check} at {field_path}",
                    expected_value,
                    actual_value,
                    last_accepted,
                )
        _verify_fields(
            object_name,
            nested,
            actual_field.fields,
            last_accepted,
            field_path,
        )


_INGESTION_TIME_PARTITIONS = frozenset({"_PARTITIONDATE", "_PARTITIONTIME"})


def _expected_partition_field(expression: str | None) -> str | None:
    # BigQuery reports ingestion-time partitioning as a DAY partitioning with no field.
    if expression is None or expression in _INGESTION_TIME_PARTITIONS:
        return None
    match = re.fullmatch(r"DATE\(([A-Za-z_][A-Za-z0-9_]*)\)", expression)
    return match.group(1) if match else expression


def _verify_table(
    statement: Statement,
    table: bigquery.Table,
    last_accepted: str,
) -> None:
    expected = parse_table_ddl(statement.sql)
    if table.table_type != "TABLE":
        raise _failure(
            statement.name,
            "table type",
            "TABLE",
            table.table_type,
            last_accepted,
        )
    _verify_fields(
        statement.name,
        expected["fields"],
        table.schema,
        last_accepted,
    )
    partitioning = table.time_partitioning
    if expected["partition"] in _INGESTION_TIME_PARTITIONS and partitioning is None:
        raise _failure(
            statement.name,
            "partition field",
            expected["partition"],
            None,
            last_accepted,
        )
    actual_partition = partitioning.field if partitioning else None
    expected_partition = _expected_partition_field(expected["partition"])
    if actual_partition != expected_partition:
        raise _failure(
            statement.name,
            "partition field",
            expected_partition,
            actual_partition,
            last_accepted,
        )
    actual_expiration = partitioning.expiration_ms if partitioning else None
    expected_expiration = (
        expected["expiration"] * 86_400_000 if expected["expiration"] is not None else None
    )
    if actual_expiration != expected_expiration:
        raise _failure(
            statement.name,
            "partition retention",
            expected_expiration,
            actual_expiration,
            last_accepted,
        )
    actual_cluster = tuple(table.clustering_fields or ())
    if actual_cluster != expected["cluster"]:
        raise _failure(
            statement.name,
            "clustering order",
            expected["cluster"],
            actual_cluster,
            last_accepted,
        )
    if table.description != expected["description"]:
        raise _failure(
            statement.name,
            "table description",
            expected["description"],
            table.description,
            last_accepted,
        )


def _view_query(sql: str) -> str:
    match = _VIEW_HEADER.match(sql.strip())
    if match is None:
        raise MigrationError("accepted view SQL has no supported header")
    return sql.strip()[match.end() :].rstrip().removesuffix(";").rstrip()


def normalize_query(query: str) -> str:
    """Collapse layout whitespace while preserving quoted text exactly."""

    normalized: list[str] = []
    quoted = False
    backticked = False
    whitespace = False
    index = 0
    query = query.strip().removesuffix(";").rstrip()
    while index < len(query):
        char = query[index]
        if char == "'" and not backticked:
            if quoted and index + 1 < len(query) and query[index + 1] == "'":
                normalized.extend(("'", "'"))
                index += 2
                continue
            quoted = not quoted
            if whitespace and normalized:
                normalized.append(" ")
            whitespace = False
            normalized.append(char)
        elif char == "`" and not quoted:
            backticked = not backticked
            if whitespace and normalized:
                normalized.append(" ")
            whitespace = False
            normalized.append(char)
        elif char.isspace() and not quoted and not backticked:
            whitespace = True
        else:
            if whitespace and normalized:
                normalized.append(" ")
            whitespace = False
            normalized.append(char)
        index += 1
    if quoted or backticked:
        raise MigrationError("unterminated quoted text in view query")
    return "".join(normalized)


def _verify_view(
    statement: Statement,
    table: bigquery.Table,
    last_accepted: str,
) -> None:
    if table.table_type != "VIEW":
        raise _failure(
            statement.name,
            "view type",
            "VIEW",
            table.table_type,
            last_accepted,
        )
    expected = normalize_query(_view_query(statement.sql))
    actual = normalize_query(table.view_query or "")
    if actual != expected:
        raise _failure(
            statement.name,
            "view query",
            expected,
            actual,
            last_accepted,
        )


def _wave1_table_contract(table: str, plan: MigrationPlan) -> dict[str, object]:
    sources = _accepted_source_texts()
    source = sources[f"infra/bigquery_schemas/{table}.sql"]
    rendered = source.replace("{project}", plan.project).replace("{dataset}", plan.dataset)
    column_block = rendered.split("(", 1)[1].split(")\nPARTITION BY", 1)[0]
    fields = []
    for line in column_block.splitlines():
        definition = line.strip().rstrip(",")
        if not definition or definition.startswith("--"):
            continue
        match = re.match(r"`?([A-Za-z_][A-Za-z0-9_]*)`?\s+([^\s]+)(.*)\Z", definition)
        if match is None:
            raise MigrationError(f"{table}: unsupported legacy field definition")
        name, field_type, tail = match.groups()
        description_match = re.search(r"OPTIONS\(description = '([^']+)'\)", tail)
        repeated = field_type.startswith("ARRAY<") and field_type.endswith(">")
        if repeated:
            field_type = field_type[6:-1]
        fields.append(
            (
                name,
                _TYPE_ALIASES.get(field_type, field_type),
                "REPEATED" if repeated else "REQUIRED" if "NOT NULL" in tail else "NULLABLE",
                description_match.group(1) if description_match else None,
            )
        )
    partition_match = re.search(r"\nPARTITION BY ([^\n]+)", rendered)
    cluster_match = re.search(r"\nCLUSTER BY ([^\n]+)", rendered)
    expiration_match = re.search(r"partition_expiration_days = (\d+)", rendered)
    description_matches = re.findall(r"description = '([^']+)'", rendered)
    return {
        "fields": tuple(fields),
        "partition": partition_match.group(1).strip() if partition_match else None,
        "cluster": tuple(item.strip() for item in cluster_match.group(1).split(","))
        if cluster_match
        else (),
        "expiration": int(expiration_match.group(1)) if expiration_match else None,
        "description": description_matches[-1] if description_matches else None,
    }


def _verify_wave1_authority_readback(client: Any, plan: MigrationPlan) -> None:
    for table in wave1_authority_upgrade.TABLES:
        expected = _wave1_table_contract(table, plan)
        resource = client.get_table(f"{plan.project}.{plan.dataset}.{table}")
        actual_fields = tuple(
            (
                field.name,
                _TYPE_ALIASES.get(field.field_type, field.field_type),
                field.mode,
                field.description,
            )
            for field in resource.schema
        )
        partition = resource.time_partitioning.field if resource.time_partitioning else None
        expected_partition = _expected_partition_field(expected["partition"])
        expiration = (
            resource.time_partitioning.expiration_ms // 86_400_000
            if resource.time_partitioning and resource.time_partitioning.expiration_ms is not None
            else None
        )
        # By name, not by position: BigQuery appends every added column after the
        # authority block, so a table that gained a column after the upgrade can
        # never match the file's order (enriched_content near_topic, 5 Sep 2026).
        if (
            sorted(actual_fields) != sorted(expected["fields"])
            or partition != expected_partition
            or tuple(resource.clustering_fields or ()) != expected["cluster"]
            or expiration != expected["expiration"]
            or resource.description != expected["description"]
        ):
            raise MigrationError(f"{table}: Wave 1 authority schema readback mismatch")


def _runtime_closure_table_contract(statement: Statement) -> tuple[tuple[str, str, str], ...]:
    if statement.name != _RUNTIME_CLOSURE_SCHEMAS[0] or statement.kind != "table":
        raise MigrationError("runtime closure table contract is invalid")
    fields = tuple(
        match.groups()
        for match in re.finditer(
            r"^  ([a-z0-9_]+) (STRING|TIMESTAMP) NOT NULL "
            r"OPTIONS\(description = '([^']+)'\),?$",
            statement.sql,
            re.MULTILINE,
        )
    )
    if len(fields) != 10 or "PARTITION BY" in statement.sql or "CLUSTER BY" in statement.sql:
        raise MigrationError("runtime closure table contract is invalid")
    return fields


def _routine_body(sql: str) -> str:
    match = re.search(r"\)\s*BEGIN\s*", sql, re.IGNORECASE)
    if match is None:
        raise MigrationError("runtime closure routine body is invalid")
    # BigQuery returns the stored body without the statement closing semicolon.
    return "BEGIN\n" + sql[match.end() :].strip().rstrip(";").rstrip()


def _verify_runtime_closure_statement(
    client: Any, plan: MigrationPlan, statement: Statement
) -> None:
    resource_ref = f"{plan.project}.{plan.dataset}.{statement.name}"
    if statement.kind == "table":
        expected = _runtime_closure_table_contract(statement)
        resource = client.get_table(resource_ref)
        actual = tuple(
            (field.name, _TYPE_ALIASES.get(field.field_type, field.field_type), field.description)
            for field in resource.schema
            if (field.mode or "NULLABLE").upper() == "REQUIRED"
        )
        if (
            actual != expected
            or resource.time_partitioning is not None
            or tuple(resource.clustering_fields or ())
        ):
            raise MigrationError("runtime closure table readback mismatch")
        return
    if statement.kind != "routine":
        raise MigrationError("runtime closure object kind is invalid")
    routine = client.get_routine(resource_ref)
    actual_body = getattr(routine, "body", "")
    if actual_body.startswith("CREATE OR REPLACE PROCEDURE"):
        actual_body = _routine_body(actual_body)
    if (
        getattr(routine, "routine_id", None) != statement.name
        or getattr(routine, "type_", None) != "PROCEDURE"
        or re.sub(r"\s+", " ", actual_body).strip()
        != re.sub(r"\s+", " ", _routine_body(statement.sql)).strip()
    ):
        raise MigrationError(f"{statement.name}: runtime closure routine readback mismatch")


def apply_plan(
    plan: MigrationPlan,
    *,
    credential_loader: Callable[[], tuple[Any, str | None]] = google.auth.default,
    client_factory: Callable[..., Any] = bigquery.Client,
    request_factory: Callable[[], Any] = google_auth_requests.Request,
) -> tuple[str, ...]:
    """Apply an allowlisted plan and read every object back before continuing."""

    _validate_plan(plan)
    credentials = _authorized_credentials(plan, credential_loader, request_factory)
    client = client_factory(
        project=plan.project,
        credentials=credentials,
        location=plan.location,
    )
    try:
        dataset = client.get_dataset(f"{plan.project}.{plan.dataset}")
    except NotFound as exc:
        raise MigrationError(f"dataset does not exist: {plan.project}.{plan.dataset}") from exc
    _verify_dataset(plan, dataset)

    accepted: list[str] = []
    last_accepted = "dataset metadata"
    try:
        for upgrade_statement in plan.wave1_authority_upgrade_statements:
            client.query(upgrade_statement).result()
        if plan.wave1_authority_upgrade_statements:
            _verify_wave1_authority_readback(client, plan)
    except Exception as exc:
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(f"Wave 1 authority upgrade or readback failed: {exc}") from exc
    for statement in plan.statements:
        try:
            client.query(statement.sql).result()
            if statement.name == source_lab_upgrade.TABLE and plan.target == "staging":
                for upgrade_statement in plan.source_lab_upgrade_statements:
                    client.query(upgrade_statement).result()
            for upgrade_statement in signal_identity_upgrade.statements_for(plan, statement.name):
                client.query(upgrade_statement).result()
            resource = client.get_table(f"{plan.project}.{plan.dataset}.{statement.name}")
            if statement.kind == "table":
                _verify_table(statement, resource, last_accepted)
            else:
                _verify_view(statement, resource, last_accepted)
        except Exception as exc:
            if isinstance(exc, MigrationError):
                exc.accepted_objects = tuple(accepted)
                raise
            error = MigrationError(
                f"{statement.name}: query or readback failed; "
                f"last accepted object: {last_accepted}; error: {exc}"
            )
            error.accepted_objects = tuple(accepted)
            raise error from exc
        accepted.append(statement.name)
        last_accepted = statement.name
    for statement in plan.runtime_closure_statements:
        try:
            client.query(statement.sql).result()
            _verify_runtime_closure_statement(client, plan, statement)
        except Exception as exc:
            if isinstance(exc, MigrationError):
                exc.accepted_objects = tuple(accepted)
                raise
            error = MigrationError(
                f"{statement.name}: runtime closure apply or readback failed; "
                f"last accepted object: {last_accepted}; error: {exc}"
            )
            error.accepted_objects = tuple(accepted)
            raise error from exc
        accepted.append(statement.name)
        last_accepted = statement.name
    return tuple(accepted)


def render_dry_run(plan: MigrationPlan) -> str:
    lines = [
        f"Target: {plan.target}",
        f"Project: {plan.project}",
        f"Dataset: {plan.dataset}",
        f"Location: {plan.location}",
        f"Required service identity: {plan.service_account}",
    ]
    if plan.origin_registry_sha256 is not None:
        lines.extend(
            (
                f"Trusted generation: registry {plan.origin_registry_sha256}, "
                f"resources {plan.resource_manifest_sha256}",
                f"Connected repository: {plan.connected_repo}",
                f"Image repository: {plan.image_repository}",
            )
        )
    lines.extend(("", "Object order:"))
    lines.extend(
        f"{index}. {statement.name} ({statement.kind})"
        for index, statement in enumerate(plan.statements, start=1)
    )
    if _is_source_lab_plan(plan):
        lines.extend(("", "Writer state: disabled"))
    lines.extend(("", "Fully rendered forward SQL:"))
    for statement in plan.statements:
        lines.extend((f"[{statement.name}]", statement.sql.rstrip(), ""))
    if plan.source_lab_upgrade_statements:
        lines.extend(("Source Lab additive forward SQL:",))
        lines.extend(f"{statement};" for statement in plan.source_lab_upgrade_statements)
        lines.append("")
    if plan.wave1_authority_upgrade_statements:
        lines.extend(("Wave 1 authority additive forward SQL:",))
        lines.extend(f"{statement};" for statement in plan.wave1_authority_upgrade_statements)
        lines.append("")
    if plan.signal_identity_upgrade_statements:
        lines.extend(("Signal identity additive forward SQL:",))
        lines.extend(f"{statement};" for statement in plan.signal_identity_upgrade_statements)
        lines.append("")
    if plan.runtime_closure_statements:
        lines.extend(("Runtime closure authorized routines:",))
        for statement in plan.runtime_closure_statements:
            lines.extend((f"[{statement.name}]", statement.sql.rstrip(), ""))
    if plan.runtime_closure_iam_bindings:
        lines.extend(("Runtime closure IAM plan only:",))
        for binding in plan.runtime_closure_iam_bindings:
            lines.append(
                f"{binding.principal} | {binding.role} | {binding.resource} | {binding.purpose}"
            )
        lines.append("")
    lines.extend(
        (
            "Readback checks on apply:",
            "Dataset project, dataset id, location, and no default table expiry.",
            "Every field name, type, mode, description, and nested field.",
            "Partition field, partition retention, clustering order, and table description.",
            "View type and normalized accepted query text.",
            "",
            "Rollback plan only:",
            "Deletion requires separate explicit destructive authorization.",
        )
    )
    lines.extend(plan.rollback_statements)
    return "\n".join(lines)


def _migration_plan_payload(plan: MigrationPlan) -> dict[str, object]:
    def statement(item: Statement) -> dict[str, str]:
        return {"name": item.name, "kind": item.kind, "sql": item.sql}

    return {
        "target": plan.target,
        "project": plan.project,
        "dataset": plan.dataset,
        "location": plan.location,
        "service_account": plan.service_account,
        "origin_registry_sha256": plan.origin_registry_sha256,
        "resource_manifest_sha256": plan.resource_manifest_sha256,
        "connected_repo": plan.connected_repo,
        "image_repository": plan.image_repository,
        "statements": tuple(statement(item) for item in plan.statements),
        "source_lab_upgrade_statements": plan.source_lab_upgrade_statements,
        "wave1_authority_upgrade_statements": plan.wave1_authority_upgrade_statements,
        "runtime_closure_statements": tuple(
            statement(item) for item in plan.runtime_closure_statements
        ),
        "runtime_closure_iam_bindings": tuple(
            {
                "principal": item.principal,
                "resource": item.resource,
                "role": item.role,
                "purpose": item.purpose,
            }
            for item in plan.runtime_closure_iam_bindings
        ),
        "rollback_statements": plan.rollback_statements,
    }


def _build_migration_execution_artifacts(plan: MigrationPlan) -> dict[str, bytes]:
    if plan.target != "staging" or plan.dataset != "trends_v2_staging":
        raise MigrationError("durable migration artifacts require the exact staging plan")
    return {
        "cloud_build": (_ROOT / "cloudbuild.staging.yaml").read_bytes(),
        "migration_contract": (
            _ROOT / "docs" / "contracts" / "open_intelligence_v2.md"
        ).read_bytes(),
        "migration_dry_run": render_dry_run(plan).encode("utf-8"),
        "migration_plan": canonical_bytes(_migration_plan_payload(plan)),
    }


def _execution_approval_artifact_bytes(name: str) -> bytes:
    if _DURABLE_ARTIFACT_CONTEXT is None or name not in _DURABLE_ARTIFACT_CONTEXT:
        raise MigrationError("migration execution approval artifact is unavailable")
    return _DURABLE_ARTIFACT_CONTEXT[name]


def _require_plan_authority(plan: MigrationPlan, authority: object) -> None:
    """The issued authority must carry the generation and origin the plan was built from.

    Job, principal, repository and image namespace all come from that origin's
    migration_apply binding; an authority without a trusted generation, or one issued
    under another generation or job, is refused before consumption.
    """
    generation = getattr(authority, "generation", None)
    manifest = getattr(authority, "manifest", None)
    approval = getattr(authority, "approval", None)
    registry = getattr(generation, "registry", None)
    pair = (
        getattr(generation, "origin_registry_sha256", None),
        getattr(generation, "resource_manifest_sha256", None),
    )
    if (
        type(approval) is not execution_approval.ExecutionApprovalV2
        or registry is None
        or pair != (plan.origin_registry_sha256, plan.resource_manifest_sha256)
        or (approval.origin_registry_sha256, approval.resource_manifest_sha256) != pair
    ):
        raise MigrationError("migration authority is not issued under the plan's origin generation")
    try:
        origin = select_origin(
            manifest_version=manifest.manifest_version,
            contract_sha256=manifest.contract_sha256,
            mode="new_consume",
            registry=registry,
        )
        binding = origin.operation_bindings["migration_apply"]
    except Exception as error:
        raise MigrationError("migration authority origin is not admitted") from error
    image_uri = getattr(authority, "image_uri", None)
    if (
        getattr(authority, "operation", None) != "migration_apply"
        or getattr(authority, "job_resource", None) != binding.job_resource
        or manifest.job_resource != binding.job_resource
        or manifest.service_identity != binding.service_identity
        or plan.service_account != binding.service_identity
        or plan.connected_repo != origin.connected_repo
        or plan.image_repository != origin.image_repository
        or not isinstance(image_uri, str)
        or re.fullmatch(origin.image_uri_regex, image_uri) is None
    ):
        raise MigrationError("migration authority differs from the plan's origin binding")


def _execute_durable_migration(
    *,
    authority_loader=execution_approval._load_execution_authority,
    authority_consumer=execution_approval._consume_execution_authority,
    plan_applier=None,
    result_recorder=execution_approval._record_execution_result,
) -> dict[str, object]:
    global _DURABLE_ARTIFACT_CONTEXT
    if _DURABLE_ARTIFACT_CONTEXT is not None:
        raise MigrationError("migration execution approval context is already active")
    plan = build_plan("staging")
    artifacts = _build_migration_execution_artifacts(plan)
    _DURABLE_ARTIFACT_CONTEXT = artifacts
    try:
        authority = authority_loader("migration_apply", mode="new_consume")
        _require_plan_authority(plan, authority)
        consumption = authority_consumer(authority)
        apply = plan_applier or (lambda selected: apply_plan(selected))
        try:
            accepted = tuple(apply(plan))
            expected = tuple(
                item.name for item in (*plan.statements, *plan.runtime_closure_statements)
            )
            if accepted != expected:
                raise MigrationError("durable migration accepted-object readback is incomplete")
        except Exception as error:
            accepted_objects = tuple(getattr(error, "accepted_objects", ()))
            failed_payload = {
                "contract_version": "open_intelligence_migration_result_v1",
                "status": "failed",
                "project": plan.project,
                "dataset": plan.dataset,
                "accepted_objects": accepted_objects,
                "migration_plan_sha256": hashlib.sha256(artifacts["migration_plan"]).hexdigest(),
                "migration_dry_run_sha256": hashlib.sha256(
                    artifacts["migration_dry_run"]
                ).hexdigest(),
                "error_code": "migration_apply_failed",
            }
            failed_json = canonical_bytes(failed_payload).decode("utf-8")
            failed_digest = hashlib.sha256(failed_json.encode("utf-8")).hexdigest()
            result_recorder(
                authority,
                consumption,
                f"bq://{plan.project}.{plan.dataset}#{failed_payload['migration_plan_sha256']}"
                f"/{consumption.consumption_id}",
                failed_json,
                failed_digest,
                "failed",
            )
            raise
        stored_payload = {
            "contract_version": "open_intelligence_migration_result_v1",
            "status": "succeeded",
            "project": plan.project,
            "dataset": plan.dataset,
            "accepted_objects": accepted,
            "migration_plan_sha256": hashlib.sha256(artifacts["migration_plan"]).hexdigest(),
            "migration_dry_run_sha256": hashlib.sha256(artifacts["migration_dry_run"]).hexdigest(),
        }
        canonical_result_json = canonical_bytes(stored_payload).decode("utf-8")
        result_digest = hashlib.sha256(canonical_result_json.encode("utf-8")).hexdigest()
        result = result_recorder(
            authority,
            consumption,
            f"bq://{plan.project}.{plan.dataset}#{stored_payload['migration_plan_sha256']}"
            f"/{consumption.consumption_id}",
            canonical_result_json,
            result_digest,
            "succeeded",
        )
        return {
            **stored_payload,
            "execution_approval": {
                "manifest_sha256": authority.approval.manifest_sha256,
                "approval_id": authority.approval.approval_id,
                "consumption_id": consumption.consumption_id,
                "result_id": result.result_id,
            },
        }
    finally:
        _DURABLE_ARTIFACT_CONTEXT = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or apply the Open Intelligence v2 staging migration."
    )
    parser.add_argument("--target", required=True, choices=tuple(_TARGET_DATASETS))
    parser.add_argument("--source-lab-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    credential_loader: Callable[[], tuple[Any, str | None]] = google.auth.default,
    client_factory: Callable[..., Any] = bigquery.Client,
    request_factory: Callable[[], Any] = google_auth_requests.Request,
) -> None:
    values = tuple(sys.argv[1:] if argv is None else argv)
    if values == ("apply",):
        print(canonical_bytes(_execute_durable_migration()).decode("utf-8"))
        return
    parser = _parser()
    args = parser.parse_args(values)
    if args.source_lab_only and args.target != "staging":
        parser.error("--source-lab-only only supports --target staging")
    plan = build_source_lab_plan() if args.source_lab_only else build_plan(args.target)
    if not args.apply:
        print(render_dry_run(plan))
        return
    if args.target == "staging" and not args.source_lab_only:
        raise MigrationError("durable migration wrapper is required")
    accepted = apply_plan(
        plan,
        credential_loader=credential_loader,
        client_factory=client_factory,
        request_factory=request_factory,
    )
    print(f"Accepted {len(accepted)} objects in order: {', '.join(accepted)}")


if __name__ == "__main__":
    main()
