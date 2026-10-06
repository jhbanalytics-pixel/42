"""Additive prediction time contract v2: first evaluation day after real availability."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo

import pytest
from src.analysis.open_intelligence import predictions
from src.analysis.open_intelligence.predictions import (
    PREDICTION_TIME_CONTRACT_V2,
    build_signal_prediction_rows,
    first_evaluation_day,
)

from tests.unit.test_dynamic_signal_predictions import SIGNAL_B, build, candidate, promoted

# Frozen 13 Sep 2026 from the unedited module at 819905d over the fixtures in
# test_dynamic_signal_predictions.py. The v2 helper must not move a byte of it.
FROZEN_ROWS_SHA256 = "264c35881fb610086cca6c14615d658b0fa5a6edd5419c3b1d8f09ad441c943b"
FROZEN_ROWS_BYTES = 8252


class _OffsetLess(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str:
        return "offset-less"

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


def _encode(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _snapshot() -> bytes:
    cases = {
        "sustained_default": build(promoted()),
        "growing": build(promoted(candidate=candidate(velocity_score=0.8, breadth_score=0.6))),
        "sustained": build(promoted(candidate=candidate(velocity_score=0.68, breadth_score=0.62))),
        "peaked": build(promoted(candidate=candidate(velocity_score=0.68, breadth_score=0.5))),
        "fading": build(promoted(candidate=candidate(velocity_score=0.5, breadth_score=0.5))),
        "changed_run": build(promoted(candidate=candidate(run_id="run_fixture_002"))),
        "two_signals": build(
            promoted(),
            promoted(candidate=candidate(signal_id=SIGNAL_B), source_families=("search", "news")),
        ),
    }
    return json.dumps(cases, default=_encode, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def test_delayed_prediction_starts_after_real_availability():
    issued = datetime(2026, 9, 8, 6, 0, tzinfo=UTC)
    assert first_evaluation_day(issued) == date(2026, 9, 9)


def test_naive_prediction_time_is_refused():
    with pytest.raises(ValueError, match="availability_timezone_required"):
        first_evaluation_day(datetime(2026, 9, 8, 6, 0))


def test_offset_less_timezone_is_refused() -> None:
    with pytest.raises(ValueError, match="availability_timezone_required"):
        first_evaluation_day(datetime(2026, 9, 8, 6, 0, tzinfo=_OffsetLess()))


def test_first_evaluation_day_uses_the_utc_date_not_the_local_date() -> None:
    late_evening_west = datetime(2026, 9, 8, 23, 30, tzinfo=timezone(timedelta(hours=-2)))
    assert late_evening_west.date() == date(2026, 9, 8)
    assert first_evaluation_day(late_evening_west) == date(2026, 9, 10)
    early_morning_east = datetime(2026, 9, 9, 1, 30, tzinfo=timezone(timedelta(hours=2)))
    assert early_morning_east.date() == date(2026, 9, 9)
    assert first_evaluation_day(early_morning_east) == date(2026, 9, 9)


def test_contract_constant_is_versioned_and_no_row_producer_calls_the_helper() -> None:
    assert PREDICTION_TIME_CONTRACT_V2 == "open_intelligence_prediction_time_v2"
    source = inspect.getsource(predictions)
    calls = re.findall(r"first_evaluation_day\(", source)
    assert len(calls) == 1, "only the definition may mention the helper"
    assert "first_evaluation_day" not in inspect.getsource(predictions._row)
    assert "first_evaluation_day" not in inspect.getsource(build_signal_prediction_rows)
    assert "PREDICTION_TIME_CONTRACT_V2" not in inspect.getsource(predictions._row)


def test_existing_prediction_rows_are_byte_identical_to_the_frozen_snapshot() -> None:
    payload = _snapshot()
    assert len(payload) == FROZEN_ROWS_BYTES
    assert hashlib.sha256(payload).hexdigest() == FROZEN_ROWS_SHA256


def test_v2_rows_declare_start_and_horizon_after_real_availability() -> None:
    from src.analysis.open_intelligence.predictions import (
        PREDICTION_ROW_FIELDS,
        PREDICTION_ROW_FIELDS_V2,
        build_signal_prediction_rows_v2,
    )

    available_at = datetime(2026, 8, 26, 6, 50, tzinfo=UTC)
    (row,) = build_signal_prediction_rows_v2(
        promoted_signals=(promoted(),),
        predicted_at=datetime(2026, 8, 25, 6, 50, tzinfo=UTC),
        available_at=available_at,
        rules=predictions.PredictionRules(),
    )
    assert tuple(row) == PREDICTION_ROW_FIELDS_V2
    assert PREDICTION_ROW_FIELDS_V2[: len(PREDICTION_ROW_FIELDS)] == PREDICTION_ROW_FIELDS
    assert row["time_contract_version"] == PREDICTION_TIME_CONTRACT_V2
    assert row["available_at"] == available_at
    assert row["evaluation_window_start"] == date(2026, 8, 27)
    assert row["evaluation_horizon_days"] == 7
    assert row["evaluation_date"] == date(2026, 9, 2)
    assert row["invalidation_condition"].endswith("before 2026-09-02.")
    assert row["signal_date"] == date(2026, 8, 25)
    (v1_row,) = build(promoted())
    assert v1_row["evaluation_date"] == date(2026, 9, 1)
    assert row["prediction_id"] != v1_row["prediction_id"]


def test_v2_rows_refuse_naive_or_retrospective_availability() -> None:
    from src.analysis.open_intelligence.predictions import build_signal_prediction_rows_v2

    with pytest.raises(ValueError, match="availability_timezone_required"):
        build_signal_prediction_rows_v2(
            promoted_signals=(promoted(),),
            predicted_at=datetime(2026, 8, 25, 6, 50, tzinfo=UTC),
            available_at=datetime(2026, 8, 26, 6, 50),
            rules=predictions.PredictionRules(),
        )
    with pytest.raises(ValueError, match="available at cannot be before predicted at"):
        build_signal_prediction_rows_v2(
            promoted_signals=(promoted(),),
            predicted_at=datetime(2026, 8, 25, 6, 50, tzinfo=UTC),
            available_at=datetime(2026, 8, 25, 6, 49, tzinfo=UTC),
            rules=predictions.PredictionRules(),
        )


def test_v2_rows_leave_the_retained_v1_fixture_bytes_untouched() -> None:
    from src.analysis.open_intelligence.predictions import build_signal_prediction_rows_v2

    build_signal_prediction_rows_v2(
        promoted_signals=(promoted(),),
        predicted_at=datetime(2026, 8, 25, 6, 50, tzinfo=UTC),
        available_at=datetime(2026, 8, 26, 6, 50, tzinfo=UTC),
        rules=predictions.PredictionRules(),
    )
    payload = _snapshot()
    assert len(payload) == FROZEN_ROWS_BYTES
    assert hashlib.sha256(payload).hexdigest() == FROZEN_ROWS_SHA256
