"""Receipt-backed historical recurrence assessment."""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise

from src.analysis.open_intelligence.historical_provenance import (
    HistoricalBridgeError,
    HistoricalEvidenceProvenanceBinding,
    binding_sort_key,
    build_recurrence_binding,
    runtime_projection_snapshot,
)

_MARKET = re.compile(r"[a-z]{2}\Z")
_RECEIPT_ID = re.compile(r"ev_[0-9a-f]{64}\Z")
_STATES = frozenset({"recurrent", "irregular", "insufficient_history"})


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _market(value: object) -> str:
    market = _text(value, "market")
    if _MARKET.fullmatch(market) is None:
        raise ValueError("market must be a lower-case market code")
    return market


def _date(value: object, field: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{field} must be a date")
    return value


def _receipts(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list, set, frozenset)):
        raise ValueError("receipt ids must be a collection")
    items = tuple(value)
    if not items or any(
        not isinstance(item, str) or _RECEIPT_ID.fullmatch(item) is None for item in items
    ):
        raise ValueError("receipt ids contain an invalid receipt")
    if len(items) != len(set(items)):
        raise ValueError("receipt ids contain duplicates")
    return tuple(sorted(items))


@dataclass(frozen=True, slots=True)
class RecurrenceOccurrence:
    occurrence_id: str
    pattern_id: str
    occurred_on: date
    market: str
    receipt_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurrence_id", _text(self.occurrence_id, "occurrence id"))
        object.__setattr__(self, "pattern_id", _text(self.pattern_id, "pattern id"))
        object.__setattr__(self, "occurred_on", _date(self.occurred_on, "occurred on"))
        object.__setattr__(self, "market", _market(self.market))
        object.__setattr__(self, "receipt_ids", _receipts(self.receipt_ids))


@dataclass(frozen=True, slots=True)
class RecurrenceRules:
    minimum_occurrences: int = 4
    maximum_absolute_interval_deviation_days: float = 3.0
    rule_version: str = "recurrence_rules_v1"

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_occurrences, bool)
            or not isinstance(self.minimum_occurrences, int)
            or self.minimum_occurrences < 4
        ):
            raise ValueError("minimum occurrences must be an integer of at least four")
        if (
            isinstance(self.maximum_absolute_interval_deviation_days, bool)
            or not isinstance(self.maximum_absolute_interval_deviation_days, (int, float))
            or not math.isfinite(float(self.maximum_absolute_interval_deviation_days))
            or float(self.maximum_absolute_interval_deviation_days) < 0
        ):
            raise ValueError("interval deviation ceiling must be finite and nonnegative")
        object.__setattr__(
            self,
            "maximum_absolute_interval_deviation_days",
            float(self.maximum_absolute_interval_deviation_days),
        )
        object.__setattr__(self, "rule_version", _text(self.rule_version, "rule version"))


