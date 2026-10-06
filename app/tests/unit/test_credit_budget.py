from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.api import credit_budget


NOW = datetime(2026, 8, 30, 7, 0, tzinfo=UTC)


def ledger_row(
    debit: str,
    *,
    lane: str = "ogilvy_funded",
    phase: str = "discover",
    event_type: str = "phase_close",
    recorded_at: datetime = NOW,
    attribution_state: str = "complete",
) -> dict[str, object]:
    return {
        "credential_lane": lane,
        "phase": phase,
        "event_type": event_type,
        "recorded_at": recorded_at,
        "budget_debit_credits": Decimal(debit),
        "attribution_state": attribution_state,
    }


def authority(
    *,
    current_balance: str | None = "250100",
    month_opening_balance: str | None = "250100",
    activation_stage: int | None = 0,
    kill_state: str = "not_tested",
    rows: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "credential_lane": "ogilvy_funded",
        "funding_account": "ogilvy_albert",
        "activation_stage": activation_stage,
        "opening_balance": Decimal("250100"),
        "current_balance": None
        if current_balance is None
        else Decimal(current_balance),
        "month_opening_balance": None
        if month_opening_balance is None
        else Decimal(month_opening_balance),
        "balance_observed_at": NOW if current_balance is not None else None,
        "kill_state": kill_state,
        "ledger_rows": list(rows or []),
    }


def funded_lane(reading: dict[str, object]) -> dict[str, object]:
    return reading["lanes"][0]


def test_lane_labels_are_not_swapped():
    reading = credit_budget.project_credit_budget(authority(), now=NOW)

    assert [
        (lane["credential_lane"], lane["funding_account"])
        for lane in reading["lanes"]
    ] == [("ogilvy_funded", "ogilvy_albert"), ("jhb_core", "jhb_analytics")]


def test_public_projection_rejects_secret_shaped_fields():
    reading = credit_budget.project_credit_budget(authority(), now=NOW)

    assert "api_key" not in reading


def test_exact_credit_budget_v2_projection_and_field_order():
    reading = credit_budget.project_credit_budget(authority(), now=NOW)

    assert tuple(reading) == (
        "contract_version",
        "state",
        "month_start",
        "unit",
        "lanes",
        "limitation",
    )
    assert reading == {
        "contract_version": "credit_budget_v2",
        "state": "blocked",
        "month_start": "2026-08-01",
        "unit": "vendor_credits",
        "lanes": [
            {
                "credential_lane": "ogilvy_funded",
                "funding_account": "ogilvy_albert",
                "activation_stage": 0,
                "opening_balance": Decimal("250100"),
                "current_balance": Decimal("250100"),
                "month_opening_balance": Decimal("250100"),
                "monthly_cap": Decimal("25000"),
                "monthly_ledger_debit": Decimal("0"),
                "monthly_balance_delta": Decimal("0"),
                "monthly_effective_spend": Decimal("0"),
                "monthly_remaining": Decimal("25000"),
                "reserve_floor": Decimal("225000"),
                "reserve_remaining": Decimal("25100"),
                "stage_cap": Decimal("0"),
                "run_allowance": Decimal("0"),
                "attribution_state": "complete",
                "kill_state": "not_tested",
                "ledger_through": "2026-08-30T07:00:00Z",
                "limitation": "Paid calls remain disabled until the approved activation gate passes.",
            },
            {
                "credential_lane": "jhb_core",
                "funding_account": "jhb_analytics",
                "activation_stage": None,
                "opening_balance": None,
                "current_balance": None,
                "month_opening_balance": None,
                "monthly_cap": None,
                "monthly_ledger_debit": None,
                "monthly_balance_delta": None,
                "monthly_effective_spend": None,
                "monthly_remaining": None,
                "reserve_floor": None,
                "reserve_remaining": None,
                "stage_cap": None,
                "run_allowance": None,
                "attribution_state": "unavailable",
                "kill_state": "not_applicable",
                "ledger_through": None,
                "limitation": "This credential remains operationally separate and has no funded-lane budget authority.",
            },
        ],
        "limitation": "Enumeration is not enablement. A positive allowance does not authorize a source or a run.",
    }


