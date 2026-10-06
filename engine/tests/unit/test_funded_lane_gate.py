"""Monthly and attribution gates for the funded SocialCrawl staging lane."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from src.analysis.open_intelligence.funded_lane import (
    APPROVED_CATALOG_DIGEST,
    APPROVED_METADATA_DIGEST,
    MONTHLY_CAP,
    STAGE_CAPS,
    FundedLaneGateResult,
    FundedLaneRules,
    MonthlySpendSnapshot,
    evaluate_funded_lane,
)

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


def rules(**overrides: object) -> FundedLaneRules:
    values = {
        "credential_lane": "ogilvy_funded",
        "opening_balance": Decimal("250100"),
        "monthly_cap": Decimal("25000"),
        "reserve_floor": Decimal("225000"),
        "stage_caps": {0: Decimal("0"), 1: Decimal("100"), 2: Decimal("250"), 3: Decimal("750")},
        "catalog_digest": APPROVED_CATALOG_DIGEST,
        "metadata_digest": APPROVED_METADATA_DIGEST,
    }
    values.update(overrides)
    return FundedLaneRules(**values)


def snapshot(**overrides: object) -> MonthlySpendSnapshot:
    values = {
        "as_of": NOW,
        "month_start": date(2026, 8, 1),
        "current_balance": Decimal("250100"),
        "month_opening_balance": Decimal("250100"),
        "monthly_ledger_debit": Decimal("0"),
        "monthly_vendor_reported": Decimal("0"),
        "unreconciled_execution_ids": (),
        "activation_stage": 1,
        "consecutive_complete_runs": 0,
        "runs_today": 0,
        "catalog_digest": APPROVED_CATALOG_DIGEST,
        "metadata_digest": APPROVED_METADATA_DIGEST,
    }
    values.update(overrides)
    return MonthlySpendSnapshot(**values)


def test_stage_one_opens_exactly_one_hundred_credit_allowance() -> None:
    result = evaluate_funded_lane(snapshot(), rules())

    assert result.allowed is True
    assert result.reasons == ()
    assert result.monthly_balance_delta == Decimal("0")
    assert result.monthly_effective_spend == Decimal("0")
    assert result.monthly_remaining == Decimal("25000")
    assert result.reserve_remaining == Decimal("25100")
    assert result.run_allowance == Decimal("100")
    assert result.attribution_gap_required == Decimal("0")
    assert result.attribution_state == "complete"


def test_thirty_three_full_stage_three_runs_refuse_call_thirty_four() -> None:
    spent = Decimal("0")
    result = None
    for run_number in range(1, 35):
        result = evaluate_funded_lane(
            snapshot(
                current_balance=Decimal("250100") - spent,
                monthly_ledger_debit=spent,
                monthly_vendor_reported=spent,
                activation_stage=3,
                consecutive_complete_runs=3,
            ),
            rules(),
        )
        if run_number <= 33:
            assert result.allowed is True
            spent += Decimal("750")

    assert result is not None
    assert result.monthly_effective_spend == Decimal("24750")
    assert result.monthly_remaining == Decimal("250")
    assert result.run_allowance == Decimal("250")
    assert result.allowed is False
    assert "full_stage_allowance_unavailable" in result.reasons


def test_exact_monthly_cap_and_reserve_floor_refuse() -> None:
    result = evaluate_funded_lane(
        snapshot(
            current_balance=Decimal("225100"),
            monthly_ledger_debit=Decimal("25000"),
            monthly_vendor_reported=Decimal("25000"),
            activation_stage=3,
            consecutive_complete_runs=3,
        ),
        rules(),
    )

    assert result.allowed is False
    assert result.run_allowance == Decimal("0")
    assert "monthly_cap_exhausted" in result.reasons


def test_exact_reserve_floor_emits_the_reserve_reason() -> None:
    result = evaluate_funded_lane(
        snapshot(
            current_balance=Decimal("225000"),
            monthly_ledger_debit=Decimal("25100"),
            monthly_vendor_reported=Decimal("25100"),
        ),
        rules(),
    )

    assert result.allowed is False
    assert "reserve_floor_reached" in result.reasons


def test_balance_delta_catches_missing_ledger_spend() -> None:
    result = evaluate_funded_lane(
        snapshot(
            current_balance=Decimal("249950"),
            monthly_ledger_debit=Decimal("100"),
            monthly_vendor_reported=Decimal("100"),
        ),
        rules(),
    )

    assert result.allowed is False
    assert result.monthly_balance_delta == Decimal("150")
    assert result.monthly_effective_spend == Decimal("150")
    assert result.attribution_gap_required == Decimal("50")
    assert result.attribution_state == "gap_detected"
    assert "attribution_gap_required" in result.reasons


def test_unclosed_execution_refuses_even_when_balance_and_ledger_match() -> None:
    result = evaluate_funded_lane(
        snapshot(unreconciled_execution_ids=("execution_001",)),
        rules(),
    )

    assert result.allowed is False
    assert result.attribution_state == "gap_detected"
    assert "unreconciled_execution" in result.reasons


def test_conservative_budget_debit_controls_cap_without_inventing_vendor_spend() -> None:
    result = evaluate_funded_lane(
        snapshot(
            current_balance=Decimal("250000"),
            monthly_ledger_debit=Decimal("150"),
            monthly_vendor_reported=Decimal("100"),
        ),
        rules(),
    )

    assert result.allowed is True
    assert result.monthly_effective_spend == Decimal("150")
    assert result.attribution_gap_required == Decimal("0")
    assert result.attribution_state == "conservative"


def test_stage_one_is_single_use_and_stage_two_requires_exact_reconciliation() -> None:
    repeated_stage_one = evaluate_funded_lane(
        snapshot(consecutive_complete_runs=1),
        rules(),
    )
    conservative_stage_two = evaluate_funded_lane(
        snapshot(
            current_balance=Decimal("250000"),
            monthly_ledger_debit=Decimal("150"),
            monthly_vendor_reported=Decimal("100"),
            activation_stage=2,
            consecutive_complete_runs=1,
        ),
        rules(),
    )

    assert repeated_stage_one.allowed is False
    assert "stage_one_complete" in repeated_stage_one.reasons
    assert conservative_stage_two.allowed is False
    assert "stage_two_reconciliation_not_exact" in conservative_stage_two.reasons


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"current_balance": None}, "balance_unavailable"),
        ({"current_balance": Decimal("250101")}, "balance_increase_unreconciled"),
        ({"current_balance": Decimal("224999")}, "reserve_floor_reached"),
        ({"catalog_digest": "f" * 64}, "catalog_mismatch"),
        ({"metadata_digest": "f" * 64}, "catalog_mismatch"),
        ({"activation_stage": 0}, "stage_zero"),
        ({"activation_stage": 2, "consecutive_complete_runs": 0}, "stage_two_unproven"),
        ({"activation_stage": 3, "consecutive_complete_runs": 2}, "stage_three_unproven"),
        (
            {"activation_stage": 3, "consecutive_complete_runs": 3, "runs_today": 1},
            "daily_run_limit",
        ),
    ],
)
def test_every_activation_and_identity_gate_fails_closed(
    overrides: dict[str, object], reason: str
) -> None:
    result = evaluate_funded_lane(snapshot(**overrides), rules())

    assert result.allowed is False
    assert reason in result.reasons


@pytest.mark.parametrize(
    "overrides",
    [
        {"monthly_ledger_debit": Decimal("99"), "monthly_vendor_reported": Decimal("100")},
        {"monthly_ledger_debit": Decimal("-1")},
        {"monthly_vendor_reported": Decimal("-1")},
        {"monthly_ledger_debit": 1.0},
        {"current_balance": 250_100.0},
        {"runs_today": True},
        {"month_start": date(2026, 7, 1)},
        {"as_of": datetime(2026, 8, 27, 10, 0)},
        {"unreconciled_execution_ids": ("execution_001", "execution_001")},
    ],
)
def test_malformed_or_inconsistent_monthly_inputs_are_rejected(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        snapshot(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"credential_lane": "jhb_core"},
        {"opening_balance": Decimal("0")},
        {"monthly_cap": Decimal("25001")},
        {"reserve_floor": Decimal("225001")},
        {"stage_caps": {0: Decimal("0"), 1: Decimal("100")}},
        {"stage_caps": {0: Decimal("0"), 1: Decimal("100"), 2: Decimal("250"), 3: Decimal("751")}},
        {"catalog_digest": "bad"},
        {"metadata_digest": "0" * 64},
    ],
)
def test_rules_are_exact_and_cannot_expand_the_approved_lane(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        rules(**overrides)


def test_cache_or_refund_zero_spend_stays_zero() -> None:
    result = evaluate_funded_lane(snapshot(), rules())

    assert result.monthly_ledger_debit == Decimal("0")
    assert result.monthly_vendor_reported == Decimal("0")


def test_new_month_uses_its_own_opening_balance_instead_of_lifetime_delta() -> None:
    result = evaluate_funded_lane(
        snapshot(
            as_of=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
            month_start=date(2026, 9, 1),
            month_opening_balance=Decimal("230000"),
            current_balance=Decimal("230000"),
            monthly_ledger_debit=Decimal("0"),
            monthly_vendor_reported=Decimal("0"),
        ),
        rules(),
    )

    assert result.monthly_balance_delta == Decimal("0")
    assert result.monthly_effective_spend == Decimal("0")
    assert result.monthly_remaining == Decimal("25000")


def test_public_gate_contract_is_frozen_and_has_no_audience_fields() -> None:
    item = snapshot()
    result = evaluate_funded_lane(item, rules())

    with pytest.raises(FrozenInstanceError):
        item.runs_today = 1
    with pytest.raises(FrozenInstanceError):
        result.allowed = False
    names = " ".join(
        (*rules().__dataclass_fields__, *item.__dataclass_fields__, *result.__dataclass_fields__)
    )
    assert "audience" not in names
    assert "genz" not in names.lower()


def test_public_result_rejects_forged_financial_and_stage_states() -> None:
    valid = evaluate_funded_lane(snapshot(), rules())
    values = asdict(valid)

    invalid_values = [
        {**values, "activation_stage": 99},
        {**values, "stage_cap": Decimal("-1")},
        {**values, "monthly_ledger_debit": Decimal("-1")},
        {**values, "monthly_vendor_reported": Decimal("999999")},
        {**values, "run_allowance": Decimal("999999")},
    ]
    for values in invalid_values:
        with pytest.raises(ValueError):
            FundedLaneGateResult(**values)


def test_missing_or_increased_balance_is_never_labelled_complete() -> None:
    missing = evaluate_funded_lane(snapshot(current_balance=None), rules())
    increased = evaluate_funded_lane(
        snapshot(current_balance=Decimal("250101")),
        rules(),
    )

    assert missing.attribution_state == "gap_detected"
    assert increased.attribution_state == "gap_detected"


def test_public_result_cannot_forge_blocked_state_as_allowed() -> None:
    for blocked in (
        evaluate_funded_lane(snapshot(activation_stage=0), rules()),
        evaluate_funded_lane(
            snapshot(
                current_balance=Decimal("225100"),
                monthly_ledger_debit=Decimal("25000"),
                monthly_vendor_reported=Decimal("25000"),
                activation_stage=3,
                consecutive_complete_runs=3,
            ),
            rules(),
        ),
    ):
        with pytest.raises(ValueError):
            FundedLaneGateResult(**{**asdict(blocked), "allowed": True})
        forged = {**asdict(blocked), "allowed": True, "reasons": ()}
        with pytest.raises(ValueError):
            FundedLaneGateResult(**forged)


def test_stage_allowances_are_exactly_the_three_accepted_credit_steps() -> None:
    assert dict(STAGE_CAPS) == {
        0: Decimal("0"),
        1: Decimal("100"),
        2: Decimal("250"),
        3: Decimal("750"),
    }
    daily_slice = MONTHLY_CAP / Decimal("30")
    assert daily_slice not in set(STAGE_CAPS.values())
    with pytest.raises(ValueError, match="stage caps differ"):
        rules(stage_caps={**dict(STAGE_CAPS), 4: daily_slice})
    with pytest.raises(ValueError, match="stage caps differ"):
        rules(stage_caps={0: Decimal("0"), 1: daily_slice, 2: Decimal("250"), 3: Decimal("750")})
    with pytest.raises(ValueError, match="activation stage is unsupported"):
        snapshot(activation_stage=4)
    for stage, cap in ((1, Decimal("100")), (2, Decimal("250")), (3, Decimal("750"))):
        result = evaluate_funded_lane(
            snapshot(activation_stage=stage, consecutive_complete_runs=3), rules()
        )
        assert result.stage_cap == cap
        assert result.run_allowance == cap


def test_month_rollover_starts_from_its_own_opening_and_refuses_a_stale_month() -> None:
    september = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="month start does not match"):
        snapshot(as_of=september, month_start=date(2026, 8, 1))
    rolled = evaluate_funded_lane(
        snapshot(
            as_of=september,
            month_start=date(2026, 9, 1),
            month_opening_balance=Decimal("249900"),
            current_balance=Decimal("249900"),
        ),
        rules(),
    )
    assert rolled.allowed is True
    assert rolled.monthly_balance_delta == Decimal("0")
    assert rolled.monthly_remaining == Decimal("25000")
    assert rolled.attribution_state == "complete"
    carried = evaluate_funded_lane(
        snapshot(
            as_of=september,
            month_start=date(2026, 9, 1),
            month_opening_balance=Decimal("250100"),
            current_balance=Decimal("249900"),
        ),
        rules(),
    )
    assert carried.allowed is False
    assert carried.attribution_gap_required == Decimal("200")
    assert "attribution_gap_required" in carried.reasons


def test_insufficient_reserve_shrinks_the_allowance_below_the_stage_and_refuses() -> None:
    thin = evaluate_funded_lane(
        snapshot(current_balance=Decimal("225050"), month_opening_balance=Decimal("225050")),
        rules(),
    )
    assert thin.reserve_remaining == Decimal("50")
    assert thin.run_allowance == Decimal("50")
    assert thin.allowed is False
    assert thin.reasons == ("full_stage_allowance_unavailable",)
    floor = evaluate_funded_lane(
        snapshot(current_balance=Decimal("225000"), month_opening_balance=Decimal("225000")),
        rules(),
    )
    assert floor.run_allowance == Decimal("0")
    assert {"reserve_floor_reached", "full_stage_allowance_unavailable"} <= set(floor.reasons)


def test_missing_balance_holds_the_allowance_at_zero_and_reports_a_gap() -> None:
    result = evaluate_funded_lane(snapshot(current_balance=None), rules())
    assert result.allowed is False
    assert "balance_unavailable" in result.reasons
    assert result.run_allowance == Decimal("0")
    assert result.reserve_remaining == Decimal("0")
    assert result.monthly_balance_delta is None
    assert result.attribution_state == "gap_detected"


def test_current_and_historical_catalog_pairs_keep_budget_admissible_without_cross_pairs() -> None:
    route_digest = "6cb7a55dbeec22d0c16bfe9c9e8fee6c2ba461e6e5ac08b762d8ca8046ffcff6"
    historical_metadata = "7d7152b2441f367417c74169a7bd4fb1a190a92763bbb43532167d5d4612fdfc"
    current_metadata = "501ff9eae79e1504840107432b58b66c19de70e44e2ebb16aea190e4d394c39a"

    for metadata_digest in (historical_metadata, current_metadata):
        gate = evaluate_funded_lane(
            snapshot(catalog_digest=route_digest, metadata_digest=metadata_digest),
            rules(),
        )
        assert "catalog_mismatch" not in gate.reasons

    for catalog_digest, metadata_digest in (
        ("0" * 64, historical_metadata),
        ("0" * 64, current_metadata),
        (route_digest, "0" * 64),
    ):
        gate = evaluate_funded_lane(
            snapshot(catalog_digest=catalog_digest, metadata_digest=metadata_digest),
            rules(),
        )
        assert "catalog_mismatch" in gate.reasons
