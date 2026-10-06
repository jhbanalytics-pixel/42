"""Manifest bound replay beside the certified fixed cutoff evaluator.

The certified evaluator in replay_open_intelligence pins one cutoff and one
event ledger count. This adapter evaluates any independently authorized
retained input under its own byte digest. The digest proves the bytes, the
versioned replay input validator proves the authority, and every cutoff and
denominator comes from the admitted immutable input, never from a caller
argument. Vendor regime, collection mode, seeded or discovered status,
geography and evidence quality are normalized through history_normalizer
before any comparison, and the admitted cutoff is the as_of that excludes
evidence unavailable at that time.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from types import MappingProxyType

from scripts.staging import replay_open_intelligence as replay
from scripts.staging.replay_open_intelligence import (
    FutureLeakageDetected,
    IncompleteReplayInput,
    KnownEvent,
    ReplayInputBundle,
    ReplayMetrics,
    ReplaySignal,
)
from src.analysis.open_intelligence.history_normalizer import (
    HistoricalObservation,
    HistoryNormalizationRules,
    NormalizedHistory,
    normalize_vendor_regimes,
)

MANIFEST_HISTORY_RULES = HistoryNormalizationRules(
    minimum_overlap_days=3,
    minimum_geo_confidence=0.7,
    minimum_evidence_quality=0.7,
    baseline_window_days=2,
    maximum_overlap_ratio_deviation=0.1,
)
_EVIDENCE_TABLES = ("enriched_content", "raw_content")


class ReplayInputDigestMismatch(IncompleteReplayInput):
    pass


@dataclass(frozen=True, slots=True)
class ManifestReplayMetrics(ReplayMetrics):
    """The certified twelve fields plus the normalization state, per market.

    The normalization travels beside the metrics rather than inside them, so
    ReplayMetrics keeps its certified fields and the approved output digest.
    A retained input whose evidence cannot bridge a regime is still evaluated;
    its state and gaps are carried here instead of being dropped.
    """

    history_normalization: Mapping[str, NormalizedHistory] = field(
        default_factory=lambda: MappingProxyType({})
    )


class KnownEventMembershipChanged(IncompleteReplayInput):
    pass


def require_input_digest(payload: dict, expected: str) -> None:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ReplayInputDigestMismatch("replay_input_digest_mismatch")


def _ordered_events(events: tuple[KnownEvent, ...]) -> tuple[KnownEvent, ...]:
    return tuple(sorted(events, key=lambda event: (event.market, event.event_id)))


def admitted_known_events(bundle: ReplayInputBundle) -> tuple[KnownEvent, ...]:
    return _ordered_events(
        tuple(
            KnownEvent(
                event["event_id"],
                event["market"],
                tuple(event["member_identities"]),
            )
            for event in bundle.known_event_set["events"]
        )
    )


def _require_known_event_membership(
    admitted: tuple[KnownEvent, ...], offered: tuple[KnownEvent, ...]
) -> None:
    if any(not isinstance(event, KnownEvent) for event in offered):
        raise KnownEventMembershipChanged("known event membership is not a known event tuple")
    if _ordered_events(tuple(offered)) != admitted:
        raise KnownEventMembershipChanged(
            "known event membership does not match the admitted input"
        )


def _cutoff_end(cutoff: date) -> datetime:
    return datetime.combine(cutoff, time.max, tzinfo=UTC)


def _timestamp(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def admitted_history(bundle: ReplayInputBundle) -> tuple[HistoricalObservation, ...]:
    """Project the admitted evidence rows onto the history normalizer's row type.

    One observation per market, vendor regime, platform, collection mode,
    seeded or discovered status and day, valued as the admitted row count.
    Geography is the row's market against the admitted scope, evidence
    quality is whether the row carries a receipt, and availability is the
    latest collection time in the group.
    """
    rows_by_table = bundle.source_snapshot["rows_by_table"]
    markets = set(bundle.scope["market_scope"])
    seeded_row_ids = {
        row_id for row in rows_by_table["seed_graph"] for row_id in row["sample_row_ids"]
    }
    receipted_row_ids = {
        receipt["row_id"] for receipts in bundle.receipts_by_member.values() for receipt in receipts
    }
    counts: dict[tuple[object, ...], int] = {}
    available: dict[tuple[object, ...], datetime] = {}
    for table in _EVIDENCE_TABLES:
        for row in rows_by_table[table]:
            collected_at = _timestamp(row["collected_at"])
            key = (
                str(row["market"]),
                str(row["vendor_family"]),
                str(row["channel_family"]),
                table,
                "seeded" if row["id"] in seeded_row_ids else "discovered",
                collected_at.date(),
                row["market"] in markets,
                row["id"] in receipted_row_ids,
            )
            counts[key] = counts.get(key, 0) + 1
            available[key] = max(available.get(key, collected_at), collected_at)
    return tuple(
        HistoricalObservation(
            metric_date=key[5],
            market=key[0],
            collection_vendor=key[1],
            platform=key[2],
            collection_mode=key[3],
            seeded_or_discovered=key[4],
            geo_confidence=1.0 if key[6] else 0.0,
            evidence_quality=1.0 if key[7] else 0.0,
            source_regime=key[1],
            value=float(counts[key]),
            available_at=available[key],
        )
        for key in sorted(counts, key=lambda item: tuple(str(part) for part in item))
    )


def manifest_history_normalization(bundle: ReplayInputBundle) -> Mapping[str, NormalizedHistory]:
    as_of = _cutoff_end(bundle.cutoff)
    rows = admitted_history(bundle)
    normalized = {}
    for market in sorted({row.market for row in rows}):
        market_rows = tuple(row for row in rows if row.market == market)
        normalized[market] = normalize_vendor_regimes(
            market_rows, MANIFEST_HISTORY_RULES, as_of=as_of
        )
    return MappingProxyType(normalized)


def _replay_metrics(
    *,
    cutoff: date,
    row_count: int,
    markets: tuple[str, ...],
    signals: tuple[ReplaySignal, ...],
    known_events: tuple[KnownEvent, ...],
    sample_per_market: int,
    history_normalization: Mapping[str, NormalizedHistory],
) -> ManifestReplayMetrics:
    ordered_signals = tuple(sorted(signals, key=lambda signal: (signal.market, signal.signal_id)))
    ordered_events = _ordered_events(known_events)
    if not ordered_signals or not ordered_events:
        raise IncompleteReplayInput("replay signals and known events must be nonempty")
    cutoff_end = _cutoff_end(cutoff)
    for signal in ordered_signals:
        if signal.source_max_observed_at.astimezone(UTC) > cutoff_end:
            raise FutureLeakageDetected(f"future source timestamp for {signal.signal_id}")
    if any(not any(signal.market == market for signal in ordered_signals) for market in markets):
        raise IncompleteReplayInput("manifest replay requires signals in every market")
    if any(not any(event.market == market for event in ordered_events) for market in markets):
        raise IncompleteReplayInput("manifest replay requires known events in every market")
    signal_ids = tuple(signal.signal_id for signal in ordered_signals)
    if len(signal_ids) != len(set(signal_ids)):
        raise IncompleteReplayInput("duplicate signal ID")

    recall_by_market = {}
    for market in markets:
        market_events = tuple(event for event in ordered_events if event.market == market)
        recalled = sum(
            any(
                signal.market == event.market
                and set(event.member_identities).intersection(signal.member_identities)
                for signal in ordered_signals
            )
            for event in market_events
        )
        recall_by_market[market] = replay._metric(recalled, len(market_events))
    recalled = sum(metric.numerator for metric in recall_by_market.values())

    cluster_counts: dict[tuple[str, str], int] = {}
    for signal in ordered_signals:
        key = (signal.market, signal.cluster_signature)
        cluster_counts[key] = cluster_counts.get(key, 0) + 1
    duplicate_count = sum(count - 1 for count in cluster_counts.values() if count > 1)
    foreign_count = sum(
        any(identity.split("|", 1)[0] != signal.market for identity in signal.member_identities)
        for signal in ordered_signals
    )
    signal_count = len(ordered_signals)
    return ManifestReplayMetrics(
        cutoff=cutoff,
        row_count=row_count,
        signal_count=signal_count,
        known_event_recall=replay._metric(recalled, len(ordered_events)),
        known_event_recall_by_market=MappingProxyType(recall_by_market),
        duplicate_rate=replay._metric(duplicate_count, signal_count),
        foreign_leakage=replay._metric(foreign_count, signal_count),
        receipt_completeness=replay._metric(
            sum(signal.membership_complete for signal in ordered_signals), signal_count
        ),
        evidence_coverage=replay._metric(
            sum(signal.evidence_ready for signal in ordered_signals), signal_count
        ),
        geo_coverage=replay._metric(
            sum(signal.geo_proven for signal in ordered_signals), signal_count
        ),
        review_sample=replay._review_sample(ordered_signals, cutoff, sample_per_market),
        history_normalization=history_normalization,
    )


def evaluate_replay_manifest(
    *,
    input_payload: dict,
    expected_input_sha256: str,
    signals: tuple[ReplaySignal, ...],
    known_events: tuple[KnownEvent, ...],
    sample_per_market: int = 5,
) -> ReplayMetrics:
    require_input_digest(input_payload, expected_input_sha256)
    bundle = replay.validate_replay_input(input_payload)
    admitted_events = admitted_known_events(bundle)
    _require_known_event_membership(admitted_events, tuple(known_events))
    history_normalization = manifest_history_normalization(bundle)
    return _replay_metrics(
        cutoff=bundle.cutoff,
        row_count=int(bundle.source_snapshot["row_counts_by_table"]["event_ledger"]),
        markets=tuple(bundle.scope["market_scope"]),
        signals=tuple(signals),
        known_events=admitted_events,
        sample_per_market=sample_per_market,
        history_normalization=history_normalization,
    )


# Retained replay over a real protected capture (L01, versioned beside the entry above).
#
# evaluate_replay_manifest is the certified path and stays exactly as it is: its validator
# admits only synthetic fixtures. The entry point below reads a local copy of one pinned
# protected capture artifact, the capture.json the capture command stored under
# gs://ogilvy-trends-v2-oi-source-artifacts-staging/captures/<initial manifest>/<sha256>/,
# checks it against the protected context registry and the identity the caller pinned,
# and only then builds the production replay payload and computes metrics.
#
# Two kinds of check, kept apart in the result.
#
# Self consistency. The capture's sha256 equals the caller's binding, the capture module's
# own validator accepts its structure and snapshot, and its snapshot digest, cutoff,
# profile, per table digests, captured tables and creation evidence agree with the
# binding and the registry. Every one of these values is read from the capture file or
# computed from it by the caller, so together they prove only that the file agrees with
# itself: its initial manifest is checked against its own field, and its client scope
# and reviewed metadata come from the file.
#
# Authority. The registry pins the result digest of the ledger row the capture command
# wrote. A copy of that row, however it was read, is authenticated by that pin: its
# canonical payload must hash to the pinned digest and its result id must recompute. The
# payload in turn names the stored artifact's sha256, the snapshot digest and the capture
# receipt digest, so a row that holds proves the capture bytes, the snapshot and the
# creation owner against a pin that does not come from the capture. Without such a row
# the result says capture_self_consistent_authority_unverified, real is false and every
# binding the row would prove is listed as unproven. The approval row and the manifest's
# input artifacts are not re-read on either path; the online reader,
# _protected_snapshot_result_to_replay, reads them.
#
# The replay payload is built by the steps the online reader runs after its read
# (_production_replay_result and adapt_production_result_to_replay_input), so both paths
# compose the same components from the same capture.
#
# Known events: no real known event set exists for any retained capture. Without one,
# recall is unknown, never zero and never a pass. A supplied set must carry the digest
# the caller pinned for it.

RETAINED_BINDING_FIELDS = frozenset(
    {
        "profile_id",
        "cutoff_date",
        "consumption_id",
        "manifest_sha256",
        "result_id",
        "result_digest",
        "capture_sha256",
        "snapshot_digest",
        "table_digests",
        "known_event_set_digest",
    }
)
_REGISTRY_PIN_FIELDS = (
    "profile_id",
    "cutoff_date",
    "consumption_id",
    "manifest_sha256",
    "result_id",
    "result_digest",
)
_KNOWN_SET_FIELDS = frozenset({"set_id", "events", "digest"})
_KNOWN_FIELDS = frozenset({"event_id", "market", "member_identities"})
KNOWN_EVENT_GAP = (
    "no real known event set exists for this retained capture, so known event recall is "
    "unknown; it is neither zero nor a pass"
)
AUTHORITY_VERIFIED = "authority_verified"
AUTHORITY_UNVERIFIED = "capture_self_consistent_authority_unverified"
# Bindings only an independent pin proves; each is self consistent on the offline path.
UNPROVEN_WITHOUT_RESULT_ROW = (
    "capture_owner",
    "capture_sha256",
    "consumption_id",
    "manifest_sha256",
    "result_digest",
    "result_id",
    "snapshot_digest",
    "table_digests",
)
_AUTHORITY_NOTES = {
    AUTHORITY_VERIFIED: (
        "the result ledger row hashes to the registry's pinned result digest, its result "
        "id recomputes, and its payload names this capture's sha256, snapshot digest and "
        "capture receipt digest; the approval row and the manifest's input artifacts are "
        "not re-read here. Verification covers the capture only, not the signals, known "
        "events or quality flags the caller supplies"
    ),
    AUTHORITY_UNVERIFIED: (
        "authority unverified: the capture agrees with itself, with the caller's binding "
        "and with the registry, but no result ledger row was supplied, so nothing outside "
        "the capture file proves that these bytes are the pinned result's capture"
    ),
}
_CALLER_ENTRY_NOTE = (
    "authority unverified: the registry entry was supplied by the caller and did not come "
    "from the repo's protected context registry, so a result row that holds against it "
    "proves nothing about the pinned result"
)
_SUCCEEDED_OPERATION = "source_snapshot_capture"


class RetainedCaptureMismatch(IncompleteReplayInput):
    pass


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _without_runtime(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: None if key == "runtime_ms" else _without_runtime(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_without_runtime(item) for item in value)
    if isinstance(value, list):
        return [_without_runtime(item) for item in value]
    return value


def stable_replay_digest(payload: Mapping[str, object]) -> str:
    """The replay payload digest with every measured runtime nulled.

    replay_digest covers provider runtimes measured by the clock, so two replays of the
    same capture differ in it. This digest drops replay_digest and nulls every runtime_ms,
    and is the same for the same capture, provider and scope. It covers the scope, whose
    run id, brand configuration and theme the retained path sets itself, so it cannot be
    compared with the digest of an online replay.
    """
    preimage = {key: item for key, item in payload.items() if key != "replay_digest"}
    return replay._digest(_without_runtime(preimage))


def _retained_binding(binding: object) -> Mapping[str, object]:
    if not isinstance(binding, Mapping) or set(binding) != RETAINED_BINDING_FIELDS:
        raise RetainedCaptureMismatch("binding_fields_invalid")
    if not isinstance(binding["table_digests"], Mapping):
        raise RetainedCaptureMismatch("binding_fields_invalid")
    return binding


def _require_registry(entry: object, binding: Mapping[str, object]) -> None:
    from src.analysis.open_intelligence.protected_context_registry import (
        RESULT_VERSION_V1,
        ProtectedContextEntry,
    )

    if type(entry) is not ProtectedContextEntry:
        raise RetainedCaptureMismatch("registry_entry_invalid")
    if not entry.pinned or any(
        getattr(entry, name) is None
        for name in ("consumption_id", "manifest_sha256", "result_id", "result_digest")
    ):
        raise RetainedCaptureMismatch("registry_entry_unpinned")
    if entry.result_contract_version != RESULT_VERSION_V1:
        raise RetainedCaptureMismatch("registry_entry_not_v1_capture")
    for name in _REGISTRY_PIN_FIELDS:
        if binding[name] != getattr(entry, name):
            raise RetainedCaptureMismatch(f"registry_pin_mismatch:{name}")


def _admitted_capture(capture_raw: object, binding: Mapping[str, object], entry) -> dict:
    """Check the local capture bytes against themselves, the binding and the registry.

    Returns the validated capture. These are self consistency checks: the initial manifest
    is compared with the artifact's own field and the client scope and reviewed metadata
    are taken from the artifact, so passing them proves no authority.
    """
    from scripts.staging import capture_protected_production_snapshot as protected
    from src.analysis.open_intelligence.production_snapshot import _deserialize_plan
    from src.analysis.open_intelligence.production_snapshot_capture import (
        validate_captured_production_snapshot,
    )
    from src.analysis.open_intelligence.production_snapshot_storage import (
        SourceCaptureStorageError,
        _unpack,
        _validate_artifact,
    )
    from src.analysis.open_intelligence.production_snapshot_tables import PROFILE_ID

    if type(capture_raw) is not bytes:
        raise RetainedCaptureMismatch("capture_invalid")
    if hashlib.sha256(capture_raw).hexdigest() != binding["capture_sha256"]:
        raise RetainedCaptureMismatch("capture_sha256_mismatch")
    try:
        artifact = _unpack(capture_raw)
        _validate_artifact(artifact, artifact.get("initial_manifest_sha256"))
        capture = validate_captured_production_snapshot(
            artifact["capture"],
            cutoff_date=date.fromisoformat(entry.cutoff_date),
            client_scope_id=artifact["capture"]["assembly"]["snapshot"]["client_scope_id"],
            market_scope=entry.market_scope,
            reviewed_metadata=artifact["capture"]["assembly"]["source_metadata_utf8"].encode(
                "utf-8"
            ),
            expected_creator_email=protected._IDENTITY,
        )
        plan = _deserialize_plan(capture["assembly"]["snapshot_plan"])
    except (SourceCaptureStorageError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise RetainedCaptureMismatch("capture_invalid") from error
    snapshot = capture["assembly"]["snapshot"]
    if snapshot["source_digest"] != binding["snapshot_digest"]:
        raise RetainedCaptureMismatch("snapshot_digest_mismatch")
    if snapshot["cutoff_date"] != entry.cutoff_date or plan.cutoff_date.isoformat() != (
        entry.cutoff_date
    ):
        raise RetainedCaptureMismatch("capture_cutoff_mismatch")
    if plan.profile_id != PROFILE_ID:
        raise RetainedCaptureMismatch("capture_profile_mismatch")
    if snapshot["source_as_of"] != entry.source_as_of:
        raise RetainedCaptureMismatch("registry_pin_mismatch:source_as_of")
    if tuple(sorted(snapshot["market_scope"])) != tuple(sorted(entry.market_scope)):
        raise RetainedCaptureMismatch("registry_pin_mismatch:market_scope")
    if tuple(item.destination_table for item in plan.statements) != tuple(entry.snapshot_tables):
        raise RetainedCaptureMismatch("registry_pin_mismatch:snapshot_tables")
    if dict(snapshot["table_digests"]) != dict(binding["table_digests"]):
        raise RetainedCaptureMismatch("table_digests_mismatch")
    owners = {
        (item.get("manifest_sha256"), item.get("consumption_id"))
        for item in artifact["creation_evidence"]
        if isinstance(item, Mapping)
    }
    if (entry.manifest_sha256, entry.consumption_id) not in owners:
        raise RetainedCaptureMismatch("capture_owner_mismatch")
    return capture


def _verify_result_row(result_row: object, entry, capture_raw: bytes, capture) -> None:
    """Prove the capture against the ledger row the registry's result digest pins.

    The row may come from any read; the pin authenticates it. Its canonical payload must
    hash to the registry's result digest and its result id must recompute (both checked
    by the retained v1 result record), it must be the pinned consumption's completed
    source snapshot capture, and its payload must name this capture's sha256, snapshot
    digest and capture receipt digest, scope, cutoff and source time.
    """
    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.production_snapshot_tables import (
        retained_origin_registry,
        retained_v1_result,
    )

    code = "result_row_invalid"
    if not isinstance(result_row, Mapping):
        raise RetainedCaptureMismatch(code)
    row = dict(result_row)
    completed = row.get("completed_at")
    if type(completed) is str:
        try:
            row["completed_at"] = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        except ValueError as error:
            raise RetainedCaptureMismatch(code) from error
    try:
        result = retained_v1_result(
            row, code, mode="historical_replay", registry=retained_origin_registry()
        )
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise RetainedCaptureMismatch(code) from error
    if result.result_id != entry.result_id or result.result_digest != entry.result_digest:
        raise RetainedCaptureMismatch("result_row_pin_mismatch")
    if (
        result.consumption_id != entry.consumption_id
        or result.manifest_sha256 != entry.manifest_sha256
    ):
        raise RetainedCaptureMismatch("result_row_owner_mismatch")
    if (
        result.operation != _SUCCEEDED_OPERATION
        or result.status != "succeeded"
        or result.result_reference != result.execution_name + "#source-snapshot"
    ):
        raise RetainedCaptureMismatch("result_row_not_a_completed_capture")
    payload = json.loads(result.canonical_result_json)
    stored = payload.get("stored_artifact")
    if (
        not isinstance(stored, Mapping)
        or stored.get("sha256") != hashlib.sha256(capture_raw).hexdigest()
    ):
        raise RetainedCaptureMismatch("result_row_capture_mismatch")
    snapshot = capture["assembly"]["snapshot"]
    if payload.get("snapshot_digest") != snapshot["source_digest"] or payload.get(
        "capture_receipt_digest"
    ) != canonical_digest(capture):
        raise RetainedCaptureMismatch("result_row_snapshot_mismatch")
    if (
        payload.get("missing_checks") != []
        or payload.get("client_scope_id") != snapshot["client_scope_id"]
        or payload.get("cutoff_date") != entry.cutoff_date
        or payload.get("source_as_of") != entry.source_as_of
        or not isinstance(payload.get("market_scope"), list)
        or sorted(payload["market_scope"]) != sorted(entry.market_scope)
    ):
        raise RetainedCaptureMismatch("result_row_scope_mismatch")


def _retained_entry_for(values: Mapping[str, object], registry_entry: object):
    from src.analysis.open_intelligence.protected_context_registry import profile_for

    if not isinstance(values["cutoff_date"], str):
        raise RetainedCaptureMismatch("registry_pin_mismatch:cutoff_date")
    try:
        entry = profile_for(values["cutoff_date"]) if registry_entry is None else registry_entry
    except ValueError as error:
        raise RetainedCaptureMismatch("registry_pin_mismatch:cutoff_date") from error
    _require_registry(entry, values)
    return entry


def _repo_entry(cutoff: str):
    """The repo registry's entry for the cutoff, or None when it has none."""
    from src.analysis.open_intelligence.protected_context_registry import profile_for

    try:
        return profile_for(cutoff)
    except ValueError:
        return None


