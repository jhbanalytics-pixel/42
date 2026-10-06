"""Immutable staging persistence for SocialCrawl credit ledger rows."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Protocol, cast

from google.api_core.retry import Retry
from google.cloud import bigquery

from src.analysis.open_intelligence.funded_lane import wave1_pilot_service_identity
from src.analysis.open_intelligence.persistence import (
    TARGET_LOCATION,
    TARGET_PROJECT,
    validate_real_client,
)
from src.analysis.open_intelligence.persistence import (
    TargetInvalid as OpenIntelligenceTargetInvalid,
)

TABLE_NAME = "socialcrawl_credit_ledger_v1"
TARGET_DATASET = "trends_v2_staging_funded"
ROW_FIELDS = (
    "ledger_id",
    "execution_id",
    "run_id",
    "trend_date",
    "recorded_at",
    "credential_lane",
    "market",
    "phase",
    "event_type",
    "calls",
    "budget_debit_credits",
    "vendor_reported_credits",
    "balance_observed",
    "opening_balance",
    "month_opening_balance",
    "month_start",
    "monthly_ledger_debit_before",
    "monthly_balance_delta_before",
    "monthly_effective_spend_before",
    "monthly_cap",
    "reserve_floor",
    "run_allowance",
    "catalog_digest",
    "metadata_digest",
    "attribution_state",
    "created_at",
)
NATURAL_KEY = (
    "execution_id",
    "credential_lane",
    "market",
    "phase",
    "event_type",
)
CREDENTIAL_LANES = frozenset({"jhb_core", "ogilvy_funded"})
PHASES = frozenset(
    {
        "preflight",
        "discover",
        "creators",
        "search",
        "reddit",
        "accounts",
        "news",
        "facebook",
        "run_close",
        "unattributed",
        "wave1_tiktok_sound",
        "wave1_instagram_reels",
        "wave1_youtube_shorts_comments",
        "wave1_reddit_comments",
    }
)
WAVE1_PHASES = frozenset(
    {
        "wave1_tiktok_sound",
        "wave1_instagram_reels",
        "wave1_youtube_shorts_comments",
        "wave1_reddit_comments",
    }
)
EVENT_TYPES = frozenset({"preflight", "phase_close", "run_close", "attribution_gap"})
ATTRIBUTION_STATES = frozenset({"complete", "conservative", "gap_detected"})

_DATE_FIELDS = frozenset({"trend_date", "month_start"})
_TIMESTAMP_FIELDS = frozenset({"recorded_at", "created_at"})
_NUMERIC_FIELDS = frozenset(
    {
        "budget_debit_credits",
        "vendor_reported_credits",
        "balance_observed",
        "opening_balance",
        "month_opening_balance",
        "monthly_ledger_debit_before",
        "monthly_balance_delta_before",
        "monthly_effective_spend_before",
        "monthly_cap",
        "reserve_floor",
        "run_allowance",
    }
)
_NULLABLE_FIELDS = frozenset({"market", "balance_observed"})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class PersistenceError(Exception):
    """Base failure for funded lane ledger persistence."""

    def __init__(
        self,
        message: str,
        *,
        result: PersistenceResult | None = None,
        cleanup_errors: tuple[CleanupFailure, ...] = (),
    ) -> None:
        super().__init__(message)
        self.result = result
        self.cleanup_errors = cleanup_errors


class TargetInvalid(PersistenceError):
    """The requested writer or table target is outside staging."""


class LedgerBatchInvalid(PersistenceError):
    """A ledger batch does not match the approved contract."""


class ImmutableConflict(PersistenceError):
    """Existing natural key content differs from the staged row."""


class CleanupFailure(PersistenceError):
    """A temporary BigQuery table could not be removed."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise LedgerBatchInvalid(f"{field} must be a nonempty trimmed string")
    return value


