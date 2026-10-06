import pytest

from ops.evaluation import forecast_cohort
from ops.evaluation.forecast_cohort import (
    observed_arrival_timing,
    review_arrival_cohort,
    score_arrival_cohort,
)

ISSUED = "2026-09-01T06:00:00Z"
DEFAULT_PROVENANCE = object()


def forecast(
    forecast_id,
    *,
    predicted,
    persistence,
    observed,
    issued=ISSUED,
    horizon="14d",
    provenance=DEFAULT_PROVENANCE,
    **extra,
):
    row = {
        "forecast_id": forecast_id,
        "forecast_issued": issued,
        "predicted_arrival": predicted,
        "persistence_arrival": persistence,
        "observed_arrival": observed,
        "horizon": horizon,
        "provenance": (
            {"origin": "za", "target": "ng", "carrier": "creator_set_1"}
            if provenance is DEFAULT_PROVENANCE
            else provenance
        ),
    }
    row.update(extra)
    return row


def test_empty_cohort_cannot_promote_forecasts():
    assert score_arrival_cohort([])["beats_persistence"] is False


def test_empty_cohort_reports_no_error_rates():
    result = score_arrival_cohort([])
    assert result == {
        "eligible": 0,
        "resolved": 0,
        "unresolved": 0,
        "forecast_error": None,
        "persistence_error": None,
        "beats_persistence": False,
    }


def test_kernel_scores_the_resolved_cohort_and_keeps_unresolved_counts():
    rows = [
        forecast("f1", predicted=True, persistence=False, observed=True),
        forecast("f2", predicted=False, persistence=False, observed=False),
        forecast("f3", predicted=True, persistence=True, observed=None),
    ]
    result = score_arrival_cohort(rows)
    assert result == {
        "eligible": 3,
        "resolved": 2,
        "unresolved": 1,
        "forecast_error": 0.0,
        "persistence_error": 0.5,
        "beats_persistence": True,
    }


def test_kernel_tie_does_not_beat_persistence():
    rows = [
        forecast("f1", predicted=True, persistence=True, observed=True),
        forecast("f2", predicted=False, persistence=False, observed=True),
    ]
    result = score_arrival_cohort(rows)
    assert result["forecast_error"] == result["persistence_error"] == 0.5
    assert result["beats_persistence"] is False


@pytest.mark.parametrize(
    "field", ["predicted_arrival", "persistence_arrival", "observed_arrival"]
)
@pytest.mark.parametrize("value", [1, "yes", 0.0])
def test_kernel_refuses_non_binary_resolved_arrivals(field, value):
    rows = [forecast("f1", predicted=True, persistence=False, observed=True)]
    rows[0][field] = value
    with pytest.raises(ValueError, match="binary_arrival_required"):
        score_arrival_cohort(rows)


def test_single_resolved_lucky_case_cannot_promote_without_the_minimum_sample():
    rows = [forecast("f1", predicted=True, persistence=False, observed=True)]
    result = review_arrival_cohort(rows, minimum_comparable_sample=3)
    assert result["beats_persistence"] is True
    assert result["resolved"] == 1
    assert result["minimum_comparable_sample"] == 3
    assert result["comparable_sample_met"] is False
    assert result["promotion_eligible"] is False


def test_minimum_comparable_sample_is_an_explicit_parameter():
    with pytest.raises(TypeError):
        review_arrival_cohort([])
    for bad in (0, -1, 2.0, True, "3", None):
        with pytest.raises(ValueError, match="minimum_comparable_sample_invalid"):
            review_arrival_cohort([], minimum_comparable_sample=bad)


def test_empty_cohort_review_is_an_explicit_incomplete_gate():
    result = review_arrival_cohort([], minimum_comparable_sample=1)
    assert result["eligible"] == 0
    assert result["resolved"] == 0
    assert result["comparable_sample_met"] is False
    assert result["promotion_eligible"] is False
    assert result["timing"]["observed_arrivals"] == 0
    assert result["timing"]["mean_absolute_days"] is None


def test_cohort_meeting_the_minimum_and_beating_persistence_is_promotable():
    rows = [
        forecast("f1", predicted=True, persistence=False, observed=True),
        forecast("f2", predicted=False, persistence=True, observed=False),
        forecast("f3", predicted=True, persistence=True, observed=True),
        forecast("f4", predicted=False, persistence=False, observed=None),
    ]
    result = review_arrival_cohort(rows, minimum_comparable_sample=3)
    assert result["resolved"] == 3
    assert result["unresolved"] == 1
    assert result["forecast_error"] == 0.0
    assert result["persistence_error"] == pytest.approx(2 / 3)
    assert result["comparable_sample_met"] is True
    assert result["promotion_eligible"] is True


def test_meeting_the_minimum_without_beating_persistence_cannot_promote():
    rows = [
        forecast("f1", predicted=True, persistence=True, observed=True),
        forecast("f2", predicted=False, persistence=False, observed=False),
        forecast("f3", predicted=True, persistence=True, observed=True),
    ]
    result = review_arrival_cohort(rows, minimum_comparable_sample=3)
    assert result["comparable_sample_met"] is True
    assert result["beats_persistence"] is False
    assert result["promotion_eligible"] is False