def _replay_payload(capture, entry, values, semantic_provider) -> dict:
    from src.contracts.open_intelligence import resolve_client_scope

    prepared = capture["assembly"]
    cutoff = date.fromisoformat(entry.cutoff_date)
    scope = resolve_client_scope(
        run_id=f"retained_replay_{cutoff:%Y%m%d}",
        client_scope_id=prepared["snapshot"]["client_scope_id"],
        market_scope=tuple(entry.market_scope),
    )
    provider = semantic_provider or replay.LexicalSimilarityProviderV2()
    result, _rules = replay._production_replay_result(prepared, cutoff, scope, provider)
    payload = replay.adapt_production_result_to_replay_input(
        result, scope=scope, production_snapshot=prepared, semantic_provider=provider
    )
    if payload["production_snapshot"]["snapshot"]["source_digest"] != values["snapshot_digest"]:
        raise RetainedCaptureMismatch("snapshot_digest_mismatch")
    return payload


def retained_replay_payload(
    *,
    capture_raw: bytes,
    binding: Mapping[str, object],
    registry_entry: object = None,
    semantic_provider: object = None,
) -> dict:
    """The production replay payload of one self consistent retained capture.

    It runs the same registry and capture checks as evaluate_retained_replay and then the
    steps the online reader runs after its read, and proves no authority by itself.
    """
    values = _retained_binding(binding)
    entry = _retained_entry_for(values, registry_entry)
    capture = _admitted_capture(capture_raw, values, entry)
    return _replay_payload(capture, entry, values, semantic_provider)