def _date(value: object, field: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise LedgerBatchInvalid(f"{field} must be a date")
    return value


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LedgerBatchInvalid(f"{field} must be a timezone aware timestamp")
    return value.astimezone(UTC)


def _numeric(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise LedgerBatchInvalid(f"{field} must be a nonnegative Decimal")
    if value == 0:
        return Decimal("0")
    return value.normalize()


def _canonicalize_value(field: str, value: object) -> object:
    if value is None:
        if field not in _NULLABLE_FIELDS:
            raise LedgerBatchInvalid(f"{field} cannot be null")
        return None
    if field in _DATE_FIELDS:
        return _date(value, field)
    if field in _TIMESTAMP_FIELDS:
        return _timestamp(value, field)
    if field in _NUMERIC_FIELDS:
        return _numeric(value, field)
    if field == "calls":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise LedgerBatchInvalid("calls must be a nonnegative integer")
        return value
    text = _text(value, field)
    if field == "credential_lane" and text not in CREDENTIAL_LANES:
        raise LedgerBatchInvalid("credential_lane is unsupported")
    if field == "phase" and text not in PHASES:
        raise LedgerBatchInvalid("phase is unsupported")
    if field == "event_type" and text not in EVENT_TYPES:
        raise LedgerBatchInvalid("event_type is unsupported")
    if field == "attribution_state" and text not in ATTRIBUTION_STATES:
        raise LedgerBatchInvalid("attribution_state is unsupported")
    if field in {"catalog_digest", "metadata_digest"} and _DIGEST.fullmatch(text) is None:
        raise LedgerBatchInvalid(f"{field} must be a lower case SHA256 digest")
    return text


def _canonicalize_row(row: object) -> Mapping[str, object]:
    if not isinstance(row, Mapping) or set(row) != set(ROW_FIELDS):
        raise LedgerBatchInvalid("ledger row fields are invalid")
    canonical = {field: _canonicalize_value(field, row[field]) for field in ROW_FIELDS}
    if canonical["month_start"] != date(
        canonical["trend_date"].year, canonical["trend_date"].month, 1
    ):
        raise LedgerBatchInvalid("month_start must match the trend date UTC month")
    if canonical["budget_debit_credits"] < canonical["vendor_reported_credits"]:
        raise LedgerBatchInvalid("budget debit cannot be below vendor reported credits")
    if canonical["monthly_effective_spend_before"] != max(
        canonical["monthly_ledger_debit_before"],
        canonical["monthly_balance_delta_before"],
    ):
        raise LedgerBatchInvalid("monthly effective spend does not match its inputs")
    return MappingProxyType(canonical)


def _natural_key(row: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(row[field] for field in NATURAL_KEY)


def _typed_value(value: object) -> Mapping[str, object]:
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, datetime):
        return {
            "type": "timestamp",
            "value": value.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"type": "numeric", "value": format(value, "f")}
    if isinstance(value, int):
        return {"type": "integer", "value": value}
    if isinstance(value, str):
        return {"type": "string", "value": value}
    raise LedgerBatchInvalid("canonical ledger value is unsupported")


def canonical_typed_json(row: object) -> str:
    """Return deterministic typed JSON for one validated ledger row."""
    canonical = _canonicalize_row(row)
    return json.dumps(
        {field: _typed_value(canonical[field]) for field in ROW_FIELDS},
        separators=(",", ":"),
        ensure_ascii=False,
    )


@dataclass(frozen=True, slots=True)
class FundedLaneLedgerBatch:
    rows: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.rows, (tuple, list)):
            raise LedgerBatchInvalid("rows must be a tuple or list")
        canonical = tuple(_canonicalize_row(row) for row in self.rows)
        if len({_natural_key(row) for row in canonical}) != len(canonical):
            raise LedgerBatchInvalid("rows contain a duplicate natural key")
        for run_close in (row for row in canonical if row["event_type"] == "run_close"):
            phases = tuple(
                row
                for row in canonical
                if row["execution_id"] == run_close["execution_id"]
                and row["credential_lane"] == run_close["credential_lane"]
                and row["event_type"] == "phase_close"
            )
            wave1 = any(row["phase"] in WAVE1_PHASES for row in phases)
            expected_count = 4 if wave1 else 21
            if wave1 and (
                {row["phase"] for row in phases} != WAVE1_PHASES
                or any(row["market"] is not None for row in phases)
                or run_close["market"] is not None
            ):
                raise LedgerBatchInvalid("Wave 1 run close requires four global phases")
            if phases and (
                len(phases) != expected_count
                or sum(row["calls"] for row in phases) != run_close["calls"]
                or sum(row["budget_debit_credits"] for row in phases)
                != run_close["budget_debit_credits"]
                or sum(row["vendor_reported_credits"] for row in phases)
                != run_close["vendor_reported_credits"]
                or any(row["recorded_at"] > run_close["recorded_at"] for row in phases)
            ):
                raise LedgerBatchInvalid(
                    f"run close totals do not reconcile with {expected_count} phases"
                )
        object.__setattr__(
            self,
            "rows",
            tuple(sorted(canonical, key=canonical_typed_json)),
        )


@dataclass(frozen=True, slots=True)
class PersistenceResult:
    project: str
    dataset: str
    dry_run: bool
    validated_count: int
    inserted_count: int
    unchanged_count: int
    conflict_count: int
    statement_digest: str
    cleanup_state: str


class _CountRowProtocol(Protocol):
    def __getitem__(self, field: str) -> object: ...


class _LoadJobProtocol(Protocol):
    errors: object
    output_rows: object

    def result(self, retry: Retry | None = ..., timeout: float | None = None) -> object: ...


class _QueryJobProtocol(Protocol):
    errors: object

    def result(
        self,
        page_size: int | None = None,
        max_results: int | None = None,
        retry: Retry | None = ...,
        timeout: float | object | None = ...,
        start_index: int | None = None,
        job_retry: Retry | None = ...,
    ) -> Iterable[_CountRowProtocol]: ...


class _BigQueryClientProtocol(Protocol):
    project: str
    dataset: str
    location: str

    def create_table(self, table: bigquery.Table, *, exists_ok: bool = False) -> bigquery.Table: ...

    def load_table_from_json(
        self,
        rows: list[dict[str, object]],
        destination: bigquery.Table,
        *,
        location: str,
        job_config: bigquery.LoadJobConfig,
    ) -> _LoadJobProtocol: ...

    def query(
        self,
        sql: str,
        *,
        job_config: bigquery.QueryJobConfig,
        location: str,
        retry: Retry | None,
        job_retry: Retry | None,
    ) -> _QueryJobProtocol: ...

    def delete_table(self, table: bigquery.Table, *, not_found_ok: bool) -> None: ...


def _validate_target(project: object, dataset: object) -> tuple[str, str]:
    if project != TARGET_PROJECT or dataset != TARGET_DATASET:
        raise TargetInvalid("only the exact approved staging target is allowed")
    return TARGET_PROJECT, TARGET_DATASET


def _validate_descriptor(descriptor: object, project: str, dataset: str) -> None:
    if (
        getattr(descriptor, "project", None) != project
        or getattr(descriptor, "dataset", None) != dataset
        or getattr(descriptor, "location", None) != TARGET_LOCATION
        or getattr(descriptor, "writer_identity", None) != wave1_pilot_service_identity()
    ):
        raise TargetInvalid("target descriptor is not the exact approved staging writer")


def _validate_client(client: object, project: str, dataset: str) -> _BigQueryClientProtocol:
    try:
        return cast(
            _BigQueryClientProtocol,
            validate_real_client(
                client,
                project,
                writer_identity=wave1_pilot_service_identity(),
            ),
        )
    except OpenIntelligenceTargetInvalid as error:
        raise TargetInvalid("client is not the exact approved staging writer") from error


def _statement_digest(project: str, dataset: str, batch: FundedLaneLedgerBatch) -> str:
    payload = json.dumps(
        {
            "project": project,
            "dataset": dataset,
            "table": TABLE_NAME,
            "natural_key": NATURAL_KEY,
            "rows": [json.loads(canonical_typed_json(row)) for row in batch.rows],
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _schema() -> list[bigquery.SchemaField]:
    fields: list[bigquery.SchemaField] = []
    for field in ROW_FIELDS:
        mode = "NULLABLE" if field in _NULLABLE_FIELDS else "REQUIRED"
        if field in _DATE_FIELDS:
            field_type = "DATE"
        elif field in _TIMESTAMP_FIELDS:
            field_type = "TIMESTAMP"
        elif field in _NUMERIC_FIELDS:
            field_type = "NUMERIC"
        elif field == "calls":
            field_type = "INT64"
        else:
            field_type = "STRING"
        fields.append(bigquery.SchemaField(field, field_type, mode=mode))
    return fields


def _temporary_table(project: str, dataset: str, statement_digest: str) -> bigquery.Table:
    temporary = bigquery.Table(
        f"{project}.{dataset}._sccl_{statement_digest[:12]}_{uuid.uuid4().hex}_{TABLE_NAME}",
        schema=_schema(),
    )
    temporary.expires = datetime.now(UTC) + timedelta(hours=1)
    temporary.labels = {"socialcrawl_credit_ledger": "v1"}
    return temporary


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _load_rows(rows: tuple[Mapping[str, object], ...]) -> list[dict[str, object]]:
    return [{field: _json_value(row[field]) for field in ROW_FIELDS} for row in rows]


def _temporary_identifier(temporary: bigquery.Table) -> str:
    return f"`{temporary.project}.{temporary.dataset_id}.{temporary.table_id}`"


def _natural_key_match() -> str:
    matches = []
    for field in NATURAL_KEY:
        operator = "IS NOT DISTINCT FROM" if field == "market" else "="
        matches.append(f"target.`{field}` {operator} staged.`{field}`")
    return " AND ".join(matches)


def _canonical_sql(alias: str) -> str:
    fields = ", ".join(f"{alias}.`{field}` AS `{field}`" for field in ROW_FIELDS)
    return f"TO_JSON_STRING(STRUCT({fields}))"


def _transaction_sql(project: str, dataset: str, temporary: bigquery.Table) -> str:
    target = f"`{project}.{dataset}.{TABLE_NAME}`"
    staged = _temporary_identifier(temporary)
    natural_key_match = _natural_key_match()
    staged_json = _canonical_sql("staged")
    target_json = _canonical_sql("target")
    fields = ", ".join(f"`{field}`" for field in ROW_FIELDS)
    selected_fields = ", ".join(f"staged.`{field}`" for field in ROW_FIELDS)
    return (
        "DECLARE unchanged_count INT64 DEFAULT 0;\n"
        "DECLARE inserted_count INT64 DEFAULT 0;\n"
        "DECLARE conflict_count INT64 DEFAULT 0;\n"
        "DECLARE status INT64 DEFAULT 0;\n"
        "BEGIN TRANSACTION;\n"
        "ASSERT NOT EXISTS (\n"
        f"  WITH combined_phases AS (\n"
        f"    SELECT target.* FROM {target} AS target\n"
        "    WHERE target.event_type = 'phase_close'\n"
        "    UNION ALL\n"
        f"    SELECT staged.* FROM {staged} AS staged\n"
        "    WHERE staged.event_type = 'phase_close'\n"
        f"      AND NOT EXISTS (SELECT 1 FROM {target} AS target\n"
        "        WHERE target.execution_id = staged.execution_id\n"
        "          AND target.credential_lane = staged.credential_lane\n"
        "          AND target.market IS NOT DISTINCT FROM staged.market\n"
        "          AND target.phase = staged.phase\n"
        "          AND target.event_type = staged.event_type)\n"
        "  ), phase_totals AS (\n"
        "    SELECT execution_id, credential_lane, COUNT(*) AS phase_close_count,\n"
        "      COUNTIF(STARTS_WITH(phase, 'wave1_')) AS wave1_phase_close_count,\n"
        "      COUNT(DISTINCT IF(STARTS_WITH(phase, 'wave1_'), phase, NULL)) "
        "AS wave1_distinct_phase_count,\n"
        "      COUNTIF(STARTS_WITH(phase, 'wave1_') AND phase NOT IN "
        "('wave1_tiktok_sound','wave1_instagram_reels',"
        "'wave1_youtube_shorts_comments','wave1_reddit_comments')) "
        "AS wave1_invalid_phase_count,\n"
        "      COUNTIF(STARTS_WITH(phase, 'wave1_') AND market IS NOT NULL) "
        "AS wave1_nonnull_market_count,\n"
        "      MAX(recorded_at) AS last_phase_close_at,\n"
        "      SUM(calls) AS calls, SUM(budget_debit_credits) AS budget_debit_credits,\n"
        "      SUM(vendor_reported_credits) AS vendor_reported_credits\n"
        "    FROM combined_phases GROUP BY execution_id, credential_lane\n"
        "  )\n"
        f"  SELECT 1 FROM {staged} AS run_close\n"
        "  LEFT JOIN phase_totals USING (execution_id, credential_lane)\n"
        "  WHERE run_close.event_type = 'run_close'\n"
        "    AND (CASE WHEN COALESCE(wave1_phase_close_count, 0) > 0 THEN\n"
        "      COALESCE(phase_close_count, 0) != 4\n"
        "      OR COALESCE(wave1_phase_close_count, 0) != 4\n"
        "      OR COALESCE(wave1_distinct_phase_count, 0) != 4\n"
        "      OR COALESCE(wave1_invalid_phase_count, 0) != 0\n"
        "      OR COALESCE(wave1_nonnull_market_count, 0) != 0\n"
        "      OR run_close.market IS NOT NULL\n"
        "    ELSE COALESCE(phase_close_count, 0) != 21 END\n"
        "      OR COALESCE(phase_totals.calls, -1) != run_close.calls\n"
        "      OR COALESCE(phase_totals.budget_debit_credits, -1) != run_close.budget_debit_credits\n"
        "      OR COALESCE(phase_totals.vendor_reported_credits, -1) != run_close.vendor_reported_credits\n"
        "      OR last_phase_close_at > run_close.recorded_at)\n"
        ") AS 'run close totals';\n"
        "SET conflict_count = (\n"
        f"  SELECT COUNT(*) FROM {staged} AS staged JOIN {target} AS target "
        f"ON {natural_key_match}\n"
        f"  WHERE {staged_json} != {target_json}\n"
        ");\n"
        "BEGIN\n"
        "  ASSERT conflict_count = 0 AS 'immutable_conflict';\n"
        "EXCEPTION WHEN ERROR THEN\n"
        "  ROLLBACK TRANSACTION;\n"
        "  SET status = 1;\n"
        "END;\n"
        "IF status = 0 THEN\n"
        "SET unchanged_count = (\n"
        f"  SELECT COUNT(*) FROM {staged} AS staged JOIN {target} AS target "
        f"ON {natural_key_match}\n"
        f"  WHERE {staged_json} = {target_json}\n"
        ");\n"
        f"INSERT INTO {target} ({fields})\n"
        f"SELECT {selected_fields} FROM {staged} AS staged\n"
        f"WHERE NOT EXISTS (SELECT 1 FROM {target} AS target WHERE {natural_key_match});\n"
        "SET inserted_count = @@row_count;\n"
        "COMMIT TRANSACTION;\n"
        "END IF;\n"
        "SELECT inserted_count AS inserted_count, unchanged_count AS unchanged_count, "
        "conflict_count AS conflict_count, status AS status;"
    )


def _count(row: _CountRowProtocol, field: str) -> int:
    try:
        value = row[field]
    except (KeyError, TypeError) as error:
        raise PersistenceError(f"transaction returned invalid {field}") from error
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PersistenceError(f"transaction returned invalid {field}")
    return value


def _transaction_counts(job: _QueryJobProtocol) -> tuple[int, int, int, int]:
    rows = iter(job.result(retry=None, job_retry=None))
    if getattr(job, "errors", None):
        raise PersistenceError("transaction job returned errors")
    try:
        row = next(rows)
    except StopIteration as error:
        raise PersistenceError("transaction did not return exactly one row") from error
    try:
        next(rows)
    except StopIteration:
        pass
    else:
        raise PersistenceError("transaction did not return exactly one row")
    return tuple(
        _count(row, field)
        for field in ("inserted_count", "unchanged_count", "conflict_count", "status")
    )


def _result(
    *,
    batch: FundedLaneLedgerBatch,
    dry_run: bool,
    statement_digest: str,
    inserted_count: int,
    unchanged_count: int,
    conflict_count: int,
    cleanup_state: str,
) -> PersistenceResult:
    return PersistenceResult(
        project=TARGET_PROJECT,
        dataset=TARGET_DATASET,
        dry_run=dry_run,
        validated_count=len(batch.rows),
        inserted_count=inserted_count,
        unchanged_count=unchanged_count,
        conflict_count=conflict_count,
        statement_digest=statement_digest,
        cleanup_state=cleanup_state,
    )


def _failure(error: Exception) -> PersistenceError:
    if isinstance(error, PersistenceError):
        return error
    if "immutable_conflict" in str(error).lower():
        return ImmutableConflict("immutable ledger content conflicts with an existing row")
    return PersistenceError("funded lane ledger persistence failed")


def persist_funded_lane_rows(
    *,
    project: str,
    dataset: str,
    client: object,
    batch: FundedLaneLedgerBatch,
    dry_run: bool,
) -> PersistenceResult:
    """Validate and insert immutable ledger rows through an injected client."""
    if type(dry_run) is not bool:
        raise LedgerBatchInvalid("dry_run must be boolean")
    project, dataset = _validate_target(project, dataset)
    if not isinstance(batch, FundedLaneLedgerBatch):
        raise LedgerBatchInvalid("batch must be FundedLaneLedgerBatch")
    statement_digest = _statement_digest(project, dataset, batch)
    if dry_run:
        _validate_descriptor(client, project, dataset)
        return _result(
            batch=batch,
            dry_run=True,
            statement_digest=statement_digest,
            inserted_count=0,
            unchanged_count=0,
            conflict_count=0,
            cleanup_state="not_started",
        )

    writer = _validate_client(client, project, dataset)
    inserted_count = 0
    unchanged_count = 0
    conflict_count = 0
    created_tables: list[bigquery.Table] = []
    primary_error: PersistenceError | None = None
    primary_cause: Exception | None = None
    cleanup_exceptions: list[Exception] = []

    try:
        if batch.rows:
            temporary = _temporary_table(project, dataset, statement_digest)
            created_tables.append(temporary)
            writer.create_table(temporary, exists_ok=False)
            load_job = writer.load_table_from_json(
                _load_rows(batch.rows),
                temporary,
                location=TARGET_LOCATION,
                job_config=bigquery.LoadJobConfig(
                    source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                    write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
                    autodetect=False,
                ),
            )
            load_job.result()
            if getattr(load_job, "errors", None):
                raise PersistenceError("temporary load returned row errors")
            if (
                isinstance(load_job.output_rows, bool)
                or not isinstance(load_job.output_rows, int)
                or load_job.output_rows != len(batch.rows)
            ):
                raise PersistenceError("temporary load was partial")
            query_job = writer.query(
                _transaction_sql(project, dataset, temporary),
                location=TARGET_LOCATION,
                job_config=bigquery.QueryJobConfig(use_legacy_sql=False),
                retry=None,
                job_retry=None,
            )
            inserted_count, unchanged_count, conflict_count, status = _transaction_counts(query_job)
            if status == 1:
                raise ImmutableConflict("immutable ledger content conflicts with an existing row")
            if status != 0 or conflict_count != 0:
                raise PersistenceError("transaction returned an invalid status")
            if inserted_count + unchanged_count != len(batch.rows):
                raise PersistenceError("transaction row counts do not reconcile")
    except Exception as error:
        primary_error = _failure(error)
        if primary_error is not error:
            primary_cause = error
    finally:
        for temporary in reversed(created_tables):
            try:
                writer.delete_table(temporary, not_found_ok=True)
            except Exception as error:
                cleanup_exceptions.append(error)

    cleanup_state = "failed" if cleanup_exceptions else "complete"
    result = _result(
        batch=batch,
        dry_run=False,
        statement_digest=statement_digest,
        inserted_count=inserted_count,
        unchanged_count=unchanged_count,
        conflict_count=conflict_count,
        cleanup_state=cleanup_state,
    )
    cleanup_errors = tuple(
        CleanupFailure("temporary table cleanup failed", result=result) for _ in cleanup_exceptions
    )
    for cleanup_error, cleanup_exception in zip(cleanup_errors, cleanup_exceptions, strict=True):
        cleanup_error.__cause__ = cleanup_exception
    if primary_error is not None:
        primary_error.result = result
        primary_error.cleanup_errors = cleanup_errors
        if primary_cause is not None:
            raise primary_error from primary_cause
        raise primary_error
    if cleanup_errors:
        aggregate = CleanupFailure(
            "temporary table cleanup failed",
            result=result,
            cleanup_errors=cleanup_errors,
        )
        raise aggregate from cleanup_exceptions[0]
    return result
