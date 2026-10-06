"""Future-bounded empirical cross-market diffusion forecasts."""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from src.analysis.open_intelligence.historical_provenance import (
    HistoricalBridgeError,
    HistoricalEvidenceProvenanceBinding,
    binding_sort_key,
    build_diffusion_binding,
    runtime_projection_snapshot,
)

_SIGNAL_ID = re.compile(r"sig_[0-9a-f]{64}\Z")
_RECEIPT_ID = re.compile(r"ev_[0-9a-f]{64}\Z")
_MARKET = re.compile(r"[a-z]{2}\Z")
_SOURCE_FAMILY = re.compile(r"[a-z][a-z0-9_]*\Z")
_AGGREGATE_SOURCE_FAMILIES = frozenset({"all", "all_sources", "aggregate", "cross_platform"})
_STATES = frozenset(
    {
        "forecast",
        "insufficient_history",
        "unbounded_dispersion",
        "already_observed",
        "invalidation_elapsed",
    }
)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _market(value: object, field: str) -> str:
    market = _text(value, field)
    if _MARKET.fullmatch(market) is None:
        raise ValueError(f"{field} must be a lower-case market code")
    return market


def _date(value: object, field: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{field} must be a date")
    return value


def _string_tuple(value: object, field: str, *, pattern: re.Pattern[str]) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list, set, frozenset)):
        raise ValueError(f"{field} must be a collection")
    items = tuple(value)
    if not items or any(
        not isinstance(item, str) or pattern.fullmatch(item) is None for item in items
    ):
        raise ValueError(f"{field} contains an invalid value")
    if len(items) != len(set(items)):
        raise ValueError(f"{field} contains duplicates")
    return tuple(sorted(items))


def _carrier_markets(value: object) -> tuple[str, ...]:
    markets = _string_tuple(value, "carrier markets", pattern=_MARKET)
    return markets


def _source_families(value: object) -> tuple[str, ...]:
    families = _string_tuple(value, "source families", pattern=_SOURCE_FAMILY)
    if len(families) < 2:
        raise ValueError("source families require at least two values")
    if any(family in _AGGREGATE_SOURCE_FAMILIES for family in families):
        raise ValueError("aggregate source family is invalid")
    return families


@dataclass(frozen=True, slots=True)
class HistoricalDiffusionSequence:
    sequence_id: str
    origin_market: str
    origin_date: date
    target_market: str
    target_date: date
    receipt_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sequence_id", _text(self.sequence_id, "sequence id"))
        object.__setattr__(self, "origin_market", _market(self.origin_market, "origin market"))
        object.__setattr__(self, "target_market", _market(self.target_market, "target market"))
        if self.origin_market == self.target_market:
            raise ValueError("diffusion requires different markets")
        object.__setattr__(self, "origin_date", _date(self.origin_date, "origin date"))
        object.__setattr__(self, "target_date", _date(self.target_date, "target date"))
        if self.target_date <= self.origin_date:
            raise ValueError("target date must be after origin date")
        object.__setattr__(
            self,
            "receipt_ids",
            _string_tuple(self.receipt_ids, "receipt ids", pattern=_RECEIPT_ID),
        )

    @property
    def lag_days(self) -> int:
        return (self.target_date - self.origin_date).days


@dataclass(frozen=True, slots=True)
class CurrentDiffusionSignal:
    signal_id: str
    origin_market: str
    origin_date: date
    as_of: date
    carrier_markets: tuple[str, ...]
    source_families: tuple[str, ...]

    def __post_init__(self) -> None:
        signal_id = _text(self.signal_id, "signal id")
        if _SIGNAL_ID.fullmatch(signal_id) is None:
            raise ValueError("signal id must be a full signal identifier")
        object.__setattr__(self, "signal_id", signal_id)
        object.__setattr__(self, "origin_market", _market(self.origin_market, "origin market"))
        object.__setattr__(self, "origin_date", _date(self.origin_date, "origin date"))
        object.__setattr__(self, "as_of", _date(self.as_of, "as of"))
        if self.as_of < self.origin_date:
            raise ValueError("as of cannot be before origin date")
        object.__setattr__(self, "carrier_markets", _carrier_markets(self.carrier_markets))
        if self.origin_market not in self.carrier_markets:
            raise ValueError("origin market must be a current carrier")
        object.__setattr__(self, "source_families", _source_families(self.source_families))