def _known_events(
    known_event_set: object, binding: Mapping[str, object], markets, observed: Mapping[str, str]
):
    pinned = binding["known_event_set_digest"]
    if known_event_set is None:
        if pinned is not None:
            raise RetainedCaptureMismatch("known_event_set_digest_mismatch")
        return None
    if not isinstance(known_event_set, Mapping) or set(known_event_set) != _KNOWN_SET_FIELDS:
        raise RetainedCaptureMismatch("known_event_set_invalid")
    events = known_event_set["events"]
    if (
        not isinstance(events, list)
        or pinned is None
        or known_event_set["digest"] != pinned
        or _canonical_sha256(events) != pinned
    ):
        raise RetainedCaptureMismatch("known_event_set_digest_mismatch")
    admitted = []
    for event in events:
        if not isinstance(event, Mapping) or set(event) != _KNOWN_FIELDS:
            raise RetainedCaptureMismatch("known_event_set_invalid")
        members = event["member_identities"]
        if event["market"] not in markets or not isinstance(members, list) or not members:
            raise RetainedCaptureMismatch("known_event_set_invalid")
        if any(observed.get(member) != event["market"] for member in members):
            raise RetainedCaptureMismatch("known_event_member_not_admitted")
        admitted.append(KnownEvent(event["event_id"], event["market"], tuple(members)))
    identifiers = [event.event_id for event in admitted]
    if len(identifiers) != len(set(identifiers)):
        raise RetainedCaptureMismatch("known_event_set_invalid")
    return _ordered_events(tuple(admitted))