def test_monthly_sum_excludes_run_close_preflight_other_month_and_jhb():
    rows = [
        ledger_row("100"),
        ledger_row("25", phase="unattributed", event_type="attribution_gap"),
        ledger_row("125", phase="run_close", event_type="run_close"),
        ledger_row("0", phase="preflight", event_type="preflight"),
        ledger_row("900", recorded_at=datetime(2026, 7, 31, 23, 59, tzinfo=UTC)),
        ledger_row("700", lane="jhb_core"),
    ]
    lane = funded_lane(
        credit_budget.project_credit_budget(
            authority(current_balance="249975", rows=rows), now=NOW
        )
    )

    assert lane["monthly_ledger_debit"] == Decimal("125")
    assert lane["monthly_balance_delta"] == Decimal("125")
    assert lane["monthly_effective_spend"] == Decimal("125")


def test_larger_ledger_debit_wins_over_smaller_balance_delta():
    lane = funded_lane(
        credit_budget.project_credit_budget(
            authority(current_balance="250000", rows=[ledger_row("150")]), now=NOW
        )
    )

    assert lane["monthly_ledger_debit"] == Decimal("150")
    assert lane["monthly_balance_delta"] == Decimal("100")
    assert lane["monthly_effective_spend"] == Decimal("150")
    assert lane["monthly_remaining"] == Decimal("24850")


def test_reserve_remaining_subtracts_the_floor_from_current_balance():
    lane = funded_lane(credit_budget.project_credit_budget(authority(), now=NOW))

    assert lane["reserve_remaining"] == Decimal("25100")


def test_balance_increase_is_unknown_until_reconciled():
    reading = credit_budget.project_credit_budget(
        authority(current_balance="250101"), now=NOW
    )

    assert reading["state"] == "unknown"
    assert funded_lane(reading)["monthly_balance_delta"] is None
    assert funded_lane(reading)["run_allowance"] is None


def test_33_stage_three_runs_refuse_a_34th_full_run():
    rows = [ledger_row("750") for _ in range(33)]
    reading = credit_budget.project_credit_budget(
        authority(
            current_balance="225350",
            activation_stage=3,
            kill_state="passed",
            rows=rows,
        ),
        now=NOW,
    )
    lane = funded_lane(reading)

    assert lane["monthly_ledger_debit"] == Decimal("24750")
    assert lane["monthly_remaining"] == Decimal("250")
    assert lane["stage_cap"] == Decimal("750")
    assert lane["run_allowance"] == Decimal("250")
    assert reading["state"] == "blocked"


def test_missing_authority_preserves_nulls_instead_of_inventing_zero():
    reading = credit_budget.project_credit_budget(None, now=NOW)
    lane = funded_lane(reading)

    assert reading["state"] == "unknown"
    for field in (
        "opening_balance",
        "current_balance",
        "month_opening_balance",
        "monthly_ledger_debit",
        "monthly_balance_delta",
        "monthly_effective_spend",
        "monthly_remaining",
        "reserve_remaining",
        "run_allowance",
        "ledger_through",
    ):
        assert lane[field] is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(credential_lane="jhb_core"),
        lambda value: value.update(funding_account="jhb_analytics"),
        lambda value: value.update(api_key="secret-shaped"),
    ],
)
def test_authority_labels_and_fields_are_fixed(mutate):
    value = authority()
    mutate(value)

    reading = credit_budget.project_credit_budget(value, now=NOW)

    assert reading["state"] == "unknown"
    assert funded_lane(reading)["run_allowance"] is None


def test_positive_allowance_is_not_source_enablement():
    reading = credit_budget.project_credit_budget(
        authority(
            current_balance="249350",
            activation_stage=3,
            kill_state="passed",
            rows=[ledger_row("750")],
        ),
        now=NOW,
    )

    assert reading["state"] == "ready"
    assert funded_lane(reading)["run_allowance"] == Decimal("750")
    assert "enabled" not in repr(reading).lower()
