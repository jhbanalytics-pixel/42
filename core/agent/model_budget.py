from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from core.llm.provider import price_for, reserve_output

MICROS = 1_000_000


class BudgetRefused(RuntimeError):
    before_dispatch = True


@dataclass(frozen=True)
class _Reservation:
    key: object
    ceiling_micros: int
    input_bound: int
    output_bound: int
    research: bool


def _micros(value) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise BudgetRefused("model_price_invalid") from None
    if not amount.is_finite() or amount < 0:
        raise BudgetRefused("model_price_invalid")
    return int((amount * MICROS).to_integral_value(rounding=ROUND_CEILING))


def _count(value, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise BudgetRefused("model_reservation_invalid")
    return value


class AskModelBudget:
    def __init__(self, hold_usd, research_usd, *, price_for_fn=None, reserve_output_fn=None):
        _micros(hold_usd)
        _micros(research_usd)
        self.cap_micros = int(Decimal(str(hold_usd)) * MICROS)
        self.research_cap_micros = int(Decimal(str(research_usd)) * MICROS)
        if self.cap_micros <= 0 or self.research_cap_micros <= 0:
            raise BudgetRefused("model_budget_invalid")
        if self.research_cap_micros > self.cap_micros:
            raise BudgetRefused("research_budget_above_question_hold")
        self._price_for = price_for_fn or price_for
        self._reserve_output = reserve_output_fn or reserve_output
        self._lock = threading.Lock()
        self._next_key = 0
        self._pending: dict[object, _Reservation] = {}
        self._booked_micros = 0
        self._conservative_micros = 0
        self._research_booked_micros = 0
        self._in_flight_micros = 0
        self._research_in_flight_micros = 0
        self._stopped = False
        self._stop_reason = None
        self._research_exhausted = False
        self._research_stop_reason = None
        self._bound_exceeded = False
        self._ceiling_exceeded = False

    @property
    def booked_usd(self) -> float:
        with self._lock:
            return float((self._booked_micros + self._in_flight_micros) / MICROS)

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped

    @property
    def conservative_usd(self) -> float:
        with self._lock:
            return self._conservative_micros / MICROS

    @property
    def bound_exceeded(self) -> bool:
        with self._lock:
            return self._bound_exceeded

    @property
    def ceiling_exceeded(self) -> bool:
        with self._lock:
            return self._ceiling_exceeded

    @property
    def stop_reason(self) -> str | None:
        with self._lock:
            return self._stop_reason

    @property
    def research_exhausted(self) -> bool:
        with self._lock:
            return self._research_exhausted

    @property
    def research_stop_reason(self) -> str | None:
        with self._lock:
            return self._research_stop_reason

    def _refuse(self, reason: str) -> BudgetRefused:
        with self._lock:
            self._stopped = True
            self._stop_reason = reason
        return BudgetRefused(reason)

    def _ceiling(self, model: str, input_bound: int, max_output_tokens: int) -> tuple[int, int, int]:
        """A call's reserve in micros with its input and output bounds, or BudgetRefused naming why it has none."""
        try:
            input_bound = _count(input_bound, positive=True)
            max_output_tokens = _count(max_output_tokens, positive=True)
            output_bound = self._reserve_output(model, max_output_tokens)
            output_bound = _count(output_bound, positive=True)
            if output_bound < max_output_tokens:
                raise BudgetRefused("model_reservation_invalid")
            price = self._price_for(model)
            input_rate = Decimal(str(price["input"]))
            output_rate = Decimal(str(price["output"]))
            if (not input_rate.is_finite() or not output_rate.is_finite()
                    or input_rate < 0 or output_rate < 0):
                raise BudgetRefused("model_price_invalid")
            reserve_usd = (Decimal(input_bound) * input_rate + Decimal(output_bound) * output_rate) / MICROS
            ceiling_micros = _micros(reserve_usd)
        except BudgetRefused:
            raise
        except (InvalidOperation, KeyError, TypeError, ValueError, OverflowError):
            raise BudgetRefused("model_price_invalid") from None
        except Exception:
            raise BudgetRefused("model_price_unavailable") from None
        if ceiling_micros <= 0:
            raise BudgetRefused("model_price_invalid")
        return ceiling_micros, input_bound, output_bound

    def affords(self, model: str, calls) -> bool:
        """Whether calls, (input_bound, max_output_tokens) each, would all fit under the question's hold now. It
        reserves nothing and, unlike a refused reserve, never stops later calls: for an optional call, which the
        question goes on without (ask.py's short answer rewrite)."""
        if not isinstance(model, str) or not model:
            return False
        try:
            need = sum(self._ceiling(model, input_bound, max_output)[0] for input_bound, max_output in calls)
        except (BudgetRefused, TypeError, ValueError):
            return False
        with self._lock:
            return not self._stopped and self._booked_micros + self._in_flight_micros + need <= self.cap_micros

    def reserve(self, model: str, input_bound: int, max_output_tokens: int, *, research: bool = False):
        if not isinstance(model, str) or not model or type(research) is not bool:
            raise self._refuse("model_reservation_invalid")
        try:
            ceiling_micros, input_bound, output_bound = self._ceiling(model, input_bound, max_output_tokens)
        except BudgetRefused as exc:
            raise self._refuse(str(exc)) from None
        return self._reserve(ceiling_micros, input_bound, output_bound, research)

    def reserve_cost(self, ceiling_usd, *, research: bool = False):
        ceiling_micros = _micros(ceiling_usd)
        if ceiling_micros <= 0 or type(research) is not bool:
            raise self._refuse("model_reservation_invalid")
        return self._reserve(ceiling_micros, 0, 0, research)

    def _reserve(self, ceiling_micros, input_bound, output_bound, research):
        with self._lock:
            if self._stopped:
                raise BudgetRefused(self._stop_reason or "prior_model_call_stopped")
            if research and self._research_exhausted:
                raise BudgetRefused(self._research_stop_reason or "research_model_budget_exhausted")
            total = self._booked_micros + self._in_flight_micros + ceiling_micros
            research_total = self._research_booked_micros + self._research_in_flight_micros + ceiling_micros
            if total > self.cap_micros:
                self._stopped = True
                self._stop_reason = "question_model_budget_exhausted"
                raise BudgetRefused("question_model_budget_exhausted")
            if research and research_total > self.research_cap_micros:
                self._research_exhausted = True
                self._research_stop_reason = "research_model_budget_exhausted"
                raise BudgetRefused("research_model_budget_exhausted")
            key = self._next_key
            self._next_key += 1
            reservation = _Reservation(key, ceiling_micros, input_bound, output_bound, research)
            self._pending[key] = reservation
            self._in_flight_micros += ceiling_micros
            if research:
                self._research_in_flight_micros += ceiling_micros
            return reservation

    def settle_cost(self, reservation: _Reservation, usd=None) -> float:
        if reservation.input_bound or reservation.output_bound:
            raise self._refuse("model_reservation_invalid")
        if usd is not None:
            _micros(usd)
        billed_micros = reservation.ceiling_micros if usd is None else Decimal(str(usd)) * MICROS
        with self._lock:
            self._take(reservation)
            self._booked_micros += billed_micros
            if reservation.research:
                self._research_booked_micros += billed_micros
            if usd is None:
                self._conservative_micros += reservation.ceiling_micros
            overrun = billed_micros > reservation.ceiling_micros
            over_cap = (self._booked_micros + self._in_flight_micros > self.cap_micros
                        or reservation.research and self._research_booked_micros
                        + self._research_in_flight_micros > self.research_cap_micros)
            if overrun or over_cap:
                self._stopped = True
                self._ceiling_exceeded = self._ceiling_exceeded or overrun
                self._stop_reason = "ceiling_exceeded" if overrun else "question_model_budget_exceeded"
            return float(billed_micros / MICROS)

    def settle(self, reservation: _Reservation, usage, *, stop_unknown: bool = True) -> bool:
        """Book a finished call. Unknown usage books the full reserve and stops later calls, unless stop_unknown is
        False: a call Google failed with a 5xx books its full reserve the same way, but one retry may still run within
        the cap, and a call that succeeded with an incomplete usage report books its full reserve and later calls
        still run within the cap. Returns True when later calls may run on this call's account: its usage is known (or
        unknown with stop_unknown False, so booked at the full reserve) and within its reserve and the caps."""
        with self._lock:
            self._take(reservation)
            known = self._known_usage(usage)
            actual_micros = _micros(usage["usd"]) if known else reservation.ceiling_micros
            bound_exceeded = (known and (usage["input_tokens"] > reservation.input_bound
                                         or usage["output_tokens"] > reservation.output_bound))
            billed_micros = (max(actual_micros, reservation.ceiling_micros) if bound_exceeded
                             else actual_micros)
            self._booked_micros += billed_micros
            if not known:
                self._conservative_micros += reservation.ceiling_micros
            if bound_exceeded:
                self._bound_exceeded = True
            if billed_micros > reservation.ceiling_micros:
                self._ceiling_exceeded = True
            if reservation.research:
                self._research_booked_micros += billed_micros
            overrun = billed_micros > reservation.ceiling_micros
            over_cap = (self._booked_micros + self._in_flight_micros > self.cap_micros
                        or reservation.research and self._research_booked_micros
                        + self._research_in_flight_micros > self.research_cap_micros)
            unknown = not known and stop_unknown
            if unknown or bound_exceeded or overrun or over_cap:
                self._stopped = True
                self._stop_reason = ("usage_unknown" if unknown else "bound_exceeded" if bound_exceeded else
                                     "ceiling_exceeded" if overrun else "question_model_budget_exceeded")
            return (known or not stop_unknown) and not bound_exceeded and not overrun and not over_cap

    def fail(self, reservation: _Reservation, *, stop: bool = True) -> float:
        self.settle(reservation, None, stop_unknown=stop)
        return reservation.ceiling_micros / MICROS

    def release(self, reservation: _Reservation) -> None:
        """Give back the whole reserve of a call the provider refused before running it (a 429 for capacity, which
        bills nothing): nothing is booked and later calls may reserve again. Only for a refusal; any call that may have
        run settles or fails instead. An unknown reservation stops later calls, as in settle."""
        with self._lock:
            self._take(reservation)

    def _take(self, reservation: _Reservation) -> None:
        """Remove a pending reservation from the in-flight totals; the caller holds the lock."""
        pending = self._pending.pop(getattr(reservation, "key", None), None)
        if pending != reservation:
            self._stopped = True
            self._stop_reason = "model_reservation_unknown"
            raise BudgetRefused("model_reservation_unknown")
        self._in_flight_micros -= reservation.ceiling_micros
        if reservation.research:
            self._research_in_flight_micros -= reservation.ceiling_micros

    @staticmethod
    def _known_usage(usage) -> bool:
        if not isinstance(usage, dict):
            return False
        input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        billed = usage.get("usd")
        if (type(input_tokens) is not int or input_tokens <= 0
                or type(output_tokens) is not int or output_tokens <= 0):
            return False
        try:
            amount = Decimal(str(billed))
        except (InvalidOperation, TypeError, ValueError):
            return False
        try:
            return amount.is_finite() and amount > 0 and math.isfinite(float(amount))
        except (OverflowError, ValueError):
            return False
