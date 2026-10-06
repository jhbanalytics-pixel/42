"""Plan and gate the fixed staging Gemini canary approval store migration."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import google.auth
from google.api_core.exceptions import NotFound
from google.auth.compute_engine import credentials as compute_credentials
from google.auth.transport import requests as google_auth_requests
from google.cloud import bigquery
from google.oauth2 import service_account

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
LOCATION = "US"
MIGRATION_IDENTITY = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
CANARY_MEMBER = "serviceAccount:trends-engine-canary@ogilvy-trends-v2.iam.gserviceaccount.com"
# The one human who may approve a canary answering plan. Deliberately not the
# identity that owns and deploys the project: owning the infrastructure is not
# authority over what the model is asked to do, and no machine identity is ever
# an approver. trends-engine-canary@ stays the planning writer and approved
# answer reader, which is what its table-scoped IAM above already grants.
CANARY_HUMAN_APPROVER = "albert.meintjes@ogilvy.co.za"
_INFRASTRUCTURE_IDENTITY = "jhb.analytics@gmail.com"

_SCHEMAS = {
    "gemini_canary_planning_outputs_v1": (
        ("storage_version", "STRING", "REQUIRED"),
        ("planning_output_id", "STRING", "REQUIRED"),
        ("manifest_sha256", "STRING", "REQUIRED"),
        ("task_ordinal", "INT64", "REQUIRED"),
        ("task_id", "STRING", "REQUIRED"),
        ("stage", "STRING", "REQUIRED"),
        ("plan_id", "STRING", "REQUIRED"),
        ("plan_schema_version", "STRING", "REQUIRED"),
        ("canonical_plan_json", "STRING", "REQUIRED"),
        ("plan_digest", "STRING", "REQUIRED"),
        ("canonical_claim_requirements_json", "STRING", "REQUIRED"),
        ("claim_requirements_digest", "STRING", "REQUIRED"),
        ("planning_receipt_id", "STRING", "REQUIRED"),
        ("usage_event_id", "STRING", "REQUIRED"),
        ("writer_principal_id", "STRING", "REQUIRED"),
        ("persisted_at", "TIMESTAMP", "REQUIRED"),
    ),
    "gemini_canary_plan_approvals_v1": (
        ("contract_version", "STRING", "REQUIRED"),
        ("manifest_sha256", "STRING", "REQUIRED"),
        ("task_id", "STRING", "REQUIRED"),
        ("stage", "STRING", "REQUIRED"),
        ("plan_id", "STRING", "REQUIRED"),
        ("plan_digest", "STRING", "REQUIRED"),
        ("claim_requirements", "STRING", "REQUIRED"),
        ("approved_by", "STRING", "REQUIRED"),
        ("approved_at", "TIMESTAMP", "REQUIRED"),
        ("approval_id", "STRING", "REQUIRED"),
    ),
    "gemini_canary_approval_companions_v1": (
        ("companion_version", "STRING", "REQUIRED"),
        ("contract_version", "STRING", "REQUIRED"),
        ("manifest_sha256", "STRING", "REQUIRED"),
        ("task_count", "INT64", "REQUIRED"),
        ("task_ids", "STRING", "REPEATED"),
        ("approval_records", "STRING", "REQUIRED"),
        ("companion_sha256", "STRING", "REQUIRED"),
        ("canonical_companion_json", "STRING", "REQUIRED"),
    ),
    "gemini_canary_approval_lock_v1": (
        ("lock_name", "STRING", "REQUIRED"),
        ("approval_contract_version", "STRING", "REQUIRED"),
        ("lock_version", "INT64", "REQUIRED"),
        ("state", "STRING", "REQUIRED"),
        ("last_manifest_sha256", "STRING", "NULLABLE"),
        ("created_at", "TIMESTAMP", "REQUIRED"),
        ("updated_at", "TIMESTAMP", "REQUIRED"),
    ),
}

_SCHEMA_DIGESTS = {
    "gemini_canary_planning_outputs_v1": (
        "pse_5069dde6b0ddc7098ac20c8caf70151457fd6a92a4eb1bfbeb27a13083b3bd14"
    ),
    "gemini_canary_plan_approvals_v1": (
        "pse_9cc29d1e3d06713be8f368955429c5fb16d3583375193e0fb1f32b0ca30c0c49"
    ),
    "gemini_canary_approval_companions_v1": (
        "pse_2f9d3f3b3946ba22ef956ae2c4d77e4646a6631f1a94284f6ae82bd8246fea48"
    ),
    "gemini_canary_approval_lock_v1": (
        "pse_057c12363143e9a4fde0cc4b28dd0ae8dc451a994a75f61ba89d8e7827cf379e"
    ),
}

_DESCRIPTIONS = {
    "gemini_canary_planning_outputs_v1": (
        "Immutable validated Gemini canary planning outputs for one frozen caller manifest."
    ),
    "gemini_canary_plan_approvals_v1": (
        "Immutable human approval records for Gemini canary answering plans."
    ),
    "gemini_canary_approval_companions_v1": (
        "Immutable canonical approval companions for complete Gemini canary batches."
    ),
    "gemini_canary_approval_lock_v1": (
        "Singleton mutable transaction lock for Gemini canary planning and approval writes."
    ),
}

_IAM = (
    ("gemini_canary_planning_outputs_v1", "roles/bigquery.dataEditor", CANARY_MEMBER),
    ("gemini_canary_approval_lock_v1", "roles/bigquery.dataEditor", CANARY_MEMBER),
    ("gemini_canary_plan_approvals_v1", "roles/bigquery.dataViewer", CANARY_MEMBER),
    ("gemini_canary_approval_companions_v1", "roles/bigquery.dataViewer", CANARY_MEMBER),
)

_LOCK_NAME = "gemini_canary_approval_v1"
_CONTRACT_VERSION = "gemini_canary_plan_approval_v1"
_LOCK_TABLE = "gemini_canary_approval_lock_v1"
_TYPE_ALIASES = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}


class MigrationRefusal(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _TableSpec:
    name: str
    table_ref: str
    schema: tuple[tuple[str, str, str], ...]
    digest: str
    description: str
    ddl: str


def _schema_json(schema: tuple[tuple[str, str, str], ...]) -> str:
    return json.dumps(
        [{"name": name, "type": field_type, "mode": mode} for name, field_type, mode in schema],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _schema_digest(schema: tuple[tuple[str, str, str], ...]) -> str:
    return "pse_" + hashlib.sha256(_schema_json(schema).encode("utf-8")).hexdigest()


def _column_sql(field: tuple[str, str, str]) -> str:
    name, field_type, mode = field
    if mode == "REPEATED":
        return f"{name} ARRAY<{field_type}> NOT NULL"
    suffix = " NOT NULL" if mode == "REQUIRED" else ""
    return f"{name} {field_type}{suffix}"


def _ddl(name: str, schema: tuple[tuple[str, str, str], ...], description: str) -> str:
    table_ref = f"{PROJECT}.{DATASET}.{name}"
    columns = ",\n  ".join(_column_sql(field) for field in schema)
    return (
        f"CREATE TABLE IF NOT EXISTS `{table_ref}` (\n  {columns}\n)\n"
        f"OPTIONS(description='{description}');"
    )


def _table_specs() -> tuple[_TableSpec, ...]:
    specs = []
    for name, schema in _SCHEMAS.items():
        digest = _schema_digest(schema)
        if digest != _SCHEMA_DIGESTS[name]:
            raise MigrationRefusal(f"canary_approval_plan_invalid: schema digest drift for {name}")
        specs.append(
            _TableSpec(
                name=name,
                table_ref=f"{PROJECT}.{DATASET}.{name}",
                schema=schema,
                digest=digest,
                description=_DESCRIPTIONS[name],
                ddl=_ddl(name, schema, _DESCRIPTIONS[name]),
            )
        )
    return tuple(specs)


def _lock_seed_sql() -> str:
    table_ref = f"{PROJECT}.{DATASET}.{_LOCK_TABLE}"
    return f"""INSERT INTO `{table_ref}`
  (lock_name, approval_contract_version, lock_version, state,
   last_manifest_sha256, created_at, updated_at)