@dataclass(frozen=True, slots=True)
class RecurrenceAssessment:
    state: str
    reason: str
    pattern_id: str
    market: str
    as_of: date
    occurrence_count: int
    first_occurrence: date | None
    last_occurrence: date | None
    median_interval_days: float | None
    interval_days: tuple[int, ...]
    interval_range_days: tuple[int, int] | None
    maximum_absolute_interval_deviation_days: float | None
    deviation_ceiling_days: float
    receipt_ids: tuple[str, ...]
    rule_version: str
    display_eligible: bool

    def __post_init__(self) -> None:
        if self.state not in _STATES:
            raise ValueError("recurrence state is invalid")
        expected_reason = {
            "recurrent": "bounded_intervals",
            "irregular": "interval_deviation_above_ceiling",
            "insufficient_history": "minimum_occurrences_not_met",
        }[self.state]
        if self.reason != expected_reason:
            raise ValueError("recurrence state and reason disagree")
        if type(self.display_eligible) is not bool or self.display_eligible:
            raise ValueError("recurrence assessment must remain display ineligible")
        if (
            isinstance(self.occurrence_count, bool)
            or not isinstance(self.occurrence_count, int)
            or self.occurrence_count < 0
        ):
            raise ValueError("occurrence count is invalid")
        if (
            isinstance(self.deviation_ceiling_days, bool)
            or not isinstance(self.deviation_ceiling_days, (int, float))
            or not math.isfinite(float(self.deviation_ceiling_days))
            or self.deviation_ceiling_days < 0
        ):
            raise ValueError("recurrence deviation ceiling is invalid")
        stats = (
            self.median_interval_days,
            self.interval_range_days,
            self.maximum_absolute_interval_deviation_days,
        )
        stats_present = all(value is not None for value in stats)
        if self.state in {"recurrent", "irregular"} and (
            not stats_present or self.occurrence_count < 4
        ):
            raise ValueError("scored recurrence requires complete interval evidence")
        if self.state == "insufficient_history" and stats_present:
            raise ValueError("insufficient recurrence cannot carry interval evidence")
        if self.state == "insufficient_history" and self.interval_days:
            raise ValueError("insufficient recurrence cannot carry interval days")
        if not stats_present and any(value is not None for value in stats):
            raise ValueError("recurrence interval evidence is incomplete")
        if stats_present:
            interval = self.interval_range_days
            if (
                not isinstance(self.interval_days, (tuple, list))
                or len(self.interval_days) != self.occurrence_count - 1
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value <= 0
                    for value in self.interval_days
                )
            ):
                raise ValueError("recurrence interval days are invalid")
            if (
                isinstance(self.median_interval_days, bool)
                or not isinstance(self.median_interval_days, (int, float))
                or not math.isfinite(float(self.median_interval_days))
                or self.median_interval_days <= 0
                or not isinstance(interval, (tuple, list))
                or len(interval) != 2
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value <= 0
                    for value in interval
                )
                or interval[0] > interval[1]
                or not interval[0] <= self.median_interval_days <= interval[1]
                or isinstance(self.maximum_absolute_interval_deviation_days, bool)
                or not isinstance(self.maximum_absolute_interval_deviation_days, (int, float))
                or not math.isfinite(float(self.maximum_absolute_interval_deviation_days))
                or self.maximum_absolute_interval_deviation_days < 0
            ):
                raise ValueError("recurrence interval evidence is invalid")
            expected_deviation = max(
                self.median_interval_days - interval[0],
                interval[1] - self.median_interval_days,
            )
            if self.maximum_absolute_interval_deviation_days != expected_deviation:
                raise ValueError("recurrence deviation does not match interval")
            if self.state == "recurrent" and (
                self.maximum_absolute_interval_deviation_days > self.deviation_ceiling_days
            ):
                raise ValueError("recurrent state exceeds deviation ceiling")
            if self.state == "irregular" and (
                self.maximum_absolute_interval_deviation_days <= self.deviation_ceiling_days
            ):
                raise ValueError("irregular state does not exceed deviation ceiling")
            derived_median = float(statistics.median(self.interval_days))
            derived_range = (min(self.interval_days), max(self.interval_days))
            derived_deviation = float(
                max(abs(value - derived_median) for value in self.interval_days)
            )
            if (
                self.median_interval_days != derived_median
                or tuple(self.interval_range_days) != derived_range
                or self.maximum_absolute_interval_deviation_days != derived_deviation
            ):
                raise ValueError("recurrence summary does not match interval days")
        if self.occurrence_count == 0:
            if self.first_occurrence is not None or self.last_occurrence is not None:
                raise ValueError("empty recurrence cannot carry occurrence dates")
        elif (
            self.first_occurrence is None
            or self.last_occurrence is None
            or self.first_occurrence > self.last_occurrence
            or self.last_occurrence >= self.as_of
        ):
            raise ValueError("recurrence occurrence dates are invalid")
        if self.occurrence_count > 0:
            normalized_receipts = _receipts(self.receipt_ids)
            if len(normalized_receipts) < self.occurrence_count:
                raise ValueError("recurrence receipt count is below occurrence count")
            object.__setattr__(self, "receipt_ids", normalized_receipts)
        elif self.receipt_ids:
            raise ValueError("empty recurrence cannot carry receipts")
        if stats_present:
            span_days = (self.last_occurrence - self.first_occurrence).days
            if sum(self.interval_days) != span_days:
                raise ValueError("recurrence occurrence span contradicts interval range")


def _route(item: object) -> tuple[object, object]:
    if isinstance(item, RecurrenceOccurrence):
        return item.pattern_id, item.market
    if isinstance(item, Mapping):
        return item.get("pattern_id"), item.get("market")
    return getattr(item, "pattern_id", None), getattr(item, "market", None)


