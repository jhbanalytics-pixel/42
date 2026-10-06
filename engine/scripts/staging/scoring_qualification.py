"""Pure frozen evidence contracts for scoring qualification."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction
from types import MappingProxyType
from typing import Literal

from scripts.staging.replay_open_intelligence import (
    FutureLeakageDetected,
    ReplayInputBundle,
    validate_replay_input_bundle,
)
from src.analysis.open_intelligence.scoring import (
    DecisionStrengthRules,
    SignalScoreInput,
    score_signal,
)

_MARKETS = frozenset({"za", "ng", "ke"})
_DISCOVERY_MODES = frozenset({"dynamic", "replay", "canary"})
_STATES = frozenset({"measured", "unavailable"})
_COMPONENT_NAMES = frozenset(
    {
        "velocity",
        "breadth",
        "source_independence",
        "source_integrity",
        "geo_confidence",
        "qualifying_source_families",
        "qualifying_current_receipts",
    }
)
_GATE_NAMES = frozenset(
    {
        "evidence_state",
        "foreign_market_sample_reviewed",
        "foreign_market_leakage",
        "duplicate_identity",
        "factual_conflict",
        "directional_conflict",
        "membership_receipts_complete",
        "cluster_build_version",
    }
)
_BOOLEAN_GATES = frozenset(
    {
        "foreign_market_sample_reviewed",
        "duplicate_identity",
        "factual_conflict",
        "directional_conflict",
        "membership_receipts_complete",
    }
)

# The formula registry. An immutable module constant, deliberately not part of
# any bundle, rules object, fixture or provider output, so a caller cannot
# supply the authority that decides whether a factor is measurable.
#
# Every raw factor is unavailable. That is not a gap waiting to be filled with a
# default: the approved sources define no numerator unit, denominator universe,
# operation or source producer for any of the seven, so there is nothing to
# derive a value from. Missing stays missing until each decision is approved on
# its own.
_COMPONENT_MISSING_REASONS = MappingProxyType(
    {
        "velocity": "velocity_formula_unapproved",
        "breadth": "breadth_formula_unapproved",
        "source_independence": "source_independence_formula_unapproved",
        "source_integrity": "source_integrity_formula_unapproved",
        "geo_confidence": "geo_confidence_formula_unapproved",
        "qualifying_source_families": "qualifying_source_family_policy_unapproved",
        "qualifying_current_receipts": "qualifying_receipt_policy_unapproved",
    }
)
_GATE_MISSING_REASONS = MappingProxyType(
    {
        "evidence_state": "evidence_state_source_unavailable",
        "foreign_market_sample_reviewed": "foreign_market_review_design_unapproved",
        "foreign_market_leakage": "foreign_market_review_design_unapproved",
        "duplicate_identity": "duplicate_identity_universe_incomplete",
        "factual_conflict": "factual_conflict_provider_unapproved",
        "directional_conflict": "directional_conflict_provider_unapproved",
        "membership_receipts_complete": "membership_receipt_universe_incomplete",
        "cluster_build_version": "cluster_build_version_source_unavailable",
    }
)
# Authority decisions that are absent regardless of which candidate is asked
# about, so they are reported once per record rather than per factor.
_AUTHORITY_MISSING_REASONS = (
    "approved_cluster_versions_unavailable",
    "approved_receipt_cap_unavailable",
    "geo_floor_by_market_unavailable",
)
_SCORING_SOURCE_CONTRACT_VERSION = "replay_scoring_source_v1"
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SIGNAL_ID = re.compile(r"sig_[0-9a-f]{64}\Z")
_LOWER_TEXT = re.compile(r"[a-z0-9][a-z0-9_.-]*\Z")
_CANONICAL_INTEGER = re.compile(r"0|[1-9][0-9]*\Z")
# Graph identity only. component_proofs and gate_proofs were removed: they
# were the path by which a caller supplied a ratio the cited rows never
# supported, and they are output types now.
_AUTHORITY_FIELDS = frozenset(
    {
        "signal_id",
        "market",
        "signal_date",
        "discovery_mode",
    }
)
_QUANTUM = Decimal("0.000000000001")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    if value != value.strip():
        raise ValueError(f"{field} must not contain surrounding whitespace")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _tuple(value: object, field: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise ValueError(f"{field} must be a tuple")
    return value


def _count(value: object, field: str, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _fraction(
    value: object,
    field: str,
    *,
    nullable: bool = False,
) -> Fraction | None:
    if value is None and nullable:
        return None
    if type(value) is not Fraction or not 0 <= value <= 1:
        raise ValueError(f"{field} must be an exact fraction within zero and one")
    return value


def _decimal(
    value: object,
    field: str,
    *,
    nullable: bool = False,
    signed: bool = False,
) -> Decimal | None:
    if value is None and nullable:
        return None
    lower = Decimal("-1") if signed else Decimal("0")
    if type(value) is not Decimal or not value.is_finite() or not lower <= value <= 1:
        bounds = "negative one and one" if signed else "zero and one"
        raise ValueError(f"{field} must be a finite Decimal within {bounds}")
    return value


def _utc_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be an aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be a UTC datetime")
    return value.astimezone(UTC)


def _plain_date(value: object, field: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{field} must be a date")
    return value


def _window(
    start: datetime | None,
    end: datetime | None,
    *,
    required: bool,
) -> tuple[datetime | None, datetime | None]:
    if (start is None) != (end is None):
        raise ValueError("evidence window must provide both bounds")
    if required and start is None:
        raise ValueError("measured evidence requires a complete window")
    if start is None:
        return None, None
    normalized_start = _utc_datetime(start, "window start")
    normalized_end = _utc_datetime(end, "window end")
    if normalized_start > normalized_end:
        raise ValueError("evidence window cannot be reversed")
    return normalized_start, normalized_end


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _HEX_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} digest must be lower-case SHA256")
    return value


def _state(value: object) -> str:
    if value not in _STATES:
        raise ValueError("evidence state must be measured or unavailable")
    return value


def _reasons(value: object, field: str) -> tuple[str, ...]:
    items = _tuple(value, field)
    normalized = tuple(sorted(_text(item, field[:-1] or field) for item in items))
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field} must be unique")
    return normalized


def _row_references(value: object) -> tuple[ScoringRowReference, ...]:
    rows = _tuple(value, "source rows")
    if any(not isinstance(item, ScoringRowReference) for item in rows):
        raise ValueError("source rows must contain ScoringRowReference values")
    identities = tuple(item.row_id for item in rows)
    if len(identities) != len(set(identities)):
        raise ValueError("source row identities must be unique")
    return rows


def _pair_values(value: object, field: str) -> tuple[tuple[str, str], ...]:
    items = _tuple(value, field)
    normalized: list[tuple[str, str]] = []
    for item in items:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError(f"{field} must contain string pairs")
        normalized.append((_text(item[0], field), _text(item[1], field)))
    keys = tuple(key for key, _value in normalized)
    if len(keys) != len(set(keys)):
        raise ValueError(f"{field} identities must be unique")
    return tuple(sorted(normalized))


def _identity_fields(
    signal_id: object,
    market: object,
    signal_date: object,
    discovery_mode: object,
    rule_version: object,
) -> tuple[str, str, date, str, str]:
    if not isinstance(signal_id, str) or _SIGNAL_ID.fullmatch(signal_id) is None:
        raise ValueError("signal id is invalid")
    if market not in _MARKETS:
        raise ValueError("market is unsupported")
    normalized_date = _plain_date(signal_date, "signal date")
    if discovery_mode not in _DISCOVERY_MODES:
        raise ValueError("discovery mode is unsupported")
    return (
        signal_id,
        market,
        normalized_date,
        discovery_mode,
        _text(rule_version, "rule version"),
    )


@dataclass(frozen=True, slots=True)
class ScoringRowReference:
    row_id: str
    observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "row_id", _text(self.row_id, "row id"))
        object.__setattr__(
            self,
            "observed_at",
            _utc_datetime(self.observed_at, "row observed at"),
        )


@dataclass(frozen=True, slots=True)
class ScoringProofValue:
    name: str
    state: Literal["measured", "unavailable"]
    value: Fraction | None
    source_rows: tuple[ScoringRowReference, ...]
    window_start: datetime | None
    window_end: datetime | None
    numerator: int | None
    denominator: int | None
    missing_reason: str | None

    def __post_init__(self) -> None:
        if self.name not in _COMPONENT_NAMES:
            raise ValueError("component proof name is unsupported")
        state = _state(self.state)
        rows = _row_references(self.source_rows)
        start, end = _window(
            self.window_start,
            self.window_end,
            required=state == "measured",
        )
        value = _fraction(self.value, "component value", nullable=True)
        numerator = _count(self.numerator, "component numerator", nullable=True)
        denominator = _count(self.denominator, "component denominator", nullable=True)
        missing_reason = _optional_text(self.missing_reason, "component missing reason")
        if state == "measured":
            if value is None or not rows or numerator is None or denominator in (None, 0):
                raise ValueError("measured component evidence is incomplete")
            if missing_reason is not None:
                raise ValueError("measured component evidence cannot carry a missing reason")
            if Fraction(numerator, denominator) != value:
                raise ValueError("measured component ratio must equal its value")
            if any(not start <= row.observed_at <= end for row in rows):
                raise ValueError("measured component row must be inside its evidence window")
        else:
            if value is not None or numerator is not None or denominator is not None:
                raise ValueError("unavailable component evidence cannot carry results")
            if missing_reason is None:
                raise ValueError("unavailable component evidence requires a missing reason")
        object.__setattr__(self, "source_rows", rows)
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)
        object.__setattr__(self, "missing_reason", missing_reason)


@dataclass(frozen=True, slots=True)
class ScoringGateProof:
    name: str
    state: Literal["measured", "unavailable"]
    observed_value: str | None
    source_rows: tuple[ScoringRowReference, ...]
    window_start: datetime | None
    window_end: datetime | None
    missing_reason: str | None

    def __post_init__(self) -> None:
        if self.name not in _GATE_NAMES:
            raise ValueError("gate proof name is unsupported")
        state = _state(self.state)
        rows = _row_references(self.source_rows)
        start, end = _window(
            self.window_start,
            self.window_end,
            required=state == "measured",
        )
        observed = _optional_text(self.observed_value, "gate observed value")
        missing_reason = _optional_text(self.missing_reason, "gate missing reason")
        if state == "measured":
            if observed is None or not rows:
                raise ValueError("measured gate evidence is incomplete")
            if missing_reason is not None:
                raise ValueError("measured gate evidence cannot carry a missing reason")
            self._validate_observed_value(observed)
            if any(not start <= row.observed_at <= end for row in rows):
                raise ValueError("measured gate row must be inside its evidence window")
        else:
            if observed is not None:
                raise ValueError("unavailable gate evidence cannot carry a result")
            if missing_reason is None:
                raise ValueError("unavailable gate evidence requires a missing reason")
        object.__setattr__(self, "source_rows", rows)
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)
        object.__setattr__(self, "missing_reason", missing_reason)

    def _validate_observed_value(self, observed: str) -> None:
        if self.name in _BOOLEAN_GATES and observed not in {"true", "false"}:
            raise ValueError("boolean gate values must use canonical lower-case text")
        if self.name == "foreign_market_leakage" and _CANONICAL_INTEGER.fullmatch(observed) is None:
            raise ValueError("leakage gate value must use canonical base-10 text")
        if self.name == "evidence_state" and observed not in {
            "ready",
            "thin",
            "contradictory",
            "unchecked",
        }:
            raise ValueError("evidence state gate value is unsupported")
        if self.name == "cluster_build_version" and _LOWER_TEXT.fullmatch(observed) is None:
            raise ValueError("cluster build gate value must use canonical lower-case text")


@dataclass(frozen=True, slots=True)
class ReceiptCapCandidate:
    candidate_id: str
    cap: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _text(self.candidate_id, "candidate id"))
        cap = _count(self.cap, "receipt cap")
        if cap == 0:
            raise ValueError("receipt cap must be positive")


@dataclass(frozen=True, slots=True)
class GeoFloorCandidate:
    candidate_id: str
    za: Fraction
    ng: Fraction
    ke: Fraction

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _text(self.candidate_id, "candidate id"))
        for market in sorted(_MARKETS):
            object.__setattr__(
                self,
                market,
                _fraction(getattr(self, market), f"{market} geo floor"),
            )


@dataclass(frozen=True, slots=True)
class ScoringCandidateComparison:
    candidate_kind: Literal["formula", "receipt_cap", "geo_floor"]
    candidate_id: str
    parameters: tuple[tuple[str, str], ...]
    factors: tuple[tuple[str, str], ...]
    parameter_digest: str
    state: Literal["measured", "unavailable"]
    value: Decimal | None
    missing_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.candidate_kind not in {"formula", "receipt_cap", "geo_floor"}:
            raise ValueError("candidate kind is unsupported")
        object.__setattr__(self, "candidate_id", _text(self.candidate_id, "candidate id"))
        object.__setattr__(self, "parameters", _pair_values(self.parameters, "parameters"))
        object.__setattr__(self, "factors", _pair_values(self.factors, "factors"))
        object.__setattr__(
            self,
            "parameter_digest",
            _digest(self.parameter_digest, "parameter"),
        )
        state = _state(self.state)
        value = _decimal(self.value, "candidate value", nullable=True)
        reasons = _reasons(self.missing_reasons, "missing reasons")
        if state == "measured" and (value is None or reasons):
            raise ValueError("measured candidate comparison is incomplete")
        if state == "unavailable" and (value is not None or not reasons):
            raise ValueError("unavailable candidate comparison cannot carry a result")
        object.__setattr__(self, "missing_reasons", reasons)


@dataclass(frozen=True, slots=True)
class MathematicalScoreProof:
    trust_gate_eligible: bool
    velocity: Fraction | None
    breadth: Fraction | None
    source_independence: Fraction | None
    evidence_strength: Decimal | None
    geo_confidence: Fraction | None
    scorer_decision_strength: Decimal | None
    independent_decision_strength: Decimal | None
    scorer_matches_independent: bool | None
    missing_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.trust_gate_eligible) is not bool:
            raise ValueError("trust gate eligible must be boolean")
        for field in ("velocity", "breadth", "source_independence", "geo_confidence"):
            object.__setattr__(
                self,
                field,
                _fraction(getattr(self, field), field.replace("_", " "), nullable=True),
            )
        for field in (
            "evidence_strength",
            "scorer_decision_strength",
            "independent_decision_strength",
        ):
            object.__setattr__(
                self,
                field,
                _decimal(getattr(self, field), field.replace("_", " "), nullable=True),
            )
        if (
            self.scorer_matches_independent is not None
            and type(self.scorer_matches_independent) is not bool
        ):
            raise ValueError("scorer match state must be boolean")
        reasons = _reasons(self.missing_reasons, "missing reasons")
        components = (
            self.velocity,
            self.breadth,
            self.source_independence,
            self.evidence_strength,
            self.geo_confidence,
        )
        decisions = (
            self.scorer_decision_strength,
            self.independent_decision_strength,
        )
        if self.trust_gate_eligible:
            if any(value is None for value in components + decisions):
                raise ValueError("eligible mathematical proof is incomplete")
            if self.scorer_matches_independent is not True or reasons:
                raise ValueError("eligible mathematical proof must match without missing reasons")
        elif (
            any(value is not None for value in decisions)
            or self.scorer_matches_independent is not None
        ):
            raise ValueError(
                "ineligible mathematical proof cannot carry decision results or a match"
            )
        if not self.trust_gate_eligible and not reasons:
            raise ValueError("ineligible mathematical proof requires missing reasons")
        object.__setattr__(self, "missing_reasons", reasons)


@dataclass(frozen=True, slots=True)
class CohortDistribution:
    market: str
    signal_date: date
    discovery_mode: str
    rule_version: str
    eligible_count: int
    distribution: tuple[Decimal, ...]
    missing_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _signal_id, market, signal_date, discovery_mode, rule_version = _identity_fields(
            "sig_" + "0" * 64,
            self.market,
            self.signal_date,
            self.discovery_mode,
            self.rule_version,
        )
        count = _count(self.eligible_count, "eligible count")
        distribution = _tuple(self.distribution, "distribution")
        if any(_decimal(item, "distribution value") is None for item in distribution):
            raise ValueError("distribution values are invalid")
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "signal_date", signal_date)
        object.__setattr__(self, "discovery_mode", discovery_mode)
        object.__setattr__(self, "rule_version", rule_version)
        object.__setattr__(self, "eligible_count", count)
        object.__setattr__(self, "distribution", tuple(sorted(distribution)))
        object.__setattr__(
            self, "missing_reasons", _reasons(self.missing_reasons, "missing reasons")
        )


@dataclass(frozen=True, slots=True)
class CohortSignalPercentile:
    signal_id: str
    market: str
    signal_date: date
    discovery_mode: str
    rule_version: str
    eligible_count: int
    first_rank: int | None
    last_rank: int | None
    percentile: Decimal | None
    missing_reason: str | None

    def __post_init__(self) -> None:
        identity = _identity_fields(
            self.signal_id,
            self.market,
            self.signal_date,
            self.discovery_mode,
            self.rule_version,
        )
        count = _count(self.eligible_count, "eligible count")
        first = _count(self.first_rank, "first rank", nullable=True)
        last = _count(self.last_rank, "last rank", nullable=True)
        percentile = _decimal(self.percentile, "percentile", nullable=True)
        missing_reason = _optional_text(self.missing_reason, "percentile missing reason")
        if percentile is None:
            if first is not None or last is not None or missing_reason is None:
                raise ValueError("unavailable percentile cannot carry ranks")
        elif first is None or last is None or missing_reason is not None:
            raise ValueError("measured percentile is incomplete")
        elif first > last or last >= count:
            raise ValueError("percentile ranks are invalid")
        for field, value in zip(
            ("signal_id", "market", "signal_date", "discovery_mode", "rule_version"),
            identity,
            strict=True,
        ):
            object.__setattr__(self, field, value)
        object.__setattr__(self, "eligible_count", count)
        object.__setattr__(self, "missing_reason", missing_reason)


@dataclass(frozen=True, slots=True)
class CorrelationDiagnostic:
    left_component: str
    right_component: str
    state: Literal["measured", "unavailable"]
    coefficient: Decimal | None
    input_count: int
    included_count: int
    excluded_count: int
    missing_reason: str | None

    def __post_init__(self) -> None:
        if (
            self.left_component not in _COMPONENT_NAMES
            or self.right_component not in _COMPONENT_NAMES
        ):
            raise ValueError("correlation component is unsupported")
        if self.left_component == self.right_component:
            raise ValueError("correlation components must differ")
        state = _state(self.state)
        coefficient = _decimal(
            self.coefficient, "correlation coefficient", nullable=True, signed=True
        )
        input_count = _count(self.input_count, "correlation input count")
        included_count = _count(self.included_count, "correlation included count")
        excluded_count = _count(self.excluded_count, "correlation excluded count")
        missing_reason = _optional_text(self.missing_reason, "correlation missing reason")
        if included_count + excluded_count != input_count:
            raise ValueError("correlation counts must partition inputs")
        if state == "measured" and (coefficient is None or missing_reason is not None):
            raise ValueError("measured correlation is incomplete")
        if state == "unavailable" and (coefficient is not None or missing_reason is None):
            raise ValueError("unavailable correlation cannot carry a result")


@dataclass(frozen=True, slots=True)
class SourceDominanceDiagnostic:
    source_family: str
    state: Literal["measured", "unavailable"]
    share: Fraction | None
    numerator: int | None
    denominator: int | None
    missing_reason: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_family", _text(self.source_family, "source family"))
        state = _state(self.state)
        share = _fraction(self.share, "source dominance share", nullable=True)
        numerator = _count(self.numerator, "source dominance numerator", nullable=True)
        denominator = _count(self.denominator, "source dominance denominator", nullable=True)
        missing_reason = _optional_text(self.missing_reason, "source dominance missing reason")
        if state == "measured":
            if share is None or numerator is None or denominator in (None, 0):
                raise ValueError("measured source dominance is incomplete")
            if missing_reason is not None or Fraction(numerator, denominator) != share:
                raise ValueError("measured source dominance ratio is invalid")
        elif share is not None or numerator is not None or denominator is not None:
            raise ValueError("unavailable source dominance cannot carry results")
        elif missing_reason is None:
            raise ValueError("unavailable source dominance requires a missing reason")


@dataclass(frozen=True, slots=True)
class ScoringQualificationRecord:
    qualification_version: str
    source_snapshot_digest: str
    provider_bundle_digest: str
    rule_candidate_digest: str
    signal_id: str
    market: str
    signal_date: date
    discovery_mode: str
    rule_version: str
    component_proofs: tuple[ScoringProofValue, ...]
    gate_proofs: tuple[ScoringGateProof, ...]
    mathematical_proof: MathematicalScoreProof
    candidate_comparisons: tuple[ScoringCandidateComparison, ...]
    source_dominance: tuple[SourceDominanceDiagnostic, ...]
    missing_reasons: tuple[str, ...]
    canonical_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "qualification_version",
            _text(self.qualification_version, "qualification version"),
        )
        for field in (
            "source_snapshot_digest",
            "provider_bundle_digest",
            "rule_candidate_digest",
            "canonical_sha256",
        ):
            object.__setattr__(self, field, _digest(getattr(self, field), field))
        identity = _identity_fields(
            self.signal_id,
            self.market,
            self.signal_date,
            self.discovery_mode,
            self.rule_version,
        )
        components = _tuple(self.component_proofs, "component proofs")
        if any(not isinstance(item, ScoringProofValue) for item in components):
            raise ValueError("component proofs are invalid")
        if {item.name for item in components} != _COMPONENT_NAMES or len(components) != len(
            _COMPONENT_NAMES
        ):
            raise ValueError("component proofs must contain each exact name once")
        gates = _tuple(self.gate_proofs, "gate proofs")
        if any(not isinstance(item, ScoringGateProof) for item in gates):
            raise ValueError("gate proofs are invalid")
        if {item.name for item in gates} != _GATE_NAMES or len(gates) != len(_GATE_NAMES):
            raise ValueError("gate proofs must contain each exact name once")
        if not isinstance(self.mathematical_proof, MathematicalScoreProof):
            raise ValueError("mathematical proof is invalid")
        comparisons = _tuple(self.candidate_comparisons, "candidate comparisons")
        if any(not isinstance(item, ScoringCandidateComparison) for item in comparisons):
            raise ValueError("candidate comparisons are invalid")
        comparison_ids = tuple((item.candidate_kind, item.candidate_id) for item in comparisons)
        if len(comparison_ids) != len(set(comparison_ids)):
            raise ValueError("candidate comparison identities must be unique")
        dominance = _tuple(self.source_dominance, "source dominance")
        if any(not isinstance(item, SourceDominanceDiagnostic) for item in dominance):
            raise ValueError("source dominance values are invalid")
        families = tuple(item.source_family for item in dominance)
        if len(families) != len(set(families)):
            raise ValueError("source dominance identities must be unique")
        for field, value in zip(
            ("signal_id", "market", "signal_date", "discovery_mode", "rule_version"),
            identity,
            strict=True,
        ):
            object.__setattr__(self, field, value)
        object.__setattr__(
            self, "component_proofs", tuple(sorted(components, key=lambda item: item.name))
        )
        object.__setattr__(self, "gate_proofs", tuple(sorted(gates, key=lambda item: item.name)))
        object.__setattr__(
            self,
            "candidate_comparisons",
            tuple(sorted(comparisons, key=lambda item: (item.candidate_kind, item.candidate_id))),
        )
        object.__setattr__(
            self,
            "source_dominance",
            tuple(sorted(dominance, key=lambda item: item.source_family)),
        )
        object.__setattr__(
            self, "missing_reasons", _reasons(self.missing_reasons, "missing reasons")
        )


@dataclass(frozen=True, slots=True)
class ScoringQualificationBatch:
    qualification_version: str
    mode: Literal["fixture_dry_run"]
    source_snapshot_digest: str
    provider_bundle_digest: str
    rule_candidate_digest: str
    records: tuple[ScoringQualificationRecord, ...]
    cohort_distributions: tuple[CohortDistribution, ...]
    signal_percentiles: tuple[CohortSignalPercentile, ...]
    correlations: tuple[CorrelationDiagnostic, ...]
    missing_reasons: tuple[str, ...]
    certified: bool
    recommendation: None
    promotion_authorized: bool
    human_review_state: Literal["pending"]
    canonical_sha256: str

    def __post_init__(self) -> None:
        version = _text(self.qualification_version, "qualification version")
        if self.mode != "fixture_dry_run":
            raise ValueError("qualification mode must remain fixture_dry_run")
        digests = {
            field: _digest(getattr(self, field), field)
            for field in (
                "source_snapshot_digest",
                "provider_bundle_digest",
                "rule_candidate_digest",
                "canonical_sha256",
            )
        }
        records = _tuple(self.records, "records")
        if any(not isinstance(item, ScoringQualificationRecord) for item in records):
            raise ValueError("qualification records are invalid")
        identities = tuple(
            (item.signal_id, item.market, item.signal_date, item.discovery_mode, item.rule_version)
            for item in records
        )
        if len(identities) != len(set(identities)):
            raise ValueError("qualification record identities must be unique")
        for item in records:
            if item.qualification_version != version:
                raise ValueError("record qualification version differs from batch")
            for field in (
                "source_snapshot_digest",
                "provider_bundle_digest",
                "rule_candidate_digest",
            ):
                if getattr(item, field) != digests[field]:
                    raise ValueError(f"record {field.replace('_', ' ')} differs from batch")
        collections = (
            (self.cohort_distributions, CohortDistribution, "cohort distributions"),
            (self.signal_percentiles, CohortSignalPercentile, "signal percentiles"),
            (self.correlations, CorrelationDiagnostic, "correlations"),
        )
        for values, expected_type, field in collections:
            items = _tuple(values, field)
            if any(not isinstance(item, expected_type) for item in items):
                raise ValueError(f"{field} are invalid")
        if type(self.certified) is not bool or self.certified:
            raise ValueError("fixture qualification cannot be certified")
        if self.recommendation is not None:
            raise ValueError("fixture qualification cannot carry a recommendation")
        if type(self.promotion_authorized) is not bool or self.promotion_authorized:
            raise ValueError("fixture qualification cannot authorize promotion")
        if self.human_review_state != "pending":
            raise ValueError("fixture qualification must remain pending human review")
        object.__setattr__(self, "qualification_version", version)
        for field, value in digests.items():
            object.__setattr__(self, field, value)
        object.__setattr__(
            self,
            "records",
            tuple(sorted(records, key=lambda item: identities[records.index(item)])),
        )
        object.__setattr__(
            self, "missing_reasons", _reasons(self.missing_reasons, "missing reasons")
        )


def _canonical_value(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        return {"float_hex": value.hex()}
    if isinstance(value, Fraction):
        return {"numerator": value.numerator, "denominator": value.denominator}
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return _utc_datetime(value, "canonical datetime").isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _canonical_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("canonical mapping keys must be strings")
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    raise ValueError("canonical value type is unsupported")


def _canonical_json(value: object) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _source_rows(bundle: ReplayInputBundle) -> dict[str, tuple[datetime, str]]:
    rows_by_id: dict[str, tuple[datetime, str]] = {}
    id_and_time_fields = {
        "enriched_content": ("id", "collected_at"),
        "event_ledger": ("ledger_id", "as_of"),
        "raw_content": ("id", "collected_at"),
    }
    for table, fields_by_table in id_and_time_fields.items():
        row_id_field, timestamp_field = fields_by_table
        for row in bundle.source_snapshot["rows_by_table"][table]:
            row_id = row[row_id_field]
            observed_at = row[timestamp_field]
            if not isinstance(row_id, str) or not isinstance(row.get("market"), str):
                raise ValueError("source row identity is invalid")
            if isinstance(observed_at, str):
                observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            observed = _utc_datetime(observed_at, "source row timestamp")
            if row_id in rows_by_id:
                raise ValueError("source row identities must be globally unique")
            rows_by_id[row_id] = (observed, row["market"])
    return rows_by_id


def _unavailable_component(name: str, reason: str) -> ScoringProofValue:
    return ScoringProofValue(
        name=name,
        state="unavailable",
        value=None,
        source_rows=(),
        window_start=None,
        window_end=None,
        numerator=None,
        denominator=None,
        missing_reason=reason,
    )


def _unavailable_gate(name: str, reason: str) -> ScoringGateProof:
    return ScoringGateProof(
        name=name,
        state="unavailable",
        observed_value=None,
        source_rows=(),
        window_start=None,
        window_end=None,
        missing_reason=reason,
    )


def _bound_rows(
    rows: tuple[ScoringRowReference, ...],
    source_rows: Mapping[str, tuple[datetime, str]],
    market: str,
    cutoff_end: datetime,
) -> tuple[ScoringRowReference, ...]:
    normalized = tuple(sorted(rows, key=lambda item: (item.row_id, item.observed_at)))
    for row in normalized:
        if row.observed_at > cutoff_end:
            raise FutureLeakageDetected("scoring proof row is after the replay cutoff")
        source = source_rows.get(row.row_id)
        if source is None:
            raise ValueError("scoring proof row identity is absent from the source snapshot")
        if source != (row.observed_at, market):
            raise ValueError("scoring proof row facts differ from the source snapshot")
    return normalized


def _normalize_component(
    name: str,
    value: object | None,
    source_rows: Mapping[str, tuple[datetime, str]],
    market: str,
    cutoff_end: datetime,
    rules: DecisionStrengthRules,
) -> ScoringProofValue:
    reason = f"{name}_unavailable"
    if value is None:
        return _unavailable_component(name, reason)
    if not isinstance(value, ScoringProofValue) or value.name != name:
        raise ValueError("component proof authority is invalid")
    if value.state == "unavailable":
        return ScoringProofValue(
            name=value.name,
            state=value.state,
            value=value.value,
            source_rows=tuple(value.source_rows),
            window_start=value.window_start,
            window_end=value.window_end,
            numerator=value.numerator,
            denominator=value.denominator,
            missing_reason=value.missing_reason,
        )
    if (
        value.value is None
        or not value.source_rows
        or value.window_start is None
        or value.window_end is None
        or value.numerator is None
        or value.denominator is None
    ):
        return _unavailable_component(name, reason)
    if value.window_end > cutoff_end:
        raise FutureLeakageDetected("scoring proof window is after the replay cutoff")
    if value.numerator > value.denominator:
        raise ValueError("component proof numerator exceeds its denominator")
    if name == "qualifying_source_families" and value.denominator != 4:
        raise ValueError("source family denominator must remain four")
    if name == "qualifying_current_receipts" and value.denominator != rules.approved_receipt_cap:
        raise ValueError("current receipt denominator must equal the approved cap")
    rows = _bound_rows(tuple(value.source_rows), source_rows, market, cutoff_end)
    return ScoringProofValue(
        name=value.name,
        state=value.state,
        value=value.value,
        source_rows=rows,
        window_start=value.window_start,
        window_end=value.window_end,
        numerator=value.numerator,
        denominator=value.denominator,
        missing_reason=value.missing_reason,
    )


def _normalize_gate(
    name: str,
    value: object | None,
    source_rows: Mapping[str, tuple[datetime, str]],
    market: str,
    cutoff_end: datetime,
) -> ScoringGateProof:
    reason = f"{name}_unavailable"
    if value is None:
        return _unavailable_gate(name, reason)
    if not isinstance(value, ScoringGateProof) or value.name != name:
        raise ValueError("gate proof authority is invalid")
    if value.state == "unavailable":
        return ScoringGateProof(
            name=value.name,
            state=value.state,
            observed_value=value.observed_value,
            source_rows=tuple(value.source_rows),
            window_start=value.window_start,
            window_end=value.window_end,
            missing_reason=value.missing_reason,
        )
    if (
        value.observed_value is None
        or not value.source_rows
        or value.window_start is None
        or value.window_end is None
    ):
        return _unavailable_gate(name, reason)
    if value.window_end > cutoff_end:
        raise FutureLeakageDetected("scoring gate window is after the replay cutoff")
    rows = _bound_rows(tuple(value.source_rows), source_rows, market, cutoff_end)
    return ScoringGateProof(
        name=value.name,
        state=value.state,
        observed_value=value.observed_value,
        source_rows=rows,
        window_start=value.window_start,
        window_end=value.window_end,
        missing_reason=value.missing_reason,
    )


def _gate_value(proof: ScoringGateProof) -> object | None:
    if proof.state != "measured" or proof.observed_value is None:
        return None
    if proof.name in _BOOLEAN_GATES:
        return proof.observed_value == "true"
    if proof.name == "foreign_market_leakage":
        return int(proof.observed_value)
    return proof.observed_value


def _rules_digest(rules: DecisionStrengthRules) -> str:
    return _canonical_sha256(
        {
            "approved_receipt_cap": rules.approved_receipt_cap,
            "geo_floor_by_market": {
                market: Fraction(str(rules.geo_floor_by_market[market]))
                for market in sorted(rules.geo_floor_by_market)
            },
            "approved_cluster_build_versions": tuple(sorted(rules.approved_cluster_build_versions)),
            "rule_version": rules.rule_version,
        }
    )


def _independent_strength(
    components: Mapping[str, ScoringProofValue],
) -> tuple[Decimal, Decimal]:
    def decimal_fraction(value: Fraction) -> Decimal:
        return Decimal(value.numerator) / Decimal(value.denominator)

    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        evidence_product = (
            decimal_fraction(components["qualifying_source_families"].value)
            * decimal_fraction(components["qualifying_current_receipts"].value)
            * decimal_fraction(components["source_integrity"].value)
        )
        evidence = evidence_product ** (Decimal(1) / Decimal(3))
        decision_product = (
            decimal_fraction(components["velocity"].value)
            * decimal_fraction(components["breadth"].value)
            * decimal_fraction(components["source_independence"].value)
            * evidence
            * decimal_fraction(components["geo_confidence"].value)
        )
        decision = decision_product ** (Decimal(1) / Decimal(5))
        return evidence.quantize(_QUANTUM), decision.quantize(_QUANTUM)


def _record_with_digest(**values: object) -> ScoringQualificationRecord:
    record = ScoringQualificationRecord(canonical_sha256="0" * 64, **values)
    payload = {
        field.name: getattr(record, field.name)
        for field in fields(record)
        if field.name != "canonical_sha256"
    }
    return replace(record, canonical_sha256=_canonical_sha256(payload))


def _formula_registry_digest() -> str:
    """The digest of the immutable registry, including its unavailable entries."""
    return _canonical_sha256(
        {
            "contract_version": _SCORING_SOURCE_CONTRACT_VERSION,
            "components": dict(_COMPONENT_MISSING_REASONS),
            "gates": dict(_GATE_MISSING_REASONS),
            "authority": _AUTHORITY_MISSING_REASONS,
        }
    )


def _scoring_source_reasons(bundle: ReplayInputBundle, candidate_id: str) -> tuple[str, ...]:
    """Why source authority is unavailable for this candidate.

    None, an empty mapping and an absent candidate are all unavailable. None of
    them is zero, and none authorizes a call to score_signal.
    """
    reasons: list[str] = ["shared_run_receipt_unavailable"]
    sources = bundle.scoring_sources_by_candidate
    if sources is not None:
        if not isinstance(sources, Mapping):
            raise ValueError("scoring sources must be a mapping")
        entry = sources.get(candidate_id)
        if entry is not None:
            # A partial source bundle is invalid rather than unavailable, so a
            # caller cannot hide an incomplete set behind a missing reason.
            raise ValueError(
                "scoring source bundles are unavailable until the source "
                "authority producer is approved"
            )
    if not _window_complete(bundle.current_window):
        reasons.append("incomplete_current_window")
    if not _window_complete(bundle.history_window):
        reasons.append("incomplete_history_window")
    return tuple(reasons)


def _observed_markets(bundle: ReplayInputBundle) -> frozenset[str]:
    """Markets the validated source rows actually carry."""
    rows_by_table = bundle.source_snapshot.get("rows_by_table")
    if not isinstance(rows_by_table, Mapping):
        return frozenset()
    observed: set[str] = set()
    for rows in rows_by_table.values():
        if not isinstance(rows, (list, tuple)):
            continue
        for row in rows:
            if isinstance(row, Mapping):
                market = row.get("market")
                if isinstance(market, str):
                    observed.add(market)
    return frozenset(observed)


def _window_complete(window: object) -> bool:
    if not isinstance(window, Mapping):
        return bool(getattr(window, "complete_partitions", False))
    return bool(window.get("complete_partitions", False))


def build_scoring_proof(
    bundle: ReplayInputBundle,
    candidate_id: str,
) -> ScoringQualificationRecord:
    """Scoring qualification for one candidate.

    There is no value input path. Every raw factor and gate is unavailable
    under the first formula registry, so this never calls score_signal and
    never produces a score. That is the correct answer today, not a limitation
    to be worked around: no approved source defines the units these factors
    would be derived from.
    """
    validated = validate_replay_input_bundle(bundle)
    candidate_id = _text(candidate_id, "candidate id")
    authority = validated.components_by_candidate.get(candidate_id)
    if not isinstance(authority, Mapping) or set(authority) != _AUTHORITY_FIELDS:
        raise ValueError("scoring proof authority fields are invalid")
    signal_id, market, signal_date, discovery_mode, rule_version = _identity_fields(
        authority["signal_id"],
        authority["market"],
        authority["signal_date"],
        authority["discovery_mode"],
        _SCORING_SOURCE_CONTRACT_VERSION,
    )
    if signal_id != candidate_id:
        raise ValueError("scoring proof signal identity differs from candidate identity")
    if signal_date != validated.cutoff or discovery_mode != "replay":
        raise ValueError("scoring proof identity differs from replay authority")
    if market not in _observed_markets(validated):
        # The market must be one the validated source rows actually observed.
        # Scope alone is not enough: a run scoped to three markets still only
        # holds evidence for the markets its rows came from.
        raise ValueError("scoring proof market is absent from the validated source rows")

    components = tuple(
        ScoringProofValue(
            name=name,
            state="unavailable",
            value=None,
            source_rows=(),
            window_start=None,
            window_end=None,
            numerator=None,
            denominator=None,
            missing_reason=_COMPONENT_MISSING_REASONS[name],
        )
        for name in sorted(_COMPONENT_NAMES)
    )
    gates = tuple(
        ScoringGateProof(
            name=name,
            state="unavailable",
            observed_value=None,
            source_rows=(),
            window_start=None,
            window_end=None,
            missing_reason=_GATE_MISSING_REASONS[name],
        )
        for name in sorted(_GATE_NAMES)
    )
    missing_reasons = tuple(
        sorted(
            {
                *(item.missing_reason for item in components),
                *(item.missing_reason for item in gates),
                *_AUTHORITY_MISSING_REASONS,
                *_scoring_source_reasons(validated, candidate_id),
            }
        )
    )
    mathematical = MathematicalScoreProof(
        trust_gate_eligible=False,
        velocity=None,
        breadth=None,
        source_independence=None,
        evidence_strength=None,
        geo_confidence=None,
        scorer_decision_strength=None,
        independent_decision_strength=None,
        scorer_matches_independent=None,
        missing_reasons=missing_reasons,
    )
    providers = tuple(sorted(validated.provider_outputs, key=_canonical_json))
    return _record_with_digest(
        qualification_version="scoring_qualification_v1",
        source_snapshot_digest=_canonical_sha256(validated.source_snapshot),
        provider_bundle_digest=_canonical_sha256(providers),
        rule_candidate_digest=_formula_registry_digest(),
        signal_id=signal_id,
        market=market,
        signal_date=signal_date,
        discovery_mode=discovery_mode,
        rule_version=rule_version,
        component_proofs=components,
        gate_proofs=gates,
        mathematical_proof=mathematical,
        candidate_comparisons=(),
        source_dominance=(),
        missing_reasons=missing_reasons,
    )


def unapproved_component_reasons() -> tuple[str, ...]:
    """The exact missing reasons for every raw factor, sorted.

    Public because the apply runner must name why a component was skipped,
    and the registry is the one authority on that. Nothing here approves a
    formula; it only reads the registry's recorded absences.
    """
    return tuple(sorted(set(_COMPONENT_MISSING_REASONS.values())))


# --- Approved raw factor formulas ---------------------------------------------
#
# Approved by Albert on 2026-08-29 via
# 42-raw-factor-formula-approval-request-2026-08-29.md: seven formula rows and
# three authority constants. Every value is the exact
# Fraction(len(numerator), len(denominator)) over content-addressed
# citing-evidence unit sets; a numerator must be a subset of its denominator;
# an empty denominator is unavailable, never zero. Novelty was not part of
# that approval and stays unapproved until its own sentence arrives, so no
# metrics object can exist yet: the gate is recorded here rather than
# scaffolded around.

APPROVED_COMPONENT_FORMULAS = MappingProxyType(
    {
        "velocity": "velocity_current_share_v1",
        "breadth": "breadth_family_coverage_v1",
        "source_independence": "independence_distinct_creators_v1",
        "source_integrity": "integrity_clean_receipts_v1",
        "geo_confidence": "geo_best_citing_post_v1",
        "qualifying_source_families": "qualifying_family_policy_v1",
        "qualifying_current_receipts": "qualifying_receipt_policy_v1",
    }
)
APPROVED_RECEIPT_CAP = 8
# Approved by Albert on 2026-09-03: the floor sits on regional_score's own saturating
# scale, hits / (hits + 1.5) over the market's regional markers, so 0.6 is three
# distinct markers in one item; 0.8 needed six and no observed row reached it.
APPROVED_GEO_FLOORS = MappingProxyType({"za": 0.6, "ng": 0.6, "ke": 0.6})
APPROVED_CLUSTER_VERSIONS = frozenset({"hybrid_graph_v2"})
# Approved by Albert on 2026-08-30: novelty is the share of a signal's
# member terms with no citing evidence before the signal date, same
# set-ratio law and boundary as the seven approved formulas.
NOVELTY_FORMULA_ID = "novelty_new_member_share_v1"


def approved_decision_strength_rules() -> DecisionStrengthRules:
    return DecisionStrengthRules(
        approved_receipt_cap=APPROVED_RECEIPT_CAP,
        geo_floor_by_market=APPROVED_GEO_FLOORS,
        approved_cluster_build_versions=tuple(APPROVED_CLUSTER_VERSIONS),
        rule_version="audience_neutral_decision_strength_v1",
    )


@dataclass(frozen=True, slots=True)
class ComponentFactorEvidence:
    """The unit identity sets one component's factors derive from.

    Every element is a content-addressed key of a citing-evidence row (or a
    family, creator or member-term identity) from the copied staging source
    tables. The derivation below consumes only these sets; no caller can
    hand it a value.
    """

    current_rows: tuple[str, ...]
    window_rows: tuple[str, ...]
    families_current: tuple[str, ...]
    family_universe: tuple[str, ...]
    creators_current: tuple[str, ...]
    clean_current: tuple[str, ...]
    geo_confirmed_current: tuple[str, ...]
    current_member_terms: tuple[str, ...]
    history_member_terms: tuple[str, ...]
    # Approved by Albert on 2026-09-03: the family direction test counts every
    # row the window's term match cites for the component, (family, current,
    # baseline) by published date, not the sampled receipts that carry the
    # displayed evidence. Sorted by family; a family absent here counts 0 and 0.
    family_direction_counts: tuple[tuple[str, int, int], ...] = ()
    # Ruled by Albert on 2026-09-04: geo confidence is the best citing post's
    # regional score over the window, on regional_score's own saturating
    # scale, so APPROVED_GEO_FLOORS compares like with like. A window with no
    # scored post reads 0.0.
    geo_best_regional_score: float = 0.0

    def __post_init__(self) -> None:
        value = self.geo_best_regional_score
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("geo best regional score must be a number")
        if not 0.0 <= float(value) <= 1.0:
            raise ValueError("geo best regional score must lie within zero and one")
        object.__setattr__(self, "geo_best_regional_score", float(value))


def _unit_set(values: tuple[str, ...], name: str) -> frozenset[str]:
    units = frozenset(values)
    if len(units) != len(values):
        raise ValueError(f"{name} unit set carries duplicates")
    return units


def derive_component_factor_ratios(
    evidence: ComponentFactorEvidence,
) -> dict[str, Fraction]:
    """The five approved raw factors plus the two qualifying counts.

    Raises on a numerator escaping its denominator; returns only for fully
    measurable evidence. Callers that need unavailable-tolerance use
    derive_component_factor_metrics, which names each absence instead.
    """
    current = _unit_set(evidence.current_rows, "current rows")
    window = _unit_set(evidence.window_rows, "window rows")
    families = _unit_set(evidence.families_current, "current families")
    universe = _unit_set(evidence.family_universe, "family universe")
    creators = _unit_set(evidence.creators_current, "current creators")
    clean = _unit_set(evidence.clean_current, "clean rows")
    confirmed = _unit_set(evidence.geo_confirmed_current, "geo confirmed rows")
    if not current <= window:
        raise ValueError("current rows must be a subset of the window rows")
    if not families <= universe:
        raise ValueError("current families must be a subset of the universe")
    if not clean <= current:
        raise ValueError("clean rows must be a subset of the current rows")
    if not confirmed <= current:
        raise ValueError("geo confirmed rows must be a subset of the current rows")
    if not window or not universe or not current or not creators:
        raise ValueError("factor denominators must be nonempty")
    return {
        "velocity": Fraction(len(current), len(window)),
        "breadth": Fraction(len(families), len(universe)),
        # Distinct voices over citing rows would exceed one only if a row
        # carried several creators, which a row cannot; distinct creators
        # over rows measures repetition by one voice directly.
        "source_independence": Fraction(len(creators), len(current)),
        "source_integrity": Fraction(len(clean), len(current)),
        "geo_confidence": Fraction(evidence.geo_best_regional_score),
        "qualifying_source_families": Fraction(len(families), 1),
        "qualifying_current_receipts": Fraction(len(clean), 1),
    }


def derive_component_factor_metrics(
    evidence: ComponentFactorEvidence,
) -> tuple[object | None, tuple[str, ...]]:
    """ObservedSignalMetrics for one component, or None with named reasons.

    Unavailable stays unavailable: an empty denominator names its absence,
    and while the novelty formula is unapproved no metrics object can exist
    at all, because a candidate row cannot carry a novelty score nobody has
    the authority to derive.
    """
    reasons: list[str] = []
    if not evidence.window_rows or not evidence.current_rows:
        reasons.append("no_citing_evidence_in_window")
    if not evidence.family_universe:
        reasons.append("family_universe_unavailable")
    if evidence.current_rows and not evidence.creators_current:
        reasons.append("no_attributable_creator_in_current_window")
    if NOVELTY_FORMULA_ID is None:
        reasons.append("novelty_formula_unapproved")
    if reasons:
        return None, tuple(sorted(set(reasons)))
    factors = derive_component_factor_ratios(evidence)
    novelty = _derive_novelty(evidence)
    # A plain mapping, so this module keeps its pure import boundary; the
    # replay side constructs the rows-layer metrics object from it.
    metrics = MappingProxyType(
        {
            "novelty_score": float(novelty),
            "velocity_score": float(factors["velocity"]),
            "breadth_score": float(factors["breadth"]),
            "independence_score": float(factors["source_independence"]),
            "historical_similarity": None,
            "geo_confidence": float(factors["geo_confidence"]),
        }
    )
    return metrics, ()


def _derive_novelty(evidence: ComponentFactorEvidence) -> Fraction:
    """novelty_new_member_share_v1, callable only once approved.

    The share of the signal's member terms absent from the history window:
    Fraction(len(current terms - history-only terms), len(current terms)).
    """
    if NOVELTY_FORMULA_ID is None:
        raise ValueError("the novelty formula is unapproved")
    current_terms = _unit_set(evidence.current_member_terms, "current member terms")
    history_terms = _unit_set(evidence.history_member_terms, "history member terms")
    if not current_terms:
        raise ValueError("member term sets are invalid")
    new_terms = current_terms - history_terms
    return Fraction(len(new_terms), len(current_terms))
