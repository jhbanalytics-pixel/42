"""Private authority for the dark Intelligence Brain fixture lane."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

from src.analysis.open_intelligence.brain_contract import (
    BRAIN_CONTRACT_VERSION,
    DARK_DEPTH_POLICY,
    DARK_DEPTH_POLICY_DIGEST,
    BrainAdmission,
    BrainMeasurement,
    BrainOutcomeRecord,
    IntelligenceBrainIntent,
    canonical_bytes,
    canonical_digest,
)

_RUNTIME_CAPABILITY = object()
_SNAPSHOT_CAPABILITY = object()
_LIVE_RUNTIME_CAPABILITY = object()
_LIVE_SNAPSHOT_CAPABILITY = object()
_STORED_INVESTIGATIONS = MappingProxyType(
    {
        "inv_fixture_01": MappingProxyType(
            {
                "fixture_id": "brain_evidence_snapshot_v1",
                "signal_id": "sig_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "client_scope_id": "fixture_scope",
                "market_scope": ("za", "ng"),
                "brand_config_id": "fixture_brand",
                "audience_lens_ids": (),
                "theme_id": "fixture_theme",
                "brand_context_id": None,
            }
        )
    }
)
_MEASUREMENT_TYPES = (
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
_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "tests/fixtures/open_intelligence/brain_v1/evidence_snapshot_v1.json"
)


@dataclass(frozen=True, slots=True)
class _StoredInvestigationFrame:
    fixture_id: str
    signal_id: str
    client_scope_id: str
    market_scope: tuple[str, ...]
    brand_config_id: str | None
    audience_lens_ids: tuple[str, ...]
    theme_id: str | None
    brand_context_id: str | None


@dataclass(frozen=True, slots=True)
class _IssuedSnapshotRoot:
    owner: object
    canonical_payload: bytes
    projection_digest: str
    public_field_digest: str
    snapshot_id: str
    snapshot_digest: str


@dataclass(frozen=True, slots=True)
class _UnavailableBrainValue:
    state: str
    reason_code: str
    admission: BrainAdmission


UNAVAILABLE_BRAIN_VALUE = _UnavailableBrainValue(
    "unavailable",
    "semantic_method_not_authorized",
    BrainAdmission(
        "unavailable",
        "rejected",
        "not_required",
        False,
        ("semantic_method_not_authorized",),
        None,
    ),
)


def _issued_snapshot_root_store():
    roots: dict[int, _IssuedSnapshotRoot] = {}

    def issue(owner: object, root: _IssuedSnapshotRoot) -> None:
        if type(root) is not _IssuedSnapshotRoot or root.owner is not owner:
            raise ValueError("snapshot root has foreign owner")
        key = id(owner)
        if key in roots:
            raise ValueError("snapshot root already issued")
        roots[key] = root

    def lookup(owner: object) -> _IssuedSnapshotRoot:
        root = roots.get(id(owner))
        if type(root) is not _IssuedSnapshotRoot or root.owner is not owner:
            raise ValueError("snapshot authority is invalid")
        return root

    return issue, lookup


_issue_snapshot_root, _lookup_snapshot_root = _issued_snapshot_root_store()


def _registry_payload(registry: object) -> dict[str, dict[str, object]]:
    try:
        return {
            key: {
                "fixture_id": value["fixture_id"],
                "signal_id": value["signal_id"],
                "client_scope_id": value["client_scope_id"],
                "market_scope": tuple(value["market_scope"]),
                "brand_config_id": value["brand_config_id"],
                "audience_lens_ids": tuple(value["audience_lens_ids"]),
                "theme_id": value["theme_id"],
                "brand_context_id": value["brand_context_id"],
            }
            for key, value in registry.items()
        }
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("investigation registry is invalid") from exc


_STORED_INVESTIGATIONS_DIGEST = canonical_digest(_registry_payload(_STORED_INVESTIGATIONS))


def _resolve_stored_investigation(
    investigation_id: str | None, error: str
) -> _StoredInvestigationFrame | None:
    try:
        payload = _registry_payload(_STORED_INVESTIGATIONS)
    except ValueError as exc:
        raise ValueError(error) from exc
    if canonical_digest(payload) != _STORED_INVESTIGATIONS_DIGEST:
        raise ValueError(error)
    stored = payload.get(investigation_id) if investigation_id is not None else None
    if stored is None:
        return None
    return _StoredInvestigationFrame(**stored)


class _Authority:
    __slots__ = ("capability", "owner", "private")

    def __init__(self, capability: object, owner: object, private: object):
        self.capability = capability
        self.owner = owner
        self.private = private


class _NoCopy:
    def __copy__(self):
        raise TypeError("authority cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("authority cannot be copied")

    def __reduce__(self):
        raise TypeError("authority cannot be serialized")

    def __reduce_ex__(self, protocol):
        raise TypeError("authority cannot be serialized")


class _IntelligenceBrainRuntimeRequest(_NoCopy):
    __slots__ = (
        "_authority",
        "as_of",
        "audience_lens_ids",
        "brand_config_id",
        "brand_context_id",
        "client_scope_id",
        "contract_version",
        "decision_question",
        "depth_policy_digest",
        "depth_policy_id",
        "investigation_id",
        "market_scope",
        "research_depth",
        "run_id",
        "signal_date",
        "signal_id",
        "theme_id",
    )

    def __new__(cls):
        raise ValueError("runtime authority is invalid")


class _BrainEvidenceSnapshot(_NoCopy):
    __slots__ = (
        "_authority",
        "as_of",
        "contract_version",
        "discovery_mode",
        "evidence_ids",
        "evidence_state",
        "historical_object_ids",
        "investigation_id",
        "market_ids",
        "observation_ids",
        "outcome_ids",
        "prediction_ids",
        "run_id",
        "scope_digest",
        "signal_date",
        "signal_id",
        "signal_version",
        "snapshot_digest",
        "snapshot_id",
        "source_family_ids",
        "source_projection_digest",
    )

    def __new__(cls):
        raise ValueError("snapshot authority is invalid")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    return parsed.astimezone(UTC)


def _outcome_records(payload: dict[str, object], as_of: datetime) -> tuple[BrainOutcomeRecord, ...]:
    try:
        raw_records = payload["outcomes"]
        if not isinstance(raw_records, list):
            raise TypeError
        records = tuple(
            BrainOutcomeRecord(
                item["outcome_id"],
                item["prediction_id"],
                _parse_timestamp(item["observed_at"]),
            )
            for item in raw_records
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("outcome observation time is invalid") from None
    if any(record.observed_at > as_of for record in records):
        raise ValueError("outcome observation time exceeds snapshot as_of")
    if len({record.outcome_id for record in records}) != len(records):
        raise ValueError("outcome observation identity is invalid")
    return records


def _prediction_window(payload: dict[str, object]) -> tuple[str, str, date, date]:
    try:
        item = payload["prediction"]
        prediction_id = item["prediction_id"]
        expected_direction = item["expected_direction"]
        start = date.fromisoformat(item["expected_window_start"])
        end = date.fromisoformat(item["expected_window_end"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("prediction window is invalid") from None
    if not prediction_id or not expected_direction or end < start:
        raise ValueError("prediction window is invalid")
    return prediction_id, expected_direction, start, end


def _issue_brain_runtime_request(
    intent: IntelligenceBrainIntent,
) -> _IntelligenceBrainRuntimeRequest:
    if not isinstance(intent, IntelligenceBrainIntent):
        raise ValueError("brain request is invalid")
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    stored_frame = _resolve_stored_investigation(
        intent.investigation_id, "investigation unavailable"
    )
    if intent.research_depth != "briefing" and stored_frame is None:
        raise ValueError("investigation unavailable")
    if stored_frame is None:
        stored_frame = _StoredInvestigationFrame(
            payload["fixture_id"],
            payload["signal_id"],
            payload["client_scope_id"],
            tuple(payload["market_scope"]),
            payload["brand_config_id"],
            tuple(payload["audience_lens_ids"]),
            payload["theme_id"],
            intent.brand_context_id,
        )
    if payload["fixture_id"] != stored_frame.fixture_id:
        raise ValueError("investigation unavailable")
    if intent.signal_id != stored_frame.signal_id:
        raise ValueError("brain signal unavailable")
    if intent.brand_context_id != stored_frame.brand_context_id:
        raise ValueError("investigation unavailable")
    value = object.__new__(_IntelligenceBrainRuntimeRequest)
    fields = {
        "contract_version": BRAIN_CONTRACT_VERSION,
        "run_id": "brain_" + canonical_digest(intent)[:32],
        "investigation_id": intent.investigation_id,
        "signal_id": intent.signal_id,
        "signal_date": date.fromisoformat(payload["signal_date"]),
        "client_scope_id": stored_frame.client_scope_id,
        "market_scope": stored_frame.market_scope,
        "brand_config_id": stored_frame.brand_config_id,
        "audience_lens_ids": stored_frame.audience_lens_ids,
        "theme_id": stored_frame.theme_id,
        "research_depth": intent.research_depth,
        "decision_question": intent.decision_question,
        "brand_context_id": stored_frame.brand_context_id,
        "as_of": _parse_timestamp(payload["as_of"]),
        "depth_policy_id": DARK_DEPTH_POLICY["policy_id"],
        "depth_policy_digest": DARK_DEPTH_POLICY_DIGEST,
    }
    for key, item in fields.items():
        object.__setattr__(value, key, item)
    object.__setattr__(
        value,
        "_authority",
        _Authority(
            _RUNTIME_CAPABILITY,
            value,
            (intent, fields, stored_frame),
        ),
    )
    return value


def _validate_brain_runtime_request(value: object) -> _IntelligenceBrainRuntimeRequest:
    authority = getattr(value, "_authority", None)
    if (
        type(value) is not _IntelligenceBrainRuntimeRequest
        or not isinstance(authority, _Authority)
        or authority.capability not in {_RUNTIME_CAPABILITY, _LIVE_RUNTIME_CAPABILITY}
        or authority.owner is not value
    ):
        raise ValueError("runtime authority is invalid")
    if authority.capability is _LIVE_RUNTIME_CAPABILITY:
        source_read, fields = authority.private
        from src.analysis.open_intelligence.brain_live_reader import validate_live_brain_read

        validate_live_brain_read(source_read)
        if any(getattr(value, key) != item for key, item in fields.items()):
            raise ValueError("runtime authority is invalid")
        return value
    _, fields, stored_frame = authority.private
    if any(getattr(value, key) != item for key, item in fields.items()):
        raise ValueError("runtime authority is invalid")
    if value.investigation_id is not None:
        fresh_frame = _resolve_stored_investigation(
            value.investigation_id, "runtime authority is invalid"
        )
        if fresh_frame != stored_frame:
            raise ValueError("runtime authority is invalid")
    return value


def _is_live_runtime(value: object) -> bool:
    authority = getattr(value, "_authority", None)
    return isinstance(authority, _Authority) and authority.capability is _LIVE_RUNTIME_CAPABILITY


def _is_live_snapshot(value: object) -> bool:
    authority = getattr(value, "_authority", None)
    return isinstance(authority, _Authority) and authority.capability is _LIVE_SNAPSHOT_CAPABILITY


def _issue_live_brain_runtime_request(
    source_read,
    *,
    research_depth: str,
    decision_question: str | None = None,
) -> _IntelligenceBrainRuntimeRequest:
    from src.analysis.open_intelligence.brain_live_reader import validate_live_brain_read

    read = validate_live_brain_read(source_read)
    if research_depth not in DARK_DEPTH_POLICY["depths"]:
        raise ValueError("research depth is invalid")
    if research_depth != "briefing" and not decision_question:
        raise ValueError("decision question is required")
    candidate = read.candidate
    value = object.__new__(_IntelligenceBrainRuntimeRequest)
    fields = {
        "contract_version": BRAIN_CONTRACT_VERSION,
        "run_id": read.receipt.run_id,
        "investigation_id": None,
        "signal_id": candidate["signal_id"],
        "signal_date": read.receipt.signal_date,
        "client_scope_id": read.receipt.client_scope_id,
        "market_scope": read.receipt.market_scope,
        "brand_config_id": candidate["brand_config_id"],
        "audience_lens_ids": tuple(candidate["audience_lens_ids"]),
        "theme_id": candidate["theme_id"],
        "research_depth": research_depth,
        "decision_question": decision_question,
        "brand_context_id": None,
        "as_of": read.receipt.completed_at,
        "depth_policy_id": DARK_DEPTH_POLICY["policy_id"],
        "depth_policy_digest": DARK_DEPTH_POLICY_DIGEST,
    }
    for key, item in fields.items():
        object.__setattr__(value, key, item)
    object.__setattr__(
        value,
        "_authority",
        _Authority(_LIVE_RUNTIME_CAPABILITY, value, (read, fields)),
    )
    return _validate_brain_runtime_request(value)


def _issue_live_brain_evidence_snapshot(
    runtime: _IntelligenceBrainRuntimeRequest,
    source_read,
) -> _BrainEvidenceSnapshot:
    from src.analysis.open_intelligence.brain_live_reader import validate_live_brain_read

    _validate_brain_runtime_request(runtime)
    read = validate_live_brain_read(source_read)
    if not _is_live_runtime(runtime) or runtime.run_id != read.receipt.run_id:
        raise ValueError("live snapshot authority is invalid")
    candidate = read.candidate
    metrics = {
        "novelty": candidate["novelty_score"],
        "velocity": candidate["velocity_score"],
        "breadth": candidate["breadth_score"],
        "source_independence": candidate["independence_score"],
        "historical_rarity": candidate["historical_similarity"],
        "geographic_movement": candidate["geo_confidence"],
    }
    measurements = [
        {
            "metric_name": metric,
            "measurement_id": "bm_" + canonical_digest((read.snapshot_digest, metric)),
            "value": value,
            "unit": "score",
            "method_id": "candidate_metric_copy_v1",
            "market": candidate["market"],
        }
        for metric, value in metrics.items()
        if value is not None
    ]
    payload = {
        "measurements": measurements,
        "historical": {},
        "prediction": {},
        "outcomes": [
            {
                "outcome_id": row["outcome_id"],
                "prediction_id": row["prediction_id"],
                "observed_at": row["evaluated_at"]
                .astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            }
            for row in read.outcomes
        ],
        "evidence_ids": [row["evidence_id"] for row in read.evidence],
        "candidate": dict(candidate),
    }
    canonical_payload = canonical_bytes(payload)
    projection_digest = canonical_digest(payload)
    observation_ids = tuple(
        next(
            (item["measurement_id"] for item in measurements if item["metric_name"] == metric),
            f"bm_unavailable_{metric}",
        )
        for metric in _MEASUREMENT_TYPES
    )
    public = {
        "contract_version": BRAIN_CONTRACT_VERSION,
        "run_id": runtime.run_id,
        "investigation_id": None,
        "signal_id": runtime.signal_id,
        "signal_date": runtime.signal_date,
        "scope_digest": canonical_digest(read.snapshot_root["resolved_scope"]),
        "as_of": runtime.as_of,
        "signal_version": candidate["contract_version"],
        "discovery_mode": candidate["discovery_mode"],
        "evidence_state": candidate["evidence_state"],
        "observation_ids": observation_ids,
        "evidence_ids": tuple(row["evidence_id"] for row in read.evidence),
        "source_family_ids": tuple(sorted({row["source_family"] for row in read.evidence})),
        "market_ids": (candidate["market"],),
        "historical_object_ids": (),
        "prediction_ids": tuple(row["prediction_id"] for row in read.predictions),
        "outcome_ids": tuple(row["outcome_id"] for row in read.outcomes),
        "source_projection_digest": projection_digest,
        "snapshot_id": "bsp_" + read.snapshot_digest,
        "snapshot_digest": read.snapshot_digest,
    }
    value = object.__new__(_BrainEvidenceSnapshot)
    for key, item in public.items():
        object.__setattr__(value, key, item)
    object.__setattr__(
        value,
        "_authority",
        _Authority(
            _LIVE_SNAPSHOT_CAPABILITY,
            value,
            (runtime, read, public, canonical_payload, projection_digest),
        ),
    )
    _issue_snapshot_root(
        value,
        _IssuedSnapshotRoot(
            value,
            canonical_payload,
            projection_digest,
            canonical_digest(public),
            public["snapshot_id"],
            public["snapshot_digest"],
        ),
    )
    return _validate_brain_evidence_snapshot(runtime, value)


def _issue_brain_evidence_snapshot(
    runtime: _IntelligenceBrainRuntimeRequest,
) -> _BrainEvidenceSnapshot:
    _validate_brain_runtime_request(runtime)
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    outcomes = _outcome_records(payload, runtime.as_of)
    prediction_id, _, _, _ = _prediction_window(payload)
    if tuple(payload["outcome_ids"]) != tuple(record.outcome_id for record in outcomes):
        raise ValueError("outcome observation identity is invalid")
    if tuple(payload["prediction_ids"]) != (prediction_id,):
        raise ValueError("prediction window identity is invalid")
    frozen_payload = canonical_bytes(payload)
    frozen_payload_digest = canonical_digest(payload)
    public = {
        "contract_version": BRAIN_CONTRACT_VERSION,
        "run_id": runtime.run_id,
        "investigation_id": runtime.investigation_id,
        "signal_id": runtime.signal_id,
        "signal_date": runtime.signal_date,
        "scope_digest": canonical_digest(
            {
                "client_scope_id": runtime.client_scope_id,
                "market_scope": runtime.market_scope,
                "brand_config_id": runtime.brand_config_id,
                "audience_lens_ids": runtime.audience_lens_ids,
                "theme_id": runtime.theme_id,
                "run_id": runtime.run_id,
                "contract_version": BRAIN_CONTRACT_VERSION,
            }
        ),
        "as_of": runtime.as_of,
        "signal_version": "dynamic_signal_v1",
        "discovery_mode": "canary",
        "evidence_state": payload["evidence_state"],
        "observation_ids": tuple(
            next(
                (
                    item["measurement_id"]
                    for item in payload["measurements"]
                    if item["metric_name"] == metric
                ),
                f"bm_unavailable_{metric}",
            )
            for metric in _MEASUREMENT_TYPES
        ),
        "evidence_ids": tuple(payload["evidence_ids"]),
        "source_family_ids": tuple(payload["source_family_ids"]),
        "market_ids": tuple(payload["market_ids"]),
        "historical_object_ids": tuple(payload["historical_object_ids"]),
        "prediction_ids": tuple(payload["prediction_ids"]),
        "outcome_ids": tuple(payload["outcome_ids"]),
        "source_projection_digest": frozen_payload_digest,
    }
    public["snapshot_id"] = "bsp_" + canonical_digest(public)
    public["snapshot_digest"] = canonical_digest(public)
    value = object.__new__(_BrainEvidenceSnapshot)
    for key, item in public.items():
        object.__setattr__(value, key, item)
    object.__setattr__(
        value,
        "_authority",
        _Authority(
            _SNAPSHOT_CAPABILITY,
            value,
            (runtime, public, frozen_payload, frozen_payload_digest),
        ),
    )
    _issue_snapshot_root(
        value,
        _IssuedSnapshotRoot(
            value,
            frozen_payload,
            frozen_payload_digest,
            canonical_digest(public),
            public["snapshot_id"],
            public["snapshot_digest"],
        ),
    )
    return value


def _snapshot_public_fields(value: _BrainEvidenceSnapshot) -> dict[str, object]:
    return {
        field: getattr(value, field)
        for field in _BrainEvidenceSnapshot.__slots__
        if field != "_authority"
    }


def _recomputed_snapshot_ids(public: dict[str, object]) -> tuple[str, str]:
    public_without_ids = {
        key: item for key, item in public.items() if key not in {"snapshot_id", "snapshot_digest"}
    }
    snapshot_id = "bsp_" + canonical_digest(public_without_ids)
    snapshot_digest = canonical_digest({**public_without_ids, "snapshot_id": snapshot_id})
    return snapshot_id, snapshot_digest


def _validate_brain_evidence_snapshot_with_root(
    runtime: _IntelligenceBrainRuntimeRequest,
    value: object,
    root_lookup,
) -> _BrainEvidenceSnapshot:
    _validate_brain_runtime_request(runtime)
    authority = getattr(value, "_authority", None)
    if (
        type(value) is not _BrainEvidenceSnapshot
        or not isinstance(authority, _Authority)
        or authority.capability not in {_SNAPSHOT_CAPABILITY, _LIVE_SNAPSHOT_CAPABILITY}
        or authority.owner is not value
    ):
        raise ValueError("snapshot authority is invalid")
    if authority.capability is _LIVE_SNAPSHOT_CAPABILITY:
        try:
            source_runtime, source_read, carried_public, frozen_payload, projection_digest = (
                authority.private
            )
            from src.analysis.open_intelligence.brain_live_reader import validate_live_brain_read

            validate_live_brain_read(source_read)
            root = root_lookup(value)
            current_public = _snapshot_public_fields(value)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("snapshot authority is invalid") from exc
        if (
            source_runtime is not runtime
            or canonical_bytes(json.loads(root.canonical_payload.decode("utf-8")))
            != root.canonical_payload
            or frozen_payload != root.canonical_payload
            or projection_digest != root.projection_digest
            or canonical_digest(carried_public) != root.public_field_digest
            or canonical_digest(current_public) != root.public_field_digest
            or value.source_projection_digest != root.projection_digest
            or value.snapshot_id != root.snapshot_id
            or value.snapshot_digest != root.snapshot_digest
        ):
            raise ValueError("snapshot authority is invalid")
        return value
    try:
        root = root_lookup(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("snapshot authority is invalid") from exc
    if type(root) is not _IssuedSnapshotRoot or root.owner is not value:
        raise ValueError("snapshot authority is invalid")
    try:
        source_runtime, carried_public, frozen_payload, frozen_payload_digest = authority.private
        decoded_payload = json.loads(root.canonical_payload.decode("utf-8"))
        current_public = _snapshot_public_fields(value)
        snapshot_id, snapshot_digest = _recomputed_snapshot_ids(current_public)
    except (AttributeError, KeyError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("snapshot authority is invalid") from exc
    if (
        source_runtime is not runtime
        or not isinstance(carried_public, dict)
        or canonical_bytes(decoded_payload) != root.canonical_payload
        or canonical_digest(decoded_payload) != root.projection_digest
        or frozen_payload != root.canonical_payload
        or frozen_payload_digest != root.projection_digest
        or canonical_digest(carried_public) != root.public_field_digest
        or canonical_digest(current_public) != root.public_field_digest
        or value.source_projection_digest != root.projection_digest
        or value.snapshot_id != root.snapshot_id
        or value.snapshot_digest != root.snapshot_digest
        or snapshot_id != root.snapshot_id
        or snapshot_digest != root.snapshot_digest
    ):
        raise ValueError("snapshot authority is invalid")
    return value


def _bind_snapshot_validator(root_lookup):
    def validate(
        runtime: _IntelligenceBrainRuntimeRequest, value: object
    ) -> _BrainEvidenceSnapshot:
        return _validate_brain_evidence_snapshot_with_root(runtime, value, root_lookup)

    return validate


_validate_brain_evidence_snapshot = _bind_snapshot_validator(_lookup_snapshot_root)


def _snapshot_payload_with_root(value: _BrainEvidenceSnapshot, root_lookup) -> dict[str, object]:
    authority = getattr(value, "_authority", None)
    if (
        not isinstance(authority, _Authority)
        or authority.capability not in {_SNAPSHOT_CAPABILITY, _LIVE_SNAPSHOT_CAPABILITY}
        or authority.owner is not value
    ):
        raise ValueError("snapshot authority is invalid")
    _validate_brain_evidence_snapshot(authority.private[0], value)
    root = root_lookup(value)
    return json.loads(root.canonical_payload.decode("utf-8"))


def _snapshot_outcomes(value: _BrainEvidenceSnapshot) -> tuple[BrainOutcomeRecord, ...]:
    records = _outcome_records(_snapshot_payload(value), value.as_of)
    if tuple(record.outcome_id for record in records) != value.outcome_ids:
        raise ValueError("outcome observation identity is invalid")
    return records


def _snapshot_prediction_window(value: _BrainEvidenceSnapshot) -> tuple[str, str, date, date]:
    prediction = _prediction_window(_snapshot_payload(value))
    if (prediction[0],) != value.prediction_ids:
        raise ValueError("prediction window identity is invalid")
    return prediction


def _bind_snapshot_payload(root_lookup):
    def payload(value: _BrainEvidenceSnapshot) -> dict[str, object]:
        return _snapshot_payload_with_root(value, root_lookup)

    return payload


_snapshot_payload = _bind_snapshot_payload(_lookup_snapshot_root)


def _snapshot_measurements(value: _BrainEvidenceSnapshot) -> tuple[BrainMeasurement, ...]:
    authority = getattr(value, "_authority", None)
    if (
        not isinstance(authority, _Authority)
        or authority.capability not in {_SNAPSHOT_CAPABILITY, _LIVE_SNAPSHOT_CAPABILITY}
        or authority.owner is not value
    ):
        raise ValueError("snapshot authority is invalid")
    payload = _snapshot_payload(value)
    by_type = {item["metric_name"]: item for item in payload["measurements"]}
    output = []
    for metric, measurement_id in zip(_MEASUREMENT_TYPES, value.observation_ids, strict=True):
        item = by_type.get(metric)
        if item is None:
            output.append(
                BrainMeasurement(
                    measurement_id,
                    metric,
                    "unavailable",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    (),
                    ("measurement unavailable",),
                )
            )
        else:
            output.append(
                BrainMeasurement(
                    measurement_id,
                    metric,
                    "measured",
                    item["value"],
                    item["unit"],
                    item["method_id"],
                    value.as_of - timedelta(days=7),
                    value.as_of,
                    item["market"],
                    tuple(value.evidence_ids),
                    (),
                )
            )
    return tuple(output)


__all__ = []