def detect_historical_recurrence(
    *,
    occurrences: Iterable[RecurrenceOccurrence],
    pattern_id: str,
    market: str,
    as_of: date,
    rules: RecurrenceRules,
) -> RecurrenceAssessment:
    """Assess one pattern and market from pre-as-of occurrence history."""
    pattern_id = _text(pattern_id, "pattern id")
    market = _market(market)
    as_of = _date(as_of, "as of")
    if not isinstance(rules, RecurrenceRules):
        raise ValueError("recurrence rules are invalid")
    if isinstance(occurrences, (str, bytes)):
        raise ValueError("recurrence occurrences must be iterable")
    try:
        supplied = tuple(occurrences)
    except TypeError as error:
        raise ValueError("recurrence occurrences must be iterable") from error
    comparable = []
    for item in supplied:
        item_pattern, item_market = _route(item)
        if item_pattern != pattern_id or item_market != market:
            if item_pattern is None or item_market is None:
                raise ValueError("recurrence occurrences are invalid")
            continue
        if not isinstance(item, RecurrenceOccurrence):
            raise ValueError("comparable recurrence occurrence is invalid")
        comparable.append(item)
    ordered = tuple(sorted(comparable, key=lambda item: (item.occurred_on, item.occurrence_id)))
    occurrence_ids = tuple(item.occurrence_id for item in ordered)
    if len(occurrence_ids) != len(set(occurrence_ids)):
        raise ValueError("duplicate occurrence id")
    occurrence_dates = tuple(item.occurred_on for item in ordered)
    if len(occurrence_dates) != len(set(occurrence_dates)):
        raise ValueError("duplicate occurrence date")
    all_receipts = tuple(receipt for item in ordered for receipt in item.receipt_ids)
    if len(all_receipts) != len(set(all_receipts)):
        raise ValueError("duplicate occurrence receipt")
    if any(item.occurred_on >= as_of for item in ordered):
        raise ValueError("comparable occurrences must be before as of")
    receipts = tuple(sorted(all_receipts))
    count = len(ordered)
    common = {
        "pattern_id": pattern_id,
        "market": market,
        "as_of": as_of,
        "occurrence_count": count,
        "first_occurrence": ordered[0].occurred_on if ordered else None,
        "last_occurrence": ordered[-1].occurred_on if ordered else None,
        "receipt_ids": receipts,
        "rule_version": rules.rule_version,
        "display_eligible": False,
    }
    if count < rules.minimum_occurrences:
        return RecurrenceAssessment(
            state="insufficient_history",
            reason="minimum_occurrences_not_met",
            median_interval_days=None,
            interval_days=(),
            interval_range_days=None,
            maximum_absolute_interval_deviation_days=None,
            deviation_ceiling_days=rules.maximum_absolute_interval_deviation_days,
            **common,
        )
    intervals = tuple(
        (later.occurred_on - earlier.occurred_on).days for earlier, later in pairwise(ordered)
    )
    median = float(statistics.median(intervals))
    interval_range = (min(intervals), max(intervals))
    deviation = float(max(abs(interval - median) for interval in intervals))
    recurrent = deviation <= rules.maximum_absolute_interval_deviation_days
    return RecurrenceAssessment(
        state="recurrent" if recurrent else "irregular",
        reason="bounded_intervals" if recurrent else "interval_deviation_above_ceiling",
        median_interval_days=median,
        interval_days=intervals,
        interval_range_days=interval_range,
        maximum_absolute_interval_deviation_days=deviation,
        deviation_ceiling_days=rules.maximum_absolute_interval_deviation_days,
        **common,
    )


def detect_historical_recurrence_with_provenance(
    *,
    occurrences: Iterable[RecurrenceOccurrence],
    pattern_id: str,
    market: str,
    as_of: date,
    rules: RecurrenceRules,
    evidence_projection: object,
) -> tuple[RecurrenceAssessment, tuple[HistoricalEvidenceProvenanceBinding, ...]]:
    items = tuple(occurrences)
    assessment = detect_historical_recurrence(
        occurrences=items, pattern_id=pattern_id, market=market, as_of=as_of, rules=rules
    )
    snapshot = runtime_projection_snapshot(evidence_projection)
    refs = {item.evidence_id: item for item in snapshot.evidence_refs}
    comparable = tuple(
        item for item in items if item.pattern_id == pattern_id and item.market == market
    )
    expected_ids = tuple(sorted(receipt for item in comparable for receipt in item.receipt_ids))
    if set(refs) != set(expected_ids):
        code = (
            "historical_provenance_binding_missing"
            if set(expected_ids) - set(refs)
            else "historical_provenance_binding_extra"
        )
        raise HistoricalBridgeError(code, "recurrence projection coverage")
    bindings = []
    for occurrence in comparable:
        for receipt_id in occurrence.receipt_ids:
            ref = refs[receipt_id]
            if ref.market != occurrence.market:
                raise HistoricalBridgeError(
                    "historical_provenance_source_object_mismatch", "recurrence market"
                )
            if ref.published_at.date() > occurrence.occurred_on:
                raise HistoricalBridgeError("historical_future_leak", "recurrence cutoff")
            bindings.append(
                build_recurrence_binding(
                    occurrence=occurrence,
                    receipt_id=receipt_id,
                    expected_signal_id=ref.signal_id,
                    expected_source_family=ref.source_family,
                    rule_version=rules.rule_version,
                )
            )
    if assessment.state != "insufficient_history" and set(assessment.receipt_ids) != set(
        expected_ids
    ):
        raise HistoricalBridgeError(
            "historical_receipt_binding_coverage_mismatch", "recurrence result"
        )
    return assessment, tuple(sorted(bindings, key=binding_sort_key))
