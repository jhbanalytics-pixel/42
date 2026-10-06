"""Wave 1 measured source values, ledgered per funded execution.

Source Lab holds one immutable row per route per day and its daily snapshot is
the only writer of those rows. A pilot therefore never writes Source Lab; it
ledgers what it measured here, in the funded dataset it already writes, and the
next snapshot carries the latest succeeded pilot's values into the day's rows
through the authorized view in the staging dataset (ruled by Albert, 5 Sep 2026,
after attempt 16 collided with the snapshot's rows on 4 Sep).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from types import MappingProxyType

from google.cloud import bigquery

from src.analysis.open_intelligence.funded_lane import wave1_pilot_service_identity
from src.analysis.open_intelligence.persistence import validate_real_client
from src.analysis.open_intelligence.source_lab import (
    Wave1SourceValueResult,
    evaluate_wave1_source_value,
)
from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

CONTRACT_VERSION = "socialcrawl_wave1_source_values_v1"
TABLE = "socialcrawl_wave1_source_values_v1"
VIEW = "v_socialcrawl_wave1_source_values_v1"
TARGET_PROJECT = "ogilvy-trends-v2"
TARGET_DATASET = "trends_v2_staging_funded"
VIEW_DATASET = "trends_v2_staging"
TARGET_LOCATION = "US"
CREDENTIAL_LANE = "ogilvy_funded"
# A pilot's values are carried by snapshots taken within two days of its close;
# after that a route stays inventory-only until a fresh pilot measures it.
CARRY_WINDOW = timedelta(hours=48)
WAVE1_ROUTE_PATHS = frozenset(f"/v1/{route}" for route in WAVE1_ROUTE_SPECS)
READ_LIMIT = len(WAVE1_ROUTE_PATHS) + 1
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
_STATES = frozenset({"passed", "permanently_rejected"})
_VERDICTS = frozenset({"succeeded", "failed"})
_ATTRIBUTION_STATES = frozenset({"complete", "conservative", "gap_detected"})


class Wave1SourceValuesInvalid(ValueError):
    pass


class Wave1SourceValuePersistenceError(RuntimeError):
    pass


class Wave1SourceValueConflict(Wave1SourceValuePersistenceError):
    pass


@dataclass(frozen=True, slots=True)
class Wave1SourceValueRow:
    values_contract_version: str
    source_value_id: str
    execution_id: str
    run_id: str
    recorded_at: datetime
    credential_lane: str
    close_verdict: str
    attribution_state: str
    source_sha: str | None
    catalog_digest: str
    metadata_digest: str
    route_path: str
    state: str
    reason: str | None
    unique_observations: int
    marginal_candidates: int
    marginal_evidence: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Wave1SourceValuePersistenceResult:
    inserted_count: int
    unchanged_count: int


@dataclass(frozen=True, slots=True)
class Wave1SourceValueCarry:
    execution_id: str
    recorded_at: datetime
    catalog_digest: str
    metadata_digest: str
    values: Mapping[str, Wave1SourceValueResult]


_FIELDS = tuple(field.name for field in fields(Wave1SourceValueRow))
_TIMESTAMPS = frozenset({"recorded_at", "created_at"})
_INTEGERS = frozenset({"unique_observations", "marginal_candidates", "marginal_evidence"})


def wave1_close_succeeded(receipt: object) -> bool:
    """The funded close verdict the durable result records.

    Complete attribution, or the conservative case where the vendor and this
    run's balance movement agree with zero gap and only the ledger's quoted debit
    sits above them: SocialCrawl serves a repeated query from cache at no charge
    and reports zero credits for it, while the ledger debits the catalog quote
    before the call (attempts 14 to 16 on staging, 4 Sep 2026; ratified by Albert
    5 Sep 2026). Either way the balance must have been measured and no terminal
    event may have closed the run.
    """

    if (
        getattr(receipt, "balance_read_status", None) != "measured"
        or getattr(receipt, "terminal_id", None) is not None
    ):
        return False
    if receipt.attribution_state == "complete":
        return True
    return (
        receipt.attribution_state == "conservative"
        and receipt.attribution_gap_credits == 0
        and receipt.run_balance_delta is not None
        and receipt.run_balance_delta == receipt.vendor_reported_credits
        and receipt.vendor_reported_credits <= receipt.budget_debit_credits
    )


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise Wave1SourceValuesInvalid(f"{field} must be a UTC timestamp")
    return value


def _text(
    value: object, field: str, *, pattern: re.Pattern[str] | None = None, nullable: bool = False
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or value.strip() != value:
        raise Wave1SourceValuesInvalid(f"{field} must be a nonempty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise Wave1SourceValuesInvalid(f"{field} is malformed")
    return value


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Wave1SourceValuesInvalid(f"{field} must be a nonnegative integer")
    return value


def source_value_id(execution_id: str, route_path: str) -> str:
    canonical = f"{execution_id}\n{route_path}".encode()
    return "swsv_" + hashlib.sha256(canonical).hexdigest()


def build_wave1_source_value_rows(
    *,
    execution_id: str,
    run_id: str,
    recorded_at: datetime,
    close_verdict: str,
    attribution_state: str,
    source_sha: str | None,
    catalog_digest: str,
    metadata_digest: str,
    values: Mapping[str, Wave1SourceValueResult],
    created_at: datetime,
) -> tuple[Wave1SourceValueRow, ...]:
    execution_id = _text(execution_id, "execution_id")
    run_id = _text(run_id, "run_id")
    recorded_at = _utc(recorded_at, "recorded_at")
    created_at = _utc(created_at, "created_at")
    if close_verdict not in _VERDICTS:
        raise Wave1SourceValuesInvalid("close_verdict must be succeeded or failed")
    if attribution_state not in _ATTRIBUTION_STATES:
        raise Wave1SourceValuesInvalid("attribution_state is invalid")
    source_sha = _text(source_sha, "source_sha", pattern=_SOURCE_SHA, nullable=True)
    catalog_digest = _text(catalog_digest, "catalog_digest", pattern=_DIGEST)
    metadata_digest = _text(metadata_digest, "metadata_digest", pattern=_DIGEST)
    if not isinstance(values, Mapping) or not values:
        raise Wave1SourceValuesInvalid("Wave 1 source values are required")
    rows = []
    for route_path in sorted(values):
        result = values[route_path]
        if route_path not in WAVE1_ROUTE_PATHS or not isinstance(result, Wave1SourceValueResult):
            raise Wave1SourceValuesInvalid("Wave 1 source values are invalid")
        if result.state not in _STATES:
            raise Wave1SourceValuesInvalid("Wave 1 source value state is invalid")
        rows.append(
            Wave1SourceValueRow(
                values_contract_version=CONTRACT_VERSION,
                source_value_id=source_value_id(execution_id, route_path),
                execution_id=execution_id,
                run_id=run_id,
                recorded_at=recorded_at,
                credential_lane=CREDENTIAL_LANE,
                close_verdict=close_verdict,
                attribution_state=attribution_state,
                source_sha=source_sha,
                catalog_digest=catalog_digest,
                metadata_digest=metadata_digest,
                route_path=route_path,
                state=result.state,
                reason=_text(result.reason, "reason", nullable=True),
                unique_observations=_count(result.unique_observations, "unique_observations"),
                marginal_candidates=_count(result.marginal_candidates, "marginal_candidates"),
                marginal_evidence=_count(result.marginal_evidence, "marginal_evidence"),
                created_at=created_at,
            )
        )
    return tuple(rows)


def _parameter(field: str, value: object) -> bigquery.ScalarQueryParameter:
    if field in _TIMESTAMPS:
        kind = "TIMESTAMP"
    elif field in _INTEGERS:
        kind = "INT64"
    else:
        kind = "STRING"
    return bigquery.ScalarQueryParameter(field, kind, value)


def _job_config(row: Wave1SourceValueRow, *, dry_run: bool) -> bigquery.QueryJobConfig:
    return bigquery.QueryJobConfig(
        query_parameters=[_parameter(field, getattr(row, field)) for field in _FIELDS],
        use_legacy_sql=False,
        dry_run=dry_run,
        use_query_cache=False if dry_run else None,
    )


def _target_identifier() -> str:
    return f"`{TARGET_PROJECT}.{TARGET_DATASET}.{TABLE}`"


def _transaction_sql() -> str:
    target = _target_identifier()
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
        "  WHERE target.source_value_id = @source_value_id\n"
        f"    AND NOT ({exact})\n"
        ");\n"
        "SET unchanged_count = (\n"
        f"  SELECT COUNT(*) FROM {target} AS target\n"
        "  WHERE target.source_value_id = @source_value_id\n"
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
    columns = ", ".join(f"`{field}`" for field in _FIELDS)
    return (
        f"SELECT {columns} FROM {_target_identifier()} "
        "WHERE source_value_id = @source_value_id LIMIT 2"
    )


def _rows(job: object, *, max_results: int) -> tuple[object, ...]:
    if getattr(job, "errors", None):
        raise Wave1SourceValuePersistenceError("Wave 1 source value query returned errors")
    return tuple(job.result(max_results=max_results, retry=None, job_retry=None))


def _mapping(row: object) -> Mapping[str, object]:
    if isinstance(row, Mapping):
        return row
    try:
        return dict(row.items())
    except (AttributeError, TypeError) as error:
        raise Wave1SourceValuePersistenceError("Wave 1 source value row is unreadable") from error


def _count_field(row: object, field: str) -> int:
    try:
        value = _mapping(row)[field]
    except KeyError as error:
        raise Wave1SourceValuePersistenceError(
            "Wave 1 source value transaction result is incomplete"
        ) from error
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Wave1SourceValuePersistenceError("Wave 1 source value transaction count is invalid")
    return value


def _read_exact_row(writer: object, row: Wave1SourceValueRow) -> None:
    rows = _rows(
        writer.query(
            _readback_sql(),
            location=TARGET_LOCATION,
            job_config=_job_config(row, dry_run=False),
            retry=None,
            job_retry=None,
        ),
        max_results=2,
    )
    if len(rows) != 1:
        raise Wave1SourceValuePersistenceError(
            "Wave 1 source value readback returned invalid cardinality"
        )
    try:
        actual = Wave1SourceValueRow(**{field: _mapping(rows[0])[field] for field in _FIELDS})
    except (KeyError, TypeError, ValueError) as error:
        raise Wave1SourceValuePersistenceError(
            "Wave 1 source value readback row is invalid"
        ) from error
    if actual != row:
        raise Wave1SourceValueConflict("Wave 1 source value ID conflicts with existing content")


def persist_wave1_source_value_rows(
    *,
    project: str,
    dataset: str,
    client: object,
    rows: tuple[Wave1SourceValueRow, ...],
) -> Wave1SourceValuePersistenceResult:
    if project != TARGET_PROJECT or dataset != TARGET_DATASET:
        raise ValueError("Wave 1 source value writer requires the exact staging target")
    if (
        not isinstance(rows, tuple)
        or not rows
        or any(not isinstance(row, Wave1SourceValueRow) for row in rows)
        or len({row.source_value_id for row in rows}) != len(rows)
    ):
        raise ValueError("Wave 1 source value rows are invalid")
    try:
        writer = validate_real_client(
            client, TARGET_PROJECT, writer_identity=wave1_pilot_service_identity()
        )
    except Exception as error:
        raise ValueError("Wave 1 source value writer requires the exact funded writer") from error
    transaction = _transaction_sql()
    inserted_total = unchanged_total = 0
    for row in rows:
        dry_job = writer.query(
            transaction,
            location=TARGET_LOCATION,
            job_config=_job_config(row, dry_run=True),
            retry=None,
            job_retry=None,
        )
        if getattr(dry_job, "errors", None):
            raise Wave1SourceValuePersistenceError("Wave 1 source value dry run returned errors")
        dry_job.result()
        try:
            transaction_rows = _rows(
                writer.query(
                    transaction,
                    location=TARGET_LOCATION,
                    job_config=_job_config(row, dry_run=False),
                    retry=None,
                    job_retry=None,
                ),
                max_results=2,
            )
        except Exception as transaction_error:
            # The response was lost after the statement may have committed; the
            # exact readback decides, the same way the control receipt does.
            try:
                _read_exact_row(writer, row)
            except Wave1SourceValuePersistenceError as readback_error:
                raise readback_error from transaction_error
            unchanged_total += 1
            continue
        if len(transaction_rows) != 1:
            raise Wave1SourceValuePersistenceError(
                "Wave 1 source value transaction returned invalid cardinality"
            )
        inserted = _count_field(transaction_rows[0], "inserted_count")
        unchanged = _count_field(transaction_rows[0], "unchanged_count")
        conflict = _count_field(transaction_rows[0], "conflict_count")
        if conflict:
            raise Wave1SourceValueConflict("Wave 1 source value ID conflicts with existing content")
        if inserted + unchanged != 1:
            raise Wave1SourceValuePersistenceError(
                "Wave 1 source value transaction did not affect one row"
            )
        _read_exact_row(writer, row)
        inserted_total += inserted
        unchanged_total += unchanged
    return Wave1SourceValuePersistenceResult(
        inserted_count=inserted_total, unchanged_count=unchanged_total
    )


def _read_sql(project: str, dataset: str) -> str:
    view = f"`{project}.{dataset}.{VIEW}`"
    return (
        "WITH latest AS (\n"
        f"  SELECT execution_id FROM {view}\n"
        "  WHERE close_verdict = 'succeeded'\n"
        "    AND recorded_at < @observed_at\n"
        "    AND recorded_at >= @window_start\n"
        "  ORDER BY recorded_at DESC, execution_id DESC\n"
        "  LIMIT 1\n"
        ")\n"
        "SELECT measured.execution_id, measured.recorded_at, measured.catalog_digest, "
        "measured.metadata_digest, measured.route_path, measured.state, measured.reason, "
        "measured.unique_observations, measured.marginal_candidates, "
        "measured.marginal_evidence\n"
        f"FROM {view} AS measured\n"
        "JOIN latest USING (execution_id)\n"
        "WHERE measured.close_verdict = 'succeeded'\n"
        "ORDER BY measured.route_path"
    )


def read_latest_wave1_source_values(
    *,
    client: object,
    observed_at: datetime,
    project: str = TARGET_PROJECT,
    dataset: str = VIEW_DATASET,
    window: timedelta = CARRY_WINDOW,
) -> Wave1SourceValueCarry | None:
    """The latest succeeded pilot's measured values, or None when no pilot closed
    inside the carry window before ``observed_at``."""

    observed_at = _utc(observed_at, "observed_at")
    job = client.query(
        _read_sql(project, dataset),
        location=TARGET_LOCATION,
        job_config=bigquery.QueryJobConfig(
            use_legacy_sql=False,
            query_parameters=[
                bigquery.ScalarQueryParameter("observed_at", "TIMESTAMP", observed_at),
                bigquery.ScalarQueryParameter("window_start", "TIMESTAMP", observed_at - window),
            ],
        ),
        retry=None,
        job_retry=None,
    )
    rows = tuple(job.result(max_results=READ_LIMIT, retry=None, job_retry=None))
    if not rows:
        return None
    if len(rows) > len(WAVE1_ROUTE_PATHS):
        raise Wave1SourceValuesInvalid("Wave 1 source value carry has too many rows")
    identity = None
    values: dict[str, Wave1SourceValueResult] = {}
    for raw in rows:
        try:
            row = _mapping(raw)
            route_path = row["route_path"]
            observed = (
                _text(row["execution_id"], "execution_id"),
                _utc(row["recorded_at"], "recorded_at"),
                _text(row["catalog_digest"], "catalog_digest", pattern=_DIGEST),
                _text(row["metadata_digest"], "metadata_digest", pattern=_DIGEST),
            )
            result = evaluate_wave1_source_value(
                unique_observations=_count(row["unique_observations"], "unique_observations"),
                marginal_candidates=_count(row["marginal_candidates"], "marginal_candidates"),
                marginal_evidence=_count(row["marginal_evidence"], "marginal_evidence"),
            )
            state = row["state"]
            reason = row["reason"]
        except (KeyError, TypeError, ValueError, Wave1SourceValuePersistenceError) as error:
            raise Wave1SourceValuesInvalid("Wave 1 source value carry row is invalid") from error
        if route_path not in WAVE1_ROUTE_PATHS or route_path in values:
            raise Wave1SourceValuesInvalid("Wave 1 source value carry routes are invalid")
        if identity is None:
            identity = observed
        elif observed != identity:
            raise Wave1SourceValuesInvalid("Wave 1 source value carry mixes executions")
        if (state, reason) != (result.state, result.reason):
            raise Wave1SourceValuesInvalid("Wave 1 source value carry state disagrees with counts")
        values[route_path] = result
    assert identity is not None
    execution_id, recorded_at, catalog_digest, metadata_digest = identity
    return Wave1SourceValueCarry(
        execution_id=execution_id,
        recorded_at=recorded_at,
        catalog_digest=catalog_digest,
        metadata_digest=metadata_digest,
        values=MappingProxyType(values),
    )