def test_duplicate_forecast_ids_are_rejected_before_the_kernel():
    rows = [
        forecast("f1", predicted=True, persistence=False, observed=True),
        forecast("f1", predicted=True, persistence=False, observed=True),
    ]
    with pytest.raises(ValueError, match="duplicate_forecast_id"):
        review_arrival_cohort(rows, minimum_comparable_sample=1)


@pytest.mark.parametrize(
    "issued",
    [
        None,
        "",
        "2026-09-01 06:00",
        "2026-09-01T06:00:00",
        1756706400,
        {"at": ISSUED},
        [ISSUED],
    ],
)
def test_forecast_issued_must_be_a_frozen_utc_instant(issued):
    rows = [
        forecast("f1", predicted=True, persistence=False, observed=True, issued=issued)
    ]
    with pytest.raises(ValueError, match="forecast_issued_immutable_required"):
        review_arrival_cohort(rows, minimum_comparable_sample=1)


def test_missing_fields_are_rejected():
    for field in forecast_cohort.REQUIRED_FIELDS:
        row = forecast("f1", predicted=True, persistence=False, observed=True)
        del row[field]
        with pytest.raises(ValueError, match="forecast_row_invalid"):
            review_arrival_cohort([row], minimum_comparable_sample=1)


def test_common_horizon_is_required():
    rows = [
        forecast("f1", predicted=True, persistence=False, observed=True, horizon="14d"),
        forecast("f2", predicted=True, persistence=False, observed=True, horizon="28d"),
    ]
    with pytest.raises(ValueError, match="horizon_mismatch"):
        review_arrival_cohort(rows, minimum_comparable_sample=1)
    for horizon in ("", None, 0, -3, True, ["14d"]):
        row = forecast(
            "f1", predicted=True, persistence=False, observed=True, horizon=horizon
        )
        with pytest.raises(ValueError, match="horizon_required"):
            review_arrival_cohort([row], minimum_comparable_sample=1)


@pytest.mark.parametrize("provenance", [{}, None, "", "reader", ["origin"]])
def test_provenance_is_required_on_every_row(provenance):
    rows = [
        forecast("f1", predicted=True, persistence=False, observed=True),
        forecast(
            "f2",
            predicted=True,
            persistence=False,
            observed=True,
            provenance=provenance,
        ),
    ]
    with pytest.raises(ValueError, match="provenance_required"):
        review_arrival_cohort(rows, minimum_comparable_sample=1)


def test_unresolved_rows_still_need_binary_forecasts():
    rows = [forecast("f1", predicted="yes", persistence=False, observed=None)]
    assert score_arrival_cohort(rows)["unresolved"] == 1
    with pytest.raises(ValueError, match="binary_arrival_required"):
        review_arrival_cohort(rows, minimum_comparable_sample=1)
    rows = [forecast("f1", predicted=True, persistence=False, observed="no")]
    with pytest.raises(ValueError, match="binary_arrival_required"):
        review_arrival_cohort(rows, minimum_comparable_sample=1)


def test_timing_error_is_computed_only_for_observed_arrivals():
    rows = [
        forecast(
            "f1",
            predicted=True,
            persistence=False,
            observed=True,
            predicted_arrival_date="2026-09-10",
            observed_arrival_date="2026-09-12",
        ),
        forecast(
            "f2",
            predicted=True,
            persistence=False,
            observed=False,
            predicted_arrival_date="2026-09-10",
            observed_arrival_date="2026-09-30",
        ),
        forecast(
            "f3",
            predicted=True,
            persistence=False,
            observed=None,
            predicted_arrival_date="2026-09-10",
            observed_arrival_date="2026-09-30",
        ),
        forecast("f4", predicted=False, persistence=False, observed=True),
        forecast(
            "f5",
            predicted=True,
            persistence=True,
            observed=True,
            predicted_arrival_date="2026-09-14",
            observed_arrival_date="2026-09-10",
        ),
    ]
    expected = {
        "label": forecast_cohort.TIMING_LABEL,
        "observed_arrivals": 3,
        "timed": 2,
        "untimed": 1,
        "mean_absolute_days": 3.0,
    }
    assert observed_arrival_timing(rows) == expected
    result = review_arrival_cohort(rows, minimum_comparable_sample=1)
    assert result["timing"] == expected
    assert result["timing"]["label"] == "conditional_on_observed_arrivals"


def test_timing_view_never_stands_in_for_the_cohort_score():
    rows = [
        forecast(
            "f1",
            predicted=True,
            persistence=True,
            observed=True,
            predicted_arrival_date="2026-09-10",
            observed_arrival_date="2026-09-10",
        )
    ]
    result = review_arrival_cohort(rows, minimum_comparable_sample=1)
    assert result["timing"]["mean_absolute_days"] == 0.0
    assert result["beats_persistence"] is False
    assert result["promotion_eligible"] is False


@pytest.mark.parametrize("value", ["2026/09/10", "", 20260910, "2026-09-10T06:00:00Z"])
def test_arrival_dates_must_be_iso_dates_when_present(value):
    rows = [
        forecast(
            "f1",
            predicted=True,
            persistence=False,
            observed=True,
            predicted_arrival_date=value,
            observed_arrival_date="2026-09-12",
        )
    ]
    with pytest.raises(ValueError, match="arrival_date_invalid"):
        review_arrival_cohort(rows, minimum_comparable_sample=1)
