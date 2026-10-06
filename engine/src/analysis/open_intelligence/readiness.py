"""Pure evidence-readiness evaluation for dynamic signal rows."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType

from src.analysis.open_intelligence.candidates import QUALIFYING_SOURCE_FAMILIES
from src.analysis.open_intelligence.graph import wave1_pair_is_independent
from src.contracts.open_intelligence import EVIDENCE_STATES

# The independence rule is versioned by run profile, as generations are. The legacy rule
# is the 613dea2 readiness rule (distinct families pair, identities unseen), proved
# equivalent against that revision in the readiness suite; the explicit origin rule
# admits only identities that passed validate_source_identity.
WAVE1_FAMILY_POLICY = "wave1_family_v1"
EXPLICIT_ORIGIN_POLICY = "explicit_origin_v2"
INDEPENDENCE_POLICIES = (WAVE1_FAMILY_POLICY, EXPLICIT_ORIGIN_POLICY)
DEFAULT_INDEPENDENCE_POLICY = WAVE1_FAMILY_POLICY
_DIRECTIONS = frozenset({"rising", "stable", "declining", "conflicting", "not_applicable"})
_AVAILABILITY = frozenset({"available", "aged_out", "unavailable"})
_ACTIVE_V2_FAMILIES = QUALIFYING_SOURCE_FAMILIES - {"brand24"}


def _utc_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone aware")
    return value.astimezone(UTC)


def _bounded_score(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise ValueError(f"{field} must be finite within zero and one")
    return float(value)


def _exact_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be boolean")
    return value


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    row_id: str
    source_family: str
    direction: str
    published_at: datetime | None
    availability: str
    geo_confidence: float
    factual_conflict: bool = False
    vendor_family: str | None = None
    channel_family: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.row_id, str) or not self.row_id:
            raise ValueError("row id must be a nonempty string")
        origin = (self.vendor_family, self.channel_family)
        if any(value is not None for value in origin) and any(
            not isinstance(value, str) or not value for value in origin
        ):
            raise ValueError("source origin must name both vendor and channel families")
        if self.source_family not in _ACTIVE_V2_FAMILIES:
            raise ValueError("source family is unsupported")
        if self.direction not in _DIRECTIONS:
            raise ValueError("direction is unsupported")
        if self.availability not in _AVAILABILITY:
            raise ValueError("availability is unsupported")
        if self.published_at is not None:
            object.__setattr__(
                self, "published_at", _utc_timestamp(self.published_at, "published at")
            )
        object.__setattr__(
            self, "geo_confidence", _bounded_score(self.geo_confidence, "geo confidence")
        )
        object.__setattr__(
            self, "factual_conflict", _exact_bool(self.factual_conflict, "factual conflict")
        )

    @property
    def source_origin(self) -> tuple[str, str] | None:
        if self.vendor_family is None:
            return None
        return (self.vendor_family, self.channel_family)


@dataclass(frozen=True, slots=True)
class ReadinessRules:
    current_cutoff: datetime
    minimum_geo_confidence: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "current_cutoff", _utc_timestamp(self.current_cutoff, "current cutoff")
        )
        object.__setattr__(
            self,
            "minimum_geo_confidence",
            _bounded_score(self.minimum_geo_confidence, "minimum geo confidence"),
        )


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    state: str
    qualifying_families: tuple[str, ...]
    direction_by_family: MappingProxyType
    reasons: tuple[str, ...]
    qualifying_row_ids: tuple[str, ...] = ()
    # Retrieval context, not decision identity: every supplied row stays reachable
    # in a thin, contradictory or unchecked result without changing what the result is.
    context_row_ids: tuple[str, ...] = field(default=(), compare=False)
    independent_pairs: tuple[tuple[str, str], ...] = ()
    independence_policy: str = DEFAULT_INDEPENDENCE_POLICY

    def __post_init__(self) -> None:
        if self.state not in EVIDENCE_STATES:
            raise ValueError("readiness state is unsupported")
        if self.independence_policy not in INDEPENDENCE_POLICIES:
            raise ValueError("independence policy is unsupported")
        object.__setattr__(self, "qualifying_families", tuple(sorted(self.qualifying_families)))
        object.__setattr__(
            self,
            "direction_by_family",
            MappingProxyType(dict(sorted(self.direction_by_family.items()))),
        )
        object.__setattr__(self, "reasons", tuple(sorted(self.reasons)))
        object.__setattr__(self, "qualifying_row_ids", tuple(sorted(self.qualifying_row_ids)))
        object.__setattr__(self, "context_row_ids", tuple(sorted(self.context_row_ids)))
        object.__setattr__(
            self,
            "independent_pairs",
            tuple(sorted(tuple(sorted(pair)) for pair in self.independent_pairs)),
        )


def _result(
    state: str,
    qualifying_families: set[str],
    direction_by_family: dict[str, str],
    reasons: set[str],
    *,
    qualifying: tuple[EvidenceRecord, ...] = (),
    context: tuple[EvidenceRecord, ...] = (),
    independent_pairs: tuple[tuple[str, str], ...] = (),
    policy: str = DEFAULT_INDEPENDENCE_POLICY,
) -> ReadinessResult:
    # The reasons name the rule whenever the rule produced the state, and always under
    # the explicit origin rule. A legacy result that the family rules decided keeps its
    # pre D07 reasons byte for byte; its policy field still says which rule applied.
    tagged = set(reasons)
    if policy == EXPLICIT_ORIGIN_POLICY or "insufficient_independent_support" in tagged:
        tagged.add(f"independence_policy:{policy}")
    return ReadinessResult(
        state=state,
        qualifying_families=tuple(qualifying_families),
        direction_by_family=MappingProxyType(direction_by_family),
        reasons=tuple(tagged),
        qualifying_row_ids=tuple(item.row_id for item in qualifying),
        context_row_ids=tuple(item.row_id for item in context),
        independent_pairs=independent_pairs,
        independence_policy=policy,
    )


def _pair_is_independent(left: EvidenceRecord, right: EvidenceRecord, policy: str) -> bool:
    if policy == EXPLICIT_ORIGIN_POLICY:
        # Only an explicit origin pairs; a record without one is unknown origin.
        if left.source_origin is None or right.source_origin is None:
            return False
        return wave1_pair_is_independent(*left.source_origin, *right.source_origin)
    # wave1_family_v1: the readiness rule as it stood at 613dea2, which never saw an
    # identity. Two supporting members pair when their families differ, whatever
    # identity either carries.
    return left.source_family != right.source_family


def admitted_independent_pairs(
    records: tuple[EvidenceRecord, ...],
    *,
    direction: str,
    policy: str = DEFAULT_INDEPENDENCE_POLICY,
) -> tuple[tuple[str, str], ...]:
    """Row pairs the independence rule of one policy admits for one direction.

    Both members must support the direction. Under explicit_origin_v2 both must carry
    an explicit origin and pass the graph pair rule (a different vendor and a different
    channel); an unknown origin never pairs, and distinct row ids alone never pair. Under
    wave1_family_v1 members pair by distinct family, the 613dea2 rule, whatever identity
    they carry.
    """
    if policy not in INDEPENDENCE_POLICIES:
        raise ValueError("independence policy is unsupported")
    supporting = tuple(item for item in records if item.direction == direction)
    pairs = set()
    for index, left in enumerate(supporting):
        for right in supporting[index + 1 :]:
            if _pair_is_independent(left, right, policy):
                pairs.add(tuple(sorted((left.row_id, right.row_id))))
    return tuple(sorted(pairs))


def _family_directions(records: tuple[EvidenceRecord, ...]) -> dict[str, str]:
    grouped: dict[str, set[str]] = {}
    for record in records:
        grouped.setdefault(record.source_family, set()).add(record.direction)
    output: dict[str, str] = {}
    for family, directions in grouped.items():
        directional = directions - {"not_applicable"}
        if "conflicting" in directional or len(directional) > 1:
            output[family] = "conflicting"
        elif directional:
            output[family] = next(iter(directional))
        else:
            output[family] = "not_applicable"
    return output


def evaluate_readiness(
    records: object,
    rules: ReadinessRules,
    *,
    quality_evaluated: bool,
    quality_failed: bool = False,
    factual_conflict: bool = False,
    independence_policy: str = DEFAULT_INDEPENDENCE_POLICY,
) -> ReadinessResult:
    """Classify supplied evidence without inventing evidence or direction."""
    if not isinstance(rules, ReadinessRules):
        raise ValueError("readiness rules are invalid")
    if independence_policy not in INDEPENDENCE_POLICIES:
        raise ValueError("independence policy is unsupported")
    policy = independence_policy
    quality_evaluated = _exact_bool(quality_evaluated, "quality evaluated")
    quality_failed = _exact_bool(quality_failed, "quality failed")
    factual_conflict = _exact_bool(factual_conflict, "factual conflict")
    try:
        items = tuple(records)
    except TypeError as error:
        raise ValueError("evidence records must be iterable") from error
    if any(not isinstance(item, EvidenceRecord) for item in items):
        raise ValueError("evidence records are invalid")
    row_ids = tuple(item.row_id for item in items)
    if len(set(row_ids)) != len(row_ids):
        raise ValueError("duplicate row id")
    if quality_failed:
        return _result(
            "unchecked", set(), {}, {"quality_evaluation_failed"}, context=items, policy=policy
        )
    if not quality_evaluated:
        return _result(
            "unchecked", set(), {}, {"quality_not_evaluated"}, context=items, policy=policy
        )
    if factual_conflict or any(item.factual_conflict for item in items):
        return _result(
            "contradictory", set(), {}, {"factual_conflict"}, context=items, policy=policy
        )

    reasons: set[str] = set()
    qualifying: list[EvidenceRecord] = []
    for item in items:
        if item.availability == "aged_out":
            reasons.add("aged_out_evidence")
            continue
        if item.availability == "unavailable":
            reasons.add("unavailable_evidence")
            continue
        if item.published_at is None:
            reasons.add("missing_published_at")
            continue
        if item.published_at < rules.current_cutoff:
            reasons.add("stale_evidence")
            continue
        if item.geo_confidence < rules.minimum_geo_confidence:
            reasons.add("weak_geo_evidence")
            continue
        qualifying.append(item)

    qualified = tuple(qualifying)
    family_directions = _family_directions(qualified)
    families = set(family_directions)
    if "conflicting" in family_directions.values():
        return _result(
            "contradictory",
            families,
            family_directions,
            {"conflicting_direction"},
            qualifying=qualified,
            context=items,
            policy=policy,
        )

    directions = set(family_directions.values()) - {"not_applicable"}
    if "rising" in directions and "declining" in directions:
        return _result(
            "contradictory",
            families,
            family_directions,
            {"opposing_family_directions"},
            qualifying=qualified,
            context=items,
            policy=policy,
        )
    if len(families) < 2:
        reasons.add("insufficient_qualifying_families")
        return _result(
            "thin",
            families,
            family_directions,
            reasons,
            qualifying=qualified,
            context=items,
            policy=policy,
        )
    if (
        len(directions) != 1
        or sum(value != "not_applicable" for value in family_directions.values()) < 2
    ):
        reasons.add("insufficient_directional_agreement")
        return _result(
            "thin",
            families,
            family_directions,
            reasons,
            qualifying=qualified,
            context=items,
            policy=policy,
        )
    # Ready is admitted only through the independence gate of the selected policy.
    pairs = admitted_independent_pairs(qualified, direction=next(iter(directions)), policy=policy)
    try:
        require_independent_release({"evidence_state": "ready"}, independent_pairs=pairs)
    except ValueError as error:
        reasons.add(str(error))
        return _result(
            "thin",
            families,
            family_directions,
            reasons,
            qualifying=qualified,
            context=items,
            policy=policy,
        )
    return _result(
        "ready",
        families,
        family_directions,
        set(),
        qualifying=qualified,
        context=items,
        independent_pairs=pairs,
        policy=policy,
    )


def require_independent_release(candidate: dict, *, independent_pairs: tuple) -> None:
    if candidate["evidence_state"] != "ready":
        raise ValueError("candidate_not_ready")
    if not independent_pairs:
        raise ValueError("insufficient_independent_support")
