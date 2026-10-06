"""Forecast versus persistence scoring for a closed arrival cohort (L03).

The kernel compares mean binary arrival error between the forecast and the
persistence baseline over the exact same resolved cohort. The wrapper checks
identity, issue time, horizon and provenance on every row before the kernel
runs, adds the labelled timing view for observed arrivals only, and applies
the approved minimum comparable sample to the promotion decision.
"""

import re
from datetime import date

UTC_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REQUIRED_FIELDS = (
    "forecast_id",
    "forecast_issued",
    "predicted_arrival",
    "persistence_arrival",
    "observed_arrival",
    "horizon",
    "provenance",
)
ARRIVAL_DATE_FIELDS = ("predicted_arrival_date", "observed_arrival_date")
TIMING_LABEL = "conditional_on_observed_arrivals"


def score_arrival_cohort(rows: list[dict]) -> dict:
    resolved = [row for row in rows if row["observed_arrival"] is not None]
    for row in resolved:
        if any(
            type(row[key]) is not bool
            for key in ("predicted_arrival", "persistence_arrival", "observed_arrival")
        ):
            raise ValueError("binary_arrival_required")
    count = len(resolved)
    forecast_errors = sum(
        row["predicted_arrival"] != row["observed_arrival"] for row in resolved
    )
    baseline_errors = sum(
        row["persistence_arrival"] != row["observed_arrival"] for row in resolved
    )
    return {
        "eligible": len(rows),
        "resolved": count,
        "unresolved": len(rows) - count,
        "forecast_error": forecast_errors / count if count else None,
        "persistence_error": baseline_errors / count if count else None,
        "beats_persistence": bool(count and forecast_errors < baseline_errors),
    }


def observed_arrival_timing(rows: list[dict]) -> dict:
    """Timing error over the labelled subset of actual observed arrivals.

    Only rows whose observed_arrival is True belong to the subset. A row in
    the subset is timed when it carries both arrival dates, and it is counted
    as untimed otherwise. Rows that did not arrive, or whose outcome is still
    unknown, never contribute, whatever dates they carry.
    """
    arrivals = [row for row in rows if row["observed_arrival"] is True]
    errors = []
    for row in arrivals:
        predicted = row.get("predicted_arrival_date")
        observed = row.get("observed_arrival_date")
        if predicted is None or observed is None:
            continue
        errors.append(
            abs((date.fromisoformat(observed) - date.fromisoformat(predicted)).days)
        )
    return {
        "label": TIMING_LABEL,
        "observed_arrivals": len(arrivals),
        "timed": len(errors),
        "untimed": len(arrivals) - len(errors),
        "mean_absolute_days": sum(errors) / len(errors) if errors else None,
    }


def _validate_row(row: dict, seen_ids: set) -> None:
    if not isinstance(row, dict) or any(field not in row for field in REQUIRED_FIELDS):
        raise ValueError("forecast_row_invalid")
    forecast_id = row["forecast_id"]
    if not isinstance(forecast_id, str) or not forecast_id:
        raise ValueError("forecast_id_required")
    if forecast_id in seen_ids:
        raise ValueError("duplicate_forecast_id")
    seen_ids.add(forecast_id)
    issued = row["forecast_issued"]
    if not isinstance(issued, str) or not UTC_INSTANT.match(issued):
        raise ValueError("forecast_issued_immutable_required")
    if any(
        type(row[key]) is not bool
        for key in ("predicted_arrival", "persistence_arrival")
    ):
        raise ValueError("binary_arrival_required")
    if (
        row["observed_arrival"] is not None
        and type(row["observed_arrival"]) is not bool
    ):
        raise ValueError("binary_arrival_required")
    horizon = row["horizon"]
    named = isinstance(horizon, str) and bool(horizon)
    counted = type(horizon) is int and horizon > 0
    if not (named or counted):
        raise ValueError("horizon_required")
    provenance = row["provenance"]
    if not isinstance(provenance, dict) or not provenance:
        raise ValueError("provenance_required")
    for field in ARRIVAL_DATE_FIELDS:
        value = row.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or not ISO_DATE.match(value):
            raise ValueError("arrival_date_invalid")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("arrival_date_invalid") from None


def review_arrival_cohort(rows: list[dict], *, minimum_comparable_sample: int) -> dict:
    """Validate the cohort, score it, and apply the minimum comparable sample.

    The minimum has no default because it is an approved promotion policy
    value, not a property of the data. A cohort is promotion eligible only
    when the forecast beats persistence and the resolved count reaches that
    minimum. An empty or undersized cohort is an explicit incomplete gate.
    Uncertainty review and the registered display gates remain outside this
    function.
    """
    if type(minimum_comparable_sample) is not int or minimum_comparable_sample < 1:
        raise ValueError("minimum_comparable_sample_invalid")
    if not isinstance(rows, list):
        raise ValueError("rows_list_required")
    seen_ids: set = set()
    for row in rows:
        _validate_row(row, seen_ids)
    if len({row["horizon"] for row in rows}) > 1:
        raise ValueError("horizon_mismatch")
    result = score_arrival_cohort(rows)
    result["timing"] = observed_arrival_timing(rows)
    result["minimum_comparable_sample"] = minimum_comparable_sample
    result["comparable_sample_met"] = result["resolved"] >= minimum_comparable_sample
    result["promotion_eligible"] = (
        result["beats_persistence"] and result["comparable_sample_met"]
    )
    return result
