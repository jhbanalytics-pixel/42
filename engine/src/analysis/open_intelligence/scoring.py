"""Pure audience-neutral decision strength and cohort calibration."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from types import MappingProxyType

DECISION_STRENGTH_VERSION = "decision_strength_v1_geometric"
EVIDENCE_STRENGTH_VERSION = "evidence_strength_v1_geometric"
COHORT_PERCENTILE_VERSION = "cohort_percentile_v1"
MINIMUM_COHORT_SIZE = 20

_MARKETS = frozenset({"za", "ng", "ke"})
_DISCOVERY_MODES = frozenset({"dynamic", "replay", "canary"})
_EVIDENCE_STATES = frozenset({"ready", "thin", "contradictory", "unchecked"})
_SIGNAL_ID = re.compile(r"sig_[0-9a-f]{64}\Z")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _bounded(value: object, field: str, *, nullable: bool = True) -> float | None:
    if value is None and nullable:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise ValueError(f"{field} must be finite within zero and one")
    return float(value)


def _count(value: object, field: str, *, nullable: bool = True) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _optional_bool(value: object, field: str) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise ValueError(f"{field} must be boolean")
    return value


@dataclass(frozen=True, slots=True)
class DecisionStrengthRules:
    approved_receipt_cap: int
    geo_floor_by_market: Mapping[str, float]
    approved_cluster_build_versions: tuple[str, ...]
    rule_version: str

    def __post_init__(self) -> None:
        cap = _count(self.approved_receipt_cap, "approved receipt cap", nullable=False)
        if cap == 0:
            raise ValueError("approved receipt cap must be positive")
        if not isinstance(self.geo_floor_by_market, Mapping):
            raise ValueError("geo floors must be a mapping")
        if set(self.geo_floor_by_market) != _MARKETS:
            raise ValueError("geo floors must contain exactly za, ng, and ke")
        floors = {
            market: _bounded(
                self.geo_floor_by_market[market], f"{market} geo floor", nullable=False
            )
            for market in sorted(_MARKETS)
        }
        try:
            versions = tuple(self.approved_cluster_build_versions)
        except TypeError as error:
            raise ValueError("approved cluster build versions must be iterable") from error
        if not versions or any(not isinstance(item, str) or not item.strip() for item in versions):
            raise ValueError("approved cluster build versions must be nonempty strings")
        normalized = tuple(sorted(item.strip() for item in versions))
        if len(normalized) != len(set(normalized)):
            raise ValueError("approved cluster build versions must be unique")
        object.__setattr__(self, "approved_receipt_cap", cap)
        object.__setattr__(self, "geo_floor_by_market", MappingProxyType(floors))
        object.__setattr__(self, "approved_cluster_build_versions", normalized)
        object.__setattr__(self, "rule_version", _text(self.rule_version, "rule version"))


@dataclass(frozen=True, slots=True)
class SignalScoreInput:
    signal_id: str
    market: str
    signal_date: date
    discovery_mode: str
    evidence_state: str | None
    qualifying_source_families: int | None
    qualifying_current_receipts: int | None
    source_integrity: float | None
    velocity: float | None
    breadth: float | None
    source_independence: float | None
    geo_confidence: float | None
    foreign_market_sample_reviewed: bool | None
    foreign_market_leakage: int | None
    duplicate_identity: bool | None
    factual_conflict: bool | None
    directional_conflict: bool | None
    membership_receipts_complete: bool | None
    cluster_build_version: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.signal_id, str) or _SIGNAL_ID.fullmatch(self.signal_id) is None:
            raise ValueError("signal id is invalid")
        if self.market not in _MARKETS:
            raise ValueError("market is unsupported")
        if isinstance(self.signal_date, datetime) or not isinstance(self.signal_date, date):
            raise ValueError("signal date must be a date")
        if self.discovery_mode not in _DISCOVERY_MODES:
            raise ValueError("discovery mode is unsupported")
        if self.evidence_state is not None and self.evidence_state not in _EVIDENCE_STATES:
            raise ValueError("evidence state is unsupported")
        for field in (
            "qualifying_source_families",
            "qualifying_current_receipts",
            "foreign_market_leakage",
        ):
            object.__setattr__(self, field, _count(getattr(self, field), field.replace("_", " ")))
        for field in (
            "source_integrity",
            "velocity",
            "breadth",
            "source_independence",
            "geo_confidence",
        ):
            object.__setattr__(self, field, _bounded(getattr(self, field), field.replace("_", " ")))
        for field in (
            "foreign_market_sample_reviewed",
            "duplicate_identity",
            "factual_conflict",
            "directional_conflict",
            "membership_receipts_complete",
        ):
            object.__setattr__(
                self, field, _optional_bool(getattr(self, field), field.replace("_", " "))
            )
        object.__setattr__(
            self,
            "cluster_build_version",
            _optional_text(self.cluster_build_version, "cluster build version"),
        )


@dataclass(frozen=True, slots=True)
class DecisionStrengthResult:
    signal_id: str
    market: str
    signal_date: date
    discovery_mode: str
    rule_version: str
    promotion_eligible: bool
    reasons: tuple[str, ...]
    velocity: float | None
    breadth: float | None
    source_independence: float | None
    evidence_strength: float | None
    geo_confidence: float | None
    decision_strength: float | None
    cohort_percentile: float | None
    evidence_strength_version: str = EVIDENCE_STRENGTH_VERSION
    decision_strength_version: str = DECISION_STRENGTH_VERSION
    cohort_percentile_version: str = COHORT_PERCENTILE_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.signal_id, str) or _SIGNAL_ID.fullmatch(self.signal_id) is None:
            raise ValueError("signal id is invalid")
        if self.market not in _MARKETS:
            raise ValueError("market is unsupported")
        if isinstance(self.signal_date, datetime) or not isinstance(self.signal_date, date):
            raise ValueError("signal date must be a date")
        if self.discovery_mode not in _DISCOVERY_MODES:
            raise ValueError("discovery mode is unsupported")
        object.__setattr__(self, "rule_version", _text(self.rule_version, "rule version"))
        if type(self.promotion_eligible) is not bool:
            raise ValueError("promotion eligible must be boolean")
        reasons = tuple(sorted(_text(reason, "reason") for reason in self.reasons))
        if len(reasons) != len(set(reasons)):
            raise ValueError("reasons must be unique")
        object.__setattr__(self, "reasons", reasons)
        for field in (
            "velocity",
            "breadth",
            "source_independence",
            "evidence_strength",
            "geo_confidence",
            "decision_strength",
            "cohort_percentile",
        ):
            object.__setattr__(self, field, _bounded(getattr(self, field), field.replace("_", " ")))
        if not self.promotion_eligible:
            if not reasons:
                raise ValueError("ineligible result requires reasons")
            if self.decision_strength is not None:
                raise ValueError("ineligible result cannot carry decision strength")
            if self.cohort_percentile is not None:
                raise ValueError("ineligible result cannot carry cohort percentile")
        else:
            if reasons:
                raise ValueError("eligible result cannot carry failure reasons")
            components = (
                self.velocity,
                self.breadth,
                self.source_independence,
                self.evidence_strength,
                self.geo_confidence,
            )
            if any(value is None for value in components) or self.decision_strength is None:
                raise ValueError("eligible result requires every decision component")
            expected = math.prod(components) ** (1 / 5)
            if not math.isclose(self.decision_strength, expected, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("decision strength must equal the five component geometric mean")
        for field, expected in (
            ("evidence_strength_version", EVIDENCE_STRENGTH_VERSION),
            ("decision_strength_version", DECISION_STRENGTH_VERSION),
            ("cohort_percentile_version", COHORT_PERCENTILE_VERSION),
        ):
            if getattr(self, field) != expected:
                raise ValueError(f"{field.replace('_', ' ')} is unsupported")


def _evidence_strength(item: SignalScoreInput, rules: DecisionStrengthRules) -> float | None:
    if (
        item.qualifying_source_families is None
        or item.qualifying_current_receipts is None
        or item.source_integrity is None
    ):
        return None
    family_factor = min(1.0, item.qualifying_source_families / 4)
    receipt_factor = min(1.0, item.qualifying_current_receipts / rules.approved_receipt_cap)
    return (family_factor * receipt_factor * item.source_integrity) ** (1 / 3)


def _gate_reasons(item: SignalScoreInput, rules: DecisionStrengthRules) -> set[str]:
    reasons: set[str] = set()
    if item.evidence_state is None:
        reasons.add("evidence_state_unmeasured")
    elif item.evidence_state != "ready":
        reasons.add("evidence_not_ready")
    if item.qualifying_source_families is None:
        reasons.add("source_families_unmeasured")
    elif item.qualifying_source_families < 2:
        reasons.add("insufficient_source_families")
    if item.geo_confidence is None:
        reasons.add("geo_confidence_unmeasured")
    elif item.geo_confidence < rules.geo_floor_by_market[item.market]:
        reasons.add("geo_below_market_floor")
    if item.foreign_market_sample_reviewed is not True:
        reasons.add("foreign_market_sample_unreviewed")
    if item.foreign_market_leakage is None:
        reasons.add("foreign_market_leakage_unmeasured")
    elif item.foreign_market_leakage != 0:
        reasons.add("foreign_market_leakage")
    for field, failed_reason, missing_reason in (
        (item.duplicate_identity, "duplicate_identity", "duplicate_identity_unmeasured"),
        (item.factual_conflict, "factual_conflict", "factual_conflict_unmeasured"),
        (item.directional_conflict, "directional_conflict", "directional_conflict_unmeasured"),
    ):
        if field is None:
            reasons.add(missing_reason)
        elif field:
            reasons.add(failed_reason)
    if item.membership_receipts_complete is None:
        reasons.add("membership_receipts_unmeasured")
    elif not item.membership_receipts_complete:
        reasons.add("membership_receipts_incomplete")
    if item.cluster_build_version is None:
        reasons.add("cluster_build_unmeasured")
    elif item.cluster_build_version not in rules.approved_cluster_build_versions:
        reasons.add("cluster_build_unapproved")
    for value, reason in (
        (item.velocity, "velocity_unmeasured"),
        (item.breadth, "breadth_unmeasured"),
        (item.source_independence, "source_independence_unmeasured"),
        (item.source_integrity, "source_integrity_unmeasured"),
        (item.qualifying_current_receipts, "current_receipts_unmeasured"),
    ):
        if value is None:
            reasons.add(reason)
    return reasons


def score_signal(
    item: SignalScoreInput,
    rules: DecisionStrengthRules,
) -> DecisionStrengthResult:
    if not isinstance(item, SignalScoreInput):
        raise ValueError("signal score input is invalid")
    if not isinstance(rules, DecisionStrengthRules):
        raise ValueError("decision strength rules are invalid")
    evidence_strength = _evidence_strength(item, rules)
    reasons = _gate_reasons(item, rules)
    decision_strength = None
    if not reasons:
        components = (
            item.velocity,
            item.breadth,
            item.source_independence,
            evidence_strength,
            item.geo_confidence,
        )
        if any(value is None for value in components):
            raise AssertionError("eligible signal has an unmeasured decision component")
        decision_strength = math.prod(components) ** (1 / 5)
    return DecisionStrengthResult(
        signal_id=item.signal_id,
        market=item.market,
        signal_date=item.signal_date,
        discovery_mode=item.discovery_mode,
        rule_version=rules.rule_version,
        promotion_eligible=not reasons,
        reasons=tuple(reasons),
        velocity=item.velocity,
        breadth=item.breadth,
        source_independence=item.source_independence,
        evidence_strength=evidence_strength,
        geo_confidence=item.geo_confidence,
        decision_strength=decision_strength,
        cohort_percentile=None,
    )


def _cohort_key(result: DecisionStrengthResult) -> tuple[str, date, str, str]:
    return (
        result.market,
        result.signal_date,
        result.discovery_mode,
        result.rule_version,
    )


def calibrate_cohort_percentiles(
    results: Iterable[DecisionStrengthResult],
) -> tuple[DecisionStrengthResult, ...]:
    try:
        items = tuple(results)
    except TypeError as error:
        raise ValueError("decision strength results must be iterable") from error
    if any(not isinstance(item, DecisionStrengthResult) for item in items):
        raise ValueError("decision strength results are invalid")
    identities = tuple(
        (item.signal_id, item.market, item.signal_date, item.discovery_mode, item.rule_version)
        for item in items
    )
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate signal score identity")
    groups: dict[tuple[str, date, str, str], list[DecisionStrengthResult]] = {}
    for item in items:
        if item.promotion_eligible:
            groups.setdefault(_cohort_key(item), []).append(item)
    percentile_by_identity: dict[tuple[str, str, date, str, str], float] = {}
    for cohort in groups.values():
        if len(cohort) < MINIMUM_COHORT_SIZE:
            continue
        ordered_scores = sorted(item.decision_strength for item in cohort)
        denominator = len(ordered_scores) - 1
        for item in cohort:
            score = item.decision_strength
            first = ordered_scores.index(score)
            last = len(ordered_scores) - 1 - ordered_scores[::-1].index(score)
            percentile_by_identity[
                (
                    item.signal_id,
                    item.market,
                    item.signal_date,
                    item.discovery_mode,
                    item.rule_version,
                )
            ] = ((first + last) / 2) / denominator
    return tuple(
        replace(
            item,
            cohort_percentile=percentile_by_identity.get(
                (
                    item.signal_id,
                    item.market,
                    item.signal_date,
                    item.discovery_mode,
                    item.rule_version,
                )
            ),
        )
        for item in items
    )