SELECT
  '{_LOCK_NAME}',
  '{_CONTRACT_VERSION}',
  0,
  'ready',
  NULL,
  CURRENT_TIMESTAMP(),
  CURRENT_TIMESTAMP()
WHERE NOT EXISTS (SELECT 1 FROM `{table_ref}`);"""


def _plan_output() -> dict[str, object]:
    specs = _table_specs()
    return {
        "mode": "plan",
        "target": {"project": PROJECT, "dataset": DATASET, "location": LOCATION},
        "ddl_statements": [spec.ddl for spec in specs],
        "lock_seed_statement": _lock_seed_sql(),
        "intended_table_iam_bindings": [
            {"table": table, "role": role, "member": member} for table, role, member in _IAM
        ],
        "schema_digests": {spec.name: spec.digest for spec in specs},
    }


def _normalized_schema(table: Any) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (
            field.name,
            _TYPE_ALIASES.get(field.field_type.upper(), field.field_type.upper()),
            (field.mode or "NULLABLE").upper(),
        )
        for field in table.schema
    )


def _table_mismatches(table: Any, spec: _TableSpec) -> list[str]:
    checks = {
        "schema": (_normalized_schema(table), spec.schema),
        "description": (table.description, spec.description),
        "location": (getattr(table, "location", None), LOCATION),
        "expiration": (getattr(table, "expires", None), None),
        "partitioning": (getattr(table, "time_partitioning", None), None),
        "range_partitioning": (getattr(table, "range_partitioning", None), None),
        "clustering": (tuple(getattr(table, "clustering_fields", None) or ()), ()),
        "type": (getattr(table, "table_type", None), "TABLE"),
    }
    return [name for name, (actual, expected) in checks.items() if actual != expected]


def _dataset_mismatches(dataset: Any) -> list[str]:
    checks = {
        "project": (getattr(dataset, "project", None), PROJECT),
        "dataset": (getattr(dataset, "dataset_id", None), DATASET),
        "location": (getattr(dataset, "location", None), LOCATION),
        "default_table_expiration": (
            getattr(dataset, "default_table_expiration_ms", None),
            None,
        ),
        "default_partition_expiration": (
            getattr(dataset, "default_partition_expiration_ms", None),
            None,
        ),
    }
    return [name for name, (actual, expected) in checks.items() if actual != expected]


def _lock_read_sql() -> str:
    return f"""SELECT
  lock_name,
  approval_contract_version,
  lock_version,
  state,
  last_manifest_sha256,
  created_at,
  updated_at
