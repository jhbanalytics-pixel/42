"""Staging-only replay source-copy infrastructure migration."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import google.auth
from google.cloud import bigquery

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
LOCATION = "US"
SERVICE_ACCOUNT = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
COPY_CONTRACT_VERSION = "dynamic_replay_source_copy_v1"
COPY_RUN_ID = "dynamic_replay_source_copy_20260821_20260903_v1"

_BQ_TYPE_ALIASES = {
    "INTEGER": "INT64",
    "FLOAT": "FLOAT64",
    "BOOLEAN": "BOOL",
}

LOCK_SCHEMA = (
    ("copy_run_id", "STRING", "REQUIRED"),
    ("copy_contract_version", "STRING", "REQUIRED"),
    ("lock_version", "INT64", "REQUIRED"),
    ("state", "STRING", "REQUIRED"),
    ("created_at", "TIMESTAMP", "REQUIRED"),
    ("updated_at", "TIMESTAMP", "REQUIRED"),
)
MANIFEST_SCHEMA = (
    ("copy_run_id", "STRING", "REQUIRED"),
    ("copy_contract_version", "STRING", "REQUIRED"),
    ("source_project", "STRING", "REQUIRED"),
    ("source_dataset", "STRING", "REQUIRED"),
    ("target_dataset", "STRING", "REQUIRED"),
    ("source_table", "STRING", "REQUIRED"),
    ("target_table", "STRING", "REQUIRED"),
    ("copy_row_id", "STRING", "REQUIRED"),
    ("source_content_sha256", "STRING", "REQUIRED"),
    ("window_start", "DATE", "REQUIRED"),
    ("window_end", "DATE", "REQUIRED"),
    ("inserted_at", "TIMESTAMP", "REQUIRED"),
)
RECEIPT_SCHEMA = (
    ("copy_run_id", "STRING", "REQUIRED"),
    ("copy_contract_version", "STRING", "REQUIRED"),
    ("source_project", "STRING", "REQUIRED"),
    ("source_dataset", "STRING", "REQUIRED"),
    ("target_dataset", "STRING", "REQUIRED"),
    ("source_table", "STRING", "REQUIRED"),
    ("target_table", "STRING", "REQUIRED"),
    ("window_start", "DATE", "REQUIRED"),
    ("window_end", "DATE", "REQUIRED"),
    ("filter_digest", "STRING", "REQUIRED"),
    ("schema_digest", "STRING", "REQUIRED"),
    ("source_set_digest", "STRING", "REQUIRED"),
    ("coverage_digest", "STRING", "NULLABLE"),
    ("source_rows", "INT64", "REQUIRED"),
    ("zero_row_status", "STRING", "REQUIRED"),
    ("hard_ceiling", "INT64", "REQUIRED"),
)


class InfraRefusal(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InfraStatement:
    name: str
    table_ref: str
    sql: str
    schema: tuple[tuple[str, str, str], ...]
    partition_field: str | None
    clustering_fields: tuple[str, ...]
    description: str


@dataclass(frozen=True, slots=True)
class InfraPlan:
    target: str
    project: str
    dataset: str
    location: str
    service_account: str
    copy_run_id: str
    statements: tuple[InfraStatement, ...]


def _columns(schema: tuple[tuple[str, str, str], ...]) -> str:
    return ",\n  ".join(
        f"{name} {field_type}{' NOT NULL' if mode == 'REQUIRED' else ''}"
        for name, field_type, mode in schema
    )


def _create_sql(
    table_ref: str,
    schema: tuple[tuple[str, str, str], ...],
    *,
    partition: str,
    cluster: tuple[str, ...],
    description: str,
) -> str:
    return (
        f"CREATE TABLE IF NOT EXISTS `{table_ref}` (\n  {_columns(schema)}\n)\n"
        f"PARTITION BY {partition}\nCLUSTER BY {', '.join(cluster)}\n"
        f"OPTIONS(description={description!r});"
    )


def build_plan(target: str, copy_run_id: str) -> InfraPlan:
    if target != "staging":
        raise InfraRefusal("target_invalid")
    if copy_run_id != COPY_RUN_ID:
        raise InfraRefusal("copy_run_id_invalid")
    lock_name = "open_intelligence_source_copy_lock_v1"
    manifest_name = "open_intelligence_source_copy_manifest_v1"
    receipt_name = "open_intelligence_source_copy_receipts_v1"
    lock_ref = f"{PROJECT}.{DATASET}.{lock_name}"
    manifest_ref = f"{PROJECT}.{DATASET}.{manifest_name}"
    receipt_ref = f"{PROJECT}.{DATASET}.{receipt_name}"
    lock_description = (
        "Singleton transaction lock for one approved Open Intelligence replay source copy."
    )
    manifest_description = "Owned rows inserted by one approved replay source copy."
    receipt_description = "Immutable frozen source receipts for one approved replay source copy."
    lock_sql = (
        f"CREATE TABLE IF NOT EXISTS `{lock_ref}` (\n  {_columns(LOCK_SCHEMA)}\n)\n"
        f"OPTIONS(description={lock_description!r})\nAS SELECT\n"
        f"  {COPY_RUN_ID!r} AS copy_run_id,\n"
        f"  {COPY_CONTRACT_VERSION!r} AS copy_contract_version,\n"
        "  CAST(0 AS INT64) AS lock_version,\n  'ready' AS state,\n"
        "  CURRENT_TIMESTAMP() AS created_at,\n  CURRENT_TIMESTAMP() AS updated_at;"
    )
    statements = (
        InfraStatement(lock_name, lock_ref, lock_sql, LOCK_SCHEMA, None, (), lock_description),
        InfraStatement(
            manifest_name,
            manifest_ref,
            _create_sql(
                manifest_ref,
                MANIFEST_SCHEMA,
                partition="window_end",
                cluster=("copy_run_id", "target_table"),
                description=manifest_description,
            ),
            MANIFEST_SCHEMA,
            "window_end",
            ("copy_run_id", "target_table"),
            manifest_description,
        ),
        InfraStatement(
            receipt_name,
            receipt_ref,
            _create_sql(
                receipt_ref,
                RECEIPT_SCHEMA,
                partition="window_end",
                cluster=("copy_run_id", "source_table"),
                description=receipt_description,
            ),
            RECEIPT_SCHEMA,
            "window_end",
            ("copy_run_id", "source_table"),
            receipt_description,
        ),
    )
    return InfraPlan(target, PROJECT, DATASET, LOCATION, SERVICE_ACCOUNT, copy_run_id, statements)


def _validate_plan(plan: InfraPlan) -> None:
    if not isinstance(plan, InfraPlan):
        raise InfraRefusal("plan_invalid")
    if plan != build_plan(plan.target, plan.copy_run_id):
        raise InfraRefusal("plan_invalid")


def _verify_table(client: Any, statement: InfraStatement) -> None:
    table = client.get_table(statement.table_ref)
    actual = tuple(
        (
            field.name,
            _BQ_TYPE_ALIASES.get(field.field_type.upper(), field.field_type.upper()),
            (field.mode or "NULLABLE").upper(),
        )
        for field in table.schema
    )
    if actual != statement.schema:
        raise InfraRefusal("infrastructure_schema_mismatch")
    partition = table.time_partitioning.field if table.time_partitioning else None
    if partition != statement.partition_field:
        raise InfraRefusal("infrastructure_schema_mismatch")
    if tuple(table.clustering_fields or ()) != statement.clustering_fields:
        raise InfraRefusal("infrastructure_schema_mismatch")
    if table.description != statement.description:
        raise InfraRefusal("infrastructure_schema_mismatch")


def execute_plan(plan: InfraPlan, *, apply: bool, client: Any | None = None) -> dict[str, object]:
    _validate_plan(plan)
    if client is None:
        client = _default_client(plan)
    if not apply:
        config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        for statement in plan.statements:
            client.query(statement.sql, location=plan.location, job_config=config).result()
        return {
            "mode": "dry-run",
            "target": plan.target,
            "copy_run_id": plan.copy_run_id,
            "statement_count": len(plan.statements),
            "write_state": "not_started",
        }
    for statement in plan.statements:
        client.query(statement.sql, location=plan.location).result()
        _verify_table(client, statement)
    lock_sql = (
        "SELECT COUNT(*) total_lock_rows,COUNTIF(copy_run_id=@copy_run_id) matching_lock_rows,"
        "ANY_VALUE(IF(copy_run_id=@copy_run_id,copy_run_id,NULL)) copy_run_id,"
        "ANY_VALUE(IF(copy_run_id=@copy_run_id,copy_contract_version,NULL)) copy_contract_version,"
        "ANY_VALUE(IF(copy_run_id=@copy_run_id,lock_version,NULL)) lock_version,"
        "ANY_VALUE(IF(copy_run_id=@copy_run_id,state,NULL)) state FROM "
        f"`{plan.statements[0].table_ref}`"
    )
    lock_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("copy_run_id", "STRING", plan.copy_run_id)]
    )
    rows = tuple(
        client.query(
            lock_sql,
            location=plan.location,
            job_config=lock_config,
        ).result()
    )
    if len(rows) != 1:
        raise InfraRefusal("lock_conflict")
    row = rows[0]
    if (
        row["total_lock_rows"] != 1
        or row["matching_lock_rows"] != 1
        or row["copy_run_id"] != COPY_RUN_ID
        or row["copy_contract_version"] != COPY_CONTRACT_VERSION
        or not isinstance(row["lock_version"], int)
        or row["lock_version"] < 0
        or row["state"] != "ready"
    ):
        raise InfraRefusal("lock_conflict")
    return {
        "mode": "apply",
        "target": plan.target,
        "copy_run_id": plan.copy_run_id,
        "tables_created_or_matched": 3,
        "lock_rows": 1,
    }


def _default_client(plan: InfraPlan) -> Any:
    credentials, adc_project = google.auth.default()
    identity = getattr(credentials, "service_account_email", None)
    if adc_project != plan.project or identity != plan.service_account:
        raise InfraRefusal("runtime_identity_invalid")
    return bigquery.Client(project=plan.project, credentials=credentials, location=plan.location)


def parser() -> argparse.ArgumentParser:
    output = argparse.ArgumentParser()
    output.add_argument("--target", required=True, choices=("staging",))
    output.add_argument("--copy-run-id", required=True)
    output.add_argument("--apply", action="store_true")
    return output


def main(argv: Sequence[str] | None = None, *, client: Any | None = None) -> int:
    args = parser().parse_args(argv)
    result = execute_plan(
        build_plan(args.target, args.copy_run_id), apply=args.apply, client=client
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
