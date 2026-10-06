"""Deterministic receipt-backed historical analogue matching."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

_TRAJECTORY_STATES = frozenset({"rising", "flat", "falling"})
_EVIDENCE_STATES = frozenset({"ready", "thin", "contradictory", "unchecked"})


class HistoricalLeakage(ValueError):
    pass


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _strings(value: object, field: str, *, nonempty: bool, unique: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{field} must be a sequence")
    items = tuple(_text(item, field) for item in value)
    if nonempty and not items:
        raise ValueError(f"{field} must be nonempty")
    if unique and len(items) != len(set(items)):
        raise ValueError(f"{field} must be unique")
    return items


def _bounded(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise ValueError(f"{field} must be finite within zero and one")
    return float(value)


@dataclass(frozen=True, slots=True)
class AnalogueReceipt:
    receipt_id: str
    signal_id: str
    published_at: datetime
    source_family: str
    market: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipt_id", _text(self.receipt_id, "receipt id"))
        object.__setattr__(self, "signal_id", _text(self.signal_id, "signal id"))
        if (
            not isinstance(self.published_at, datetime)
            or self.published_at.tzinfo is None
            or self.published_at.utcoffset() is None
        ):
            raise ValueError("receipt published at must be timezone aware")
        object.__setattr__(self, "published_at", self.published_at.astimezone(UTC))
        object.__setattr__(self, "source_family", _text(self.source_family, "source family"))
        object.__setattr__(self, "market", _text(self.market, "market"))
        if self.market != self.market.lower():
            raise ValueError("receipt market must be lower case")


@dataclass(frozen=True, slots=True)
class HistoricalSignalSnapshot:
    signal_id: str
    as_of: datetime
    market: str
    terms: tuple[str, ...]
    source_families: tuple[str, ...]
    trajectory_signature: tuple[str, ...]
    evidence_state: str
    receipts: tuple[AnalogueReceipt, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_id", _text(self.signal_id, "signal id"))
        if (
            not isinstance(self.as_of, datetime)
            or self.as_of.tzinfo is None
            or self.as_of.utcoffset() is None
        ):
            raise ValueError("snapshot as of must be timezone aware")
        object.__setattr__(self, "as_of", self.as_of.astimezone(UTC))
        object.__setattr__(self, "market", _text(self.market, "market"))
        if self.market != self.market.lower():
            raise ValueError("snapshot market must be lower case")
        object.__setattr__(self, "terms", _strings(self.terms, "terms", nonempty=True))
        object.__setattr__(
            self,
            "source_families",
            _strings(self.source_families, "source families", nonempty=True),
        )
        signature = _strings(
            self.trajectory_signature,
            "trajectory signature",
            nonempty=True,
            unique=False,
        )
        if any(state not in _TRAJECTORY_STATES for state in signature):
            raise ValueError("trajectory signature is invalid")
        object.__setattr__(self, "trajectory_signature", signature)
        if self.evidence_state not in _EVIDENCE_STATES:
            raise ValueError("evidence state is invalid")
        if not isinstance(self.receipts, (tuple, list)) or any(
            not isinstance(item, AnalogueReceipt) for item in self.receipts
        ):
            raise ValueError("snapshot receipts are invalid")
        receipts = tuple(self.receipts)
        if len({item.receipt_id for item in receipts}) != len(receipts):
            raise ValueError("snapshot receipt ids must be unique")
        object.__setattr__(self, "receipts", receipts)


@dataclass(frozen=True, slots=True)
class HistoricalAnalogueRules:
    term_weight: float
    source_weight: float
    trajectory_weight: float
    geography_weight: float
    minimum_similarity: float
    allow_cross_market: bool
    minimum_trajectory_points: int
    eligible_evidence_states: tuple[str, ...]

    def __post_init__(self) -> None:
        weights = (
            self.term_weight,
            self.source_weight,
            self.trajectory_weight,
            self.geography_weight,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
            for value in weights
        ) or not math.isclose(sum(float(value) for value in weights), 1.0, abs_tol=1e-9):
            raise ValueError("similarity weights must be finite and sum to one")
        for field in (
            "term_weight",
            "source_weight",
            "trajectory_weight",
            "geography_weight",
        ):
            object.__setattr__(self, field, float(getattr(self, field)))
        object.__setattr__(
            self,
            "minimum_similarity",
            _bounded(self.minimum_similarity, "minimum similarity"),
        )
        if type(self.allow_cross_market) is not bool:
            raise ValueError("allow cross market must be boolean")
        if (
            isinstance(self.minimum_trajectory_points, bool)
            or not isinstance(self.minimum_trajectory_points, int)
            or self.minimum_trajectory_points < 1
        ):
            raise ValueError("minimum trajectory points must be positive")
        eligible = _strings(
            self.eligible_evidence_states, "eligible evidence states", nonempty=True
        )
        if any(state not in _EVIDENCE_STATES for state in eligible):
            raise ValueError("eligible evidence state is invalid")
        object.__setattr__(self, "eligible_evidence_states", eligible)


@dataclass(frozen=True, slots=True)
class HistoricalAnalogueMatch:
    signal_id: str
    as_of: datetime
    market: str
    similarity: float
    term_similarity: float
    source_similarity: float
    trajectory_similarity: float
    geography_similarity: float
    difference_codes: tuple[str, ...]
    receipt_ids: tuple[str, ...]
    transfer_limits: tuple[str, ...]

    @property
    def what_is_different_now(self) -> tuple[str, ...]:
        return self.difference_codes


@dataclass(frozen=True, slots=True)
class AnalogueRejection:
    signal_id: str
    as_of: datetime
    market: str
    reason: str


@dataclass(frozen=True, slots=True)
class HistoricalAnalogueSearchResult:
    matches: tuple[HistoricalAnalogueMatch, ...]
    rejections: tuple[AnalogueRejection, ...]


def _jaccard(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    left_set = set(left)
    right_set = set(right)
    return len(left_set.intersection(right_set)) / len(left_set.union(right_set))


def _trajectory_similarity(
    left: tuple[str, ...], right: tuple[str, ...], minimum_points: int
) -> float | None:
    if len(left) != len(right) or len(left) < minimum_points:
        return None
    matches = sum(a == b for a, b in zip(left, right, strict=True))
    return matches / len(left)


def _validate_receipts(snapshot: HistoricalSignalSnapshot) -> tuple[str, ...]:
    if not snapshot.receipts:
        raise ValueError("missing_receipts")
    for item in snapshot.receipts:
        if item.signal_id != snapshot.signal_id:
            raise ValueError("receipt_signal_mismatch")
        if item.market != snapshot.market:
            raise ValueError("receipt_market_mismatch")
        if item.source_family not in snapshot.source_families:
            raise ValueError("receipt_source_family_mismatch")
        if item.published_at > snapshot.as_of:
            raise HistoricalLeakage("future_receipt")
    if {item.source_family for item in snapshot.receipts} != set(snapshot.source_families):
        raise ValueError("source_receipt_coverage_incomplete")
    return tuple(sorted(item.receipt_id for item in snapshot.receipts))


def _differences(
    current: HistoricalSignalSnapshot, candidate: HistoricalSignalSnapshot
) -> tuple[str, ...]:
    output = []
    if set(current.terms) != set(candidate.terms):
        output.append("terms_changed")
    if set(current.source_families) != set(candidate.source_families):
        output.append("source_mix_changed")
    if current.trajectory_signature != candidate.trajectory_signature:
        output.append("trajectory_changed")
    if current.market != candidate.market:
        output.append("market_changed")
    if current.evidence_state != candidate.evidence_state:
        output.append("evidence_state_changed")
    return tuple(output)


def _transfer_limits(difference_codes: tuple[str, ...]) -> tuple[str, ...]:
    mapping = {
        "market_changed": "market_context_not_transferable",
        "source_mix_changed": "source_mix_not_transferable",
        "evidence_state_changed": "evidence_state_not_transferable",
        "trajectory_changed": "time_regime_not_transferable",
    }
    return tuple(mapping[code] for code in difference_codes if code in mapping)


def find_historical_analogues(
    current: HistoricalSignalSnapshot,
    candidates: object,
    rules: HistoricalAnalogueRules,
    *,
    limit: int,
) -> HistoricalAnalogueSearchResult:
    if not isinstance(current, HistoricalSignalSnapshot):
        raise ValueError("current snapshot is invalid")
    if not isinstance(rules, HistoricalAnalogueRules):
        raise ValueError("historical analogue rules are invalid")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("limit must be between one and twenty")
    _validate_receipts(current)
    try:
        items = tuple(candidates)
    except TypeError as error:
        raise ValueError("candidate snapshots must be iterable") from error
    if any(not isinstance(item, HistoricalSignalSnapshot) for item in items):
        raise ValueError("candidate snapshots are invalid")
    identities = tuple((item.signal_id, item.as_of, item.market) for item in items)
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate historical candidate identity")
    matches: list[HistoricalAnalogueMatch] = []
    rejections: list[AnalogueRejection] = []

    def reject(candidate: HistoricalSignalSnapshot, reason: str) -> None:
        rejections.append(
            AnalogueRejection(candidate.signal_id, candidate.as_of, candidate.market, reason)
        )

    for candidate in items:
        if candidate.signal_id == current.signal_id:
            reject(candidate, "self_match")
            continue
        if candidate.as_of >= current.as_of:
            reject(candidate, "candidate_not_prior")
            continue
        if candidate.market != current.market and not rules.allow_cross_market:
            reject(candidate, "market_not_allowed")
            continue
        if candidate.evidence_state not in rules.eligible_evidence_states:
            reject(candidate, "evidence_state_ineligible")
            continue
        if len(candidate.trajectory_signature) != len(current.trajectory_signature):
            reject(candidate, "trajectory_window_mismatch")
            continue
        try:
            receipt_ids = _validate_receipts(candidate)
        except HistoricalLeakage:
            reject(candidate, "future_receipt")
            continue
        except ValueError as error:
            reject(candidate, str(error))
            continue
        trajectory = _trajectory_similarity(
            current.trajectory_signature,
            candidate.trajectory_signature,
            rules.minimum_trajectory_points,
        )
        if trajectory is None:
            continue
        term = _jaccard(current.terms, candidate.terms)
        source = _jaccard(current.source_families, candidate.source_families)
        geography = 1.0 if current.market == candidate.market else 0.0
        similarity = (
            term * rules.term_weight
            + source * rules.source_weight
            + trajectory * rules.trajectory_weight
            + geography * rules.geography_weight
        )
        if not math.isfinite(similarity):
            raise ValueError("historical similarity is nonfinite")
        if similarity < rules.minimum_similarity:
            reject(candidate, "below_similarity_threshold")
            continue
        difference_codes = _differences(current, candidate)
        matches.append(
            HistoricalAnalogueMatch(
                signal_id=candidate.signal_id,
                as_of=candidate.as_of,
                market=candidate.market,
                similarity=similarity,
                term_similarity=term,
                source_similarity=source,
                trajectory_similarity=trajectory,
                geography_similarity=geography,
                difference_codes=difference_codes,
                receipt_ids=receipt_ids,
                transfer_limits=_transfer_limits(difference_codes),
            )
        )
    ordered_matches = tuple(
        sorted(
            matches,
            key=lambda match: (
                -match.similarity,
                -match.as_of.timestamp(),
                match.signal_id,
                match.market,
                match.receipt_ids,
            ),
        )[:limit]
    )
    ordered_rejections = tuple(
        sorted(
            rejections,
            key=lambda item: (item.signal_id, item.market, item.as_of, item.reason),
        )
    )
    return HistoricalAnalogueSearchResult(ordered_matches, ordered_rejections)
