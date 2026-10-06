"""Run one zero-credit Source Lab snapshot against the staging boundary."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import google.auth
import requests
from google.auth.compute_engine import credentials as compute_credentials
from google.auth.transport.requests import Request
from google.cloud import bigquery, secretmanager
from google.oauth2 import service_account

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.open_intelligence.funded_lane import (
    APPROVED_CATALOG_DIGEST,
    APPROVED_METADATA_DIGEST,
    HISTORICAL_METADATA_DIGEST,
)
from src.analysis.open_intelligence.funded_source_values import (
    Wave1SourceValuesInvalid,
    read_latest_wave1_source_values,
)
from src.analysis.open_intelligence.persistence import TARGET_WRITER_IDENTITY, PersistenceTarget
from src.analysis.open_intelligence.source_lab import (
    SOURCE_LAB_CONTRACT_VERSION,
    CatalogInvalid,
    SourceLabInventoryInvalid,
    build_source_performance_rows,
    normalize_balance,
    normalize_catalog,
    wave1_route_identity,
)
from src.analysis.open_intelligence.source_lab_persistence import (
    SOURCE_LAB_TABLE,
    CleanupFailure,
    ImmutableConflict,
    PersistenceError,
    SourceLabBatchInvalid,
    SourceLabRowBatch,
    persist_source_lab_rows,
)
from src.contracts.open_intelligence import CONTRACT_VERSION, ResolvedScope
from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
LOCATION = "US"
SERVICE_ACCOUNT = TARGET_WRITER_IDENTITY
SECRET_ID = "SOCIALCRAWL_OGILVY_API_KEY"
# The approved IAM condition admits exactly version 1 of the funded secret, so
# the reader names that version rather than the latest alias.
SECRET_VERSION = "1"
CATALOG_URL = "https://www.socialcrawl.dev/v1/utility/endpoints"
BALANCE_URL = "https://www.socialcrawl.dev/v1/credits/balance"
EXPECTATION_VERSION = "socialcrawl_catalog_2026_09_27"
HISTORICAL_EXPECTATION_VERSION = "socialcrawl_catalog_2026_09_05"
CURRENT_CATALOG_DATE = date(2026, 9, 27)
HTTP_TIMEOUT_SECONDS = 30
# The first snapshot of a day owns the day's rows: a rerun reuses its observation
# timestamps and, on inventory rows, the measured fields it carried from the
# pilot, so a pilot closing between two snapshots waits for the next day.
MEASURED_FIELDS = (
    "status",
    "rows",
    "unique_lift",
    "blocking_reason",
    "kill_test_result",
    "downstream_consumers",
)
EXISTING_TIMESTAMP_QUERY = (
    "SELECT `source_performance_id`, `route_role`, `last_checked_at`, `observed_at`, "
    "`status`, `rows`, `unique_lift`, `blocking_reason`, `kill_test_result`, "
    "`downstream_consumers`\n"
    f"FROM `{PROJECT}.{DATASET}.{SOURCE_LAB_TABLE}`\n"
    "WHERE `client_scope_id` = @client_scope_id\n"
    "  AND `run_id` = @run_id\n"
    "  AND `contract_version` = @contract_version\n"
    "  AND `metric_date` = @metric_date\n"
)


class SnapshotFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--target", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser


def _authorized_credentials(
    credential_loader: Callable[[], tuple[Any, str | None]],
) -> object:
    try:
        credentials, adc_project = credential_loader()
        credential_type = type(credentials)
        if adc_project != PROJECT or credential_type not in (
            service_account.Credentials,
            compute_credentials.Credentials,
        ):
            raise ValueError
        quota_project = credentials.quota_project_id
        if quota_project is not None and quota_project != PROJECT:
            raise ValueError
        identity = credentials.service_account_email
        if credential_type is compute_credentials.Credentials and identity in (
            None,
            "",
            "default",
        ):
            credentials.refresh(Request())
            identity = credentials.service_account_email
        if identity != SERVICE_ACCOUNT:
            raise ValueError
    except Exception:
        raise SnapshotFailure("runtime_identity_invalid") from None
    return credentials


def _read_json(session: object, url: str, code: str) -> object:
    try:
        response = session.get(
            url,
            timeout=HTTP_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        if 300 <= response.status_code < 400 or response.history:
            raise ValueError
        response.raise_for_status()
        return response.json()
    except Exception:
        raise SnapshotFailure(code) from None


def _require_zero_credit(payload: object) -> None:
    if not isinstance(payload, Mapping) or "credits_used" not in payload:
        raise SnapshotFailure("nonzero_credit_receipt")
    value = payload["credits_used"]
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise SnapshotFailure("nonzero_credit_receipt")
    credits_used = Decimal(str(value))
    if not credits_used.is_finite() or credits_used != 0:
        raise SnapshotFailure("nonzero_credit_receipt")


def _observed_catalog_counts(payload: object) -> tuple[int, int]:
    try:
        data = payload["data"]
        stats = data["stats"]
        platforms = stats["platforms"]
        endpoints = stats["endpoints"]
        if (
            type(platforms) is not int
            or platforms <= 0
            or type(endpoints) is not int
            or endpoints <= 0
        ):
            raise ValueError
        return platforms, endpoints
    except (KeyError, TypeError, ValueError):
        raise SnapshotFailure("catalog_contract_invalid") from None


def _scope(observed_at: datetime) -> ResolvedScope:
    metric_date = observed_at.date().isoformat()
    return ResolvedScope(
        client_scope_id="ogilvy_default",
        market_scope=("za", "ng", "ke"),
        brand_config_id="ogilvy_42",
        audience_lens_ids=(),
        theme_id="ogilvy_intelligence",
        run_id=f"source_lab_{metric_date}",
        contract_version=CONTRACT_VERSION,
    )


def _number(value: Decimal) -> int | float:
    integral = value.to_integral_value()
    return int(integral) if value == integral else float(value)


def _persisted_timestamp(value: object, *, metric_date: date, nullable: bool) -> datetime | None:
    if value is None:
        if nullable:
            return None
        raise ImmutableConflict("existing Source Lab timestamp is missing")
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ImmutableConflict("existing Source Lab timestamp is invalid")
    timestamp = value.astimezone(UTC)
    if timestamp.date() != metric_date:
        raise ImmutableConflict("existing Source Lab timestamp is outside the metric date")
    return timestamp


def _persisted_measured_fields(existing: Mapping[str, object]) -> dict[str, object]:
    fields: dict[str, object] = {}
    for field in MEASURED_FIELDS:
        try:
            value = existing[field]
        except (KeyError, TypeError):
            raise ImmutableConflict("existing Source Lab measured fields are missing") from None
        if field == "downstream_consumers":
            value = tuple(value or ())
        elif field == "unique_lift" and value is not None:
            value = float(value)
        fields[field] = value
    return fields


def _wave1_carry(
    client: object, *, observed_at: datetime, catalog: object
) -> tuple[Mapping[str, object] | None, dict[str, object]]:
    """The latest succeeded pilot's values when its route identity is the approved one."""

    try:
        carry = read_latest_wave1_source_values(client=client, observed_at=observed_at)
    except Wave1SourceValuesInvalid:
        raise SnapshotFailure("wave1_values_invalid") from None
    if carry is None:
        return None, {"state": "none"}
    report = {
        "execution_id": carry.execution_id,
        "recorded_at": carry.recorded_at.isoformat().replace("+00:00", "Z"),
        "routes": len(carry.values),
    }
    try:
        observed_identity = wave1_route_identity(catalog, tuple(WAVE1_ROUTE_SPECS))
    except CatalogInvalid:
        observed_identity = None
    approved = (
        APPROVED_CATALOG_DIGEST,
        APPROVED_METADATA_DIGEST
        if observed_at.date() >= CURRENT_CATALOG_DATE
        else HISTORICAL_METADATA_DIGEST,
    )
    if (carry.catalog_digest, carry.metadata_digest) != approved or observed_identity != approved:
        return None, {"state": "withheld_route_identity_drift", **report}
    return carry.values, {"state": "merged", **report}


