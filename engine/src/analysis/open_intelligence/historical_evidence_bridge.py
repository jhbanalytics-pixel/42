"""Internal historical computation and provenance receipt bridge."""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from datetime import UTC, date, datetime
from typing import Literal

from src.analysis.open_intelligence.historical_analogue import (
    HistoricalAnalogueRules,
    HistoricalSignalSnapshot,
    find_historical_analogues,
)
from src.analysis.open_intelligence.historical_diffusion import (
    CurrentDiffusionSignal,
    DiffusionRules,
    HistoricalDiffusionSequence,
    forecast_cross_market_diffusion_with_provenance,
)
from src.analysis.open_intelligence.historical_evidence_reader import (
    READER_VERSION,
    ProjectionReadReceipt,
    RuntimeHistoricalEvidenceProjectionRead,
    read_runtime_analogue_evidence_projection,
    validate_projection_read_receipt,
    validate_runtime_projection_read,
)
from src.analysis.open_intelligence.historical_provenance import (
    EvidenceReceiptRef,
    HistoricalBridgeError,
    HistoricalEvidenceProjectionSnapshot,
    HistoricalEvidenceProvenanceBinding,
    binding_sort_key,
    canonical_json_bytes,
    evidence_refs_digest,
    provenance_bindings_digest,
    scope_digest,
    validate_projection_snapshot,
    validate_provenance_binding,
)
from src.analysis.open_intelligence.historical_recurrence import (
    RecurrenceOccurrence,
    RecurrenceRules,
    detect_historical_recurrence_with_provenance,
)

BRIDGE_VERSION = "historical_evidence_bridge_v1"
CONTRACT_VERSION = "2.1.0"
INTERNAL_OUTPUT_MODE = "internal_working_paper"
_MARKET = re.compile(r"[a-z]{2}\Z")
_NATURAL_KEY_DIGEST = re.compile(r"sen_[0-9a-f]{64}\Z")
_BRIDGE_AUTHORITY_CAPABILITY = object()
_SCOPE_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
)


