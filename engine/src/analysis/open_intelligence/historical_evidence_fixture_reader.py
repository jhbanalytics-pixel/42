"""Fixture-only historical evidence projection reader."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

from src.analysis.open_intelligence.historical_evidence_reader import (
    CONTRACT_VERSION,
    READER_VERSION,
    VIEW_SCHEMA_DIGEST,
    PersistedSignalEvidenceRow,
    ProjectionReadReceipt,
    natural_key_digest,
    source_projection_digest,
)
from src.analysis.open_intelligence.historical_provenance import (
    EvidenceReceiptRef,
    HistoricalBridgeError,
    HistoricalEvidenceProjectionSnapshot,
    build_projection_snapshot,
    canonical_json_bytes,
    scope_digest,
)

_FIXTURE_ID = "historical_evidence_provenance_v2"
_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "tests/fixtures/open_intelligence/historical_evidence_provenance_v2.json"
)
_FIXTURE_CAPABILITY = object()


class FixtureHistoricalEvidenceProjectionRead:
    __slots__ = ("_capability", "receipt", "snapshot")

    def __new__(cls):
        raise HistoricalBridgeError(
            "historical_projection_capability_invalid", "fixture capability"
        )


def _issue_fixture_projection(
    receipt: ProjectionReadReceipt,
    snapshot: HistoricalEvidenceProjectionSnapshot,
) -> FixtureHistoricalEvidenceProjectionRead:
    value = object.__new__(FixtureHistoricalEvidenceProjectionRead)
    value._capability = _FIXTURE_CAPABILITY
    value.receipt = receipt
    value.snapshot = snapshot
    return value


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise HistoricalBridgeError(
            "historical_evidence_reader_semantics_invalid", "fixture datetime"
        )
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _parse_row(value: dict[str, object]) -> PersistedSignalEvidenceRow:
    parsed = dict(value)
    parsed["market_scope"] = tuple(parsed["market_scope"])
    parsed["audience_lens_ids"] = tuple(parsed["audience_lens_ids"])
    parsed["signal_date"] = date.fromisoformat(parsed["signal_date"])
    parsed["published_at"] = _parse_datetime(parsed["published_at"])
    parsed["created_at"] = _parse_datetime(parsed["created_at"])
    return PersistedSignalEvidenceRow(**parsed)


def read_fixture_historical_evidence_projection(
    *,
    frame: object,
    fixture_id: str,
    expected_fixture_digest: str,
) -> FixtureHistoricalEvidenceProjectionRead:
    raw = _FIXTURE_PATH.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if fixture_id != _FIXTURE_ID or expected_fixture_digest != digest:
        raise HistoricalBridgeError(
            "historical_evidence_projection_digest_mismatch", "fixture digest"
        )
    payload = json.loads(raw.decode("utf-8"))
    if (
        payload.get("fixture_version") != "historical_evidence_provenance_fixture_v2"
        or payload.get("contract_version") != CONTRACT_VERSION
    ):
        raise HistoricalBridgeError("historical_evidence_reader_scope_mismatch", "fixture contract")
    rows = tuple(
        sorted((_parse_row(item) for item in payload["persisted_rows"]), key=natural_key_digest)
    )
    if any(scope_digest(item) != scope_digest(frame) for item in rows):
        raise HistoricalBridgeError("historical_evidence_reader_scope_mismatch", "fixture scope")
    projection_digest = source_projection_digest(rows)
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
        for item in rows
    )
    values = {
        "reader_version": READER_VERSION,
        "reader_mode": "fixture",
        "source_project": None,
        "source_dataset": None,
        "source_view": None,
        "source_location": None,
        "reader_identity": None,
        "fixture_id": fixture_id,
        "fixture_digest": digest,
        "view_schema_digest": VIEW_SCHEMA_DIGEST,
        "scope_digest": scope_digest(frame),
        "run_id": frame.run_id,
        "contract_version": CONTRACT_VERSION,
        "markets": tuple(sorted({item.market for item in rows})),
        "requested_evidence_ids": tuple(sorted(item.evidence_id for item in rows)),
        "row_natural_key_digests": tuple(natural_key_digest(item) for item in rows),
        "row_count": len(rows),
        "as_of": frame.as_of,
        "read_at": frame.as_of,
        "source_projection_digest": projection_digest,
    }
    identifier = "hrr_" + hashlib.sha256(canonical_json_bytes(values)).hexdigest()
    receipt = ProjectionReadReceipt(projection_read_receipt_id=identifier, **values)
    snapshot = build_projection_snapshot(
        projection_read_receipt_id=identifier,
        scope_digest_value=scope_digest(frame),
        evidence_refs=refs,
        source_projection_digest=projection_digest,
    )
    return _issue_fixture_projection(receipt, snapshot)


__all__ = ["FixtureHistoricalEvidenceProjectionRead", "read_fixture_historical_evidence_projection"]
