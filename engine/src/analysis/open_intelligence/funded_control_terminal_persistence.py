"""Insert-only persistence for funded terminal events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields

from google.cloud import bigquery

from src.analysis.open_intelligence.funded_control_terminal import (
    FUNDED_TERMINAL_TABLE,
    FundedTerminalEvent,
)
from src.analysis.open_intelligence.funded_lane import wave1_pilot_service_identity
from src.analysis.open_intelligence.persistence import validate_real_client

TARGET_PROJECT = "ogilvy-trends-v2"
TARGET_DATASET = "trends_v2_staging_funded"
TARGET_LOCATION = "US"
_FIELDS = tuple(field.name for field in fields(FundedTerminalEvent))
_TIMESTAMPS = frozenset({"recorded_at", "last_balance_observed_at", "created_at"})
_NUMERICS = frozenset(
    {
        "last_measured_balance",
        "authorized_quoted_debit",
        "vendor_reported_debit",
        "overage_debit",
        "ledger_debit",
    }
)


class FundedTerminalPersistenceError(RuntimeError):
    pass


class FundedTerminalConflict(FundedTerminalPersistenceError):
    pass


@dataclass(frozen=True, slots=True)
class FundedTerminalPersistenceResult:
    terminal_id: str
    inserted_count: int
    unchanged_count: int


def _client(client: object) -> object:
    try:
        writer = validate_real_client(
            client,
            TARGET_PROJECT,
            writer_identity=wave1_pilot_service_identity(),
        )
    except Exception as error:
        raise ValueError("funded terminal writer requires the exact staging writer") from error
    # The dataset is validated by _target; the real BigQuery client exposes
    # dataset() as a method, so it is never read off the client (4 Sep 2026).
    return writer


def _parameter(field: str, value: object):
    if field == "reason_codes":
        return bigquery.ArrayQueryParameter(field, "STRING", value)
    if field in _TIMESTAMPS:
        kind = "TIMESTAMP"
    elif field in _NUMERICS:
        kind = "NUMERIC"
    else:
        kind = "STRING"
    return bigquery.ScalarQueryParameter(field, kind, value)


def _job_config(event: FundedTerminalEvent, *, dry_run: bool) -> bigquery.QueryJobConfig:
    return bigquery.QueryJobConfig(
        query_parameters=[_parameter(field, getattr(event, field)) for field in _FIELDS],
        use_legacy_sql=False,
        dry_run=dry_run,
        use_query_cache=False if dry_run else None,
    )


def _transaction_sql() -> str:
    target = f"`{TARGET_PROJECT}.{TARGET_DATASET}.{FUNDED_TERMINAL_TABLE}`"
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
        "  WHERE target.terminal_id = @terminal_id\n"
        f"    AND NOT ({exact})\n"
        ");\n"
        "SET unchanged_count = (\n"
        f"  SELECT COUNT(*) FROM {target} AS target\n"
        "  WHERE target.terminal_id = @terminal_id\n"
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
    target = f"`{TARGET_PROJECT}.{TARGET_DATASET}.{FUNDED_TERMINAL_TABLE}`"
    columns = ", ".join(f"`{field}`" for field in _FIELDS)
    return f"SELECT {columns} FROM {target} WHERE terminal_id = @terminal_id LIMIT 2"


def _rows(job: object, *, maximum: int) -> tuple[object, ...]:
    if getattr(job, "errors", None):
        raise FundedTerminalPersistenceError("funded terminal query returned errors")
    return tuple(job.result(max_results=maximum, retry=None, job_retry=None))


def _mapping(row: object) -> Mapping[str, object]:
    if isinstance(row, Mapping):
        return row
    items = getattr(row, "items", None)
    if callable(items):
        return dict(items())
    raise FundedTerminalPersistenceError("funded terminal row is invalid")


def _count(row: object, field: str) -> int:
    value = _mapping(row).get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FundedTerminalPersistenceError("funded terminal count is invalid")
    return value


def _read_exact_event(writer: object, event: FundedTerminalEvent) -> None:
    rows = _rows(
        writer.query(
            _readback_sql(),
            location=TARGET_LOCATION,
            job_config=_job_config(event, dry_run=False),
            retry=None,
            job_retry=None,
        ),
        maximum=2,
    )
    if len(rows) != 1:
        raise FundedTerminalPersistenceError("funded terminal readback is incomplete")
    try:
        actual = FundedTerminalEvent(**{field: _mapping(rows[0])[field] for field in _FIELDS})
    except (KeyError, TypeError, ValueError) as error:
        raise FundedTerminalPersistenceError("funded terminal row is invalid") from error
    if actual != event:
        raise FundedTerminalConflict("funded terminal ID conflicts with stored content")


def persist_funded_terminal_event(
    *, project: str, dataset: str, client: object, event: FundedTerminalEvent
) -> FundedTerminalPersistenceResult:
    if project != TARGET_PROJECT or dataset != TARGET_DATASET:
        raise ValueError("funded terminal writer requires the exact staging target")
    if not isinstance(event, FundedTerminalEvent):
        raise ValueError("funded terminal event is invalid")
    writer = _client(client)
    transaction = _transaction_sql()
    dry_job = writer.query(
        transaction,
        location=TARGET_LOCATION,
        job_config=_job_config(event, dry_run=True),
        retry=None,
        job_retry=None,
    )
    if getattr(dry_job, "errors", None):
        raise FundedTerminalPersistenceError("funded terminal dry run returned errors")
    dry_job.result()
    try:
        result_rows = _rows(
            writer.query(
                transaction,
                location=TARGET_LOCATION,
                job_config=_job_config(event, dry_run=False),
                retry=None,
                job_retry=None,
            ),
            maximum=2,
        )
    except Exception as transaction_error:
        try:
            _read_exact_event(writer, event)
        except FundedTerminalPersistenceError as readback_error:
            raise readback_error from transaction_error
        return FundedTerminalPersistenceResult(event.terminal_id, 0, 1)
    if len(result_rows) != 1:
        raise FundedTerminalPersistenceError("funded terminal result cardinality is invalid")
    inserted = _count(result_rows[0], "inserted_count")
    unchanged = _count(result_rows[0], "unchanged_count")
    conflict = _count(result_rows[0], "conflict_count")
    if conflict:
        raise FundedTerminalConflict("funded terminal ID conflicts with stored content")
    if inserted + unchanged != 1:
        raise FundedTerminalPersistenceError("funded terminal result is incomplete")
    _read_exact_event(writer, event)
    return FundedTerminalPersistenceResult(event.terminal_id, inserted, unchanged)
