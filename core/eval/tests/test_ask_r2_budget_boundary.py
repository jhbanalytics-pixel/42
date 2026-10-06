import math

import pytest

from core.eval.ask_r2 import BudgetRefused, SessionBudget


def price_for(model):
    if model == "one_dollar_per_token":
        return {"input": 1_000_000, "output": 1_000_000}
    if model == "ninety_nine_cents_per_token":
        return {"input": 990_000, "output": 990_000}
    if model == "two_dollars_and_forty_seven_cents":
        return {"input": 1_000_000, "output": 475_000}
    if model == "half_micro_per_token":
        return {"input": 0.5, "output": 0.5}
    if model == "invalid_nan":
        return {"input": math.nan, "output": 1_000_000}
    if model == "invalid_negative":
        return {"input": 1_000_000, "output": -1}
    raise AssertionError(f"unexpected model: {model}")


def reserve_input(budget, tokens, model="one_dollar_per_token"):
    return budget.reserve(
        phase="research",
        model=model,
        input_bound_tokens=tokens,
        output_reserve_tokens=0,
    )


def assert_refused(call, cause):
    with pytest.raises(BudgetRefused) as error:
        call()
    assert error.value.args[0] == cause


def test_aggregate_settled_spend_refuses_before_next_dispatch():
    budget = SessionBudget(cap_usd=10.0, price_for=price_for)
    dispatched = []

    for _ in range(2):
        model_ticket = budget.reserve(
            phase="research",
            model="two_dollars_and_forty_seven_cents",
            input_bound_tokens=2,
            output_reserve_tokens=1,
        )
        budget.mark_dispatched(model_ticket)
        dispatched.append(model_ticket)
        budget.settle(model_ticket, actual_usd=2.475)

        embedding_ticket = budget.reserve_fixed(
            phase="research_embedding",
            model="embedding-test",
            amount_usd=2.475,
            input_bound_tokens=2,
        )
        budget.mark_dispatched(embedding_ticket)
        dispatched.append(embedding_ticket)
        budget.settle(embedding_ticket, actual_usd=2.475)

    assert budget.charged_usd == pytest.approx(9.9)
    assert_refused(lambda: reserve_input(budget, 1), "session_cap_exhausted")
    assert len(dispatched) == 4


def test_exact_ten_dollars_is_accepted_but_next_microdollar_is_refused():
    budget = SessionBudget(cap_usd=10.0, price_for=price_for)
    ticket = reserve_input(budget, 10)
    budget.mark_dispatched(ticket)
    budget.settle(ticket, actual_usd=10.0)

    assert budget.charged_usd == pytest.approx(10.0)
    assert_refused(lambda: reserve_input(budget, 1, "half_micro_per_token"), "session_cap_exhausted")


def test_outstanding_reservations_share_the_aggregate_cap():
    budget = SessionBudget(cap_usd=10.0, price_for=price_for)
    first = reserve_input(budget, 6)
    second = reserve_input(budget, 4)

    assert first is not second
    assert_refused(lambda: reserve_input(budget, 1, "half_micro_per_token"), "session_cap_exhausted")


def test_known_lower_actual_cost_releases_unused_reservation():
    budget = SessionBudget(cap_usd=10.0, price_for=price_for)
    first = reserve_input(budget, 8)
    budget.mark_dispatched(first)
    budget.settle(first, actual_usd=2.0)

    second = reserve_input(budget, 8)
    budget.mark_dispatched(second)
    budget.settle(second, actual_usd=8.0)

    assert budget.charged_usd == pytest.approx(10.0)
    assert_refused(lambda: reserve_input(budget, 1, "half_micro_per_token"), "session_cap_exhausted")


def test_unknown_dispatched_cost_books_full_ceiling_and_stops_later_calls():
    budget = SessionBudget(cap_usd=10.0, price_for=price_for)
    ticket = reserve_input(budget, 3)
    budget.mark_dispatched(ticket)
    budget.settle(ticket, unknown=True)

    assert budget.charged_usd == pytest.approx(3.0)
    assert budget.stop_reason == "unknown_dispatched_cost"
    assert_refused(lambda: reserve_input(budget, 1), "prior_call_failure")


def test_pre_dispatch_zero_settlement_releases_budget_without_a_charge():
    budget = SessionBudget(cap_usd=10.0, price_for=price_for)
    ticket = reserve_input(budget, 9)
    budget.settle(ticket, actual_usd=0.0)

    assert budget.charged_usd == pytest.approx(0.0)
    assert budget.stop_reason is None
    replacement = reserve_input(budget, 10)
    assert replacement is not None


def test_microdollar_rounding_never_allows_spend_over_the_cap():
    budget = SessionBudget(cap_usd=0.000003, price_for=price_for)
    first = reserve_input(budget, 3, "half_micro_per_token")
    budget.mark_dispatched(first)
    budget.settle(first, actual_usd=0.0000011)

    second = reserve_input(budget, 1, "half_micro_per_token")
    budget.mark_dispatched(second)
    budget.settle(second, actual_usd=0.000001)

    assert budget.charged_usd == pytest.approx(0.000003)
    assert budget.charged_usd <= 0.000003
    assert_refused(lambda: reserve_input(budget, 1, "half_micro_per_token"), "session_cap_exhausted")


@pytest.mark.parametrize("model", ["invalid_nan", "invalid_negative"])
def test_invalid_price_fails_closed_before_reservation(model):
    budget = SessionBudget(cap_usd=10.0, price_for=price_for)

    assert_refused(lambda: reserve_input(budget, 1, model), "model_price_invalid")
    assert budget.charged_usd == pytest.approx(0.0)


@pytest.mark.parametrize("actual_usd", [math.nan, -0.01])
def test_invalid_dispatched_actual_cost_books_reservation_and_stops(actual_usd):
    budget = SessionBudget(cap_usd=10.0, price_for=price_for)
    ticket = reserve_input(budget, 3)
    budget.mark_dispatched(ticket)
    budget.settle(ticket, actual_usd=actual_usd)

    assert budget.charged_usd == pytest.approx(3.0)
    assert budget.stop_reason == "unknown_dispatched_cost"
    assert_refused(lambda: reserve_input(budget, 1), "prior_call_failure")
