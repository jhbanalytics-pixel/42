"""Deterministic Observer role for the dark Brain kernel."""

from __future__ import annotations

from dataclasses import dataclass

from src.analysis.open_intelligence.brain_authority import (
    UNAVAILABLE_BRAIN_VALUE,
    _BrainEvidenceSnapshot,
    _IntelligenceBrainRuntimeRequest,
    _is_live_runtime,
    _snapshot_measurements,
    _validate_brain_evidence_snapshot,
)
from src.analysis.open_intelligence.brain_contract import (
    AudienceAvailabilityRecord,
    canonical_digest,
)

OBSERVATION_TYPES = (
    "novelty",
    "velocity",
    "breadth",
    "source_independence",
    "persistence",
    "creator_spread",
    "engagement_quality",
    "search_movement",
    "geographic_movement",
    "historical_rarity",
    "entity_emergence",
    "language_emergence",
    "cross_platform_spread",
    "source_movement",
)
_ROLE_CAPABILITY = object()


class _RoleBase:
    __slots__ = ("_authority",)

    def __copy__(self):
        raise TypeError("role result cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("role result cannot be copied")

    def __reduce__(self):
        raise TypeError("role result cannot be serialized")


class _RoleAuthority:
    __slots__ = ("capability", "owner", "public", "role", "runtime", "snapshot", "upstream")

    def __init__(self, owner, role, runtime, snapshot, upstream, public):
        self.capability = _ROLE_CAPABILITY
        self.owner = owner
        self.role = role
        self.runtime = runtime
        self.snapshot = snapshot
        self.upstream = upstream
        self.public = public


def _bind_role(value, role, runtime, snapshot, upstream=()):
    public = {name: getattr(value, name) for name in value.__dataclass_fields__}
    object.__setattr__(
        value, "_authority", _RoleAuthority(value, role, runtime, snapshot, upstream, public)
    )
    return value


def _validate_role(value, role, runtime, snapshot, upstream=()):
    _validate_brain_evidence_snapshot(runtime, snapshot)
    authority = getattr(value, "_authority", None)
    if (
        not isinstance(authority, _RoleAuthority)
        or authority.capability is not _ROLE_CAPABILITY
        or authority.owner is not value
        or authority.role != role
        or authority.runtime is not runtime
        or authority.snapshot is not snapshot
        or authority.upstream != upstream
        or any(getattr(value, key) != item for key, item in authority.public.items())
    ):
        raise ValueError("role authority is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    observation_id: str
    observation_type: str
    statement: str
    measurement_id: str
    evidence_ids: tuple[str, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ObserverResult(_RoleBase):
    role_version: str
    signal_id: str
    snapshot_id: str
    what_changed_claim_id: str
    observations: tuple[ObservationRecord, ...]
    movement_profile: tuple[str, ...]
    source_independence: str
    geographic_profile: tuple[str, ...]
    audience_availability: AudienceAvailabilityRecord
    evidence_state: str
    limitations: tuple[str, ...]
    missing_work: tuple[str, ...]
    result_digest: str


def _run_observer(
    runtime: _IntelligenceBrainRuntimeRequest, snapshot: _BrainEvidenceSnapshot
) -> ObserverResult:
    _validate_brain_evidence_snapshot(runtime, snapshot)
    if _is_live_runtime(runtime):
        measured = tuple(
            item for item in _snapshot_measurements(snapshot) if item.availability == "measured"
        )
        observations = tuple(
            ObservationRecord(
                "obs_" + canonical_digest((snapshot.snapshot_id, item.metric_name)),
                item.metric_name,
                f"{item.metric_name}={item.value}",
                item.measurement_id,
                item.evidence_ids,
                (),
            )
            for item in measured
        )
        by_name = {item.metric_name: item.value for item in measured}
        values = {
            "role_version": "observer_live_v1",
            "signal_id": snapshot.signal_id,
            "snapshot_id": snapshot.snapshot_id,
            "what_changed_claim_id": UNAVAILABLE_BRAIN_VALUE,
            "observations": observations,
            "movement_profile": tuple(item.metric_name for item in measured),
            "source_independence": by_name.get("source_independence", UNAVAILABLE_BRAIN_VALUE),
            "geographic_profile": by_name.get("geographic_movement", UNAVAILABLE_BRAIN_VALUE),
            "audience_availability": UNAVAILABLE_BRAIN_VALUE,
            "evidence_state": snapshot.evidence_state,
            "limitations": ("semantic_method_not_authorized",),
            "missing_work": ("semantic_method_not_authorized",),
        }
        result = ObserverResult(**values, result_digest=canonical_digest(values))
        return _bind_role(result, "observer", runtime, snapshot)
    measurements = {item.metric_name: item for item in _snapshot_measurements(snapshot)}
    observations = []
    missing = []
    for kind in OBSERVATION_TYPES:
        item = measurements[kind]
        if item.availability == "unavailable":
            evidence_ids = item.evidence_ids
            missing.append(f"{kind}_unavailable")
        else:
            evidence_ids = item.evidence_ids
        observations.append(
            ObservationRecord(
                "obs_" + canonical_digest((snapshot.snapshot_id, kind)),
                kind,
                f"Measured {kind.replace('_', ' ')} for the frozen signal.",
                item.measurement_id,
                evidence_ids,
                () if item else ("measurement unavailable",),
            )
        )
    values = {
        "role_version": "observer_v1",
        "signal_id": snapshot.signal_id,
        "snapshot_id": snapshot.snapshot_id,
        "what_changed_claim_id": "clm_" + canonical_digest((snapshot.snapshot_id, "what_changed")),
        "observations": tuple(observations),
        "movement_profile": tuple(
            item.observation_type for item in observations if item.evidence_ids
        ),
        "source_independence": "measured",
        "geographic_profile": tuple(snapshot.market_ids),
        "audience_availability": AudienceAvailabilityRecord(
            "unavailable", None, None, None, None, None, None, None, (), ("no audience receipt",)
        ),
        "evidence_state": snapshot.evidence_state,
        "limitations": ("frozen evidence only",),
        "missing_work": tuple(sorted(missing)),
    }
    result = ObserverResult(**values, result_digest=canonical_digest(values))
    return _bind_role(result, "observer", runtime, snapshot)


def _validate_observer(runtime, snapshot, value):
    if _is_live_runtime(runtime) and (
        value.what_changed_claim_id != UNAVAILABLE_BRAIN_VALUE
        or value.audience_availability != UNAVAILABLE_BRAIN_VALUE
        or any(not item.evidence_ids for item in value.observations)
    ):
        raise ValueError("live observer authority is invalid")
    return _validate_role(value, "observer", runtime, snapshot)


__all__ = []
