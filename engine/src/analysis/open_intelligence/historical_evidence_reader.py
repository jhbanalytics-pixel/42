"""Fixed staging reader for authoritative historical evidence projections."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from datetime import UTC, date, datetime
from typing import Literal

import google.auth
from google.cloud import bigquery

from src.analysis.open_intelligence.historical_provenance import (
    EvidenceReceiptRef,
    HistoricalBridgeError,
    HistoricalEvidenceProjectionSnapshot,
    _runtime_capability,
    build_projection_snapshot,
    canonical_json_bytes,
    scope_digest,
    validate_projection_snapshot,
)

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
VIEW = "v_signal_evidence_v2"
LOCATION = "US"
READER_IDENTITY = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
CONTRACT_VERSION = "2.1.0"
READER_VERSION = "historical_evidence_projection_reader_v1"
VIEW_SCHEMA_DIGEST = "pse_b41555fd47cb2447afa5e7fd85c587814f719e9c44da91a28faea7869f30587c"
TABLE_ID = f"{PROJECT}.{DATASET}.{VIEW}"
ALLOWED_MARKETS = ("za", "ng", "ke")

VIEW_SCHEMA = (
    {"name": "contract_version", "type": "STRING", "mode": "REQUIRED"},
    {"name": "run_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "client_scope_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "market_scope", "type": "STRING", "mode": "REPEATED"},
    {"name": "brand_config_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "audience_lens_ids", "type": "STRING", "mode": "REPEATED"},
    {"name": "theme_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "signal_date", "type": "DATE", "mode": "REQUIRED"},
    {"name": "market", "type": "STRING", "mode": "REQUIRED"},
    {"name": "signal_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "evidence_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "row_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "source_family", "type": "STRING", "mode": "REQUIRED"},
    {"name": "platform", "type": "STRING", "mode": "REQUIRED"},
    {"name": "source_label", "type": "STRING", "mode": "NULLABLE"},
    {"name": "author_label", "type": "STRING", "mode": "NULLABLE"},
    {"name": "excerpt", "type": "STRING", "mode": "NULLABLE"},
    {"name": "metric_label", "type": "STRING", "mode": "NULLABLE"},
    {"name": "url", "type": "STRING", "mode": "NULLABLE"},
    {"name": "published_at", "type": "TIMESTAMP", "mode": "NULLABLE"},
    {"name": "claim_role", "type": "STRING", "mode": "REQUIRED"},
    {"name": "direction", "type": "STRING", "mode": "REQUIRED"},
    {"name": "geo_confidence", "type": "FLOAT64", "mode": "REQUIRED"},
    {"name": "evidence_state", "type": "STRING", "mode": "REQUIRED"},
    {"name": "availability", "type": "STRING", "mode": "REQUIRED"},
    {"name": "created_at", "type": "TIMESTAMP", "mode": "REQUIRED"},
)

_ROW_FIELDS = tuple(item["name"] for item in VIEW_SCHEMA)
_NATURAL_KEY_FIELDS = (
    "client_scope_id",
    "signal_date",
    "market",
    "signal_id",
    "evidence_id",
    "run_id",
)
_EVIDENCE_ID = re.compile(r"ev_[0-9a-f]{64}\Z")
_SIGNAL_ID = re.compile(r"sig_[0-9a-f]{64}\Z")
_SOURCE_FAMILY = re.compile(r"[a-z][a-z0-9_]*\Z")
_CLAIM_ROLES = frozenset({"identity", "direction", "context", "contradiction", "geo"})
_DIRECTIONS = frozenset({"rising", "stable", "declining", "conflicting", "not_applicable"})
_EVIDENCE_STATES = frozenset({"ready", "thin", "contradictory", "unchecked"})
_AVAILABILITY = frozenset({"available", "aged_out", "unavailable"})
_RUNTIME_CAPABILITY = object()


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise HistoricalBridgeError("historical_evidence_reader_semantics_invalid", field)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PersistedSignalEvidenceRow:
    contract_version: str
    run_id: str
    client_scope_id: str
    market_scope: tuple[str, ...]
    brand_config_id: str
    audience_lens_ids: tuple[str, ...]
    theme_id: str
    signal_date: date
    market: str
    signal_id: str
    evidence_id: str
    row_id: str
    source_family: str
    platform: str
    source_label: str | None
    author_label: str | None
    excerpt: str | None
    metric_label: str | None
    url: str | None
    published_at: datetime | None
    claim_role: Literal["identity", "direction", "context", "contradiction", "geo"]
    direction: Literal["rising", "stable", "declining", "conflicting", "not_applicable"]
    geo_confidence: float
    evidence_state: Literal["ready", "thin", "contradictory", "unchecked"]
    availability: Literal["available", "aged_out", "unavailable"]
    created_at: datetime

    def __post_init__(self) -> None:
        for field in ("market_scope", "audience_lens_ids"):
            value = getattr(self, field)
            if not isinstance(value, (tuple, list)) or any(
                not isinstance(item, str) for item in value
            ):
                raise HistoricalBridgeError("historical_evidence_reader_semantics_invalid", field)
            object.__setattr__(self, field, tuple(value))
        if isinstance(self.signal_date, datetime) or not isinstance(self.signal_date, date):
            raise HistoricalBridgeError(
                "historical_evidence_reader_semantics_invalid", "signal date"
            )
        required_text = (
            "contract_version",
            "run_id",
            "client_scope_id",
            "brand_config_id",
            "theme_id",
            "market",
            "signal_id",
            "evidence_id",
            "row_id",
            "source_family",
            "platform",
        )
        if any(
            not isinstance(getattr(self, field), str) or not getattr(self, field)
            for field in required_text
        ):
            raise HistoricalBridgeError(
                "historical_evidence_reader_semantics_invalid", "required text"
            )
        if (
            _EVIDENCE_ID.fullmatch(self.evidence_id) is None
            or _SIGNAL_ID.fullmatch(self.signal_id) is None
        ):
            raise HistoricalBridgeError("historical_evidence_reader_semantics_invalid", "identity")
        if (
            _SOURCE_FAMILY.fullmatch(self.source_family) is None
            or self.claim_role not in _CLAIM_ROLES
        ):
            raise HistoricalBridgeError("historical_evidence_reader_semantics_invalid", "semantics")
        if self.direction not in _DIRECTIONS or self.evidence_state not in _EVIDENCE_STATES:
            raise HistoricalBridgeError("historical_evidence_reader_semantics_invalid", "semantics")
        if self.availability not in _AVAILABILITY:
            raise HistoricalBridgeError(
                "historical_evidence_reader_semantics_invalid", "availability"
            )
        if (
            isinstance(self.geo_confidence, bool)
            or not isinstance(self.geo_confidence, (int, float))
            or not math.isfinite(float(self.geo_confidence))
            or not 0 <= float(self.geo_confidence) <= 1
        ):
            raise HistoricalBridgeError(
                "historical_evidence_reader_semantics_invalid", "geo confidence"
            )
        object.__setattr__(self, "geo_confidence", float(self.geo_confidence))
        if self.published_at is not None:
            object.__setattr__(self, "published_at", _utc(self.published_at, "published at"))
        object.__setattr__(self, "created_at", _utc(self.created_at, "created at"))


@dataclass(frozen=True, slots=True)
class ProjectionReadReceipt:
    projection_read_receipt_id: str
    reader_version: Literal["historical_evidence_projection_reader_v1"]
    reader_mode: Literal["runtime_view", "fixture"]
    source_project: str | None
    source_dataset: str | None
    source_view: str | None
    source_location: str | None
    reader_identity: str | None
    fixture_id: str | None
    fixture_digest: str | None
    view_schema_digest: str
    scope_digest: str
    run_id: str
    contract_version: Literal["2.1.0"]
    markets: tuple[str, ...]
    requested_evidence_ids: tuple[str, ...]
    row_natural_key_digests: tuple[str, ...]
    row_count: int
    as_of: datetime
    read_at: datetime
    source_projection_digest: str


def natural_key_digest(row: PersistedSignalEvidenceRow) -> str:
    return (
        "sen_"
        + hashlib.sha256(
            canonical_json_bytes({field: getattr(row, field) for field in _NATURAL_KEY_FIELDS})
        ).hexdigest()
    )


def source_projection_digest(rows: Iterable[PersistedSignalEvidenceRow]) -> str:
    ordered = tuple(
        sorted(rows, key=lambda row: tuple(getattr(row, field) for field in _NATURAL_KEY_FIELDS))
    )
    arrays = [[getattr(item, field.name) for field in fields(item)] for item in ordered]
    return "hsp_" + hashlib.sha256(canonical_json_bytes(arrays)).hexdigest()


def _read_receipt_id(values: Mapping[str, object]) -> str:
    return "hrr_" + hashlib.sha256(canonical_json_bytes(values)).hexdigest()


class RuntimeHistoricalEvidenceProjectionRead:
    __slots__ = ("_capability", "_provenance_capability", "receipt", "snapshot")

    def __new__(cls):
        raise HistoricalBridgeError(
            "historical_projection_capability_invalid", "runtime capability"
        )

    def _validated_snapshot(self) -> HistoricalEvidenceProjectionSnapshot:
        return validate_runtime_projection_read(self)[1]


def _issue_runtime_projection(
    receipt: ProjectionReadReceipt,
    snapshot: HistoricalEvidenceProjectionSnapshot,
) -> RuntimeHistoricalEvidenceProjectionRead:
    value = object.__new__(RuntimeHistoricalEvidenceProjectionRead)
    value._capability = _RUNTIME_CAPABILITY
    value._provenance_capability = _runtime_capability()
    value.receipt = receipt
    value.snapshot = snapshot
    return value


def _validate_read_receipt(receipt: ProjectionReadReceipt) -> None:
    values = asdict(receipt)
    identifier = values.pop("projection_read_receipt_id")
    if identifier != _read_receipt_id(values):
        raise HistoricalBridgeError(
            "historical_projection_read_receipt_digest_mismatch", "read receipt"
        )


def validate_projection_read_receipt(receipt: ProjectionReadReceipt) -> None:
    if not isinstance(receipt, ProjectionReadReceipt):
        raise HistoricalBridgeError(
            "historical_projection_read_receipt_digest_mismatch", "read receipt"
        )
    _validate_read_receipt(receipt)
    if (
        receipt.reader_mode != "runtime_view"
        or receipt.reader_version != READER_VERSION
        or receipt.source_project != PROJECT
        or receipt.source_dataset != DATASET
        or receipt.source_view != VIEW
        or receipt.source_location != LOCATION
        or receipt.reader_identity != READER_IDENTITY
        or receipt.fixture_id is not None
        or receipt.fixture_digest is not None
        or receipt.view_schema_digest != VIEW_SCHEMA_DIGEST
    ):
        raise HistoricalBridgeError("historical_evidence_reader_target_invalid", "runtime target")


def validate_runtime_projection_read(
    value: object,
) -> tuple[ProjectionReadReceipt, HistoricalEvidenceProjectionSnapshot]:
    if type(value).__module__.endswith("historical_evidence_fixture_reader"):
        raise HistoricalBridgeError(
            "historical_fixture_projection_runtime_ineligible", "fixture projection"
        )
    if (
        type(value) is not RuntimeHistoricalEvidenceProjectionRead
        or getattr(value, "_capability", None) is not _RUNTIME_CAPABILITY
    ):
        raise HistoricalBridgeError(
            "historical_projection_capability_invalid", "runtime projection"
        )
    receipt, snapshot = value.receipt, value.snapshot
    validate_projection_read_receipt(receipt)
    validate_projection_snapshot(snapshot)
    if (
        snapshot.projection_read_receipt_id != receipt.projection_read_receipt_id
        or snapshot.source_projection_digest != receipt.source_projection_digest
    ):
        raise HistoricalBridgeError(
            "historical_evidence_projection_digest_mismatch", "projection linkage"
        )
    return receipt, snapshot


def _create_runtime_client() -> bigquery.Client:
    credentials, adc_project = google.auth.default(
        scopes=("https://www.googleapis.com/auth/bigquery.readonly",)
    )
    identity = getattr(credentials, "service_account_email", None)
    if adc_project != PROJECT or identity != READER_IDENTITY:
        raise HistoricalBridgeError("historical_evidence_reader_target_invalid", "reader identity")
    return bigquery.Client(project=PROJECT, credentials=credentials, location=LOCATION)


def _validate_client(client: object) -> None:
    project = getattr(client, "project", None)
    location = getattr(client, "location", None)
    identity = getattr(getattr(client, "credentials", None), "service_account_email", None)
    if DATASET == "trends_v2" or not DATASET.endswith("_staging"):
        raise HistoricalBridgeError(
            "historical_evidence_reader_production_forbidden", "production source"
        )
    if project != PROJECT or location != LOCATION or identity != READER_IDENTITY:
        raise HistoricalBridgeError("historical_evidence_reader_target_invalid", "reader target")


def _schema_digest(schema: object) -> str:
    fields_value = []
    for item in schema:
        fields_value.append(
            {
                "name": getattr(item, "name", None),
                "type": getattr(item, "field_type", getattr(item, "type", None)),
                "mode": getattr(item, "mode", None),
            }
        )
    return "pse_" + hashlib.sha256(canonical_json_bytes(fields_value)).hexdigest()


def _query_sql(expected_count: int) -> str:
    selected = ",\n  ".join(_ROW_FIELDS)
    return f"""SELECT
  {selected}