def _reuse_first_observation_timestamps(
    client: object,
    rows: tuple[Mapping[str, object], ...],
    *,
    scope: ResolvedScope,
    metric_date: date,
    expected_row_count: int,
) -> tuple[Mapping[str, object], ...]:
    fresh_by_id = {row["source_performance_id"]: row for row in rows}
    if len(rows) != expected_row_count or len(fresh_by_id) != len(rows):
        raise SourceLabBatchInvalid("fresh Source Lab rows are incomplete")
    existing_timestamp_limit = expected_row_count + 1
    query = client.query(
        f"{EXISTING_TIMESTAMP_QUERY}LIMIT {existing_timestamp_limit}",
        location=LOCATION,
        job_config=bigquery.QueryJobConfig(
            use_legacy_sql=False,
            query_parameters=[
                bigquery.ScalarQueryParameter("client_scope_id", "STRING", scope.client_scope_id),
                bigquery.ScalarQueryParameter("run_id", "STRING", scope.run_id),
                bigquery.ScalarQueryParameter(
                    "contract_version", "STRING", SOURCE_LAB_CONTRACT_VERSION
                ),
                bigquery.ScalarQueryParameter("metric_date", "DATE", metric_date),
            ],
        ),
        retry=None,
        job_retry=None,
    )
    existing_rows = tuple(query.result(max_results=existing_timestamp_limit))
    if not existing_rows:
        return rows
    if len(existing_rows) != expected_row_count:
        raise ImmutableConflict("existing Source Lab snapshot is incomplete")
    existing_by_id: dict[str, dict[str, object]] = {}
    for existing in existing_rows:
        try:
            source_performance_id = existing["source_performance_id"]
            route_role = existing["route_role"]
            last_checked_at = existing["last_checked_at"]
            observed_at = existing["observed_at"]
        except (KeyError, TypeError):
            raise ImmutableConflict("existing Source Lab snapshot row is invalid") from None
        if not isinstance(source_performance_id, str) or source_performance_id in existing_by_id:
            raise ImmutableConflict("existing Source Lab snapshot keys are invalid")
        fresh = fresh_by_id.get(source_performance_id)
        if fresh is None or route_role != fresh["route_role"]:
            raise ImmutableConflict("existing Source Lab snapshot identity is mixed")
        is_balance = route_role == "utility_balance"
        persisted_last_checked_at = _persisted_timestamp(
            last_checked_at, metric_date=metric_date, nullable=False
        )
        persisted_observed_at = _persisted_timestamp(
            observed_at, metric_date=metric_date, nullable=not is_balance
        )
        if not is_balance and persisted_observed_at is not None:
            raise ImmutableConflict("existing inventory observation timestamp is invalid")
        existing_by_id[source_performance_id] = {
            "last_checked_at": persisted_last_checked_at,
            "observed_at": persisted_observed_at,
        }
        if not is_balance:
            existing_by_id[source_performance_id].update(_persisted_measured_fields(existing))
    if set(existing_by_id) != set(fresh_by_id):
        raise ImmutableConflict("existing Source Lab snapshot keys are incomplete")
    return tuple({**row, **existing_by_id[row["source_performance_id"]]} for row in rows)


