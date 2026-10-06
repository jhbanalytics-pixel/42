"""Deterministic lineage edges for dated dynamic signal components."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from src.analysis.open_intelligence.graph import SignalComponent
from src.contracts.open_intelligence import ResolvedScope

LINEAGE_ROW_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
    "signal_date",
    "market",
    "from_signal_id",
    "to_signal_id",
    "relation",
    "overlap_score",
    "created_at",
)
_SIGNAL_ID = re.compile(r"sig_[0-9a-f]{64}\Z")


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone aware")
    return value.astimezone(UTC)


def _members(component: SignalComponent) -> frozenset[str]:
    members = component.member_identities
    if not members or len(members) != len(set(members)):
        raise ValueError("component members must be unique and nonempty")
    expected = f"{component.market}|"
    if any(not isinstance(member, str) or not member.startswith(expected) for member in members):
        raise ValueError("component member market is invalid")
    return frozenset(members)


@dataclass(frozen=True, slots=True)
class LineageRules:
    overlap_floor: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.overlap_floor, bool)
            or not isinstance(self.overlap_floor, (int, float))
            or not math.isfinite(float(self.overlap_floor))
            or not 0 <= float(self.overlap_floor) <= 1
        ):
            raise ValueError("overlap floor must be finite within zero and one")
        object.__setattr__(self, "overlap_floor", float(self.overlap_floor))


@dataclass(frozen=True, slots=True)
class SignalSnapshot:
    signal_id: str
    signal_date: date
    component: SignalComponent

    def __post_init__(self) -> None:
        if not isinstance(self.signal_id, str) or _SIGNAL_ID.fullmatch(self.signal_id) is None:
            raise ValueError("signal id must be a full signal identifier")
        if isinstance(self.signal_date, datetime) or not isinstance(self.signal_date, date):
            raise ValueError("signal date is invalid")
        if not isinstance(self.component, SignalComponent):
            raise ValueError("signal component is invalid")
        _members(self.component)


def _scope_values(scope: ResolvedScope) -> dict[str, object]:
    return {
        "client_scope_id": scope.client_scope_id,
        "market_scope": list(scope.market_scope),
        "brand_config_id": scope.brand_config_id,
        "audience_lens_ids": list(scope.audience_lens_ids),
        "theme_id": scope.theme_id,
        "run_id": scope.run_id,
        "contract_version": scope.contract_version,
    }


def _snapshots(
    values: object,
    *,
    scope: ResolvedScope,
    signal_date: date,
    current: bool,
) -> tuple[SignalSnapshot, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("signal snapshots must be iterable")
    try:
        items = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError("signal snapshots must be iterable") from error
    if any(not isinstance(item, SignalSnapshot) for item in items):
        raise ValueError("signal snapshots are invalid")
    keys = {(item.component.market, item.signal_id) for item in items}
    if len(keys) != len(items):
        raise ValueError("duplicate signal snapshot")
    for item in items:
        if item.component.market not in scope.market_scope:
            raise ValueError("component market is outside market scope")
        if current and item.signal_date != signal_date:
            raise ValueError("current signal date does not match lineage date")
        if not current and item.signal_date >= signal_date:
            raise ValueError("prior signal date must be earlier than lineage date")
    return tuple(sorted(items, key=lambda item: (item.component.market, item.signal_id)))


def _overlap(prior: SignalSnapshot, current: SignalSnapshot) -> float:
    prior_members = _members(prior.component)
    current_members = _members(current.component)
    return len(prior_members & current_members) / len(prior_members | current_members)


def build_signal_lineage_rows(
    *,
    prior_signals: Iterable[SignalSnapshot],
    current_signals: Iterable[SignalSnapshot],
    scope: ResolvedScope,
    signal_date: date,
    created_at: datetime,
    rules: LineageRules,
) -> tuple[dict[str, object], ...]:
    """Build exact lineage rows from immutable prior and current components."""
    if not isinstance(scope, ResolvedScope):
        raise ValueError("resolved scope is invalid")
    if isinstance(signal_date, datetime) or not isinstance(signal_date, date):
        raise ValueError("signal date is invalid")
    if not isinstance(rules, LineageRules):
        raise ValueError("lineage rules are invalid")
    created_at = _timestamp(created_at, "created at")
    prior = _snapshots(prior_signals, scope=scope, signal_date=signal_date, current=False)
    current = _snapshots(current_signals, scope=scope, signal_date=signal_date, current=True)
    edges = tuple(
        (previous, next_signal, _overlap(previous, next_signal))
        for previous in prior
        for next_signal in current
        if previous.component.market == next_signal.component.market
        and _overlap(previous, next_signal) >= rules.overlap_floor
    )
    prior_degree: dict[tuple[str, str], int] = {}
    current_degree: dict[tuple[str, str], int] = {}
    for previous, next_signal, _ in edges:
        prior_key = (previous.component.market, previous.signal_id)
        current_key = (next_signal.component.market, next_signal.signal_id)
        prior_degree[prior_key] = prior_degree.get(prior_key, 0) + 1
        current_degree[current_key] = current_degree.get(current_key, 0) + 1
    if any(
        prior_degree[(previous.component.market, previous.signal_id)] > 1
        and current_degree[(next_signal.component.market, next_signal.signal_id)] > 1
        for previous, next_signal, _ in edges
    ):
        raise ValueError("many-to-many lineage is ambiguous")
    scope_values = _scope_values(scope)
    rows = []
    for previous, next_signal, overlap_score in edges:
        prior_key = (previous.component.market, previous.signal_id)
        current_key = (next_signal.component.market, next_signal.signal_id)
        relation = (
            "merges_into"
            if current_degree[current_key] > 1
            else "splits_into"
            if prior_degree[prior_key] > 1
            else "continues"
        )
        rows.append(
            {
                **scope_values,
                "signal_date": signal_date,
                "market": previous.component.market,
                "from_signal_id": previous.signal_id,
                "to_signal_id": next_signal.signal_id,
                "relation": relation,
                "overlap_score": overlap_score,
                "created_at": created_at,
            }
        )
    ordered = tuple(
        sorted(
            rows,
            key=lambda row: (
                row["market"],
                row["from_signal_id"],
                row["to_signal_id"],
                row["relation"],
            ),
        )
    )
    if any(tuple(row) != LINEAGE_ROW_FIELDS for row in ordered):
        raise AssertionError("lineage row field order drift")
    return ordered
