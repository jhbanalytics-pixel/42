"""Pure measured source value and optional-credit allocation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

_MEASURED_STATUSES = frozenset({"active", "pilot"})
_INVENTORY_STATUSES = frozenset({"inventory_only", "available_unwired"})
_INELIGIBLE_STATUSES = frozenset({"blocked", "permanently_rejected"})


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _nonnegative(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{field} must be finite and nonnegative")
    return float(value)


def _bounded(value: object, field: str) -> float:
    result = _nonnegative(value, field)
    if result > 1:
        raise ValueError(f"{field} must be within zero and one")
    return result


def _optional(value: object, field: str, *, bounded: bool = False) -> float | None:
    if value is None:
        return None
    return _bounded(value, field) if bounded else _nonnegative(value, field)


def _optional_count(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


@dataclass(frozen=True, slots=True)
class SourcePerformanceObservation:
    endpoint_id: str
    status: str
    calls: int | None
    confirmed_credits: int | None
    usable_receipts: int | None
    unique_receipts: int | None
    integrity: float | None
    geo_precision: float | None
    median_lead_hours: float | None
    false_discovery_rate: float | None
    outcome_contribution: float | None
    kill_test_passed: bool | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint_id", _text(self.endpoint_id, "endpoint id"))
        if self.status not in _MEASURED_STATUSES | _INVENTORY_STATUSES | _INELIGIBLE_STATUSES:
            raise ValueError("source status is invalid")
        for field in ("calls", "confirmed_credits", "usable_receipts", "unique_receipts"):
            object.__setattr__(
                self,
                field,
                _optional_count(getattr(self, field), field.replace("_", " ")),
            )
        for field in ("integrity", "geo_precision", "false_discovery_rate", "outcome_contribution"):
            object.__setattr__(
                self,
                field,
                _optional(getattr(self, field), field.replace("_", " "), bounded=True),
            )
        object.__setattr__(
            self,
            "median_lead_hours",
            _optional(self.median_lead_hours, "median lead hours"),
        )
        if self.kill_test_passed is not None and type(self.kill_test_passed) is not bool:
            raise ValueError("kill test passed must be boolean or null")


@dataclass(frozen=True, slots=True)
class SourceYieldRules:
    efficiency_weight: float
    unique_lift_weight: float
    integrity_weight: float
    geo_precision_weight: float
    lead_time_weight: float
    precision_weight: float
    outcome_weight: float
    receipts_per_credit_cap: float
    maximum_lead_hours: float
    minimum_integrity: float
    minimum_geo_precision: float
    maximum_false_discovery_rate: float

    def __post_init__(self) -> None:
        weight_fields = (
            "efficiency_weight",
            "unique_lift_weight",
            "integrity_weight",
            "geo_precision_weight",
            "lead_time_weight",
            "precision_weight",
            "outcome_weight",
        )
        weights = tuple(getattr(self, field) for field in weight_fields)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
            for value in weights
        ):
            raise ValueError("source yield weights must be finite and sum to one")
        # Exactly rounded, so the divisor is the same on every version.
        # Plain sum() accumulated left to right before CPython 3.12 and
        # compensates after it, which moved this divisor and with it every
        # normalised weight below.
        total_weight = math.fsum(float(value) for value in weights)
        if not math.isclose(total_weight, 1.0, abs_tol=1e-9):
            raise ValueError("source yield weights must be finite and sum to one")
        for field in weight_fields:
            object.__setattr__(self, field, float(getattr(self, field)) / total_weight)
        for field in (
            "receipts_per_credit_cap",
            "maximum_lead_hours",
        ):
            value = _nonnegative(getattr(self, field), field.replace("_", " "))
            if value == 0:
                raise ValueError(f"{field.replace('_', ' ')} must be positive")
            object.__setattr__(self, field, value)
        for field in (
            "minimum_integrity",
            "minimum_geo_precision",
            "maximum_false_discovery_rate",
        ):
            object.__setattr__(self, field, _bounded(getattr(self, field), field.replace("_", " ")))


@dataclass(frozen=True, slots=True)
class SourceYieldEvaluation:
    endpoint_id: str
    state: str
    score: float | None
    usable_receipts_per_credit: float | None
    unique_receipts_per_credit: float | None
    unique_lift: float | None
    lead_time_score: float | None
    precision: float | None
    eligible_for_optional: bool
    kill_test_passed: bool | None
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint_id", _text(self.endpoint_id, "endpoint id"))
        if self.state not in {"eligible", "ineligible", "unmeasured"}:
            raise ValueError("source yield state is invalid")
        if self.score is not None:
            try:
                score = _bounded(self.score, "source yield score")
            except ValueError as error:
                raise ValueError("source yield score is invalid") from error
            object.__setattr__(self, "score", score)
        for field in ("usable_receipts_per_credit", "unique_receipts_per_credit"):
            object.__setattr__(
                self,
                field,
                _optional(getattr(self, field), field.replace("_", " ")),
            )
        for field in ("unique_lift", "lead_time_score", "precision"):
            object.__setattr__(
                self,
                field,
                _optional(getattr(self, field), field.replace("_", " "), bounded=True),
            )
        if type(self.eligible_for_optional) is not bool:
            raise ValueError("eligible for optional must be boolean")
        if (self.state == "eligible") != self.eligible_for_optional:
            raise ValueError("eligible state and flag disagree")
        if self.kill_test_passed is not None and type(self.kill_test_passed) is not bool:
            raise ValueError("kill test passed must be boolean or null")
        if not isinstance(self.reasons, (tuple, list)) or any(
            not isinstance(reason, str) or not reason for reason in self.reasons
        ):
            raise ValueError("source yield reasons are invalid")
        object.__setattr__(self, "reasons", tuple(self.reasons))
        if self.state == "eligible":
            components = (
                self.score,
                self.usable_receipts_per_credit,
                self.unique_receipts_per_credit,
                self.unique_lift,
                self.lead_time_score,
                self.precision,
            )
            if any(value is None for value in components):
                raise ValueError("eligible evaluations require complete metrics")
            if self.kill_test_passed is not True:
                raise ValueError("eligible evaluations require a passed kill test")
            if self.reasons:
                raise ValueError("eligible evaluations cannot carry rejection reasons")


@dataclass(frozen=True, slots=True)
class CreditEnvelope:
    funding_math_status: str
    funded_increase_observed: bool
    funded_increase_amount: int
    optional_funded_credits_used: int
    balance: int
    baseline_credits_per_day: float
    runway_floor_days: int
    monthly_optional_credit_cap: int
    monthly_optional_credits_used: int

    def __post_init__(self) -> None:
        if self.funding_math_status not in {"complete", "unknown"}:
            raise ValueError("funding math status is invalid")
        if type(self.funded_increase_observed) is not bool:
            raise ValueError("funded increase observed must be boolean")
        for field in (
            "funded_increase_amount",
            "optional_funded_credits_used",
            "balance",
            "runway_floor_days",
            "monthly_optional_credit_cap",
            "monthly_optional_credits_used",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field.replace('_', ' ')} must be a nonnegative integer")
        object.__setattr__(
            self,
            "baseline_credits_per_day",
            _nonnegative(self.baseline_credits_per_day, "baseline credits per day"),
        )


@dataclass(frozen=True, slots=True)
class RouteAllocation:
    endpoint_id: str
    credits: int
    score: float


def _unmeasured(item: SourcePerformanceObservation, reason: str) -> SourceYieldEvaluation:
    return SourceYieldEvaluation(
        endpoint_id=item.endpoint_id,
        state="unmeasured",
        score=None,
        usable_receipts_per_credit=None,
        unique_receipts_per_credit=None,
        unique_lift=None,
        lead_time_score=None,
        precision=None,
        eligible_for_optional=False,
        kill_test_passed=None,
        reasons=(reason,),
    )


def evaluate_source_yield(
    item: SourcePerformanceObservation, rules: SourceYieldRules
) -> SourceYieldEvaluation:
    if not isinstance(item, SourcePerformanceObservation):
        raise ValueError("source performance observation is invalid")
    if not isinstance(rules, SourceYieldRules):
        raise ValueError("source yield rules are invalid")
    if item.status in _INVENTORY_STATUSES:
        return _unmeasured(item, item.status)
    if item.status in _INELIGIBLE_STATUSES:
        return SourceYieldEvaluation(
            endpoint_id=item.endpoint_id,
            state="ineligible",
            score=None,
            usable_receipts_per_credit=None,
            unique_receipts_per_credit=None,
            unique_lift=None,
            lead_time_score=None,
            precision=None,
            eligible_for_optional=False,
            kill_test_passed=None,
            reasons=(item.status,),
        )
    required = (
        item.calls,
        item.confirmed_credits,
        item.usable_receipts,
        item.unique_receipts,
        item.integrity,
        item.geo_precision,
        item.median_lead_hours,
        item.false_discovery_rate,
        item.outcome_contribution,
        item.kill_test_passed,
    )
    if any(value is None for value in required):
        return _unmeasured(item, "incomplete_measurement")
    if item.confirmed_credits == 0:
        return _unmeasured(item, "no_confirmed_credit_spend")
    if item.calls == 0:
        return _unmeasured(item, "no_completed_calls")
    if item.unique_receipts > item.usable_receipts:
        raise ValueError("unique receipts cannot exceed usable receipts")
    per_credit = item.usable_receipts / item.confirmed_credits
    unique_per_credit = item.unique_receipts / item.confirmed_credits
    unique_lift = 0.0 if item.usable_receipts == 0 else item.unique_receipts / item.usable_receipts
    efficiency = min(1.0, unique_per_credit / rules.receipts_per_credit_cap)
    lead_time = max(0.0, 1.0 - item.median_lead_hours / rules.maximum_lead_hours)
    precision = 1.0 - item.false_discovery_rate
    score = (
        efficiency * rules.efficiency_weight
        + unique_lift * rules.unique_lift_weight
        + item.integrity * rules.integrity_weight
        + item.geo_precision * rules.geo_precision_weight
        + lead_time * rules.lead_time_weight
        + precision * rules.precision_weight
        + item.outcome_contribution * rules.outcome_weight
    )
    score = min(1.0, max(0.0, score))
    reasons = []
    if item.integrity < rules.minimum_integrity:
        reasons.append("integrity_below_floor")
    if item.geo_precision < rules.minimum_geo_precision:
        reasons.append("geo_precision_below_floor")
    if item.false_discovery_rate > rules.maximum_false_discovery_rate:
        reasons.append("false_discovery_above_ceiling")
    if item.kill_test_passed is False:
        reasons.append("kill_test_failed")
    eligible = not reasons
    return SourceYieldEvaluation(
        endpoint_id=item.endpoint_id,
        state="eligible" if eligible else "ineligible",
        score=score,
        usable_receipts_per_credit=per_credit,
        unique_receipts_per_credit=unique_per_credit,
        unique_lift=unique_lift,
        lead_time_score=lead_time,
        precision=precision,
        eligible_for_optional=eligible,
        kill_test_passed=item.kill_test_passed,
        reasons=tuple(reasons),
    )


def available_optional_credits(envelope: CreditEnvelope) -> int:
    if not isinstance(envelope, CreditEnvelope):
        raise ValueError("credit envelope is invalid")
    if envelope.funding_math_status != "complete" or not envelope.funded_increase_observed:
        return 0
    funded_increase_remaining = max(
        0,
        envelope.funded_increase_amount - envelope.optional_funded_credits_used,
    )
    monthly_remaining = max(
        0,
        envelope.monthly_optional_credit_cap - envelope.monthly_optional_credits_used,
    )
    reserve = math.ceil(envelope.baseline_credits_per_day * envelope.runway_floor_days)
    runway_remaining = max(0, envelope.balance - reserve)
    return min(funded_increase_remaining, monthly_remaining, runway_remaining)


def allocate_optional_credits(
    evaluations: object,
    envelope: CreditEnvelope,
    *,
    route_caps: Mapping[str, int],
) -> tuple[RouteAllocation, ...]:
    try:
        items = tuple(evaluations)
    except TypeError as error:
        raise ValueError("source yield evaluations must be iterable") from error
    if any(not isinstance(item, SourceYieldEvaluation) for item in items):
        raise ValueError("source yield evaluations are invalid")
    ids = tuple(item.endpoint_id for item in items)
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate source yield endpoint")
    if not isinstance(route_caps, Mapping):
        raise ValueError("route caps must be an object")
    caps = {}
    for endpoint_id, cap in route_caps.items():
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
            raise ValueError("route cap must be a nonnegative integer")
        normalized_endpoint_id = _text(endpoint_id, "route cap endpoint id")
        if normalized_endpoint_id in caps:
            raise ValueError("duplicate normalized route cap endpoint")
        caps[normalized_endpoint_id] = cap
    budget = available_optional_credits(envelope)
    eligible = {}
    for item in items:
        bounded_components = (item.unique_lift, item.lead_time_score, item.precision)
        nonnegative_components = (
            item.usable_receipts_per_credit,
            item.unique_receipts_per_credit,
        )
        if (
            item.state != "eligible"
            or item.eligible_for_optional is not True
            or item.kill_test_passed is not True
            or item.reasons != ()
            or item.score is None
            or isinstance(item.score, bool)
            or not isinstance(item.score, (int, float))
            or not math.isfinite(float(item.score))
            or not 0 < item.score <= 1
            or any(
                value is None
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0 <= value <= 1
                for value in bounded_components
            )
            or any(
                value is None
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
                for value in nonnegative_components
            )
            or caps.get(item.endpoint_id, 0) <= 0
        ):
            continue
        eligible[item.endpoint_id] = item
    allocated = dict.fromkeys(eligible, 0)
    remaining = budget
    active = set(eligible)
    while remaining > 0 and active:
        total_score = sum(eligible[endpoint_id].score for endpoint_id in active)
        if total_score <= 0:
            break
        shares = {
            endpoint_id: remaining * eligible[endpoint_id].score / total_score
            for endpoint_id in active
        }
        grants = {
            endpoint_id: min(
                caps[endpoint_id] - allocated[endpoint_id],
                math.floor(shares[endpoint_id]),
            )
            for endpoint_id in active
        }
        for endpoint_id, grant in grants.items():
            allocated[endpoint_id] += grant
        remaining -= sum(grants.values())
        if remaining > 0:
            remainder_order = sorted(
                (
                    endpoint_id
                    for endpoint_id in active
                    if allocated[endpoint_id] < caps[endpoint_id]
                ),
                key=lambda item: (
                    -(shares[item] - math.floor(shares[item])),
                    -eligible[item].score,
                    item,
                ),
            )
            for endpoint_id in remainder_order:
                if remaining == 0:
                    break
                allocated[endpoint_id] += 1
                remaining -= 1
        active = {
            endpoint_id for endpoint_id in active if allocated[endpoint_id] < caps[endpoint_id]
        }
    return tuple(
        RouteAllocation(endpoint_id, allocated[endpoint_id], eligible[endpoint_id].score)
        for endpoint_id in sorted(allocated)
        if allocated[endpoint_id] > 0
    )
