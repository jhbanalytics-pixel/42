"""Canonical provenance values for internal historical evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Literal

_EVIDENCE_ID = re.compile(r"ev_[0-9a-f]{64}\Z")
_SIGNAL_ID = re.compile(r"sig_[0-9a-f]{64}\Z")
_SOURCE_FAMILY = re.compile(r"[a-z][a-z0-9_]*\Z")
_BINDING_VERSION = "historical_evidence_provenance_binding_v1"
PROJECTION_OWNER_VERSION = "historical_evidence_projection_v1"
EMPTY_BINDINGS_DIGEST = "hbd_4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
_RUNTIME_PROJECTION_CAPABILITY = object()


class HistoricalBridgeError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        source_object_ids: Iterable[str] = (),
        evidence_ids: Iterable[str] = (),
        reasons: Iterable[str] = (),
    ):
        super().__init__(message)
        self.code = code
        self.source_object_ids = tuple(sorted(source_object_ids))
        self.evidence_ids = tuple(sorted(evidence_ids))
        self.reasons = tuple(sorted(reasons))


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise HistoricalBridgeError("historical_provenance_incomplete", f"{field} is invalid")
    return value.astimezone(UTC)


def _canonical_value(value: object) -> object:
    if isinstance(value, datetime):
        return _utc(value, "datetime").strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, float) and not math.isfinite(value):
        raise HistoricalBridgeError("historical_provenance_incomplete", "number is not finite")
    return value


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")


def _digest(prefix: str, value: object) -> str:
    return prefix + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class EvidenceReceiptRef:
    client_scope_id: str
    market_scope: tuple[str, ...]
    brand_config_id: str
    audience_lens_ids: tuple[str, ...]
    theme_id: str
    run_id: str
    contract_version: str
    evidence_id: str
    signal_id: str
    signal_date: date
    market: str
    source_family: str
    published_at: datetime
    claim_role: Literal["identity", "direction", "context", "contradiction", "geo"]
    direction: Literal["rising", "stable", "declining", "conflicting", "not_applicable"]
    geo_confidence: float
    availability: Literal["available", "aged_out", "unavailable"]
    evidence_state: Literal["ready", "thin", "contradictory", "unchecked"]

    def __post_init__(self) -> None:
        if _EVIDENCE_ID.fullmatch(self.evidence_id) is None:
            raise HistoricalBridgeError(
                "historical_evidence_reader_semantics_invalid", "evidence id"
            )
        if _SIGNAL_ID.fullmatch(self.signal_id) is None:
            raise HistoricalBridgeError("historical_evidence_reader_semantics_invalid", "signal id")
        if _SOURCE_FAMILY.fullmatch(self.source_family) is None:
            raise HistoricalBridgeError(
                "historical_evidence_reader_semantics_invalid", "source family"
            )
        if not 0 <= self.geo_confidence <= 1 or not math.isfinite(self.geo_confidence):
            raise HistoricalBridgeError(
                "historical_evidence_reader_semantics_invalid", "geo confidence"
            )
        object.__setattr__(self, "published_at", _utc(self.published_at, "published at"))


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceProjectionSnapshot:
    projection_receipt_id: str
    projection_owner_version: Literal["historical_evidence_projection_v1"]
    projection_read_receipt_id: str
    scope_digest: str
    evidence_refs: tuple[EvidenceReceiptRef, ...]
    evidence_refs_digest: str
    source_projection_digest: str


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceProvenanceBinding:
    binding_id: str
    binding_version: Literal["historical_evidence_provenance_binding_v1"]
    source_kind: Literal["recurrence", "diffusion"]
    source_object_id: str
    source_object_digest: str
    receipt_id: str
    expected_signal_id: str
    expected_source_family: str
    historical_cutoff: date
    rule_version: str


def scope_digest(value: object) -> str:
    fields = (
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "run_id",
        "contract_version",
    )
    return _digest("scp_", {field: getattr(value, field) for field in fields})


def evidence_refs_digest(refs: Iterable[EvidenceReceiptRef]) -> str:
    items = tuple(sorted(refs, key=lambda item: item.evidence_id))
    if len({item.evidence_id for item in items}) != len(items):
        raise HistoricalBridgeError("historical_evidence_reader_duplicate", "evidence references")
    return _digest("evr_", [asdict(item) for item in items])


def projection_receipt_id(
    *,
    projection_read_receipt_id: str,
    scope_digest_value: str,
    evidence_refs: tuple[EvidenceReceiptRef, ...],
    evidence_refs_digest_value: str,
    source_projection_digest: str,
) -> str:
    return _digest(
        "hpr_",
        {
            "projection_owner_version": PROJECTION_OWNER_VERSION,
            "projection_read_receipt_id": projection_read_receipt_id,
            "scope_digest": scope_digest_value,
            "evidence_refs": [
                asdict(item) for item in sorted(evidence_refs, key=lambda item: item.evidence_id)
            ],
            "evidence_refs_digest": evidence_refs_digest_value,
            "source_projection_digest": source_projection_digest,
        },
    )


def build_projection_snapshot(
    *,
    projection_read_receipt_id: str,
    scope_digest_value: str,
    evidence_refs: Iterable[EvidenceReceiptRef],
    source_projection_digest: str,
) -> HistoricalEvidenceProjectionSnapshot:
    refs = tuple(sorted(evidence_refs, key=lambda item: item.evidence_id))
    refs_digest = evidence_refs_digest(refs)
    receipt_id = projection_receipt_id(
        projection_read_receipt_id=projection_read_receipt_id,
        scope_digest_value=scope_digest_value,
        evidence_refs=refs,
        evidence_refs_digest_value=refs_digest,
        source_projection_digest=source_projection_digest,
    )
    return HistoricalEvidenceProjectionSnapshot(
        projection_receipt_id=receipt_id,
        projection_owner_version=PROJECTION_OWNER_VERSION,
        projection_read_receipt_id=projection_read_receipt_id,
        scope_digest=scope_digest_value,
        evidence_refs=refs,
        evidence_refs_digest=refs_digest,
        source_projection_digest=source_projection_digest,
    )


def validate_projection_snapshot(snapshot: HistoricalEvidenceProjectionSnapshot) -> None:
    if not isinstance(snapshot, HistoricalEvidenceProjectionSnapshot):
        raise HistoricalBridgeError("historical_evidence_projection_digest_mismatch", "snapshot")
    expected_refs = evidence_refs_digest(snapshot.evidence_refs)
    expected_id = projection_receipt_id(
        projection_read_receipt_id=snapshot.projection_read_receipt_id,
        scope_digest_value=snapshot.scope_digest,
        evidence_refs=snapshot.evidence_refs,
        evidence_refs_digest_value=expected_refs,
        source_projection_digest=snapshot.source_projection_digest,
    )
    if (
        snapshot.evidence_refs_digest != expected_refs
        or snapshot.projection_receipt_id != expected_id
    ):
        raise HistoricalBridgeError("historical_evidence_projection_digest_mismatch", "snapshot")


def _runtime_capability() -> object:
    return _RUNTIME_PROJECTION_CAPABILITY


def runtime_projection_snapshot(value: object) -> HistoricalEvidenceProjectionSnapshot:
    module = type(value).__module__
    if module.endswith("historical_evidence_fixture_reader"):
        raise HistoricalBridgeError(
            "historical_fixture_projection_runtime_ineligible", "fixture projection"
        )
    if (
        module != "src.analysis.open_intelligence.historical_evidence_reader"
        or type(value).__name__ != "RuntimeHistoricalEvidenceProjectionRead"
        or getattr(value, "_provenance_capability", None) is not _RUNTIME_PROJECTION_CAPABILITY
    ):
        raise HistoricalBridgeError(
            "historical_projection_capability_invalid", "runtime projection"
        )
    validator = getattr(value, "_validated_snapshot", None)
    if not callable(validator):
        raise HistoricalBridgeError(
            "historical_projection_capability_invalid", "runtime projection"
        )
    return validator()


def _source_digest(source_kind: str, source: object) -> str:
    if source_kind == "recurrence":
        values = {
            "occurrence_id": source.occurrence_id,
            "pattern_id": source.pattern_id,
            "occurred_on": source.occurred_on,
            "market": source.market,
            "receipt_ids": source.receipt_ids,
        }
    else:
        values = {
            "sequence_id": source.sequence_id,
            "origin_market": source.origin_market,
            "origin_date": source.origin_date,
            "target_market": source.target_market,
            "target_date": source.target_date,
            "receipt_ids": source.receipt_ids,
        }
    return _digest("hso_", values)


def _build_binding(
    *,
    source_kind: str,
    source: object,
    receipt_id: str,
    expected_signal_id: str,
    expected_source_family: str,
    historical_cutoff: date,
    rule_version: str,
) -> HistoricalEvidenceProvenanceBinding:
    source_object_id = source.occurrence_id if source_kind == "recurrence" else source.sequence_id
    values = {
        "binding_version": _BINDING_VERSION,
        "source_kind": source_kind,
        "source_object_id": source_object_id,
        "source_object_digest": _source_digest(source_kind, source),
        "receipt_id": receipt_id,
        "expected_signal_id": expected_signal_id,
        "expected_source_family": expected_source_family,
        "historical_cutoff": historical_cutoff,
        "rule_version": rule_version,
    }
    return HistoricalEvidenceProvenanceBinding(binding_id=_digest("hpb_", values), **values)


def build_recurrence_binding(
    *,
    occurrence: object,
    receipt_id: str,
    expected_signal_id: str,
    expected_source_family: str,
    rule_version: str,
) -> HistoricalEvidenceProvenanceBinding:
    return _build_binding(
        source_kind="recurrence",
        source=occurrence,
        receipt_id=receipt_id,
        expected_signal_id=expected_signal_id,
        expected_source_family=expected_source_family,
        historical_cutoff=occurrence.occurred_on,
        rule_version=rule_version,
    )


def build_diffusion_binding(
    *,
    sequence: object,
    receipt_id: str,
    expected_signal_id: str,
    expected_source_family: str,
    rule_version: str,
) -> HistoricalEvidenceProvenanceBinding:
    return _build_binding(
        source_kind="diffusion",
        source=sequence,
        receipt_id=receipt_id,
        expected_signal_id=expected_signal_id,
        expected_source_family=expected_source_family,
        historical_cutoff=sequence.target_date,
        rule_version=rule_version,
    )


def provenance_bindings_digest(
    bindings: Iterable[HistoricalEvidenceProvenanceBinding],
) -> str:
    items = tuple(sorted(bindings, key=binding_sort_key))
    if len({item.binding_id for item in items}) != len(items) or len(
        {item.receipt_id for item in items}
    ) != len(items):
        raise HistoricalBridgeError("historical_provenance_binding_duplicate", "bindings")
    return _digest("hbd_", [asdict(item) for item in items])


def binding_sort_key(
    binding: HistoricalEvidenceProvenanceBinding,
) -> tuple[str, str, str]:
    return binding.source_kind, binding.source_object_id, binding.receipt_id


def validate_provenance_binding(binding: HistoricalEvidenceProvenanceBinding) -> None:
    if not isinstance(binding, HistoricalEvidenceProvenanceBinding):
        raise HistoricalBridgeError("historical_provenance_binding_digest_mismatch", "binding")
    values = asdict(binding)
    identifier = values.pop("binding_id")
    if identifier != _digest("hpb_", values):
        raise HistoricalBridgeError("historical_provenance_binding_digest_mismatch", "binding")


__all__ = [
    "EMPTY_BINDINGS_DIGEST",
    "EvidenceReceiptRef",
    "HistoricalBridgeError",
    "HistoricalEvidenceProjectionSnapshot",
    "HistoricalEvidenceProvenanceBinding",
    "binding_sort_key",
    "build_diffusion_binding",
    "build_projection_snapshot",
    "build_recurrence_binding",
    "canonical_json_bytes",
    "evidence_refs_digest",
    "projection_receipt_id",
    "provenance_bindings_digest",
    "runtime_projection_snapshot",
    "scope_digest",
    "validate_projection_snapshot",
    "validate_provenance_binding",
]
