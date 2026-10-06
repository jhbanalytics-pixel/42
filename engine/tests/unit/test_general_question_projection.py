import copy

import pytest
from src.analysis.open_intelligence.general_question_projection import materialize_claim_text


def _reading(reading_id, value, unit):
    return {
        "reading_id": reading_id,
        "value": value,
        "unit": unit,
        "window": {"start": "2026-09-01", "end": "2026-09-05", "closed": True},
        "method": "Synthetic controlled measurement",
        "denominator": None,
        "source_receipt_ids": ["receipt_1"],
        "limitations": [],
    }


def test_materializes_declared_readings_once_in_segment_order_without_mutation():
    readings = {
        "reading_count": _reading("reading_count", 42, "records"),
        "reading_tiny": _reading("reading_tiny", 1e-15, "share"),
        "reading_label": _reading("reading_label", "rising", "trajectory"),
    }
    segments = [
        {"kind": "text", "text": "Model 42 observed "},
        {"kind": "reading", "reading_id": "reading_count"},
        {"kind": "text", "text": "; the smallest measured value was "},
        {"kind": "reading", "reading_id": "reading_tiny"},
        {"kind": "text", "text": ", classified as "},
        {"kind": "reading", "reading_id": "reading_label"},
        {"kind": "text", "text": "."},
    ]
    before_segments = copy.deepcopy(segments)
    before_readings = copy.deepcopy(readings)

    result = materialize_claim_text(
        segments,
        readings,
        reading_ids=["reading_count", "reading_tiny", "reading_label"],
    )

    assert result == (
        "Model 42 observed 42 records; the smallest measured value was "
        "1e-15 share, classified as rising trajectory."
    )
    assert segments == before_segments
    assert readings == before_readings


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "Unmeasured records"),
        (True, "true status"),
        (False, "false status"),
        (0, "0 records"),
        (-0.25, "-0.25 change"),
    ],
)
def test_formats_null_boolean_and_numeric_categories_without_coercion(value, expected):
    unit = "status" if isinstance(value, bool) else "change" if value == -0.25 else "records"
    readings = {"reading_1": _reading("reading_1", value, unit)}

    result = materialize_claim_text(
        [{"kind": "reading", "reading_id": "reading_1"}],
        readings,
        reading_ids=["reading_1"],
    )

    assert result == expected


@pytest.mark.parametrize(
    "segments",
    [
        [{"kind": "text", "text": "safe", "reading_id": "reading_1"}],
        [{"kind": "reading", "reading_id": "reading_1", "unit": "changed"}],
        [{"kind": "reading", "text": "42 records"}],
        [{"kind": "unknown", "text": "safe"}],
        [{"kind": "text", "text": 42}],
        "not-a-list",
    ],
)
def test_refuses_invalid_or_unit_bearing_segments(segments):
    with pytest.raises(ValueError, match="segment"):
        materialize_claim_text(segments, {}, reading_ids=[])


@pytest.mark.parametrize(
    ("segments", "readings", "reading_ids", "error"),
    [
        (
            [
                {"kind": "reading", "reading_id": "reading_1"},
                {"kind": "reading", "reading_id": "reading_1"},
            ],
            {"reading_1": _reading("reading_1", 1, "record")},
            ["reading_1"],
            "duplicate",
        ),
        (
            [{"kind": "text", "text": "No reading used."}],
            {"reading_1": _reading("reading_1", 1, "record")},
            ["reading_1"],
            "unused",
        ),
        (
            [{"kind": "reading", "reading_id": "reading_2"}],
            {"reading_2": _reading("reading_2", 2, "records")},
            ["reading_1"],
            "undeclared",
        ),
        (
            [{"kind": "reading", "reading_id": "reading_1"}],
            {},
            ["reading_1"],
            "unknown",
        ),
        (
            [{"kind": "reading", "reading_id": "reading_1"}],
            {"reading_1": _reading("reading_1", 1, "record")},
            ["reading_1", "reading_1"],
            "declared",
        ),
    ],
)
def test_refuses_duplicate_unused_undeclared_or_unknown_reading_refs(
    segments, readings, reading_ids, error
):
    with pytest.raises(ValueError, match=error):
        materialize_claim_text(segments, readings, reading_ids=reading_ids)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), object()])
def test_refuses_nonfinite_or_unsupported_reading_values(value):
    readings = {"reading_1": _reading("reading_1", value, "records")}

    with pytest.raises(ValueError, match="value"):
        materialize_claim_text(
            [{"kind": "reading", "reading_id": "reading_1"}],
            readings,
            reading_ids=["reading_1"],
        )


@pytest.mark.parametrize("unit", [None, 42, "", "   "])
def test_refuses_missing_or_invalid_declared_unit(unit):
    readings = {"reading_1": _reading("reading_1", 1, unit)}

    with pytest.raises(ValueError, match="unit"):
        materialize_claim_text(
            [{"kind": "reading", "reading_id": "reading_1"}],
            readings,
            reading_ids=["reading_1"],
        )


def test_refuses_mismatched_reading_identity():
    readings = {"reading_1": _reading("another_reading", 1, "record")}

    with pytest.raises(ValueError, match="identity"):
        materialize_claim_text(
            [{"kind": "reading", "reading_id": "reading_1"}],
            readings,
            reading_ids=["reading_1"],
        )


def test_missing_value_refuses_while_explicit_null_remains_unmeasured():
    missing = _reading("reading_1", None, "records")
    missing.pop("value")

    with pytest.raises(ValueError, match="value"):
        materialize_claim_text(
            [{"kind": "reading", "reading_id": "reading_1"}],
            {"reading_1": missing},
            reading_ids=["reading_1"],
        )

    assert (
        materialize_claim_text(
            [{"kind": "reading", "reading_id": "reading_1"}],
            {"reading_1": _reading("reading_1", None, "records")},
            reading_ids=["reading_1"],
        )
        == "Unmeasured records"
    )


def test_unhashable_segment_kind_refuses_with_controlled_value_error():
    with pytest.raises(ValueError, match="segment"):
        materialize_claim_text([{"kind": []}], {}, reading_ids=[])


@pytest.mark.parametrize(
    ("segments", "reading_ids", "error"),
    [
        ([], ["   "], "declared"),
        ([{"kind": "reading", "reading_id": "   "}], ["reading_1"], "identity"),
    ],
)
def test_whitespace_only_reading_ids_refuse_without_normalization(segments, reading_ids, error):
    with pytest.raises(ValueError, match=error):
        materialize_claim_text(segments, {}, reading_ids=reading_ids)
