"""Unit tests for the Open Intelligence per-run token budget."""

from __future__ import annotations

import datetime
from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import MagicMock

import pytest
from src.analysis.gemini_client import BriefResponse, GeminiClient
from src.contracts.open_intelligence_budget import (
    BudgetCeilingError,
    BudgetSnapshot,
    BudgetStateError,
    CallPermit,
    MeteringPersistenceError,
    OpenIntelligenceRunBudget,
)


def _budget(consumer: str = "open_question_answer") -> OpenIntelligenceRunBudget:
    return OpenIntelligenceRunBudget(
        run_id="question_run_001",
        consumer=consumer,
        trend_date=datetime.date(2026, 8, 25),
        market="za",
    )


def _response(
    *,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    model: object = "gemini-3.5-flash",
) -> BriefResponse:
    return BriefResponse(
        parsed={"answer": "grounded"},
        raw_text='{"answer":"grounded"}',
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=model,  # type: ignore[arg-type]
        usage_metadata_complete=True,
    )


def _acknowledge(events: list[Any]):
    def persist(event):
        events.append(event)
        return event

    return persist


def test_known_response_recovery_preserves_immutable_usage_and_remaining_budget():
    recorded_at = datetime.datetime(2026, 9, 6, 10, 0, tzinfo=datetime.UTC)
    stored = {}

    def persist(event):
        if event.usage_id in stored and stored[event.usage_id] != event:
            raise ValueError("immutable usage conflict")
        stored[event.usage_id] = event
        return stored[event.usage_id]

    results = []
    for _ in range(2):
        budget = _budget()
        permit = budget.prepare_call("planning", "captured input", lambda _prompt: 500)
        results.append(
            budget.record_response(
                permit,
                _response(prompt_tokens=500, completion_tokens=100),
                persist,
                recorded_at=recorded_at,
            )
        )
        answering = budget.prepare_call("answering", "next stage", lambda _prompt: 31500)
        assert answering.max_output_tokens == 3900
        assert budget.snapshot.input_used == 500
        assert budget.snapshot.output_used == 100
    assert len(stored) == 1
    assert results[0] == results[1]
    assert results[0].recorded_at == recorded_at

    later = _budget()
    permit = later.prepare_call("planning", "captured input", lambda _prompt: 500)
    with pytest.raises(MeteringPersistenceError):
        later.record_response(
            permit,
            _response(prompt_tokens=500, completion_tokens=100),
            persist,
            recorded_at=recorded_at + datetime.timedelta(seconds=1),
        )
    assert len(stored) == 1
    assert next(iter(stored.values())) == results[0]


@pytest.mark.parametrize(
    "recorded_at",
    [
        "2026-09-06T10:00:00Z",
        True,
        datetime.datetime(2026, 9, 6, 10, 0),
        datetime.datetime.min.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=2))),
    ],
    ids=["text", "boolean", "naive", "utc_underflow"],
)
def test_invalid_recovered_usage_timestamp_blocks_persistence_and_new_calls(recorded_at):
    budget = _budget()
    permit = budget.prepare_call("planning", "input", lambda _prompt: 100)
    stored = []
    with pytest.raises(MeteringPersistenceError):
        budget.record_response(permit, _response(), _acknowledge(stored), recorded_at=recorded_at)
    assert stored == []
    assert budget.snapshot.metering_status == "failed"
    with pytest.raises(MeteringPersistenceError):
        budget.prepare_call("answering", "no new call", lambda _prompt: 100)


def test_initial_snapshot_has_exact_approved_fields_and_summary_ceilings():
    budget = _budget("dynamic_signal_summary")

    assert budget.snapshot == BudgetSnapshot(
        input_ceiling=8_000,
        output_ceiling=800,
        input_used=0,
        output_used=0,
        remaining_max_output_tokens=800,
        exhausted=False,
        metering_status="ready",
    )
    assert tuple(BudgetSnapshot.__dataclass_fields__) == (
        "input_ceiling",
        "output_ceiling",
        "input_used",
        "output_used",
        "remaining_max_output_tokens",
        "exhausted",
        "metering_status",
    )
    with pytest.raises(FrozenInstanceError):
        budget.snapshot.input_used = 1  # type: ignore[misc]