@dataclass(frozen=True, slots=True)
class DiffusionRules:
    minimum_comparable_sequences: int = 3
    maximum_absolute_deviation_days: float = 2.0
    rule_version: str = "diffusion_rules_v1"

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_comparable_sequences, bool)
            or not isinstance(self.minimum_comparable_sequences, int)
            or self.minimum_comparable_sequences < 3
        ):
            raise ValueError("minimum comparable sequences must be an integer of at least three")
        if (
            isinstance(self.maximum_absolute_deviation_days, bool)
            or not isinstance(self.maximum_absolute_deviation_days, (int, float))
            or not math.isfinite(float(self.maximum_absolute_deviation_days))
            or float(self.maximum_absolute_deviation_days) < 0
        ):
            raise ValueError("maximum absolute deviation ceiling must be finite and nonnegative")
        object.__setattr__(
            self,
            "maximum_absolute_deviation_days",
            float(self.maximum_absolute_deviation_days),
        )
        object.__setattr__(self, "rule_version", _text(self.rule_version, "rule version"))


@dataclass(frozen=True, slots=True)
class DiffusionForecast:
    state: str
    reason: str
    signal_id: str
    as_of: date
    origin_market: str
    origin_date: date
    current_carrier_markets: tuple[str, ...]
    current_source_families: tuple[str, ...]
    historical_analogue_count: int
    median_lag_days: float | None
    lag_interval_days: tuple[int, int] | None
    maximum_absolute_deviation_days: float | None
    target_market: str
    forecast_date: date | None
    invalidation_date: date | None
    confidence: str
    receipt_ids: tuple[str, ...]
    rule_version: str
    display_eligible: bool

    def __post_init__(self) -> None:
        if self.state not in _STATES:
            raise ValueError("diffusion forecast state is invalid")
        if self.confidence not in {"bounded", "none"}:
            raise ValueError("diffusion confidence is invalid")
        if type(self.display_eligible) is not bool or self.display_eligible:
            raise ValueError("diffusion forecast must remain display ineligible")
        expected_reason = {
            "forecast": "bounded_history",
            "insufficient_history": "minimum_comparable_sequences_not_met",
            "unbounded_dispersion": "maximum_absolute_deviation_above_ceiling",
            "already_observed": "target_market_is_current_carrier",
            "invalidation_elapsed": "upper_lag_bound_elapsed",
        }[self.state]
        if self.reason != expected_reason:
            raise ValueError("diffusion state and reason disagree")
        if (
            isinstance(self.historical_analogue_count, bool)
            or not isinstance(self.historical_analogue_count, int)
            or self.historical_analogue_count < 0
        ):
            raise ValueError("historical analogue count is invalid")
        required = (
            self.median_lag_days,
            self.lag_interval_days,
            self.maximum_absolute_deviation_days,
            self.forecast_date,
            self.invalidation_date,
        )
        stats = required[:3]
        stats_present = all(value is not None for value in stats)
        if stats_present:
            interval = self.lag_interval_days
            if (
                isinstance(self.median_lag_days, bool)
                or not isinstance(self.median_lag_days, (int, float))
                or not math.isfinite(float(self.median_lag_days))
                or self.median_lag_days <= 0
                or not isinstance(interval, (tuple, list))
                or len(interval) != 2
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value <= 0
                    for value in interval
                )
                or interval[0] > interval[1]
                or not interval[0] <= self.median_lag_days <= interval[1]
                or isinstance(self.maximum_absolute_deviation_days, bool)
                or not isinstance(self.maximum_absolute_deviation_days, (int, float))
                or not math.isfinite(float(self.maximum_absolute_deviation_days))
                or self.maximum_absolute_deviation_days < 0
            ):
                raise ValueError("diffusion lag evidence is invalid")
            expected_deviation = max(
                self.median_lag_days - interval[0],
                interval[1] - self.median_lag_days,
            )
            if not math.isclose(
                self.maximum_absolute_deviation_days,
                expected_deviation,
                abs_tol=1e-9,
            ):
                raise ValueError("diffusion deviation does not match interval")
        elif any(value is not None for value in stats):
            raise ValueError("diffusion lag evidence is incomplete")
        if self.state == "forecast" and (
            any(value is None for value in required)
            or self.confidence != "bounded"
            or self.historical_analogue_count < 3
        ):
            raise ValueError("forecast state requires complete bounded evidence")
        if self.state == "invalidation_elapsed" and (
            any(value is None for value in required)
            or self.confidence != "none"
            or self.historical_analogue_count < 3
        ):
            raise ValueError("elapsed state requires complete unpromoted evidence")
        if self.state != "forecast" and self.confidence != "none":
            raise ValueError("blocked forecast confidence must be none")
        if self.state == "unbounded_dispersion" and (
            not stats_present or self.historical_analogue_count < 3
        ):
            raise ValueError("unbounded state requires complete lag evidence")
        if self.state in {"insufficient_history", "already_observed"} and stats_present:
            raise ValueError("unscored state cannot carry lag evidence")
        if self.state not in {"forecast", "invalidation_elapsed"} and (
            self.forecast_date is not None or self.invalidation_date is not None
        ):
            raise ValueError("blocked forecast cannot carry forecast dates")
        if self.state in {"forecast", "invalidation_elapsed"} and (
            self.forecast_date < self.origin_date or self.invalidation_date < self.forecast_date
        ):
            raise ValueError("diffusion forecast dates are invalid")
        if self.state in {"forecast", "invalidation_elapsed"} and (
            self.forecast_date != self.origin_date + timedelta(days=math.ceil(self.median_lag_days))
            or self.invalidation_date
            != self.origin_date + timedelta(days=self.lag_interval_days[1])
        ):
            raise ValueError("diffusion forecast dates do not match lag evidence")
        if self.state == "forecast" and self.as_of > self.invalidation_date:
            raise ValueError("forecast state is past invalidation")
        if self.state == "invalidation_elapsed" and self.as_of <= self.invalidation_date:
            raise ValueError("elapsed state has not passed invalidation")


