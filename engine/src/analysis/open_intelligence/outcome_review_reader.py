"""Immutable outcome review records, the only source of review inputs.

A record is one reviewer's finding set and calibration for one prediction and
one evaluation date. Its record_id is the digest of its own canonical body, so
a record whose bytes moved is refused rather than reinterpreted. A prediction
with no covering record is not reviewed; the evaluator keeps it unresolved as
review_record_missing instead of treating an empty map as a clean review.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType

from src.analysis.open_intelligence.outcome_evaluator import (
    REVIEW_RECORD_MISSING,
    HumanCalibration,
)
from src.analysis.open_intelligence.outcomes import OutcomeQualityFinding

REVIEW_RECORDS_CONTRACT_VERSION = "outcome_review_records_v1"
REVIEW_RECORD_FIELDS = (
    "record_id",
    "prediction_id",
    "evaluation_date",
    "quality_findings",
    "human_calibration",
    "reviewed_by",
    "recorded_at",
)
CALIBRATION_LABELS = frozenset({"noise", "signal"})
_PREDICTION_ID = re.compile(r"pred_[0-9a-f]{64}\Z")
_RECORD_ID = re.compile(r"orr_[0-9a-f]{64}\Z")


class OutcomeReviewRefusal(ValueError):
    """The review records document is outside the immutable record contract."""


@dataclass(frozen=True, slots=True)
class OutcomeReviewRecord:
    record_id: str
    prediction_id: str
    evaluation_date: date
    quality_findings: tuple[OutcomeQualityFinding, ...]
    human_calibration: HumanCalibration | None
    reviewed_by: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class OutcomeReviewRecords:
    contract_version: str
    document_digest: str
    records: tuple[OutcomeReviewRecord, ...]
    by_prediction: Mapping[str, OutcomeReviewRecord]


@dataclass(frozen=True, slots=True)
class OutcomeReviewInputs:
    quality_findings_by_prediction: Mapping[str, tuple[OutcomeQualityFinding, ...]]
    human_calibrations_by_prediction: Mapping[str, HumanCalibration]
    reviewed_prediction_ids: tuple[str, ...]
    missing_prediction_ids: tuple[str, ...]
    missing_reason: str = REVIEW_RECORD_MISSING


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def record_digest(body: Mapping[str, object]) -> str:
    """Digest of a record body without its record_id, prefixed for the record namespace."""
    return "orr_" + hashlib.sha256(_canonical(dict(body))).hexdigest()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OutcomeReviewRefusal(f"review record {field} is invalid")
    return value.strip()


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise OutcomeReviewRefusal(f"review record {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise OutcomeReviewRefusal(f"review record {field} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OutcomeReviewRefusal(f"review record {field} must be timezone aware")
    return parsed.astimezone(UTC)


def _date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise OutcomeReviewRefusal(f"review record {field} is invalid")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise OutcomeReviewRefusal(f"review record {field} is invalid") from error


def _finding(value: object) -> OutcomeQualityFinding:
    if not isinstance(value, Mapping) or set(value) != {"reason", "found_at"}:
        raise OutcomeReviewRefusal("review record quality finding is invalid")
    try:
        return OutcomeQualityFinding(
            found_at=_timestamp(value["found_at"], "quality finding found_at"),
            reason=value["reason"],
        )
    except ValueError as error:
        if isinstance(error, OutcomeReviewRefusal):
            raise
        raise OutcomeReviewRefusal("review record quality finding is invalid") from error


def _calibration(value: object) -> HumanCalibration | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"label", "reviewed_at"}:
        raise OutcomeReviewRefusal("review record human calibration is invalid")
    label = value["label"]
    if not isinstance(label, str) or label not in CALIBRATION_LABELS:
        raise OutcomeReviewRefusal("review record human calibration label is invalid")
    return HumanCalibration(
        label=label,
        reviewed_at=_timestamp(value["reviewed_at"], "human calibration reviewed_at"),
    )


def _record(value: object) -> OutcomeReviewRecord:
    if not isinstance(value, Mapping) or set(value) != set(REVIEW_RECORD_FIELDS):
        raise OutcomeReviewRefusal("review_record_fields_invalid")
    record_id = value["record_id"]
    if not isinstance(record_id, str) or _RECORD_ID.fullmatch(record_id) is None:
        raise OutcomeReviewRefusal("review record record_id is invalid")
    body = {field: value[field] for field in REVIEW_RECORD_FIELDS if field != "record_id"}
    try:
        expected = record_digest(body)
    except (TypeError, ValueError) as error:
        raise OutcomeReviewRefusal("review_record_fields_invalid") from error
    if expected != record_id:
        raise OutcomeReviewRefusal("review_record_digest_mismatch")
    prediction_id = value["prediction_id"]
    if not isinstance(prediction_id, str) or _PREDICTION_ID.fullmatch(prediction_id) is None:
        raise OutcomeReviewRefusal("review record prediction id is invalid")
    findings = value["quality_findings"]
    if not isinstance(findings, list):
        raise OutcomeReviewRefusal("review record quality findings are invalid")
    normalized_findings = tuple(_finding(item) for item in findings)
    if len(normalized_findings) != len(set(normalized_findings)):
        raise OutcomeReviewRefusal("review record quality findings are invalid")
    return OutcomeReviewRecord(
        record_id=record_id,
        prediction_id=prediction_id,
        evaluation_date=_date(value["evaluation_date"], "evaluation_date"),
        quality_findings=normalized_findings,
        human_calibration=_calibration(value["human_calibration"]),
        reviewed_by=_text(value["reviewed_by"], "reviewed_by"),
        recorded_at=_timestamp(value["recorded_at"], "recorded_at"),
    )


def parse_outcome_review_records(document: object) -> OutcomeReviewRecords:
    """Validate one review records document and index it by prediction id."""
    if not isinstance(document, Mapping) or set(document) != {"contract_version", "records"}:
        raise OutcomeReviewRefusal("review_records_contract_invalid")
    if document["contract_version"] != REVIEW_RECORDS_CONTRACT_VERSION:
        raise OutcomeReviewRefusal("review_records_contract_invalid")
    raw_records = document["records"]
    if not isinstance(raw_records, list):
        raise OutcomeReviewRefusal("review_records_contract_invalid")
    try:
        document_digest = hashlib.sha256(_canonical(dict(document))).hexdigest()
    except (TypeError, ValueError) as error:
        raise OutcomeReviewRefusal("review_records_contract_invalid") from error
    records = []
    by_prediction: dict[str, OutcomeReviewRecord] = {}
    for raw in raw_records:
        record = _record(raw)
        if record.prediction_id in by_prediction:
            raise OutcomeReviewRefusal("duplicate_review_record")
        by_prediction[record.prediction_id] = record
        records.append(record)
    return OutcomeReviewRecords(
        contract_version=REVIEW_RECORDS_CONTRACT_VERSION,
        document_digest=document_digest,
        records=tuple(records),
        by_prediction=MappingProxyType(by_prediction),
    )


def load_outcome_review_records(path: Path) -> OutcomeReviewRecords:
    """Read one immutable review records document from disk."""
    if not isinstance(path, Path) or not path.is_file():
        raise OutcomeReviewRefusal("review records document is missing")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise OutcomeReviewRefusal("review records document is malformed") from error
    return parse_outcome_review_records(document)


def review_inputs_for(
    records: OutcomeReviewRecords,
    *,
    prediction_ids: tuple[str, ...],
    evaluation_date: date,
) -> OutcomeReviewInputs:
    """Project the records covering one due cohort into evaluator inputs.

    A record covers a prediction only for the evaluation date it names. Every
    due prediction without a covering record is listed as missing with the
    named reason, never as an empty review.
    """
    if not isinstance(records, OutcomeReviewRecords):
        raise OutcomeReviewRefusal("review records are invalid")
    if isinstance(evaluation_date, datetime) or not isinstance(evaluation_date, date):
        raise OutcomeReviewRefusal("evaluation date must be a date")
    ordered = tuple(prediction_ids)
    if len(ordered) != len(set(ordered)) or any(
        not isinstance(item, str) or _PREDICTION_ID.fullmatch(item) is None for item in ordered
    ):
        raise OutcomeReviewRefusal("prediction ids are invalid")
    quality: dict[str, tuple[OutcomeQualityFinding, ...]] = {}
    human: dict[str, HumanCalibration] = {}
    reviewed = []
    missing = []
    for prediction_id in ordered:
        record = records.by_prediction.get(prediction_id)
        if record is None or record.evaluation_date != evaluation_date:
            missing.append(prediction_id)
            continue
        reviewed.append(prediction_id)
        if record.quality_findings:
            quality[prediction_id] = record.quality_findings
        if record.human_calibration is not None:
            human[prediction_id] = record.human_calibration
    return OutcomeReviewInputs(
        quality_findings_by_prediction=MappingProxyType(quality),
        human_calibrations_by_prediction=MappingProxyType(human),
        reviewed_prediction_ids=tuple(reviewed),
        missing_prediction_ids=tuple(missing),
    )
