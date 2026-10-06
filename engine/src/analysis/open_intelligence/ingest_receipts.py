"""Collection receipts of the ingest job, kept in the staging source dataset.

The ingest job ``intelligence-42-ingest-staging`` runs the collection entry point as the
ingest identity, whose only grants are ``roles/bigquery.dataEditor`` on
``intelligence_42_sources_staging`` and ``roles/bigquery.jobUser`` on the project. It holds
no object grant, so the receipt ledger the producer requires in collection only mode is a
table in that same dataset: ``collection_receipts``, one row per execution, created once
and never updated.

``WarehouseCollectionLedger`` is the producer's ledger contract over that table, keyed by
the ingest execution id the entry point verified. ``BigQueryReceiptTable`` is the table,
every value bound as a query parameter. The orchestration identity reads the same table
by execution id through ``BigQueryReceiptTable.by_execution``: its dataset grant there is
``roles/bigquery.dataViewer``, which is why the daily collect stage dispatches the ingest
job and reads its receipt back instead of writing the dataset itself.

Nothing here opens a connection at import; ``native_collection_ledger`` builds the
BigQuery client when it is called.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from .brain_contract import canonical_bytes

PROJECT = "ogilvy-trends-v2"
SOURCE_DATASET = "intelligence_42_sources_staging"
RECEIPT_TABLE = "collection_receipts"
INGEST_JOB_ID = "intelligence-42-ingest-staging"
# The runtime names an execution of a job by the job's name and a five character suffix.
INGEST_EXECUTION = re.compile(rf"{INGEST_JOB_ID}-[a-z0-9]{{5}}")
# Each receipt query is bounded: the request and the wait for its result.
READ_TIMEOUT_SECONDS = 5
ROW_FIELDS = ("execution_id", "trend_date", "receipt_json", "receipt_sha256")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class ReceiptNotYetReadable(Exception):
    """A receipt read the service could not answer now; reading again may succeed."""


def _execution(value: object, code: str) -> str:
    if not isinstance(value, str) or INGEST_EXECUTION.fullmatch(value) is None:
        raise ValueError(code)
    return value


def _day(value: object) -> str:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise ValueError("collection_day_invalid")
    return value


def receipt_row(receipt: Mapping[str, object], *, trend_date: str) -> dict:
    """The row one receipt is stored as: its canonical text and that text's digest."""
    payload = canonical_bytes(dict(receipt)).decode("utf-8")
    return {
        "execution_id": _execution(receipt.get("execution_id"), "collection_receipt_invalid"),
        "trend_date": _day(trend_date),
        "receipt_json": payload,
        "receipt_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }


def daily_ledger_copy(receipt: Mapping[str, object], *, attempt_id: object) -> dict:
    """The copy of an admitted ingest receipt the daily collect stage records.

    The authority kind is dropped and the execution id becomes the collect stage's own
    attempt id, which keys the collection ledger. The candidate binding rebuilds this copy
    from the receipt it bound, so both go through this one function.
    """
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValueError("collection_attempt_invalid")
    copy = {key: value for key, value in receipt.items() if key != "authority_kind"}
    copy["execution_id"] = attempt_id
    return copy


def receipt_source_digest(receipt: Mapping[str, object]) -> str:
    """The source digest a daily capture records for the collection receipt it closed."""
    return hashlib.sha256(canonical_bytes(dict(receipt))).hexdigest()


def read_receipt_row(row: object) -> dict:
    """The receipt a stored row carries, or ``collection_receipt_invalid``.

    The text must be canonical, must hash to the digest stored beside it and must name
    the execution the row is keyed by; a row that fails any of these is not a record.
    """
    code = "collection_receipt_invalid"
    if not isinstance(row, Mapping) or not set(ROW_FIELDS) <= set(row):
        raise ValueError(code)
    payload = row["receipt_json"]
    if not isinstance(payload, str):
        raise ValueError(code)
    try:
        receipt = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError(code) from error
    if (
        not isinstance(receipt, dict)
        or canonical_bytes(receipt).decode("utf-8") != payload
        or hashlib.sha256(payload.encode("utf-8")).hexdigest() != row["receipt_sha256"]
        or receipt.get("execution_id") != _execution(row["execution_id"], code)
    ):
        raise ValueError(code)
    _day(row["trend_date"])
    return receipt


class WarehouseCollectionLedger:
    """The producer's receipt ledger over the ingest receipt table.

    ``operation`` returns the receipt recorded under an ingest execution id or None,
    ``market_day`` the receipts recorded for one ISO date, and ``record`` writes a receipt
    once: the same receipt again is a no op and a differing receipt under the same
    execution id refuses. The insert is conditional on the key being absent and is read
    back, so a concurrent writer is judged by the same rule.
    """

    def __init__(self, table) -> None:
        self._table = table

    def _stored(self, execution_id: str) -> dict | None:
        rows = self._table.by_execution(execution_id)
        if not rows:
            return None
        receipts = [read_receipt_row(row) for row in rows]
        if len(receipts) != 1:
            raise ValueError("collection_receipt_conflict")
        return receipts[0]

    def operation(self, operation_id: str) -> dict | None:
        return self._stored(_execution(operation_id, "collection_operation_invalid"))

    def market_day(self, trend_date: str) -> list[dict]:
        return [read_receipt_row(row) for row in self._table.by_day(_day(trend_date))]

    def record(self, receipt: Mapping[str, object], *, trend_date: str) -> None:
        row = receipt_row(receipt, trend_date=trend_date)
        self._table.insert_once(**row)
        stored = self._stored(row["execution_id"])
        if stored is None:
            raise ValueError("collection_receipt_unrecorded")
        if canonical_bytes(stored) != canonical_bytes(dict(receipt)):
            raise ValueError("collection_receipt_conflict")


class BigQueryReceiptTable:
    """The ``collection_receipts`` table, created once, written by conditional insert.

    Only the ingest identity writes it, and its first write creates it. The ingest side
    passes ``create=True`` so that its reads, which come before its first write, create
    it too; the orchestration side reads with the default and never issues a create.
    """

    def __init__(
        self, client, *, project: str = PROJECT, dataset: str = SOURCE_DATASET, create=False
    ):
        self._client = client
        self._table = f"`{project}.{dataset}.{RECEIPT_TABLE}`"
        self._create = create is True
        self._ensured = False

    def _read(self, sql: str, parameters=()) -> list[dict]:
        """A read: bounded, an absent table is no row, and an unavailable service or a
        timed out read is ``ReceiptNotYetReadable``. Every other error propagates."""
        from google.api_core import exceptions

        try:
            return self._query(sql, parameters, timeout=READ_TIMEOUT_SECONDS)
        except exceptions.NotFound:
            return []
        except (
            exceptions.ServiceUnavailable,
            exceptions.InternalServerError,
            exceptions.TooManyRequests,
            exceptions.GatewayTimeout,
            exceptions.BadGateway,
            TimeoutError,
        ) as error:
            raise ReceiptNotYetReadable(type(error).__name__) from error

    def _query(self, sql: str, parameters=(), *, timeout=None) -> list[dict]:
        from google.cloud import bigquery

        config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(name, "STRING", value) for name, value in parameters
            ]
        )
        if timeout is None:
            job = self._client.query(sql, job_config=config)
            return [dict(row) for row in job.result()]
        job = self._client.query(sql, job_config=config, timeout=timeout)
        return [dict(row) for row in job.result(timeout=timeout)]

    def _ensure(self) -> None:
        if self._ensured:
            return
        self._query(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "execution_id STRING NOT NULL, trend_date STRING NOT NULL, "
            "receipt_json STRING NOT NULL, receipt_sha256 STRING NOT NULL, "
            "recorded_at TIMESTAMP NOT NULL)"
        )
        self._ensured = True

    def insert_once(self, *, execution_id, trend_date, receipt_json, receipt_sha256) -> None:
        self._ensure()
        self._query(
            f"INSERT INTO {self._table} "
            "(execution_id, trend_date, receipt_json, receipt_sha256, recorded_at) "
            "SELECT @execution_id, @trend_date, @receipt_json, @receipt_sha256, "
            "CURRENT_TIMESTAMP() FROM UNNEST([1]) WHERE NOT EXISTS "
            f"(SELECT 1 FROM {self._table} WHERE execution_id = @execution_id)",
            (
                ("execution_id", execution_id),
                ("trend_date", trend_date),
                ("receipt_json", receipt_json),
                ("receipt_sha256", receipt_sha256),
            ),
        )

    def _select(self, column: str, value: str) -> list[dict]:
        if self._create:
            self._ensure()
        return self._read(
            "SELECT execution_id, trend_date, receipt_json, receipt_sha256 "
            f"FROM {self._table} WHERE {column} = @{column} ORDER BY recorded_at, execution_id",
            ((column, value),),
        )

    def by_execution(self, execution_id: str) -> list[dict]:
        return self._select("execution_id", execution_id)

    def by_day(self, trend_date: str) -> list[dict]:
        return self._select("trend_date", trend_date)


class _DeferredClient:
    """A BigQuery client of the attached identity, constructed at its first query."""

    def __init__(self) -> None:
        self._client = None

    def query(self, sql, **kwargs):
        if self._client is None:
            from google.cloud import bigquery

            self._client = bigquery.Client(project=PROJECT)
        return self._client.query(sql, **kwargs)


def native_receipt_table(*, create: bool = False) -> BigQueryReceiptTable:
    """The receipt table over the attached identity; no client exists until the first query.

    The orchestration side reads with the default and never issues a create; the ingest
    side passes ``create=True``.
    """
    return BigQueryReceiptTable(_DeferredClient(), create=create)


def native_collection_ledger() -> WarehouseCollectionLedger:
    """The ingest entry point's ledger; no client exists until the producer first reads it."""
    return WarehouseCollectionLedger(native_receipt_table(create=True))


__all__ = [
    "INGEST_EXECUTION",
    "READ_TIMEOUT_SECONDS",
    "RECEIPT_TABLE",
    "BigQueryReceiptTable",
    "ReceiptNotYetReadable",
    "WarehouseCollectionLedger",
    "native_collection_ledger",
    "native_receipt_table",
    "read_receipt_row",
    "receipt_row",
]