@dataclass(frozen=True, slots=True)
class HistoricalBridgeFrame:
    bridge_version: Literal["historical_evidence_bridge_v1"]
    investigation_id: str
    client_scope_id: str
    market_scope: tuple[str, ...]
    brand_config_id: str
    audience_lens_ids: tuple[str, ...]
    theme_id: str
    run_id: str
    contract_version: Literal["2.1.0"]
    output_mode: Literal["internal_working_paper"]
    as_of: datetime

    def __post_init__(self) -> None:
        if self.bridge_version != BRIDGE_VERSION:
            raise HistoricalBridgeError("historical_provenance_incomplete", "bridge version")
        if self.contract_version != CONTRACT_VERSION:
            raise HistoricalBridgeError("historical_contract_version_invalid", "contract version")
        if self.output_mode != INTERNAL_OUTPUT_MODE:
            raise HistoricalBridgeError("historical_output_mode_ineligible", "output mode")
        for field in (
            "investigation_id",
            "client_scope_id",
            "brand_config_id",
            "theme_id",
            "run_id",
        ):
            if not isinstance(getattr(self, field), str) or not getattr(self, field):
                raise HistoricalBridgeError("historical_provenance_incomplete", field)
        for field in ("market_scope", "audience_lens_ids"):
            value = getattr(self, field)
            if not isinstance(value, (tuple, list)) or len(value) != len(set(value)):
                raise HistoricalBridgeError("historical_scope_invalid", field)
            object.__setattr__(self, field, tuple(value))
        if not self.market_scope or any(
            _MARKET.fullmatch(item) is None or item not in {"za", "ng", "ke"}
            for item in self.market_scope
        ):
            raise HistoricalBridgeError("historical_scope_invalid", "market scope")
        if (
            not isinstance(self.as_of, datetime)
            or self.as_of.tzinfo is None
            or self.as_of.utcoffset() is None
        ):
            raise HistoricalBridgeError("historical_provenance_incomplete", "as of")
        object.__setattr__(self, "as_of", self.as_of.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceReceipt:
    historical_receipt_id: str
    bridge_version: Literal["historical_evidence_bridge_v1"]
    investigation_id: str
    scope_digest: str
    kind: Literal["analogue", "recurrence", "diffusion_shadow"]
    source_object_id: str
    source_object_state: str
    as_of: datetime
    source_market: str
    target_market: str
    source_families: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    evidence_refs_digest: str
    evidence_projection_receipt_id: str
    source_projection_digest: str
    provenance_binding_ids: tuple[str, ...]
    provenance_bindings_digest: str
    observed_window_start: datetime
    observed_window_end: datetime
    historical_window_start: date
    historical_window_end: date
    difference_reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    rule_version: str
    future_leak_state: Literal["passed", "rejected"]
    future_leak_reasons: tuple[str, ...]
    attachment_eligible: bool
    display_eligible: bool

    def __post_init__(self) -> None:
        values = asdict(self)
        identifier = values.pop("historical_receipt_id")
        expected = "heb_" + hashlib.sha256(canonical_json_bytes(values)).hexdigest()
        if identifier != expected:
            raise HistoricalBridgeError(
                "historical_provenance_binding_digest_mismatch", "receipt id"
            )
        if self.kind == "diffusion_shadow" and (self.attachment_eligible or self.display_eligible):
            raise HistoricalBridgeError("historical_diffusion_ineligible", "diffusion eligibility")
        if (
            self.observed_window_end < self.observed_window_start
            or self.historical_window_end < self.historical_window_start
        ):
            raise HistoricalBridgeError("historical_provenance_incomplete", "receipt window")


@dataclass(frozen=True, slots=True)
class HistoricalBridgeIssue:
    code: str
    kind: str
    source_object_id: str
    evidence_ids: tuple[str, ...]
    reasons: tuple[str, ...]


class _AuthorityBoundResult:
    __slots__ = ("_authority",)


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceBridgeResult(_AuthorityBoundResult):
    frame: HistoricalBridgeFrame
    projection_read_receipt: ProjectionReadReceipt
    evidence_projection: HistoricalEvidenceProjectionSnapshot
    bindings: tuple[HistoricalEvidenceProvenanceBinding, ...]
    receipts: tuple[HistoricalEvidenceReceipt, ...]
    issues: tuple[HistoricalBridgeIssue, ...]


@dataclass(frozen=True, slots=True)
class _HistoricalReceiptSourceAuthority:
    kind: str
    source_object_id: str
    source_object_state: str
    source_market: str
    target_market: str
    evidence_ids: tuple[str, ...]
    observed_window_start: datetime
    observed_window_end: datetime
    historical_window_start: date
    historical_window_end: date
    rule_version: str
    evidence_projection_receipt_id: str
    source_projection_digest: str
    provenance_binding_ids: tuple[str, ...]
    provenance_bindings_digest: str


@dataclass(frozen=True, slots=True)
class _HistoricalBridgeAuthority:
    capability: object
    evidence_projection: RuntimeHistoricalEvidenceProjectionRead
    frame: HistoricalBridgeFrame
    bindings: tuple[HistoricalEvidenceProvenanceBinding, ...]
    receipts: tuple[HistoricalEvidenceReceipt, ...]
    issues: tuple[HistoricalBridgeIssue, ...]
    receipt_sources: tuple[_HistoricalReceiptSourceAuthority, ...]


def analogue_rules_version(rules: HistoricalAnalogueRules) -> str:
    return "analogue_rules_" + hashlib.sha256(canonical_json_bytes(asdict(rules))).hexdigest()


def diffusion_source_object_id(current_signal_id: str, target_market: str) -> str:
    return (
        "hds_"
        + hashlib.sha256(
            canonical_json_bytes(
                {"current_signal_id": current_signal_id, "target_market": target_market}
            )
        ).hexdigest()
    )


def _scope_values(value: object) -> tuple[object, ...]:
    return tuple(
        tuple(getattr(value, field))
        if field in {"market_scope", "audience_lens_ids"}
        else getattr(value, field)
        for field in _SCOPE_FIELDS
    )


def _raise_result_error(
    result: HistoricalEvidenceBridgeResult,
    code: str,
    message: str,
    reason: str,
) -> None:
    source_object_ids = {
        item.source_object_id for item in (*result.bindings, *result.receipts, *result.issues)
    }
    authority = getattr(result, "_authority", None)
    if (
        type(authority) is _HistoricalBridgeAuthority
        and authority.capability is _BRIDGE_AUTHORITY_CAPABILITY
    ):
        source_object_ids.update(item.source_object_id for item in authority.receipt_sources)
    evidence_ids = {item.evidence_id for item in result.evidence_projection.evidence_refs}
    raise HistoricalBridgeError(
        code,
        message,
        source_object_ids=source_object_ids,
        evidence_ids=evidence_ids,
        reasons=(reason,),
    )


def _projection(
    frame: HistoricalBridgeFrame,
    value: RuntimeHistoricalEvidenceProjectionRead,
) -> tuple[
    ProjectionReadReceipt, HistoricalEvidenceProjectionSnapshot, dict[str, EvidenceReceiptRef]
]:
    receipt, snapshot = validate_runtime_projection_read(value)
    if receipt.scope_digest != scope_digest(frame) or snapshot.scope_digest != scope_digest(frame):
        raise HistoricalBridgeError("historical_evidence_reader_scope_mismatch", "projection scope")
    refs = {item.evidence_id: item for item in snapshot.evidence_refs}
    if len(refs) != len(snapshot.evidence_refs):
        raise HistoricalBridgeError("historical_evidence_reader_duplicate", "projection refs")
    return receipt, snapshot, refs


def _bindings_for_receipt(
    bindings: Iterable[HistoricalEvidenceProvenanceBinding], evidence_ids: Iterable[str]
) -> tuple[HistoricalEvidenceProvenanceBinding, ...]:
    ids = set(evidence_ids)
    selected = tuple(
        sorted((item for item in bindings if item.receipt_id in ids), key=binding_sort_key)
    )
    if {item.receipt_id for item in selected} != ids:
        raise HistoricalBridgeError(
            "historical_receipt_binding_coverage_mismatch", "receipt bindings"
        )
    return selected


def _build_receipt(
    *,
    frame: HistoricalBridgeFrame,
    projection: HistoricalEvidenceProjectionSnapshot,
    kind: str,
    source_object_id: str,
    source_object_state: str,
    source_market: str,
    target_market: str,
    refs: Iterable[EvidenceReceiptRef],
    bindings: Iterable[HistoricalEvidenceProvenanceBinding],
    historical_window_start: date,
    historical_window_end: date,
    difference_reasons: Iterable[str],
    limitations: Iterable[str],
    rule_version: str,
    attachment_eligible: bool,
) -> HistoricalEvidenceReceipt:
    ref_items = tuple(sorted(refs, key=lambda item: item.evidence_id))
    if not ref_items:
        raise HistoricalBridgeError("historical_evidence_projection_missing", "receipt refs")
    if any(item.availability == "unavailable" for item in ref_items):
        raise HistoricalBridgeError("historical_source_mismatch", "unavailable evidence")
    binding_items = tuple(sorted(bindings, key=binding_sort_key))
    binding_digest = provenance_bindings_digest(binding_items)
    if not binding_items and kind != "analogue":
        raise HistoricalBridgeError("historical_provenance_binding_missing", "receipt bindings")
    values = {
        "bridge_version": BRIDGE_VERSION,
        "investigation_id": frame.investigation_id,
        "scope_digest": scope_digest(frame),
        "kind": kind,
        "source_object_id": source_object_id,
        "source_object_state": source_object_state,
        "as_of": frame.as_of,
        "source_market": source_market,
        "target_market": target_market,
        "source_families": tuple(sorted({item.source_family for item in ref_items})),
        "evidence_ids": tuple(item.evidence_id for item in ref_items),
        "evidence_refs_digest": evidence_refs_digest(ref_items),
        "evidence_projection_receipt_id": projection.projection_receipt_id,
        "source_projection_digest": projection.source_projection_digest,
        "provenance_binding_ids": tuple(item.binding_id for item in binding_items),
        "provenance_bindings_digest": binding_digest,
        "observed_window_start": min(item.published_at for item in ref_items),
        "observed_window_end": max(item.published_at for item in ref_items),
        "historical_window_start": historical_window_start,
        "historical_window_end": historical_window_end,
        "difference_reasons": tuple(sorted(set(difference_reasons))),
        "limitations": tuple(
            sorted(
                set(limitations)
                | (
                    {"display_material_aged_out"}
                    if any(item.availability == "aged_out" for item in ref_items)
                    else set()
                )
            )
        ),
        "rule_version": rule_version,
        "future_leak_state": "passed",
        "future_leak_reasons": (),
        "attachment_eligible": attachment_eligible,
        "display_eligible": False,
    }
    identifier = "heb_" + hashlib.sha256(canonical_json_bytes(values)).hexdigest()
    return HistoricalEvidenceReceipt(historical_receipt_id=identifier, **values)


def _receipt_source_authority(
    *,
    projection: HistoricalEvidenceProjectionSnapshot,
    kind: str,
    source_object_id: str,
    source_object_state: str,
    source_market: str,
    target_market: str,
    refs: Iterable[EvidenceReceiptRef],
    bindings: Iterable[HistoricalEvidenceProvenanceBinding],
    historical_window_start: date,
    historical_window_end: date,
    rule_version: str,
) -> _HistoricalReceiptSourceAuthority:
    ref_items = tuple(sorted(refs, key=lambda item: item.evidence_id))
    binding_items = tuple(sorted(bindings, key=binding_sort_key))
    return _HistoricalReceiptSourceAuthority(
        kind=kind,
        source_object_id=source_object_id,
        source_object_state=source_object_state,
        source_market=source_market,
        target_market=target_market,
        evidence_ids=tuple(item.evidence_id for item in ref_items),
        observed_window_start=min(item.published_at for item in ref_items),
        observed_window_end=max(item.published_at for item in ref_items),
        historical_window_start=historical_window_start,
        historical_window_end=historical_window_end,
        rule_version=rule_version,
        evidence_projection_receipt_id=projection.projection_receipt_id,
        source_projection_digest=projection.source_projection_digest,
        provenance_binding_ids=tuple(item.binding_id for item in binding_items),
        provenance_bindings_digest=provenance_bindings_digest(binding_items),
    )


def _result(
    frame: HistoricalBridgeFrame,
    read_receipt: ProjectionReadReceipt,
    projection: HistoricalEvidenceProjectionSnapshot,
    bindings: Iterable[HistoricalEvidenceProvenanceBinding],
    receipts: Iterable[HistoricalEvidenceReceipt],
    issues: Iterable[HistoricalBridgeIssue],
    evidence_projection: RuntimeHistoricalEvidenceProjectionRead,
    receipt_sources: Iterable[_HistoricalReceiptSourceAuthority],
) -> HistoricalEvidenceBridgeResult:
    authoritative_read_receipt, authoritative_projection = validate_runtime_projection_read(
        evidence_projection
    )
    if read_receipt != authoritative_read_receipt or projection != authoritative_projection:
        raise HistoricalBridgeError(
            "historical_evidence_projection_digest_mismatch",
            "bridge source projection",
            reasons=("authoritative_source_projection",),
        )
    binding_items = tuple(sorted(bindings, key=binding_sort_key))
    if len({item.receipt_id for item in binding_items}) != len(binding_items):
        raise HistoricalBridgeError("historical_provenance_binding_duplicate", "binding ownership")
    receipt_items = tuple(sorted(receipts, key=lambda item: item.historical_receipt_id))
    issue_items = tuple(sorted(issues, key=lambda item: (item.source_object_id, item.code)))
    source_items = tuple(
        sorted(receipt_sources, key=lambda item: (item.kind, item.source_object_id))
    )
    if {(item.kind, item.source_object_id) for item in source_items} != {
        (item.kind, item.source_object_id) for item in receipt_items
    }:
        raise HistoricalBridgeError(
            "historical_provenance_source_object_mismatch",
            "bridge receipt authority",
            reasons=("authoritative_receipt_coverage",),
        )
    result = HistoricalEvidenceBridgeResult(
        frame=copy.deepcopy(frame),
        projection_read_receipt=copy.deepcopy(read_receipt),
        evidence_projection=copy.deepcopy(projection),
        bindings=binding_items,
        receipts=receipt_items,
        issues=issue_items,
    )
    object.__setattr__(
        result,
        "_authority",
        _HistoricalBridgeAuthority(
            capability=_BRIDGE_AUTHORITY_CAPABILITY,
            evidence_projection=evidence_projection,
            frame=copy.deepcopy(frame),
            bindings=copy.deepcopy(binding_items),
            receipts=copy.deepcopy(receipt_items),
            issues=copy.deepcopy(issue_items),
            receipt_sources=copy.deepcopy(source_items),
        ),
    )
    validate_historical_evidence_bridge_result(result)
    return result


def validate_historical_evidence_bridge_result(
    result: HistoricalEvidenceBridgeResult,
) -> None:
    if not isinstance(result, HistoricalEvidenceBridgeResult):
        raise HistoricalBridgeError("historical_provenance_incomplete", "bridge result")
    authority = getattr(result, "_authority", None)
    if (
        type(authority) is not _HistoricalBridgeAuthority
        or authority.capability is not _BRIDGE_AUTHORITY_CAPABILITY
    ):
        raise HistoricalBridgeError(
            "historical_projection_capability_invalid",
            "bridge result authority",
            source_object_ids=(
                item.source_object_id
                for item in (*result.bindings, *result.receipts, *result.issues)
            ),
            evidence_ids=(item.evidence_id for item in result.evidence_projection.evidence_refs),
            reasons=("bridge_result_authority",),
        )
    authoritative_read_receipt, authoritative_projection = validate_runtime_projection_read(
        authority.evidence_projection
    )
    try:
        validate_projection_read_receipt(result.projection_read_receipt)
    except HistoricalBridgeError as error:
        _raise_result_error(
            result,
            error.code,
            str(error),
            error.reasons[0] if error.reasons else "projection_read_receipt",
        )
    try:
        validate_projection_snapshot(result.evidence_projection)
    except HistoricalBridgeError as error:
        _raise_result_error(
            result,
            error.code,
            str(error),
            error.reasons[0] if error.reasons else "evidence_projection",
        )
    expected_scope_digest = scope_digest(result.frame)
    if (
        result.projection_read_receipt.scope_digest != expected_scope_digest
        or result.evidence_projection.scope_digest != expected_scope_digest
    ):
        _raise_result_error(
            result,
            "historical_evidence_reader_scope_mismatch",
            "result scope",
            "scope_digest",
        )
    if result.projection_read_receipt.contract_version != result.frame.contract_version:
        _raise_result_error(
            result,
            "historical_evidence_reader_scope_mismatch",
            "result contract",
            "contract_version",
        )
    if result.projection_read_receipt.run_id != result.frame.run_id:
        _raise_result_error(
            result,
            "historical_evidence_reader_scope_mismatch",
            "result run",
            "run_id",
        )
    if result.projection_read_receipt.as_of != result.frame.as_of:
        _raise_result_error(
            result,
            "historical_evidence_reader_scope_mismatch",
            "result as of",
            "as_of",
        )
    refs = {item.evidence_id: item for item in result.evidence_projection.evidence_refs}
    if len(refs) != len(result.evidence_projection.evidence_refs):
        _raise_result_error(
            result,
            "historical_evidence_reader_duplicate",
            "result references",
            "evidence_ids",
        )
    if any(
        _scope_values(item) != _scope_values(result.frame)
        for item in result.evidence_projection.evidence_refs
    ):
        _raise_result_error(
            result,
            "historical_evidence_reader_scope_mismatch",
            "result reference scope",
            "evidence_reference_scope",
        )
    requested_ids = result.projection_read_receipt.requested_evidence_ids
    reference_ids = tuple(sorted(refs))
    if tuple(sorted(set(requested_ids))) != requested_ids or set(requested_ids) != set(
        reference_ids
    ):
        code = (
            "historical_evidence_projection_missing"
            if set(requested_ids) - set(reference_ids)
            else "historical_evidence_projection_extra"
        )
        _raise_result_error(result, code, "result requested evidence", "requested_evidence_ids")
    if result.projection_read_receipt.row_count != len(reference_ids):
        _raise_result_error(
            result,
            "historical_evidence_reader_incomplete",
            "result row count",
            "row_count",
        )
    natural_keys = result.projection_read_receipt.row_natural_key_digests
    if len(natural_keys) != result.projection_read_receipt.row_count:
        _raise_result_error(
            result,
            "historical_evidence_reader_incomplete",
            "result natural keys",
            "row_natural_key_digests",
        )
    if len(set(natural_keys)) != len(natural_keys) or any(
        _NATURAL_KEY_DIGEST.fullmatch(item) is None for item in natural_keys
    ):
        _raise_result_error(
            result,
            "historical_evidence_reader_semantics_invalid",
            "result natural keys",
            "row_natural_key_digests",
        )
    expected_markets = tuple(sorted({item.market for item in refs.values()}))
    if result.projection_read_receipt.markets != expected_markets:
        _raise_result_error(
            result,
            "historical_evidence_reader_scope_mismatch",
            "result markets",
            "markets",
        )
    if (
        result.evidence_projection.projection_read_receipt_id
        != result.projection_read_receipt.projection_read_receipt_id
        or result.evidence_projection.source_projection_digest
        != result.projection_read_receipt.source_projection_digest
    ):
        _raise_result_error(
            result,
            "historical_evidence_projection_digest_mismatch",
            "result projection",
            "projection_linkage",
        )
    if result.frame != authority.frame:
        _raise_result_error(
            result,
            "historical_evidence_reader_scope_mismatch",
            "authoritative frame",
            "authoritative_frame",
        )
    if (
        result.projection_read_receipt != authoritative_read_receipt
        or result.evidence_projection != authoritative_projection
    ):
        _raise_result_error(
            result,
            "historical_evidence_projection_digest_mismatch",
            "authoritative source projection",
            "authoritative_source_projection",
        )
    receipt_sources = {
        (item.kind, item.source_object_id): item for item in authority.receipt_sources
    }
    receipts_by_source = {(item.kind, item.source_object_id): item for item in result.receipts}
    if set(receipt_sources) != set(receipts_by_source):
        _raise_result_error(
            result,
            "historical_provenance_source_object_mismatch",
            "authoritative receipt coverage",
            "authoritative_receipt_coverage",
        )
    source_fields = (
        "source_object_state",
        "source_market",
        "target_market",
        "evidence_ids",
        "observed_window_start",
        "observed_window_end",
        "historical_window_start",
        "historical_window_end",
        "rule_version",
        "evidence_projection_receipt_id",
        "source_projection_digest",
        "provenance_binding_ids",
        "provenance_bindings_digest",
    )
    for key, source in receipt_sources.items():
        receipt = receipts_by_source[key]
        for field in source_fields:
            if getattr(receipt, field) != getattr(source, field):
                _raise_result_error(
                    result,
                    "historical_provenance_source_object_mismatch",
                    "authoritative receipt source",
                    f"authoritative_receipt_{field}",
                )
    bindings = {item.binding_id: item for item in result.bindings}
    if len(bindings) != len(result.bindings):
        raise HistoricalBridgeError("historical_provenance_binding_duplicate", "result bindings")
    for binding in result.bindings:
        validate_provenance_binding(binding)
        ref = refs.get(binding.receipt_id)
        if ref is None:
            raise HistoricalBridgeError("historical_provenance_binding_extra", "binding receipt")
        if binding.expected_signal_id != ref.signal_id:
            raise HistoricalBridgeError("historical_provenance_signal_mismatch", "binding signal")
        if binding.expected_source_family != ref.source_family:
            raise HistoricalBridgeError(
                "historical_provenance_source_family_mismatch", "binding family"
            )
    if result.bindings != authority.bindings:
        _raise_result_error(
            result,
            "historical_provenance_binding_digest_mismatch",
            "authoritative bindings",
            "authoritative_bindings",
        )
    for receipt in result.receipts:
        values = asdict(receipt)
        identifier = values.pop("historical_receipt_id")
        if identifier != "heb_" + hashlib.sha256(canonical_json_bytes(values)).hexdigest():
            raise HistoricalBridgeError(
                "historical_provenance_binding_digest_mismatch", "historical receipt"
            )
        if (
            receipt.evidence_projection_receipt_id
            != result.evidence_projection.projection_receipt_id
            or receipt.source_projection_digest
            != result.evidence_projection.source_projection_digest
        ):
            raise HistoricalBridgeError(
                "historical_evidence_projection_digest_mismatch", "receipt projection"
            )
        selected_refs = tuple(refs[item] for item in receipt.evidence_ids if item in refs)
        if len(selected_refs) != len(receipt.evidence_ids):
            raise HistoricalBridgeError("historical_evidence_projection_missing", "receipt refs")
        if receipt.evidence_refs_digest != evidence_refs_digest(selected_refs):
            raise HistoricalBridgeError(
                "historical_evidence_projection_digest_mismatch", "receipt refs"
            )
        expected_families = tuple(sorted({item.source_family for item in selected_refs}))
        if receipt.source_families != expected_families:
            raise HistoricalBridgeError(
                "historical_provenance_source_family_mismatch", "receipt families"
            )
        if (
            receipt.source_market not in result.frame.market_scope
            or receipt.target_market not in result.frame.market_scope
        ):
            raise HistoricalBridgeError(
                "historical_provenance_source_object_mismatch", "receipt markets"
            )
        selected_bindings = tuple(
            bindings[item] for item in receipt.provenance_binding_ids if item in bindings
        )
        if len(selected_bindings) != len(receipt.provenance_binding_ids):
            raise HistoricalBridgeError("historical_provenance_binding_missing", "receipt binding")
        expected_digest = provenance_bindings_digest(selected_bindings)
        if receipt.provenance_bindings_digest != expected_digest:
            raise HistoricalBridgeError(
                "historical_provenance_binding_digest_mismatch", "receipt binding digest"
            )
        if receipt.kind == "analogue" and selected_bindings:
            raise HistoricalBridgeError("historical_provenance_binding_extra", "analogue binding")
        if receipt.kind != "analogue" and {item.receipt_id for item in selected_bindings} != set(
            receipt.evidence_ids
        ):
            raise HistoricalBridgeError(
                "historical_receipt_binding_coverage_mismatch", "receipt binding coverage"
            )
        if receipt.kind == "diffusion_shadow" and (
            receipt.attachment_eligible or receipt.display_eligible
        ):
            raise HistoricalBridgeError("historical_diffusion_ineligible", "diffusion receipt")
    if result.receipts != authority.receipts:
        _raise_result_error(
            result,
            "historical_provenance_binding_digest_mismatch",
            "authoritative receipts",
            "authoritative_receipts",
        )
    if result.issues != authority.issues:
        _raise_result_error(
            result,
            "historical_provenance_incomplete",
            "authoritative issues",
            "authoritative_issues",
        )


def build_analogue_evidence(
    *,
    frame: HistoricalBridgeFrame,
    current: HistoricalSignalSnapshot,
    candidates: Iterable[HistoricalSignalSnapshot],
    rules: HistoricalAnalogueRules,
    evidence_projection: RuntimeHistoricalEvidenceProjectionRead,
    limit: int,
) -> HistoricalEvidenceBridgeResult:
    if current.as_of != frame.as_of:
        raise HistoricalBridgeError("historical_as_of_mismatch", "analogue as of")
    candidate_items = tuple(candidates)
    future_candidates = tuple(item for item in candidate_items if item.as_of >= current.as_of)
    if future_candidates:
        raise HistoricalBridgeError(
            "historical_future_leak",
            "analogue subject is not historical",
            source_object_ids=(item.signal_id for item in future_candidates),
            evidence_ids=(
                receipt.receipt_id for item in future_candidates for receipt in item.receipts
            ),
            reasons=("candidate_not_prior",),
        )
    read_receipt, projection, refs = _projection(frame, evidence_projection)
    source_ids = {
        item.receipt_id for snapshot in (current, *candidate_items) for item in snapshot.receipts
    }
    if set(refs) != source_ids:
        code = (
            "historical_evidence_projection_missing"
            if source_ids - set(refs)
            else "historical_evidence_projection_extra"
        )
        raise HistoricalBridgeError(code, "analogue coverage")
    search = find_historical_analogues(current, candidate_items, rules, limit=limit)
    candidates_by_id = {item.signal_id: item for item in candidate_items}
    receipts = []
    receipt_sources = []
    for match in search.matches:
        candidate = candidates_by_id[match.signal_id]
        selected = tuple(refs[item] for item in match.receipt_ids)
        if any(item.published_at > candidate.as_of for item in selected):
            raise HistoricalBridgeError("historical_future_leak", "analogue cutoff")
        limitations = [
            "historical_comparison_not_prediction",
            "similarity_does_not_establish_cause",
        ]
        if match.market != current.market:
            limitations.append("market_context_differs")
        historical_window_start = min(item.signal_date for item in selected)
        historical_window_end = max(item.signal_date for item in selected)
        rule_version = analogue_rules_version(rules)
        receipts.append(
            _build_receipt(
                frame=frame,
                projection=projection,
                kind="analogue",
                source_object_id=match.signal_id,
                source_object_state="match",
                source_market=match.market,
                target_market=current.market,
                refs=selected,
                bindings=(),
                historical_window_start=historical_window_start,
                historical_window_end=historical_window_end,
                difference_reasons=match.difference_codes,
                limitations=limitations,
                rule_version=rule_version,
                attachment_eligible=True,
            )
        )
        receipt_sources.append(
            _receipt_source_authority(
                projection=projection,
                kind="analogue",
                source_object_id=match.signal_id,
                source_object_state="match",
                source_market=match.market,
                target_market=current.market,
                refs=selected,
                bindings=(),
                historical_window_start=historical_window_start,
                historical_window_end=historical_window_end,
                rule_version=rule_version,
            )
        )
    issues = tuple(
        HistoricalBridgeIssue(
            code="historical_future_leak"
            if item.reason.startswith("future")
            else "historical_analogue_unavailable",
            kind="analogue",
            source_object_id=item.signal_id,
            evidence_ids=tuple(
                receipt.receipt_id for receipt in candidates_by_id[item.signal_id].receipts
            ),
            reasons=(item.reason,),
        )
        for item in search.rejections
    )
    return _result(
        frame,
        read_receipt,
        projection,
        (),
        receipts,
        issues,
        evidence_projection,
        receipt_sources,
    )


def build_recurrence_evidence(
    *,
    frame: HistoricalBridgeFrame,
    occurrences: Iterable[RecurrenceOccurrence],
    pattern_id: str,
    market: str,
    rules: RecurrenceRules,
    evidence_projection: RuntimeHistoricalEvidenceProjectionRead,
) -> HistoricalEvidenceBridgeResult:
    items = tuple(occurrences)
    read_receipt, projection, refs = _projection(frame, evidence_projection)
    assessment, bindings = detect_historical_recurrence_with_provenance(
        occurrences=items,
        pattern_id=pattern_id,
        market=market,
        as_of=frame.as_of.date(),
        rules=rules,
        evidence_projection=evidence_projection,
    )
    if assessment.state == "insufficient_history":
        issue = HistoricalBridgeIssue(
            code="historical_recurrence_insufficient",
            kind="recurrence",
            source_object_id=assessment.pattern_id,
            evidence_ids=assessment.receipt_ids,
            reasons=(assessment.reason,),
        )
        return _result(
            frame,
            read_receipt,
            projection,
            bindings,
            (),
            (issue,),
            evidence_projection,
            (),
        )
    selected = tuple(refs[item] for item in assessment.receipt_ids)
    receipt_bindings = _bindings_for_receipt(bindings, assessment.receipt_ids)
    receipt = _build_receipt(
        frame=frame,
        projection=projection,
        kind="recurrence",
        source_object_id=assessment.pattern_id,
        source_object_state=assessment.state,
        source_market=assessment.market,
        target_market=assessment.market,
        refs=selected,
        bindings=receipt_bindings,
        historical_window_start=assessment.first_occurrence,
        historical_window_end=assessment.last_occurrence,
        difference_reasons=(assessment.reason,),
        limitations=("recurrence_does_not_establish_cause", "recurrence_is_not_a_forecast"),
        rule_version=assessment.rule_version,
        attachment_eligible=True,
    )
    receipt_source = _receipt_source_authority(
        projection=projection,
        kind="recurrence",
        source_object_id=assessment.pattern_id,
        source_object_state=assessment.state,
        source_market=assessment.market,
        target_market=assessment.market,
        refs=selected,
        bindings=receipt_bindings,
        historical_window_start=assessment.first_occurrence,
        historical_window_end=assessment.last_occurrence,
        rule_version=assessment.rule_version,
    )
    return _result(
        frame,
        read_receipt,
        projection,
        bindings,
        (receipt,),
        (),
        evidence_projection,
        (receipt_source,),
    )


def build_diffusion_shadow(
    *,
    frame: HistoricalBridgeFrame,
    current: CurrentDiffusionSignal,
    historical_sequences: Iterable[HistoricalDiffusionSequence],
    target_markets: Iterable[str],
    rules: DiffusionRules,
    evidence_projection: RuntimeHistoricalEvidenceProjectionRead,
) -> HistoricalEvidenceBridgeResult:
    if current.as_of != frame.as_of.date():
        raise HistoricalBridgeError("historical_as_of_mismatch", "diffusion as of")
    sequences = tuple(historical_sequences)
    targets = tuple(sorted(set(target_markets)))
    read_receipt, projection, refs = _projection(frame, evidence_projection)
    all_bindings = []
    receipts = []
    receipt_sources = []
    issues = []
    for target in targets:
        forecast, bindings = forecast_cross_market_diffusion_with_provenance(
            current=current,
            historical_sequences=sequences,
            target_market=target,
            rules=rules,
            evidence_projection=evidence_projection,
        )
        all_bindings.extend(bindings)
        source_id = diffusion_source_object_id(current.signal_id, target)
        if not forecast.receipt_ids:
            issues.append(
                HistoricalBridgeIssue(
                    code="historical_provenance_incomplete",
                    kind="diffusion_shadow",
                    source_object_id=source_id,
                    evidence_ids=(),
                    reasons=(forecast.reason,),
                )
            )
            continue
        selected = tuple(refs[item] for item in forecast.receipt_ids)
        receipt_bindings = _bindings_for_receipt(bindings, forecast.receipt_ids)
        comparable = tuple(
            item
            for item in sequences
            if item.origin_market == current.origin_market and item.target_market == target
        )
        historical_window_start = min(item.origin_date for item in comparable)
        historical_window_end = max(item.target_date for item in comparable)
        receipts.append(
            _build_receipt(
                frame=frame,
                projection=projection,
                kind="diffusion_shadow",
                source_object_id=source_id,
                source_object_state=forecast.state,
                source_market=forecast.origin_market,
                target_market=forecast.target_market,
                refs=selected,
                bindings=receipt_bindings,
                historical_window_start=historical_window_start,
                historical_window_end=historical_window_end,
                difference_reasons=(forecast.reason,),
                limitations=("diffusion_shadow_only", "must_beat_persistence_before_promotion"),
                rule_version=forecast.rule_version,
                attachment_eligible=False,
            )
        )
        receipt_sources.append(
            _receipt_source_authority(
                projection=projection,
                kind="diffusion_shadow",
                source_object_id=source_id,
                source_object_state=forecast.state,
                source_market=forecast.origin_market,
                target_market=forecast.target_market,
                refs=selected,
                bindings=receipt_bindings,
                historical_window_start=historical_window_start,
                historical_window_end=historical_window_end,
                rule_version=forecast.rule_version,
            )
        )
    expected_ids = {
        receipt
        for item in sequences
        if item.origin_market == current.origin_market and item.target_market in targets
        for receipt in item.receipt_ids
    }
    if set(refs) != expected_ids:
        raise HistoricalBridgeError("historical_evidence_projection_extra", "diffusion coverage")
    return _result(
        frame,
        read_receipt,
        projection,
        all_bindings,
        receipts,
        issues,
        evidence_projection,
        receipt_sources,
    )


HISTORY_RECORD_VERSION = "history_record_v2"
HISTORY_REQUIREMENT_KIND = "history"
_HISTORY_AVAILABILITY = ("available", "aged_out")
_HISTORY_KINDS = ("analogue", "recurrence", "diffusion_shadow")
# The historical receipt digests these fields, in this order. The order is the
# receipt's own field order and is read from the dataclass rather than written
# out again, so a field added to the receipt cannot silently drop out of the
# rederivation below.
_HISTORICAL_RECEIPT_DIGEST_FIELDS = tuple(
    item.name for item in fields(HistoricalEvidenceReceipt) if item.name != "historical_receipt_id"
)


class _BridgeBuiltRecord:
    """Slot holder for the capability a record the bridge built carries.

    The capability is not a field, so it never reaches a stored projection,
    never enters a digest and cannot be restored by deserialising a snapshot.
    That holds against untrusted data only. It is not protected against code
    in the same process: the module attribute can be imported by name, and it
    can be read off any genuine record with getattr and set on another.
    """

    __slots__ = ("_bridge_capability",)


def _rederived_historical_receipt_id(values: Mapping[str, object]) -> str:
    """Recompute, from a record's own fields, the receipt id it names.

    The record carries every field the historical receipt digested except the
    leak reasons, and the record admits only a passed leak state, which is the
    state that leaves those reasons empty. So the receipt id is a pure function
    of twenty six of the record's own fields, and comparing the two shows only
    that: the id cannot be set independently of those fields. Every input comes
    from the same record; no receipt is fetched and no store is read, so a
    record for a receipt that was never issued, with its id computed here,
    passes.
    """
    receipt_values = {
        name: () if name == "future_leak_reasons" else values[name]
        for name in _HISTORICAL_RECEIPT_DIGEST_FIELDS
    }
    return "heb_" + hashlib.sha256(canonical_json_bytes(receipt_values)).hexdigest()


@dataclass(frozen=True, slots=True)
class HistoryRecord(_BridgeBuiltRecord):
    """One admitted retained historical finding, frozen for downstream reading.

    Every field is copied from a value the bridge already admitted and bound:
    the historical receipt, the projection read receipt and the evidence refs
    of the exact rows that were read. Nothing here is recomputed from source
    rows and nothing is invented, so a consumer can read a record without
    holding the bridge result and without reaching into the reader.

    Its windows are split: the observed window is when the evidence was
    published, the historical window is the period the finding is about, and a
    consumer that needs one must not read the other.

    Three things are checked here. The content digest fixes what the record
    says. The enumerated values a consumer reads as guarantees are held to
    their sets: the two versions, the contract version, a requirement id that
    is text, the leak state, availability, the kind, the reader mode and the
    reader version, the diffusion eligibility rule, a non-empty evidence set
    and both window orderings. And the historical receipt id is rederived from
    twenty six of the record's own fields, so the id cannot be set
    independently of them. That is a consistency check within the record, not
    a check against any receipt.

    The requirement id is inside the digested field set, so one record answers
    exactly one planned requirement and cannot be copied onto a second.

    What this does not establish. The content digest is computed over the
    record's own values, so it proves only that these values were hashed
    together; anyone holding the values can reproduce it, and it says nothing
    about where they came from. The rederived receipt id binds the record to
    one historical receipt, but that receipt is never fetched and compared
    against a stored one, so a record and a receipt fabricated together are
    consistent here. Four fields sit outside both checks and are believed as
    written: the record version, the contract version, the projection read
    receipt id and the availability. Nothing here reaches the evidence
    projection, the rows that were read or the investigation the scope digest
    names, so this type says the record is internally coherent, not that any
    read ever happened. What the answer type requires in addition is the
    bridge capability a built record carries. It is not a field, so it is
    absent from the stored projection and cannot be restored by deserialising
    a snapshot; it guards against untrusted data, not against code in the same
    process, which can lift it off a genuine record.
    """

    history_record_id: str
    record_version: Literal["history_record_v2"]
    bridge_version: Literal["historical_evidence_bridge_v1"]
    contract_version: Literal["2.1.0"]
    requirement_id: str
    investigation_id: str
    scope_digest: str
    kind: Literal["analogue", "recurrence", "diffusion_shadow"]
    source_object_id: str
    source_object_state: str
    as_of: datetime
    source_market: str
    target_market: str
    source_families: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    evidence_refs_digest: str
    historical_receipt_id: str
    projection_read_receipt_id: str
    evidence_projection_receipt_id: str
    source_projection_digest: str
    provenance_binding_ids: tuple[str, ...]
    provenance_bindings_digest: str
    observed_window_start: datetime
    observed_window_end: datetime
    historical_window_start: date
    historical_window_end: date
    difference_reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    rule_version: str
    availability: Literal["available", "aged_out"]
    future_leak_state: Literal["passed"]
    attachment_eligible: bool
    display_eligible: bool
    reader_mode: Literal["runtime_view"]
    reader_version: Literal["historical_evidence_projection_reader_v1"]

    def __post_init__(self) -> None:
        values = asdict(self)
        identifier = values.pop("history_record_id")
        if identifier != _history_record_id(values):
            raise HistoricalBridgeError(
                "historical_provenance_binding_digest_mismatch", "history record id"
            )
        if self.record_version != HISTORY_RECORD_VERSION:
            raise HistoricalBridgeError("historical_provenance_incomplete", "record version")
        if self.bridge_version != BRIDGE_VERSION:
            raise HistoricalBridgeError("historical_provenance_incomplete", "bridge version")
        if self.contract_version != CONTRACT_VERSION:
            raise HistoricalBridgeError("historical_contract_version_invalid", "contract version")
        if not isinstance(self.requirement_id, str) or not self.requirement_id.strip():
            raise HistoricalBridgeError("historical_provenance_incomplete", "record requirement id")
        if self.future_leak_state != "passed":
            raise HistoricalBridgeError("historical_future_leak", "record leak state")
        if self.availability not in _HISTORY_AVAILABILITY:
            raise HistoricalBridgeError("historical_source_mismatch", "record availability")
        if self.kind not in _HISTORY_KINDS:
            raise HistoricalBridgeError(
                "historical_provenance_source_object_mismatch", "record kind"
            )
        if self.reader_mode != "runtime_view":
            raise HistoricalBridgeError(
                "historical_evidence_reader_target_invalid", "record reader mode"
            )
        if self.reader_version != READER_VERSION:
            raise HistoricalBridgeError(
                "historical_evidence_reader_target_invalid", "record reader version"
            )
        if self.kind == "diffusion_shadow" and (self.attachment_eligible or self.display_eligible):
            raise HistoricalBridgeError("historical_diffusion_ineligible", "record eligibility")
        if not self.evidence_ids:
            raise HistoricalBridgeError("historical_evidence_projection_missing", "record evidence")
        if (
            self.observed_window_end < self.observed_window_start
            or self.historical_window_end < self.historical_window_start
        ):
            raise HistoricalBridgeError("historical_provenance_incomplete", "record window")
        # Last, so that every enumerated value above answers with its own code
        # rather than with a digest mismatch. A record that got this far may
        # still have been rehashed around a field the historical receipt
        # already fixed, so that id is rederived from the record's own fields
        # and a record whose id disagrees with them is refused. No receipt is
        # fetched, so a record and an id fabricated together still pass.
        if self.historical_receipt_id != _rederived_historical_receipt_id(values):
            raise HistoricalBridgeError(
                "historical_provenance_binding_digest_mismatch", "record historical receipt id"
            )


def _history_record_id(values: Mapping[str, object]) -> str:
    """The record's own content digest over its values in declaration order.

    Contract, not an accident: the canonical encoder this slice uses does not
    sort keys, so the digest depends on the order the record declares its
    fields. Reordering a field, and not only changing one, invalidates every
    record already written. Fields are therefore appended, never moved, and a
    reorder is a record version change. The encoder is shared with every other
    digest in this slice, so it is not sorted here for this one caller.

    The digest fixes what the record says and nothing more. It is computed
    over the record's own values, so it does not establish where any of them
    came from.
    """
    return "hrc_" + hashlib.sha256(canonical_json_bytes(values)).hexdigest()


def build_history_records(
    result: HistoricalEvidenceBridgeResult,
    *,
    requirement_id: str,
) -> tuple[HistoryRecord, ...]:
    """Project an admitted bridge result into frozen history records.

    The result is revalidated against its own authority first, so a result
    that lost or never had its bridge authority yields no record at all rather
    than a record that looks admitted. One record is produced per historical
    receipt, in the receipt order the result already fixed.

    The planned requirement the records answer is named here and digested with
    them, so a record is bound to that one requirement and cannot be presented
    as the answer to a second.

    Each record built here is bound to the module's bridge capability, the
    same object the bridge result carries. The capability is not a field, so
    it is not digested, not projected into a stored snapshot and not
    reproducible from the record's values; it is what the answer type checks,
    so a record rebuilt from stored or other untrusted data cannot be
    presented as admitted. Code in the same process can still lift it off a
    genuine record, so it is no defence against that code.
    """
    if not isinstance(requirement_id, str) or not requirement_id.strip():
        raise HistoricalBridgeError("historical_provenance_incomplete", "requirement id")
    validate_historical_evidence_bridge_result(result)
    refs = {item.evidence_id: item for item in result.evidence_projection.evidence_refs}
    read_receipt = result.projection_read_receipt
    records = []
    for receipt in result.receipts:
        selected = tuple(refs[item] for item in receipt.evidence_ids)
        availability = (
            "aged_out" if any(item.availability == "aged_out" for item in selected) else "available"
        )
        values = {
            "record_version": HISTORY_RECORD_VERSION,
            "bridge_version": receipt.bridge_version,
            "contract_version": result.frame.contract_version,
            "requirement_id": requirement_id,
            "investigation_id": receipt.investigation_id,
            "scope_digest": receipt.scope_digest,
            "kind": receipt.kind,
            "source_object_id": receipt.source_object_id,
            "source_object_state": receipt.source_object_state,
            "as_of": receipt.as_of,
            "source_market": receipt.source_market,
            "target_market": receipt.target_market,
            "source_families": receipt.source_families,
            "evidence_ids": receipt.evidence_ids,
            "evidence_refs_digest": receipt.evidence_refs_digest,
            "historical_receipt_id": receipt.historical_receipt_id,
            "projection_read_receipt_id": read_receipt.projection_read_receipt_id,
            "evidence_projection_receipt_id": receipt.evidence_projection_receipt_id,
            "source_projection_digest": receipt.source_projection_digest,
            "provenance_binding_ids": receipt.provenance_binding_ids,
            "provenance_bindings_digest": receipt.provenance_bindings_digest,
            "observed_window_start": receipt.observed_window_start,
            "observed_window_end": receipt.observed_window_end,
            "historical_window_start": receipt.historical_window_start,
            "historical_window_end": receipt.historical_window_end,
            "difference_reasons": receipt.difference_reasons,
            "limitations": receipt.limitations,
            "rule_version": receipt.rule_version,
            "availability": availability,
            "future_leak_state": receipt.future_leak_state,
            "attachment_eligible": receipt.attachment_eligible,
            "display_eligible": receipt.display_eligible,
            "reader_mode": read_receipt.reader_mode,
            "reader_version": read_receipt.reader_version,
        }
        record = HistoryRecord(history_record_id=_history_record_id(values), **values)
        object.__setattr__(record, "_bridge_capability", _BRIDGE_AUTHORITY_CAPABILITY)
        records.append(record)
    return tuple(records)


@dataclass(frozen=True, slots=True)
class RetainedHistorySource:
    """The retained signals one planned history requirement is answered from."""

    requirement_id: str
    current: HistoricalSignalSnapshot
    candidates: tuple[HistoricalSignalSnapshot, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.requirement_id, str) or not self.requirement_id:
            raise HistoricalBridgeError("historical_provenance_incomplete", "requirement id")
        object.__setattr__(self, "candidates", tuple(self.candidates))


@dataclass(frozen=True, slots=True)
class HistoryRecordAnswer:
    """A planned history requirement answered by one admitted history record.

    Only a record the bridge built enters here. The record must carry the
    module's bridge capability, which no value in the record can produce, so a
    record assembled from a stored projection, or rehashed around whatever an
    editor liked, is refused at this door however coherent it looks. Code in
    the same process that copies the capability off a genuine record is not
    refused; the guard is against untrusted data only.

    The answer's requirement id and its record's requirement id are the same
    value, checked here, so an admitted record cannot be moved onto a
    requirement it was never built for.
    """

    requirement_id: str
    record: HistoryRecord
    state: str = "retained_analogue"

    def __post_init__(self) -> None:
        if not isinstance(self.requirement_id, str) or not self.requirement_id.strip():
            raise HistoricalBridgeError("historical_provenance_incomplete", "requirement id")
        if getattr(self.record, "_bridge_capability", None) is not _BRIDGE_AUTHORITY_CAPABILITY:
            raise HistoricalBridgeError(
                "historical_projection_capability_invalid", "answer record capability"
            )
        if self.record.requirement_id != self.requirement_id:
            raise HistoricalBridgeError(
                "historical_provenance_source_object_mismatch", "answer requirement id"
            )

    @property
    def matched_signal_id(self) -> str:
        """The earlier signal this record matched, never the current one."""
        return self.record.source_object_id

    @property
    def as_of(self) -> datetime:
        return self.record.as_of


@dataclass(frozen=True, slots=True)
class UnresolvedHistory:
    """A planned history requirement that no admitted retained read answered.

    This state exists so that answering nothing is something the snapshot
    carries and the product can say, never an omission a reader has to notice.
    The reason is the exact refusal code, or the exact name of the gap.
    """

    requirement_id: str
    reason: str
    state: str = "unresolved"

    def __post_init__(self) -> None:
        if not isinstance(self.requirement_id, str) or not self.requirement_id:
            raise HistoricalBridgeError("historical_provenance_incomplete", "requirement id")
        if not isinstance(self.reason, str) or not self.reason:
            raise HistoricalBridgeError("historical_provenance_incomplete", "unresolved reason")


def planned_history_requirement_ids(plan: object) -> tuple[str, ...]:
    """The history requirement ids of one question plan, in a fixed order.

    This is the one reading of a plan's history requirements. The worker that
    builds the resolver, the snapshot builder that calls it and the stored
    snapshot validator all ask here, so a plan cannot mean one set of history
    requirements in one place and another set somewhere else.

    A requirement with no kind is refused rather than skipped: skipping it
    quietly stops a planned history requirement from being history. An id that
    is not text is refused because the built record would be keyed by one
    value and the canonicalized snapshot by another, so the snapshot would
    never read back. Two requirements sharing an id are refused because they
    would collapse into a single answer and one of them would go unanswered
    with nothing saying so.
    """
    requirements = plan["requirements"]
    for item in requirements:
        if not isinstance(item, dict) or "kind" not in item:
            raise HistoricalBridgeError("historical_provenance_incomplete", "requirement kind")
    identifiers = tuple(
        item["requirement_id"] for item in requirements if item["kind"] == HISTORY_REQUIREMENT_KIND
    )
    if any(not isinstance(item, str) or not item.strip() for item in identifiers):
        raise HistoricalBridgeError("historical_provenance_incomplete", "history requirement id")
    if len(set(identifiers)) != len(identifiers):
        raise HistoricalBridgeError("historical_provenance_incomplete", "history requirement ids")
    return tuple(sorted(identifiers))


def build_question_history_resolver(
    *,
    frame: HistoricalBridgeFrame,
    sources: Iterable[RetainedHistorySource],
    rules: HistoricalAnalogueRules,
    limit: int = 1,
    read_projection=None,
):
    """Answer a question plan's history requirements from the retained path.

    Every planned history requirement gets an answer. One with a retained
    source is read through the admitted runtime projection reader and bridged
    into a history record; one with no retained source, one whose read or
    bridge refuses, one whose read fails for any other reason, and one whose
    retained signals yield no analogue each get an UnresolvedHistory naming the
    exact reason. The resolver never returns a requirement without an entry,
    because a missing entry is the silence this seam exists to remove.
    """
    reader = (
        read_runtime_analogue_evidence_projection if read_projection is None else read_projection
    )
    by_requirement = {}
    for item in sources:
        if item.requirement_id in by_requirement:
            raise HistoricalBridgeError(
                "historical_provenance_source_object_mismatch", "duplicate history source"
            )
        by_requirement[item.requirement_id] = item

    def resolve(plan):
        answers = {}
        for requirement_id in planned_history_requirement_ids(plan):
            source = by_requirement.get(requirement_id)
            if source is None:
                answers[requirement_id] = UnresolvedHistory(
                    requirement_id, "historical_source_not_retained"
                )
                continue
            try:
                projection = reader(
                    frame=frame, current=source.current, candidates=source.candidates
                )
                result = build_analogue_evidence(
                    frame=frame,
                    current=source.current,
                    candidates=source.candidates,
                    rules=rules,
                    evidence_projection=projection,
                    limit=limit,
                )
                records = build_history_records(result, requirement_id=requirement_id)
            except HistoricalBridgeError as error:
                answers[requirement_id] = UnresolvedHistory(requirement_id, error.code)
                continue
            except Exception:
                # The reader seam reaches a real transport once the production
                # target is live, so a failure there leaves one requirement
                # unresolved rather than failing the whole question.
                answers[requirement_id] = UnresolvedHistory(
                    requirement_id, "historical_evidence_read_failed"
                )
                continue
            if not records:
                answers[requirement_id] = UnresolvedHistory(
                    requirement_id, "historical_analogue_unavailable"
                )
            elif len(records) > 1:
                answers[requirement_id] = UnresolvedHistory(
                    requirement_id, "historical_analogue_ambiguous"
                )
            else:
                answers[requirement_id] = HistoryRecordAnswer(requirement_id, records[0])
        return answers

    return resolve


__all__ = [
    "BRIDGE_VERSION",
    "CONTRACT_VERSION",
    "HISTORY_RECORD_VERSION",
    "HISTORY_REQUIREMENT_KIND",
    "HistoricalBridgeFrame",
    "HistoricalBridgeIssue",
    "HistoricalEvidenceBridgeResult",
    "HistoricalEvidenceReceipt",
    "HistoryRecord",
    "HistoryRecordAnswer",
    "RetainedHistorySource",
    "UnresolvedHistory",
    "analogue_rules_version",
    "build_analogue_evidence",
    "build_diffusion_shadow",
    "build_history_records",
    "build_question_history_resolver",
    "build_recurrence_evidence",
    "diffusion_source_object_id",
    "planned_history_requirement_ids",
    "validate_historical_evidence_bridge_result",
]