def test_question_budget_uses_approved_cumulative_ceilings():
    assert _budget().snapshot == BudgetSnapshot(
        input_ceiling=32_000,
        output_ceiling=4_000,
        input_used=0,
        output_used=0,
        remaining_max_output_tokens=4_000,
        exhausted=False,
        metering_status="ready",
    )


@pytest.mark.parametrize("consumer", ["", "daily_summary", True, None])
def test_budget_rejects_any_consumer_outside_the_two_approved_new_consumers(consumer):
    with pytest.raises((TypeError, ValueError)):
        _budget(consumer)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("consumer", "stage", "want_output"),
    [
        ("dynamic_signal_summary", "summary", 800),
        ("open_question_answer", "planning", 800),
        ("open_question_answer", "answering", 4_000),
    ],
)
def test_prepare_call_applies_exact_stage_output_caps_and_counts_exact_prompt_once(
    consumer, stage, want_output
):
    seen: list[str] = []

    def count_tokens(prompt: str) -> int:
        seen.append(prompt)
        return 321

    permit = _budget(consumer).prepare_call(stage, "exact prompt", count_tokens)

    assert permit == CallPermit(
        stage=stage,
        call_index=0,
        counted_input=321,
        max_output_tokens=want_output,
    )
    assert seen == ["exact prompt"]
    with pytest.raises(FrozenInstanceError):
        permit.counted_input = 1  # type: ignore[misc]


def test_call_permit_has_exact_approved_field_order():
    assert tuple(CallPermit.__dataclass_fields__) == (
        "stage",
        "call_index",
        "counted_input",
        "max_output_tokens",
    )


@pytest.mark.parametrize(
    ("consumer", "stage"),
    [
        ("dynamic_signal_summary", "planning"),
        ("open_question_answer", "summary"),
        ("open_question_answer", "drafting"),
    ],
)
def test_invalid_stage_fails_before_count_boundary(consumer, stage):
    calls = 0

    def count_tokens(_prompt: str) -> int:
        nonlocal calls
        calls += 1
        return 1

    with pytest.raises(ValueError, match="stage"):
        _budget(consumer).prepare_call(stage, "prompt", count_tokens)
    assert calls == 0


@pytest.mark.parametrize("bad_total", [None, True, -1, 1.5, "1"])
def test_prepare_call_rejects_malformed_count_result_after_one_boundary_call(bad_total):
    calls = 0

    def count_tokens(_prompt: str):
        nonlocal calls
        calls += 1
        return bad_total

    budget = _budget()
    with pytest.raises((TypeError, ValueError), match="counted input"):
        budget.prepare_call("planning", "prompt", count_tokens)

    assert calls == 1
    assert budget.snapshot.exhausted is False
    assert budget.snapshot.metering_status == "ready"


@pytest.mark.parametrize(
    ("consumer", "stage", "counted"),
    [
        ("dynamic_signal_summary", "summary", 8_001),
        ("open_question_answer", "planning", 8_001),
        ("open_question_answer", "answering", 32_001),
    ],
)
def test_estimated_input_above_smaller_stage_or_cumulative_allowance_is_a_ceiling_stop(
    consumer, stage, counted
):
    calls = 0

    def count_tokens(_prompt: str) -> int:
        nonlocal calls
        calls += 1
        return counted

    budget = _budget(consumer)
    with pytest.raises(BudgetCeilingError):
        budget.prepare_call(stage, "prompt", count_tokens)

    assert calls == 1
    assert budget.snapshot.exhausted is True
    assert budget.snapshot.metering_status == "ready"


def test_second_prepare_is_locked_before_count_boundary_until_pending_call_resolves():
    budget = _budget()
    budget.prepare_call("planning", "first", lambda _prompt: 100)
    calls = 0

    def count_tokens(_prompt: str) -> int:
        nonlocal calls
        calls += 1
        return 100

    with pytest.raises(BudgetStateError, match="pending"):
        budget.prepare_call("planning", "second", count_tokens)
    assert calls == 0