FROM `{PROJECT}.{DATASET}.{_LOCK_TABLE}`
LIMIT 2"""


def _lock_matches(rows: Sequence[Any]) -> bool:
    if len(rows) != 1:
        return False
    row = rows[0]
    created_at = row["created_at"]
    updated_at = row["updated_at"]
    return (
        row["lock_name"] == _LOCK_NAME
        and row["approval_contract_version"] == _CONTRACT_VERSION
        and row["lock_version"] == 0
        and row["state"] == "ready"
        and row["last_manifest_sha256"] is None
        and isinstance(created_at, datetime)
        and isinstance(updated_at, datetime)
        and created_at.tzinfo is not None
        and updated_at.tzinfo is not None
        and created_at == updated_at
    )


def _validate_approval_principal(value: object) -> str:
    """The approving principal, or a refusal. There is exactly one accepted.

    An allowlist of one, compared after normalising case and surrounding
    whitespace so the same human is not two identities. Everything else is
    refused by exclusion rather than by pattern, so a lookalike domain, a
    service account or a future unknown principal cannot slip through a rule
    that was only written to catch the cases someone thought of.
    """
    if not isinstance(value, str):
        raise MigrationRefusal("approval principal must be a string")
    principal = value.strip().lower()
    if not principal:
        raise MigrationRefusal("approval principal must not be empty")
    if principal == CANARY_HUMAN_APPROVER:
        return principal
    if principal == _INFRASTRUCTURE_IDENTITY:
        raise MigrationRefusal(
            "the infrastructure and deployment identity may not approve a canary plan"
        )
    if "gserviceaccount.com" in principal or principal.startswith("serviceaccount:"):
        raise MigrationRefusal("a machine identity may not approve a canary plan")
    raise MigrationRefusal(f"approval principal is not the approved human approver: {principal!r}")


def _policy_members(policy: Any, role: str) -> set[str]:
    if isinstance(policy, Mapping):
        return set(policy.get(role, ()))
    try:
        return set(policy[role])
    except (KeyError, TypeError):
        return set()


def _authorized_client() -> Any:
    credentials, adc_project = google.auth.default()
    if adc_project != PROJECT:
        raise MigrationRefusal("canary_approval_identity_invalid")
    credential_type = type(credentials)
    if credential_type not in (
        service_account.Credentials,
        compute_credentials.Credentials,
    ):
        raise MigrationRefusal("canary_approval_identity_invalid")
    quota_project = credentials.quota_project_id
    if quota_project is not None and quota_project != PROJECT:
        raise MigrationRefusal("canary_approval_identity_invalid")
    identity = credentials.service_account_email
    if credential_type is compute_credentials.Credentials and identity in (None, "", "default"):
        try:
            credentials.refresh(google_auth_requests.Request())
        except Exception:
            raise MigrationRefusal("canary_approval_identity_invalid") from None
        identity = credentials.service_account_email
    if identity != MIGRATION_IDENTITY:
        raise MigrationRefusal("canary_approval_identity_invalid")
    return bigquery.Client(project=PROJECT, credentials=credentials, location=LOCATION)


def _apply() -> dict[str, object]:
    specs = _table_specs()
    client = _authorized_client()
    dataset = client.get_dataset(f"{PROJECT}.{DATASET}")
    problems = [f"dataset:{name}" for name in _dataset_mismatches(dataset)]
    discovered = {}
    for spec in specs:
        try:
            table = client.get_table(spec.table_ref)
        except NotFound:
            table = None
        discovered[spec.name] = table
        if table is not None:
            problems.extend(f"{spec.name}:{name}" for name in _table_mismatches(table, spec))

    if discovered[_LOCK_TABLE] is not None:
        rows = tuple(client.query(_lock_read_sql(), location=LOCATION).result())
        if not _lock_matches(rows):
            problems.append("lock")
    if problems:
        raise MigrationRefusal("canary_approval_storage_schema_mismatch: " + ",".join(problems))

    created_tables = []
    for spec in specs:
        if discovered[spec.name] is None:
            client.query(spec.ddl, location=LOCATION).result()
            created_tables.append(spec.table_ref)
    lock_seeded = discovered[_LOCK_TABLE] is None
    if lock_seeded:
        client.query(_lock_seed_sql(), location=LOCATION).result()

    readback_problems = []
    for spec in specs:
        table = client.get_table(spec.table_ref)
        readback_problems.extend(f"{spec.name}:{name}" for name in _table_mismatches(table, spec))
    rows = tuple(client.query(_lock_read_sql(), location=LOCATION).result())
    if not _lock_matches(rows):
        readback_problems.append("lock")
    if readback_problems:
        raise MigrationRefusal(
            "canary_approval_storage_schema_mismatch: " + ",".join(readback_problems)
        )

    current = []
    delta = []
    for table, role, member in _IAM:
        policy = client.get_iam_policy(f"{PROJECT}.{DATASET}.{table}")
        members = sorted(_policy_members(policy, role))
        current.append({"table": table, "role": role, "members": members})
        if member not in members:
            delta.append({"table": table, "role": role, "member": member})
    return {
        "mode": "apply",
        "created_tables": created_tables,
        "lock_seeded": lock_seeded,
        "current_table_iam_bindings": current,
        "iam_delta": delta,
        "iam_mutation": "not_executed",
        "approval_dml": "not_executed",
    }


def parser() -> argparse.ArgumentParser:
    output = argparse.ArgumentParser()
    output.add_argument("command", choices=("plan", "apply"))
    return output


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    result = _plan_output() if args.command == "plan" else _apply()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
