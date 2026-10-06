"""Deterministic outcome rows for promoted dynamic signals."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from src.analysis.open_intelligence.predictions import (
    PREDICTION_ROW_FIELDS,
    PREDICTION_ROW_FIELDS_V2,
    PREDICTION_TIME_CONTRACT_V2,
    first_evaluation_day,
)
from src.contracts.open_intelligence import OUTCOME_STATES, encode_identifier_part

OUTCOME_ROW_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
    "outcome_id",
    "prediction_id",
    "signal_id",
    "signal_date",
    "market",
    "discovery_mode",
    "source_families",
    "source_family_map_version",
    "evaluation_date",
    "evaluated_at",
    "outcome",
    "observed_velocity",
    "observed_breadth",
    "observed_evidence_family_count",
    "human_calibration_label",
    "human_reviewed_at",
    "resolution_reason",
    "rule_version",
)

_PREDICTION_ID = re.compile(r"pred_[0-9a-f]{64}\Z")
_SIGNAL_ID = re.compile(r"sig_[0-9a-f]{64}\Z")
_SOURCE_FAMILY = re.compile(r"[a-z][a-z0-9_]*\Z")
_DISCOVERY_MODES = frozenset({"dynamic", "replay", "canary"})
_EXPECTED_TRAJECTORIES = frozenset({"growing", "sustained", "peaked", "fading"})
_AGGREGATE_SOURCE_FAMILIES = frozenset({"all", "all_sources", "aggregate", "cross_platform"})
_QUALITY_FAILURES = frozenset(
    {
        "incoherence",
        "foreign_market_leakage",
        "duplicate_identity",
        "unsupported_identity",
    }
)
_METRIC_FIELDS = ("velocity", "breadth", "evidence_family_count")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone aware")
    return value.astimezone(UTC)


def _date(value: object, field: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{field} must be a date")
    return value


def _optional_score(value: object, field: str) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise ValueError(f"{field} must be finite within zero and one or null")
    return float(value)


def _optional_count(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer or null")
    return value


def _identifier(*parts: str) -> str:
    canonical = "|".join(encode_identifier_part(part) for part in parts)
    return "out_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _string_list(value: object, field: str, *, nonempty: bool) -> list[str]:
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{field} must be a list")
    if nonempty and not value:
        raise ValueError(f"{field} must not be empty")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{field} contains an invalid value")
    if len(value) != len(set(value)):
        raise ValueError(f"{field} contains duplicates")
    return list(value)


def _metric_contract(value: object, field: str) -> dict[str, float | int]:
    if not isinstance(value, Mapping) or tuple(value) != _METRIC_FIELDS:
        raise ValueError(f"{field} fields are invalid")
    velocity = _optional_score(value["velocity"], f"{field} velocity")
    breadth = _optional_score(value["breadth"], f"{field} breadth")
    family_count = _optional_count(value["evidence_family_count"], f"{field} evidence family count")
    if velocity is None or breadth is None or family_count is None:
        raise ValueError(f"{field} metrics cannot be null")
    return {
        "velocity": velocity,
        "breadth": breadth,
        "evidence_family_count": family_count,
    }


def _time_contract_v2(
    prediction: Mapping[str, object], contract: Mapping[str, object]
) -> dict[str, object]:
    if prediction["time_contract_version"] != PREDICTION_TIME_CONTRACT_V2:
        raise ValueError("prediction time contract is invalid")
    available_at = _timestamp(prediction["available_at"], "available at")
    if available_at < contract["predicted_at"]:
        raise ValueError("available at cannot be before predicted at")
    window_start = _date(prediction["evaluation_window_start"], "evaluation window start")
    if window_start != first_evaluation_day(available_at):
        raise ValueError("evaluation window start must follow availability")
    horizon = prediction["evaluation_horizon_days"]
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
        raise ValueError("evaluation horizon must be a positive integer")
    if contract["evaluation_date"] != window_start + timedelta(days=horizon - 1):
        raise ValueError("evaluation date must close the declared horizon")
    return {
        "time_contract_version": PREDICTION_TIME_CONTRACT_V2,
        "available_at": available_at,
        "evaluation_window_start": window_start,
        "evaluation_horizon_days": horizon,
    }


def _window_start(contract: Mapping[str, object]) -> date:
    declared = contract.get("evaluation_window_start")
    if declared is not None:
        return declared
    return contract["signal_date"] + timedelta(days=1)


def validate_prediction_row(prediction: object) -> dict[str, object]:
    if not isinstance(prediction, Mapping) or tuple(prediction) not in (
        PREDICTION_ROW_FIELDS,
        PREDICTION_ROW_FIELDS_V2,
    ):
        raise ValueError("prediction row fields are invalid")
    prediction_id = _text(prediction["prediction_id"], "prediction id")
    if _PREDICTION_ID.fullmatch(prediction_id) is None:
        raise ValueError("prediction id must be a full prediction identifier")
    signal_id = _text(prediction["signal_id"], "signal id")
    if _SIGNAL_ID.fullmatch(signal_id) is None:
        raise ValueError("signal id must be a full signal identifier")
    market_scope = _string_list(prediction["market_scope"], "market scope", nonempty=True)
    if any(market != market.lower() for market in market_scope):
        raise ValueError("market scope must contain lower-case markets")
    audience_lens_ids = _string_list(
        prediction["audience_lens_ids"], "audience lens ids", nonempty=False
    )
    market = _text(prediction["market"], "market")
    if market != market.lower() or market not in market_scope:
        raise ValueError("prediction market is outside market scope")
    discovery_mode = prediction["discovery_mode"]
    if discovery_mode not in _DISCOVERY_MODES:
        raise ValueError("prediction discovery mode is invalid")
    source_families = _string_list(prediction["source_families"], "source families", nonempty=True)
    if any(family in _AGGREGATE_SOURCE_FAMILIES for family in source_families):
        raise ValueError("aggregate source family is invalid")
    if (
        len(source_families) < 2
        or source_families != sorted(source_families)
        or any(_SOURCE_FAMILY.fullmatch(family) is None for family in source_families)
    ):
        raise ValueError("source families must be sorted normalized values")
    if prediction["evidence_state"] != "ready":
        raise ValueError("outcomes require a ready prediction")
    signal_date = _date(prediction["signal_date"], "signal date")
    evaluation_date = _date(prediction["evaluation_date"], "evaluation date")
    if evaluation_date <= signal_date:
        raise ValueError("evaluation date must be after signal date")
    run_id = _text(prediction["run_id"], "run id")
    first_seen_at = _timestamp(prediction["first_seen_at"], "first seen at")
    predicted_at = _timestamp(prediction["predicted_at"], "predicted at")
    if first_seen_at > predicted_at:
        raise ValueError("first seen at cannot be after predicted at")
    if signal_date > predicted_at.date():
        raise ValueError("signal date cannot be after predicted at")
    expected_trajectory = prediction["expected_trajectory"]
    if expected_trajectory not in _EXPECTED_TRAJECTORIES:
        raise ValueError("expected trajectory is invalid")
    display_eligible = prediction["display_eligible"]
    if type(display_eligible) is not bool:
        raise ValueError("display eligible must be boolean")
    contract = {
        "client_scope_id": _text(prediction["client_scope_id"], "client scope id"),
        "market_scope": market_scope,
        "brand_config_id": _text(prediction["brand_config_id"], "brand config id"),
        "audience_lens_ids": audience_lens_ids,
        "theme_id": _text(prediction["theme_id"], "theme id"),
        "run_id": run_id,
        "contract_version": _text(prediction["contract_version"], "contract version"),
        "prediction_id": prediction_id,
        "signal_id": signal_id,
        "signal_date": signal_date,
        "market": market,
        "discovery_mode": discovery_mode,
        "source_families": source_families,
        "source_family_map_version": _text(
            prediction["source_family_map_version"], "source family map version"
        ),
        "evaluation_date": evaluation_date,
        "first_seen_at": first_seen_at,
        "predicted_at": predicted_at,
        "expected_trajectory": expected_trajectory,
        "baseline": _metric_contract(prediction["baseline"], "baseline"),
        "promotion_target": _metric_contract(prediction["promotion_target"], "promotion target"),
        "invalidation_condition": _text(
            prediction["invalidation_condition"], "invalidation condition"
        ),
        "cluster_build_version": _text(
            prediction["cluster_build_version"], "cluster build version"
        ),
        "rule_version": _text(prediction["rule_version"], "rule version"),
        "display_eligible": display_eligible,
    }
    if tuple(prediction) == PREDICTION_ROW_FIELDS_V2:
        contract.update(_time_contract_v2(prediction, contract))
    return contract


def _quality_findings(
    value: object,
    *,
    predicted_at: datetime,
    evaluated_at: datetime,
    evaluation_date: date,
) -> tuple[OutcomeQualityFinding, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError("quality findings must be a collection")
    try:
        findings = tuple(value)
    except TypeError as error:
        raise ValueError("quality findings must be a collection") from error
    if any(not isinstance(item, OutcomeQualityFinding) for item in findings):
        raise ValueError("quality findings are invalid")
    if len(findings) != len(set(findings)):
        raise ValueError("duplicate quality finding")
    ordered = tuple(sorted(findings, key=lambda item: (item.found_at, item.reason)))
    for item in ordered:
        if item.found_at.date() > evaluation_date:
            raise ValueError("quality finding is outside the contracted evaluation window")
        if item.found_at <= predicted_at:
            raise ValueError("quality finding must be after predicted at")
        if item.found_at > evaluated_at:
            raise ValueError("quality finding cannot be after evaluated at")
    return ordered


@dataclass(frozen=True, slots=True)
class OutcomeRules:
    rule_version: str = "outcome_rules_v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_version", _text(self.rule_version, "rule version"))


@dataclass(frozen=True, slots=True)
class OutcomeObservation:
    observed_at: datetime
    velocity: float | None
    breadth: float | None
    evidence_family_count: int | None
    available_at: datetime | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _timestamp(self.observed_at, "observed at"))
        if (self.available_at is None) != (self.run_id is None):
            raise ValueError("observation availability requires its run id")
        if self.available_at is not None:
            available_at = _timestamp(self.available_at, "observation available at")
            if available_at < self.observed_at:
                raise ValueError("observation availability precedes its observation date")
            object.__setattr__(self, "available_at", available_at)
            object.__setattr__(self, "run_id", _text(self.run_id, "observation run id"))
        object.__setattr__(self, "velocity", _optional_score(self.velocity, "velocity"))
        object.__setattr__(self, "breadth", _optional_score(self.breadth, "breadth"))
        object.__setattr__(
            self,
            "evidence_family_count",
            _optional_count(self.evidence_family_count, "evidence family count"),
        )


@dataclass(frozen=True, slots=True)
class OutcomeQualityFinding:
    found_at: datetime
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "found_at", _timestamp(self.found_at, "quality found at"))
        if self.reason not in _QUALITY_FAILURES:
            raise ValueError("quality failure is invalid")


def _observations(
    value: object,
    *,
    predicted_at: datetime,
    evaluated_at: datetime,
    window_start: date,
    evaluation_date: date,
    availability_required: bool,
) -> tuple[OutcomeObservation, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError("outcome observations must be iterable")
    try:
        observations = tuple(value)
    except TypeError as error:
        raise ValueError("outcome observations must be iterable") from error
    if any(not isinstance(item, OutcomeObservation) for item in observations):
        raise ValueError("outcome observations are invalid")
    ordered = tuple(sorted(observations, key=lambda item: item.observed_at))
    dates = tuple(item.observed_at.date() for item in ordered)
    if len(dates) != len(set(dates)):
        raise ValueError("duplicate observation date")
    for item in ordered:
        if item.observed_at.date() < window_start or item.observed_at.date() > evaluation_date:
            raise ValueError("observation is outside the contracted evaluation window")
        if item.observed_at <= predicted_at:
            raise ValueError("observation must be after predicted at")
        if item.observed_at > evaluated_at:
            raise ValueError("observation cannot be after evaluated at")
        if availability_required and item.available_at is None:
            raise ValueError("observation availability is required")
        if item.available_at is not None and item.available_at > evaluated_at:
            raise ValueError("observation availability cannot be after evaluated at")
    return ordered


def _required_dates(signal_date: date, evaluation_date: date) -> tuple[date, ...]:
    days = (evaluation_date - signal_date).days
    if days < 1:
        raise ValueError("evaluation date must be after signal date")
    return tuple(signal_date + timedelta(days=offset) for offset in range(1, days + 1))


def _human_calibration(
    label: object,
    reviewed_at: object,
    *,
    predicted_at: datetime,
    evaluated_at: datetime,
    evaluation_date: date,
) -> tuple[str | None, datetime | None]:
    if label is None:
        if reviewed_at is not None:
            raise ValueError("human calibration label is required")
        return None, None
    normalized_label = _text(label, "human calibration label").lower()
    if reviewed_at is None:
        raise ValueError("human reviewed at is required")
    normalized_reviewed_at = _timestamp(reviewed_at, "human reviewed at")
    if normalized_reviewed_at <= predicted_at:
        raise ValueError("human reviewed at must be after predicted at")
    if normalized_reviewed_at.date() > evaluation_date:
        raise ValueError("human review is outside the contracted evaluation window")
    if normalized_reviewed_at > evaluated_at:
        raise ValueError("human reviewed at cannot be after evaluated at")
    return normalized_label, normalized_reviewed_at


def _classify(
    *,
    contract: Mapping[str, object],
    observations: tuple[OutcomeObservation, ...],
    evaluated_at: datetime,
    quality_failures: tuple[str, ...],
    human_calibration_label: str | None,
    review_record_missing: bool,
) -> tuple[str, str]:
    evaluation_date = contract["evaluation_date"]
    if evaluated_at.date() < evaluation_date:
        return "unresolved", "evaluation_window_open"
    if review_record_missing:
        return "unresolved", "review_record_missing"
    if quality_failures:
        return "noise", f"quality_failure:{quality_failures[0]}"
    if human_calibration_label == "noise":
        return "noise", "human_calibration_noise"
    required_dates = _required_dates(_window_start(contract) - timedelta(days=1), evaluation_date)
    observed_dates = tuple(item.observed_at.date() for item in observations)
    if observed_dates != required_dates:
        return "unresolved", "daily_window_incomplete"
    if any(
        item.velocity is None or item.breadth is None or item.evidence_family_count is None
        for item in observations
    ):
        return "unresolved", "observed_metrics_incomplete"
    baseline = contract["baseline"]
    target = contract["promotion_target"]
    sustained = all(
        item.breadth >= target["breadth"]
        and item.evidence_family_count >= target["evidence_family_count"]
        for item in observations
    )
    peaked = not sustained and any(
        item.velocity > baseline["velocity"]
        and item.breadth > baseline["breadth"]
        and item.evidence_family_count > baseline["evidence_family_count"]
        for item in observations
    )
    if sustained:
        return "sustained", "breadth_and_evidence_floor_sustained"
    if peaked:
        return "peaked", "promotion_baseline_exceeded"
    crossed_target = any(
        item.velocity >= target["velocity"]
        and item.breadth >= target["breadth"]
        and item.evidence_family_count >= target["evidence_family_count"]
        for item in observations
    )
    latest = observations[-1]
    if (
        not crossed_target
        and latest.velocity < target["velocity"]
        and latest.breadth < target["breadth"]
    ):
        return "fizzled", "velocity_and_breadth_lost_before_target"
    return "unresolved", "outcome_unresolved"


def build_signal_outcome_row(
    *,
    prediction: Mapping[str, object],
    observations: Iterable[OutcomeObservation],
    evaluated_at: datetime,
    evaluation_run_id: str,
    rules: OutcomeRules,
    quality_findings: Iterable[OutcomeQualityFinding] = (),
    human_calibration_label: str | None = None,
    human_reviewed_at: datetime | None = None,
    review_record_missing: bool = False,
) -> dict[str, object]:
    """Build one immutable, future-bounded outcome row."""
    if not isinstance(rules, OutcomeRules):
        raise ValueError("outcome rules are invalid")
    if type(review_record_missing) is not bool:
        raise ValueError("review record missing must be boolean")
    contract = validate_prediction_row(prediction)
    evaluated_at = _timestamp(evaluated_at, "evaluated at")
    if evaluated_at <= contract["predicted_at"]:
        raise ValueError("evaluated at must be after predicted at")
    evaluation_run_id = _text(evaluation_run_id, "evaluation run id")
    normalized_observations = _observations(
        observations,
        predicted_at=contract["predicted_at"],
        evaluated_at=evaluated_at,
        window_start=_window_start(contract),
        evaluation_date=contract["evaluation_date"],
        availability_required="time_contract_version" in contract,
    )
    normalized_findings = _quality_findings(
        quality_findings,
        predicted_at=contract["predicted_at"],
        evaluated_at=evaluated_at,
        evaluation_date=contract["evaluation_date"],
    )
    normalized_label, normalized_reviewed_at = _human_calibration(
        human_calibration_label,
        human_reviewed_at,
        predicted_at=contract["predicted_at"],
        evaluated_at=evaluated_at,
        evaluation_date=contract["evaluation_date"],
    )
    if review_record_missing and (
        normalized_findings or normalized_label is not None or normalized_reviewed_at is not None
    ):
        raise ValueError("review record missing cannot carry review inputs")
    outcome, reason = _classify(
        contract=contract,
        observations=normalized_observations,
        evaluated_at=evaluated_at,
        quality_failures=tuple(item.reason for item in normalized_findings),
        human_calibration_label=normalized_label,
        review_record_missing=review_record_missing,
    )
    latest = normalized_observations[-1] if normalized_observations else None
    row = {
        "client_scope_id": contract["client_scope_id"],
        "market_scope": contract["market_scope"],
        "brand_config_id": contract["brand_config_id"],
        "audience_lens_ids": contract["audience_lens_ids"],
        "theme_id": contract["theme_id"],
        "run_id": evaluation_run_id,
        "contract_version": contract["contract_version"],
        "outcome_id": _identifier(
            contract["prediction_id"],
            contract["evaluation_date"].isoformat(),
            evaluation_run_id,
        ),
        "prediction_id": contract["prediction_id"],
        "signal_id": contract["signal_id"],
        "signal_date": contract["signal_date"],
        "market": contract["market"],
        "discovery_mode": contract["discovery_mode"],
        "source_families": contract["source_families"],
        "source_family_map_version": contract["source_family_map_version"],
        "evaluation_date": contract["evaluation_date"],
        "evaluated_at": evaluated_at,
        "outcome": outcome,
        "observed_velocity": None if latest is None else latest.velocity,
        "observed_breadth": None if latest is None else latest.breadth,
        "observed_evidence_family_count": (
            None if latest is None else latest.evidence_family_count
        ),
        "human_calibration_label": normalized_label,
        "human_reviewed_at": normalized_reviewed_at,
        "resolution_reason": reason,
        "rule_version": rules.rule_version,
    }
    if outcome not in OUTCOME_STATES:
        raise AssertionError("outcome state drift")
    if tuple(row) != OUTCOME_ROW_FIELDS:
        raise AssertionError("outcome row field order drift")
    return row
