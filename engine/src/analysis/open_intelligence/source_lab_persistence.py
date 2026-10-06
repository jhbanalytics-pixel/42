"""Source Lab inventory persistence for the approved staging boundary."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

from google.cloud import bigquery

from src.analysis.open_intelligence.persistence import (
    TARGET_DATASET,
    TARGET_LOCATION,
    TARGET_PROJECT,
    TARGET_WRITER_IDENTITY,
    BatchInvalid,
    CleanupFailure,
    ImmutableConflict,
    PersistenceError,
    _date,
    _failure_for,
    _float,
    _load_job_config,
    _query_job_config,
    _row_counts,
    _temporary_identifier,
    _text,
    _timestamp,
    _validate_target,
    _validated_target_descriptor,
    validate_real_client,
)
from src.analysis.open_intelligence.persistence import (
    TargetInvalid as TargetInvalid,
)
from src.analysis.open_intelligence.source_lab import (
    _WAVE1_CHANNEL_BY_ROUTE,
    CATALOG_EXPECTATIONS,
    CATALOG_NORMALIZED_ROUTE_COUNTS,
    SOURCE_PERFORMANCE_FIELDS,
    _source_performance_id,
    inventory_metadata_digest,
)


class SourceLabBatchInvalid(PersistenceError):
    """The Source Lab batch does not match the approved inventory contract."""


SOURCE_LAB_TABLE = "source_performance_daily_v2"
SOURCE_LAB_NATURAL_KEY = ("source_performance_id",)
# The statuses a writer can produce today: the catalog row, the measured route
# and the measured kill verdict. The contract tuple may name more; it may not
# name fewer.
WRITABLE_STATUSES = frozenset({"inventory_only", "active", "permanently_rejected"})
_SOURCE_LAB_REPEATED = frozenset(
    {
        "market_scope",
        "audience_lens_ids",
        "downstream_consumers",
        "official_parameters",
        "official_price_components",
    }
)
_SOURCE_LAB_NUMERIC = frozenset(
    {
        "official_credits",
        "credits",
        "balance",
        "recent_deductions",
        "funded_increase_amount",
        "selected_top_up_credits",
        "baseline_credits_per_day",
        "runway_days",
        "monthly_optional_credit_cap",
        "monthly_optional_credits_used",
    }
)
_SOURCE_LAB_FLOAT = frozenset({"integrity", "geo_precision", "unique_lift"})
_SOURCE_LAB_INTEGER = frozenset({"calls", "rows", "cache_ttl_seconds"})
_SOURCE_LAB_BOOLEAN = frozenset(
    {
        "catalog_paginated",
        "catalog_metered",
        "funded_increase_observed",
        "optional_calls_enabled",
    }
)
_SOURCE_LAB_TIMESTAMPS = frozenset({"last_success_at", "last_checked_at", "observed_at"})
_SOURCE_LAB_DATES = frozenset({"metric_date", "review_date"})
_SOURCE_LAB_STRUCTS = frozenset({"deductions_interval", "baseline_window"})
_SOURCE_LAB_NULLABLE = frozenset(
    {
        "market",
        "funded_lane",
        "platform",
        "resource",
        "catalog_digest",
        "official_credits",
        "official_credits_label",
        "official_archetype",
        "catalog_paginated",
        "catalog_metered",
        "cache_ttl_seconds",
        "delivery",
        "docs_url",
        "calls",
        "credits",
        "rows",
        "integrity",
        "geo_precision",
        "unique_lift",
        "last_success_at",
        "blocking_reason",
        "kill_test_result",
        "review_date",
        "balance",
        "observed_at",
        "balance_read_status",
        "recent_deductions",
        "deductions_interval",
        "funding_math_status",
        "funded_increase_amount",
        "funded_increase_observed",
        "selected_top_up_credits",
        "baseline_credits_per_day",
        "baseline_window",
        "runway_days",
        "monthly_optional_credit_cap",
        "monthly_optional_credits_used",
        "optional_calls_enabled",
    }
)
_SOURCE_LAB_UNMEASURED = frozenset(
    {
        "calls",
        "credits",
        "rows",
        "integrity",
        "geo_precision",
        "unique_lift",
        "last_success_at",
    }
)
_SOURCE_LAB_BALANCE_ONLY = frozenset(
    {
        "balance",
        "observed_at",
        "balance_read_status",
        "recent_deductions",
        "deductions_interval",
        "funding_math_status",
        "funded_increase_amount",
        "funded_increase_observed",
        "selected_top_up_credits",
        "baseline_credits_per_day",
        "baseline_window",
        "runway_days",
        "monthly_optional_credit_cap",
        "monthly_optional_credits_used",
        "optional_calls_enabled",
    }
)
_SOURCE_LAB_SNAPSHOT_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
    "metric_date",
)


def _source_lab_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise SourceLabBatchInvalid(f"{field} must be a nonnegative Decimal")
    return Decimal("0") if value == 0 else value.normalize()


def _source_lab_value(field: str, value: object) -> object:
    if value is None:
        if field not in _SOURCE_LAB_NULLABLE:
            raise SourceLabBatchInvalid(f"{field} cannot be null")
        return None
    if field in _SOURCE_LAB_REPEATED:
        if not isinstance(value, (tuple, list)):
            raise SourceLabBatchInvalid(f"{field} must be a tuple or list")
        if field == "official_price_components" and value:
            raise SourceLabBatchInvalid("inventory price components must remain empty")
        values = tuple(value)
        if any(not isinstance(item, str) or not item for item in values):
            raise SourceLabBatchInvalid(f"{field} values must be nonempty strings")
        if len(set(values)) != len(values):
            raise SourceLabBatchInvalid(f"{field} contains duplicates")
        return values
    if field in _SOURCE_LAB_NUMERIC:
        return _source_lab_decimal(value, field)
    if field in _SOURCE_LAB_FLOAT:
        return _float(value, field)
    if field in _SOURCE_LAB_INTEGER:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SourceLabBatchInvalid(f"{field} must be a nonnegative integer")
        return value
    if field in _SOURCE_LAB_BOOLEAN:
        if type(value) is not bool:
            raise SourceLabBatchInvalid(f"{field} must be boolean")
        return value
    if field in _SOURCE_LAB_TIMESTAMPS:
        return _timestamp(value, field)
    if field in _SOURCE_LAB_DATES:
        return _date(value, field)
    if field in _SOURCE_LAB_STRUCTS:
        if not isinstance(value, Mapping):
            raise SourceLabBatchInvalid(f"{field} must be an object")
        return MappingProxyType(dict(value))
    try:
        return _text(value, field)
    except BatchInvalid as error:
        raise SourceLabBatchInvalid(str(error)) from error


def _canonical_source_lab_row(row: object) -> Mapping[str, object]:
    if not isinstance(row, Mapping) or set(row) != set(SOURCE_PERFORMANCE_FIELDS):
        raise SourceLabBatchInvalid("Source Lab row fields are invalid")
    canonical = MappingProxyType(
        {field: _source_lab_value(field, row[field]) for field in SOURCE_PERFORMANCE_FIELDS}
    )
    expected_id = _source_performance_id(
        canonical["client_scope_id"],
        canonical["metric_date"],
        canonical["vendor"],
        canonical["http_method"],
        canonical["route_path"],
        canonical["market"],
    )
    if canonical["source_performance_id"] != expected_id:
        raise SourceLabBatchInvalid("source_performance_id is invalid")
    if (
        canonical["vendor"] != "socialcrawl"
        or canonical["vendor_family"] != "socialcrawl"
        or canonical["source_family"] != canonical["channel_family"]
    ):
        raise SourceLabBatchInvalid("Source Lab vendor identity is invalid")
    if canonical["market"] is not None:
        raise SourceLabBatchInvalid("Source Lab rows must remain global")
    status = canonical["status"]
    if status not in WRITABLE_STATUSES:
        raise SourceLabBatchInvalid("Source Lab status is invalid")
    measured = status != "inventory_only"
    wave1_channel = _WAVE1_CHANNEL_BY_ROUTE.get(canonical["route_path"])
    if wave1_channel is not None:
        if (
            canonical["funded_lane"] != "stage_1_wave_1"
            or canonical["vendor_family"] != "socialcrawl"
            or canonical["channel_family"] != wave1_channel
        ):
            raise SourceLabBatchInvalid("Wave 1 Source Lab identity is invalid")
    elif canonical["funded_lane"] is not None:
        raise SourceLabBatchInvalid("non-Wave 1 route cannot carry the Wave 1 funded lane")
    if measured:
        if wave1_channel is None:
            raise SourceLabBatchInvalid("only Wave 1 routes can carry measured source value")
        if canonical["rows"] is None or canonical["unique_lift"] is None:
            raise SourceLabBatchInvalid("measured source value is incomplete")
        if any(
            canonical[field] is not None
            for field in _SOURCE_LAB_UNMEASURED - {"rows", "unique_lift"}
        ):
            raise SourceLabBatchInvalid("unsupported measured Source Lab values must remain null")
        if status == "active":
            if (
                canonical["rows"] <= 0
                or canonical["unique_lift"] <= 0
                or canonical["blocking_reason"] is not None
                or canonical["kill_test_result"] != "passed"
                or not canonical["downstream_consumers"]
            ):
                raise SourceLabBatchInvalid("active Wave 1 source value is invalid")
        elif (
            (canonical["rows"] > 0 and canonical["unique_lift"] > 0)
            or canonical["blocking_reason"]
            not in {"zero_unique_observations", "zero_marginal_downstream_rows"}
            or canonical["kill_test_result"] != canonical["blocking_reason"]
            or canonical["downstream_consumers"]
        ):
            raise SourceLabBatchInvalid("permanent Wave 1 rejection is invalid")
    else:
        if any(canonical[field] is not None for field in _SOURCE_LAB_UNMEASURED):
            raise SourceLabBatchInvalid("unmeasured Source Lab values must remain null")
        if canonical["downstream_consumers"]:
            raise SourceLabBatchInvalid("unmeasured Source Lab collections must remain empty")
    if canonical["official_price_components"]:
        raise SourceLabBatchInvalid("inventory price components must remain empty")
    if canonical["route_role"] == "inventory":
        required = (
            "platform",
            "resource",
            "catalog_digest",
            "official_credits",
            "official_credits_label",
            "official_archetype",
            "catalog_paginated",
            "catalog_metered",
            "cache_ttl_seconds",
            "docs_url",
        )
        if any(canonical[field] is None for field in required):
            raise SourceLabBatchInvalid("inventory catalog facts are incomplete")
        if any(canonical[field] is not None for field in _SOURCE_LAB_BALANCE_ONLY):
            raise SourceLabBatchInvalid("inventory rows cannot carry funding facts")
    elif canonical["route_role"] == "utility_balance":
        if canonical["route_path"] != "/v1/credits/balance":
            raise SourceLabBatchInvalid("utility balance route is invalid")
        if any(
            canonical[field] is not None
            for field in (
                "platform",
                "resource",
                "catalog_digest",
                "official_credits",
                "official_credits_label",
                "official_archetype",
                "catalog_paginated",
                "catalog_metered",
                "cache_ttl_seconds",
                "delivery",
                "docs_url",
            )
        ):
            raise SourceLabBatchInvalid("balance row cannot become catalog inventory")
        if (
            canonical["balance"] is None
            or canonical["observed_at"] is None
            or canonical["recent_deductions"] is None
            or canonical["balance_read_status"] != "ok"
            or canonical["funding_math_status"] != "unknown"
            or canonical["funded_increase_observed"] is not False
            or canonical["monthly_optional_credit_cap"] != Decimal("25000")
            or canonical["monthly_optional_credits_used"] is not None
            or canonical["optional_calls_enabled"] is not False
        ):
            raise SourceLabBatchInvalid("balance funding controls must fail closed")
    else:
        raise SourceLabBatchInvalid("Source Lab route role is unsupported")
    return canonical


def _source_lab_typed_value(value: object) -> Mapping[str, object]:
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, datetime):
        return {"type": "timestamp", "value": value.isoformat().replace("+00:00", "Z")}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"type": "numeric", "value": format(value, "f")}
    if type(value) is bool:
        return {"type": "boolean", "value": value}
    if isinstance(value, int):
        return {"type": "integer", "value": value}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, str):
        return {"type": "string", "value": value}
    if isinstance(value, tuple):
        return {"type": "repeated", "value": [_source_lab_typed_value(item) for item in value]}
    if isinstance(value, Mapping):
        return {
            "type": "struct",
            "value": {field: _source_lab_typed_value(item) for field, item in value.items()},
        }
    raise SourceLabBatchInvalid("canonical Source Lab value is unsupported")


def canonical_source_lab_json(row: object) -> str:
    canonical = _canonical_source_lab_row(row)
    return json.dumps(
        {field: _source_lab_typed_value(canonical[field]) for field in SOURCE_PERFORMANCE_FIELDS},
        separators=(",", ":"),
        ensure_ascii=False,
    )


@dataclass(frozen=True, slots=True)
class SourceLabRowBatch:
    rows: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.rows, (tuple, list)):
            raise SourceLabBatchInvalid("Source Lab rows must be a tuple or list")
        canonical = tuple(_canonical_source_lab_row(row) for row in self.rows)
        if not canonical:
            raise SourceLabBatchInvalid("Source Lab batch must not be empty")
        keys = tuple(row["source_performance_id"] for row in canonical)
        if len(set(keys)) != len(keys):
            raise SourceLabBatchInvalid("Source Lab rows contain a duplicate natural key")
        if sum(row["route_role"] == "utility_balance" for row in canonical) != 1:
            raise SourceLabBatchInvalid("Source Lab batch must contain one balance row")
        snapshots = {
            tuple(row[field] for field in _SOURCE_LAB_SNAPSHOT_FIELDS) for row in canonical
        }
        if len(snapshots) != 1:
            raise SourceLabBatchInvalid("Source Lab batch mixes snapshot identity")
        inventory_rows = tuple(row for row in canonical if row["route_role"] == "inventory")
        catalog_digests = {row["catalog_digest"] for row in inventory_rows}
        if len(catalog_digests) != 1:
            raise SourceLabBatchInvalid("Source Lab batch mixes snapshot catalog digest")
        catalog_digest = next(iter(catalog_digests))
        approved_route_counts = {
            CATALOG_NORMALIZED_ROUTE_COUNTS.get(version, expectation[1])
            for version, expectation in CATALOG_EXPECTATIONS.items()
            if expectation[3] == catalog_digest
        }
        if not approved_route_counts:
            raise SourceLabBatchInvalid("Source Lab batch uses an unapproved catalog digest")
        # The batch is exactly the approved catalog's routes plus the single balance row.
        if len(inventory_rows) not in approved_route_counts:
            raise SourceLabBatchInvalid(
                "Source Lab batch must contain the approved catalog's inventory rows"
            )
        if len(canonical) != len(inventory_rows) + 1:
            raise SourceLabBatchInvalid(
                "Source Lab batch must contain only inventory rows and one balance row"
            )
        metadata_digest = inventory_metadata_digest(inventory_rows)
        approved_pair = any(
            expectation[3] == catalog_digest and expectation[4] == metadata_digest
            for expectation in CATALOG_EXPECTATIONS.values()
        )
        if approved_pair:
            object.__setattr__(
                self, "rows", tuple(sorted(canonical, key=lambda row: row["source_performance_id"]))
            )
            return
        if not any(
            expectation[3] == catalog_digest for expectation in CATALOG_EXPECTATIONS.values()
        ):
            raise SourceLabBatchInvalid("Source Lab batch uses an unapproved catalog digest")
        raise SourceLabBatchInvalid("Source Lab inventory metadata digest is invalid")


@dataclass(frozen=True, slots=True)
class SourceLabPersistenceResult:
    project: str
    dataset: str
    dry_run: bool
    validated_count: int
    inserted_count: int
    unchanged_count: int
    conflict_count: int
    statement_digest: str
    cleanup_state: str


def _source_lab_statement_digest(batch: SourceLabRowBatch) -> str:
    payload = json.dumps(
        {
            "project": TARGET_PROJECT,
            "dataset": TARGET_DATASET,
            "table": SOURCE_LAB_TABLE,
            "natural_key": SOURCE_LAB_NATURAL_KEY,
            "rows": [json.loads(canonical_source_lab_json(row)) for row in batch.rows],
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_lab_schema() -> list[bigquery.SchemaField]:
    nested = {
        "official_price_components": (
            bigquery.SchemaField("meter", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("billing_basis", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("currency", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("unit", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("quantity", "NUMERIC", mode="REQUIRED"),
            bigquery.SchemaField("source_url", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("checked_at", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("amount", "NUMERIC"),
            bigquery.SchemaField("minimum_amount", "NUMERIC"),
            bigquery.SchemaField("maximum_amount", "NUMERIC"),
            bigquery.SchemaField("tier_condition", "STRING"),
        ),
        "deductions_interval": (
            bigquery.SchemaField("start_at", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("end_at", "TIMESTAMP", mode="REQUIRED"),
        ),
        "baseline_window": (
            bigquery.SchemaField("start_date", "DATE", mode="REQUIRED"),
            bigquery.SchemaField("end_date", "DATE", mode="REQUIRED"),
            bigquery.SchemaField("completed_days", "INT64", mode="REQUIRED"),
        ),
    }
    fields = []
    for field in SOURCE_PERFORMANCE_FIELDS:
        mode = (
            "REPEATED"
            if field in _SOURCE_LAB_REPEATED
            else ("NULLABLE" if field in _SOURCE_LAB_NULLABLE else "REQUIRED")
        )
        if field in nested:
            field_type = "RECORD"
        elif field in _SOURCE_LAB_NUMERIC:
            field_type = "NUMERIC"
        elif field in _SOURCE_LAB_FLOAT:
            field_type = "FLOAT64"
        elif field in _SOURCE_LAB_INTEGER:
            field_type = "INT64"
        elif field in _SOURCE_LAB_BOOLEAN:
            field_type = "BOOL"
        elif field in _SOURCE_LAB_TIMESTAMPS:
            field_type = "TIMESTAMP"
        elif field in _SOURCE_LAB_DATES:
            field_type = "DATE"
        else:
            field_type = "STRING"
        fields.append(
            bigquery.SchemaField(field, field_type, mode=mode, fields=nested.get(field, ()))
        )
    return fields


def _source_lab_json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, tuple):
        return [_source_lab_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {field: _source_lab_json_value(item) for field, item in value.items()}
    return value


def _source_lab_transaction_sql(temporary: bigquery.Table) -> str:
    target = f"`{TARGET_PROJECT}.{TARGET_DATASET}.{SOURCE_LAB_TABLE}`"
    staged = _temporary_identifier(temporary)
    match = "target.`source_performance_id` = staged.`source_performance_id`"
    target_json = (
        "TO_JSON_STRING(STRUCT("
        + ", ".join(f"target.`{field}` AS `{field}`" for field in SOURCE_PERFORMANCE_FIELDS)
        + "))"
    )
    staged_json = (
        "TO_JSON_STRING(STRUCT("
        + ", ".join(f"staged.`{field}` AS `{field}`" for field in SOURCE_PERFORMANCE_FIELDS)
        + "))"
    )
    fields = ", ".join(f"`{field}`" for field in SOURCE_PERFORMANCE_FIELDS)
    selected = ", ".join(f"staged.`{field}`" for field in SOURCE_PERFORMANCE_FIELDS)
    return (
        "DECLARE unchanged_count INT64 DEFAULT 0;\n"
        "DECLARE inserted_count INT64 DEFAULT 0;\n"
        "DECLARE conflict_count INT64 DEFAULT 0;\n"
        "DECLARE status INT64 DEFAULT 0;\n"
        "BEGIN TRANSACTION;\n"
        f"SET conflict_count = (SELECT COUNT(*) FROM {staged} AS staged JOIN {target} AS target ON {match} WHERE {staged_json} != {target_json});\n"
        "BEGIN\n  ASSERT conflict_count = 0 AS 'immutable_conflict';\n"
        "EXCEPTION WHEN ERROR THEN\n  ROLLBACK TRANSACTION;\n  SET status = 1;\nEND;\n"
        "IF status = 0 THEN\n"
        f"SET unchanged_count = (SELECT COUNT(*) FROM {staged} AS staged JOIN {target} AS target ON {match} WHERE {staged_json} = {target_json});\n"
        f"INSERT INTO {target} ({fields})\nSELECT {selected} FROM {staged} AS staged\n"
        f"WHERE NOT EXISTS (SELECT 1 FROM {target} AS target WHERE {match});\n"
        "SET inserted_count = @@row_count;\nCOMMIT TRANSACTION;\nEND IF;\n"
        "SELECT inserted_count AS inserted_count, unchanged_count AS unchanged_count, conflict_count AS conflict_count, status AS status;"
    )


def _validate_source_lab_transaction_sql(sql: str) -> None:
    required = (
        ("BEGIN TRANSACTION;", 1),
        ("ASSERT conflict_count = 0 AS 'immutable_conflict';", 1),
        ("ROLLBACK TRANSACTION;", 1),
        ("COMMIT TRANSACTION;", 1),
        ("SET unchanged_count = (", 1),
        ("target.`source_performance_id` = staged.`source_performance_id`", 3),
        (f"INSERT INTO `{TARGET_PROJECT}.{TARGET_DATASET}.{SOURCE_LAB_TABLE}`", 1),
        ("WHERE NOT EXISTS (", 1),
    )
    if any(sql.count(fragment) != count for fragment, count in required):
        raise PersistenceError("rendered Source Lab transaction SQL is invalid")
    if any(token in sql for token in ("MERGE ", "UPDATE ", "DELETE FROM")):
        raise PersistenceError("rendered Source Lab transaction SQL is not insert only")
    target_fields = tuple(re.findall(r"target\.``?([^`]+)``? AS `([^`]+)`", sql))
    staged_fields = tuple(re.findall(r"staged\.``?([^`]+)``? AS `([^`]+)`", sql))
    expected = tuple((field, field) for field in SOURCE_PERFORMANCE_FIELDS)
    if target_fields[: len(expected)] != expected or staged_fields[: len(expected)] != expected:
        raise PersistenceError("rendered Source Lab transaction SQL field projection is invalid")


def _source_lab_result(
    batch: SourceLabRowBatch,
    *,
    dry_run: bool,
    digest: str,
    inserted: int = 0,
    unchanged: int = 0,
    conflicts: int = 0,
    cleanup_state: str,
) -> SourceLabPersistenceResult:
    return SourceLabPersistenceResult(
        project=TARGET_PROJECT,
        dataset=TARGET_DATASET,
        dry_run=dry_run,
        validated_count=len(batch.rows),
        inserted_count=inserted,
        unchanged_count=unchanged,
        conflict_count=conflicts,
        statement_digest=digest,
        cleanup_state=cleanup_state,
    )


def _validate_source_lab_transaction_receipt(
    batch: SourceLabRowBatch,
    *,
    inserted: int,
    unchanged: int,
    conflicts: int,
    status: int,
) -> None:
    if status == 0:
        if conflicts != 0 or inserted + unchanged != len(batch.rows):
            raise PersistenceError("Source Lab transaction receipt is incomplete")
        return
    if status == 1:
        if inserted != 0 or unchanged != 0 or conflicts <= 0:
            raise PersistenceError("Source Lab conflict receipt is invalid")
        raise ImmutableConflict("Source Lab content conflicts with an existing row")
    raise PersistenceError("Source Lab transaction receipt status is invalid")


def persist_source_lab_rows(
    *,
    project: str,
    dataset: str,
    client: object | None,
    batch: SourceLabRowBatch,
    dry_run: bool,
    writer_identity: str = TARGET_WRITER_IDENTITY,
) -> SourceLabPersistenceResult:
    """Persist one deterministic Source Lab snapshot through the staging writer."""

    if type(dry_run) is not bool or not isinstance(batch, SourceLabRowBatch):
        raise SourceLabBatchInvalid("Source Lab persistence inputs are invalid")
    project, dataset = _validate_target(project, dataset)
    digest = _source_lab_statement_digest(batch)
    if dry_run:
        _validated_target_descriptor(
            client,
            project,
            dataset,
            writer_identity=writer_identity,
        )
        return _source_lab_result(batch, dry_run=True, digest=digest, cleanup_state="not_started")

    writer = validate_real_client(client, project, writer_identity=writer_identity)
    temporary = bigquery.Table(
        f"{project}.{dataset}._sl_{digest[:12]}_{uuid.uuid4().hex}_{SOURCE_LAB_TABLE}",
        schema=_source_lab_schema(),
    )
    temporary.expires = datetime.now(UTC) + timedelta(hours=1)
    temporary.labels = {"source_lab_inventory": "v1"}
    primary_error: PersistenceError | None = None
    cleanup_exception: Exception | None = None
    inserted = unchanged = conflicts = 0
    created = False
    try:
        created = True
        writer.create_table(temporary, exists_ok=False)
        loaded_rows = [
            {field: _source_lab_json_value(row[field]) for field in SOURCE_PERFORMANCE_FIELDS}
            for row in batch.rows
        ]
        load_job = writer.load_table_from_json(
            loaded_rows,
            temporary,
            location=TARGET_LOCATION,
            job_config=_load_job_config(),
        )
        load_job.result()
        if getattr(load_job, "errors", None) or getattr(load_job, "output_rows", None) != len(
            batch.rows
        ):
            raise PersistenceError("Source Lab temporary load was incomplete")
        transaction_sql = _source_lab_transaction_sql(temporary)
        _validate_source_lab_transaction_sql(transaction_sql)
        transaction = writer.query(
            transaction_sql,
            location=TARGET_LOCATION,
            job_config=_query_job_config(),
            retry=None,
            job_retry=None,
        )
        inserted, unchanged, conflicts, status = _row_counts(
            transaction, "inserted_count", "unchanged_count", "conflict_count", "status"
        )
        _validate_source_lab_transaction_receipt(
            batch,
            inserted=inserted,
            unchanged=unchanged,
            conflicts=conflicts,
            status=status,
        )
    except Exception as error:
        primary_error = (
            error if isinstance(error, PersistenceError) else _failure_for("source_lab", error)
        )
    finally:
        if created:
            try:
                writer.delete_table(temporary, not_found_ok=True)
            except Exception as error:
                cleanup_exception = error

    cleanup_state = "failed" if cleanup_exception else "complete"
    result = _source_lab_result(
        batch,
        dry_run=False,
        digest=digest,
        inserted=inserted,
        unchanged=unchanged,
        conflicts=conflicts,
        cleanup_state=cleanup_state,
    )
    if primary_error is not None:
        primary_error.result = result
        if cleanup_exception is not None:
            primary_error.cleanup_errors = (
                CleanupFailure("temporary table cleanup failed", result=result),
            )
        raise primary_error
    if cleanup_exception is not None:
        raise CleanupFailure("temporary table cleanup failed", result=result) from cleanup_exception
    return result
