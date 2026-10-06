"""Point-in-time history normalization across collection-vendor regimes."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from itertools import combinations, pairwise
from statistics import median


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _bounded(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise ValueError(f"{field} must be finite within zero and one")
    return float(value)


def _nonnegative(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{field} must be finite and nonnegative")
    return float(value)


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class HistoricalObservation:
    metric_date: date
    market: str
    collection_vendor: str
    platform: str
    collection_mode: str
    seeded_or_discovered: str
    geo_confidence: float
    evidence_quality: float
    source_regime: str
    value: float
    available_at: datetime | None = None

    def __post_init__(self) -> None:
        if isinstance(self.metric_date, datetime) or not isinstance(self.metric_date, date):
            raise ValueError("metric date must be a date")
        if self.available_at is not None and (
            not isinstance(self.available_at, datetime)
            or self.available_at.tzinfo is None
            or self.available_at.utcoffset() is None
        ):
            raise ValueError("available at must be a timezone aware datetime")
        for field in (
            "market",
            "collection_vendor",
            "platform",
            "collection_mode",
            "source_regime",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field.replace("_", " ")))
        if self.market != self.market.lower():
            raise ValueError("market must be lower case")
        if self.seeded_or_discovered not in {"seeded", "discovered"}:
            raise ValueError("seeded or discovered is invalid")
        object.__setattr__(self, "geo_confidence", _bounded(self.geo_confidence, "geo confidence"))
        object.__setattr__(
            self, "evidence_quality", _bounded(self.evidence_quality, "evidence quality")
        )
        object.__setattr__(self, "value", _nonnegative(self.value, "value"))


@dataclass(frozen=True, slots=True)
class HistoryNormalizationRules:
    minimum_overlap_days: int
    minimum_geo_confidence: float
    minimum_evidence_quality: float
    baseline_window_days: int
    maximum_overlap_ratio_deviation: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "minimum_overlap_days",
            _positive_integer(self.minimum_overlap_days, "minimum overlap days"),
        )
        object.__setattr__(
            self,
            "minimum_geo_confidence",
            _bounded(self.minimum_geo_confidence, "minimum geo confidence"),
        )
        object.__setattr__(
            self,
            "minimum_evidence_quality",
            _bounded(self.minimum_evidence_quality, "minimum evidence quality"),
        )
        object.__setattr__(
            self,
            "baseline_window_days",
            _positive_integer(self.baseline_window_days, "baseline window days"),
        )
        object.__setattr__(
            self,
            "maximum_overlap_ratio_deviation",
            _bounded(
                self.maximum_overlap_ratio_deviation,
                "maximum overlap ratio deviation",
            ),
        )


@dataclass(frozen=True, slots=True)
class RegimeBridge:
    market: str
    platform: str
    collection_mode: str
    seeded_or_discovered: str
    from_regime: str
    to_regime: str
    overlap_days: int
    scale_factor: float


@dataclass(frozen=True, slots=True)
class NormalizedHistoryPoint:
    metric_date: date
    index_value: float
    contributing_platforms: int


@dataclass(frozen=True, slots=True)
class NormalizedHistory:
    state: str
    points: tuple[NormalizedHistoryPoint, ...]
    bridges: tuple[RegimeBridge, ...]
    platform_adjusted_delta: float | None
    gaps: tuple[str, ...]
    excluded_rows: int
    unavailable_rows: int = 0


def _group_identity(row: HistoricalObservation) -> tuple[str, str, str, str]:
    return (
        row.market,
        row.platform,
        row.collection_mode,
        row.seeded_or_discovered,
    )


def _regime_identity(row: HistoricalObservation) -> tuple[object, ...]:
    return (*_group_identity(row), row.source_regime, row.metric_date)


def _normalize_group(
    rows: tuple[HistoricalObservation, ...],
    rules: HistoryNormalizationRules,
) -> tuple[dict[date, float], tuple[RegimeBridge, ...], str | None]:
    by_regime: dict[str, dict[date, float]] = defaultdict(dict)
    for item in rows:
        by_regime[item.source_regime][item.metric_date] = item.value
    ordered_regimes = tuple(sorted(by_regime, key=lambda regime: (min(by_regime[regime]), regime)))
    starts = tuple(min(by_regime[regime]) for regime in ordered_regimes)
    if len(starts) != len(set(starts)):
        return {}, (), "ambiguous_regime_order"
    scales = {ordered_regimes[0]: 1.0}
    bridges: list[RegimeBridge] = []
    group = _group_identity(rows[0])
    for previous, current in pairwise(ordered_regimes):
        shared_days = set(by_regime[previous]).intersection(by_regime[current])
        if any(
            (by_regime[previous][day] == 0) != (by_regime[current][day] == 0) for day in shared_days
        ):
            return {}, (), "asymmetric_zero_overlap"
        overlap = tuple(
            sorted(
                day
                for day in shared_days
                if by_regime[previous][day] > 0 and by_regime[current][day] > 0
            )
        )
        if len(overlap) < rules.minimum_overlap_days:
            return {}, (), "insufficient_regime_overlap"
        ratios = []
        for day in overlap:
            prior = by_regime[previous][day] * scales[previous]
            ratio = prior / by_regime[current][day]
            if not math.isfinite(prior) or not math.isfinite(ratio):
                return {}, (), "nonfinite_regime_adjustment"
            ratios.append(ratio)
        scale = median(ratios)
        if not math.isfinite(scale) or scale <= 0:
            return {}, (), "nonfinite_regime_adjustment"
        deviation = max(abs(ratio / scale - 1) for ratio in ratios)
        if not math.isfinite(deviation) or deviation > rules.maximum_overlap_ratio_deviation:
            return {}, (), "unstable_regime_overlap"
        scales[current] = scale
        bridges.append(
            RegimeBridge(
                market=group[0],
                platform=group[1],
                collection_mode=group[2],
                seeded_or_discovered=group[3],
                from_regime=previous,
                to_regime=current,
                overlap_days=len(overlap),
                scale_factor=scale,
            )
        )
    for left, right in combinations(ordered_regimes, 2):
        shared_days = set(by_regime[left]).intersection(by_regime[right])
        if any((by_regime[left][day] == 0) != (by_regime[right][day] == 0) for day in shared_days):
            return {}, (), "asymmetric_zero_overlap"
        for day in shared_days:
            if by_regime[left][day] == 0:
                continue
            left_value = by_regime[left][day] * scales[left]
            right_value = by_regime[right][day] * scales[right]
            if not math.isfinite(left_value) or not math.isfinite(right_value):
                return {}, (), "nonfinite_regime_adjustment"
            residual = abs(left_value - right_value) / max(left_value, right_value)
            if not math.isfinite(residual) or residual > rules.maximum_overlap_ratio_deviation:
                return {}, (), "unstable_regime_overlap"
    scaled_by_date: dict[date, list[float]] = defaultdict(list)
    for regime, values in by_regime.items():
        for day, value in values.items():
            scaled_value = value * scales[regime]
            if not math.isfinite(scaled_value):
                return {}, (), "nonfinite_regime_adjustment"
            scaled_by_date[day].append(scaled_value)
    scaled = {day: median(values) for day, values in scaled_by_date.items()}
    first_days = tuple(sorted(scaled))[: rules.baseline_window_days]
    baseline = median(scaled[day] for day in first_days)
    if not math.isfinite(baseline):
        return {}, (), "nonfinite_regime_adjustment"
    if baseline <= 0:
        return {}, (), "zero_history_baseline"
    index = {day: value / baseline * 100 for day, value in scaled.items()}
    if any(not math.isfinite(value) for value in index.values()):
        return {}, (), "nonfinite_regime_adjustment"
    return index, tuple(bridges), None


def published_as_of(
    rows: tuple[HistoricalObservation, ...], as_of: datetime
) -> tuple[tuple[HistoricalObservation, ...], int]:
    """Keep only the rows that were available at as_of.

    A row whose availability is after as_of did not exist for anyone reading
    at that time, so it leaves both the numerator and the denominator rather
    than being counted as excluded. A row without an availability time cannot
    prove it was available, so an as_of read refuses it.
    """
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone aware")
    if any(item.available_at is None for item in rows):
        raise ValueError("availability is required for an as_of history")
    published = tuple(item for item in rows if item.available_at <= as_of)
    return published, len(rows) - len(published)


def normalize_vendor_regimes(
    rows: object,
    rules: HistoryNormalizationRules,
    *,
    as_of: datetime | None = None,
) -> NormalizedHistory:
    if not isinstance(rules, HistoryNormalizationRules):
        raise ValueError("history normalization rules are invalid")
    try:
        items = tuple(rows)
    except TypeError as error:
        raise ValueError("historical observations must be iterable") from error
    if any(not isinstance(item, HistoricalObservation) for item in items):
        raise ValueError("historical observations are invalid")
    unavailable = 0
    if as_of is not None:
        items, unavailable = published_as_of(items, as_of)
    identities = tuple(_regime_identity(item) for item in items)
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate historical observation identity")
    markets = {item.market for item in items}
    if len(markets) > 1:
        raise ValueError("history series must contain one market")
    vendors_by_regime: dict[str, set[str]] = defaultdict(set)
    for item in items:
        vendors_by_regime[item.source_regime].add(item.collection_vendor)
    if any(len(vendors) != 1 for vendors in vendors_by_regime.values()):
        raise ValueError("source regime must map to one vendor")
    qualified = tuple(
        item
        for item in items
        if item.geo_confidence >= rules.minimum_geo_confidence
        and item.evidence_quality >= rules.minimum_evidence_quality
    )
    excluded = len(items) - len(qualified)
    if not qualified:
        return NormalizedHistory(
            "unchecked", (), (), None, ("no_qualified_history",), excluded, unavailable
        )
    grouped: dict[tuple[str, str, str, str], list[HistoricalObservation]] = defaultdict(list)
    for item in qualified:
        grouped[_group_identity(item)].append(item)
    group_indexes: dict[tuple[str, str, str, str], dict[date, float]] = {}
    bridges: list[RegimeBridge] = []
    for key in sorted(grouped):
        index, group_bridges, gap = _normalize_group(tuple(grouped[key]), rules)
        if gap is not None:
            return NormalizedHistory("unbridgeable", (), (), None, (gap,), excluded, unavailable)
        group_indexes[key] = index
        bridges.extend(group_bridges)
    indexes_by_platform: dict[str, list[dict[date, float]]] = defaultdict(list)
    for key, index in group_indexes.items():
        indexes_by_platform[key[1]].append(index)
    platform_indexes: dict[str, dict[date, float]] = {}
    for platform, indexes in indexes_by_platform.items():
        platform_dates = set(indexes[0])
        for index in indexes[1:]:
            platform_dates.intersection_update(index)
        platform_indexes[platform] = {
            day: median(index[day] for index in indexes) for day in platform_dates
        }
    common_dates = set(next(iter(platform_indexes.values())))
    for index in tuple(platform_indexes.values())[1:]:
        common_dates.intersection_update(index)
    full_dates = tuple(sorted({item.metric_date for item in qualified}))
    required_dates = set(full_dates[: rules.baseline_window_days]).union(
        full_dates[-rules.baseline_window_days :]
    )
    if len(common_dates) < rules.baseline_window_days or not required_dates.issubset(common_dates):
        return NormalizedHistory(
            "unbridgeable",
            (),
            (),
            None,
            ("platform_coverage_not_comparable",),
            excluded,
            unavailable,
        )
    aggregate: dict[date, list[float]] = defaultdict(list)
    for index in platform_indexes.values():
        for day in common_dates:
            aggregate[day].append(index[day])
    points = tuple(
        NormalizedHistoryPoint(day, median(aggregate[day]), len(platform_indexes))
        for day in sorted(aggregate)
    )
    window = rules.baseline_window_days
    first = median(point.index_value for point in points[:window])
    last = median(point.index_value for point in points[-window:])
    delta = None if first == 0 else last / first - 1
    if not math.isfinite(first) or not math.isfinite(last) or not math.isfinite(delta):
        return NormalizedHistory(
            "unbridgeable",
            (),
            (),
            None,
            ("nonfinite_regime_adjustment",),
            excluded,
            unavailable,
        )
    return NormalizedHistory("ready", points, tuple(bridges), delta, (), excluded, unavailable)
