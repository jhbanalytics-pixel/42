"""Discovery parity rows bind one verified enrollment to one cutoff and market."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from src.analysis.open_intelligence.discovery_parity import (
    PARITY_REQUIRED_DAYS,
    DiscoveryParityError,
    add_discovery_parity_row,
    build_discovery_parity_row,
    evaluate_discovery_parity,
)
from src.analysis.open_intelligence.parity_enrollment import build_parity_enrollment

from tests.unit.test_parity_enrollment import current, legacy

MARKETS = ("za", "ng", "ke")
KNOWN_SET = "d" * 64
FIRST_DAY = date(2026, 9, 10)


def day_receipts(day: date, *, repeat: int = 0):
    completed = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    completed += timedelta(hours=6, minutes=38, seconds=repeat)
    shared = {
        "signal_date": day,
        "observation_start": day - timedelta(days=6),
        "observation_end": day,
        "completed_at": completed,
    }
    suffix = f"{day:%Y%m%d}_{repeat}"
    return (
        legacy(run_id=f"oi_{suffix}_legacy", **shared),
        current(run_id=f"oi_{suffix}_current", **shared),
    )


def day_enrollment(day: date, *, repeat: int = 0):
    left, right = day_receipts(day, repeat=repeat)
    enrolled = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return (
        build_parity_enrollment(
            legacy=left, current=right, enrolled_at=enrolled + timedelta(hours=7, minutes=30)
        ),
        left,
        right,
    )


def recall(numerator: int, denominator: int = 4) -> dict:
    return {"numerator": numerator, "denominator": denominator}


def row(day: date = FIRST_DAY, market: str = "za", *, repeat: int = 0, **overrides):
    record, left, right = day_enrollment(day, repeat=repeat)
    values = {
        "enrollment": record,
        "legacy_receipt": left,
        "current_receipt": right,
        "market": market,
        "known_event_set_digest": KNOWN_SET,
        "legacy_qualified_ids": (f"{market}|keyword|alpha", f"{market}|entity|beta"),
        "represented": {
            f"{market}|keyword|alpha": (
                {"relation": "membership", "signal_id": f"sig_{market}_1"},
            ),
            f"{market}|entity|beta": ({"relation": "topic_tag", "signal_id": f"sig_{market}_2"},),
        },
        "rejected": {f"{market}|keyword|gamma": "safety_auto_reject"},
        "legacy_recall": recall(3),
        "current_recall": recall(3),
    }
    values.update(overrides)
    return build_discovery_parity_row(**values)


def full_ledger(days: int, **overrides):
    held = ()
    for offset in range(days):
        for market in MARKETS:
            held = add_discovery_parity_row(
                held, row(FIRST_DAY + timedelta(days=offset), market, **overrides)
            )
    return held


def test_row_records_qualified_represented_rejected_and_recall():
    built = row()
    assert built.key == ("2026-09-10", "za")
    assert built.legacy_qualified_ids == ("za|entity|beta", "za|keyword|alpha")
    assert built.represented == (
        ("za|entity|beta", "topic_tag", "sig_za_2"),
        ("za|keyword|alpha", "membership", "sig_za_1"),
    )
    assert built.lost_ids == ()
    assert built.rejected == (("za|keyword|gamma", "safety_auto_reject"),)
    assert (built.legacy_recall, built.current_recall) == ((3, 4), (3, 4))
    assert built.recall_status == "held"
    assert built.legacy_run_id == "oi_20260910_0_legacy"
    assert built.current_run_id == "oi_20260910_0_current"
    assert len(built.row_digest) == 64


def test_row_refuses_an_enrollment_that_fails_verification():
    record, left, _right = day_enrollment(FIRST_DAY)
    _other_record, _other_left, other_right = day_enrollment(FIRST_DAY + timedelta(days=1))
    with pytest.raises(DiscoveryParityError, match="parity_enrollment_unverified"):
        row(enrollment=record, legacy_receipt=left, current_receipt=other_right)
    with pytest.raises(DiscoveryParityError, match="parity_enrollment_unverified"):
        row(enrollment=object())


def test_held_row_with_a_restated_field_refuses():
    built = row()
    for mutation in (
        {"lost_ids": ("za|keyword|alpha",)},
        {"represented": ()},
        {"rejected": ()},
        {"legacy_qualified_ids": ("za|keyword|alpha",)},
        {"recall_status": "held", "current_recall": (1, 4)},
        {"market": "ng"},
        {"cutoff": "2026-09-11"},
    ):
        with pytest.raises(DiscoveryParityError):
            add_discovery_parity_row((), replace(built, **mutation))


def test_held_row_restated_and_resealed_still_refuses():
    from src.analysis.open_intelligence import discovery_parity

    built = row()
    resealed = discovery_parity._sealed(replace(built, represented=(), lost_ids=(), row_digest=""))
    assert resealed.row_digest != built.row_digest
    with pytest.raises(DiscoveryParityError, match="parity_row_unproven"):
        add_discovery_parity_row((), resealed)


def test_identities_must_belong_to_the_row_market():
    with pytest.raises(DiscoveryParityError, match="identity_market_mismatch"):
        row(legacy_qualified_ids=("za|keyword|alpha", "ng|entity|beta"), represented={})
    with pytest.raises(DiscoveryParityError, match="identity_market_mismatch"):
        row(rejected={"ke|keyword|gamma": "safety_auto_reject"})


def test_market_outside_the_enrollment_scope_refuses():
    with pytest.raises(DiscoveryParityError, match="market_outside_enrollment_scope"):
        row(market="gh")


def test_second_row_for_one_cutoff_and_market_refuses_even_when_identical():
    held = add_discovery_parity_row((), row())
    with pytest.raises(DiscoveryParityError, match="parity_row_duplicate"):
        add_discovery_parity_row(held, row())


def test_a_repeated_run_of_one_day_neither_adds_a_row_nor_a_day():
    held = add_discovery_parity_row((), row())
    with pytest.raises(DiscoveryParityError, match="parity_row_duplicate"):
        add_discovery_parity_row(held, row(repeat=1))
    with pytest.raises(DiscoveryParityError, match="parity_cutoff_enrollment_conflict"):
        add_discovery_parity_row(held, row(market="ng", repeat=1))


def test_ten_complete_clean_days_pass():
    result = evaluate_discovery_parity(full_ledger(PARITY_REQUIRED_DAYS))
    assert result["eligible_days"] == 10
    assert result["row_count"] == 30
    assert result["incomplete_days"] == []
    assert result["lost_qualified"] == []
    assert result["recall_regressions"] == []
    assert result["recall_unknown"] == []
    assert result["markets"]["za"] == {
        "rows": 10,
        "qualified": 20,
        "represented": 20,
        "lost": 0,
        "rejected": 10,
    }
    assert result["verdict"] == "pass"


def test_nine_days_are_insufficient():
    result = evaluate_discovery_parity(full_ledger(9))
    assert result["eligible_days"] == 9
    assert result["verdict"] == "insufficient_days"


def test_a_day_missing_a_market_does_not_count():
    held = full_ledger(9)
    extra_day = FIRST_DAY + timedelta(days=9)
    held = add_discovery_parity_row(held, row(extra_day, "za"))
    held = add_discovery_parity_row(held, row(extra_day, "ng"))
    result = evaluate_discovery_parity(held)
    assert result["eligible_days"] == 9
    assert result["incomplete_days"] == [{"cutoff": "2026-09-19", "missing_markets": ["ke"]}]
    assert result["verdict"] == "insufficient_days"


def test_any_lost_qualified_item_fails_parity():
    held = full_ledger(PARITY_REQUIRED_DAYS)
    lossy = row(
        FIRST_DAY + timedelta(days=PARITY_REQUIRED_DAYS),
        "za",
        represented={"za|keyword|alpha": ({"relation": "membership", "signal_id": "sig_za_1"},)},
    )
    assert lossy.lost_ids == ("za|entity|beta",)
    result = evaluate_discovery_parity((*held, lossy))
    assert result["lost_qualified"] == [
        {"cutoff": "2026-09-20", "market": "za", "identity": "za|entity|beta"}
    ]
    assert result["verdict"] == "fail"


def test_a_ground_truth_regression_fails_parity():
    held = full_ledger(PARITY_REQUIRED_DAYS)
    worse = row(FIRST_DAY + timedelta(days=PARITY_REQUIRED_DAYS), current_recall=recall(2))
    assert worse.recall_status == "regressed"
    result = evaluate_discovery_parity((*held, worse))
    assert result["recall_regressions"] == [{"cutoff": "2026-09-20", "market": "za"}]
    assert result["verdict"] == "fail"


def test_unmeasured_recall_is_unknown_never_zero():
    held = full_ledger(PARITY_REQUIRED_DAYS)
    unmeasured = row(FIRST_DAY + timedelta(days=PARITY_REQUIRED_DAYS), current_recall=None)
    assert unmeasured.current_recall is None
    assert unmeasured.recall_status == "unknown"
    result = evaluate_discovery_parity((*held, unmeasured))
    assert result["recall_unknown"] == [{"cutoff": "2026-09-20", "market": "za"}]
    unknown_row = next(item for item in result["rows"] if item["cutoff"] == "2026-09-20")
    assert unknown_row["current_recall"] is None
    assert result["verdict"] == "unknown"


def test_recall_over_two_different_denominators_refuses():
    with pytest.raises(DiscoveryParityError, match="recall_denominator_differs"):
        row(current_recall=recall(3, 5))


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"rejected": {"za|keyword|alpha": "rejected"}}, "rejected_identity_is_qualified"),
        ({"rejected": {"za|keyword|gamma": ""}}, "rejection_reason_missing"),
        ({"represented": {"za|keyword|other": ()}}, "represented_outside_qualified"),
        ({"represented": {"za|keyword|alpha": ()}}, "represented_empty"),
        (
            {"represented": {"za|keyword|alpha": ({"relation": "guess", "signal_id": "s"},)}},
            "represented_relation_invalid",
        ),
        ({"legacy_qualified_ids": ("za|a", "za|a")}, "legacy_qualified_ids_invalid"),
        ({"legacy_qualified_ids": "za|a"}, "legacy_qualified_ids_invalid"),
        ({"legacy_recall": {"numerator": 5, "denominator": 4}}, "recall_invalid"),
        ({"legacy_recall": {"numerator": True, "denominator": 4}}, "recall_invalid"),
        ({"legacy_recall": 0}, "recall_invalid"),
        ({"known_event_set_digest": "short"}, "known_event_set_digest_invalid"),
    ],
)
def test_row_inputs_are_validated_not_repaired(override, code):
    with pytest.raises(DiscoveryParityError, match=code):
        row(**override)


def test_empty_ledger_counts_zero_days_and_does_not_pass():
    result = evaluate_discovery_parity(())
    assert result["eligible_days"] == 0
    assert result["verdict"] == "insufficient_days"


def scoped_day(day: date, scope: tuple[str, ...]):
    completed = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    completed += timedelta(hours=6)
    shared = {
        "signal_date": day,
        "observation_start": day - timedelta(days=6),
        "observation_end": day,
        "completed_at": completed,
        "market_scope": scope,
    }
    left = legacy(run_id=f"l_{day:%Y%m%d}", **shared)
    right = current(run_id=f"c_{day:%Y%m%d}", **shared)
    record = build_parity_enrollment(
        legacy=left, current=right, enrolled_at=completed + timedelta(hours=1)
    )
    return record, left, right


def scoped_ledger(scopes):
    held = ()
    for offset, scope in enumerate(scopes):
        day = FIRST_DAY + timedelta(days=offset)
        record, left, right = scoped_day(day, scope)
        for market in scope:
            held = add_discovery_parity_row(
                held,
                row(day, market, enrollment=record, legacy_receipt=left, current_receipt=right),
            )
    return held


def test_ten_single_market_days_never_pass_without_the_promised_markets():
    result = evaluate_discovery_parity(scoped_ledger([("za",)] * PARITY_REQUIRED_DAYS))
    assert result["eligible_days"] == 0
    assert result["promised_markets"] == ["ke", "ng", "za"]
    assert result["scope_mismatched_days"][0] == {
        "cutoff": "2026-09-10",
        "market_scope": ["za"],
    }
    assert len(result["scope_mismatched_days"]) == PARITY_REQUIRED_DAYS
    assert result["verdict"] == "insufficient_days"


def test_days_of_different_scopes_do_not_count_together():
    scopes = [("za", "ng", "ke")] * 9 + [("za", "ng")]
    result = evaluate_discovery_parity(scoped_ledger(scopes))
    assert result["eligible_days"] == 9
    assert result["scope_mismatched_days"] == [
        {"cutoff": "2026-09-19", "market_scope": ["za", "ng"]}
    ]
    assert result["verdict"] == "insufficient_days"


def test_the_promised_market_set_cannot_be_narrowed():
    held = scoped_ledger([("za",)] * PARITY_REQUIRED_DAYS)
    for narrowed in (("za",), ("za", "ng"), ("za", "ng", "ke")):
        with pytest.raises(TypeError, match="promised_markets"):
            evaluate_discovery_parity(held, promised_markets=narrowed)
    result = evaluate_discovery_parity(held)
    assert result["promised_markets"] == ["ke", "ng", "za"]
    assert result["eligible_days"] == 0
    assert result["verdict"] == "insufficient_days"


def test_evaluator_reverifies_a_resealed_row_read_back_directly():
    from src.analysis.open_intelligence import discovery_parity

    lossy = row(represented={"za|keyword|alpha": ({"relation": "membership", "signal_id": "s"},)})
    assert lossy.lost_ids == ("za|entity|beta",)
    forged = discovery_parity._sealed(replace(lossy, lost_ids=(), row_digest=""))
    with pytest.raises(DiscoveryParityError, match="parity_row_unproven"):
        evaluate_discovery_parity((forged,))


def test_evaluator_refuses_a_duplicate_key_read_back_directly():
    with pytest.raises(DiscoveryParityError, match="parity_row_duplicate"):
        evaluate_discovery_parity((row(), row(repeat=1)))


def test_evaluator_refuses_one_cutoff_bound_to_two_enrollments():
    with pytest.raises(DiscoveryParityError, match="parity_cutoff_enrollment_conflict"):
        evaluate_discovery_parity((row(market="za"), row(market="ng", repeat=1)))
