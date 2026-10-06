"""Identity based observation flow telemetry.

``summarize_observation_flow`` reconciles unique native observation identities
across one whole stated window. Its inputs are sets of observation keys, never
row counts, so a post collected twice by two routes is one observation.
``disposition_record`` validates one sidecar disposition written at a real
pipeline boundary. Inferred identities are validated and counted apart from
exact native identities, and telemetry that could not be read is recorded as
an unknown outcome rather than as a zero.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path

IDENTITY_KINDS = frozenset({"native", "inferred"})
BOUNDARIES = (
    "producer",
    "dedup",
    "enrichment",
    "classification",
    "capture",
    "membership",
    "release",
)
OUTCOMES = frozenset({"admitted", "rejected", "unknown"})
TELEMETRY_UNAVAILABLE = "telemetry_unavailable"
DISPOSITION_MARKETS = frozenset({"za", "ng", "ke"})
UNCLASSIFIED_LABELS = frozenset({"", "other"})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]*\Z")


def summarize_observation_flow(emitted, persisted, qualified, classified):
    if not classified <= qualified <= persisted <= emitted:
        raise ValueError("observation_population_mismatch")
    return {
        "emitted_observations": len(emitted),
        "persisted_observations": len(persisted),
        "qualified_observations": len(qualified),
        "classified_observations": len(classified),
        "persisted_not_qualified": len(persisted - qualified),
    }


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"disposition_field_invalid:{field}")
    return value


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _text(value, field)


def disposition_record(
    *,
    operation_id: str,
    observation_key: str,
    identity_kind: str,
    native_namespace: str | None,
    native_id: str | None,
    collection_event_id: str,
    source_row_id: str | None,
    boundary: str,
    outcome: str,
    reason_code: str | None,
    market: str,
    route: str,
    observed_at: datetime,
    source_binding_digest: str,
) -> dict:
    """Validate one sidecar disposition and return it as a plain record."""
    if identity_kind not in IDENTITY_KINDS:
        raise ValueError("disposition_field_invalid:identity_kind")
    namespace = _optional_text(native_namespace, "native_namespace")
    identifier = _optional_text(native_id, "native_id")
    if identity_kind == "native" and (namespace is None or identifier is None):
        raise ValueError("native_identity_incomplete")
    if identity_kind == "inferred" and identifier is not None:
        raise ValueError("inferred_identity_claims_native_id")
    if boundary not in BOUNDARIES:
        raise ValueError("disposition_field_invalid:boundary")
    if outcome not in OUTCOMES:
        raise ValueError("disposition_field_invalid:outcome")
    reason = _optional_text(reason_code, "reason_code")
    if reason is not None and _CODE.fullmatch(reason) is None:
        raise ValueError("disposition_field_invalid:reason_code")
    if outcome == "rejected" and reason is None:
        raise ValueError("rejection_reason_missing")
    if outcome == "unknown" and reason != TELEMETRY_UNAVAILABLE:
        raise ValueError("unknown_outcome_reason_invalid")
    if market not in DISPOSITION_MARKETS:
        raise ValueError("disposition_field_invalid:market")
    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise ValueError("disposition_field_invalid:observed_at")
    if (
        not isinstance(source_binding_digest, str)
        or _DIGEST.fullmatch(source_binding_digest) is None
    ):
        raise ValueError("disposition_field_invalid:source_binding_digest")
    return {
        "operation_id": _text(operation_id, "operation_id"),
        "observation_key": _text(observation_key, "observation_key"),
        "identity_kind": identity_kind,
        "native_namespace": namespace,
        "native_id": identifier,
        "collection_event_id": _text(collection_event_id, "collection_event_id"),
        "source_row_id": _optional_text(source_row_id, "source_row_id"),
        "boundary": boundary,
        "outcome": outcome,
        "reason_code": reason,
        "market": market,
        "route": _text(route, "route"),
        "observed_at": observed_at,
        "source_binding_digest": source_binding_digest,
    }


def count_identities(records: Iterable[Mapping]) -> dict[str, int]:
    """Count exact native observations apart from inferred identity records."""
    native: set[tuple[str, str]] = set()
    inferred: set[str] = set()
    unknown = 0
    for record in records:
        if record["outcome"] == "unknown":
            unknown += 1
            continue
        if record["identity_kind"] == "native":
            native.add((record["native_namespace"], record["native_id"]))
        else:
            inferred.add(record["observation_key"])
    return {
        "native_observations": len(native),
        "inferred_identity_observations": len(inferred),
        "unknown_dispositions": unknown,
    }


def classified_observation_keys(
    rows: Iterable[Mapping],
    *,
    key_field: str = "observation_key",
    label_field: str = "topic_groups",
) -> set[str]:
    """Return observation keys with at least one real classification label."""
    keys: set[str] = set()
    for row in rows:
        labels = row.get(label_field)
        if labels is None:
            continue
        if isinstance(labels, str):
            labels = [labels]
        if any(
            isinstance(label, str) and label.strip().lower() not in UNCLASSIFIED_LABELS
            for label in labels
        ):
            keys.add(row[key_field])
    return keys


# Distinct named units over one window

COVERAGE_UNITS = (
    "emissions",
    "physical_persisted_rows",
    "unique_native_observations",
    "inferred_identity_observations",
    "qualified_observations",
    "classified_observations",
    "topic_day_scores",
    "membership_edges",
    "candidates",
    "released_signals",
    "retrieved_observations",
    "admitted_receipts",
    "cited_receipts",
)
# Units the dispositions themselves measure; every other unit is a count a
# boundary recorded in its own terms, and stays unknown until one does.
_DISPOSITION_UNITS = frozenset(
    {
        "emissions",
        "unique_native_observations",
        "inferred_identity_observations",
        "qualified_observations",
        "classified_observations",
    }
)
# Same identity transitions: the boundary and the boundary whose admitted keys
# it receives. Membership is a separate bridge and receives no producer keys.
_TRANSITIONS = (
    ("dedup", "producer"),
    ("enrichment", "dedup"),
    ("classification", "enrichment"),
    ("membership", None),
)
NATIVE_WINDOW_QUERY_PATH = str(
    Path(__file__).resolve().parents[3]
    / "infra"
    / "bigquery_queries"
    / "open_intelligence_coverage_window_totals_v1.sql"
)
NATIVE_WINDOW_PARAMETERS = (
    "window_start",
    "window_end",
    "market_strata",
    "query_limit",
)
NATIVE_WINDOW_ASSERTS = (
    "window_is_ordered",
    "market_strata_are_declared",
    "query_limit_is_positive",
    "every_market_is_a_declared_stratum",
    "rejections_carry_a_reason",
)
# The five units the dispositions themselves measure, by the boundary each is
# read from. A marker on that boundary inside the window makes the unit unknown.
_UNIT_BOUNDARIES = {
    "emissions": "producer",
    "unique_native_observations": "producer",
    "inferred_identity_observations": "producer",
    "qualified_observations": "enrichment",
    "classified_observations": "classification",
}


def _window(window: tuple[datetime, datetime]) -> tuple[datetime, datetime]:
    try:
        start, end = window
    except (TypeError, ValueError) as error:
        raise ValueError("window_invalid") from error
    for value in (start, end):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("window_invalid")
    if not start < end:
        raise ValueError("window_invalid")
    return start, end


def _origin(record: Mapping) -> str:
    if record["identity_kind"] == "native":
        return record["native_namespace"]
    key = record["observation_key"]
    if key.startswith("url:"):
        return key.split(":", 2)[1]
    return "unknown"


def _shares(values: Iterable[str | None]) -> dict[str, float]:
    counts: dict[str, int] = {}
    for value in values:
        label = value if isinstance(value, str) and value else "unknown"
        counts[label] = counts.get(label, 0) + 1
    total = sum(counts.values())
    return {label: round(count / total, 6) for label, count in sorted(counts.items())}


def _transition(records: list[Mapping], previous_admitted: set[str] | None) -> dict:
    admitted = {r["observation_key"] for r in records if r["outcome"] == "admitted"}
    unknown = {r["observation_key"] for r in records if r["outcome"] == "unknown"}
    rejected: dict[str, set[str]] = {}
    for record in records:
        if record["outcome"] == "rejected":
            rejected.setdefault(record["reason_code"], set()).add(record["observation_key"])
    seen = {r["observation_key"] for r in records}
    unrecorded = len(previous_admitted - seen) if previous_admitted is not None else 0
    return {
        "admitted": len(admitted),
        "rejected": {reason: len(keys) for reason, keys in sorted(rejected.items())},
        "unknown": len(unknown),
        "unrecorded": unrecorded,
    }


def _route_states(notes: list[Mapping], known_quiet: frozenset[tuple[str, str]]) -> dict:
    routes: dict[str, dict[str, dict]] = {}
    for note in notes:
        if note.get("kind") != "route":
            continue
        market, route = note["market"], note["route"]
        emissions = int(note["emissions"])
        reason = note.get("reason_code")
        if emissions > 0:
            state = "emitting"
        elif reason is not None:
            state = "failed"
        elif (market, route) in known_quiet:
            state, reason = "known_quiet", "declared_quiet"
        else:
            state, reason = "unexplained_zero", "zero_without_decision_record"
        routes.setdefault(market, {})[route] = {
            "state": state,
            "emissions": emissions,
            "reason_code": reason,
        }
    return routes


def coverage_units_report(
    window: tuple[datetime, datetime],
    telemetry: object,
    *,
    known_quiet: Iterable[tuple[str, str]] = (),
) -> dict:
    """Publish the distinct named units of one window from recorded telemetry.

    ``telemetry`` is anything exposing ``records`` (validated dispositions),
    ``notes`` (counts, route outcomes and unavailable markers) and ``enabled``.
    Emissions are collection events; unique observations are sets over the
    whole window, never per day distinct counts added up; every unit no
    boundary measured is ``None``. Loss arithmetic exists only on same identity
    transitions and is read from rejection records, never from subtraction: a
    key with no record at the next boundary is unrecorded, not lost. A boundary
    carrying an unavailable marker inside the window leaves the units read from
    it unknown, and identity counts are taken over producer records alone, so
    member identities from the bridge never join the observation counts.
    """
    start, end = _window(window)
    quiet = frozenset(tuple(item) for item in known_quiet)
    published = {"start": start.isoformat(), "end": end.isoformat()}
    if not getattr(telemetry, "enabled", True):
        return {
            "window": published,
            "telemetry": "disabled",
            "units": dict.fromkeys(COVERAGE_UNITS),
            "flow": None,
            "transitions": {},
            "concentration": {},
            "routes": {},
            "controls": None,
            "unavailable": [],
            "identity_counts": None,
            "independent_support": {
                "basis": "membership_edges_qualifying",
                "value": None,
            },
        }
    records = [r for r in telemetry.records if start <= r["observed_at"] < end]
    notes = list(telemetry.notes)
    markers = [
        note
        for note in notes
        if note.get("kind") == "unavailable"
        and (note.get("observed_at") is None or start <= note["observed_at"] < end)
    ]
    unavailable_boundaries = {note["boundary"] for note in markers}
    by_boundary: dict[str, list[Mapping]] = {boundary: [] for boundary in BOUNDARIES}
    for record in records:
        by_boundary[record["boundary"]].append(record)
    admitted_keys = {
        boundary: {r["observation_key"] for r in items if r["outcome"] == "admitted"}
        for boundary, items in by_boundary.items()
    }
    producer = by_boundary["producer"]
    counted = [r for r in producer if r["outcome"] != "unknown"]
    units: dict[str, int | None] = {
        "emissions": len(producer),
        "unique_native_observations": len(
            {
                (r["native_namespace"], r["native_id"])
                for r in counted
                if r["identity_kind"] == "native"
            }
        ),
        "inferred_identity_observations": len(
            {r["observation_key"] for r in counted if r["identity_kind"] == "inferred"}
        ),
        "qualified_observations": len(admitted_keys["enrichment"]),
        "classified_observations": len(admitted_keys["classification"]),
    }
    for unit, boundary in _UNIT_BOUNDARIES.items():
        if boundary in unavailable_boundaries:
            units[unit] = None
    for unit in COVERAGE_UNITS:
        if unit in _DISPOSITION_UNITS:
            continue
        values = [
            int(note["value"])
            for note in notes
            if note.get("kind") == "count"
            and note.get("unit") == unit
            and (note.get("observed_at") is None or start <= note["observed_at"] < end)
        ]
        units[unit] = sum(values) if values else None
    try:
        flow = summarize_observation_flow(
            admitted_keys["producer"],
            admitted_keys["dedup"],
            admitted_keys["enrichment"],
            admitted_keys["classification"],
        )
    except ValueError as error:
        flow = {"error": str(error)}
    transitions = {
        boundary: _transition(by_boundary[boundary], admitted_keys[previous] if previous else None)
        for boundary, previous in _TRANSITIONS
    }
    routes = _route_states(notes, quiet)
    states = [entry["state"] for market in routes.values() for entry in market.values()]
    membership = by_boundary["membership"]
    return {
        "window": published,
        "telemetry": "enabled",
        "units": {unit: units[unit] for unit in COVERAGE_UNITS},
        "flow": flow,
        "transitions": transitions,
        "concentration": {
            "platform": _shares(r["native_namespace"] for r in counted),
            "vendor": _shares(r["route"] for r in counted),
            "common_origin": _shares(_origin(r) for r in counted),
        },
        "routes": routes,
        "controls": {
            "failed_routes": states.count("failed"),
            "known_quiet_routes": states.count("known_quiet"),
            "unexplained_zero_routes": states.count("unexplained_zero"),
        },
        "unavailable": [
            {
                "boundary": note["boundary"],
                "operation_id": note["operation_id"],
                "reason_code": note["reason_code"],
                "market": note.get("market"),
            }
            for note in markers
        ],
        "identity_counts": count_identities(producer),
        "independent_support": {
            "basis": "membership_edges_qualifying",
            "value": len(admitted_keys["membership"]) if membership else None,
        },
    }