def _empty(
    current: CurrentDiffusionSignal,
    target_market: str,
    rules: DiffusionRules,
    *,
    state: str,
    reason: str,
    count: int,
    receipts: tuple[str, ...],
    median: float | None = None,
    interval: tuple[int, int] | None = None,
    deviation: float | None = None,
    forecast_date: date | None = None,
    invalidation_date: date | None = None,
) -> DiffusionForecast:
    return DiffusionForecast(
        state=state,
        reason=reason,
        signal_id=current.signal_id,
        as_of=current.as_of,
        origin_market=current.origin_market,
        origin_date=current.origin_date,
        current_carrier_markets=current.carrier_markets,
        current_source_families=current.source_families,
        historical_analogue_count=count,
        median_lag_days=median,
        lag_interval_days=interval,
        maximum_absolute_deviation_days=deviation,
        target_market=target_market,
        forecast_date=forecast_date,
        invalidation_date=invalidation_date,
        confidence="none",
        receipt_ids=receipts,
        rule_version=rules.rule_version,
        display_eligible=False,
    )


def forecast_cross_market_diffusion_with_provenance(
    *,
    current: CurrentDiffusionSignal,
    historical_sequences: Iterable[HistoricalDiffusionSequence],
    target_market: str,
    rules: DiffusionRules,
    evidence_projection: object,
) -> tuple[DiffusionForecast, tuple[HistoricalEvidenceProvenanceBinding, ...]]:
    sequences = tuple(historical_sequences)
    forecast = forecast_cross_market_diffusion(
        current=current,
        historical_sequences=sequences,
        target_market=target_market,
        rules=rules,
    )
    snapshot = runtime_projection_snapshot(evidence_projection)
    refs = {item.evidence_id: item for item in snapshot.evidence_refs}
    comparable = tuple(
        item
        for item in sequences
        if item.origin_market == current.origin_market and item.target_market == target_market
    )
    expected_ids = tuple(sorted(receipt for item in comparable for receipt in item.receipt_ids))
    if set(expected_ids) - set(refs):
        raise HistoricalBridgeError(
            "historical_provenance_binding_missing", "diffusion projection coverage"
        )
    bindings = []
    for sequence in comparable:
        for receipt_id in sequence.receipt_ids:
            ref = refs[receipt_id]
            if ref.market != sequence.target_market:
                raise HistoricalBridgeError(
                    "historical_provenance_source_object_mismatch", "diffusion market"
                )
            if ref.published_at.date() > sequence.target_date:
                raise HistoricalBridgeError("historical_future_leak", "diffusion cutoff")
            bindings.append(
                build_diffusion_binding(
                    sequence=sequence,
                    receipt_id=receipt_id,
                    expected_signal_id=ref.signal_id,
                    expected_source_family=ref.source_family,
                    rule_version=rules.rule_version,
                )
            )
    if forecast.receipt_ids and set(forecast.receipt_ids) != set(expected_ids):
        raise HistoricalBridgeError(
            "historical_receipt_binding_coverage_mismatch", "diffusion result"
        )
    return forecast, tuple(sorted(bindings, key=binding_sort_key))