FROM `{TABLE_ID}`
WHERE client_scope_id = @client_scope_id
  AND run_id = @run_id
  AND contract_version = @contract_version
  AND market = @market
  AND evidence_id IN UNNEST(@evidence_ids)
  AND published_at IS NOT NULL
  AND published_at <= @as_of
  AND created_at <= @as_of
  AND signal_date <= DATE(@as_of)
LIMIT {expected_count + 1}"""


def _frame_scope(frame: object) -> tuple[object, ...]:
    return (
        frame.client_scope_id,
        tuple(frame.market_scope),
        frame.brand_config_id,
        tuple(frame.audience_lens_ids),
        frame.theme_id,
        frame.run_id,
        frame.contract_version,
    )


def _row_from_mapping(value: object) -> PersistedSignalEvidenceRow:
    try:
        mapped = dict(value)
    except Exception as error:
        raise HistoricalBridgeError(
            "historical_evidence_reader_semantics_invalid", "row"
        ) from error
    if tuple(mapped) != _ROW_FIELDS or set(mapped) != set(_ROW_FIELDS):
        raise HistoricalBridgeError("historical_evidence_reader_schema_mismatch", "row fields")
    return PersistedSignalEvidenceRow(**mapped)


def _expected_for_analogue(
    current: object, candidates: Iterable[object]
) -> dict[str, tuple[str, str, datetime, str]]:
    expected = {}
    for snapshot in (current, *tuple(candidates)):
        for receipt in snapshot.receipts:
            expected[receipt.receipt_id] = (
                receipt.signal_id,
                receipt.source_family,
                snapshot.as_of,
                receipt.market,
            )
    return expected


def _expected_for_recurrence(
    occurrences: Iterable[object], pattern_id: str, market: str
) -> dict[str, tuple[str | None, str | None, date, str]]:
    expected = {}
    for item in occurrences:
        if item.pattern_id == pattern_id and item.market == market:
            for receipt_id in item.receipt_ids:
                expected[receipt_id] = (None, None, item.occurred_on, market)
    return expected


def _expected_for_diffusion(
    sequences: Iterable[object], current: object, targets: Iterable[str]
) -> dict[str, tuple[str | None, str | None, date, str]]:
    target_set = set(targets)
    expected = {}
    for item in sequences:
        if item.origin_market == current.origin_market and item.target_market in target_set:
            for receipt_id in item.receipt_ids:
                expected[receipt_id] = (None, None, item.target_date, item.target_market)
    return expected


def _assert_static_target() -> None:
    if (
        PROJECT != "ogilvy-trends-v2"
        or DATASET != "trends_v2_staging"
        or VIEW != "v_signal_evidence_v2"
    ):
        code = (
            "historical_evidence_reader_production_forbidden"
            if DATASET == "trends_v2" or not DATASET.endswith("_staging")
            else "historical_evidence_reader_target_invalid"
        )
        raise HistoricalBridgeError(code, "static reader target")


def _read(
    frame: object,
    expected: Mapping[str, tuple[str | None, str | None, date | datetime, str]],
) -> RuntimeHistoricalEvidenceProjectionRead:
    if frame.contract_version != CONTRACT_VERSION or frame.output_mode != "internal_working_paper":
        raise HistoricalBridgeError("historical_evidence_reader_scope_mismatch", "frame")
    requested = tuple(sorted(expected))
    if not requested:
        raise HistoricalBridgeError("historical_evidence_reader_incomplete", "requested evidence")
    if any(_EVIDENCE_ID.fullmatch(item) is None for item in requested):
        raise HistoricalBridgeError(
            "historical_evidence_reader_semantics_invalid", "requested evidence"
        )
    _assert_static_target()
    client = _create_runtime_client()
    _validate_client(client)
    all_rows = []
    for market in sorted({item[3] for item in expected.values()}):
        ids = tuple(item for item in requested if expected[item][3] == market)
        config = bigquery.QueryJobConfig(
            use_legacy_sql=False,
            query_parameters=(
                bigquery.ScalarQueryParameter("client_scope_id", "STRING", frame.client_scope_id),
                bigquery.ScalarQueryParameter("run_id", "STRING", frame.run_id),
                bigquery.ScalarQueryParameter("contract_version", "STRING", CONTRACT_VERSION),
                bigquery.ScalarQueryParameter("market", "STRING", market),
                bigquery.ArrayQueryParameter("evidence_ids", "STRING", ids),
                bigquery.ScalarQueryParameter("as_of", "TIMESTAMP", frame.as_of),
            ),
        )
        job = client.query(_query_sql(len(ids)), job_config=config, location=LOCATION)
        if _schema_digest(job.schema) != VIEW_SCHEMA_DIGEST:
            raise HistoricalBridgeError("historical_evidence_reader_schema_mismatch", "view schema")
        result = tuple(job.result(max_results=len(ids) + 1))
        if len(result) > len(ids):
            raise HistoricalBridgeError("historical_evidence_reader_limit_exceeded", "row count")
        all_rows.extend(result)
    if len(all_rows) < len(requested):
        raise HistoricalBridgeError("historical_evidence_reader_incomplete", "row count")
    if len(all_rows) > len(requested):
        raise HistoricalBridgeError("historical_evidence_reader_limit_exceeded", "row count")
    rows = tuple(_row_from_mapping(item) for item in all_rows)
    if len({natural_key_digest(item) for item in rows}) != len(rows) or len(
        {item.evidence_id for item in rows}
    ) != len(rows):
        raise HistoricalBridgeError("historical_evidence_reader_duplicate", "rows")
    if {item.evidence_id for item in rows} != set(requested):
        missing = set(requested) - {item.evidence_id for item in rows}
        code = (
            "historical_evidence_reader_incomplete"
            if missing
            else "historical_evidence_projection_extra"
        )
        raise HistoricalBridgeError(code, "evidence set")
    frame_scope = _frame_scope(frame)
    for item in rows:
        expected_signal, expected_family, cutoff, expected_market = expected[item.evidence_id]
        if (
            _frame_scope(item) != frame_scope
            or item.market != expected_market
            or item.market not in frame.market_scope
        ):
            raise HistoricalBridgeError("historical_evidence_reader_scope_mismatch", "row scope")
        if (
            item.published_at is None
            or item.published_at > frame.as_of
            or item.created_at > frame.as_of
            or item.signal_date > frame.as_of.date()
        ):
            raise HistoricalBridgeError("historical_evidence_reader_future_leak", "row time")
        if expected_signal is not None and item.signal_id != expected_signal:
            raise HistoricalBridgeError("historical_evidence_reader_semantics_invalid", "signal")
        if expected_family is not None and item.source_family != expected_family:
            raise HistoricalBridgeError(
                "historical_evidence_reader_semantics_invalid", "source family"
            )
        if isinstance(cutoff, datetime):
            after_source_cutoff = item.published_at > cutoff or item.created_at > cutoff
        else:
            after_source_cutoff = (
                item.published_at.date() > cutoff or item.created_at.date() > cutoff
            )
        if after_source_cutoff:
            raise HistoricalBridgeError("historical_evidence_reader_future_leak", "source cutoff")
    ordered = tuple(
        sorted(rows, key=lambda row: tuple(getattr(row, field) for field in _NATURAL_KEY_FIELDS))
    )
    projection_digest = source_projection_digest(ordered)
    refs = tuple(
        EvidenceReceiptRef(
            client_scope_id=item.client_scope_id,
            market_scope=item.market_scope,
            brand_config_id=item.brand_config_id,
            audience_lens_ids=item.audience_lens_ids,
            theme_id=item.theme_id,
            run_id=item.run_id,
            contract_version=item.contract_version,
            evidence_id=item.evidence_id,
            signal_id=item.signal_id,
            signal_date=item.signal_date,
            market=item.market,
            source_family=item.source_family,
            published_at=item.published_at,
            claim_role=item.claim_role,
            direction=item.direction,
            geo_confidence=item.geo_confidence,
            availability=item.availability,
            evidence_state=item.evidence_state,
        )
        for item in ordered
    )
    scope_value = scope_digest(frame)
    receipt_values = {
        "reader_version": READER_VERSION,
        "reader_mode": "runtime_view",
        "source_project": PROJECT,
        "source_dataset": DATASET,
        "source_view": VIEW,
        "source_location": LOCATION,
        "reader_identity": READER_IDENTITY,
        "fixture_id": None,
        "fixture_digest": None,
        "view_schema_digest": VIEW_SCHEMA_DIGEST,
        "scope_digest": scope_value,
        "run_id": frame.run_id,
        "contract_version": CONTRACT_VERSION,
        "markets": tuple(sorted({item.market for item in ordered})),
        "requested_evidence_ids": requested,
        "row_natural_key_digests": tuple(natural_key_digest(item) for item in ordered),
        "row_count": len(ordered),
        "as_of": frame.as_of,
        "read_at": _utc_now(),
        "source_projection_digest": projection_digest,
    }
    receipt = ProjectionReadReceipt(
        projection_read_receipt_id=_read_receipt_id(receipt_values), **receipt_values
    )
    snapshot = build_projection_snapshot(
        projection_read_receipt_id=receipt.projection_read_receipt_id,
        scope_digest_value=scope_value,
        evidence_refs=refs,
        source_projection_digest=projection_digest,
    )
    return _issue_runtime_projection(receipt, snapshot)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def read_runtime_analogue_evidence_projection(
    *, frame: object, current: object, candidates: Iterable[object]
) -> RuntimeHistoricalEvidenceProjectionRead:
    candidate_items = tuple(candidates)
    return _read(frame, _expected_for_analogue(current, candidate_items))


def read_runtime_recurrence_evidence_projection(
    *, frame: object, occurrences: Iterable[object], pattern_id: str, market: str
) -> RuntimeHistoricalEvidenceProjectionRead:
    items = tuple(occurrences)
    return _read(frame, _expected_for_recurrence(items, pattern_id, market))


def read_runtime_diffusion_evidence_projection(
    *,
    frame: object,
    current: object,
    historical_sequences: Iterable[object],
    target_markets: Iterable[str],
) -> RuntimeHistoricalEvidenceProjectionRead:
    sequences = tuple(historical_sequences)
    targets = tuple(target_markets)
    return _read(frame, _expected_for_diffusion(sequences, current, targets))


__all__ = [
    "CONTRACT_VERSION",
    "DATASET",
    "LOCATION",
    "PROJECT",
    "READER_IDENTITY",
    "READER_VERSION",
    "TABLE_ID",
    "VIEW",
    "VIEW_SCHEMA",
    "VIEW_SCHEMA_DIGEST",
    "PersistedSignalEvidenceRow",
    "ProjectionReadReceipt",
    "RuntimeHistoricalEvidenceProjectionRead",
    "natural_key_digest",
    "read_runtime_analogue_evidence_projection",
    "read_runtime_diffusion_evidence_projection",
    "read_runtime_recurrence_evidence_projection",
    "source_projection_digest",
    "validate_projection_read_receipt",
    "validate_runtime_projection_read",
]