def _unknown(reason: str) -> dict[str, object]:
    return {"state": "unknown", "reason": reason, "metric": None}


def _measured(metric) -> dict[str, object]:
    numerator, denominator, rate = metric.as_tuple()
    return {
        "state": "measured",
        "reason": None,
        "metric": {"numerator": numerator, "denominator": denominator, "rate": rate},
    }


def _quality_metric(numerator: int, denominator: int, measured: bool) -> dict[str, object]:
    metric = replay._metric(numerator, denominator)
    return {
        "numerator": metric.numerator,
        "denominator": metric.denominator,
        "rate": metric.rate if measured else None,
    }


def _quality(ordered, measured: bool) -> dict[str, object]:
    """Quality counts over the signals; rates only when the caller measured the flags."""
    cluster_counts: dict[tuple[str, str], int] = {}
    for signal in ordered:
        key = (signal.market, signal.cluster_signature)
        cluster_counts[key] = cluster_counts.get(key, 0) + 1
    count = len(ordered)
    duplicates = sum(value - 1 for value in cluster_counts.values() if value > 1)
    leaked = sum(
        any(identity.split("|", 1)[0] != signal.market for identity in signal.member_identities)
        for signal in ordered
    )
    return {
        "measured": measured,
        "duplicate_rate": _quality_metric(duplicates, count, measured),
        "foreign_leakage": _quality_metric(leaked, count, measured),
        "receipt_completeness": _quality_metric(
            sum(signal.membership_complete for signal in ordered), count, measured
        ),
        "evidence_coverage": _quality_metric(
            sum(signal.evidence_ready for signal in ordered), count, measured
        ),
        "geo_coverage": _quality_metric(
            sum(signal.geo_proven for signal in ordered), count, measured
        ),
        "human_coherence_status": "pending",
    }