def test_persisted_actual_counters_not_preflight_estimate_govern_cumulative_remainder():
    budget = _budget()
    events: list[Any] = []
    permit = budget.prepare_call("planning", "large estimate", lambda _prompt: 7_000)

    event = budget.record_response(
        permit,
        _response(prompt_tokens=500, completion_tokens=100),
        _acknowledge(events),
    )
    answer_permit = budget.prepare_call("answering", "answer", lambda _prompt: 31_500)

    assert event is events[0]
    assert event.prompt_tokens == 500
    assert event.completion_tokens == 100
    assert budget.snapshot.input_used == 500
    assert budget.snapshot.output_used == 100
    assert budget.snapshot.remaining_max_output_tokens == 3_900
    assert answer_permit.counted_input == 31_500
    assert answer_permit.max_output_tokens == 3_900


def test_answering_receives_unused_cumulative_output_after_full_planning_allowance():
    budget = _budget()
    permit = budget.prepare_call("planning", "plan", lambda _prompt: 8_000)
    budget.record_response(
        permit,
        _response(prompt_tokens=8_000, completion_tokens=800),
        _acknowledge([]),
    )

    answer_permit = budget.prepare_call("answering", "answer", lambda _prompt: 24_000)

    assert answer_permit.max_output_tokens == 3_200


def test_stage_local_call_indexes_increment_only_the_acknowledged_stage():
    budget = _budget()
    persist = _acknowledge([])

    planning_zero = budget.prepare_call("planning", "p0", lambda _prompt: 1)
    budget.record_response(planning_zero, _response(prompt_tokens=1), persist)
    answering_zero = budget.prepare_call("answering", "a0", lambda _prompt: 1)
    budget.record_response(answering_zero, _response(prompt_tokens=1), persist)
    planning_one = budget.prepare_call("planning", "p1", lambda _prompt: 1)

    assert planning_zero.call_index == 0
    assert answering_zero.call_index == 0
    assert planning_one.call_index == 1


def test_persistence_acknowledgement_sets_status_and_permits_next_call():
    budget = _budget("dynamic_signal_summary")
    permit = budget.prepare_call("summary", "first", lambda _prompt: 100)

    budget.record_response(permit, _response(), _acknowledge([]))
    next_permit = budget.prepare_call("summary", "second", lambda _prompt: 100)

    assert budget.snapshot.metering_status == "persisted"
    assert next_permit.call_index == 1


def test_actual_stage_overage_is_persisted_then_exhausts_the_run():
    budget = _budget()
    events: list[Any] = []
    permit = budget.prepare_call("planning", "plan", lambda _prompt: 100)

    budget.record_response(
        permit,
        _response(prompt_tokens=8_001, completion_tokens=801),
        _acknowledge(events),
    )
    calls = 0

    def count_tokens(_prompt: str) -> int:
        nonlocal calls
        calls += 1
        return 1

    with pytest.raises(BudgetCeilingError):
        budget.prepare_call("answering", "answer", count_tokens)

    assert len(events) == 1
    assert budget.snapshot.input_used == 8_001
    assert budget.snapshot.output_used == 801
    assert budget.snapshot.exhausted is True
    assert budget.snapshot.metering_status == "persisted"
    assert calls == 0


def test_actual_cumulative_overage_uses_actual_counts_and_clamps_output_remainder():
    budget = _budget()
    permit = budget.prepare_call("answering", "answer", lambda _prompt: 100)

    budget.record_response(
        permit,
        _response(prompt_tokens=32_001, completion_tokens=4_001),
        _acknowledge([]),
    )

    assert budget.snapshot.input_used == 32_001
    assert budget.snapshot.output_used == 4_001
    assert budget.snapshot.remaining_max_output_tokens == 0
    assert budget.snapshot.exhausted is True


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("prompt_tokens", True),
        ("prompt_tokens", -1),
        ("completion_tokens", True),
        ("completion_tokens", -1),
    ],
)
def test_record_response_rejects_invalid_actual_counters_without_resolving_permit(field, bad_value):
    budget = _budget()
    permit = budget.prepare_call("planning", "plan", lambda _prompt: 100)
    values = {"prompt_tokens": 100, "completion_tokens": 50}
    values[field] = bad_value

    with pytest.raises(MeteringPersistenceError, match=field):
        budget.record_response(permit, _response(**values), _acknowledge([]))
    assert budget.snapshot.metering_status == "failed"
    assert budget.snapshot.exhausted is False
    with pytest.raises(MeteringPersistenceError):
        budget.prepare_call("planning", "next", lambda _prompt: 1)


