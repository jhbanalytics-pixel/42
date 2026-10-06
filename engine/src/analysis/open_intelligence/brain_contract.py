"""Immutable values for the dark Intelligence Brain kernel."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime
from typing import Literal

BRAIN_CONTRACT_VERSION = "intelligence_brain_v1"
ROLE_ORDER = ("observer", "historian", "analyst", "skeptic", "strategist", "editor")
RESEARCH_DEPTHS = ("briefing", "scan", "investigation", "evidence_room", "client_read")
APPROVED_MARKETS = frozenset({"za", "ng", "ke"})
_EVIDENCE_ID = re.compile(r"ev_[0-9a-f]{64}\Z")


def _utc(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
        and value.utcoffset().total_seconds() == 0
    )


def _valid_evidence_ids(values: tuple[str, ...]) -> bool:
    return (
        bool(values)
        and len(values) == len(set(values))
        and all(
            isinstance(value, str) and _EVIDENCE_ID.fullmatch(value) is not None for value in values
        )
    )


def _depth(limit: int, timeout: int) -> dict[str, object]:
    return {
        "max_cost_microusd": 0,
        "max_evidence_ids": limit,
        "max_input_tokens": 0,
        "max_output_tokens": 0,
        "model_stage_limit": 0,
        "optional_roles": [],
        "required_roles": list(ROLE_ORDER),
        "timeout_seconds": timeout,
    }


DARK_DEPTH_POLICY = {
    "contract_version": BRAIN_CONTRACT_VERSION,
    "depths": {
        "briefing": _depth(100, 5),
        "client_read": _depth(1000, 10),
        "evidence_room": _depth(2000, 30),
        "investigation": _depth(1000, 30),
        "scan": _depth(250, 10),
    },
    "policy_id": "brain_depth_policy_dark_v1",
}


def _canonical(value: object) -> object:
    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    return value


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _canonical(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


DARK_DEPTH_POLICY_DIGEST = canonical_digest(DARK_DEPTH_POLICY)


@dataclass(frozen=True)
class IntelligenceBrainIntent:
    contract_version: str
    investigation_id: str | None
    signal_id: str
    research_depth: str
    decision_question: str | None
    brand_context_id: str | None

    def __post_init__(self) -> None:
        if self.contract_version != BRAIN_CONTRACT_VERSION:
            raise ValueError("brain contract unsupported")
        if not isinstance(self.signal_id, str) or not self.signal_id:
            raise ValueError("signal id is invalid")
        if self.research_depth not in RESEARCH_DEPTHS:
            raise ValueError("research depth is invalid")
        if self.research_depth != "briefing" and not self.investigation_id:
            raise ValueError("investigation is required")
        if self.research_depth != "briefing" and not self.decision_question:
            raise ValueError("decision question is required")


@dataclass(frozen=True)
class BrainMeasurement:
    measurement_id: str
    metric_name: str
    availability: Literal["measured", "unavailable"]
    value: float | None
    unit: str | None
    method_id: str | None
    window_start: datetime | None
    window_end: datetime | None
    market: str | None
    evidence_ids: tuple[str, ...]
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.availability == "unavailable":
            if (
                any(
                    value is not None
                    for value in (
                        self.value,
                        self.unit,
                        self.method_id,
                        self.window_start,
                        self.window_end,
                        self.market,
                    )
                )
                or self.evidence_ids
            ):
                raise ValueError("unavailable measurement must be null")
            return
        if (
            self.availability != "measured"
            or isinstance(self.value, bool)
            or not isinstance(self.value, int | float)
            or not math.isfinite(float(self.value))
        ):
            raise ValueError("measured value is invalid")
        if (
            not self.unit
            or not self.method_id
            or self.market not in APPROVED_MARKETS
            or not _valid_evidence_ids(self.evidence_ids)
        ):
            raise ValueError("measured metadata is invalid")
        if (
            self.window_start is None
            or self.window_end is None
            or not _utc(self.window_start)
            or not _utc(self.window_end)
            or self.window_end < self.window_start
        ):
            raise ValueError("measurement window is invalid")


@dataclass(frozen=True)
class BrainConfidence:
    availability: Literal["available", "unavailable"]
    value: float | None
    method_id: str | None
    evidence_ids: tuple[str, ...]
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.availability == "unavailable":
            if self.value is not None or self.method_id is not None or self.evidence_ids:
                raise ValueError("unavailable confidence is invalid")
            return
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, int | float)
            or not (0 <= float(self.value) <= 1)
            or not self.method_id
            or not _valid_evidence_ids(self.evidence_ids)
        ):
            raise ValueError("confidence is invalid")


@dataclass(frozen=True)
class AudienceAvailabilityRecord:
    state: Literal["measured", "inferred", "unavailable"]
    statement: str | None
    source_id: str | None
    window_start: datetime | None
    window_end: datetime | None
    method_id: str | None
    scope_digest: str | None
    confidence: BrainConfidence | None
    evidence_ids: tuple[str, ...]
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.state == "unavailable":
            if (
                any(
                    value is not None
                    for value in (
                        self.statement,
                        self.source_id,
                        self.window_start,
                        self.window_end,
                        self.method_id,
                        self.scope_digest,
                        self.confidence,
                    )
                )
                or self.evidence_ids
            ):
                raise ValueError("unavailable audience is invalid")
            return
        if (
            self.state not in {"measured", "inferred"}
            or not all(
                (
                    self.statement,
                    self.source_id,
                    self.method_id,
                    self.scope_digest,
                    self.confidence,
                    self.evidence_ids,
                    self.window_start,
                    self.window_end,
                )
            )
            or not _utc(self.window_start)
            or not _utc(self.window_end)
            or self.window_end < self.window_start
            or self.confidence.availability != "available"
            or not _valid_evidence_ids(self.evidence_ids)
        ):
            raise ValueError("audience record is invalid")


@dataclass(frozen=True)
class BrainAdmission:
    availability_state: Literal["available", "unavailable"]
    validation_state: Literal["proposed", "validated", "rejected"]
    approval_state: Literal["not_required", "pending", "approved", "rejected"]
    ready_for_downstream: bool
    reason_codes: tuple[str, ...]
    decision_record_id: str | None

    def __post_init__(self) -> None:
        expected = (
            self.availability_state == "available"
            and self.validation_state == "validated"
            and self.approval_state in {"not_required", "approved"}
        )
        if self.ready_for_downstream is not expected:
            raise ValueError("admission decision is invalid")
        if (self.approval_state in {"approved", "rejected"}) != (
            self.decision_record_id is not None
        ):
            raise ValueError("decision record is invalid")


@dataclass(frozen=True, slots=True)
class BrainOutcomeRecord:
    outcome_id: str
    prediction_id: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.outcome_id or not self.prediction_id or not _utc(self.observed_at):
            raise ValueError("outcome observation is invalid")


@dataclass(frozen=True)
class BrainClaimRecord:
    claim_id: str
    claim_type: str
    statement: str
    statement_digest: str
    dependent_observation_ids: tuple[str, ...]
    supporting_evidence_ids: tuple[str, ...]
    challenging_evidence_ids: tuple[str, ...]
    scope_digest: str
    method_id: str
    confidence: BrainConfidence
    admission: BrainAdmission
    limitations: tuple[str, ...]
    claim_digest: str


@dataclass(frozen=True)
class IntelligenceBrainResult:
    contract_version: str
    run_id: str
    signal_id: str
    signal_date: str
    client_scope_id: str
    market_scope: tuple[str, ...]
    brand_config_id: str
    audience_lens_ids: tuple[str, ...]
    theme_id: str
    research_depth: str
    depth_policy_id: str
    depth_policy_digest: str
    as_of: datetime
    snapshot_id: str
    observer: object
    historian: object
    analyst: object
    skeptic: object
    strategist: object
    editor: object
    evidence_graph: object
    evidence_state: str
    overall_admission: BrainAdmission
    limitations: tuple[str, ...]
    missing_work: tuple[str, ...]
    model_usage_receipt_ids: tuple[str, ...]
    result_digest: str


__all__ = [
    "BRAIN_CONTRACT_VERSION",
    "DARK_DEPTH_POLICY",
    "DARK_DEPTH_POLICY_DIGEST",
    "AudienceAvailabilityRecord",
    "BrainAdmission",
    "BrainClaimRecord",
    "BrainConfidence",
    "BrainMeasurement",
    "BrainOutcomeRecord",
    "IntelligenceBrainIntent",
    "IntelligenceBrainResult",
    "canonical_bytes",
    "canonical_digest",
]
