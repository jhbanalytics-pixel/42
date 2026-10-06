"""Boundary tests for receipt-backed historical recurrence."""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import asdict
from datetime import date

import pytest


def recurrence_module():
    name = "src.analysis.open_intelligence.historical_recurrence"
    assert importlib.util.find_spec(name) is not None, "historical recurrence module is missing"
    return importlib.import_module(name)


def occurrence(module, index, occurred_on, **overrides):
    values = {
        "occurrence_id": f"occurrence_{index:03d}",
        "pattern_id": "pattern_fixture",
        "occurred_on": occurred_on,
        "market": "za",
        "receipt_ids": (f"ev_{index:064x}",),
    }
    values.update(overrides)
    return module.RecurrenceOccurrence(**values)


def rules(module, **overrides):
    values = {
        "minimum_occurrences": 4,
        "maximum_absolute_interval_deviation_days": 3.0,
        "rule_version": "recurrence_rules_v1",
    }
    values.update(overrides)
    return module.RecurrenceRules(**values)


def detect(module, occurrences, **overrides):
    values = {
        "occurrences": occurrences,
        "pattern_id": "pattern_fixture",
        "market": "za",
        "as_of": date(2026, 2, 1),
        "rules": rules(module),
    }
    values.update(overrides)
    return module.detect_historical_recurrence(**values)


def test_four_evenly_spaced_occurrences_are_recurrent():
    module = recurrence_module()
    rows = tuple(
        occurrence(module, index, date(2026, 1, day)) for index, day in enumerate((1, 8, 15, 22), 1)
    )

    result = detect(module, rows)

    assert result == module.RecurrenceAssessment(
        state="recurrent",
        reason="bounded_intervals",
        pattern_id="pattern_fixture",
        market="za",
        as_of=date(2026, 2, 1),
        occurrence_count=4,
        first_occurrence=date(2026, 1, 1),
        last_occurrence=date(2026, 1, 22),
        median_interval_days=7.0,
        interval_days=(7, 7, 7),
        interval_range_days=(7, 7),
        maximum_absolute_interval_deviation_days=0.0,
        deviation_ceiling_days=3.0,
        receipt_ids=(f"ev_{1:064x}", f"ev_{2:064x}", f"ev_{3:064x}", f"ev_{4:064x}"),
        rule_version="recurrence_rules_v1",
        display_eligible=False,
    )


def test_one_extreme_gap_makes_recurrence_irregular():
    module = recurrence_module()
    rows = tuple(
        occurrence(module, index, occurred_on)
        for index, occurred_on in enumerate(
            (date(2026, 1, 1), date(2026, 1, 3), date(2026, 1, 10), date(2026, 1, 30)),
            1,
        )
    )

    result = detect(module, rows)

    assert result.state == "irregular"
    assert result.reason == "interval_deviation_above_ceiling"
    assert result.median_interval_days == 7.0
    assert result.interval_range_days == (2, 20)
    assert result.maximum_absolute_interval_deviation_days == 13.0


def test_three_occurrences_are_insufficient_not_zero_recurrence():
    module = recurrence_module()
    rows = tuple(
        occurrence(module, index, date(2026, 1, day)) for index, day in enumerate((1, 8, 15), 1)
    )

    result = detect(module, rows)

    assert result.state == "insufficient_history"
    assert result.reason == "minimum_occurrences_not_met"
    assert result.occurrence_count == 3
    assert result.median_interval_days is None


def test_comparable_occurrences_must_precede_as_of():
    module = recurrence_module()
    future = occurrence(module, 1, date(2026, 2, 1))

    with pytest.raises(ValueError, match="before as of"):
        detect(module, (future,))


def test_unrelated_malformed_record_is_ignored_but_comparable_one_fails_closed():
    module = recurrence_module()
    rows = tuple(
        occurrence(module, index, date(2026, 1, day)) for index, day in enumerate((1, 8, 15, 22), 1)
    )
    unrelated = {"pattern_id": "other_pattern", "market": "ng", "broken": True}
    comparable = {"pattern_id": "pattern_fixture", "market": "za", "broken": True}

    assert detect(module, (*rows, unrelated)).state == "recurrent"
    with pytest.raises(ValueError, match="comparable recurrence occurrence"):
        detect(module, (*rows, comparable))


