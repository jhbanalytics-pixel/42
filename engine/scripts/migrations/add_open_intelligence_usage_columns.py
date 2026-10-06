"""Add nullable per-call metadata to the staging gemini_usage table."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import Any

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
LOCATION = "US"
SERVICE_ACCOUNT = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
TABLE = "gemini_usage"
COLUMNS: tuple[tuple[str, str], ...] = (
    ("run_id", "STRING"),
    ("stage", "STRING"),
    ("call_index", "INT64"),
)


class UsageColumnMigrationError(RuntimeError):
    """The table resource does not match the approved nullable schema."""


def validate_target(
    project: str,
    dataset: str,
    location: str,
    service_account: str,
) -> None:
    if (
        project != PROJECT
        or dataset != DATASET
        or location != LOCATION
        or service_account != SERVICE_ACCOUNT
    ):
        raise ValueError(
            "migration is staging only and requires the approved project, "
            "dataset, location, and service account"
        )


def build_statements(project: str = PROJECT, dataset: str = DATASET) -> list[str]:
    table_ref = f"{project}.{dataset}.{TABLE}"
    return [
        f"ALTER TABLE `{table_ref}` ADD COLUMN IF NOT EXISTS {name} {field_type}"
        for name, field_type in COLUMNS
    ]


def verify_columns(table: Any) -> None:
    fields = {field.name: field for field in table.schema}
    problems: list[str] = []
    for name, expected_type in COLUMNS:
        field = fields.get(name)
        if field is None:
            problems.append(f"missing {name}")
            continue
        actual_type = "INT64" if field.field_type == "INTEGER" else field.field_type
        if actual_type != expected_type or field.mode != "NULLABLE":
            problems.append(
                f"{name} is {field.field_type} {field.mode}, expected {expected_type} NULLABLE"
            )
    if problems:
        raise UsageColumnMigrationError("schema verification failed: " + "; ".join(problems))


def _verify_client(client: Any, service_account: str) -> None:
    credentials = getattr(client, "_credentials", None)
    credential_email = getattr(credentials, "service_account_email", None)
    if credential_email in (None, "default"):
        from google.auth.transport.requests import Request

        credentials.refresh(Request())
        credential_email = getattr(credentials, "service_account_email", None)
    if credential_email != service_account:
        raise ValueError("migration requires the approved staging service account")
    if client.project != PROJECT or client.location != LOCATION:
        raise ValueError("migration client is outside the approved staging target")


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[..., Any] | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--dataset", default=DATASET)
    parser.add_argument("--location", default=LOCATION)
    parser.add_argument("--service-account", default=SERVICE_ACCOUNT)
    args = parser.parse_args(argv)

    validate_target(args.project, args.dataset, args.location, args.service_account)
    statements = build_statements(args.project, args.dataset)
    table_ref = f"{args.project}.{args.dataset}.{TABLE}"
    print(f"Target: {table_ref}")
    print(f"Location: {args.location}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    for statement in statements:
        print(statement)
    if not args.apply:
        return 0

    if client_factory is None:
        from google.cloud import bigquery

        client_factory = bigquery.Client
    client = client_factory(project=args.project, location=args.location)
    _verify_client(client, args.service_account)
    for statement in statements:
        client.query(statement, location=args.location).result()
    verify_columns(client.get_table(table_ref))
    print("Schema verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