def test_record_response_requires_exact_brief_response_type():
    budget = _budget()
    permit = budget.prepare_call("planning", "plan", lambda _prompt: 100)

    class ResponseLookalike:
        prompt_tokens = 100
        completion_tokens = 50
        model = "gemini-3.5-flash"

    with pytest.raises(MeteringPersistenceError, match="BriefResponse"):
        budget.record_response(permit, ResponseLookalike(), _acknowledge([]))
    assert budget.snapshot.metering_status == "failed"
    assert budget.snapshot.exhausted is False


@pytest.mark.parametrize("model", ["", None, True, 123])
def test_invalid_response_model_is_a_permanent_metering_stop_without_persistence(model):
    budget = _budget()
    permit = budget.prepare_call("planning", "plan", lambda _prompt: 100)
    persisted: list[Any] = []

    with pytest.raises(MeteringPersistenceError, match="response model"):
        budget.record_response(permit, _response(model=model), _acknowledge(persisted))

    count_calls = 0

    def count_tokens(_prompt: str) -> int:
        nonlocal count_calls
        count_calls += 1
        return 1

    with pytest.raises(MeteringPersistenceError):
        budget.prepare_call("planning", "next", count_tokens)

    assert persisted == []
    assert budget.snapshot.input_used == 0
    assert budget.snapshot.output_used == 0
    assert budget.snapshot.metering_status == "failed"
    assert budget.snapshot.exhausted is False
    assert budget._pending_permit is permit
    assert budget._pending_event is None
    assert budget._stage_call_indexes["planning"] == 0
    assert count_calls == 0
    with pytest.raises(MeteringPersistenceError):
        budget.record_response(permit, _response(), _acknowledge(persisted))
    assert persisted == []


def test_real_generation_with_missing_usage_metadata_is_a_permanent_metering_stop(
    monkeypatch,
):
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    fake_sdk = MagicMock()
    generated = MagicMock()
    generated.text = "{}"
    generated.parsed = {}
    generated.usage_metadata = None
    fake_sdk.models.generate_content.return_value = generated
    response = GeminiClient(client=fake_sdk).generate_brief(
        prompt="model prompt",
        response_schema={"type": "OBJECT"},
    )
    budget = _budget()
    permit = budget.prepare_call("planning", "model prompt", lambda _prompt: 2)
    persisted: list[Any] = []

    with pytest.raises(MeteringPersistenceError, match="usage metadata"):
        budget.record_response(permit, response, _acknowledge(persisted))

    count_calls = 0

    def count_tokens(_prompt: str) -> int:
        nonlocal count_calls
        count_calls += 1
        return 1

    with pytest.raises(MeteringPersistenceError):
        budget.prepare_call("planning", "must not run", count_tokens)

    assert response.prompt_tokens == 0
    assert response.completion_tokens == 0
    assert response.usage_metadata_complete is False
    assert persisted == []
    assert budget.snapshot.input_used == 0
    assert budget.snapshot.output_used == 0
    assert budget.snapshot.metering_status == "failed"
    assert budget.snapshot.exhausted is False
    assert budget._pending_permit is permit
    assert budget._pending_event is None
    assert budget._stage_call_indexes["planning"] == 0
    assert count_calls == 0
    with pytest.raises(MeteringPersistenceError):
        budget.record_response(permit, _response(), _acknowledge(persisted))
    assert persisted == []


def test_record_response_accepts_only_the_active_permit_object():
    budget = _budget()
    active = budget.prepare_call("planning", "plan", lambda _prompt: 100)
    equal_but_inactive = CallPermit(
        stage=active.stage,
        call_index=active.call_index,
        counted_input=active.counted_input,
        max_output_tokens=active.max_output_tokens,
    )

    with pytest.raises(BudgetStateError, match="active permit"):
        budget.record_response(equal_but_inactive, _response(), _acknowledge([]))


