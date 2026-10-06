"""Deterministic immutable prediction rows for promoted dynamic signals."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType

from src.analysis.open_intelligence.candidates import (
    QUALIFYING_SOURCE_FAMILIES,
    SOURCE_FAMILY_MAP_VERSION,
)
from src.contracts.open_intelligence import encode_identifier_part

PREDICTION_ROW_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
    "prediction_id",
    "signal_id",
    "signal_date",
    "market",
    "discovery_mode",
    "source_families",
    "evidence_state",
    "first_seen_at",
    "predicted_at",
    "expected_trajectory",
    "evaluation_date",
    "baseline",
    "promotion_target",
    "invalidation_condition",
    "cluster_build_version",
    "source_family_map_version",
    "rule_version",
    "display_eligible",
)
_SIGNAL_ID = re.compile(r"sig_[0-9a-f]{64}\Z")
_DISCOVERY_MODES = frozenset({"dynamic", "replay", "canary"})
_ACTIVE_FAMILIES = QUALIFYING_SOURCE_FAMILIES - {"brand24"}
# Additive time contract (C06). No row producer calls it yet: build_signal_prediction_rows
# keeps its v1 signal_date arithmetic and bytes until the versioned schema is approved.
PREDICTION_TIME_CONTRACT_V2 = "open_intelligence_prediction_time_v2"
# Rows issued under the v2 time contract declare their window start and horizon.
PREDICTION_ROW_FIELDS_V2 = (
    *PREDICTION_ROW_FIELDS,
    "time_contract_version",
    "available_at",
    "evaluation_window_start",
    "evaluation_horizon_days",
)


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone aware")
    return value.astimezone(UTC)


def first_evaluation_day(available_at: datetime) -> date:
    """First complete UTC day after the prediction actually became available."""
    if available_at.tzinfo is None or available_at.utcoffset() is None:
        raise ValueError("availability_timezone_required")
    return available_at.astimezone(UTC).date() + timedelta(days=1)


# Window start rule per time contract. The v1 producer keeps its signal_date
# arithmetic in _row and has no entry here.
_WINDOW_START_RULES = MappingProxyType({PREDICTION_TIME_CONTRACT_V2: first_evaluation_day})


def _score(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise ValueError(f"{field} must be finite within zero and one")
    return float(value)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _families(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list, set, frozenset)):
        raise ValueError("source families must be a collection")
    if any(not isinstance(family, str) or family not in _ACTIVE_FAMILIES for family in value):
        raise ValueError("source family is unsupported")
    families = tuple(sorted(set(value)))
    if len(families) < 2:
        raise ValueError("promoted signals require two qualifying source families")
    return families


def _identifier(*parts: str) -> str:
    canonical = "|".join(encode_identifier_part(part) for part in parts)
    return "pred_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PredictionRules:
    evaluation_days: int = 7
    rule_version: str = "prediction_rules_v1"

    def __post_init__(self) -> None:
        if (
            isinstance(self.evaluation_days, bool)
            or not isinstance(self.evaluation_days, int)
            or self.evaluation_days < 1
        ):
            raise ValueError("evaluation days must be a positive integer")
        if not isinstance(self.rule_version, str) or not self.rule_version:
            raise ValueError("rule version must be a nonempty string")


@dataclass(frozen=True, slots=True)
class PromotedSignal:
    candidate: Mapping[str, object]
    source_families: object
    first_seen_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, Mapping):
            raise ValueError("candidate row is invalid")
        object.__setattr__(self, "source_families", _families(self.source_families))
        object.__setattr__(self, "first_seen_at", _timestamp(self.first_seen_at, "first seen at"))


def _candidate_value(candidate: Mapping[str, object], key: str) -> object:
    if key not in candidate:
        raise ValueError(f"candidate row is missing {key}")
    return candidate[key]


def _candidate_scope(candidate: Mapping[str, object]) -> dict[str, object]:
    client_scope_id = _text(_candidate_value(candidate, "client_scope_id"), "client scope id")
    market_scope = _candidate_value(candidate, "market_scope")
    audience_lens_ids = _candidate_value(candidate, "audience_lens_ids")
    if not isinstance(market_scope, (tuple, list)) or not market_scope:
        raise ValueError("market scope is invalid")
    if not isinstance(audience_lens_ids, (tuple, list)):
        raise ValueError("audience lens ids are invalid")
    if any(not isinstance(market, str) or market != market.lower() for market in market_scope):
        raise ValueError("market scope is invalid")
    if len(set(market_scope)) != len(market_scope):
        raise ValueError("market scope is invalid")
    return {
        "client_scope_id": client_scope_id,
        "market_scope": list(market_scope),
        "brand_config_id": _text(_candidate_value(candidate, "brand_config_id"), "brand config id"),
        "audience_lens_ids": list(audience_lens_ids),
        "theme_id": _text(_candidate_value(candidate, "theme_id"), "theme id"),
        "run_id": _text(_candidate_value(candidate, "run_id"), "run id"),
        "contract_version": _text(
            _candidate_value(candidate, "contract_version"), "contract version"
        ),
    }


def _prediction_rule(
    velocity: float, breadth: float, families: int, evaluation_date: date
) -> tuple[str, dict[str, object], str]:
    if velocity >= 0.75 and breadth >= 0.6:
        return (
            "growing",
            {"velocity": 0.7, "breadth": 0.6, "evidence_family_count": families},
            f"Velocity falls below 0.70 before {evaluation_date.isoformat()}.",
        )
    if velocity >= 0.6 and breadth >= 0.6:
        return (
            "sustained",
            {"velocity": 0.6, "breadth": 0.6, "evidence_family_count": families},
            f"Breadth falls below 0.60 before {evaluation_date.isoformat()}.",
        )
    if velocity >= 0.6:
        return (
            "peaked",
            {"velocity": 0.4, "breadth": 0.45, "evidence_family_count": families},
            f"Velocity falls below 0.40 before {evaluation_date.isoformat()}.",
        )
    return (
        "fading",
        {"velocity": 0.35, "breadth": 0.35, "evidence_family_count": families},
        f"Breadth falls below 0.35 before {evaluation_date.isoformat()}.",
    )


def _row(
    promoted: PromotedSignal, predicted_at: datetime, rules: PredictionRules
) -> dict[str, object]:
    candidate = promoted.candidate
    scope = _candidate_scope(candidate)
    signal_id = _candidate_value(candidate, "signal_id")
    if not isinstance(signal_id, str) or _SIGNAL_ID.fullmatch(signal_id) is None:
        raise ValueError("candidate signal id must be a full signal identifier")
    signal_date = _candidate_value(candidate, "signal_date")
    if isinstance(signal_date, datetime) or not isinstance(signal_date, date):
        raise ValueError("candidate signal date is invalid")
    market = _text(_candidate_value(candidate, "market"), "candidate market")
    if market not in scope["market_scope"]:
        raise ValueError("candidate market is outside market scope")
    discovery_mode = _candidate_value(candidate, "discovery_mode")
    if discovery_mode not in _DISCOVERY_MODES:
        raise ValueError("candidate discovery mode is unsupported")
    if _candidate_value(candidate, "evidence_state") != "ready":
        raise ValueError("only ready candidates may be promoted")
    if promoted.first_seen_at > predicted_at:
        raise ValueError("first seen at cannot be after predicted at")
    if signal_date > predicted_at.date():
        raise ValueError("candidate signal date cannot be after predicted at")
    velocity = _score(_candidate_value(candidate, "velocity_score"), "velocity score")
    breadth = _score(_candidate_value(candidate, "breadth_score"), "breadth score")
    evaluation_date = signal_date + timedelta(days=rules.evaluation_days)
    expected_trajectory, promotion_target, invalidation_condition = _prediction_rule(
        velocity, breadth, len(promoted.source_families), evaluation_date
    )
    return {
        **scope,
        "prediction_id": _identifier(
            scope["client_scope_id"],
            signal_date.isoformat(),
            market,
            signal_id,
            scope["run_id"],
            rules.rule_version,
        ),
        "signal_id": signal_id,
        "signal_date": signal_date,
        "market": market,
        "discovery_mode": discovery_mode,
        "source_families": list(promoted.source_families),
        "evidence_state": "ready",
        "first_seen_at": promoted.first_seen_at,
        "predicted_at": predicted_at,
        "expected_trajectory": expected_trajectory,
        "evaluation_date": evaluation_date,
        "baseline": {
            "velocity": velocity,
            "breadth": breadth,
            "evidence_family_count": len(promoted.source_families),
        },
        "promotion_target": promotion_target,
        "invalidation_condition": invalidation_condition,
        "cluster_build_version": _text(
            _candidate_value(candidate, "cluster_build_version"), "cluster build version"
        ),
        "source_family_map_version": SOURCE_FAMILY_MAP_VERSION,
        "rule_version": rules.rule_version,
        "display_eligible": False,
    }


def build_signal_prediction_rows(
    *,
    promoted_signals: Iterable[PromotedSignal],
    predicted_at: datetime,
    rules: PredictionRules,
) -> tuple[dict[str, object], ...]:
    """Build deterministic immutable prediction rows without model judgment."""
    if isinstance(promoted_signals, (str, bytes)):
        raise ValueError("promoted signals must be iterable")
    try:
        promoted = tuple(promoted_signals)
    except TypeError as error:
        raise ValueError("promoted signals must be iterable") from error
    if any(not isinstance(item, PromotedSignal) for item in promoted):
        raise ValueError("promoted signals are invalid")
    if not isinstance(rules, PredictionRules):
        raise ValueError("prediction rules are invalid")
    predicted_at = _timestamp(predicted_at, "predicted at")
    rows = tuple(
        sorted(
            (_row(item, predicted_at, rules) for item in promoted),
            key=lambda row: (row["market"], row["signal_date"], row["signal_id"]),
        )
    )
    prediction_ids = tuple(row["prediction_id"] for row in rows)
    if len(prediction_ids) != len(set(prediction_ids)):
        raise ValueError("duplicate immutable prediction id")
    if any(tuple(row) != PREDICTION_ROW_FIELDS for row in rows):
        raise AssertionError("prediction row field order drift")
    return rows


def _row_v2(
    promoted: PromotedSignal,
    predicted_at: datetime,
    available_at: datetime,
    rules: PredictionRules,
) -> dict[str, object]:
    base = _row(promoted, predicted_at, rules)
    window_start = _WINDOW_START_RULES[PREDICTION_TIME_CONTRACT_V2](available_at)
    available_at = available_at.astimezone(UTC)
    if available_at < predicted_at:
        raise ValueError("available at cannot be before predicted at")
    evaluation_date = window_start + timedelta(days=rules.evaluation_days - 1)
    baseline = base["baseline"]
    expected_trajectory, promotion_target, invalidation_condition = _prediction_rule(
        baseline["velocity"],
        baseline["breadth"],
        baseline["evidence_family_count"],
        evaluation_date,
    )
    row = dict(base)
    row["prediction_id"] = _identifier(
        base["client_scope_id"],
        base["signal_date"].isoformat(),
        base["market"],
        base["signal_id"],
        base["run_id"],
        rules.rule_version,
        PREDICTION_TIME_CONTRACT_V2,
    )
    row["expected_trajectory"] = expected_trajectory
    row["evaluation_date"] = evaluation_date
    row["promotion_target"] = promotion_target
    row["invalidation_condition"] = invalidation_condition
    row["time_contract_version"] = PREDICTION_TIME_CONTRACT_V2
    row["available_at"] = available_at
    row["evaluation_window_start"] = window_start
    row["evaluation_horizon_days"] = rules.evaluation_days
    return row


def build_signal_prediction_rows_v2(
    *,
    promoted_signals: Iterable[PromotedSignal],
    predicted_at: datetime,
    available_at: datetime,
    rules: PredictionRules,
) -> tuple[dict[str, object], ...]:
    """Issue rows under the v2 time contract.

    The evaluation window opens on the first complete UTC day after the
    prediction actually became available and closes after the declared horizon.
    The v1 producer and its bytes are untouched.
    """
    if isinstance(promoted_signals, (str, bytes)):
        raise ValueError("promoted signals must be iterable")
    try:
        promoted = tuple(promoted_signals)
    except TypeError as error:
        raise ValueError("promoted signals must be iterable") from error
    if any(not isinstance(item, PromotedSignal) for item in promoted):
        raise ValueError("promoted signals are invalid")
    if not isinstance(rules, PredictionRules):
        raise ValueError("prediction rules are invalid")
    predicted_at = _timestamp(predicted_at, "predicted at")
    rows = tuple(
        sorted(
            (_row_v2(item, predicted_at, available_at, rules) for item in promoted),
            key=lambda row: (row["market"], row["signal_date"], row["signal_id"]),
        )
    )
    prediction_ids = tuple(row["prediction_id"] for row in rows)
    if len(prediction_ids) != len(set(prediction_ids)):
        raise ValueError("duplicate immutable prediction id")
    if any(tuple(row) != PREDICTION_ROW_FIELDS_V2 for row in rows):
        raise AssertionError("prediction row field order drift")
    return rows
