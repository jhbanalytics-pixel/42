"""Immutable metadata-only reservation and terminal receipts for Gemini canaries."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol, TypeAlias

from google.genai import types

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TERMINAL_STATES = frozenset({"complete", "error", "timeout", "partial", "uncertain"})
CANARY_LOG_NAME = "open-intelligence-gemini-canary"


class CanaryReceiptConflict(RuntimeError):
    """Receipt history is missing, duplicated, mismatched, or non-immutable."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _digest(value: object, field: str) -> str:
    value = _text(value, field)
    if _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA256")
    return value


def _nonnegative(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CanaryCallReservation:
    receipt_id: str
    lane: str
    consumer: str
    stage: str
    model: str
    thinking_level: str
    run_id: str
    call_index: int
    input_digest: str
    system_instruction_digest: str
    response_schema_digest: str
    sdk_version: str
    api_version: str
    pricing_version: str
    reserved_at: datetime
    contract_version: str


@dataclass(frozen=True, slots=True)
class CanaryCallTerminal:
    receipt_id: str
    run_id: str
    consumer: str
    stage: str
    model: str
    thinking_level: str
    started_at: datetime
    completed_at: datetime
    latency_ms: int
    terminal_state: str
    prompt_token_count: int | None
    candidates_token_count: int | None
    thoughts_token_count: int | None
    total_token_count: int | None
    estimated_cost_usd: Decimal | None
    usage_event_id: str | None
    error_code: str | None
    completed_contract_version: str


CanaryEvent: TypeAlias = CanaryCallReservation | CanaryCallTerminal


@dataclass(frozen=True, slots=True)
class CanaryCallReceipt:
    receipt_id: str
    reservation: CanaryCallReservation
    terminal: CanaryCallTerminal


def _reservation_payload(event: CanaryCallReservation) -> dict[str, object]:
    return {
        field: getattr(event, field)
        for field in (
            "lane",
            "consumer",
            "stage",
            "run_id",
            "call_index",
            "model",
            "thinking_level",
            "input_digest",
            "system_instruction_digest",
            "response_schema_digest",
            "sdk_version",
            "api_version",
            "pricing_version",
        )
    }


def reservation_id_for(event: CanaryCallReservation) -> str:
    payload = json.dumps(
        _reservation_payload(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_reservation(
    *,
    lane: str,
    consumer: str,
    stage: str,
    model: str,
    thinking_level: types.ThinkingLevel,
    run_id: str,
    call_index: int,
    input_digest: str,
    system_instruction_digest: str,
    response_schema_digest: str,
    sdk_version: str,
    api_version: str,
    pricing_version: str,
    reserved_at: datetime,
    contract_version: str,
) -> CanaryCallReservation:
    if lane not in {"baseline", "canary"}:
        raise ValueError("lane is invalid")
    if not isinstance(thinking_level, types.ThinkingLevel):
        raise ValueError("thinking level must be typed")
    event = CanaryCallReservation(
        receipt_id="0" * 64,
        lane=lane,
        consumer=_text(consumer, "consumer"),
        stage=_text(stage, "stage"),
        model=_text(model, "model"),
        thinking_level=thinking_level.value,
        run_id=_text(run_id, "run_id"),
        call_index=_nonnegative(call_index, "call_index"),
        input_digest=_digest(input_digest, "input_digest"),
        system_instruction_digest=_digest(system_instruction_digest, "system_instruction_digest"),
        response_schema_digest=_digest(response_schema_digest, "response_schema_digest"),
        sdk_version=_text(sdk_version, "sdk_version"),
        api_version=_text(api_version, "api_version"),
        pricing_version=_text(pricing_version, "pricing_version"),
        reserved_at=_utc(reserved_at, "reserved_at"),
        contract_version=_text(contract_version, "contract_version"),
    )
    return CanaryCallReservation(**{**asdict(event), "receipt_id": reservation_id_for(event)})


def build_terminal(
    *,
    reservation: CanaryCallReservation,
    started_at: datetime,
    completed_at: datetime,
    terminal_state: str,
    prompt_token_count: int | None,
    candidates_token_count: int | None,
    thoughts_token_count: int | None,
    total_token_count: int | None,
    estimated_cost_usd: Decimal | None,
    usage_event_id: str | None,
    error_code: str | None,
    completed_contract_version: str,
) -> CanaryCallTerminal:
    if not isinstance(reservation, CanaryCallReservation):
        raise ValueError("reservation is invalid")
    started_at = _utc(started_at, "started_at")
    completed_at = _utc(completed_at, "completed_at")
    if completed_at < started_at:
        raise ValueError("terminal timestamps are reversed")
    if terminal_state not in _TERMINAL_STATES:
        raise ValueError("terminal state is invalid")
    if completed_contract_version != reservation.contract_version:
        raise ValueError("terminal contract version differs from reservation")
    latency_ms = int((completed_at - started_at).total_seconds() * 1000)
    counts = (prompt_token_count, candidates_token_count, thoughts_token_count, total_token_count)
    if terminal_state == "complete":
        if (
            any(value is None for value in counts)
            or usage_event_id is None
            or error_code is not None
        ):
            raise ValueError("complete terminal requires complete usage metadata")
        typed_counts = tuple(_nonnegative(value, "token count") for value in counts)
        if typed_counts[3] != sum(typed_counts[:3]):
            raise ValueError("complete terminal token totals do not reconcile")
        if not isinstance(estimated_cost_usd, Decimal) or estimated_cost_usd < 0:
            raise ValueError("complete terminal requires estimated cost")
        _text(usage_event_id, "usage_event_id")
    elif not error_code or usage_event_id is not None:
        raise ValueError("noncomplete terminal requires error and no usage event")
    return CanaryCallTerminal(
        receipt_id=reservation.receipt_id,
        run_id=reservation.run_id,
        consumer=reservation.consumer,
        stage=reservation.stage,
        model=reservation.model,
        thinking_level=reservation.thinking_level,
        started_at=started_at,
        completed_at=completed_at,
        latency_ms=latency_ms,
        terminal_state=terminal_state,
        prompt_token_count=prompt_token_count,
        candidates_token_count=candidates_token_count,
        thoughts_token_count=thoughts_token_count,
        total_token_count=total_token_count,
        estimated_cost_usd=estimated_cost_usd,
        usage_event_id=usage_event_id,
        error_code=error_code,
        completed_contract_version=_text(completed_contract_version, "completed_contract_version"),
    )


def join_receipt(events: Sequence[CanaryEvent]) -> CanaryCallReceipt:
    reservations = [event for event in events if isinstance(event, CanaryCallReservation)]
    terminals = [event for event in events if isinstance(event, CanaryCallTerminal)]
    if len(reservations) != 1 or len(terminals) != 1:
        raise CanaryReceiptConflict("receipt requires one reservation and one terminal")
    reservation = reservations[0]
    terminal = terminals[0]
    if reservation.receipt_id != reservation_id_for(reservation):
        raise CanaryReceiptConflict("reservation identity is invalid")
    if (
        terminal.receipt_id,
        terminal.run_id,
        terminal.consumer,
        terminal.stage,
        terminal.model,
        terminal.thinking_level,
        terminal.completed_contract_version,
    ) != (
        reservation.receipt_id,
        reservation.run_id,
        reservation.consumer,
        reservation.stage,
        reservation.model,
        reservation.thinking_level,
        reservation.contract_version,
    ):
        raise CanaryReceiptConflict("reservation and terminal do not match")
    try:
        rebuilt = build_terminal(
            reservation=reservation,
            started_at=terminal.started_at,
            completed_at=terminal.completed_at,
            terminal_state=terminal.terminal_state,
            prompt_token_count=terminal.prompt_token_count,
            candidates_token_count=terminal.candidates_token_count,
            thoughts_token_count=terminal.thoughts_token_count,
            total_token_count=terminal.total_token_count,
            estimated_cost_usd=terminal.estimated_cost_usd,
            usage_event_id=terminal.usage_event_id,
            error_code=terminal.error_code,
            completed_contract_version=terminal.completed_contract_version,
        )
    except ValueError as error:
        raise CanaryReceiptConflict("terminal content is invalid") from error
    if rebuilt != terminal:
        raise CanaryReceiptConflict("terminal content is not canonical")
    return CanaryCallReceipt(reservation.receipt_id, reservation, terminal)


class CanaryReceiptSink(Protocol):
    def reserve(self, event: CanaryCallReservation) -> None: ...
    def complete(self, event: CanaryCallTerminal) -> None: ...
    def read(self, receipt_id: str) -> tuple[CanaryEvent, ...]: ...
    def list_run(self, run_id: str, contract_version: str) -> tuple[CanaryEvent, ...]: ...


class MemoryCanaryReceiptSink:
    def __init__(self) -> None:
        self._events: list[CanaryEvent] = []

    def reserve(self, event: CanaryCallReservation) -> None:
        if event.receipt_id != reservation_id_for(event):
            raise CanaryReceiptConflict("reservation identity is invalid")
        if self.read(event.receipt_id):
            raise CanaryReceiptConflict("duplicate reservation")
        self._events.append(event)

    def complete(self, event: CanaryCallTerminal) -> None:
        existing = self.read(event.receipt_id)
        if len(existing) != 1 or not isinstance(existing[0], CanaryCallReservation):
            raise CanaryReceiptConflict("terminal requires one reservation")
        join_receipt((*existing, event))
        self._events.append(event)

    def read(self, receipt_id: str) -> tuple[CanaryEvent, ...]:
        return tuple(event for event in self._events if event.receipt_id == receipt_id)

    def list_run(self, run_id: str, contract_version: str) -> tuple[CanaryEvent, ...]:
        receipt_ids = {
            event.receipt_id
            for event in self._events
            if isinstance(event, CanaryCallReservation)
            and event.run_id == run_id
            and event.contract_version == contract_version
        }
        return tuple(event for event in self._events if event.receipt_id in receipt_ids)


def _event_payload(event: CanaryEvent) -> dict[str, object]:
    payload = asdict(event)
    payload["event_type"] = (
        "reservation" if isinstance(event, CanaryCallReservation) else "terminal"
    )
    for key, value in tuple(payload.items()):
        if isinstance(value, datetime):
            payload[key] = value.isoformat().replace("+00:00", "Z")
        elif isinstance(value, Decimal):
            payload[key] = format(value, "f")
    return payload


def _payload_event(payload: Mapping[str, object]) -> CanaryEvent:
    values = dict(payload)
    event_type = values.pop("event_type", None)
    for key in ("reserved_at", "started_at", "completed_at"):
        if key in values:
            values[key] = datetime.fromisoformat(str(values[key]).replace("Z", "+00:00"))
    if values.get("estimated_cost_usd") is not None:
        values["estimated_cost_usd"] = Decimal(str(values["estimated_cost_usd"]))
    if event_type == "reservation":
        return CanaryCallReservation(**values)
    if event_type == "terminal":
        return CanaryCallTerminal(**values)
    raise CanaryReceiptConflict("structured receipt event type is invalid")


class StructuredLoggingCanaryReceiptSink:
    log_name = CANARY_LOG_NAME

    def __init__(
        self,
        *,
        write_structured: Callable[[Mapping[str, object]], None],
        read_structured: Callable[[str], Sequence[Mapping[str, object]]],
        list_structured: Callable[[str, str], Sequence[Mapping[str, object]]],
    ) -> None:
        self._write = write_structured
        self._read = read_structured
        self._list = list_structured

    def reserve(self, event: CanaryCallReservation) -> None:
        if event.receipt_id != reservation_id_for(event):
            raise CanaryReceiptConflict("reservation identity is invalid")
        if self.read(event.receipt_id):
            raise CanaryReceiptConflict("duplicate reservation")
        self._write(_event_payload(event))

    def complete(self, event: CanaryCallTerminal) -> None:
        existing = self.read(event.receipt_id)
        if len(existing) != 1 or not isinstance(existing[0], CanaryCallReservation):
            raise CanaryReceiptConflict("terminal requires one reservation")
        join_receipt((*existing, event))
        self._write(_event_payload(event))

    def read(self, receipt_id: str) -> tuple[CanaryEvent, ...]:
        return tuple(_payload_event(payload) for payload in self._read(receipt_id))

    def list_run(self, run_id: str, contract_version: str) -> tuple[CanaryEvent, ...]:
        return tuple(_payload_event(payload) for payload in self._list(run_id, contract_version))


@dataclass(frozen=True, slots=True)
class ExpectedCanaryCall:
    receipt_id: str


@dataclass(frozen=True, slots=True)
class ReconciledCall:
    receipt_id: str
    state: str
    error_code: str | None


@dataclass(frozen=True, slots=True)
class CanaryRunReconciliation:
    run_id: str
    contract_version: str
    complete: bool
    results: tuple[ReconciledCall, ...]


def _validated_run_events(
    events: Sequence[CanaryEvent], run_id: str, contract_version: str
) -> tuple[CanaryEvent, ...]:
    validated = tuple(events)
    for event in validated:
        if event.run_id != run_id:
            raise CanaryReceiptConflict("canary event is outside the requested run scope")
        if isinstance(event, CanaryCallReservation):
            if event.contract_version != contract_version:
                raise CanaryReceiptConflict("canary event is outside the requested run scope")
            if event.receipt_id != reservation_id_for(event):
                raise CanaryReceiptConflict("reservation identity is invalid")
        elif event.completed_contract_version != contract_version:
            raise CanaryReceiptConflict("canary event is outside the requested run scope")
    return validated


def next_call_index(
    *,
    sink: CanaryReceiptSink,
    run_id: str,
    contract_version: str,
    lane: str,
    consumer: str,
    stage: str,
) -> int:
    events = _validated_run_events(
        sink.list_run(run_id, contract_version), run_id, contract_version
    )
    indexes = sorted(
        event.call_index
        for event in events
        if isinstance(event, CanaryCallReservation)
        and event.lane == lane
        and event.consumer == consumer
        and event.stage == stage
    )
    if indexes != list(range(len(indexes))):
        raise CanaryReceiptConflict("canary reservation call indexes are invalid")
    return len(indexes)


def reconcile_canary_run(
    run_id: str,
    contract_version: str,
    expected_calls: Sequence[ExpectedCanaryCall],
    *,
    sink: CanaryReceiptSink,
) -> CanaryRunReconciliation:
    expected_ids = tuple(call.receipt_id for call in expected_calls)
    if len(expected_ids) != len(set(expected_ids)):
        raise CanaryReceiptConflict("expected calls contain duplicates")
    events = _validated_run_events(
        sink.list_run(run_id, contract_version), run_id, contract_version
    )
    by_id: dict[str, list[CanaryEvent]] = {}
    for event in events:
        by_id.setdefault(event.receipt_id, []).append(event)
    if set(by_id) - set(expected_ids):
        raise CanaryReceiptConflict("canary run contains orphan events")
    results = []
    for receipt_id in expected_ids:
        pair = by_id.get(receipt_id, [])
        reservations = [event for event in pair if isinstance(event, CanaryCallReservation)]
        terminals = [event for event in pair if isinstance(event, CanaryCallTerminal)]
        if len(reservations) != 1 or len(terminals) > 1:
            raise CanaryReceiptConflict("canary receipt cardinality is invalid")
        if not terminals:
            results.append(ReconciledCall(receipt_id, "incomplete_uncertain", "terminal_missing"))
            continue
        terminal = terminals[0]
        join_receipt(tuple(pair))
        results.append(ReconciledCall(receipt_id, terminal.terminal_state, terminal.error_code))
    return CanaryRunReconciliation(
        run_id=run_id,
        contract_version=contract_version,
        complete=bool(results) and all(item.state == "complete" for item in results),
        results=tuple(results),
    )


__all__ = [
    "CANARY_LOG_NAME",
    "CanaryCallReceipt",
    "CanaryCallReservation",
    "CanaryCallTerminal",
    "CanaryReceiptConflict",
    "CanaryReceiptSink",
    "CanaryRunReconciliation",
    "ExpectedCanaryCall",
    "MemoryCanaryReceiptSink",
    "StructuredLoggingCanaryReceiptSink",
    "build_reservation",
    "build_terminal",
    "join_receipt",
    "next_call_index",
    "reconcile_canary_run",
    "reservation_id_for",
]