def test_duplicate_occurrence_date_identity_and_receipts_fail_closed():
    module = recurrence_module()
    first = occurrence(module, 1, date(2026, 1, 1))
    duplicate_id = occurrence(module, 1, date(2026, 1, 8))
    duplicate_date = occurrence(module, 2, date(2026, 1, 1))
    duplicate_receipt = occurrence(module, 3, date(2026, 1, 15), receipt_ids=first.receipt_ids)

    with pytest.raises(ValueError, match="duplicate occurrence id"):
        detect(module, (first, duplicate_id))
    with pytest.raises(ValueError, match="duplicate occurrence date"):
        detect(module, (first, duplicate_date))
    with pytest.raises(ValueError, match="duplicate occurrence receipt"):
        detect(module, (first, duplicate_receipt))


def test_order_is_deterministic_and_display_cannot_be_enabled():
    module = recurrence_module()
    rows = tuple(
        occurrence(module, index, date(2026, 1, day)) for index, day in enumerate((1, 8, 15, 22), 1)
    )

    assert detect(module, rows) == detect(module, tuple(reversed(rows)))
    with pytest.raises(ValueError, match="display ineligible"):
        module.RecurrenceAssessment(**{**asdict(detect(module, rows)), "display_eligible": True})


def test_assessment_rejects_state_math_partial_stats_and_date_contradictions():
    module = recurrence_module()
    rows = tuple(
        occurrence(module, index, date(2026, 1, day)) for index, day in enumerate((1, 8, 15, 22), 1)
    )
    recurrent = asdict(detect(module, rows))
    insufficient = asdict(detect(module, rows[:3]))

    with pytest.raises(ValueError, match="deviation ceiling"):
        module.RecurrenceAssessment(
            **{
                **recurrent,
                "maximum_absolute_interval_deviation_days": 4.0,
                "interval_range_days": (3, 11),
            }
        )
    with pytest.raises(ValueError, match="incomplete"):
        module.RecurrenceAssessment(**{**insufficient, "median_interval_days": 7.0})
    with pytest.raises(ValueError, match="occurrence dates"):
        module.RecurrenceAssessment(**{**recurrent, "last_occurrence": date(2026, 2, 2)})
    with pytest.raises(ValueError, match="occurrence span"):
        module.RecurrenceAssessment(
            **{**recurrent, "last_occurrence": recurrent["first_occurrence"]}
        )
    with pytest.raises(ValueError, match="occurrence span"):
        module.RecurrenceAssessment(
            **{
                **recurrent,
                "last_occurrence": date(2026, 1, 18),
                "median_interval_days": 5.0,
                "interval_days": (1, 5, 10),
                "interval_range_days": (1, 10),
                "maximum_absolute_interval_deviation_days": 5.0,
                "deviation_ceiling_days": 5.0,
            }
        )
    with pytest.raises(ValueError, match="summary does not match"):
        module.RecurrenceAssessment(
            **{
                **recurrent,
                "median_interval_days": 100.0,
                "interval_range_days": (100, 100),
                "maximum_absolute_interval_deviation_days": 0.0,
            }
        )
    with pytest.raises(ValueError, match="deviation does not match"):
        module.RecurrenceAssessment(
            **{
                **recurrent,
                "state": "irregular",
                "reason": "interval_deviation_above_ceiling",
                "median_interval_days": 7.0,
                "interval_range_days": (4, 10),
                "maximum_absolute_interval_deviation_days": 3.0000000005,
            }
        )
    with pytest.raises(ValueError, match="receipt"):
        module.RecurrenceAssessment(**{**recurrent, "receipt_ids": ()})
    with pytest.raises(ValueError, match="receipt"):
        module.RecurrenceAssessment(
            **{**recurrent, "receipt_ids": (f"ev_{1:064x}", f"ev_{1:064x}")}
        )


def test_invalid_rules_market_dates_and_receipts_are_rejected():
    module = recurrence_module()

    with pytest.raises(ValueError, match="minimum occurrences"):
        rules(module, minimum_occurrences=3)
    with pytest.raises(ValueError, match="interval deviation"):
        rules(module, maximum_absolute_interval_deviation_days=-1)
    with pytest.raises(ValueError, match="lower-case market"):
        occurrence(module, 1, date(2026, 1, 1), market="ZA")
    with pytest.raises(ValueError, match="receipt"):
        occurrence(module, 1, date(2026, 1, 1), receipt_ids=())