def evaluate_retained_replay(
    *,
    capture_raw: bytes,
    binding: Mapping[str, object],
    signals: tuple[ReplaySignal, ...],
    known_event_set: Mapping[str, object] | None = None,
    registry_entry: object = None,
    semantic_provider: object = None,
    sample_per_market: int = 5,
    result_row: Mapping[str, object] | None = None,
    quality_measured: bool = False,
) -> dict[str, object]:
    """Evaluate one retained protected capture.

    Order: binding shape, registry pins, the capture's self consistency (bytes, structure,
    snapshot digest, cutoff, profile, captured tables, per table digests and creation
    owner), then the result ledger row when one is supplied, all before the replay payload
    is built; signals and known events are checked against the admitted observations
    before any metric. Any mismatch refuses.

    authority is authority_verified, and real is true, only when a result row proved every
    binding against the repo registry's pinned result digest. Without a row, or with a
    registry_entry the caller supplied that is not the repo registry's own entry, the
    authority is capture_self_consistent_authority_unverified, real is false, the
    bindings the row would prove are listed as unproven and unproven_reasons says why. quality_measured says whether the signal flags are
    measurements; when false every quality rate is null and only counts are reported.
    Recall stays unknown without a pinned known event set.
    """
    if type(quality_measured) is not bool:
        raise RetainedCaptureMismatch("quality_measured_invalid")
    values = _retained_binding(binding)
    entry = _retained_entry_for(values, registry_entry)
    capture = _admitted_capture(capture_raw, values, entry)
    from_repo = registry_entry is None or registry_entry == _repo_entry(values["cutoff_date"])
    reasons = []
    if result_row is None:
        independent = "absent"
        reasons.append("no_result_row")
    else:
        _verify_result_row(result_row, entry, capture_raw, capture)
        independent = "held"
    if not from_repo:
        # A caller's entry can pin anything, so a row that holds against it proves nothing.
        reasons.append("registry_entry_not_from_repo")
    authority = AUTHORITY_UNVERIFIED if reasons else AUTHORITY_VERIFIED
    notes = [_AUTHORITY_NOTES[authority]] if from_repo or result_row is None else []
    if not from_repo:
        notes.append(_CALLER_ENTRY_NOTE)
    note = ". ".join(notes)
    payload = _replay_payload(capture, entry, values, semantic_provider)
    snapshot = capture["assembly"]["snapshot"]
    cutoff = date.fromisoformat(entry.cutoff_date)
    # One admitted member identity per observation, market|candidate_type|term, the
    # identity pipeline._observation_identity gives it; its value is its market.
    observed = {
        f"{item['market']}|{item['candidate_type']}|{item['term']}": item["market"]
        for item in payload["observations"]
    }
    markets = tuple(entry.market_scope)
    ordered = tuple(sorted(signals, key=lambda signal: (signal.market, signal.signal_id)))
    if not ordered:
        raise IncompleteReplayInput("replay signals must be nonempty")
    cutoff_end = _cutoff_end(cutoff)
    for signal in ordered:
        if signal.source_max_observed_at.astimezone(UTC) > cutoff_end:
            raise FutureLeakageDetected(f"future source timestamp for {signal.signal_id}")
    identifiers = [signal.signal_id for signal in ordered]
    if len(identifiers) != len(set(identifiers)):
        raise IncompleteReplayInput("duplicate signal ID")
    for signal in ordered:
        if signal.market not in markets or any(
            observed.get(member) != signal.market for member in signal.member_identities
        ):
            raise RetainedCaptureMismatch("signal_member_not_admitted")
    events = _known_events(known_event_set, values, markets, observed)
    if events is None:
        recall = _unknown("no_known_event_set")
        by_market = {market: _unknown("no_known_event_set") for market in sorted(markets)}
    else:
        by_market = {}
        for market in sorted(markets):
            market_events = tuple(event for event in events if event.market == market)
            if not market_events:
                by_market[market] = _unknown("no_known_events_in_market")
                continue
            recalled = sum(
                any(
                    signal.market == event.market
                    and set(event.member_identities).intersection(signal.member_identities)
                    for signal in ordered
                )
                for event in market_events
            )
            by_market[market] = _measured(replay._metric(recalled, len(market_events)))
        if any(value["state"] == "unknown" for value in by_market.values()):
            recall = _unknown("markets_without_known_events")
        else:
            numerator = sum(value["metric"]["numerator"] for value in by_market.values())
            denominator = sum(value["metric"]["denominator"] for value in by_market.values())
            recall = _measured(replay._metric(numerator, denominator))
    return {
        "evidence_scope": "retained_capture",
        "authority": authority,
        "real": authority == AUTHORITY_VERIFIED,
        "authority_note": note,
        "registry_entry_source": "repo" if from_repo else "caller_supplied",
        "cutoff": cutoff.isoformat(),
        "profile_id": entry.profile_id,
        "snapshot_digest": values["snapshot_digest"],
        "capture_sha256": values["capture_sha256"],
        "replay_digest": payload["replay_digest"],
        "stable_replay_digest": stable_replay_digest(payload),
        "components_digest": replay._digest(payload["components_by_candidate"]),
        "bindings": {
            "registry_pins": "held",
            "known_event_set": "absent" if events is None else "held",
        },
        "self_consistency": {
            "capture_sha256": "held",
            "capture_structure": "held",
            "snapshot_digest": "held",
            "cutoff": "held",
            "profile": "held",
            "registry_scope": "held",
            "table_digests": "held",
            "capture_owner": "held",
        },
        "independent_pins": {"result_row": independent},
        "unproven": list(UNPROVEN_WITHOUT_RESULT_ROW) if reasons else [],
        "unproven_reasons": reasons,
        "counts": {
            "row_counts_by_table": dict(sorted(snapshot["row_counts_by_table"].items())),
            "section_counts": dict(sorted(snapshot["section_counts"].items())),
            "observations": len(payload["observations"]),
            "components": len(payload["components_by_candidate"]),
            "signals": len(ordered),
            "known_events": 0 if events is None else len(events),
        },
        "table_digests": dict(sorted(values["table_digests"].items())),
        "known_event_recall": recall,
        "known_event_recall_by_market": by_market,
        "known_event_gap": KNOWN_EVENT_GAP if events is None else None,
        "quality": _quality(ordered, quality_measured),
        "exclusions": {
            "coverage_limitations": sorted(snapshot["coverage_limitations"]),
        },
        "markets_without_signals": sorted(
            market for market in markets if not any(s.market == market for s in ordered)
        ),
        "review_sample_signal_ids": _retained_review_sample(ordered, cutoff, sample_per_market),
    }


