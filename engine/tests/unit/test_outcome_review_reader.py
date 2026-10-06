"""Boundary tests for the immutable outcome review record reader."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from datetime import UTC, date, datetime

import pytest

_MODULE_NAME = "src.analysis.open_intelligence.outcome_review_reader"
review_reader = (
    importlib.import_module(_MODULE_NAME) if importlib.util.find_spec(_MODULE_NAME) else None
)

PREDICTION_ID = "pred_" + "a" * 64
OTHER_ID = "pred_" + "c" * 64
EVALUATION_DATE = date(2026, 9, 1)


def record_body(**overrides):
    body = {
        "prediction_id": PREDICTION_ID,
        "evaluation_date": "2026-09-01",
        "quality_findings": [{"reason": "incoherence", "found_at": "2026-08-30T10:00:00Z"}],
        "human_calibration": {"label": "noise", "reviewed_at": "2026-09-01T09:00:00Z"},
        "reviewed_by": "reviewer_pseudonym",
        "recorded_at": "2026-09-01T09:05:00Z",
    }
    body.update(overrides)
    return body


def record(**overrides):
    body = record_body(**overrides)
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = "orr_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {"record_id": digest, **body}


def document(*records):
    return {"contract_version": "outcome_review_records_v1", "records": list(records)}


def load(tmp_path, payload):
    path = tmp_path / "review_records.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return review_reader.load_outcome_review_records(path)


def test_reader_returns_typed_inputs_keyed_by_prediction_id(tmp_path):
    assert review_reader is not None, "outcome review reader module is missing"
    clean = record(prediction_id=OTHER_ID, quality_findings=[], human_calibration=None)
    records = load(tmp_path, document(record(), clean))

    assert records.contract_version == "outcome_review_records_v1"
    assert len(records.document_digest) == 64
    assert tuple(records.by_prediction) == (PREDICTION_ID, OTHER_ID)
    inputs = review_reader.review_inputs_for(
        records,
        prediction_ids=(PREDICTION_ID, OTHER_ID, "pred_" + "e" * 64),
        evaluation_date=EVALUATION_DATE,
    )
    assert inputs.reviewed_prediction_ids == (PREDICTION_ID, OTHER_ID)
    assert inputs.missing_prediction_ids == ("pred_" + "e" * 64,)
    assert inputs.missing_reason == "review_record_missing"
    finding = inputs.quality_findings_by_prediction[PREDICTION_ID][0]
    assert finding.reason == "incoherence"
    assert finding.found_at == datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    calibration = inputs.human_calibrations_by_prediction[PREDICTION_ID]
    assert calibration.label == "noise"
    assert calibration.reviewed_at == datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    assert OTHER_ID not in inputs.quality_findings_by_prediction
    assert OTHER_ID not in inputs.human_calibrations_by_prediction
    assert OTHER_ID in inputs.reviewed_prediction_ids


def test_record_for_another_evaluation_date_does_not_cover_the_prediction(tmp_path):
    records = load(tmp_path, document(record(evaluation_date="2026-09-02")))

    inputs = review_reader.review_inputs_for(
        records, prediction_ids=(PREDICTION_ID,), evaluation_date=EVALUATION_DATE
    )

    assert inputs.reviewed_prediction_ids == ()
    assert inputs.missing_prediction_ids == (PREDICTION_ID,)
    assert inputs.quality_findings_by_prediction == {}
    assert inputs.human_calibrations_by_prediction == {}


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda payload: payload.__setitem__("contract_version", "v0"), "review_records_contract"),
        (lambda payload: payload["records"][0].__setitem__("reviewed_by", "x"), "digest_mismatch"),
        (lambda payload: payload["records"][0].pop("reviewed_by"), "review_record_fields"),
        (lambda payload: payload["records"].append(dict(payload["records"][0])), "duplicate"),
        (
            lambda payload: payload["records"].__setitem__(
                0,
                record(quality_findings=[{"reason": "vibes", "found_at": "2026-08-30T10:00:00Z"}]),
            ),
            "quality",
        ),
        (
            lambda payload: payload["records"].__setitem__(
                0,
                record(human_calibration={"label": "noise", "reviewed_at": "2026-09-01T09:00:00"}),
            ),
            "timezone",
        ),
        (
            lambda payload: payload["records"].__setitem__(
                0,
                record(human_calibration={"label": "maybe", "reviewed_at": "2026-09-01T09:00:00Z"}),
            ),
            "label",
        ),
        (
            lambda payload: payload["records"].__setitem__(0, record(prediction_id="pred_x")),
            "prediction",
        ),
    ],
)
def test_malformed_tampered_or_duplicate_records_are_refused(tmp_path, mutate, code):
    payload = document(record())
    mutate(payload)

    with pytest.raises(review_reader.OutcomeReviewRefusal, match=code):
        load(tmp_path, payload)


def test_missing_or_unparseable_document_is_refused(tmp_path):
    with pytest.raises(review_reader.OutcomeReviewRefusal, match="review records document"):
        review_reader.load_outcome_review_records(tmp_path / "absent.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(review_reader.OutcomeReviewRefusal, match="review records document"):
        review_reader.load_outcome_review_records(broken)
