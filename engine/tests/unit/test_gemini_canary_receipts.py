from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from google.genai import types

NOW = datetime(2026, 8, 28, 8, 0, tzinfo=UTC)


def reservation(**overrides):
    from src.analysis.open_intelligence.canary_receipts import build_reservation

    values = {
        "lane": "canary",
        "consumer": "dynamic_signal_summary",
        "stage": "summary",
        "model": "gemini-3.7-flash",
        "thinking_level": types.ThinkingLevel.MEDIUM,
        "run_id": "canary_run_001",
        "call_index": 0,
        "input_digest": "1" * 64,
        "system_instruction_digest": "2" * 64,
        "response_schema_digest": "3" * 64,
        "sdk_version": "2.20.0",
        "api_version": "v1",
        "pricing_version": "gemini_3_7_intro_2026_v1",
        "reserved_at": NOW,
        "contract_version": "gemini_3_7_canary_v1",
    }
    values.update(overrides)
    return build_reservation(**values)


def terminal(reservation_event, **overrides):
    from src.analysis.open_intelligence.canary_receipts import build_terminal

    values = {
        "reservation": reservation_event,
        "started_at": NOW + timedelta(seconds=1),
        "completed_at": NOW + timedelta(seconds=2),
        "terminal_state": "complete",
        "prompt_token_count": 100,
        "candidates_token_count": 20,
        "thoughts_token_count": 5,
        "total_token_count": 125,
        "estimated_cost_usd": Decimal("0.00016875"),
        "usage_event_id": "usage_" + "4" * 64,
        "error_code": None,
        "completed_contract_version": "gemini_3_7_canary_v1",
    }
    values.update(overrides)
    return build_terminal(**values)


def test_reservation_identity_is_full_canonical_sha_and_immutable() -> None:
    from src.analysis.open_intelligence.canary_receipts import reservation_id_for

    event = reservation()

    assert event.receipt_id == reservation_id_for(event)
    assert len(event.receipt_id) == 64
    assert reservation() == event
    assert replace(event, input_digest="f" * 64).receipt_id == event.receipt_id


def test_sink_requires_verified_reservation_before_terminal_and_refuses_duplicates() -> None:
    from src.analysis.open_intelligence.canary_receipts import (
        CanaryReceiptConflict,
        MemoryCanaryReceiptSink,
    )

    sink = MemoryCanaryReceiptSink()
    reserved = reservation()
    completed = terminal(reserved)

    with pytest.raises(CanaryReceiptConflict):
        sink.complete(completed)
    sink.reserve(reserved)
    assert sink.read(reserved.receipt_id) == (reserved,)
    with pytest.raises(CanaryReceiptConflict):
        sink.reserve(reserved)
    sink.complete(completed)
    assert sink.read(reserved.receipt_id) == (reserved, completed)
    with pytest.raises(CanaryReceiptConflict):
        sink.complete(completed)


def test_structured_sink_writes_metadata_only_and_reads_back_exactly() -> None:
    from src.analysis.open_intelligence.canary_receipts import StructuredLoggingCanaryReceiptSink

    events = []
    sink = StructuredLoggingCanaryReceiptSink(
        write_structured=lambda payload: events.append(payload),
        read_structured=lambda receipt_id: tuple(
            event for event in events if event["receipt_id"] == receipt_id
        ),
        list_structured=lambda run_id, contract_version: tuple(events),
    )
    reserved = reservation()
    completed = terminal(reserved)

    sink.reserve(reserved)
    sink.complete(completed)

    forbidden = {"contents", "prompt", "system_instruction", "response_schema", "evidence"}
    assert all(forbidden.isdisjoint(event) for event in events)
    assert sink.log_name == "open-intelligence-gemini-canary"
    assert sink.read(reserved.receipt_id) == (reserved, completed)


def test_reconciliation_marks_missing_terminal_uncertain_without_appending_one() -> None:
    from src.analysis.open_intelligence.canary_receipts import (
        ExpectedCanaryCall,
        MemoryCanaryReceiptSink,
        reconcile_canary_run,
    )

    sink = MemoryCanaryReceiptSink()
    reserved = reservation()
    sink.reserve(reserved)

    result = reconcile_canary_run(
        reserved.run_id,
        reserved.contract_version,
        (ExpectedCanaryCall(receipt_id=reserved.receipt_id),),
        sink=sink,
    )

    assert result.complete is False
    assert result.results[0].state == "incomplete_uncertain"
    assert result.results[0].error_code == "terminal_missing"
    assert sink.read(reserved.receipt_id) == (reserved,)


def test_reconciliation_accepts_one_pair_and_blocks_orphans() -> None:
    from src.analysis.open_intelligence.canary_receipts import (
        CanaryReceiptConflict,
        ExpectedCanaryCall,
        MemoryCanaryReceiptSink,
        reconcile_canary_run,
    )

    sink = MemoryCanaryReceiptSink()
    reserved = reservation()
    sink.reserve(reserved)
    sink.complete(terminal(reserved))
    expected = (ExpectedCanaryCall(receipt_id=reserved.receipt_id),)

    assert (
        reconcile_canary_run(
            reserved.run_id, reserved.contract_version, expected, sink=sink
        ).complete
        is True
    )

    orphan = reservation(run_id=reserved.run_id, call_index=1)
    sink.reserve(orphan)
    with pytest.raises(CanaryReceiptConflict):
        reconcile_canary_run(reserved.run_id, reserved.contract_version, expected, sink=sink)


def test_joined_receipt_requires_one_matching_reservation_and_terminal() -> None:
    from src.analysis.open_intelligence.canary_receipts import join_receipt

    reserved = reservation()
    completed = terminal(reserved)

    receipt = join_receipt((reserved, completed))

    assert receipt.reservation == reserved
    assert receipt.terminal == completed
    assert receipt.receipt_id == reserved.receipt_id


def test_terminal_contract_version_must_match_the_reservation() -> None:
    with pytest.raises(ValueError, match="contract"):
        terminal(reservation(), completed_contract_version="wrong_contract")


def test_joined_receipt_revalidates_structured_terminal_content() -> None:
    from src.analysis.open_intelligence.canary_receipts import (
        CanaryReceiptConflict,
        join_receipt,
    )

    reserved = reservation()
    completed = terminal(reserved)

    with pytest.raises(CanaryReceiptConflict):
        join_receipt((reserved, replace(completed, total_token_count=124)))
    with pytest.raises(CanaryReceiptConflict):
        join_receipt((reserved, replace(completed, usage_event_id="")))


@pytest.mark.parametrize("field", ["thoughts_token_count", "total_token_count", "usage_event_id"])
def test_complete_terminal_requires_complete_usage_metadata(field) -> None:
    reserved = reservation()

    with pytest.raises(ValueError):
        terminal(reserved, **{field: None})
