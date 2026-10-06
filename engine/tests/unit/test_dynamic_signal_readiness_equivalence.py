"""wave1_family_v1 is the 613dea2 readiness rule, proved against that revision's module."""

from __future__ import annotations

import hashlib
import random
import sys
import types
from datetime import UTC, datetime, timedelta

from src.analysis.open_intelligence.candidates import APPROVED_SOURCE_IDENTITY_PAIRS
from src.analysis.open_intelligence.readiness import (
    EvidenceRecord,
    ReadinessRules,
    evaluate_readiness,
)

# git rev-parse 613dea2:engine/src/analysis/open_intelligence/readiness.py
BASELINE_BLOB = "72d94313ccc5c63851a06a59f1db1ab41b0dbb09"
BASELINE_SOURCE = r'''"""Pure evidence-readiness evaluation for dynamic signal rows."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from src.analysis.open_intelligence.candidates import QUALIFYING_SOURCE_FAMILIES
from src.contracts.open_intelligence import EVIDENCE_STATES

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

    def __post_init__(self) -> None:
        if not isinstance(self.row_id, str) or not self.row_id:
            raise ValueError("row id must be a nonempty string")
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

    def __post_init__(self) -> None:
        if self.state not in EVIDENCE_STATES:
            raise ValueError("readiness state is unsupported")
        object.__setattr__(self, "qualifying_families", tuple(sorted(self.qualifying_families)))
        object.__setattr__(
            self,
            "direction_by_family",
            MappingProxyType(dict(sorted(self.direction_by_family.items()))),
        )
        object.__setattr__(self, "reasons", tuple(sorted(self.reasons)))


def _result(
    state: str,
    qualifying_families: set[str],
    direction_by_family: dict[str, str],
    reasons: set[str],
) -> ReadinessResult:
    return ReadinessResult(
        state=state,
        qualifying_families=tuple(qualifying_families),
        direction_by_family=MappingProxyType(direction_by_family),
        reasons=tuple(reasons),
    )


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
) -> ReadinessResult:
    """Classify supplied evidence without inventing evidence or direction."""
    if not isinstance(rules, ReadinessRules):
        raise ValueError("readiness rules are invalid")
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
        return _result("unchecked", set(), {}, {"quality_evaluation_failed"})
    if not quality_evaluated:
        return _result("unchecked", set(), {}, {"quality_not_evaluated"})
    if factual_conflict or any(item.factual_conflict for item in items):
        return _result("contradictory", set(), {}, {"factual_conflict"})

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

    family_directions = _family_directions(tuple(qualifying))
    families = set(family_directions)
    if "conflicting" in family_directions.values():
        return _result("contradictory", families, family_directions, {"conflicting_direction"})

    directions = set(family_directions.values()) - {"not_applicable"}
    if "rising" in directions and "declining" in directions:
        return _result("contradictory", families, family_directions, {"opposing_family_directions"})
    if len(families) < 2:
        reasons.add("insufficient_qualifying_families")
        return _result("thin", families, family_directions, reasons)
    if (
        len(directions) != 1
        or sum(value != "not_applicable" for value in family_directions.values()) < 2
    ):
        reasons.add("insufficient_directional_agreement")
        return _result("thin", families, family_directions, reasons)
    return _result("ready", families, family_directions, set())


def require_independent_release(candidate: dict, *, independent_pairs: tuple) -> None:
    if candidate["evidence_state"] != "ready":
        raise ValueError("candidate_not_ready")
    if not independent_pairs:
        raise ValueError("insufficient_independent_support")
'''
CUTOFF = datetime(2026, 8, 24, tzinfo=UTC)
DIRECTIONS = ("rising", "stable", "declining", "conflicting", "not_applicable")
AVAILABILITY = ("available", "available", "available", "aged_out", "unavailable")
FAMILIES = ("news", "reddit", "youtube", "search", "short_video", "twitter", "threads", "music")
VENDORS_BY_CHANNEL = {
    channel: tuple(
        sorted(vendor for vendor, item in APPROVED_SOURCE_IDENTITY_PAIRS if item == channel)
    )
    for channel in FAMILIES
}


def load_baseline() -> types.ModuleType:
    data = BASELINE_SOURCE.encode("utf-8")
    blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    assert blob == BASELINE_BLOB, "embedded source is not the 613dea2 readiness module"
    module = types.ModuleType("readiness_613dea2")
    module.__file__ = "readiness_613dea2.py"
    sys.modules[module.__name__] = module
    exec(compile(BASELINE_SOURCE, module.__file__, "exec"), module.__dict__)
    return module


def rules(module) -> object:
    return module.ReadinessRules(current_cutoff=CUTOFF, minimum_geo_confidence=0.8)


