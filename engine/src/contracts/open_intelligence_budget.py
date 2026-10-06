"""Per-run token budget for the approved Open Intelligence consumers."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from src.analysis.gemini_client import BriefResponse
    from src.utils.gemini_usage import GeminiUsageEvent

MeteringStatus = Literal["ready", "persisted", "failed"]

_CONSUMER_CEILINGS: dict[str, tuple[int, int]] = {
    "dynamic_signal_summary": (8_000, 800),
    "open_question_answer": (32_000, 4_000),
}
_STAGE_CEILINGS: dict[str, dict[str, tuple[int, int]]] = {
    "dynamic_signal_summary": {"summary": (8_000, 800)},
    "open_question_answer": {
        "planning": (8_000, 800),
        "answering": (32_000, 4_000),
    },
}


class BudgetStateError(RuntimeError):
    """The requested operation is invalid for the current run state."""


class BudgetCeilingError(RuntimeError):
    """A stage or cumulative token ceiling prevents another call."""


class MeteringPersistenceError(RuntimeError):
    """The pending usage event was not acknowledged exactly."""


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    input_ceiling: int
    output_ceiling: int
    input_used: int
    output_used: int
    remaining_max_output_tokens: int
    exhausted: bool
    metering_status: MeteringStatus


@dataclass(frozen=True, slots=True)
class CallPermit:
    stage: str
    call_index: int
    counted_input: int
    max_output_tokens: int


class OpenIntelligenceRunBudget:
    """Enforce one consumer's stage and cumulative token ceilings."""

    def __init__(
        self,
        *,
        run_id: str,
        consumer: str,
        trend_date: datetime.date,
        market: str | None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a nonempty string")
        if not isinstance(consumer, str):
            raise TypeError("consumer must be a string")
        if consumer not in _CONSUMER_CEILINGS:
            raise ValueError(f"unsupported Open Intelligence consumer: {consumer!r}")
        if isinstance(trend_date, datetime.datetime) or not isinstance(trend_date, datetime.date):
            raise TypeError("trend_date must be a date")
        if market is not None and (not isinstance(market, str) or not market):
            raise ValueError("market must be a nonempty string or None")

        self._run_id = run_id
        self._consumer = consumer
        self._trend_date = trend_date
        self._market = market
        self._input_ceiling, self._output_ceiling = _CONSUMER_CEILINGS[consumer]
        self._input_used = 0
        self._output_used = 0
        self._stage_input_used = dict.fromkeys(_STAGE_CEILINGS[consumer], 0)
        self._stage_output_used = dict.fromkeys(_STAGE_CEILINGS[consumer], 0)
        self._stage_call_indexes = dict.fromkeys(_STAGE_CEILINGS[consumer], 0)
        self._exhausted = False
        self._metering_status: MeteringStatus = "ready"
        self._metering_failed = False
        self._pending_permit: CallPermit | None = None
        self._pending_event: GeminiUsageEvent | None = None

    @property
    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            input_ceiling=self._input_ceiling,
            output_ceiling=self._output_ceiling,
            input_used=self._input_used,
            output_used=self._output_used,
            remaining_max_output_tokens=max(0, self._output_ceiling - self._output_used),
            exhausted=self._exhausted,
            metering_status=self._metering_status,
        )

    def prepare_call(
        self,
        stage: str,
        prompt: str,
        count_tokens: Callable[[str], int],
    ) -> CallPermit:
        if self._metering_failed:
            raise MeteringPersistenceError("metering failed; this run cannot make new calls")
        if self._exhausted:
            raise BudgetCeilingError("token budget is exhausted")
        if self._pending_permit is not None:
            raise BudgetStateError("a call permit is already pending")
        stage_ceiling = _STAGE_CEILINGS[self._consumer].get(stage)
        if stage_ceiling is None:
            raise ValueError(f"consumer {self._consumer!r} does not allow stage {stage!r}")
        if not callable(count_tokens):
            raise TypeError("count_tokens must be callable")

        stage_input_ceiling, stage_output_ceiling = stage_ceiling
        remaining_input = min(
            self._input_ceiling - self._input_used,
            stage_input_ceiling - self._stage_input_used[stage],
        )
        remaining_output = min(
            self._output_ceiling - self._output_used,
            stage_output_ceiling - self._stage_output_used[stage],
        )
        if remaining_input < 0 or remaining_output <= 0:
            self._exhausted = True
            raise BudgetCeilingError("stage or cumulative token allowance is exhausted")

        counted_input = count_tokens(prompt)
        self._require_nonnegative_int(counted_input, "counted input")
        if counted_input > remaining_input:
            self._exhausted = True
            raise BudgetCeilingError("counted input exceeds the remaining token allowance")

        permit = CallPermit(
            stage=stage,
            call_index=self._stage_call_indexes[stage],
            counted_input=counted_input,
            max_output_tokens=remaining_output,
        )
        self._pending_permit = permit
        return permit

    def record_response(
        self,
        permit: CallPermit,
        response: BriefResponse,
        persist: Callable[[GeminiUsageEvent], GeminiUsageEvent],
        *,
        recorded_at: datetime.datetime | None = None,
    ) -> GeminiUsageEvent:
        from src.analysis.gemini_client import BriefResponse
        from src.utils.gemini_usage import build_usage_event

        if self._metering_failed:
            raise MeteringPersistenceError("metering failed; this run cannot record a response")
        if permit is not self._pending_permit:
            raise BudgetStateError("record_response requires the active permit object")
        if self._pending_event is not None:
            raise BudgetStateError("the active response already has a pending event")
        try:
            if type(response) is not BriefResponse:
                raise TypeError("response must be an exact BriefResponse")
            self._require_nonnegative_int(response.prompt_tokens, "prompt_tokens")
            self._require_nonnegative_int(response.completion_tokens, "completion_tokens")
            if not isinstance(response.model, str) or not response.model:
                raise ValueError("response model must be a nonempty string")
            if response.usage_metadata_complete is not True:
                raise ValueError("response usage metadata is incomplete")
            event = build_usage_event(
                trend_date=self._trend_date,
                run_id=self._run_id,
                consumer=self._consumer,
                stage=permit.stage,
                call_index=permit.call_index,
                market=self._market,
                gemini_model=response.model,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                recorded_at=(
                    datetime.datetime.now(datetime.UTC) if recorded_at is None else recorded_at
                ),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            self._metering_failed = True
            self._metering_status = "failed"
            raise MeteringPersistenceError(str(exc)) from exc

        self._pending_event = event
        try:
            self._persist_exact(event, persist)
        except Exception as exc:
            self._metering_failed = True
            self._metering_status = "failed"
            if isinstance(exc, MeteringPersistenceError):
                raise
            raise MeteringPersistenceError(
                f"usage event persistence failed for {event.usage_id}"
            ) from exc

        self._acknowledge_event(event, metering_status="persisted")
        return event

    def retry_persistence(
        self,
        persist: Callable[[GeminiUsageEvent], GeminiUsageEvent],
    ) -> GeminiUsageEvent:
        event = self._pending_event
        if not self._metering_failed or event is None:
            raise BudgetStateError("no failed event is pending persistence")
        try:
            self._persist_exact(event, persist)
        except Exception as exc:
            if isinstance(exc, MeteringPersistenceError):
                raise
            raise MeteringPersistenceError(
                f"usage event persistence retry failed for {event.usage_id}"
            ) from exc

        self._acknowledge_event(event, metering_status="failed")
        return event

    @staticmethod
    def _require_nonnegative_int(value: object, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{field} must be an integer")
        if value < 0:
            raise ValueError(f"{field} must be nonnegative")
        return value

    @staticmethod
    def _persist_exact(
        event: GeminiUsageEvent,
        persist: Callable[[GeminiUsageEvent], GeminiUsageEvent],
    ) -> None:
        acknowledged = persist(event)
        if acknowledged != event:
            raise MeteringPersistenceError("persistence did not acknowledge the exact event")

    def _acknowledge_event(
        self,
        event: GeminiUsageEvent,
        *,
        metering_status: MeteringStatus,
    ) -> None:
        stage = event.stage
        self._input_used += event.prompt_tokens
        self._output_used += event.completion_tokens
        self._stage_input_used[stage] += event.prompt_tokens
        self._stage_output_used[stage] += event.completion_tokens
        self._stage_call_indexes[stage] += 1
        stage_input_ceiling, stage_output_ceiling = _STAGE_CEILINGS[self._consumer][stage]
        if (
            self._input_used > self._input_ceiling
            or self._output_used > self._output_ceiling
            or self._stage_input_used[stage] > stage_input_ceiling
            or self._stage_output_used[stage] > stage_output_ceiling
        ):
            self._exhausted = True
        self._metering_status = metering_status
        self._pending_permit = None
        self._pending_event = None


__all__ = [
    "BudgetCeilingError",
    "BudgetSnapshot",
    "BudgetStateError",
    "CallPermit",
    "MeteringPersistenceError",
    "OpenIntelligenceRunBudget",
]
