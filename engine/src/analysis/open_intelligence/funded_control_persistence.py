"""Insert-only persistence for funded control receipts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields

from google.cloud import bigquery

from src.analysis.open_intelligence.funded_control import (
    FUNDED_CONTROL_TABLE,
    FundedControlReceipt,
)
from src.analysis.open_intelligence.funded_lane import wave1_pilot_service_identity
from src.analysis.open_intelligence.persistence import validate_real_client

TARGET_PROJECT = "ogilvy-trends-v2"
TARGET_DATASET = "trends_v2_staging_funded"
TARGET_LOCATION = "US"
_FIELDS = tuple(field.name for field in fields(FundedControlReceipt))
_TIMESTAMPS = frozenset({"recorded_at", "balance_observed_at", "created_at"})
_DATES = frozenset({"month_start"})
_INTEGERS = frozenset({"activation_stage", "consecutive_complete_runs", "runs_today"})
_NUMERICS = frozenset(
    {
        "current_balance",
        "opening_balance",
        "month_opening_balance",
        "monthly_cap",
        "reserve_floor",
        "stage_cap",
        "monthly_ledger_debit",
        "monthly_balance_delta",
        "monthly_effective_spend",
        "monthly_remaining",
        "reserve_remaining",
        "run_allowance",
    }
)


class FundedControlPersistenceError(RuntimeError):
    pass


class FundedControlConflict(FundedControlPersistenceError):
    pass


@dataclass(frozen=True, slots=True)
class FundedControlPersistenceResult:
    control_id: str
    inserted_count: int
    unchanged_count: int


def _target(project: object, dataset: object) -> tuple[str, str]:
    if project != TARGET_PROJECT or dataset != TARGET_DATASET:
        raise ValueError("funded control writer requires the exact staging target")
    return TARGET_PROJECT, TARGET_DATASET


def _client(client: object) -> object:
    try:
        writer = validate_real_client(
            client,
            TARGET_PROJECT,
            writer_identity=wave1_pilot_service_identity(),
        )
    except Exception as error:
        raise ValueError("funded control writer requires the exact staging writer") from error
    # The dataset is validated by _target; the real BigQuery client exposes
    # dataset() as a method, so it is never read off the client (4 Sep 2026).
    return writer


def _parameter(field: str, value: object) -> bigquery.ScalarQueryParameter:
    if field in _TIMESTAMPS:
        kind = "TIMESTAMP"
    elif field in _DATES:
        kind = "DATE"
    elif field in _INTEGERS:
        kind = "INT64"
    elif field in _NUMERICS:
        kind = "NUMERIC"
    else:
        kind = "STRING"
    return bigquery.ScalarQueryParameter(field, kind, value)


def _job_config(receipt: FundedControlReceipt, *, dry_run: bool) -> bigquery.QueryJobConfig:
    return bigquery.QueryJobConfig(
        query_parameters=[_parameter(field, getattr(receipt, field)) for field in _FIELDS],
        use_legacy_sql=False,
        dry_run=dry_run,
        use_query_cache=False if dry_run else None,
    )


def _transaction_sql() -> str:
    target = f"`{TARGET_PROJECT}.{TARGET_DATASET}.{FUNDED_CONTROL_TABLE}`"
    exact = " AND\n      ".join(
        f"target.`{field}` IS NOT DISTINCT FROM @{field}" for field in _FIELDS
    )
    columns = ", ".join(f"`{field}`" for field in _FIELDS)
    values = ", ".join(f"@{field}" for field in _FIELDS)
    return (
        "DECLARE inserted_count INT64 DEFAULT 0;\n"
        "DECLARE unchanged_count INT64 DEFAULT 0;\n"
        "DECLARE conflict_count INT64 DEFAULT 0;\n"
        "BEGIN TRANSACTION;\n"
        "SET conflict_count = (\n"
        f"  SELECT COUNT(*) FROM {target} AS target\n"
        "  WHERE target.control_id = @control_id\n"
        f"    AND NOT ({exact})\n"
        ");\n"
        "SET unchanged_count = (\n"
        f"  SELECT COUNT(*) FROM {target} AS target\n"
        "  WHERE target.control_id = @control_id\n"
        f"    AND ({exact})\n"
        ");\n"
        "IF conflict_count = 0 AND unchanged_count = 0 THEN\n"
        f"  INSERT INTO {target} ({columns}) VALUES ({values});\n"
        "  SET inserted_count = @@row_count;\n"
        "END IF;\n"
        "COMMIT TRANSACTION;\n"
        "SELECT inserted_count AS inserted_count, unchanged_count AS unchanged_count, "
        "conflict_count AS conflict_count;"
    )


def _readback_sql() -> str:
    target = f"`{TARGET_PROJECT}.{TARGET_DATASET}.{FUNDED_CONTROL_TABLE}`"
    columns = ", ".join(f"`{field}`" for field in _FIELDS)
    return f"SELECT {columns} FROM {target} WHERE control_id = @control_id LIMIT 2"


def _rows(job: object, *, max_results: int) -> tuple[object, ...]:
    if getattr(job, "errors", None):
        raise FundedControlPersistenceError("funded control query returned errors")
    return tuple(job.result(max_results=max_results, retry=None, job_retry=None))


def _count(row: object, field: str) -> int:
    try:
        value = row[field]
    except (KeyError, TypeError) as error:
        raise FundedControlPersistenceError(
            "funded control transaction result is invalid"
        ) from error
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FundedControlPersistenceError("funded control transaction result is invalid")
    return value


def _mapping(row: object) -> Mapping[str, object]:
    if isinstance(row, Mapping):
        return row
    items = getattr(row, "items", None)
    if callable(items):
        return dict(items())
    raise FundedControlPersistenceError("funded control readback row is invalid")


def _read_exact_receipt(writer: object, receipt: FundedControlReceipt) -> None:
    rows = _rows(
        writer.query(
            _readback_sql(),
            location=TARGET_LOCATION,
            job_config=_job_config(receipt, dry_run=False),
            retry=None,
            job_retry=None,
        ),
        max_results=2,
    )
    if len(rows) != 1:
        raise FundedControlPersistenceError("funded control readback returned invalid cardinality")
    try:
        actual = FundedControlReceipt(**{field: _mapping(rows[0])[field] for field in _FIELDS})
    except (KeyError, TypeError, ValueError) as error:
        raise FundedControlPersistenceError("funded control readback row is invalid") from error
    if actual != receipt:
        raise FundedControlConflict("funded control ID conflicts with existing content")


def persist_funded_control_receipt(
    *,
    project: str,
    dataset: str,
    client: object,
    receipt: FundedControlReceipt,
) -> FundedControlPersistenceResult:
    _target(project, dataset)
    if not isinstance(receipt, FundedControlReceipt):
        raise ValueError("funded control receipt is invalid")
    writer = _client(client)
    transaction = _transaction_sql()
    dry_job = writer.query(
        transaction,
        location=TARGET_LOCATION,
        job_config=_job_config(receipt, dry_run=True),
        retry=None,
        job_retry=None,
    )
    if getattr(dry_job, "errors", None):
        raise FundedControlPersistenceError("funded control dry run returned errors")
    dry_job.result()

    try:
        transaction_rows = _rows(
            writer.query(
                transaction,
                location=TARGET_LOCATION,
                job_config=_job_config(receipt, dry_run=False),
                retry=None,
                job_retry=None,
            ),
            max_results=2,
        )
    except Exception as transaction_error:
        try:
            _read_exact_receipt(writer, receipt)
        except FundedControlPersistenceError as readback_error:
            raise readback_error from transaction_error
        return FundedControlPersistenceResult(
            control_id=receipt.control_id,
            inserted_count=0,
            unchanged_count=1,
        )
    if len(transaction_rows) != 1:
        raise FundedControlPersistenceError(
            "funded control transaction returned invalid cardinality"
        )
    inserted = _count(transaction_rows[0], "inserted_count")
    unchanged = _count(transaction_rows[0], "unchanged_count")
    conflict = _count(transaction_rows[0], "conflict_count")
    if conflict:
        raise FundedControlConflict("funded control ID conflicts with existing content")
    if inserted + unchanged != 1:
        raise FundedControlPersistenceError("funded control transaction did not affect one row")

    _read_exact_receipt(writer, receipt)
    return FundedControlPersistenceResult(
        control_id=receipt.control_id,
        inserted_count=inserted,
        unchanged_count=unchanged,
    )