def _validate_receipt(receipt: object, *, dry_run: bool, expected_count: int) -> None:
    expected_cleanup = "not_started" if dry_run else "complete"
    if (
        getattr(receipt, "project", None) != PROJECT
        or getattr(receipt, "dataset", None) != DATASET
        or getattr(receipt, "dry_run", None) is not dry_run
        or getattr(receipt, "validated_count", None) != expected_count
        or getattr(receipt, "conflict_count", None) != 0
        or getattr(receipt, "cleanup_state", None) != expected_cleanup
    ):
        raise SnapshotFailure("persistence_failed")
    inserted = getattr(receipt, "inserted_count", None)
    unchanged = getattr(receipt, "unchanged_count", None)
    if (
        type(inserted) is not int
        or type(unchanged) is not int
        or inserted < 0
        or unchanged < 0
        or (dry_run and (inserted != 0 or unchanged != 0))
        or (not dry_run and inserted + unchanged != expected_count)
    ):
        raise SnapshotFailure("persistence_failed")


def run_snapshot(
    *,
    target: str,
    apply: bool,
    clock: Callable[[], datetime],
    credential_loader: Callable[[], tuple[object, str | None]],
    secret_reader: Callable[[str], str],
    session_factory: Callable[[], object],
    client_factory: Callable[..., object],
    persist: Callable[..., object],
) -> dict[str, object]:
    if target != "staging" or type(apply) is not bool:
        raise SnapshotFailure("target_invalid")
    credentials = _authorized_credentials(credential_loader)
    try:
        observed_at = clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError
        observed_at = observed_at.astimezone(UTC)
    except Exception:
        raise SnapshotFailure("catalog_contract_invalid") from None
    try:
        key = secret_reader(SECRET_ID)
        if not isinstance(key, str) or not key or key.strip() != key:
            raise ValueError
    except Exception:
        raise SnapshotFailure("secret_unavailable") from None

    session = None
    primary: SnapshotFailure | None = None
    result = None
    try:
        session = session_factory()
        session.headers.update({"x-api-key": key})
        raw_catalog = _read_json(session, CATALOG_URL, "catalog_read_failed")
        _require_zero_credit(raw_catalog)
        platforms, endpoints = _observed_catalog_counts(raw_catalog)
        try:
            catalog = normalize_catalog(
                raw_catalog,
                checked_at=observed_at,
                expected_platforms=platforms,
                expected_endpoints=endpoints,
                wave1_route_ids=tuple(WAVE1_ROUTE_SPECS),
            )
        except CatalogInvalid:
            raise SnapshotFailure("catalog_contract_invalid") from None
        current_catalog = observed_at.date() >= CURRENT_CATALOG_DATE
        if current_catalog:
            try:
                observed_identity = wave1_route_identity(catalog, tuple(WAVE1_ROUTE_SPECS))
            except CatalogInvalid:
                raise SnapshotFailure("catalog_contract_invalid") from None
            if observed_identity != (APPROVED_CATALOG_DIGEST, APPROVED_METADATA_DIGEST):
                raise SnapshotFailure("catalog_contract_invalid")
        raw_balance = _read_json(session, BALANCE_URL, "balance_read_failed")
        _require_zero_credit(raw_balance)
        try:
            balance = normalize_balance(raw_balance, observed_at=observed_at)
        except CatalogInvalid:
            raise SnapshotFailure("balance_read_failed") from None
        scope = _scope(observed_at)
        wave1_values = None
        wave1_report: dict[str, object] = {"state": "unread_in_dry_run"}
        if apply:
            try:
                client = client_factory(
                    project=PROJECT,
                    location=LOCATION,
                    credentials=credentials,
                )
                if (
                    getattr(client, "project", None) != PROJECT
                    or getattr(client, "location", None) != LOCATION
                    or getattr(client, "_credentials", None) is not credentials
                ):
                    raise SnapshotFailure("runtime_identity_invalid")
            except SnapshotFailure:
                raise
            except Exception:
                raise SnapshotFailure("runtime_identity_invalid") from None
            wave1_values, wave1_report = _wave1_carry(
                client, observed_at=observed_at, catalog=catalog
            )
        try:
            rows = build_source_performance_rows(
                scope=scope,
                catalog=catalog,
                balance=balance,
                expectation_version=(
                    EXPECTATION_VERSION if current_catalog else HISTORICAL_EXPECTATION_VERSION
                ),
                wave1_source_values=wave1_values,
            )
        except SourceLabInventoryInvalid:
            raise SnapshotFailure("catalog_contract_invalid") from None
        if apply:
            fresh_rows = rows
            try:
                rows = _reuse_first_observation_timestamps(
                    client,
                    rows,
                    scope=scope,
                    metric_date=observed_at.date(),
                    expected_row_count=len(catalog.endpoints) + 1,
                )
            except ImmutableConflict:
                raise SnapshotFailure("immutable_conflict") from None
            if rows is not fresh_rows:
                wave1_report = {
                    "state": "reused_first_snapshot",
                    "routes": sum(row["status"] != "inventory_only" for row in rows),
                }
        else:
            client = PersistenceTarget(
                project=PROJECT,
                dataset=DATASET,
                location=LOCATION,
                writer_identity=TARGET_WRITER_IDENTITY,
            )
        try:
            batch = SourceLabRowBatch(rows=rows)
        except SourceLabBatchInvalid:
            raise SnapshotFailure("batch_contract_invalid") from None
        try:
            receipt = persist(
                project=PROJECT,
                dataset=DATASET,
                client=client,
                batch=batch,
                dry_run=not apply,
            )
        except ImmutableConflict:
            raise SnapshotFailure("immutable_conflict") from None
        except CleanupFailure:
            raise SnapshotFailure("cleanup_failed") from None
        except PersistenceError:
            raise SnapshotFailure("persistence_failed") from None
        _validate_receipt(receipt, dry_run=not apply, expected_count=len(batch.rows))
        result = {
            "mode": "apply" if apply else "dry-run",
            "target": "staging",
            "run_id": scope.run_id,
            "metric_date": observed_at.date().isoformat(),
            "platforms_observed": catalog.platform_count,
            "endpoints_observed": catalog.route_count,
            "endpoints_normalized": len(catalog.endpoints),
            "dropped_endpoint_rows": [
                {"row_number": item.row_number, "route": item.route, "reason": item.reason}
                for item in catalog.dropped_rows
            ],
            "catalog_digest": catalog.catalog_digest,
            "balance": _number(balance.balance),
            "credits_used": 0,
            "rows_planned": len(batch.rows),
            "inserted": receipt.inserted_count,
            "unchanged": receipt.unchanged_count,
            "conflicts": receipt.conflict_count,
            "cleanup_state": receipt.cleanup_state,
            "wave1_values": wave1_report,
        }
    except SnapshotFailure as error:
        primary = error
    except Exception:
        primary = SnapshotFailure("persistence_failed")
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                primary = SnapshotFailure("cleanup_failed")
    if primary is not None:
        raise primary
    return result


def _default_client_factory(**kwargs: object) -> object:
    return bigquery.Client(**kwargs)


def _default_secret_reader(secret_id: str) -> str:
    if secret_id != SECRET_ID:
        raise ValueError("unexpected secret ID")
    name = f"projects/{PROJECT}/secrets/{SECRET_ID}/versions/{SECRET_VERSION}"
    response = secretmanager.SecretManagerServiceClient().access_secret_version(
        request={"name": name}
    )
    return response.payload.data.decode("utf-8")


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = run_snapshot(
            target=args.target,
            apply=args.apply,
            clock=lambda: datetime.now(UTC),
            credential_loader=google.auth.default,
            secret_reader=_default_secret_reader,
            session_factory=requests.Session,
            client_factory=_default_client_factory,
            persist=persist_source_lab_rows,
        )
    except SnapshotFailure as error:
        print(json.dumps({"error": error.code}, separators=(",", ":")), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
