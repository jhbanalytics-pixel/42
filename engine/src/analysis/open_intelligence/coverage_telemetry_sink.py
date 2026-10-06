"""Injectable disposition sink and the boundary emitters that feed it.

The pipeline boundaries never build disposition records themselves. They hand
their own decision records (the fetched frames, the dedup decision list, the
enriched rows, the membership rows, the release readback) to a
``BoundaryTelemetry`` bound to one operation, and it writes validated
dispositions through one sink. A sink can be replaced by
``DisabledDispositionSink`` during recovery: the boundaries keep running and
nothing is recorded, so a faulty projection is switched off without touching
raw evidence or release authority. A disposition the kernel refuses is never
retried or guessed at; the boundary is marked unavailable for that operation
instead, so a missing count reads as unknown and never as zero. No exception
raised while deriving an identity, validating a record or writing to the sink
reaches the caller: the boundary is marked unavailable with ``emission_failed``
and the producer continues, and a sink that refuses the marker itself is left
silent rather than allowed to stop evidence.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from functools import partial
from urllib.parse import urlsplit

from src.analysis.open_intelligence.coverage_telemetry import (
    BOUNDARIES,
    COVERAGE_UNITS,
    classified_observation_keys,
    disposition_record,
)

DEDUP_DUPLICATE_REASON = "youtube_scrape_duplicate"
EMISSION_FAILED_REASON = "emission_failed"
UNCLASSIFIED_REASON = "unclassified"
STAGE_FAILED_REASON = "stage_failed"
_ROUTE_FAILURE_REASONS = {True: "fetch_failed", False: "endpoint_failure"}
_UNKNOWN_ROUTE = "unknown"


class MemoryDispositionSink:
    """Keeps every disposition and note in memory, in emission order."""

    enabled = True

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.notes: list[dict] = []

    def emit(self, record: Mapping) -> None:
        self.records.append(dict(record))

    def note(self, note: Mapping) -> None:
        self.notes.append(dict(note))


class DisabledDispositionSink:
    """Drops everything. The projection is off; the boundaries are untouched."""

    enabled = False
    records: tuple = ()
    notes: tuple = ()

    def emit(self, record: Mapping) -> None:
        return None

    def note(self, note: Mapping) -> None:
        return None


def _text(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _rows(frame: object) -> list[Mapping]:
    to_dict = getattr(frame, "to_dict", None)
    if callable(to_dict):
        return list(to_dict(orient="records"))
    return list(frame)


def observation_identity(
    row: Mapping, collection_event_id: str
) -> tuple[str, str, str | None, str | None]:
    """Return (observation_key, identity_kind, native_namespace, native_id) for one row.

    A row is a native identity only when it carries both a platform and the
    ``native_id`` the producer preserved. Otherwise the identity is inferred: the
    normalised URL when the row has one, else the collection event itself. Text
    never takes part, so identical text alone never merges two records. A URL
    the parser refuses raises; the emitter contains that and marks the boundary.
    """
    platform = _text(row.get("platform"))
    native_id = _text(row.get("native_id"))
    if platform is not None and native_id is not None:
        return f"{platform}:{native_id}", "native", platform, native_id
    url = _text(row.get("url"))
    if url is not None:
        parts = urlsplit(url)
        host = (parts.netloc or "unknown").lower()
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        return f"url:{host}:{digest}", "inferred", platform, None
    return f"row:{collection_event_id}", "inferred", platform, None


class BoundaryTelemetry:
    """One operation's boundary emitter over an injectable sink."""

    def __init__(self, sink: object) -> None:
        self._sink = sink
        self._binding: dict | None = None
        self._identities: dict[tuple[str, str], dict] = {}
        self._marked: set[tuple[str, str | None, str, str | None]] = set()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._sink, "enabled", True))

    @property
    def records(self) -> list[dict]:
        return list(getattr(self._sink, "records", ()))

    @property
    def notes(self) -> list[dict]:
        return list(getattr(self._sink, "notes", ()))

    def bind(self, *, operation_id: str, source_binding_digest: str, observed_at: datetime) -> None:
        self._binding = {
            "operation_id": operation_id,
            "source_binding_digest": source_binding_digest,
            "observed_at": observed_at,
        }

    def _require_binding(self) -> dict:
        if self._binding is None:
            raise ValueError("telemetry_unbound")
        return self._binding

    def _note(self, note: Mapping) -> None:
        try:
            self._sink.note(note)
        except Exception:
            return None

    def _emit(self, boundary: str, **fields: object) -> bool:
        try:
            record = disposition_record(boundary=boundary, **fields)
        except ValueError as error:
            self._mark_unavailable(
                boundary,
                operation_id=fields.get("operation_id"),
                reason_code="disposition_invalid",
                market=fields.get("market"),
                detail=str(error),
            )
            return False
        self._sink.emit(record)
        return True

    def _mark_unavailable(
        self,
        boundary: str,
        *,
        operation_id: object,
        reason_code: str,
        market: object = None,
        detail: str | None = None,
    ) -> None:
        market_name = market if isinstance(market, str) else None
        marker = (
            boundary,
            operation_id if isinstance(operation_id, str) else None,
            reason_code,
            market_name,
        )
        if marker in self._marked:
            return
        self._marked.add(marker)
        note = {
            "kind": "unavailable",
            "boundary": boundary,
            "operation_id": marker[1],
            "reason_code": reason_code,
            "market": market_name,
            "observed_at": self._binding["observed_at"] if self._binding else None,
        }
        if detail is not None:
            note["detail"] = detail
        self._note(note)

    def _guarded(
        self,
        boundaries: tuple[str, ...],
        *,
        operation_id: object,
        market: object,
        emit: Callable[[], None],
    ) -> None:
        """Run one emission; any exception marks its boundaries unavailable, none is raised."""
        try:
            emit()
        except Exception as error:
            detail = f"{type(error).__name__}: {error}"
            for boundary in boundaries:
                self._mark_unavailable(
                    boundary,
                    operation_id=operation_id,
                    reason_code=EMISSION_FAILED_REASON,
                    market=market,
                    detail=detail,
                )

    def boundary_unavailable(self, boundary: str, *, operation_id: str, reason_code: str) -> None:
        """Record that a boundary could not produce observation level telemetry."""
        if boundary not in BOUNDARIES:
            raise ValueError(f"boundary_unknown:{boundary}")
        if _text(operation_id) is None or _text(reason_code) is None:
            raise ValueError("unavailable_marker_invalid")
        self._mark_unavailable(boundary, operation_id=operation_id, reason_code=reason_code)

    def count(
        self,
        unit: str,
        value: int,
        *,
        market: str | None = None,
        operation_id: str | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        """Record one named unit measured at a boundary, in that unit's own terms."""
        if unit not in COVERAGE_UNITS:
            raise ValueError(f"unit_unknown:{unit}")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"count_invalid:{unit}")
        if operation_id is None or observed_at is None:
            binding = self._require_binding()
            operation_id = operation_id or binding["operation_id"]
            observed_at = observed_at or binding["observed_at"]
        self._note(
            {
                "kind": "count",
                "unit": unit,
                "value": value,
                "market": market,
                "operation_id": operation_id,
                "observed_at": observed_at,
            }
        )

    def emit_collection(
        self,
        market: str,
        frames: Sequence[object],
        dedup_decisions: Iterable[Mapping],
        raw_rows: Sequence[Mapping],
    ) -> None:
        """Emit the producer and dedup dispositions for one market's fetched frames.

        ``frames`` are the connector frames before dedup, ``dedup_decisions`` the
        decision list the dedup wrote for every row it dropped, and ``raw_rows``
        the rows built from the surviving frames in order. A surviving row takes
        its source row id from the raw row; a dropped row has none. The caller
        invokes this after the raw rows are written; a row whose emission fails
        marks the producer and dedup boundaries unavailable and the walk goes on.
        """
        binding = self._require_binding()
        dropped = {
            (decision["frame_index"], decision["row_index"]): decision["reason_code"]
            for decision in dedup_decisions
        }
        survivors = iter(raw_rows)
        for frame_index, frame in enumerate(frames):
            for row_index, row in enumerate(_rows(frame)):
                reason = dropped.get((frame_index, row_index))
                source_row_id = None
                if reason is None:
                    raw_row = next(survivors, None)
                    source_row_id = _text(raw_row.get("id")) if raw_row is not None else None
                self._guarded(
                    ("producer", "dedup"),
                    operation_id=binding["operation_id"],
                    market=market,
                    emit=partial(
                        self._emit_collected_row,
                        market,
                        row,
                        frame_index,
                        row_index,
                        reason,
                        source_row_id,
                    ),
                )

    def _emit_collected_row(
        self,
        market: str,
        row: Mapping,
        frame_index: int,
        row_index: int,
        reason: str | None,
        source_row_id: str | None,
    ) -> None:
        binding = self._require_binding()
        route = _text(row.get("source")) or _UNKNOWN_ROUTE
        event_id = f"{binding['operation_id']}:{market}:{route}:{frame_index}:{row_index}"
        key, kind, namespace, native_id = observation_identity(row, event_id)
        common = {
            "operation_id": binding["operation_id"],
            "observation_key": key,
            "identity_kind": kind,
            "native_namespace": namespace,
            "native_id": native_id,
            "collection_event_id": event_id,
            "source_row_id": source_row_id,
            "market": market,
            "route": route,
            "observed_at": binding["observed_at"],
            "source_binding_digest": binding["source_binding_digest"],
        }
        self._emit("producer", outcome="admitted", reason_code=None, **common)
        if reason is None:
            self._emit("dedup", outcome="admitted", reason_code=None, **common)
            if source_row_id is not None:
                self._identities[(market, source_row_id)] = common
        else:
            self._emit("dedup", outcome="rejected", reason_code=reason, **common)

    def emit_enrichment(
        self, market: str, raw_rows: Sequence[Mapping], enriched_rows: Sequence[Mapping]
    ) -> None:
        """Emit the enrichment and classification dispositions from the enriched rows.

        Every enriched row is admitted at enrichment. Classification admits a row
        with at least one real topic label and rejects the rest as unclassified,
        which is the classifier's own record: null, empty and ``other`` labels are
        not classifications. The caller invokes this after the enriched rows are
        written.
        """
        binding = self._require_binding()
        for raw_row, enriched in zip(raw_rows, enriched_rows, strict=False):
            self._guarded(
                ("enrichment", "classification"),
                operation_id=binding["operation_id"],
                market=market,
                emit=partial(self._emit_enriched_row, market, raw_row, enriched),
            )

    def _emit_enriched_row(self, market: str, raw_row: Mapping, enriched: Mapping) -> None:
        binding = self._require_binding()
        source_row_id = _text(raw_row.get("id"))
        common = self._identities.get((market, source_row_id or ""))
        if common is None:
            self._mark_unavailable(
                "enrichment",
                operation_id=binding["operation_id"],
                reason_code="collection_identity_missing",
                market=market,
            )
            return
        self._emit("enrichment", outcome="admitted", reason_code=None, **common)
        classified = classified_observation_keys(
            [
                {
                    "observation_key": common["observation_key"],
                    "topic_groups": enriched.get("topic_groups"),
                }
            ]
        )
        if classified:
            self._emit("classification", outcome="admitted", reason_code=None, **common)
        else:
            self._emit(
                "classification", outcome="rejected", reason_code=UNCLASSIFIED_REASON, **common
            )

    def route_outcomes(
        self, market_source_counts: Mapping[str, Mapping[str, int]], errors: Iterable[Mapping]
    ) -> None:
        """Record each route's emission count with the failure the producer recorded.

        The reason codes mirror the producer's own diagnostics at the decision
        site: a connector that crashed is ``fetch_failed``, a feed or endpoint the
        connector survived is ``endpoint_failure``, and a market whose ingest body
        failed marks that market's enrichment and classification telemetry
        unavailable as ``stage_failed``.
        """
        failures: dict[tuple[str, str], str] = {}
        for error in errors:
            market = error.get("market")
            route = error.get("source")
            if not isinstance(market, str) or not isinstance(route, str):
                continue
            if route == "pipeline":
                operation_id = self._binding["operation_id"] if self._binding else None
                for boundary in ("enrichment", "classification"):
                    self._mark_unavailable(
                        boundary,
                        operation_id=operation_id,
                        reason_code=STAGE_FAILED_REASON,
                        market=market,
                    )
                continue
            fatal = bool(error.get("fatal", True))
            current = failures.get((market, route))
            if current is None or fatal:
                failures[(market, route)] = _ROUTE_FAILURE_REASONS[fatal]
        for market, counts in market_source_counts.items():
            for route, emissions in counts.items():
                self._note(
                    {
                        "kind": "route",
                        "market": market,
                        "route": route,
                        "emissions": int(emissions),
                        "reason_code": failures.get((market, route)),
                    }
                )

    def emit_membership(
        self,
        rows: Iterable[Mapping],
        *,
        created_at: datetime,
        source_binding_digest: str,
        availability_by_member: Mapping[str, object],
    ) -> None:
        """Emit one membership disposition per bridge row.

        A member identity is an inferred identity: the bridge carries no native
        id. The candidate composition (``signal_id``) is the event that produced
        the edge and the candidate type is the route the member was extracted
        through. A non qualifying member takes its reason from the receipt's
        availability record; a member with no receipt at all is
        ``member_receipt_missing``.
        """
        for row in rows:
            self._guarded(
                ("membership",),
                operation_id=row.get("run_id"),
                market=row.get("market"),
                emit=partial(
                    self._emit_membership_row,
                    row,
                    created_at=created_at,
                    source_binding_digest=source_binding_digest,
                    availability_by_member=availability_by_member,
                ),
            )

    def _emit_membership_row(
        self,
        row: Mapping,
        *,
        created_at: datetime,
        source_binding_digest: str,
        availability_by_member: Mapping[str, object],
    ) -> None:
        qualifies = bool(row.get("qualifies_evidence"))
        reason = None
        if not qualifies:
            availability = _text(availability_by_member.get(row.get("member_identity")))
            reason = (
                f"member_{availability}" if availability is not None else "member_receipt_missing"
            )
        self._emit(
            "membership",
            operation_id=row.get("run_id"),
            observation_key=row.get("member_identity"),
            identity_kind="inferred",
            native_namespace=None,
            native_id=None,
            collection_event_id=row.get("signal_id"),
            source_row_id=_text(row.get("row_id")),
            outcome="admitted" if qualifies else "rejected",
            reason_code=reason,
            market=row.get("market"),
            route=row.get("candidate_type"),
            observed_at=created_at,
            source_binding_digest=source_binding_digest,
        )


__all__ = [
    "DEDUP_DUPLICATE_REASON",
    "EMISSION_FAILED_REASON",
    "STAGE_FAILED_REASON",
    "UNCLASSIFIED_REASON",
    "BoundaryTelemetry",
    "DisabledDispositionSink",
    "MemoryDispositionSink",
    "observation_identity",
]