def _retained_review_sample(signals, cutoff: date, sample_per_market: int) -> list[str]:
    """Up to sample_per_market signals per market, in an order seeded by cutoff and id.

    A market without signals contributes none and is listed separately rather than
    refusing, because a retained capture can hold markets the signals do not reach.
    """
    if type(sample_per_market) is not int or sample_per_market < 1:
        raise ValueError("sample_per_market must be a positive integer")
    selected = []
    for market in sorted({signal.market for signal in signals}):
        ranked = sorted(
            (signal for signal in signals if signal.market == market),
            key=lambda signal: hashlib.sha256(
                f"{cutoff.isoformat()}|{signal.signal_id}".encode()
            ).hexdigest(),
        )
        selected.extend(signal.signal_id for signal in ranked[:sample_per_market])
    return selected


__all__ = [
    "AUTHORITY_UNVERIFIED",
    "AUTHORITY_VERIFIED",
    "KNOWN_EVENT_GAP",
    "MANIFEST_HISTORY_RULES",
    "RETAINED_BINDING_FIELDS",
    "UNPROVEN_WITHOUT_RESULT_ROW",
    "KnownEventMembershipChanged",
    "ManifestReplayMetrics",
    "ReplayInputDigestMismatch",
    "RetainedCaptureMismatch",
    "admitted_history",
    "admitted_known_events",
    "evaluate_replay_manifest",
    "evaluate_retained_replay",
    "manifest_history_normalization",
    "require_input_digest",
    "retained_replay_payload",
    "stable_replay_digest",
]