def outcome(result) -> tuple:
    return (
        result.state,
        tuple(result.reasons),
        tuple(result.qualifying_families),
        dict(result.direction_by_family),
    )


def evaluate_both(baseline, specs, *, quality_evaluated=True, quality_failed=False, conflict=False):
    common = {
        "quality_evaluated": quality_evaluated,
        "quality_failed": quality_failed,
        "factual_conflict": conflict,
    }
    old = baseline.evaluate_readiness(
        [
            baseline.EvidenceRecord(
                **{
                    k: v
                    for k, v in spec.items()
                    if not k.endswith("_family") or k == "source_family"
                }
            )
            for spec in specs
        ],
        rules(baseline),
        **common,
    )
    new = evaluate_readiness(
        [EvidenceRecord(**spec) for spec in specs],
        rules(sys.modules[EvidenceRecord.__module__]),
        **common,
    )
    assert new.independence_policy == "wave1_family_v1"
    return outcome(old), outcome(new)


def spec(row_id, family, direction, origin, **overrides) -> dict:
    vendor, channel = origin if origin is not None else (None, None)
    values = {
        "row_id": row_id,
        "source_family": family,
        "direction": direction,
        "published_at": datetime(2026, 8, 24, 9, tzinfo=UTC),
        "availability": "available",
        "geo_confidence": 0.91,
        "factual_conflict": False,
        "vendor_family": vendor,
        "channel_family": channel,
    }
    values.update(overrides)
    return values


def test_the_embedded_baseline_is_the_613dea2_module():
    baseline = load_baseline()
    assert not hasattr(baseline.EvidenceRecord, "source_origin")
    assert "independence" not in BASELINE_SOURCE.split("def require_independent_release")[0]


def test_reviewer_records_agree_with_613dea2_under_the_default_policy():
    baseline = load_baseline()
    cases = (
        [
            spec("row_1", "reddit", "rising", ("socialcrawl", "reddit")),
            spec("row_2", "news", "rising", ("socialcrawl", "news")),
        ],
        [
            spec("row_1", "reddit", "rising", ("socialcrawl", "reddit")),
            spec("row_2", "youtube", "rising", ("socialcrawl", "youtube")),
        ],
        [
            spec("row_1", "reddit", "rising", ("reddit", "reddit")),
            spec("row_2", "youtube", "rising", ("google_youtube", "youtube")),
        ],
        [spec("row_1", "reddit", "rising", None), spec("row_2", "youtube", "rising", None)],
        [
            spec("row_1", "reddit", "rising", ("socialcrawl", "reddit")),
            spec("row_2", "youtube", "rising", None),
        ],
    )
    for records in cases:
        old, new = evaluate_both(baseline, records)
        assert old == new, records
        assert old[0] == "ready"
        assert old[1] == ()


def generated_sets(count: int, seed: int):
    generator = random.Random(seed)
    for index in range(count):
        size = generator.randint(1, 5)
        mode = generator.choice(("absent", "present", "shared"))
        shared_vendor = generator.choice(("socialcrawl", "rss", "gdelt", "google_youtube"))
        specs = []
        for position in range(size):
            family = generator.choice(FAMILIES)
            if mode == "absent":
                origin = None
            elif mode == "shared":
                origin = (shared_vendor, family)
            else:
                vendors = VENDORS_BY_CHANNEL[family]
                origin = (generator.choice(vendors), family) if vendors else None
            published = (
                None
                if generator.random() < 0.1
                else CUTOFF + timedelta(hours=generator.randint(-48, 30))
            )
            specs.append(
                spec(
                    f"row_{index}_{position}",
                    family,
                    generator.choice(DIRECTIONS),
                    origin,
                    published_at=published,
                    availability=generator.choice(AVAILABILITY),
                    geo_confidence=round(generator.uniform(0.5, 1.0), 3),
                    factual_conflict=generator.random() < 0.05,
                )
            )
        flags = {
            "quality_evaluated": generator.random() >= 0.1,
            "quality_failed": generator.random() < 0.05,
            "conflict": generator.random() < 0.05,
        }
        yield index, mode, specs, flags


def test_generated_record_sets_agree_with_613dea2_under_the_default_policy():
    baseline = load_baseline()
    states = set()
    modes = set()
    for index, mode, specs, flags in generated_sets(300, 20260913):
        old, new = evaluate_both(baseline, specs, **flags)
        assert old == new, (index, mode, specs, flags, old, new)
        states.add(old[0])
        modes.add(mode)
    assert states == {"ready", "thin", "contradictory", "unchecked"}
    assert modes == {"absent", "present", "shared"}