def forecast_cross_market_diffusion(
    *,
    current: CurrentDiffusionSignal,
    historical_sequences: Iterable[HistoricalDiffusionSequence],
    target_market: str,
    rules: DiffusionRules,
) -> DiffusionForecast:
    """Forecast one market route from completed pre-signal history only."""
    if not isinstance(current, CurrentDiffusionSignal):
        raise ValueError("current diffusion signal is invalid")
    if not isinstance(rules, DiffusionRules):
        raise ValueError("diffusion rules are invalid")
    target_market = _market(target_market, "target market")
    if target_market == current.origin_market:
        raise ValueError("diffusion requires different markets")
    if target_market in current.carrier_markets:
        return _empty(
            current,
            target_market,
            rules,
            state="already_observed",
            reason="target_market_is_current_carrier",
            count=0,
            receipts=(),
        )
    if isinstance(historical_sequences, (str, bytes)):
        raise ValueError("historical sequences must be iterable")
    try:
        sequences = tuple(historical_sequences)
    except TypeError as error:
        raise ValueError("historical sequences must be iterable") from error
    comparable_items = []
    for item in sequences:
        if isinstance(item, HistoricalDiffusionSequence):
            origin_market = item.origin_market
            item_target_market = item.target_market
        elif isinstance(item, Mapping):
            origin_market = item.get("origin_market")
            item_target_market = item.get("target_market")
            if origin_market != current.origin_market or item_target_market != target_market:
                continue
            raise ValueError("comparable historical sequence is invalid")
        else:
            origin_market = getattr(item, "origin_market", None)
            item_target_market = getattr(item, "target_market", None)
            if origin_market is None or item_target_market is None:
                raise ValueError("historical sequences are invalid")
            if origin_market != current.origin_market or item_target_market != target_market:
                continue
            raise ValueError("comparable historical sequence is invalid")
        if origin_market == current.origin_market and item_target_market == target_market:
            comparable_items.append(item)
    comparable = tuple(
        sorted(
            comparable_items,
            key=lambda item: (item.origin_date, item.target_date, item.sequence_id),
        )
    )
    sequence_ids = tuple(item.sequence_id for item in comparable)
    if len(sequence_ids) != len(set(sequence_ids)):
        raise ValueError("duplicate historical sequence")
    all_receipts = tuple(receipt for item in comparable for receipt in item.receipt_ids)
    if len(all_receipts) != len(set(all_receipts)):
        raise ValueError("duplicate historical receipt")
    if any(item.target_date >= current.origin_date for item in comparable):
        raise ValueError("historical sequences must complete before current origin")
    receipts = tuple(sorted(receipt for item in comparable for receipt in item.receipt_ids))
    count = len(comparable)
    if count < rules.minimum_comparable_sequences:
        return _empty(
            current,
            target_market,
            rules,
            state="insufficient_history",
            reason="minimum_comparable_sequences_not_met",
            count=count,
            receipts=receipts,
        )
    lags = tuple(item.lag_days for item in comparable)
    median = float(statistics.median(lags))
    interval = (min(lags), max(lags))
    deviation = float(max(abs(lag - median) for lag in lags))
    if deviation > rules.maximum_absolute_deviation_days:
        return _empty(
            current,
            target_market,
            rules,
            state="unbounded_dispersion",
            reason="maximum_absolute_deviation_above_ceiling",
            count=count,
            receipts=receipts,
            median=median,
            interval=interval,
            deviation=deviation,
        )
    forecast_date = current.origin_date + timedelta(days=math.ceil(median))
    invalidation_date = current.origin_date + timedelta(days=interval[1])
    if current.as_of > invalidation_date:
        return _empty(
            current,
            target_market,
            rules,
            state="invalidation_elapsed",
            reason="upper_lag_bound_elapsed",
            count=count,
            receipts=receipts,
            median=median,
            interval=interval,
            deviation=deviation,
            forecast_date=forecast_date,
            invalidation_date=invalidation_date,
        )
    return DiffusionForecast(
        state="forecast",
        reason="bounded_history",
        signal_id=current.signal_id,
        as_of=current.as_of,
        origin_market=current.origin_market,
        origin_date=current.origin_date,
        current_carrier_markets=current.carrier_markets,
        current_source_families=current.source_families,
        historical_analogue_count=count,
        median_lag_days=median,
        lag_interval_days=interval,
        maximum_absolute_deviation_days=deviation,
        target_market=target_market,
        forecast_date=forecast_date,
        invalidation_date=invalidation_date,
        confidence="bounded",
        receipt_ids=receipts,
        rule_version=rules.rule_version,
        display_eligible=False,
    )