def test_persistence_failure_retains_same_event_and_permanently_stops_new_calls():
    budget = _budget()
    permit = budget.prepare_call("planning", "plan", lambda _prompt: 100)
    attempted: list[Any] = []

    def fail(event):
        attempted.append(event)
        raise RuntimeError("store unavailable")

    with pytest.raises(MeteringPersistenceError):
        budget.record_response(permit, _response(), fail)

    calls = 0

    def count_tokens(_prompt: str) -> int:
        nonlocal calls
        calls += 1
        return 1

    with pytest.raises(MeteringPersistenceError):
        budget.prepare_call("answering", "must not count or generate", count_tokens)

    assert len(attempted) == 1
    assert budget.snapshot.input_used == 0
    assert budget.snapshot.output_used == 0
    assert budget.snapshot.metering_status == "failed"
    assert budget.snapshot.exhausted is False
    assert calls == 0


def test_retry_persistence_reuses_same_immutable_event_and_usage_id_without_reopening():
    budget = _budget()
    permit = budget.prepare_call("planning", "plan", lambda _prompt: 100)
    attempts: list[Any] = []

    def fail(event):
        attempts.append(event)
        raise RuntimeError("ambiguous write")

    with pytest.raises(MeteringPersistenceError):
        budget.record_response(permit, _response(prompt_tokens=120, completion_tokens=60), fail)

    def succeed(event):
        attempts.append(event)
        return event

    acknowledged = budget.retry_persistence(succeed)
    calls = 0

    def count_tokens(_prompt: str) -> int:
        nonlocal calls
        calls += 1
        return 1

    with pytest.raises(MeteringPersistenceError):
        budget.prepare_call("answering", "must stay disabled", count_tokens)

    assert attempts[0] is attempts[1]
    assert attempts[0].usage_id == attempts[1].usage_id
    assert acknowledged is attempts[0]
    assert budget.snapshot.input_used == 120
    assert budget.snapshot.output_used == 60
    assert budget.snapshot.metering_status == "failed"
    assert budget.snapshot.exhausted is False
    assert calls == 0


def test_retry_persistence_failure_keeps_same_event_available_for_later_retry():
    budget = _budget()
    permit = budget.prepare_call("planning", "plan", lambda _prompt: 100)
    attempts: list[Any] = []

    def fail(event):
        attempts.append(event)
        raise RuntimeError("still unavailable")

    with pytest.raises(MeteringPersistenceError):
        budget.record_response(permit, _response(), fail)
    with pytest.raises(MeteringPersistenceError):
        budget.retry_persistence(fail)
    with pytest.raises(MeteringPersistenceError):
        budget.retry_persistence(fail)

    assert len(attempts) == 3
    assert attempts[0] is attempts[1] is attempts[2]


def test_mismatched_acknowledgement_retries_same_event_and_applies_usage_once():
    budget = _budget()
    permit = budget.prepare_call("planning", "plan", lambda _prompt: 100)
    attempts: list[Any] = []

    def mismatch(event):
        attempts.append(event)
        return None

    with pytest.raises(MeteringPersistenceError, match="acknowledge"):
        budget.record_response(
            permit,
            _response(prompt_tokens=120, completion_tokens=60),
            mismatch,
        )

    assert budget.snapshot.input_used == 0
    assert budget.snapshot.output_used == 0

    def exact(event):
        attempts.append(event)
        return event

    acknowledged = budget.retry_persistence(exact)

    assert attempts[0] is attempts[1]
    assert attempts[0].usage_id == attempts[1].usage_id
    assert acknowledged is attempts[0]
    assert budget.snapshot.input_used == 120
    assert budget.snapshot.output_used == 60
    assert budget.snapshot.metering_status == "failed"
    assert budget.snapshot.exhausted is False
    with pytest.raises(MeteringPersistenceError):
        budget.prepare_call("answering", "must stay disabled", lambda _prompt: 1)
    with pytest.raises(BudgetStateError, match="failed event"):
        budget.retry_persistence(exact)
    assert len(attempts) == 2


def test_retry_persistence_is_available_only_for_failed_pending_event():
    budget = _budget()

    with pytest.raises(BudgetStateError, match="failed event"):
        budget.retry_persistence(lambda event: event)
